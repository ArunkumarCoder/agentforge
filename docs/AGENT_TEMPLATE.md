# Writing an agent

Every LLM-backed agent follows the same layout. Copy `src/agentforge/agents/code_review/` as a starting point.

```
src/agentforge/agents/<agent_name>/
├── __init__.py     # re-exports the agent class and schemas
├── agent.py        # @register_agent class XAgent(LLMAgent[XInput, XOutput])
├── schema.py       # Pydantic input/output models (the agent's contract)
├── prompt.md       # system prompt; {{output_schema}} is filled in automatically
├── scoring.py      # how one output is scored against a golden case (deterministic)
└── evals/*.json    # golden cases: {name, description, input, expect}
```

## The agent class

```python
@register_agent
class XAgent(LLMAgent[XInput, XOutput]):
    name = "x"  # kebab-case, unique
    description = "One sentence."  # shown in `agentforge agents list` and used for routing
    input_model = XInput
    output_model = XOutput
    max_steps = 6  # tool-loop guard

    def build_user_message(self, data, ctx) -> str: ...  # the task, as the model sees it
    def build_tools(self, data, ctx) -> ToolRegistry | None: ...
    def postprocess(self, output, data, ctx) -> XOutput: ...  # enforce rules the model may miss
```

`LLMAgent` renders the prompt, runs the tool loop, parses the JSON answer into `XOutput`, and sends validation errors back **once** for a repair before failing the run with `output_validation`.

## Definition of Done

- [ ] Input and output are Pydantic models with field descriptions.
- [ ] `prompt.md` states priorities, rules, the severity or quality scale, and the output format.
- [ ] `postprocess` enforces anything that must be true regardless of the model (for example, the verdict follows from the findings, and only files from the diff are referenced).
- [ ] At least 5 golden cases in `evals/`, including at least one "nothing to report" case.
- [ ] Offline tests with `FakeProvider` cover success, repair, post-processing and scoring.
- [ ] Live run: `make test-live` passes at least 4 of 5 golden cases.
- [ ] Listed in `docs/AGENTS.md` and runnable with `agentforge agents run <name>`.
