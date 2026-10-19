"""The ``agentforge`` command line.

    agentforge --version
    agentforge agents list [--json]
    agentforge agents show <name>
    agentforge agents run <name> --data '{"text": "hi"}'   # or --input file.json, or stdin
    agentforge eval list
    agentforge eval run <agent|all> [--case NAME] [--min-pass-rate 0.8] [--out DIR]
    agentforge runs list [--agent NAME] [--status failed] [--limit 20] [--json]
    agentforge runs show <run_id> [--json]
    agentforge runs stats [--agent NAME] [--days 7] [--json]
    agentforge runs prune --days 30 [--yes]
    agentforge llm ping [--prompt "..."]
    agentforge config show
    agentforge serve [--host 127.0.0.1] [--port 8000]

Results go to stdout (pretty by default, ``--json`` for scripts); logs go to
stderr, so ``agentforge agents run ... --json | jq`` works.

Exit codes: 0 success, 1 the agent run failed (or an eval fell below its pass rate),
2 bad input or usage.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from agentforge import __version__
from agentforge.config import LLMProvider, LogFormat, Settings, get_settings
from agentforge.core.registry import UnknownAgentError, registry
from agentforge.core.schemas import Message, RunStatus
from agentforge.evals import CaseStatus, SuiteReport, UnknownCaseError, all_suites, run_suite
from agentforge.llm import LLMError, LLMRequest, create_provider
from agentforge.log import configure_logging
from agentforge.storage import RunStore, StorageError, open_store
from agentforge.storage.runs import database_url

EXIT_RUN_FAILED = 1
EXIT_USAGE = 2

app = typer.Typer(
    name="agentforge",
    help="AgentForge: run and inspect AI agents.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
agents_app = typer.Typer(help="List, inspect and run agents.", no_args_is_help=True)
llm_app = typer.Typer(help="Check the configured LLM provider.", no_args_is_help=True)
config_app = typer.Typer(help="Show the effective configuration.", no_args_is_help=True)
eval_app = typer.Typer(help="Run agents' golden eval cases.", no_args_is_help=True)
runs_app = typer.Typer(help="Browse the saved run history.", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(eval_app, name="eval")
app.add_typer(runs_app, name="runs")
app.add_typer(llm_app, name="llm")
app.add_typer(config_app, name="config")

out = Console()
err = Console(stderr=True)


def _version(value: bool) -> None:
    if value:
        out.print(f"agentforge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show info logs (default: warnings only).")
    ] = False,
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR.")
    ] = None,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Log as JSON lines.")] = False,
) -> None:
    """Configure logging once for every command (logs go to stderr)."""
    settings = get_settings()
    # Quiet by default; -v uses the configured level; --log-level wins over both.
    level = log_level or (settings.logging.level if verbose else "WARNING")
    updates: dict[str, Any] = {"level": level}
    if json_logs:
        updates["format"] = LogFormat.JSON
    try:
        log_settings = settings.logging.model_validate({**settings.logging.model_dump(), **updates})
    except ValidationError as exc:
        err.print(f"[red]Invalid option:[/] {escape(exc.errors()[0]['msg'])}")
        raise typer.Exit(EXIT_USAGE) from None
    configure_logging(settings.model_copy(update={"logging": log_settings}))


def _load_agents() -> None:
    registry.discover()


def _fail(message: str, code: int = EXIT_USAGE) -> typer.Exit:
    err.print(f"[red]Error:[/] {escape(message)}")
    return typer.Exit(code)


def _print_json(data: Any) -> None:
    out.print_json(json.dumps(data, default=str))


# --------------------------------------------------------------------------- agents


@agents_app.command("list")
def agents_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """List all available agents."""
    _load_agents()
    cards = registry.cards()
    if as_json:
        _print_json(
            [
                {"name": c.name, "version": c.version, "description": c.description, "tags": c.tags}
                for c in cards
            ]
        )
        return
    table = Table(title=f"{len(cards)} agent(s)", title_justify="left")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Version")
    table.add_column("Tags")
    table.add_column("Description")
    for card in cards:
        cells = (card.name, card.version, ", ".join(card.tags), card.description)
        table.add_row(*(escape(cell) for cell in cells))
    out.print(table)


@agents_app.command("show")
def agents_show(name: Annotated[str, typer.Argument(help="Agent name, e.g. 'echo'.")]) -> None:
    """Show an agent's description and its input/output schemas."""
    _load_agents()
    try:
        card = registry.get(name).card()
    except UnknownAgentError as exc:
        raise _fail(str(exc)) from None
    body = f"{escape(card.description)}\n\nversion {card.version}"
    out.print(Panel(body, title=escape(card.name), expand=False))
    for label, schema in (("Input", card.input_schema), ("Output", card.output_schema)):
        out.print(f"[bold]{label} schema[/]")
        out.print(Syntax(json.dumps(schema, indent=2), "json", word_wrap=True))


@agents_app.command("run")
def agents_run(
    name: Annotated[str, typer.Argument(help="Agent name, e.g. 'echo'.")],
    data: Annotated[
        str | None, typer.Option("--data", "-d", help="Input as a JSON string.")
    ] = None,
    input_file: Annotated[
        Path | None,
        typer.Option("--input", "-i", help="Input JSON file ('-' reads stdin).", allow_dash=True),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full RunResult as JSON.")
    ] = False,
) -> None:
    """Run an agent once and print its result."""
    _load_agents()
    try:
        agent = registry.create(name)
    except UnknownAgentError as exc:
        raise _fail(str(exc)) from None
    payload = _read_input(data, input_file)
    store = _optional_store()
    result = asyncio.run(agent.arun(payload))
    if store is not None:
        store.try_save(result, input=payload, source="cli")
        store.close()

    if as_json:
        out.print_json(result.model_dump_json())
    elif result.ok and result.output is not None:
        out.print(Syntax(result.output.model_dump_json(indent=2), "json", word_wrap=True))
        usage = result.usage
        err.print(
            f"[green]✓[/] {result.agent} succeeded in {result.duration_ms:.0f} ms · "
            f"{usage.total_tokens} tokens · ${usage.cost_usd:.4f} · run {result.run_id}"
        )
    else:
        error = result.error
        reason = f"({error.kind.value}): {error.message}" if error else ""
        err.print(f"[red]✗[/] {escape(result.agent)} failed {escape(reason)}")
    if not result.ok:
        raise typer.Exit(EXIT_RUN_FAILED)


def _read_input(data: str | None, input_file: Path | None) -> dict[str, Any]:
    if data is not None and input_file is not None:
        raise _fail("use either --data or --input, not both")
    if data is not None:
        raw, source = data, "--data"
    elif input_file is not None:
        if str(input_file) == "-":
            raw, source = sys.stdin.read(), "stdin"
        else:
            try:
                raw, source = input_file.read_text(encoding="utf-8"), str(input_file)
            except OSError as exc:
                raise _fail(f"cannot read {input_file}: {exc.strerror}") from None
    else:
        raw, source = "{}", "default"
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise _fail(f"{source} is not valid JSON: {exc.msg} (line {exc.lineno})") from None
    if not isinstance(parsed, dict):
        raise _fail(f'{source} must be a JSON object, e.g. \'{{"text": "hi"}}\'')
    return parsed


# --------------------------------------------------------------------------- runs


def _optional_store() -> RunStore | None:
    """The run store if one is configured. A broken one is a warning, not a reason to refuse
    to run the agent."""
    try:
        return open_store(get_settings())
    except StorageError as exc:
        err.print(f"[yellow]Warning:[/] runs will not be saved: {escape(str(exc))}")
        return None


def _required_store() -> RunStore:
    settings = get_settings()
    if settings.storage.url is None:
        raise _fail(
            "run history is off. Set AGENTFORGE_STORAGE__URL, e.g. sqlite:///.agentforge/runs.db "
            "or postgresql://user:password@localhost/agentforge"
        )
    try:
        store = open_store(settings)
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    assert store is not None  # noqa: S101 - the URL was checked above
    return store


@runs_app.command("list")
def runs_list(
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Only this agent.")] = None,
    status: Annotated[
        RunStatus | None, typer.Option("--status", "-s", help="succeeded or failed.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, max=500)] = 20,
    offset: Annotated[int, typer.Option("--offset", min=0)] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """List saved runs, newest first."""
    store = _required_store()
    try:
        page = store.list_runs(agent=agent, status=status, limit=limit, offset=offset)
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    finally:
        store.close()
    if as_json:
        _print_json(page.model_dump(mode="json"))
        return
    table = Table(
        title=f"{len(page.runs)} of {page.total} run(s)", title_justify="left", show_lines=False
    )
    table.add_column("Run", no_wrap=True)
    table.add_column("Agent", style="bold cyan")
    table.add_column("Status")
    table.add_column("Started (UTC)", no_wrap=True)
    table.add_column("Time", justify="right", no_wrap=True)
    table.add_column("Tokens", justify="right", no_wrap=True)
    table.add_column("Cost", justify="right", no_wrap=True)
    for r in page.runs:
        outcome = "[green]ok[/]" if r.status is RunStatus.SUCCEEDED else f"[red]{r.error_kind}[/]"
        table.add_row(
            r.run_id,
            escape(r.agent),
            outcome,
            r.started_at.strftime("%m-%d %H:%M:%S"),
            f"{r.duration_ms / 1000:.1f}s",
            str(r.input_tokens + r.output_tokens),
            f"${r.cost_usd:.4f}",
        )
    out.print(table)


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="A run id from `agentforge runs list`.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the whole record as JSON.")
    ] = False,
) -> None:
    """Show one saved run: input, output or error, usage and timing."""
    store = _required_store()
    try:
        record = store.get(run_id)
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    finally:
        store.close()
    if record is None:
        raise _fail(f"no run with id '{run_id}'")
    if as_json:
        _print_json(record.model_dump(mode="json"))
        return
    ok = record.status is RunStatus.SUCCEEDED
    header = (
        f"{'[green]succeeded[/]' if ok else '[red]failed[/]'} · {record.input_tokens} in / "
        f"{record.output_tokens} out tokens · ${record.cost_usd:.4f} · "
        f"{record.duration_ms / 1000:.1f}s · {record.started_at:%Y-%m-%d %H:%M:%S} UTC · "
        f"via {record.source}"
    )
    out.print(
        Panel(
            header, title=f"{escape(record.agent)} {escape(record.agent_version)} · {record.run_id}"
        )
    )
    if record.error_kind:
        out.print(f"[red]{record.error_kind}[/] {escape(record.error_type or '')}")
        out.print(escape(record.error_message or ""), highlight=False)
    for label, body in (("Input", record.input), ("Output", record.output)):
        if body is not None:
            out.print(f"[bold]{label}[/]")
            out.print(Syntax(json.dumps(body, indent=2), "json", word_wrap=True))
    if record.input is None:
        out.print("[dim]input was not stored[/]")


@runs_app.command("stats")
def runs_stats(
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="Only this agent.")] = None,
    days: Annotated[
        int | None, typer.Option("--days", min=1, help="Only runs from the last N days.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Per-agent totals: runs, success rate, average time, tokens and cost."""
    store = _required_store()
    try:
        stats = store.stats(agent=agent, since=timedelta(days=days) if days else None)
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    finally:
        store.close()
    if as_json:
        _print_json([s.model_dump(mode="json") for s in stats])
        return
    table = Table(title=f"{len(stats)} agent(s)", title_justify="left")
    table.add_column("Agent", style="bold cyan", no_wrap=True)
    for column in ("Runs", "Failed", "Success", "Avg time", "Tokens", "Cost"):
        table.add_column(column, justify="right")
    for s in stats:
        table.add_row(
            escape(s.agent),
            str(s.runs),
            str(s.failed),
            f"{s.success_rate:.0%}",
            f"{s.avg_duration_ms / 1000:.1f}s",
            str(s.input_tokens + s.output_tokens),
            f"${s.cost_usd:.4f}",
        )
    out.print(table)
    out.print(
        f"[bold]{sum(s.runs for s in stats)} run(s)[/] · "
        f"{sum(s.input_tokens + s.output_tokens for s in stats)} tokens · "
        f"${sum(s.cost_usd for s in stats):.4f}"
    )


@runs_app.command("prune")
def runs_prune(
    days: Annotated[int, typer.Option("--days", min=0, help="Delete runs older than N days.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
) -> None:
    """Delete old runs (inputs can contain source code you may not want to keep)."""
    store = _required_store()
    try:
        if not yes and not typer.confirm(f"Delete every saved run older than {days} day(s)?"):
            raise typer.Exit(EXIT_USAGE)
        deleted = store.prune(timedelta(days=days))
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    finally:
        store.close()
    out.print(f"Deleted {deleted} run(s).")


# --------------------------------------------------------------------------- serve


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option("--host", help="Interface to listen on.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Port to listen on.")] = None,
) -> None:
    """Start the HTTP API (docs at /docs). Loopback only unless an API key is set."""
    import uvicorn

    from agentforge.api import create_app

    settings = get_settings()
    api = settings.api.model_copy(
        update={"host": host or settings.api.host, "port": port or settings.api.port}
    )
    if not api.is_loopback and api.api_key is None:
        raise _fail(
            f"refusing to listen on {api.host} without an API key: agents run the code they are "
            "sent. Set AGENTFORGE_API__API_KEY, or use the default 127.0.0.1"
        )
    served = settings.model_copy(update={"api": api})
    try:
        application = create_app(served)
    except StorageError as exc:
        raise _fail(str(exc), EXIT_RUN_FAILED) from None
    history = "saving runs" if served.storage.url else "run history is off"
    err.print(f"Serving on http://{api.host}:{api.port} (interactive docs at /docs; {history})")
    uvicorn.run(application, host=api.host, port=api.port, log_config=None)


# --------------------------------------------------------------------------- eval


@eval_app.command("list")
def eval_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """List the agents that have golden eval cases."""
    suites = all_suites()
    rows = {name: sorted(c.name for c in suite.load_cases()) for name, suite in suites.items()}
    if as_json:
        _print_json(rows)
        return
    table = Table(title=f"{len(rows)} eval suite(s)", title_justify="left")
    table.add_column("Agent", style="bold cyan", no_wrap=True)
    table.add_column("Cases", justify="right")
    table.add_column("Case names")
    for name, cases in rows.items():
        table.add_row(escape(name), str(len(cases)), escape(", ".join(cases)))
    out.print(table)


@eval_app.command("run")
def eval_run(
    agent: Annotated[str, typer.Argument(help="Agent name, or 'all'.")],
    case: Annotated[
        list[str] | None, typer.Option("--case", "-c", help="Only this case (repeatable).")
    ] = None,
    min_pass_rate: Annotated[
        float, typer.Option("--min-pass-rate", min=0.0, max=1.0, help="Exit 1 below this rate.")
    ] = 0.8,
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, max=8, help="Cases to run at once.")
    ] = 1,
    out_dir: Annotated[Path, typer.Option("--out", help="Folder for the JSON reports.")] = Path(
        ".cache/evals"
    ),
    save: Annotated[bool, typer.Option("--save/--no-save", help="Write JSON reports.")] = True,
    allow_fake: Annotated[
        bool, typer.Option("--allow-fake", help="Allow the fake provider (tests and demos).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the summaries as JSON.")] = False,
) -> None:
    """Run golden cases against the configured model and report pass rates.

    Costs real tokens: one agent run per case. Needs a real provider and key.
    """
    suites = all_suites()
    if agent != "all" and agent not in suites:
        raise _fail(f"no eval suite for '{agent}' (available: {', '.join(suites)}, all)")
    if agent == "all" and case:
        raise _fail("--case needs a single agent, not 'all'")
    names = list(suites) if agent == "all" else [agent]

    settings = get_settings()
    if settings.llm.provider is LLMProvider.FAKE and not allow_fake:
        raise _fail(
            "the fake provider cannot solve eval cases. Set AGENTFORGE_LLM__PROVIDER "
            "(and the API key) in .env, or pass --allow-fake to test the harness"
        )
    try:
        provider = create_provider(settings)
    except LLMError as exc:
        raise _fail(f"{type(exc).__name__}: {exc}") from None
    _load_agents()

    reports: list[SuiteReport] = []
    for name in names:
        instance = registry.create(name, llm=provider)
        try:
            report = asyncio.run(
                run_suite(
                    suites[name],
                    instance,
                    cases=case or None,
                    concurrency=concurrency,
                    provider=settings.llm.provider.value,
                    model=settings.llm.model,
                )
            )
        except UnknownCaseError as exc:
            raise _fail(str(exc)) from None
        reports.append(report)
        if not as_json:
            _print_report(report)
        if save:
            path = report.save(out_dir)
            err.print(f"[dim]saved {escape(str(path))}[/]")

    if as_json:
        _print_json([r.summary() for r in reports])
    else:
        _print_totals(reports)
    if any(r.pass_rate < min_pass_rate for r in reports):
        raise typer.Exit(EXIT_RUN_FAILED)


_STATUS_STYLE = {
    CaseStatus.PASSED: "[green]pass[/]",
    CaseStatus.FAILED: "[red]FAIL[/]",
    CaseStatus.ERROR: "[red]ERROR[/]",
}


def _print_report(report: SuiteReport) -> None:
    table = Table(
        title=f"{report.agent}: {report.passed}/{report.total} passed", title_justify="left"
    )
    table.add_column("Case", no_wrap=True)
    table.add_column("Result")
    table.add_column("Score", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Time", justify="right")
    table.add_column("Problems")
    for r in report.results:
        problems = r.error or "; ".join(f"{c.name} ({c.detail})" for c in r.failed_checks())
        table.add_row(
            escape(r.case),
            _STATUS_STYLE[r.status],
            f"{r.score:.2f}",
            str(r.tokens),
            f"${r.cost_usd:.4f}",
            f"{r.duration_ms / 1000:.1f}s",
            escape(problems),
        )
    out.print(table)


def _print_totals(reports: list[SuiteReport]) -> None:
    passed, total = sum(r.passed for r in reports), sum(r.total for r in reports)
    cost, tokens = sum(r.cost_usd for r in reports), sum(r.tokens for r in reports)
    out.print(f"[bold]{passed}/{total} cases passed[/] · {tokens} tokens · ${cost:.4f}")


# --------------------------------------------------------------------------- llm


@llm_app.command("ping")
def llm_ping(
    prompt: Annotated[
        str, typer.Option("--prompt", "-p", help="What to send.")
    ] = "Reply with the single word: pong",
) -> None:
    """Send one short request to the configured provider and show the reply, tokens and cost."""
    settings = get_settings()
    try:
        provider = create_provider(settings)
        response = asyncio.run(
            provider.complete(LLMRequest(messages=[Message.user(prompt)], max_tokens=256))
        )
    except LLMError as exc:
        raise _fail(f"{type(exc).__name__}: {exc}", EXIT_RUN_FAILED) from None
    out.print(response.text, markup=False, highlight=False)
    usage = response.usage
    err.print(
        f"[green]✓[/] {response.provider}/{response.model} · {response.latency_ms:.0f} ms · "
        f"{usage.input_tokens} in / {usage.output_tokens} out · ${usage.cost_usd:.6f}"
        + (" · cached" if response.cached else "")
    )


# --------------------------------------------------------------------------- config


@config_app.command("show")
def config_show() -> None:
    """Print the effective settings. API keys are shown only as set / not set."""
    _print_json(_public_settings(get_settings()))


def _public_settings(settings: Settings) -> dict[str, Any]:
    data = settings.model_dump(mode="json", exclude={"anthropic_api_key", "openai_api_key"})
    data["anthropic_api_key"] = "set" if settings.anthropic_api_key else "not set"
    data["openai_api_key"] = "set" if settings.openai_api_key else "not set"
    data["api"]["api_key"] = "set" if settings.api.api_key else "not set"
    data["storage"]["url"] = _masked_url(settings)
    return data


def _masked_url(settings: Settings) -> str:
    """The database URL with its password hidden, or 'off'."""
    url = settings.storage.url
    if url is None:
        return "off"
    try:
        return database_url(url.get_secret_value()).render_as_string(hide_password=True)
    except StorageError:
        return "set (invalid)"


if __name__ == "__main__":  # pragma: no cover
    app()
