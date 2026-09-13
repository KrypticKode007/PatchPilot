"""
Bounded asyncio.Queue + the dedicated async worker loop that
dequeues job IDs and dispatches them to direct or subprocess
processing.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import settings
from .job_store import job_store
from .models import JobOptions, JobStatus, ProcessingMode
from .processing import SubprocessTimeoutError, process_direct, process_subprocess

logger = logging.getLogger("patchpilot.worker")

job_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_maxsize)
_worker_task: asyncio.Task | None = None


async def enqueue_job(job_id: str) -> None:
    job_queue.put_nowait(job_id)


async def _process_one(job_id: str) -> None:
    record = await job_store.get(job_id)
    if record is None:
        return
    if record.status == JobStatus.cancelled:
        return

    await job_store.update(job_id, status=JobStatus.running, started_at=time.time())
    await job_store.append_log(job_id, "Job started.")

    options: JobOptions = record.options

    async def on_log(line: str) -> None:
        await job_store.append_log(job_id, line)

    try:
        if options.mode == ProcessingMode.direct:
            await process_direct(
                job_id=job_id,
                target_path=record.target_path,
                reference_path=record.reference_path,
                output_path=record.output_path,
                options=options,
                temp_dir=str(settings.temp_dir),
                max_workers=settings.max_workers,
                on_log=on_log,
            )
        else:
            await process_subprocess(
                job_id=job_id,
                target_path=record.target_path,
                reference_path=record.reference_path,
                output_path=record.output_path,
                options=options,
                timeout_seconds=settings.timeout_seconds,
                on_log=on_log,
            )

        await job_store.update(
            job_id,
            status=JobStatus.succeeded,
            progress=1.0,
            finished_at=time.time(),
        )
        await job_store.append_log(job_id, "Processing completed successfully.")

    except SubprocessTimeoutError as exc:
        logger.warning("Job %s timed out: %s", job_id, exc)
        await job_store.update(
            job_id, status=JobStatus.failed, error=str(exc), finished_at=time.time()
        )
        await job_store.append_log(job_id, "Job timed out and was killed.")

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        await job_store.update(
            job_id,
            status=JobStatus.failed,
            error="An internal error occurred while processing this job.",
            finished_at=time.time(),
        )
        await job_store.append_log(job_id, "Job failed. See server logs for details.")


async def worker_loop() -> None:
    logger.info("Worker loop started.")
    while True:
        job_id = await job_queue.get()
        try:
            await _process_one(job_id)
        finally:
            job_queue.task_done()


def start_worker() -> asyncio.Task:
    global _worker_task
    _worker_task = asyncio.create_task(worker_loop())
    return _worker_task


async def stop_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None