#!/usr/bin/env python3
"""
PatchPilot — Audio Mastering API
=================================

A single-file production-ready Python application that combines a FastAPI
backend, a background job queue, and Matchering 2.0 audio
matching/mastering integration.

Features
--------
- REST API for uploading target + reference audio, submitting mastering
  jobs, polling job status, listing jobs, cancelling queued jobs, and
  downloading mastered results.
- Background job queue using ``asyncio.Queue`` with a dedicated async
  worker loop.
- Two processing modes:
    * ``direct``     — in-process Matchering Python API (blocking call
      dispatched to a ``ThreadPoolExecutor`` so the event loop stays free).
    * ``subprocess`` — external ``mg_cli.py`` CLI invoked via
      ``asyncio.create_subprocess_exec`` with ``asyncio.wait_for`` timeout
      and proper process kill/reap on timeout.
- Pydantic v2 validation models for every request and response.
- Multipart file upload with **streaming chunked writes** (the full file
  is never loaded into memory), extension validation, size enforcement,
  and path-traversal protection.
- Subprocess timeout handling: hard cap per job, process killed and
  reaped on timeout, truncated stdout/stderr capture.
- Robust structured error logging: request-ID middleware, per-job
  exception capture with full tracebacks in logs, sanitized
  client-facing error messages, global unhandled-exception handler.
- Startup checks: verifies directories, libsndfile (via soundfile),
  FFmpeg, Matchering import, and matchering-cli availability; results
  exposed through ``GET /health``.

Configuration (environment variables)
-------------------------------------
    PATCHPILOT_HOST              Listen host        (default 0.0.0.0)
    PATCHPILOT_PORT              Listen port        (default 8000)
    PATCHPILOT_UPLOAD_DIR        Upload directory    (default ./uploads)
    PATCHPILOT_OUTPUT_DIR        Output directory    (default ./outputs)
    PATCHPILOT_TEMP_DIR          Temp directory      (default ./tmp)
    PATCHPILOT_MAX_UPLOAD_MB     Max upload size MB  (default 200)
    PATCHPILOT_MAX_WORKERS       Worker thread count (default 1)
    PATCHPILOT_QUEUE_MAXSIZE     Queue capacity      (default 50)
    PATCHPILOT_TIMEOUT_SECONDS    Processing timeout  (default 1800 = 30 min)
    PATCHPILOT_MATCHERING_CLI    CLI script path/name (default mg_cli.py)
    PATCHPILOT_CORS_ORIGINS      CORS origins, comma-sep or * (default localhost)

Requirements
------------
    pip install fastapi uvicorn[standard] matchering pydantic python-multipart soundfile

System dependencies
-------------------
    sudo apt install libsndfile1 ffmpeg        # Debian/Ubuntu
    brew install libsndfile ffmpeg              # macOS
    pkg install libsndfile ffmpeg               # Termux (Android)

Running
-------
    python patchpilot.py
    # or:
    uvicorn patchpilot:app --host 0.0.0.0 --port 8000

Important caveats
-----------------
- The in-memory job store and queue are suitable for a **single-process**
  Uvicorn deployment (``--workers 1``).  For multi-worker production,
  replace ``JobStore`` and ``asyncio.Queue`` with Redis / Celery / RQ.
- ``MAX_WORKERS`` defaults to 1 because the Matchering Python API uses
  module-level state (logging handlers, temp files) that is not
  guaranteed thread-safe.
- Uploaded files are saved with UUID-based names; original filenames are
  preserved only as metadata.
- Subprocess mode requires ``mg_cli.py`` (from the matchering-cli repo)
  to be on ``PATH`` or locatable near the matchering package.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# Optional heavy deps — imported eagerly so startup checks can detect
# missing packages and report them via /health.
try:
    import matchering as mg
except ImportError:  # pragma: no cover
    mg = None  # type: ignore[assignment]

try:
    import soundfile as sf
except ImportError:  # pragma: no cover
    sf = None  # type: ignore[assignment]


# ===========================================================================
# Configuration (environment variables with sensible defaults)
# ===========================================================================

BASE_DIR: Path = Path(__file__).resolve().parent

UPLOAD_DIR: Path = Path(
    os.environ.get("PATCHPILOT_UPLOAD_DIR", str(BASE_DIR / "uploads"))
)
OUTPUT_DIR: Path = Path(
    os.environ.get("PATCHPILOT_OUTPUT_DIR", str(BASE_DIR / "outputs"))
)
TEMP_DIR: Path = Path(
    os.environ.get("PATCHPILOT_TEMP_DIR", str(BASE_DIR / "tmp"))
)

MAX_UPLOAD_SIZE_BYTES: int = int(
    os.environ.get("PATCHPILOT_MAX_UPLOAD_MB", "200")
) * 1024 * 1024

MAX_WORKERS: int = int(os.environ.get("PATCHPILOT_MAX_WORKERS", "1"))
QUEUE_MAXSIZE: int = int(os.environ.get("PATCHPILOT_QUEUE_MAXSIZE", "50"))
PROCESSING_TIMEOUT_SECONDS: int = int(
    os.environ.get("PATCHPILOT_TIMEOUT_SECONDS", "1800")
)
MATCHERING_CLI_BIN: str = os.environ.get(
    "PATCHPILOT_MATCHERING_CLI", "mg_cli.py"
)

LISTEN_HOST: str = os.environ.get("PATCHPILOT_HOST", "0.0.0.0")
LISTEN_PORT: int = int(os.environ.get("PATCHPILOT_PORT", "8000"))

APP_VERSION: str = "2.0.0"

ALLOWED_EXTENSIONS: set[str] = {
    "wav", "flac", "mp3", "aiff", "aif", "ogg", "m4a",
}

# soundfile / Matchering subtype mapping
BIT_DEPTH_TO_SUBTYPE: dict[int, str] = {
    16: "PCM_16",
    24: "PCM_24",
    32: "FLOAT",
}

# Cap captured stdout/stderr lines per job to prevent memory bloat.
MAX_LOG_LINES_PER_STREAM: int = 200


# ===========================================================================
# Logging configuration
# ===========================================================================

logger = logging.getLogger("patchpilot")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(_console_handler)


# ===========================================================================
# Pydantic validation models
# ===========================================================================

class JobStatus(str, Enum):
    """Lifecycle states for a mastering job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProcessingMode(str, Enum):
    """How the audio is processed."""

    DIRECT = "direct"          # In-process Matchering Python API
    SUBPROCESS = "subprocess"  # External matchering-cli command


class BitDepth(int, Enum):
    """Output bit depth."""

    BIT_16 = 16
    BIT_24 = 24
    BIT_32 = 32


class MasteringOptions(BaseModel):
    """User-supplied options that control the mastering pipeline."""

    bit_depth: BitDepth = BitDepth.BIT_16
    normalize: bool = True
    use_limiter: bool = True
    mode: ProcessingMode = ProcessingMode.DIRECT
    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    max_length: float = Field(default=900.0, gt=0, le=7200)


class JobRecord(BaseModel):
    """Internal representation of a job, also returned via the API."""

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: float = 0.0
    logs: list[str] = Field(default_factory=list)
    error: str | None = None
    target_original_name: str = ""
    reference_original_name: str = ""
    target_path: str = ""
    reference_path: str = ""
    output_path: str = ""
    options: MasteringOptions = Field(default_factory=MasteringOptions)

    model_config = {"arbitrary_types_allowed": True}


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: float
    created_at: float
    started_at: float | None
    finished_at: float | None
    error: str | None
    logs: list[str]
    download_url: str | None
    options: MasteringOptions


class StartupCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class HealthResponse(BaseModel):
    status: str
    version: str
    checks: list[StartupCheck]
    active_jobs: int
    queued_jobs: int


class ErrorResponse(BaseModel):
    detail: str


# ===========================================================================
# Job store  (in-memory; see module docstring for production notes)
# ===========================================================================

class JobStore:
    """Async-safe in-memory job registry."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: JobRecord) -> None:
        async with self._lock:
            self._jobs[record.job_id] = record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **fields: Any) -> JobRecord | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            return job

    async def all(self) -> list[JobRecord]:
        async with self._lock:
            return list(self._jobs.values())

    async def count_by_status(self, status: JobStatus) -> int:
        async with self._lock:
            return sum(1 for j in self._jobs.values() if j.status == status)


# ===========================================================================
# Global state  (initialised in lifespan)
# ===========================================================================

store: JobStore = JobStore()
queue: asyncio.Queue[str] | None = None
executor: ThreadPoolExecutor | None = None
_startup_checks: list[StartupCheck] = []
_workers_running: bool = False


# ===========================================================================
# Utility helpers
# ===========================================================================

def _safe_join(base: Path, *parts: str) -> Path:
    """Join *parts* onto *base* and verify the result stays inside *base*."""
    joined = (base / Path(*parts)).resolve()
    base_resolved = base.resolve()
    try:
        joined.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"Path escapes base directory: {joined}")
    return joined


def _validate_extension(filename: str) -> str:
    """Return the lowercase extension or raise HTTPException."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file extension '.{ext}'. "
                f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
            ),
        )
    return ext


async def _save_upload_streaming(
    upload: UploadFile, dest_dir: Path, prefix: str
) -> tuple[str, str]:
    """
    Save an UploadFile to *dest_dir* using **streaming chunked writes**.

    The full file content is never loaded into memory.  Size is enforced
    during streaming.  Returns ``(saved_path_str, original_filename)``.
    """
    original_name = upload.filename or "unknown.wav"
    ext = _validate_extension(original_name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{prefix}_{uuid.uuid4().hex}.{ext}"
    dest_path = _safe_join(dest_dir, safe_name)

    chunk_size = 1024 * 1024  # 1 MB
    total_size = 0
    file_handle = None

    try:
        file_handle = open(dest_path, "wb")
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE_BYTES:
                file_handle.close()
                dest_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"File '{original_name}' exceeds max upload size "
                        f"({MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB)."
                    ),
                )
            file_handle.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        if file_handle:
            file_handle.close()
        dest_path.unlink(missing_ok=True)
        logger.error("Failed to save upload '%s': %s", original_name, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save uploaded file.",
        )
    finally:
        if file_handle:
            file_handle.close()

    logger.info(
        "Saved upload '%s' -> %s (%d bytes)", original_name, dest_path, total_size
    )
    return str(dest_path), original_name


def _sanitize_error_message(exc: Exception) -> str:
    """Return a safe, truncated error message for API responses.

    Full tracebacks are logged separately; the client sees only a
    sanitized summary to avoid leaking internal paths or excessive detail.
    """
    if isinstance(exc, asyncio.TimeoutError) or (
        "timed out" in str(exc).lower()
    ):
        return f"Processing timed out after {PROCESSING_TIMEOUT_SECONDS} seconds."
    msg = str(exc)
    if len(msg) > 300:
        msg = msg[:300] + "..."
    return f"{type(exc).__name__}: {msg}"


def _truncate_log_lines(text: str, max_lines: int = MAX_LOG_LINES_PER_STREAM) -> list[str]:
    """Split text into lines and truncate to *max_lines* entries."""
    if not text:
        return []
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines.append(f"... [truncated {len(text.strip().splitlines()) - max_lines} lines]")
    return lines


# ===========================================================================
# Startup checks
# ===========================================================================

def run_startup_checks() -> list[StartupCheck]:
    """Run all environment / dependency checks and return the results."""
    checks: list[StartupCheck] = []

    # --- Directories ---
    for d in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file = d / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            checks.append(StartupCheck(
                name=f"directory:{d.name}",
                passed=True,
                detail=str(d),
            ))
        except Exception as exc:
            checks.append(StartupCheck(
                name=f"directory:{d.name}",
                passed=False,
                detail=f"Cannot create/write to {d}: {exc}",
            ))

    # --- Matchering Python package ---
    if mg is not None:
        checks.append(StartupCheck(
            name="matchering",
            passed=True,
            detail=f"matchering {getattr(mg, '__version__', 'unknown')} imported",
        ))
    else:
        checks.append(StartupCheck(
            name="matchering",
            passed=False,
            detail="pip install matchering  (also requires libsndfile)",
        ))

    # --- soundfile (libsndfile bridge) ---
    if sf is not None:
        try:
            formats = sf.available_formats()
            checks.append(StartupCheck(
                name="soundfile",
                passed=True,
                detail=f"libsndfile OK, formats: {len(formats)}",
            ))
        except Exception as exc:
            checks.append(StartupCheck(
                name="soundfile",
                passed=False,
                detail=f"soundfile import OK but libsndfile missing: {exc}",
            ))
    else:
        checks.append(StartupCheck(
            name="soundfile",
            passed=False,
            detail="pip install soundfile  (system: apt install libsndfile1)",
        ))

    # --- FFmpeg ---
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        try:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            first_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
            checks.append(StartupCheck(
                name="ffmpeg",
                passed=True,
                detail=f"{ffmpeg_path} - {first_line}",
            ))
        except Exception as exc:
            checks.append(StartupCheck(
                name="ffmpeg",
                passed=False,
                detail=f"ffmpeg found at {ffmpeg_path} but failed: {exc}",
            ))
    else:
        checks.append(StartupCheck(
            name="ffmpeg",
            passed=False,
            detail="ffmpeg not on PATH (optional; needed for MP3 input)",
        ))

    # --- matchering-cli (optional, for subprocess mode) ---
    cli_found = shutil.which(MATCHERING_CLI_BIN)
    if cli_found is None and mg is not None:
        pkg_dir = Path(mg.__file__).resolve().parent.parent
        candidate = pkg_dir / "matchering-cli" / MATCHERING_CLI_BIN
        if candidate.exists():
            cli_found = str(candidate)
    if cli_found:
        checks.append(StartupCheck(
            name="matchering-cli",
            passed=True,
            detail=f"CLI at {cli_found} (subprocess mode available)",
        ))
    else:
        checks.append(StartupCheck(
            name="matchering-cli",
            passed=False,
            detail="matchering-cli not found (subprocess mode unavailable)",
        ))

    return checks


# ===========================================================================
# Matchering processing — direct mode (sync, runs in ThreadPoolExecutor)
# ===========================================================================

def _log_handler_factory(job: JobRecord) -> Any:
    """Return a log handler that appends Matchering log lines to the job record.

    Runs in a sync thread; list.append is thread-safe in CPython.
    """

    def handler(message: Any) -> None:
        text = str(message).rstrip()
        if text:
            logger.info("[job %s] %s", job.job_id, text)
            job.logs.append(text)

    return handler


def process_direct(job: JobRecord) -> str:
    """
    Run Matchering mastering using the in-process Python API.

    Returns the path to the mastered output file.
    Raises ``RuntimeError`` on failure.
    """
    if mg is None:
        raise RuntimeError("Matchering library is not installed.")

    output_dir = OUTPUT_DIR / job.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"mastered_{job.job_id}.wav"

    opts = job.options
    subtype = BIT_DEPTH_TO_SUBTYPE.get(opts.bit_depth.value, "PCM_16")

    result = mg.Result(
        file=str(output_path),
        subtype=subtype,
        use_limiter=opts.use_limiter,
        normalize=opts.normalize,
    )

    config = mg.Config(
        internal_sample_rate=opts.sample_rate,
        max_length=opts.max_length,
        temp_folder=str(TEMP_DIR),
    )

    handler = _log_handler_factory(job)
    mg.log(default_handler=handler, info_handler=handler, warning_handler=handler)

    logger.info("[job %s] Starting direct Matchering processing", job.job_id)
    logger.info(
        "[job %s] target=%s  reference=%s  output=%s  bit_depth=%d  "
        "normalize=%s  limiter=%s",
        job.job_id, job.target_path, job.reference_path, output_path,
        opts.bit_depth.value, opts.normalize, opts.use_limiter,
    )

    mg.process(
        target=job.target_path,
        reference=job.reference_path,
        results=[result],
        config=config,
    )

    if not output_path.exists():
        raise RuntimeError(
            f"Matchering completed but output file was not created: {output_path}"
        )

    logger.info("[job %s] Direct processing complete: %s", job.job_id, output_path)
    return str(output_path)


# ===========================================================================
# Matchering processing — subprocess mode (async, uses create_subprocess_exec)
# ===========================================================================

def _find_matchering_cli() -> str:
    """Locate the matchering-cli script on PATH or near the package."""
    cli_path = shutil.which(MATCHERING_CLI_BIN)
    if cli_path is not None:
        return cli_path
    if mg is not None:
        pkg_dir = Path(mg.__file__).resolve().parent.parent
        candidate = pkg_dir / "matchering-cli" / MATCHERING_CLI_BIN
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(
        f"matchering-cli ('{MATCHERING_CLI_BIN}') not found on PATH "
        f"or near the matchering package."
    )


def _build_subprocess_argv(job: JobRecord, output_path: Path) -> list[str]:
    """Build the argv list for the matchering-cli subprocess."""
    opts = job.options
    cli_path = _find_matchering_cli()
    cmd: list[str] = [
        sys.executable,
        cli_path,
        job.target_path,
        job.reference_path,
        str(output_path),
        f"-b{opts.bit_depth.value}",
    ]
    if not opts.use_limiter:
        cmd.append("--no_limiter")
    if not opts.normalize:
        cmd.append("--dont_normalize")
    return cmd


async def process_subprocess_async(job: JobRecord) -> str:
    """
    Run Matchering mastering via the external ``mg_cli.py`` CLI using
    ``asyncio.create_subprocess_exec``.

    Features:
    - Non-blocking: runs as an async subprocess, no thread consumed.
    - Timeout: ``asyncio.wait_for`` enforces a hard cap; on timeout the
      process is killed and reaped before raising.
    - Output capture: stdout/stderr captured, truncated, logged, and
      appended to the job record.

    Returns the path to the mastered output file.
    Raises ``RuntimeError`` on failure or timeout.
    """
    output_dir = OUTPUT_DIR / job.job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"mastered_{job.job_id}.wav"

    cmd = _build_subprocess_argv(job, output_path)

    logger.info(
        "[job %s] Starting subprocess Matchering: %s",
        job.job_id, " ".join(cmd),
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=PROCESSING_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        # Kill the process and reap it before raising.
        logger.error(
            "[job %s] Subprocess timed out after %ds, killing PID %s",
            job.job_id, PROCESSING_TIMEOUT_SECONDS, proc.pid,
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # Already exited
        await proc.wait()
        raise RuntimeError(
            f"Subprocess timed out after {PROCESSING_TIMEOUT_SECONDS} seconds."
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    # Capture and log stdout
    for line in _truncate_log_lines(stdout):
        logger.info("[job %s] CLI stdout: %s", job.job_id, line)
        job.logs.append(f"[CLI] {line}")

    # Capture and log stderr
    for line in _truncate_log_lines(stderr):
        logger.warning("[job %s] CLI stderr: %s", job.job_id, line)
        job.logs.append(f"[CLI stderr] {line}")

    if proc.returncode != 0:
        # Sanitized error: client sees return code, full stderr stays in logs.
        raise RuntimeError(
            f"matchering-cli exited with code {proc.returncode}."
        )

    if not output_path.exists():
        raise RuntimeError(
            f"matchering-cli completed but output file not found: {output_path}"
        )

    logger.info(
        "[job %s] Subprocess processing complete: %s", job.job_id, output_path
    )
    return str(output_path)


# ===========================================================================
# Async worker loop
# ===========================================================================

async def _worker_loop(name: str) -> None:
    """Consume job_ids from the queue and dispatch them to processing."""
    global _workers_running
    assert queue is not None, "Queue must be initialised before workers start"
    assert executor is not None, "Executor must be initialised before workers start"

    logger.info("Worker '%s' started", name)

    while _workers_running:
        try:
            job_id = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        job = await store.get(job_id)
        if job is None:
            logger.warning("Worker '%s': job %s not found in store", name, job_id)
            queue.task_done()
            continue

        if job.status == JobStatus.CANCELLED:
            logger.info(
                "Worker '%s': job %s was cancelled before start", name, job_id
            )
            queue.task_done()
            continue

        # Mark as running
        await store.update(
            job_id,
            status=JobStatus.RUNNING,
            started_at=time.time(),
            progress=0.1,
        )
        job.logs.append("Job started.")

        logger.info("Worker '%s' dispatching job %s (mode=%s)",
                     name, job_id, job.options.mode.value)

        try:
            if job.options.mode == ProcessingMode.SUBPROCESS:
                # Async subprocess — no thread needed
                output_path = await process_subprocess_async(job)
            else:
                # Direct mode — blocking call dispatched to thread pool
                loop = asyncio.get_running_loop()
                output_path = await loop.run_in_executor(
                    executor, process_direct, job
                )

            await store.update(
                job_id,
                status=JobStatus.SUCCEEDED,
                output_path=output_path,
                progress=1.0,
                finished_at=time.time(),
            )
            job.logs.append("Processing completed successfully.")
            logger.info("[job %s] Marked as SUCCEEDED", job_id)

        except Exception as exc:
            error_msg = _sanitize_error_message(exc)
            # Full traceback goes to logs only; API sees sanitized message.
            logger.error(
                "[job %s] FAILED: %s\n%s", job_id, exc,
                traceback.format_exc(),
            )
            job.logs.append(f"Processing failed: {error_msg}")
            await store.update(
                job_id,
                status=JobStatus.FAILED,
                error=error_msg,
                finished_at=time.time(),
            )

        finally:
            queue.task_done()

    logger.info("Worker '%s' stopped", name)


# ===========================================================================
# FastAPI lifespan  (startup + shutdown)
# ===========================================================================

async def lifespan(app: FastAPI) -> None:
    """Run startup checks and spin up worker tasks; clean up on shutdown."""
    global queue, executor, _startup_checks, _workers_running

    logger.info("=" * 60)
    logger.info("PatchPilot Audio Mastering API v%s starting up", APP_VERSION)
    logger.info("=" * 60)

    # --- Startup checks ---
    _startup_checks = run_startup_checks()
    for check in _startup_checks:
        level = logging.INFO if check.passed else logging.WARNING
        logger.log(
            level, "  [%s] %s: %s",
            "OK" if check.passed else "FAIL", check.name, check.detail,
        )

    failed = [c for c in _startup_checks if not c.passed]
    critical_failures = [c for c in failed if c.name in ("matchering", "soundfile")]
    if critical_failures:
        for c in critical_failures:
            logger.error(
                "Critical startup check failed: %s - %s", c.name, c.detail
            )
        logger.error(
            "Fix critical failures and restart. The API will still start "
            "but mastering jobs will fail until dependencies are resolved."
        )

    # --- Initialise queue and executor ---
    if MAX_WORKERS > 1:
        logger.warning(
            "MAX_WORKERS=%d > 1: direct mode is NOT thread-safe "
            "(Matchering uses module-level state). Use subprocess mode "
            "or keep MAX_WORKERS=1.",
            MAX_WORKERS,
        )
    queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    executor = ThreadPoolExecutor(
        max_workers=MAX_WORKERS,
        thread_name_prefix="patchpilot-worker",
    )
    _workers_running = True

    # --- Start worker tasks ---
    worker_tasks: list[asyncio.Task] = []
    for i in range(MAX_WORKERS):
        task = asyncio.create_task(_worker_loop(f"worker-{i + 1}"))
        worker_tasks.append(task)

    logger.info(
        "Started %d worker(s), queue capacity=%d, timeout=%ds",
        MAX_WORKERS, QUEUE_MAXSIZE, PROCESSING_TIMEOUT_SECONDS,
    )
    logger.info("Upload dir:  %s", UPLOAD_DIR)
    logger.info("Output dir:  %s", OUTPUT_DIR)
    logger.info("Temp dir:    %s", TEMP_DIR)
    logger.info("Max upload:  %d MB", MAX_UPLOAD_SIZE_BYTES // (1024 * 1024))

    yield  # --- App runs here ---

    # --- Shutdown ---
    logger.info("Shutting down PatchPilot...")
    _workers_running = False

    for task in worker_tasks:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()

    if executor:
        executor.shutdown(wait=True, cancel_futures=True)

    logger.info("Shutdown complete.")


# ===========================================================================
# FastAPI application
# ===========================================================================

app = FastAPI(
    title="PatchPilot — Audio Mastering API",
    description=(
        "Upload a target track and a reference track, submit a mastering "
        "job, poll for completion, and download the mastered result. "
        "Powered by Matchering 2.0."
    ),
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS middleware -- origins configurable via env var (comma-separated).
# Default restricts to localhost for safety; set PATCHPILOT_CORS_ORIGINS=* to allow all.
_cors_env = os.environ.get("PATCHPILOT_CORS_ORIGINS", "")
if _cors_env.strip() == "*":
    _cors_origins: list[str] = ["*"]
    _cors_credentials = False
else:
    _cors_origins = (
        [o.strip() for o in _cors_env.split(",") if o.strip()]
        if _cors_env.strip()
        else ["http://localhost", "http://localhost:3000", "http://127.0.0.1"]
    )
    _cors_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Middleware: request ID + structured request logging
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Any) -> Any:
    """Attach a unique request ID to every request and log it."""
    request_id = request.headers.get(
        "X-Request-ID", uuid.uuid4().hex[:12]
    )
    request.state.request_id = request_id

    logger.info(
        "[%s] %s %s", request_id, request.method, request.url.path
    )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unhandled exceptions and return a clean 500 response."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        "[%s] Unhandled exception: %s", request_id, exc, exc_info=True
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error."},
        headers={"X-Request-ID": request_id},
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Service health and startup check results",
)
async def health() -> HealthResponse:
    """Return service health, startup check results, and queue metrics."""
    active = await store.count_by_status(JobStatus.RUNNING)
    queued = await store.count_by_status(JobStatus.QUEUED)
    has_critical_failure = any(
        not c.passed and c.name in ("matchering", "soundfile")
        for c in _startup_checks
    )
    return HealthResponse(
        status="degraded" if has_critical_failure else "ok",
        version=APP_VERSION,
        checks=_startup_checks,
        active_jobs=active,
        queued_jobs=queued,
    )


@app.post(
    "/jobs",
    response_model=JobCreateResponse,
    status_code=202,
    tags=["jobs"],
    summary="Upload target + reference audio and queue a mastering job",
)
async def create_job(
    target_file: UploadFile = File(
        ..., description="The track to master (TARGET)"
    ),
    reference_file: UploadFile = File(
        ..., description="The reference track to match (REFERENCE)"
    ),
    bit_depth: int = Form(default=16),
    normalize: bool = Form(default=True),
    use_limiter: bool = Form(default=True),
    mode: str = Form(default="direct"),
    sample_rate: int = Form(default=44100),
    max_length: float = Form(default=900.0),
) -> JobCreateResponse:
    """
    Upload target and reference audio files and queue a mastering job.

    Returns the job ID and initial status.  Poll ``GET /jobs/{job_id}``
    to check progress, then ``GET /jobs/{job_id}/download`` to retrieve
    the mastered file.
    """
    # --- Validate options via Pydantic ---
    try:
        opts = MasteringOptions(
            bit_depth=BitDepth(bit_depth),
            normalize=normalize,
            use_limiter=use_limiter,
            mode=ProcessingMode(mode),
            sample_rate=sample_rate,
            max_length=max_length,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # --- Save uploaded files (streaming, chunked) ---
    job_id = uuid.uuid4().hex
    job_upload_dir = UPLOAD_DIR / job_id
    job_upload_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []

    try:
        target_path, target_name = await _save_upload_streaming(
            target_file, job_upload_dir, "target"
        )
        saved_files.append(Path(target_path))
        reference_path, reference_name = await _save_upload_streaming(
            reference_file, job_upload_dir, "reference"
        )
        saved_files.append(Path(reference_path))
    except HTTPException:
        # Clean up any partially saved files on failure
        for f in saved_files:
            f.unlink(missing_ok=True)
        raise
    except Exception:
        for f in saved_files:
            f.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail="Failed to save uploaded files."
        )

    # --- Create job record ---
    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        target_path=target_path,
        reference_path=reference_path,
        target_original_name=target_name,
        reference_original_name=reference_name,
        options=opts,
    )
    await store.create(job)

    # --- Enqueue ---
    assert queue is not None, "Queue not initialised"
    try:
        queue.put_nowait(job_id)
    except asyncio.QueueFull:
        await store.update(
            job_id,
            status=JobStatus.FAILED,
            error="Queue is full. Try again later.",
            finished_at=time.time(),
        )
        # Clean up uploaded files since the job will never run
        for f in saved_files:
            f.unlink(missing_ok=True)
        raise HTTPException(
            status_code=503,
            detail="Job queue is full. Please try again later.",
        )

    logger.info(
        "Created job %s (mode=%s, bit_depth=%d)", job_id, mode, bit_depth
    )
    return JobCreateResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        message="Job queued successfully. Poll GET /jobs/{job_id} for status.",
    )


@app.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["jobs"],
    summary="Get job status, progress, and logs",
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Return the current status, logs, and download URL for a job."""
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    download_url: str | None = None
    if job.status == JobStatus.SUCCEEDED and job.output_path:
        download_url = f"/jobs/{job_id}/download"

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error=job.error,
        logs=job.logs[-100:],  # last 100 lines
        download_url=download_url,
        options=job.options,
    )


@app.get(
    "/jobs/{job_id}/download",
    tags=["jobs"],
    summary="Download the mastered audio file",
)
async def download_job_result(job_id: str) -> FileResponse:
    """Download the mastered audio file for a completed job."""
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=409,
            detail=f"Job is not completed (status: {job.status.value}).",
        )

    output_path = Path(job.output_path) if job.output_path else None
    if not output_path or not output_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Output file not found on disk. It may have been cleaned up.",
        )

    # Safety: ensure the output path is inside OUTPUT_DIR
    try:
        output_path.resolve().relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid output path.")

    # Sanitize original filename to basename only (prevent path traversal)
    safe_orig_name = Path(job.target_original_name).name if job.target_original_name else "output.wav"
    download_name = f"mastered_{safe_orig_name}"
    return FileResponse(
        path=str(output_path),
        media_type="audio/wav",
        filename=download_name,
    )


@app.delete(
    "/jobs/{job_id}",
    tags=["jobs"],
    summary="Cancel a queued job",
)
async def cancel_job(job_id: str) -> JSONResponse:
    """
    Cancel a queued job.

    Only jobs in the ``queued`` state can be cancelled.  Running jobs
    cannot be interrupted (Matchering processing is blocking in direct
    mode, and subprocess management does not track active PIDs for
    termination).  Completed or already-cancelled jobs return 409/200
    respectively.
    """
    job = await store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if job.status == JobStatus.SUCCEEDED:
        return JSONResponse(
            status_code=409,
            content={"detail": "Job already completed.", "job_id": job_id},
        )

    if job.status == JobStatus.CANCELLED:
        return JSONResponse(
            status_code=200,
            content={"detail": "Job already cancelled.", "job_id": job_id},
        )

    if job.status == JobStatus.QUEUED:
        await store.update(
            job_id,
            status=JobStatus.CANCELLED,
            finished_at=time.time(),
        )
        return JSONResponse(
            status_code=200,
            content={"detail": "Queued job cancelled.", "job_id": job_id},
        )

    # Running jobs cannot be safely cancelled
    raise HTTPException(
        status_code=409,
        detail=(
            f"Job is currently running (status: {job.status.value}) and "
            "cannot be cancelled. Running jobs must complete or fail."
        ),
    )


@app.get(
    "/jobs",
    response_model=list[JobStatusResponse],
    tags=["jobs"],
    summary="List recent jobs",
)
async def list_jobs(limit: int = 50) -> list[JobStatusResponse]:
    """List recent jobs (most recent first)."""
    all_jobs = await store.all()
    recent = sorted(all_jobs, key=lambda j: j.created_at, reverse=True)[:limit]
    return [
        JobStatusResponse(
            job_id=j.job_id,
            status=j.status,
            progress=j.progress,
            created_at=j.created_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
            error=j.error,
            logs=j.logs[-20:],
            download_url=(
                f"/jobs/{j.job_id}/download"
                if j.status == JobStatus.SUCCEEDED and j.output_path
                else None
            ),
            options=j.options,
        )
        for j in recent
    ]


# ---------------------------------------------------------------------------
# Root / docs
# ---------------------------------------------------------------------------

@app.get("/", tags=["system"], summary="Service info")
async def root() -> dict:
    """Basic service info and link to docs."""
    return {
        "service": "PatchPilot — Audio Mastering API",
        "version": APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "create_job": "POST /jobs",
            "job_status": "GET /jobs/{job_id}",
            "download": "GET /jobs/{job_id}/download",
            "cancel": "DELETE /jobs/{job_id}",
            "list": "GET /jobs",
        },
    }


# ===========================================================================
# Main entry point
# ===========================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "patchpilot:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        reload=False,
        log_level="info",
    )
