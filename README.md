# AI Agents in Production — companion code

Runnable code for **[AI Agents in Production: An Engineer's Guide to Building,
Running, and Trusting Autonomous Systems](https://github.com/bhanumahesh1993/ai-agents-in-production)**
by Bhanu Mahesh (The Production AI Series, Volume 3).

**Everything here runs with no API keys, no network, and no cloud account.**
That is the point. Mock mode is the default: a deterministic fake model, fake
tools, an in-memory authoritative world, and a fault injector. You get the same
result twice, which is what makes an agent testable at all. Live providers and
cloud deployment are opt-in overlays behind environment variables.

## Quickstart

```bash
git clone https://github.com/bhanumahesh1993/ai-agents-in-production
cd ai-agents-in-production
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
make demo-ch01
```

`make demo-ch01` reproduces the incident the whole book is organised around: a
refund API times out *after* the write commits, the agent retries, the customer
is refunded twice, and the run reports `succeeded` with a clean trace. Then it
reruns the same trajectory with a derived idempotency key and shows a single
refund in the ledger. If you read nothing else, read that diff.

## The running example

**Northstar Returns** is a fictional mid-size online retailer. Its support agent
answers order questions and issues refunds — a task with genuine ambiguity, a
verifiable outcome, and an action that moves real money. Every chapter's artifact
acts on the same world.

Money is always an integer number of cents with an explicit currency. The refund
approval threshold is 5,000 cents. Fixture orders are `NR-2026-0041827`
(8,400 cents, delivered), `NR-2026-0041903` (3,250 cents, damaged), and
`NR-2026-0042110` (24,000 cents, flagged for fraud review).

Tools: `get_order`, `get_policy`, `search_orders` (reads); `issue_refund`,
`send_message` (writes); `escalate_to_specialist` (handoff).

## Layout

| Path | What it holds |
|---|---|
| `packages/northstar_contracts/` | Typed tool contracts, run/message/event models, `idempotency_key()`, and the `World` authoritative store with its fault injector |
| `packages/northstar_runtime/` | The agent loop, model providers (`FakeModel`, `FlakyModel`, `LiveModel`), checkpointers, the tool registry, and the `DurableRunner` with its journal and replay |
| `packages/northstar_policy/` | Policy decision point, `Principal`, approval store with call fingerprints, and budget guards |
| `packages/northstar_telemetry/` | OpenTelemetry `gen_ai.*` instrumentation (works without OTel installed), the cost ledger, and field redaction |
| `packages/northstar_evals/` | State, trajectory, and judge graders; the simulated user; `pass_k()` and `run_repeated()` with Wilson intervals |
| `artifacts/chNN-slug/` | One directory per chapter: the code excerpted in that chapter, its tests, and a README saying what it proves |
| `deploy/` | Local Compose stack and the cloud, Kubernetes, edge, and durable-execution overlays |
| `docs/` | Architecture notes and the review checklists from Appendix B |

## Model modes

| Mode | How | Determinism | Cost | Use it for |
|---|---|---|---|---|
| mock | default | total | zero | development, tests, CI, every example in the book |
| replay | recorded cassettes | total | zero | regression gates over real recorded behaviour |
| flaky | `FlakyModel(seed=…)` | seeded | zero | reliability and recovery testing |
| live | `NORTHSTAR_MODEL=live` + provider key | none | metered | the one time you want to see a real model in the loop |

Nothing in mock mode imports a provider SDK. `pip install -e .` pulls zero
runtime dependencies.

## Maintained per edition

- `VERSIONS.md` — every framework, SDK, protocol revision, and service status this edition of the book assumes, with the date it was verified.
- `deployment-matrix.csv` — the machine-readable form of the book's Appendix A comparison.
- `trend-tracker.md` — every forward-looking claim in Chapter 27, with its source and date, so it can be refreshed without touching prose.

Because this field moves quickly, treat this repository as the living edition
and the book as the map that explains it. Issues and pull requests welcome,
especially for a stale version pin or a deployment target the matrix should
cover.

## License

MIT. See `LICENSE`.
