# VERSIONS.md — what this edition assumes

**Edition:** first edition, 2026. **Verified:** July 2026.

This file is the single place where the book's volatile facts are pinned. The
prose in the book carries status badges and dated "As of July 2026" boxes; this
table is the machine-checkable companion. Every row is a fact that can rot.

**How to use it:** before you rely on any version-specific claim in the book,
check the row here, then check the vendor's own current documentation. Where a
cell says `verify`, the research behind the book deliberately declined to state
a number rather than guess one — go to the primary source.

**How to re-verify:** the schedule the book recommends is monthly for protocol
revisions and cloud release notes, at every cloud-chapter freeze for product
status and pricing, and at press lock for every dated claim. See Chapter 27 and
Appendix C.

## Frameworks and SDKs

| Component | Version / status this edition assumes | Date | Book ref |
|---|---|---|---|
| LangChain / LangGraph | 1.0, with a stated no-breaking-changes-until-2.0 policy; Python 3.10+ | 22 Oct 2025 | Ch 3, 8 |
| Microsoft Agent Framework | 1.0, generally available, MIT licensed; successor to its two predecessor projects | 3 Apr 2026 | Ch 3, 6, 22 |
| Google ADK | 1.0 | 2026 | Ch 3, 22 |
| OpenAI Agents SDK | current at time of writing; primitives are agents, handoffs, guardrails | Jul 2026 | Ch 3, 6 |
| OpenAI AgentKit | announced Oct 2025; **Agent Builder and Evals are scheduled to wind down 30 Nov 2026** | 6 Oct 2025 | Ch 3 |
| Claude Agent SDK | session-based interface; `allowedTools` / `permissionMode` | 29 Sep 2025 | Ch 2, 3 |
| Pydantic AI, CrewAI, LlamaIndex Workflows, DSPy, AWS Strands | surveyed as categories, not pinned | Jul 2026 | Ch 3 |

## Protocols

| Component | Version / status | Date | Book ref |
|---|---|---|---|
| Model Context Protocol | revision **2025-11-25** is the stable revision this edition targets | 25 Nov 2025 | Ch 9 |
| Model Context Protocol (next) | a **2026-07-28 release candidate** existed at press time — treat as not-yet-final | 28 Jul 2026 | Ch 9 |
| MCP transports | stdio and Streamable HTTP. The older SSE transport is deprecated | 2025-06-18 revision | Ch 9 |
| MCP authorization | OAuth 2.1; issuer validation; Client ID Metadata Documents in place of dynamic client registration | 2025-11-25 | Ch 9, 19 |
| A2A | v1.0; stewarded by the Linux Foundation since June 2025 | Jun 2025 → 2026 | Ch 10 |
| OpenTelemetry GenAI semantic conventions | `gen_ai.*` namespace, **Development stability** — attribute names can change without a major version bump | Jul 2026 | Ch 17 |

> The `gen_ai.*` row is the one most likely to bite you. Development stability
> means the instrumentation in `packages/northstar_telemetry/` may need attribute
> renames on a minor upgrade. The package centralises every attribute name in one
> module for exactly this reason.

## Managed agent platforms

Status labels below are the ones the book prints. Pricing is deliberately absent:
the book names billing *dimensions* and ships a calculator rather than quoting
rates that expire. Check the vendor's current pricing page.

| Platform | Status this edition assumes | Notes | Book ref |
|---|---|---|---|
| AWS Bedrock AgentCore | generally available | Runtime, Memory, Gateway, Identity, Browser, Code Interpreter, Observability. Composable — adopt one component at a time | Ch 22 |
| Google Cloud managed agent runtime | generally available, **renamed** during this edition's research window | The Vertex-era naming was reorganised; check current product naming before quoting it | Ch 22 |
| Azure AI Foundry Agent Service | generally available; **hosted agents** were tracking to GA around July 2026 — `verify` | SDK surface moved to a project-client shape; check the current package | Ch 22 |
| Kubernetes-native agent platforms | early / sandbox-stage projects | Agents, tools, and skills as custom resources | Ch 23 |
| Edge durable-object agent runtimes | generally available | Per-agent object with embedded storage and hibernation | Ch 23 |

## Durable execution engines

| Engine | Status | Book ref |
|---|---|---|
| Temporal | generally available; has a first-party agent-SDK integration | Ch 24 |
| DBOS | generally available; Postgres-in-process model | Ch 24 |
| Restate | generally available | Ch 24 |
| Inngest | generally available | Ch 24 |
| AWS Step Functions / Lambda durable functions | generally available | Ch 24 |
| Azure Durable Task | generally available | Ch 24 |
| Cloudflare Workflows | generally available | Ch 23, 24 |

## Benchmarks and research figures

Figures quoted in the book come from the cited papers at the versions cited
there. Leaderboards move monthly; the book's numbers are anchored with model,
task set, and date at the point of use, and Chapter 14 explains why a headline
score does not transfer to your system.

| Source | Used for | Book ref |
|---|---|---|
| τ-bench / τ²-bench | `pass^k` and the pass^1 → pass^8 reliability drop | Ch 13, 14 |
| MAST failure taxonomy | the three categories, fourteen modes, and their prevalences | Ch 16 |
| METR time-horizon work | the capability trend line, its doubling estimate, and its own reliability ceiling caveat | Ch 27 |
| Anthropic multi-agent research-system report | the research-eval gain and the ~4x / ~15x token multiples | Ch 5, 25 |

## Python and tooling

| Component | Pin |
|---|---|
| Python | 3.11, 3.12, 3.13 (CI matrix) |
| Runtime dependencies in mock mode | none, by design |
| `[dev]` | pytest, pytest-cov, ruff, mypy |
| `[otel]` | opentelemetry-sdk, opentelemetry-exporter-otlp |
| `[live]` | provider SDKs, never imported unless live mode is selected |
