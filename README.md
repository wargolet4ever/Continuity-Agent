# Continuity Agent

**English** · [中文](README.zh-CN.md)

**A script supervisor for AI-generated films — it catches the errors you cannot see in a single frame.**

![Five frames from one generation: the camera holds still, the control handle does not](docs/shot21_frames.png)

Those five frames come from **one** five-second generation. Same shot, no cut. The camera never moves and neither do the actors — but watch the control handle.

Every individual frame passes. The error isn't *in* any frame; it only exists *between* them. Scrubbing through your takes will not catch this.

---

## Why this happens

Film sets have a job for exactly this: the **script supervisor**, who watches whether this shot matches the last one — which hand held the cup, where the key was, whether the light was on.

AI video generation has no such role. Every shot is an independent call, and the model does not remember what it drew last time. Films that revisit the same space — loops, flashbacks, multiple timelines — get hit hardest: one location gets generated five, six, seven separate times, and nothing keeps those generations in agreement.

Continuity Agent compiles the story facts you lock down into **per-shot visual checks**, so cross-shot consistency becomes mechanically verifiable instead of a thing you squint at.

## Where it sits in your pipeline

| | What happens | Where |
|---|---|---|
| ① | One sentence → shot plan, per-shot prompts, **and a continuity rulebook** | This tool, "shot plan" tab |
| ② | Generate the shots | **Veo / Sora / Runway / Kling / Seedance — anywhere. Not this tool.** |
| ③ | Bring the footage back, check it against ①'s rulebook | This tool, "check footage" tab |

**① and ③ are two ends of the same rulebook.** The planning step emits a `canon_draft.json` that mounts into your session; step ③ checks against *that*, not against some generic standard. You are checking footage against **the space you just defined**.

Already have footage? Start at ③ — it falls back to the rulebook from the short film this was built for.

Step ② is deliberately not here. **This is a verifier, not a generator.**

## It outputs a decision, not a score

The interface speaks plainly; the codes below live in a collapsed "detailed report".

| What you see | Code | Meaning |
|---|---|---|
| ✅ Looks fine | `PASS` | Nothing violated within what could be observed |
| 🔧 Fixable in post | `LOCAL FIX` | **Don't re-render.** Paint it out and keep the take |
| 🔁 Needs regenerating | `REGENERATE` | Structural error; post can't recover it |
| 🤔 I'm not sure — look yourself | `HUMAN REVIEW` | Performance or weak evidence. **It refuses to guess** |

`LOCAL FIX` is where the money is. A generation invents an extra display panel — a violation, but twenty minutes of compositing fixes it while the blocking, geometry and lighting are all correct. Calling that `LOCAL FIX` saves the whole take instead of burning another generation.

`HUMAN REVIEW` matters just as much. Vision models get things wrong. **Without an "I don't know" exit, a model will confidently misjudge — and after the third time you stop trusting any of its output.**

## Every conclusion is tagged with its evidence level

This is the part the project cares about most: **you must know how a conclusion was reached.**

| Tag | Meaning |
|---|---|
| `USER-REPORTED RULE TRIAGE` | Nothing uploaded. "*If* what you describe is true, the rules say this." **It never saw a picture** |
| `RULE + PIXEL CHECK` | Image present, but only colors and pixels were read — no model understood it |
| `VIDEO FRAME + RULE CHECK` | Frames sampled across the clip; the semantic claim still comes from your description |
| `VISUAL AUDIT` | A multimodal model actually looked and checked each rule |
| `VIDEO VISUAL AUDIT` | Frames sampled and checked across time by a multimodal model |

If you didn't upload anything, the UI says so in plain words rather than letting you assume it looked.

## Quick start

**No API key required.** Without one, everything still runs — understanding and planning fall back to deterministic templates, and the execution trace labels the degradation instead of hiding it.

```bash
git clone https://github.com/wargolet4ever/Continuity-Agent.git
cd Continuity-Agent
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:7860` and click **"example 2: handle deforms + key misplaced"** — that's the clip at the top of this page, running the full audit chain in one click.

Two dependencies are **optional** and the app starts fine without either: `imageio-ffmpeg` (frame sampling — without it, video checks switch off and image checks are untouched) and `pillow-heif` (iPhone `.heic` files).

Optional, for multimodal auditing:

```bash
export LLM_API_KEY=your-key
export LLM_MODEL=a-model-with-vision-input
export LLM_BASE_URL=an-openai-compatible-/v1-endpoint
```

### If it won't start

**`Couldn't start the app because 'http://localhost:7860/...' failed (code 503)`**

A system proxy is intercepting Gradio's own loopback health check. `app.py` adds loopback addresses to `NO_PROXY` automatically; if you launch some other way, set it yourself:

```bash
export NO_PROXY=localhost,127.0.0.1
```

**Video errors / `FileNotFoundError: [WinError 2]`** — the `imageio-ffmpeg` *package* is installed but its **ffmpeg binary** isn't (antivirus removes it; a wrong `IMAGEIO_FFMPEG_EXE` does it too). Run `python diagnose.py` — it reports whether the package is present, which path ffmpeg resolves to, whether that file exists, and what happens when it actually runs. Usual fix: `pip install --force-reinstall imageio-ffmpeg`. **Image checks work either way.**

**Port taken**: `PORT=7870 python app.py`. **Can't bind 0.0.0.0**: `HOST=127.0.0.1 python app.py`.

## What it actually caught

This was built for a 3m22s hard-SF short, not invented and then given a problem to solve. Four things that really happened:

**1. The ending rested on a post-production patch that turned out to be impossible.** The causality audit raised three errors: both questions the ending had to answer depended on one planned overlay in Shot 05. That shot had no reusable footage — the patch could not be made. **Because the dependency was reported out loud, the ending got redesigned** instead of silently failing in the finished film.

**2. A metric of our own, 100% false-positive, got deleted.** A "loop divergence" check compared each take against the previous reference. It fired constantly under "same room, locked camera" — which the rulebook *requires*. **The metric was punishing compliance.** No threshold tuning; it was removed. A metric measuring the wrong thing can't be saved by parameters.

**3. A color check that classified faces as alarm lights.** The first red predicate flagged warm skin close-ups, amber instrument lamps and copper props as alarm red. Rewritten as "high R *and* simultaneously low G and low B", it got all nine test frames right. Plain lesson: **a threshold written from intuition has to be validated against real pixel values.**

**4. An error that no single frame could ever show.** The image at the top of this page — the control handle changing shape inside one generation. This produced the video frame-sampling path and a new rule (console geometry must stay stable within a shot), judged `REGENERATE`. **That rule was forced out by real footage, not designed in advance.**

## What it does not do

- **It does not generate video.** There is a MiniMax Hailuo adapter in the repo, but it is **off by default** behind three gates (feature flag, separate key, access code). A real feasibility probe was completed — credentials, network, endpoint and request structure all verified — but the submission was rejected on account entitlement, so no video was produced and no generation path was wired in. Sanitized records in [`evidence/`](evidence/).
- No editing, no scoring, no video extension.
- **It does not rewrite your story.** The rulebook is human-maintained; the app only reads it. Conflicts are surfaced for you to confirm, never auto-resolved.
- Model routing emits a decision only — it does not dispatch.

## Known gaps

Written here rather than hidden; full version in [`scoring_gap.md`](scoring_gap.md).

- **The rulebook is hand-maintained.** That's where the precision comes from, and also the barrier to entry — it is not yet friendly to a project it knows nothing about.
- Rulebooks produced by the planning step are skeletons; rule density is well below a hand-written one.
- **Identity consistency has no quantitative grounding.** Judgement comes from rules plus multimodal verification; color is only a weak prior. We chose to state the gap rather than ship an unreliable similarity number dressed up as "grounded evaluation".
- Production logs are a single shared CSV with no per-user isolation (not rendered publicly — see below).
- Without a model API, understanding and planning use deterministic templates and quality drops noticeably.

## Safe by default

Production logs contain user-entered prompts, notes and filenames. A public deployment therefore:

- renders no raw log table — only aggregate counts with no free text
- **binds no callback that returns log rows.** `refresh_log` never enters the event table, so it never appears in `/gradio_api/info`. Hiding a component is not access control; Gradio events can be called directly
- turns `show_error` off, so tracebacks don't leak container paths

13 two-session isolation tests enforce it: a unique prompt submitted by visitor A cannot be read back by visitor B through UI callbacks, aggregate stats, `/config` or `/gradio_api/info`.

Private deployments can set `SHOW_RAW_LOGS=1`. **Do not set it on a public instance.**

## Tests

```bash
python -m unittest discover -s . -p 'test_*.py'
```

101 tests, all passing. MiniMax and multimodal failure paths are entirely mocked — **no external requests, no cost**. Full report in [`test_report.md`](test_report.md).

## Docs

| File | Contents |
|---|---|
| [`README.zh-CN.md`](README.zh-CN.md) | 中文文档 |
| [`architecture.md`](architecture.md) | Architecture, state machine, provider mapping, cost and retry policy |
| [`test_report.md`](test_report.md) | Full report for all 101 tests |
| [`scoring_gap.md`](scoring_gap.md) | Capability gaps, stated openly |
| [`demo_script.md`](demo_script.md) | 60-second demo script |

## License

Code is [MIT](LICENSE). The footage in `assets/examples/` and the frames in `docs/` are from the short film *Passenger Zero*, included only as test fixtures demonstrating the failures this tool detects; **all rights to the film are reserved by the author**.
