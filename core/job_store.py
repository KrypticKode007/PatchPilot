"""
In-memory job store with an async lock.

NOTE (from README Production Notes): suitable for single-process
deployments only. For multi-worker/distributed production, replace
this with Redis or a database - that migration should only touch
this file, since routes/queue_worker consume it through the same
interface below.
"""
from __future__ import annotations

import asyncio

from .models import JobRecord, JobStatus


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: JobRecord) -> JobRecord:
        async with self._lock:
            self._jobs[record.job_id] = record
        return record

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **fields) -> JobRecord | None:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            updated = record.model_copy(update=fields)
            self._jobs[job_id] = updated
            return updated

    async def append_log(self, job_id: str, message: str) -> None:
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.append_log(message)

    async def list_recent(self, limit: int = 50) -> list[JobRecord]:
        async with self._lock:
            records = sorted(
                self._jobs.values(), key=lambda r: r.created_at, reverse=True
            )
            return records[:limit]

    async def count_by_status(self, *statuses: JobStatus) -> int:
        async with self._lock:
            return sum(1 for r in self._jobs.values() if r.status in statuses)

    async def delete(self, job_id: str) -> bool:
        async with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                return True
            return False


job_store = JobStore()