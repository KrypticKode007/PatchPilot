# core/job_store.py
from __future__ import annotations
from typing import Any
from .models import JobStatus

class JobRecord:
    def __init__(self, target_path: str, reference_path: str, output_path: str, options: Any):
        self.status = JobStatus.pending
        self.target_path = target_path
        self.reference_path = reference_path
        self.output_path = output_path
        self.options = options
        self.logs: list[str] = []

class JobStore:
    def __init__(self):
        self._db: dict[str, JobRecord] = {}

    async def create(self, **kwargs) -> str:
        import uuid
        job_id = str(uuid.uuid4())
        self._db[job_id] = JobRecord(kwargs['target_path'], kwargs['reference_path'], kwargs['output_path'], kwargs['options'])
        return job_id

    async def get(self, job_id: str) -> JobRecord | None:
        return self._db.get(job_id)

    async def update(self, job_id: str, **kwargs) -> None:
        record = self._db.get(job_id)
        if record:
            for k, v in kwargs.items():
                setattr(record, k, v)

    async def append_log(self, job_id: str, line: str) -> None:
        record = self._db.get(job_id)
        if record:
            record.logs.append(line)

job_store = JobStore()