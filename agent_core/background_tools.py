from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BackgroundToolJob:
    job_id: str
    tool_name: str
    args: dict[str, Any]
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class BackgroundToolRunner:
    def __init__(self, on_complete: Callable[[BackgroundToolJob], None] | None = None) -> None:
        self._on_complete = on_complete
        self._lock = threading.Lock()
        self._jobs: dict[str, BackgroundToolJob] = {}

    def submit(self, tool_name: str, args: dict[str, Any], run_sync: Callable[[], dict[str, Any]]) -> str:
        job_id = str(uuid.uuid4())
        job = BackgroundToolJob(job_id=job_id, tool_name=tool_name, args=dict(args))
        with self._lock:
            self._jobs[job_id] = job

        t = threading.Thread(target=self._run_job, args=(job_id, run_sync), daemon=True, name=f"bg_tool_{tool_name}")
        t.start()
        return job_id

    def _run_job(self, job_id: str, run_sync: Callable[[], dict[str, Any]]) -> None:
        with self._lock:
            job = self._jobs[job_id]
        try:
            result = run_sync()
            job.status = "done"
            job.result = result
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.result = {"ok": False, "error": job.error}
        finally:
            job.finished_at = time.time()
            if self._on_complete is not None:
                try:
                    self._on_complete(job)
                except Exception:
                    pass

    def get(self, job_id: str) -> BackgroundToolJob | None:
        with self._lock:
            return self._jobs.get(job_id)
