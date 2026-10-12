"""The ``agentforge`` command line.

    agentforge --version
    agentforge agents list [--json]
    agentforge agents show <name>
    agentforge agents run <name> --data '{"text": "hi"}'   # or --input file.json, or stdin
    agentforge eval list
    agentforge eval run <agent|all> [--case NAME] [--min-pass-rate 0.8] [--out DIR]
    agentforge llm ping [--prompt "..."]
    agentforge config show

Results go to stdout (pretty by default, ``--json`` for scripts); logs go to
stderr, so ``agentforge agents run ... --json | jq`` works.

Exit codes: 0 success, 1 the agent run failed (or an eval fell below its pass rate),
2 bad input or usage.
"""

from __future__ import annotations

import asyncio
import json
import sys
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
from agentforge.core.schemas import Message
from agentforge.evals import CaseStatus, SuiteReport, UnknownCaseError, all_suites, run_suite
from agentforge.llm import LLMError, LLMRequest, create_provider
from agentforge.log import configure_logging

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
app.add_typer(agents_app, name="agents")
app.add_typer(eval_app, name="eval")
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
    result = asyncio.run(agent.arun(payload))

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
    return data


if __name__ == "__main__":  # pragma: no cover
    app()
