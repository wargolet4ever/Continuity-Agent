# Deployment and cost runbook

This runbook deploys the validator on CPU while keeping the default public path at **zero API cost**.
It does not turn the optional MiniMax adapter into a product feature.

## Release contract

| Boundary | Public default | Hard limit |
|---|---|---|
| Compute | CPU only; no GPU dependency | Python 3.10+, 2 vCPU / 16 GB target |
| Image input | Local rules; no model call | 10 files/batch, 10 MB/file, 1280 px longest edge |
| Video audit | Local ffmpeg frame sampling | 20 seconds, 50 MB, five frames |
| Raw logs | Hidden and not API-bound | `SHOW_RAW_LOGS` must not be `1` |
| Multimodal model | Off | Requires the operator's `LLM_API_KEY` and provider billing |
| Video generation | Off and absent from the UI | Requires three explicit gates plus 1 active job, 3 jobs/day, ¥10/day estimate cap |

Run the preflight before every deployment:

```bash
continuity deploy-check --target modelscope
continuity deploy-check --target huggingface --json
```

Exit `0` means the public safety and cost boundary is ready. Exit `40` blocks deployment. A warning is an
explicit degradation, such as ffmpeg being unavailable while image audit remains usable. Reports never print
secret values or absolute host paths.

## Environment profiles

### Free validator (recommended public profile)

No secrets are required. Keep these values absent or explicit:

```text
SHOW_RAW_LOGS=0
ENABLE_VIDEO_GENERATION=0
```

With no `LLM_API_KEY`, planning uses deterministic templates and audit uses local rules/pixels. This path makes
no paid API calls. The offline `continuity film` fixture also makes no external call.

### Optional paid multimodal audit

Store these in the hosting platform's secret manager, never in Git:

```text
LLM_API_KEY
LLM_MODEL
LLM_BASE_URL
```

The provider's current token/image pricing applies. No repository-level monthly-cost promise is possible, so
the public free instance should leave these unset unless the operator separately adds provider rate limits.

### Controlled paid video test

This is not the default public profile. All values below are required before the controls appear:

```text
ENABLE_VIDEO_GENERATION=1
VIDEO_PROVIDER=minimax
MINIMAX_VIDEO_API_KEY=<platform secret>
VIDEO_ACCESS_CODE=<platform secret>
MAX_ACTIVE_VIDEO_JOBS=1
MAX_DAILY_VIDEO_JOBS=3
VIDEO_BUDGET_CNY=10
```

The UI shows the verified per-job estimate before submission. Unknown model/resolution/duration combinations are
rejected. These estimates are guardrails from the adapter, not a provider price guarantee.

## Local CPU deployment

```bash
git clone https://github.com/wargolet4ever/Continuity-Agent.git
cd Continuity-Agent
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e .
continuity deploy-check --target local
python app.py
```

On Windows, activate with `.venv\Scripts\activate`. The app listens on `0.0.0.0:7860` in a container; use
`HOST=127.0.0.1` for local-only access or `PORT=7870` if the default port is occupied.

## ModelScope Studio (domestic target)

1. Create a **Gradio** Studio from this repository and select the CPU `2 vCPU / 16 GB` resource.
2. Use Python 3.10+ and `app.py` as the entry file; install `requirements.txt`.
3. Add only non-secret variables for the free profile: `SHOW_RAW_LOGS=0`, `ENABLE_VIDEO_GENERATION=0`.
4. If the controlled paid test is intentionally enabled, add API key and access code in **Secrets**, not the
   repository or ordinary environment-variable fields.
5. Run `continuity deploy-check --target modelscope` in the build/start workflow, then start `python app.py`.

ModelScope's official [quick deployment guide](https://modelscope.cn/docs/studios/quick-start) remains the source
of truth for platform UI steps and resource availability.

## Hugging Face Spaces (international target)

Put this YAML block at the top of the Space repository's README (not this GitHub README):

```yaml
---
title: Continuity Agent
emoji: 🎬
colorFrom: gray
colorTo: red
sdk: gradio
sdk_version: 5.50.0
python_version: 3.10
app_file: app.py
suggested_hardware: cpu-basic
pinned: false
---
```

Then push the repository and set `SHOW_RAW_LOGS=0`, `ENABLE_VIDEO_GENERATION=0` as Space variables. Hugging Face
documents `app_file`, Python and hardware metadata in its
[Spaces configuration reference](https://huggingface.co/docs/hub/spaces-config-reference).

Cost wording matters: CPU Basic is listed as **$0 hourly hardware cost**, but as of 2026-09-17 Hugging Face says
creating a Gradio/Docker compute Space requires an eligible paid plan, except its stated ZeroGPU personal-account
exception. Do not advertise this route as universally free. Free hardware may also sleep when idle. Verify the
current account requirement and prices in the official
[Spaces overview](https://huggingface.co/docs/hub/spaces-overview) before launch.

## Health checks

Use all three layers; a green process alone is insufficient.

1. **Preflight:** `continuity deploy-check --target <target>` returns `0`.
2. **HTTP:** `curl -fsS "$BASE_URL/config" >/dev/null` returns success. This proves the Gradio application loaded.
3. **Functional smoke:** open the page, run example 2, and confirm a decision plus downloadable report. Upload one
   image under the public limits. If ffmpeg passed preflight, also run one bundled video example.

The release is unhealthy if `/config` fails, uploads return 500, raw logs are exposed, or the example cannot
produce an audit report. A missing ffmpeg binary is degraded rather than fatal only when image audit still works
and the video controls are absent.

## Rollout and rollback

1. Record the current production commit SHA and deployment URL.
2. Deploy one immutable candidate commit; do not deploy a moving local directory.
3. Run preflight, HTTP health, the bundled example, upload smoke, and privacy check.
4. Keep the prior deployment until the candidate passes all checks.
5. If any check fails, redeploy the recorded prior SHA. On Hugging Face or ModelScope, use repository history to
   reset the Space/Studio deployment source to that commit, then restart/rebuild.
6. Re-run the same health checks after rollback and record the failed SHA and symptom.

Never solve a public outage by enabling `SHOW_RAW_LOGS`, committing a secret, or bypassing the upload limits.

## Cost matrix

| Mode | Hosting | API cost | Operator ceiling |
|---|---:|---:|---|
| Local CPU validator | Existing machine | ¥0 incremental API cost | CPU/RAM only |
| ModelScope CPU public validator | Platform quota/plan | ¥0 API cost | Platform quota; verify current terms |
| Hugging Face CPU Basic | $0/hour hardware line item, account eligibility may cost | ¥0 API cost | Verify current plan and sleep policy |
| Optional multimodal audit | Same hosting | Provider-billed | No built-in monetary ceiling; keep off publicly |
| Offline six-shot fixture | Local ffmpeg | ¥0 | No external request |
| Controlled MiniMax generation | Same hosting | Provider-billed | 1 active, 3/day, estimated ¥10/day |

“Zero cost” in this repository means **no paid model/API call on the default validator path**. It does not promise
that a third-party hosting account, network, storage, or future platform policy will always be free.
