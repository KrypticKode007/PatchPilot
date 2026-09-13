"""
Pydantic v2 request/response models for the PatchPilot mastering API.
"""
from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ProcessingMode(StrEnum):
    direct = "direct"
    subprocess = "subprocess"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class JobOptions(BaseModel):
    bit_depth: Literal[16, 24, 32] = 16
    normalize: bool = True
    use_limiter: bool = True
    mode: ProcessingMode = ProcessingMode.direct
    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    max_length: float = Field(default=900.0, gt=0, le=7200)


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class JobRecord(BaseModel):
    """Internal + external representation of a mastering job."""

    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: JobStatus = JobStatus.queued
    progress: float = 0.0

    target_path: str
    reference_path: str
    output_path: str | None = None

    options: JobOptions

    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    error: str | None = None
    logs: list[str] = Field(default_factory=list)

    def append_log(self, message: str, cap: int = 100) -> None:
        self.logs.append(message)
        if len(self.logs) > cap:
            self.logs = self.logs[-cap:]

    @property
    def download_url(self) -> str | None:
        if self.status == JobStatus.succeeded:
            return f"/jobs/{self.job_id}/download"
        return None


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
    options: JobOptions

    @classmethod
    def from_record(cls, record: JobRecord) -> "JobStatusResponse":
        return cls(
            job_id=record.job_id,
            status=record.status,
            progress=record.progress,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            error=record.error,
            logs=record.logs[-100:],
            download_url=record.download_url,
            options=record.options,
        )


class CancelResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str


class HealthChecks(BaseModel):
    libsndfile: bool
    ffmpeg: bool
    matchering_import: bool
    matchering_cli: bool
    directories: bool


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    checks: HealthChecks
    active_jobs: int
    queued_jobs: int