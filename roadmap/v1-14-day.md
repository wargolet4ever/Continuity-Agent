# Continuity Agent v1 · 14-day execution schedule

Day labels are relative workdays, not fabricated calendar dates. The live state is computed from repository
evidence:

```bash
continuity schedule
continuity schedule --json
continuity schedule --strict
```

`--strict` returns exit code `30` until all fourteen days and all hard gates are complete. An invalid schedule
returns `31`. The schedule command reads files only; it never executes commands stored in the plan.

| Day | Focus | Required output | Gate / fallback |
|---|---|---|---|
| D1 | Positioning and acceptance | README + capability gaps | — |
| D2 | Architecture boundaries | architecture + orchestrator | — |
| D3 | Public data contracts | runtime validators + JSON Schema | — |
| D4 | High-confidence rules | plugin rules + regressions | — |
| **D5** | Benchmark and calibration | calibration/validation protocol | **Hard gate:** real calibration report, or explicitly remain provisional and keep unsupported claims blocked |
| D6 | ffmpeg ingestion | binary probe + ordered extraction | — |
| **D7** | Integration output | machine report + human report + exit codes | **Hard gate:** if absent, cut the film loop and reinvest in benchmark/distribution |
| D8 | Six-shot film loop | working fixture sample + terminal recording | One vendor; blocker shots only; at most two repair rounds |
| D9 | Executable schedule | JSON + Schema + evaluator + tests | — |
| D10 | Deployment and cost | runbook + executable preflight + public resource limits | — |
| **D11** | Real external user | redacted completed-session evidence | **Hard gate:** if absent, stop feature work and return to outreach/onboarding |
| D12 | User-led iteration | top observed blocker fixed + regression | Requires D11 evidence |
| D13 | Risk and portfolio | cut lines + verified case study | No unsupported launch or quality claims |
| D14 | v1 release | changelog + claim/release checklist | Requires D11–D13 |

## Hard-gate semantics

- **D5** may complete with an explicit fallback. That means the calibration machinery is shipped, but identity
  thresholds remain provisional and the project does not claim production grounding.
- **D7** must pass before D8 exists. Machine-readable output, human-readable output, and stable exit behavior
  are prerequisites rather than polish.
- **D11 has no paperwork fallback.** The v1 success metric is at least one real external user completing the
  core audit flow. A self-run demo does not satisfy it.

The machine source of truth is [`v1-14-day.json`](v1-14-day.json). The external contract is
[`schemas/v1-schedule-v1.schema.json`](../schemas/v1-schedule-v1.schema.json).
