"""
Security helpers: path traversal protection, filename sanitization,
and upload validation. Kept isolated and easy to unit-test.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .config import settings


def sanitize_filename(original_name: str) -> str:
    """Return just the basename - strips any directory components."""
    return Path(original_name).name


def validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_extensions:
        allowed = ", ".join(sorted(settings.allowed_extensions))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '{ext}'. Allowed: {allowed}",
        )
    return ext


def build_safe_upload_path(base_dir: Path, original_filename: str) -> Path:
    """
    Build a UUID-named path inside base_dir. Guards against path
    traversal by resolving and checking containment.
    """
    ext = validate_extension(original_filename)
    candidate = base_dir / f"{uuid.uuid4().hex}{ext}"
    resolved_base = base_dir.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload path.")
    return candidate


async def stream_upload_to_disk(upload: UploadFile, destination: Path) -> int:
    """
    Streams an UploadFile to disk in 1 MB chunks, enforcing the
    configured max upload size. Returns total bytes written.
    """
    chunk_size = 1024 * 1024
    total = 0
    max_bytes = settings.max_upload_bytes

    with destination.open("wb") as out:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                out.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds max size of {settings.max_upload_mb} MB.",
                )
            out.write(chunk)

    return total


def sanitize_download_filename(job_id: str) -> str:
    """Download filenames are basename-only, tied to the job id."""
    return f"{job_id}_mastered.wav"


def sanitize_error_for_client(exc: Exception) -> str:
    """
    Client-facing error messages must never leak internal paths or
    tracebacks. Full detail still goes to server logs by the caller.
    """
    return "An internal error occurred while processing this job."