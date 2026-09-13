from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, HTTPException, BackgroundTasks, status
from pydantic import BaseModel, Field

from .config import settings
from .job_store import job_store
from .models import JobOptions, JobStatus
from .queue_worker import enqueue_job, start_worker, stop_worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lifecycle: Initialize background queue task when the server turns on
    start_worker()
    yield
    # Lifecycle: Clean up and cancel background workers during teardown
    await stop_worker()


app = FastAPI(
    title="PatchPilot Engine",
    description="High-performance asynchronous Audio Mastering API",
    version="2.0.0",
    lifespan=lifespan,
)


class MasterRequest(BaseModel):
    target_path: str = Field(..., description="Absolute path to target file to master")
    reference_path: str = Field(..., description="Absolute path to reference target audio curve")
    output_path: str = Field(..., description="Destination storage path for output file")
    options: JobOptions = Field(default_factory=JobOptions, description="DSP processing settings")


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str


@app.post("/api/v1/master", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_mastering_job(payload: MasterRequest) -> Any:
    """
    Submits an audio mastering job configuration payload directly into the system workflow.
    """
    # 1. Create unique job tracking document state
    try:
        job_id = await job_store.create(
            target_path=payload.target_path,
            reference_path=payload.reference_path,
            output_path=payload.output_path,
            options=payload.options
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create job manifest: {str(e)}"
        )

    # 2. Push job ID to the asyncio.Queue array logic
    try:
        await enqueue_job(job_id)
    except Exception as e:
        # Fallback if queue put_nowait fails (e.g. queue full bounds)
        await job_store.update(job_id, status=JobStatus.failed, error_message="Queue saturation failure.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Queue bounds saturated. Try again later: {str(e)}"
        )

    return JobResponse(
        job_id=job_id,
        status=JobStatus.pending,
        message="Mastering job pushed to asynchronous pipeline sequence successfully."
    )


@app.get("/api/v1/jobs/{job_id}", status_code=status.HTTP_200_OK)
async def get_job_status(job_id: str) -> Any:
    """
    Polls runtime metrics, lifecycle logs, and status states of a running processing execution.
    """
    record = await job_store.get(job_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job sequence identifier '{job_id}' not found inside datastore registry."
        )
    
    return {
        "job_id": job_id,
        "status": record.status,
        "started_at": getattr(record, "started_at", None),
        "completed_at": getattr(record, "completed_at", None),
        "error_message": getattr(record, "error_message", None),
        "logs": getattr(record, "logs", [])
    }