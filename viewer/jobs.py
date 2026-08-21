"""Allowlisted requeue commands (Cloud Run / Workflows / local cloud_job.py)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from dataset_paths import find_dataset_dir

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"


def _local_python() -> str:
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return sys.executable

PROJECT = os.environ.get("GCP_PROJECT") or os.environ.get("PROJECT_ID") or "poststream-assistant"
REGION = os.environ.get("GCP_REGION") or os.environ.get("REGION") or "us-east1"

JOBS: dict[str, dict[str, Any]] = {
    "review-stitch": {
        "label": "Stitch picks",
        "help": "Stitch godly+excellent into lol_compilations_picks/ (local)",
        "localOnly": True,
        "surface": "review",
        "args": [
            "review_export.py",
            "--vod-id",
            "{vodId}",
            "--skip-portrait",
        ],
        "needs": "vodId",
    },
    "review-portraits": {
        "label": "Picks portraits",
        "help": "Dry 9:16 from stitched picks (local)",
        "localOnly": True,
        "surface": "review",
        "args": [
            "review_export.py",
            "--vod-id",
            "{vodId}",
            "--portrait-only",
        ],
        "needs": "vodId",
    },
    "review-decorate": {
        "label": "Decorate picks",
        "help": "Combos + captions + wrap on dry picks (local, no music)",
        "localOnly": True,
        "surface": "review",
        "args": [
            "decorate_portrait.py",
            "--dataset-id",
            "{vodId}",
            "--from-picks",
            "--music",
            "off",
        ],
        "needs": "vodId",
    },
    "review-music": {
        "label": "Add music",
        "help": "Mix a chosen pool track onto decorated picks (local)",
        "localOnly": True,
        "surface": "review",
        "args": [
            "mix_portrait_music.py",
            "--dataset-id",
            "{vodId}",
            "--from-picks",
        ],
        "needs": "vodId",
    },
    "review-post": {
        "label": "Post shorts",
        "help": "Private YouTube upload + TikTok draft from post/ (local)",
        "localOnly": True,
        "surface": "review",
        "wait": True,
        "timeout": 1800,
        "args": [
            "post_short.py",
            "--dataset-id",
            "{vodId}",
            "--from-picks",
        ],
        "needs": "vodId",
    },
    "process-portraits": {
        "label": "Portraits (auto)",
        "help": "Dry 9:16 of auto weaves (no intro/outro/music)",
        "cloudJob": "vod-portrait-process",
        "args": [
            "cloud_job.py",
            "process-portraits",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--clean-work",
            "--force",
            "--preset",
            "veryfast",
            "--crf",
            "20",
            "--track-champion",
            "--game-zoom",
            "0.65",
            "--cam-hole",
            "fill",
            "--music",
            "off",
            "--intro",
            "none",
            "--no-outro",
        ],
        "needs": "vodId",
    },
    "decorate-portraits": {
        "label": "Decorate (auto)",
        "help": "Combos + captions + wrap on auto dry portraits (no music)",
        "cloudJob": "vod-portrait-process",
        "args": [
            "cloud_job.py",
            "process-decorate-portraits",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--clean-work",
            "--force",
        ],
        "needs": "vodId",
    },
    "process-clips": {
        "label": "Stitch weaves (auto)",
        "help": "Rank + stitch lol_clips/ into lol_compilations/",
        "cloudJob": "vod-clip-process",
        "args": [
            "cloud_job.py",
            "process-clips",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--clean-work",
        ],
        "needs": "vodId",
    },
    "recut-clips": {
        "label": "Recut clips",
        "help": "Re-snap + recut from source.mp4 (no Twitch download)",
        "cloudJob": "vod-archive-nightly",
        "args": [
            "cloud_job.py",
            "recut-clips",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--fast",
        ],
        "needs": "vodId",
    },
    "process-vod": {
        "label": "Archive VOD",
        "help": "Ingest + index + cut (Twitch if no GCS source)",
        "cloudJob": "vod-archive-nightly",
        "args": [
            "cloud_job.py",
            "process-vod",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--fast",
        ],
        "needs": "vodId",
    },
    "process-daily": {
        "label": "Daily compilation",
        "help": "Rank clips across the day → vods/{day}/_daily/",
        "cloudJob": "vod-clip-process",
        "args": [
            "cloud_job.py",
            "process-daily",
            "--day-key",
            "{dayKey}",
            "--cleanup",
        ],
        "needs": "dayKey",
    },
    "clip-then-portrait": {
        "label": "Clips → portraits (auto)",
        "help": "Workflow: auto stitch then dry portraits",
        "workflow": "clip-then-portrait",
        "needs": "vodId",
    },
    "archive-clip": {
        "label": "Archive → clips",
        "help": "Workflow: ingest/cut then stitch weaves",
        "workflow": "archive-clip-portrait",
        "needs": "vodId",
    },
}


def catalog() -> list[dict[str, Any]]:
    out = []
    for key, spec in JOBS.items():
        out.append(
            {
                "id": key,
                "label": spec["label"],
                "help": spec["help"],
                "needs": spec["needs"],
                "kind": "workflow" if spec.get("workflow") else "job",
                "target": spec.get("workflow") or spec.get("cloudJob"),
                "localOnly": bool(spec.get("localOnly")),
                "surface": spec.get("surface") or "archive",
            }
        )
    return out


def _fill(args: list[str], *, vod_id: str, day_key: str) -> list[str]:
    return [a.replace("{vodId}", vod_id).replace("{dayKey}", day_key) for a in args]


def _active_path(vod_id: str) -> Path:
    vid = (vod_id or "").strip().lstrip("v")
    return LOGS / f"viewer-active-{vid}.json"


def _script_name(cmd: list[str]) -> str:
    for part in cmd:
        if str(part).endswith(".py"):
            return Path(str(part)).name
    return ""


def _pid_running(pid: int, *, script: str = "", vod_id: str = "") -> bool:
    if pid <= 0:
        return False
    try:
        args = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    if not args:
        return False
    if script and script not in args:
        return False
    vid = (vod_id or "").strip().lstrip("v")
    if vid and vid not in args:
        return False
    return True


def _log_tail(path: str, *, limit: int = 6000) -> str:
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _write_active(payload: dict[str, Any]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    path = _active_path(str(payload.get("vodId") or ""))
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _clear_active(vod_id: str, pid: int | None = None) -> None:
    path = _active_path(vod_id)
    if not path.is_file():
        return
    if pid is not None:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("pid") not in {None, pid}:
            return
    try:
        path.unlink()
    except OSError:
        pass


def active_job(vod_id: str) -> dict[str, Any] | None:
    vid = (vod_id or "").strip().lstrip("v")
    if not vid:
        return None
    path = _active_path(vid)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pid = int(payload.get("pid") or 0)
    script = str(payload.get("script") or "")
    running = _pid_running(pid, script=script, vod_id=vid)
    payload["running"] = running
    payload["logTail"] = _log_tail(str(payload.get("log") or ""))
    if not running:
        _clear_active(vid, pid)
        return None
    return payload


def preview_command(
    job_id: str,
    *,
    vod_id: str,
    day_key: str,
    extra: list[str] | None = None,
    where: str = "cloud",
) -> list[str]:
    spec = JOBS.get(job_id)
    if spec is None:
        raise KeyError(f"unknown job: {job_id}")
    extra = extra or []
    if spec.get("localOnly"):
        where = "local"
    if spec.get("workflow"):
        data = json.dumps({"vodId": vod_id})
        return [
            "gcloud",
            "workflows",
            "run",
            spec["workflow"],
            f"--location={REGION}",
            f"--project={PROJECT}",
            f"--data={data}",
        ]
    filled = _fill(spec.get("args") or [], vod_id=vod_id, day_key=day_key) + extra
    if where == "local":
        if not filled:
            raise ValueError(f"{job_id} has no local command")
        return [_local_python(), str(ROOT / filled[0]), *filled[1:]]
    if not spec.get("cloudJob"):
        raise ValueError(f"{job_id} is local-only")
    return [
        "gcloud",
        "run",
        "jobs",
        "execute",
        spec["cloudJob"],
        f"--region={REGION}",
        f"--project={PROJECT}",
        "--args=" + ",".join(filled),
    ]


def run_job(
    job_id: str,
    *,
    vod_id: str = "",
    day_key: str = "",
    extra: list[str] | None = None,
    where: str = "cloud",
) -> dict[str, Any]:
    spec = JOBS.get(job_id)
    if spec is None:
        raise KeyError(f"unknown job: {job_id}")
    if spec.get("localOnly"):
        where = "local"
    needs = spec["needs"]
    if needs == "vodId" and not vod_id:
        raise ValueError("vodId required")
    if needs == "dayKey" and not day_key:
        raise ValueError("dayKey required")
    if spec.get("localOnly"):
        found = find_dataset_dir(DATA, vod_id)
        if found is None:
            raise ValueError("No local dataset. Sync clips from review first.")
        current = active_job(vod_id)
        if current:
            raise ValueError(
                f"{current.get('label') or current.get('job')} is already running "
                f"(pid {current.get('pid')})"
            )
    cmd = preview_command(job_id, vod_id=vod_id, day_key=day_key, extra=extra, where=where)
    command_line = " ".join(shlex.quote(c) for c in cmd)
    if spec.get("cloudJob"):
        console = (
            f"https://console.cloud.google.com/run/jobs/details/{REGION}/"
            f"{spec['cloudJob']}?project={PROJECT}"
        )
    elif spec.get("workflow"):
        console = (
            f"https://console.cloud.google.com/workflows/workflow/{REGION}/"
            f"{spec['workflow']}/executions?project={PROJECT}"
        )
    else:
        console = None
    if where == "local":
        LOGS.mkdir(parents=True, exist_ok=True)
        stamp = vod_id or day_key or "job"
        log_path = LOGS / f"viewer-{job_id}-{stamp}.log"
        # Mixes and uploads wait so the review UI can show Mixing… / Posting…
        # until they finish. Stitch / portraits / decorate return immediately
        # and stay visible via logs/viewer-active-{vod}.json.
        wait = bool(spec.get("wait")) or job_id == "review-music"
        handle = log_path.open("w", encoding="utf-8")
        handle.write(command_line + "\n\n")
        handle.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=not wait,
        )
        _write_active(
            {
                "job": job_id,
                "label": spec.get("label") or job_id,
                "vodId": vod_id,
                "dayKey": day_key,
                "pid": proc.pid,
                "log": str(log_path),
                "script": _script_name(cmd),
                "commandLine": command_line,
                "background": not wait,
            }
        )
        if wait:
            timeout = float(spec.get("timeout") or 600)
            returncode = -1
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                returncode = proc.returncode if proc.returncode is not None else -1
            finally:
                handle.close()
                _clear_active(vod_id, proc.pid)
            tail = _log_tail(str(log_path), limit=8000)
            return {
                "ok": returncode == 0,
                "background": False,
                "returncode": returncode,
                "pid": proc.pid,
                "log": str(log_path),
                "command": cmd,
                "commandLine": command_line,
                "stdout": tail or f"exit {returncode}",
                "stderr": "",
                "console": None,
            }
        return {
            "ok": True,
            "background": True,
            "pid": proc.pid,
            "log": str(log_path),
            "command": cmd,
            "commandLine": command_line,
            "stdout": f"started pid {proc.pid}\nlog {log_path}",
            "stderr": "",
            "console": None,
        }
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "command": cmd,
            "commandLine": command_line,
            "stdout": "",
            "stderr": f"missing executable: {exc}",
            "console": console,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "command": cmd,
            "commandLine": command_line,
            "stdout": "",
            "stderr": "timed out waiting for gcloud (120s)",
            "console": console,
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "commandLine": command_line,
        "stdout": (proc.stdout or "")[-8000:],
        "stderr": (proc.stderr or "")[-8000:],
        "console": console,
    }
