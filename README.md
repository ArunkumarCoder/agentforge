# AgentForge

> 25 Practical AI Agents for Software Engineering, Business, Data & Automation.

AgentForge is an open-source AI agent engineering laboratory containing 25 specialized agents built around a shared runtime for tool use, memory, planning, evaluation, and multi-agent orchestration.

The goal is not simply to demonstrate 25 prompts.

The project explores how practical AI agents can be designed, tested, evaluated, secured, and orchestrated into reliable software systems.

## ✨ What is AgentForge?

AgentForge contains 25 specialized AI agents covering:

* Software engineering
* Code analysis
* Testing
* Security
* Data analysis
* RAG
* Research
* Product management
* Business analysis
* SEO
* DevOps
* QA
* GitHub automation
* Multi-agent orchestration

### Architecture

```text
                         User
                          │
                 ┌────────▼────────┐
                 │ CLI / REST / UI │
                 └────────┬────────┘
                          │
                 ┌────────▼────────┐
                 │  Orchestrator   │
                 └────────┬────────┘
                          │
             ┌────────────┼────────────┐
             │            │            │
             ▼            ▼            ▼
          Agent A      Agent B      Agent C
             │            │            │
             └────────────┼────────────┘
                          ▼
                  ┌───────────────┐
                  │ Agent Runtime │
                  ├───────────────┤
                  │ LLM           │
                  │ Tools         │
                  │ Memory        │
                  │ Guardrails    │
                  │ Evaluation    │
                  └───────┬───────┘
                          │
                    Data / Tools
```

## 🤖 25 Agents

### Software Engineering

| #  | Agent           | Purpose                             |
| -- | --------------- | ----------------------------------- |
| 01 | Code Review     | Analyze source code                 |
| 02 | Bug Analysis    | Diagnose bugs and errors            |
| 03 | Test Generation | Generate automated tests            |
| 04 | Documentation   | Generate technical documentation    |
| 05 | Refactoring     | Identify and improve code structure |
| 06 | API Design      | Design application APIs             |
| 07 | Database        | Design and optimize databases       |
| 08 | Security Audit  | Identify security risks             |
| 09 | Dependency      | Analyze dependencies                |
| 10 | Code Migration  | Plan and assist migrations          |

### AI & Data

| #  | Agent               | Purpose                                 |
| -- | ------------------- | --------------------------------------- |
| 11 | Research            | Research and synthesize information     |
| 12 | Data Analysis       | Analyze structured datasets             |
| 13 | Document Extraction | Extract structured information          |
| 14 | RAG                 | Question answering over knowledge bases |
| 15 | Prompt Optimization | Evaluate and improve prompts            |

### Business

| #  | Agent            | Purpose                                  |
| -- | ---------------- | ---------------------------------------- |
| 16 | Business Analyst | Convert requirements into specifications |
| 17 | Project Planner  | Create implementation plans              |
| 18 | Product Manager  | Generate product specifications          |
| 19 | SEO              | Analyze and improve SEO                  |
| 20 | Content          | Generate structured content              |

### Automation

| #  | Agent         | Purpose                            |
| -- | ------------- | ---------------------------------- |
| 21 | GitHub Issue  | Generate and manage GitHub issues  |
| 22 | Release Notes | Generate release notes             |
| 23 | DevOps        | Analyze CI/CD and deployment       |
| 24 | QA            | Generate QA strategy and scenarios |
| 25 | Orchestrator  | Coordinate multiple agents         |

## 🧰 Technology

* Python
* FastAPI
* Pydantic
* LangGraph
* LiteLLM
* PostgreSQL
* pgvector
* Redis
* Typer
* pytest
* Ruff
* mypy
* Docker
* GitHub Actions
* Next.js

## 🚀 Quick Start

### Requirements

* Python 3.12+
* uv
* Docker
* PostgreSQL
* Redis
* An API key for a supported LLM provider

### Installation

```bash
git clone https://github.com/<your-username>/agentforge.git

cd agentforge

uv sync
```

Copy the environment configuration:

```bash
cp .env.example .env
```

Start infrastructure:

```bash
docker compose up -d
```

Run the API:

```bash
uv run uvicorn api.main:app --reload
```

## 💻 CLI

List available agents:

```bash
uv run agentforge list
```

Run code review:

```bash
uv run agentforge run code-review ./example-project
```

Run research:

```bash
uv run agentforge run research "Explain retrieval augmented generation"
```

Run the orchestrator:

```bash
uv run agentforge run orchestrator requirements.md
```

## 🔌 Agent Architecture

Every agent follows a common contract:

```text
Input
  ↓
Context
  ↓
Planning
  ↓
Tool Selection
  ↓
LLM
  ↓
Validation
  ↓
Output
  ↓
Evaluation
```

This makes agents interchangeable and allows them to participate in larger workflows.

## 🛠 Tool System

Agents can use controlled tools including:

* File system
* Git
* GitHub
* Web search
* Database
* Vector search
* Document processing
* Shell commands
* Browser automation

Tools use permission boundaries to prevent agents from performing unauthorized operations.

## 🧠 Memory

AgentForge supports:

### Short-term memory

Execution-specific context.

### Long-term memory

Persistent agent information.

### Vector memory

Semantic retrieval using PostgreSQL and pgvector.

## 🔀 Multi-Agent Orchestration

Agent #25 is the Agent Orchestrator.

Example:

```text
Business Requirement
        │
        ▼
Business Analyst
        │
        ▼
Product Manager
        │
        ▼
Project Planner
        │
        ├──────────────┐
        ▼              ▼
    API Design      Database
        │              │
        └──────┬───────┘
               ▼
          Security Audit
               │
               ▼
               QA
               │
               ▼
          Documentation
               │
               ▼
             Review
               │
               ▼
             Result
```

The orchestrator does not blindly execute all agents.

It determines which agents are relevant to the task.

## 📊 Evaluation

AgentForge treats evaluation as a first-class capability.

Agents can be evaluated for:

* Accuracy
* Relevance
* Completeness
* Tool usage
* Structured output validity
* Hallucination
* Latency
* Token usage
* Cost

Evaluation datasets are stored under:

```text
evals/
```

## 🔐 Security

AgentForge follows a permission-based tool model.

Tools can require:

* Read permission
* Write permission
* Execute permission
* Network permission
* Human approval

Production-impacting operations should require explicit authorization.

## 🧪 Testing

Run tests:

```bash
uv run pytest
```

Run linting:

```bash
uv run ruff check .
```

Run type checking:

```bash
uv run mypy .
```

## 🗺 Roadmap

* [x] Agent runtime architecture
* [ ] First 5 agents
* [ ] Engineering agents
* [ ] AI/Data agents
* [ ] Business agents
* [ ] Automation agents
* [ ] Multi-agent orchestrator
* [ ] Evaluation framework
* [ ] Web interface
* [ ] Agent benchmark suite
* [ ] Production deployment examples

## 🎯 Project Goals

AgentForge is intended to demonstrate practical AI engineering concepts including:

* LLM integration
* Tool calling
* Structured outputs
* Agent planning
* Memory
* RAG
* Evaluation
* Guardrails
* Multi-agent systems
* API design
* Observability
* Production-oriented architecture

## 🤝 Contributing

Contributions are welcome.

Please read `CONTRIBUTING.md` before submitting a pull request.

## 📄 License

MIT License.
