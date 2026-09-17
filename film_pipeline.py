"""Six-shot portfolio loop: plan, generate, audit blockers, repair, concatenate.

The default ``fixture`` vendor is deliberately synthetic.  It makes tiny local
MP4 clips with ffmpeg and injects declared demo findings; it never calls a paid
generation API and never pretends that a model inspected the pixels.  The loop
it exercises is the same one a real single-vendor adapter can implement.
"""

from __future__ import annotations

import html
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from canon_loader import CanonStore
from orchestrator import run_create

FILM_REPORT_VERSION = "1.0"
MAX_REPAIR_ROUNDS = 2

EXIT_OK = 0
EXIT_BLOCKERS_REMAIN = 20
EXIT_PIPELINE_ERROR = 21
EXIT_FFMPEG_UNAVAILABLE = 22


class FilmPipelineError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_PIPELINE_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class BlockerFinding:
    rule_id: str
    evidence: str
    minimal_fix: str
    severity: str = "regenerate"


@dataclass
class GeneratedClip:
    shot_id: str
    attempt: int
    provider: str
    path: Path
    prompt: str
    demo_finding: BlockerFinding | None = None


@dataclass(frozen=True)
class FilmRun:
    exit_code: int
    report: dict[str, Any]
    report_json: Path
    report_html: Path
    video_path: Path | None


class FilmVendor(Protocol):
    """One run owns exactly one vendor instance."""

    name: str

    def generate(
        self,
        shot: dict[str, Any],
        prompt: str,
        attempt: int,
        target: Path,
    ) -> GeneratedClip: ...


class ClipAuditor(Protocol):
    name: str

    def audit(self, clip: GeneratedClip) -> list[BlockerFinding]: ...


class FixtureVideoVendor:
    """Offline demo vendor that renders deterministic color clips with ffmpeg."""

    name = "fixture-ffmpeg"

    _COLORS = ("26364a", "3f5068", "5b6475", "705b63", "5f4b52", "334758")

    def __init__(
        self,
        *,
        ffmpeg_exe: str | None = None,
        blocker_shots: set[str] | None = None,
        persistent_blocker_shots: set[str] | None = None,
        duration: float = 0.35,
    ) -> None:
        if ffmpeg_exe is None:
            import video_audit

            ffmpeg_exe = video_audit.FFMPEG_EXE
        if not ffmpeg_exe:
            raise FilmPipelineError(
                "ffmpeg 不可用；请先运行 python diagnose.py。",
                EXIT_FFMPEG_UNAVAILABLE,
            )
        self.ffmpeg_exe = ffmpeg_exe
        self.blocker_shots = {"2", "5"} if blocker_shots is None else blocker_shots
        self.persistent_blocker_shots = (
            set() if persistent_blocker_shots is None else persistent_blocker_shots
        )
        self.duration = duration

    def generate(
        self,
        shot: dict[str, Any],
        prompt: str,
        attempt: int,
        target: Path,
    ) -> GeneratedClip:
        target.parent.mkdir(parents=True, exist_ok=True)
        index = (int(shot["shot_id"]) - 1) % len(self._COLORS)
        color = self._COLORS[index]
        if attempt > 1:
            # Repaired takes are visibly distinct while retaining the same codec.
            color = f"{min(255, int(color[:2], 16) + 24):02x}{color[2:]}"
        command = [
            self.ffmpeg_exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{color}:s=320x180:d={self.duration}:r=12",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
            raise FilmPipelineError(f"Shot {shot['shot_id']} 生成失败：{detail[:240]}")

        shot_id = str(shot["shot_id"])
        has_blocker = shot_id in self.persistent_blocker_shots or (
            shot_id in self.blocker_shots and attempt == 1
        )
        finding = None
        if has_blocker:
            if shot_id == "2":
                finding = BlockerFinding(
                    "DRAFT-CHAR-01",
                    "演示夹具声明：主角身份与第一镜不一致。",
                    "锁定第一镜身份参考，只重生成 Shot 2。",
                )
            else:
                finding = BlockerFinding(
                    "DRAFT-CAM-02",
                    "演示夹具声明：固定空间出现了机位位移。",
                    f"保持固定机位，只重生成 Shot {shot_id}。",
                )
        return GeneratedClip(
            shot_id=shot_id,
            attempt=attempt,
            provider=self.name,
            path=target,
            prompt=prompt,
            demo_finding=finding,
        )


class FixtureBlockerAuditor:
    """Reads declared fixture findings; it does not claim visual inspection."""

    name = "scripted-fixture-blocker-check"

    def audit(self, clip: GeneratedClip) -> list[BlockerFinding]:
        return [clip.demo_finding] if clip.demo_finding else []


def _canon_prompt(store: CanonStore, shot: dict[str, Any], positive: str) -> str:
    pack = store.compile_rule_pack(shot["shot_id"])
    lines = [positive, "", "[CANON CONTINUITY GUARDRAILS]"]
    lines.extend(f"MUST: {rule['text']}" for rule in pack["rules"])
    lines.extend(f"MUST SHOW: {item}" for item in pack.get("must_show", []))
    lines.extend(f"MUST NOT SHOW: {item}" for item in pack.get("must_not_show", []))
    return "\n".join(lines)


def _repair_prompt(base: str, findings: list[BlockerFinding]) -> str:
    lines = [base, "", "[BLOCKER REPAIR — KEEP EVERYTHING ELSE UNCHANGED]"]
    lines.extend(f"{item.rule_id}: {item.minimal_fix}" for item in findings)
    return "\n".join(lines)


def _concat_clips(ffmpeg_exe: str, clips: list[GeneratedClip], root: Path) -> Path:
    manifest = root / "concat.txt"
    manifest.write_text(
        "".join(f"file '{clip.path.relative_to(root).as_posix()}'\n" for clip in clips),
        encoding="utf-8",
    )
    target = root / "film.mp4"
    command = [
        ffmpeg_exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        manifest.name,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-y",
        target.name,
    ]
    completed = subprocess.run(
        command, cwd=root, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0 or not target.is_file():
        detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
        raise FilmPipelineError(f"六镜拼接失败：{detail[:240]}")
    return target


def _relative(path: Path | None, root: Path) -> str | None:
    return path.relative_to(root).as_posix() if path else None


def _render_html(report: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value))
    rows = []
    for shot in report["shots"]:
        rows.append(
            "<tr>"
            f"<td>{esc(shot['shot_id'])}</td>"
            f"<td>{esc(shot['beat'])}</td>"
            f"<td>{esc(shot['final_take'])}</td>"
            f"<td>{esc(shot['decision'])}</td>"
            f"<td><code>{esc(shot['file'])}</code></td>"
            "</tr>"
        )
    repaired = ", ".join(report["summary"]["repaired_shot_ids"]) or "none"
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>Continuity Film Report</title>
<style>body{{font:16px/1.5 system-ui;max-width:960px;margin:40px auto;padding:0 20px;color:#20242a}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd1d8;padding:8px;text-align:left}}code{{font-size:13px}}.ok{{color:#087a42}}.note{{background:#f3f5f7;padding:12px}}</style>
<h1>Continuity Film · {esc(report["summary"]["status"])}</h1>
<p>{esc(report["idea"])}</p>
<p class="note">Demo evidence mode: <b>{esc(report["audit"]["mode"])}</b>. The fixture auditor reads declared test findings; it does not claim a visual model watched the clips.</p>
<ul><li>Vendor: {esc(report["vendor"])}</li><li>Initial shots: 6</li><li>Repaired shots: {esc(repaired)}</li><li>Whole-film rerun: no</li><li>Repair rounds used: {esc(report["summary"]["repair_rounds_used"])} / {esc(report["policy"]["max_repair_rounds"])}</li></ul>
<table><thead><tr><th>Shot</th><th>Beat</th><th>Final take</th><th>Decision</th><th>File</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
</html>\n"""


def _write_reports(report: dict[str, Any], root: Path) -> tuple[Path, Path]:
    validate_film_report(report)
    json_path = root / "film-report.json"
    html_path = root / "film-report.html"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    html_path.write_text(_render_html(report), encoding="utf-8")
    return json_path, html_path


def validate_film_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate the invariants that make the repair claim meaningful."""

    if report.get("report_version") != FILM_REPORT_VERSION:
        raise FilmPipelineError("film report version mismatch")
    policy = report.get("policy") or {}
    summary = report.get("summary") or {}
    events = report.get("generation_events") or []
    audits = report.get("audit_events") or []
    if policy.get("shot_count") != 6 or policy.get("single_vendor") is not True:
        raise FilmPipelineError(
            "film report must describe one six-shot, single-vendor run"
        )
    if policy.get("regenerate_blockers_only") is not True:
        raise FilmPipelineError("film report must enforce blocker-only regeneration")
    if not 0 <= int(policy.get("max_repair_rounds", -1)) <= MAX_REPAIR_ROUNDS:
        raise FilmPipelineError("film report exceeds the two-round repair cap")
    initial = [event for event in events if event.get("round") == 0]
    if len(initial) != 6 or {event.get("shot_id") for event in initial} != {
        str(i) for i in range(1, 7)
    }:
        raise FilmPipelineError(
            "film report must contain exactly the six initial shots"
        )
    providers = {event.get("provider") for event in events}
    if providers != {report.get("vendor")}:
        raise FilmPipelineError("film report contains mixed vendors")
    permitted_repairs = {
        (int(event["round"]) + 1, str(event["shot_id"]))
        for event in audits
        if event.get("decision") == "REGENERATE"
        and int(event.get("round", -1)) < int(policy["max_repair_rounds"])
    }
    actual_repairs = {
        (int(event["round"]), str(event["shot_id"]))
        for event in events
        if int(event.get("round", 0)) > 0
    }
    if len(actual_repairs) != len(events) - len(initial):
        raise FilmPipelineError("film report contains duplicate repair generations")
    if actual_repairs - permitted_repairs:
        raise FilmPipelineError("film report regenerated a shot that was not a blocker")
    if summary.get("initial_generation_count") != 6:
        raise FilmPipelineError("film report initial generation count must be six")
    if summary.get("total_generation_count") != len(events):
        raise FilmPipelineError("film report generation count mismatch")
    if summary.get("whole_film_rerun") is not False:
        raise FilmPipelineError("whole-film rerun is forbidden")
    repaired_ids = sorted({shot_id for _, shot_id in actual_repairs}, key=int)
    if summary.get("repaired_shot_ids") != repaired_ids:
        raise FilmPipelineError("film report repaired-shot summary mismatch")
    return report


def run_film(
    idea: str,
    output_dir: str | Path,
    *,
    vendor: FilmVendor,
    auditor: ClipAuditor,
    max_repair_rounds: int = MAX_REPAIR_ROUNDS,
) -> FilmRun:
    """Run the six-shot loop without ever regenerating a passing shot."""

    if not 0 <= max_repair_rounds <= MAX_REPAIR_ROUNDS:
        raise ValueError(f"max_repair_rounds must be between 0 and {MAX_REPAIR_ROUNDS}")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan = run_create(
        idea,
        6,
        root / "plan",
        max_shots=6,
        allow_model=False,
    )
    if len(plan["shots"]) != 6:
        raise FilmPipelineError("film 模式必须得到恰好六个镜头。")
    store = CanonStore(plan["canon_path"])

    prompts = {
        shot["shot_id"]: _canon_prompt(
            store, shot, plan["prompts"][shot["shot_id"]]["positive"]
        )
        for shot in plan["shots"]
    }
    latest: dict[str, GeneratedClip] = {}
    generation_events: list[dict[str, Any]] = []
    audit_events: list[dict[str, Any]] = []
    repaired_shots: set[str] = set()

    def generate_shot(
        shot: dict[str, Any], round_number: int, prompt: str
    ) -> GeneratedClip:
        shot_id = shot["shot_id"]
        attempt = latest[shot_id].attempt + 1 if shot_id in latest else 1
        target = root / "clips" / f"shot-{int(shot_id):02d}-take-{attempt:02d}.mp4"
        clip = vendor.generate(shot, prompt, attempt, target)
        if clip.provider != vendor.name:
            raise FilmPipelineError("检测到混用供应商，film 运行已停止。")
        latest[shot_id] = clip
        generation_events.append(
            {
                "round": round_number,
                "shot_id": shot_id,
                "attempt": attempt,
                "provider": clip.provider,
                "file": _relative(clip.path, root),
            }
        )
        return clip

    for shot in plan["shots"]:
        generate_shot(shot, 0, prompts[shot["shot_id"]])

    def audit_clips(
        round_number: int, shot_ids: list[str]
    ) -> dict[str, list[BlockerFinding]]:
        blockers: dict[str, list[BlockerFinding]] = {}
        for shot_id in shot_ids:
            clip = latest[shot_id]
            findings = [
                item for item in auditor.audit(clip) if item.severity == "regenerate"
            ]
            audit_events.append(
                {
                    "round": round_number,
                    "shot_id": shot_id,
                    "attempt": clip.attempt,
                    "decision": "REGENERATE" if findings else "PASS",
                    "blockers": [asdict(item) for item in findings],
                }
            )
            if findings:
                blockers[shot_id] = findings
        return blockers

    blockers = audit_clips(0, [shot["shot_id"] for shot in plan["shots"]])
    rounds_used = 0
    for repair_round in range(1, max_repair_rounds + 1):
        if not blockers:
            break
        rounds_used = repair_round
        current = blockers
        repaired_shots.update(current)
        for shot in plan["shots"]:
            shot_id = shot["shot_id"]
            if shot_id in current:
                generate_shot(
                    shot,
                    repair_round,
                    _repair_prompt(prompts[shot_id], current[shot_id]),
                )
        blockers = audit_clips(repair_round, sorted(current, key=int))

    status = "PASS" if not blockers else "BLOCKED"
    video_path = None
    if status == "PASS":
        ffmpeg_exe = getattr(vendor, "ffmpeg_exe", None)
        if not ffmpeg_exe:
            raise FilmPipelineError(
                "当前 vendor 没有提供 ffmpeg 路径，无法拼接。",
                EXIT_FFMPEG_UNAVAILABLE,
            )
        video_path = _concat_clips(
            ffmpeg_exe,
            [latest[str(index)] for index in range(1, 7)],
            root,
        )

    report = {
        "report_version": FILM_REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "idea": idea.strip(),
        "vendor": vendor.name,
        "audit": {
            "mode": auditor.name,
            "evidence_note": (
                "The fixture audit consumes declared test findings; no visual model inspection is claimed."
            ),
        },
        "policy": {
            "shot_count": 6,
            "single_vendor": True,
            "regenerate_blockers_only": True,
            "max_repair_rounds": max_repair_rounds,
        },
        "summary": {
            "status": status,
            "initial_generation_count": 6,
            "total_generation_count": len(generation_events),
            "repair_rounds_used": rounds_used,
            "repaired_shot_ids": sorted(repaired_shots, key=int),
            "remaining_blocker_shot_ids": sorted(blockers, key=int),
            "whole_film_rerun": False,
            "film": _relative(video_path, root),
        },
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "beat": shot["beat_name"],
                "location": shot["location"],
                "canon_rule_count": len(
                    store.compile_rule_pack(shot["shot_id"])["rules"]
                ),
                "final_take": latest[shot["shot_id"]].attempt,
                "decision": "REGENERATE" if shot["shot_id"] in blockers else "PASS",
                "file": _relative(latest[shot["shot_id"]].path, root),
            }
            for shot in plan["shots"]
        ],
        "generation_events": generation_events,
        "audit_events": audit_events,
        "artifacts": {
            "canon": _relative(Path(plan["canon_path"]), root),
            "film": _relative(video_path, root),
            "json_report": "film-report.json",
            "html_report": "film-report.html",
        },
    }
    report_json, report_html = _write_reports(report, root)
    return FilmRun(
        exit_code=EXIT_OK if status == "PASS" else EXIT_BLOCKERS_REMAIN,
        report=report,
        report_json=report_json,
        report_html=report_html,
        video_path=video_path,
    )
