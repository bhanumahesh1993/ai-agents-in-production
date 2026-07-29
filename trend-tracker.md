# trend-tracker.md — dated forward-looking claims

Every forward-looking statement in Chapter 27 of the book lives here with its
source and the date it was made, so a future edition can refresh the claims
without rewriting prose. **A forecast is not a measurement.** The `kind` column
says which one you are looking at.

`kind` values:

- `measurement` — something that was observed and reported, with a method.
- `forecast` — an extrapolation or projection, including the source's own.
- `status` — a fact about a product, spec, or organisation at a point in time.
- `directional` — a figure that circulates mainly through secondary trackers and
  has not been confirmed against the primary organisation. Treat as indicative
  only. The book flags these in text.

## How to refresh

1. Work down the table. For each row, open the source and check whether the claim still holds.
2. Update `verified` to today's date if it does; add a new row and mark the old one superseded if it does not.
3. A row that cannot be re-verified against a primary source gets demoted to `directional` or removed. It does not get quietly kept.
4. Anything that changes a number printed in the book goes into the errata list as well.

## Claims

| # | Claim | kind | Source org | Source date | verified | Book ref |
|---|---|---|---|---|---|---|
| 1 | The 50%-task-completion time horizon for software tasks has been doubling roughly every seven months since 2019 | measurement | METR | Mar 2025 | Jul 2026 | Ch 27 |
| 2 | The recent doubling time looks faster than the long-run trend, on the order of a few months by 2026 | measurement | METR | Jan 2026 | Jul 2026 | Ch 27 |
| 3 | Time-horizon measurements above roughly 16 hours are unreliable with the current task suite | measurement | METR (own caveat) | Mar 2025 | Jul 2026 | Ch 27 |
| 4 | Within about five years, systems will be able to automate many software tasks that currently take humans a month | forecast | METR (extrapolation) | Mar 2025 | Jul 2026 | Ch 27 |
| 5 | Agentic reinforcement-learning environments are becoming a distinct market layer in agent development | directional | trade press | Sep 2025 | Jul 2026 | Ch 27 |
| 6 | Frontier-lab spend on RL environments is reported in the high hundreds of millions to billions | directional | secondary trackers | 2025-2026 | **unverified** | Ch 27 |
| 7 | Agent payment and commerce protocols launched with backer counts in the dozens-to-hundreds range | directional | vendor announcements | Sep 2025 onward | Jul 2026 | Ch 27 |
| 8 | MCP is stewarded by a neutral foundation | status | Linux Foundation / Agentic AI Foundation | Dec 2025 | Jul 2026 | Ch 9, 27 |
| 9 | A2A is stewarded by the Linux Foundation | status | Linux Foundation | Jun 2025 | Jul 2026 | Ch 10, 27 |
| 10 | OpenTelemetry GenAI semantic conventions remain at Development stability, so attribute names may change | status | OpenTelemetry | Jul 2026 | Jul 2026 | Ch 17, 27 |
| 11 | Enterprise agent-identity products reached general availability during the 2026 window | status | vendor docs | Apr 2026 | Jul 2026 | Ch 19, 27 |
| 12 | An agent turn uses roughly 4x the tokens of a chat turn; multi-agent roughly 15x | measurement | Anthropic engineering | Jun 2025 | Jul 2026 | Ch 5, 25, 27 |
| 13 | Token usage alone explained about 80% of the variance in one internal research eval | measurement | Anthropic engineering | Jun 2025 | Jul 2026 | Ch 5 |
| 14 | Better base models alone will not close the multi-agent failure taxonomy | forecast | MAST authors | 2025 | Jul 2026 | Ch 16, 27 |
| 15 | Computer-use and GUI agents continue maturing toward production viability | forecast | multiple vendors | 2026 | Jul 2026 | Ch 12, 27 |
| 16 | Small and on-device agents grow as a deployment class | forecast | multiple vendors | 2026 | Jul 2026 | Ch 23, 27 |

## Rot rates

From Chapter 27's rot-watch map. Re-verify at the interval given.

| Layer | Rot rate | Re-verify |
|---|---|---|
| Agent loop mechanics, reliability arithmetic, isolation-boundary reasoning, threat model | very low | per edition |
| Context-engineering principles, failure taxonomy, OTel span-hierarchy concepts | low | per edition |
| Protocol authorization details, durable-engine features, sandbox providers | medium | quarterly |
| Framework versions and API surfaces | high | quarterly |
| Cloud product names, GA status, quotas | high | monthly |
| Cloud pricing, benchmark leaderboards | very high | monthly, and never quote without a date |
