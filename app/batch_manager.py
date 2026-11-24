"""In-memory batch jobs: background execution, progress, TTL cleanup, and locking."""

from __future__ import annotations

import csv
import io
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engine_wrapper import Engine, _read_jsonl, compute_analytics


@dataclass
class BatchJob:
    job_id: str
    source_name: str
    threads: list[Any]
    created_at: float
    status: str = "queued"  # queued | running | completed | failed
    done: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    analytics: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        return len(self.threads)

    def progress(self) -> dict[str, Any]:
        end = self.finished_at or time.time()
        return {
            "job_id": self.job_id,
            "source": self.source_name,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "percent": round(100.0 * self.done / self.total, 1) if self.total else 0.0,
            "elapsed_ms": int((end - self.started_at) * 1000) if self.started_at else 0,
            "error": self.error,
        }


class BatchManager:
    def __init__(self, engine: Engine, ttl_seconds: int):
        self.engine = engine
        self.ttl_seconds = ttl_seconds
        self._jobs: dict[str, BatchJob] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()

    def cleanup(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            expired = [jid for jid, j in self._jobs.items()
                       if now - j.created_at > self.ttl_seconds and j.status in ("completed", "failed")]
            for jid in expired:
                del self._jobs[jid]
        return len(expired)

    def create(self, threads: list[Any], source_name: str) -> BatchJob:
        self.cleanup()
        job = BatchJob(job_id=uuid.uuid4().hex[:12], source_name=source_name, threads=threads, created_at=time.time())
        with self._lock:
            self._jobs[job.job_id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"batch-{job.job_id}").start()
        return job

    def get(self, job_id: str) -> BatchJob | None:
        self.cleanup()
        with self._lock:
            return self._jobs.get(job_id)

    def progress(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        with self._lock:
            return job.progress()

    def _run(self, job: BatchJob) -> None:
        with self._run_lock:
            with self._lock:
                job.status = "running"
                job.started_at = time.time()
            try:
                rows: list[dict[str, Any]] = []
                with tempfile.TemporaryDirectory(prefix=f"pd_batch_{job.job_id}_") as td:
                    wpath = Path(td) / "warnings.jsonl"
                    for t in job.threads:
                        res = self.engine.decide(t, wpath)
                        rows.append(self.engine.summarize(t, res))
                        with self._lock:
                            job.done += 1
                    warns = _read_jsonl(wpath)
                by_tid: dict[str, list[str]] = {}
                for w in warns:
                    by_tid.setdefault(w["thread_id"], []).append(w["reason"])
                for r in rows:
                    r["warnings"] = by_tid.get(r["thread_id"], [])
                analytics = compute_analytics(rows)
                with self._lock:
                    job.rows = rows
                    job.analytics = analytics
                    job.status = "completed"
                    job.finished_at = time.time()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_at = time.time()

    @staticmethod
    def submission_csv(job: BatchJob) -> bytes:
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(["thread_id", "label"])
        for r in job.rows:
            w.writerow([r["thread_id"], r["label"]])
        return buf.getvalue().encode("utf-8")
