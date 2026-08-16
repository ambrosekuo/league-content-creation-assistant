"""Allowlisted requeue commands (Cloud Run / Workflows / local cloud_job.py)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

PROJECT = os.environ.get("GCP_PROJECT") or os.environ.get("PROJECT_ID") or "poststream-assistant"
REGION = os.environ.get("GCP_REGION") or os.environ.get("REGION") or "us-east1"

JOBS: dict[str, dict[str, Any]] = {
    "process-portraits": {
        "label": "Portraits",
        "help": "Render 9:16 facecam+KDA from lol_compilations/",
        "cloudJob": "vod-portrait-process",
        "args": [
            "cloud_job.py",
            "process-portraits",
            "--vod-id",
            "{vodId}",
            "--cleanup",
            "--clean-work",
            "--force",
        ],
        "needs": "vodId",
    },
    "process-clips": {
        "label": "Stitch weaves",
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
        "label": "Clips → portraits",
        "help": "Workflow: stitch weaves then render portraits",
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
            }
        )
    return out


def _fill(args: list[str], *, vod_id: str, day_key: str) -> list[str]:
    return [a.replace("{vodId}", vod_id).replace("{dayKey}", day_key) for a in args]


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
    filled = _fill(spec["args"], vod_id=vod_id, day_key=day_key) + extra
    if where == "local":
        return [sys.executable, str(ROOT / filled[0]), *filled[1:]]
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
    needs = spec["needs"]
    if needs == "vodId" and not vod_id:
        raise ValueError("vodId required")
    if needs == "dayKey" and not day_key:
        raise ValueError("dayKey required")
    cmd = preview_command(job_id, vod_id=vod_id, day_key=day_key, extra=extra, where=where)
    command_line = " ".join(shlex.quote(c) for c in cmd)
    console = (
        f"https://console.cloud.google.com/run/jobs/details/{REGION}/"
        f"{spec.get('cloudJob')}?project={PROJECT}"
        if spec.get("cloudJob")
        else f"https://console.cloud.google.com/workflows/workflow/{REGION}/"
        f"{spec.get('workflow')}/executions?project={PROJECT}"
    )
    if where == "local":
        logs = ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = vod_id or day_key or "job"
        log_path = logs / f"viewer-{job_id}-{stamp}.log"
        handle = log_path.open("w", encoding="utf-8")
        handle.write(command_line + "\n\n")
        handle.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
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
