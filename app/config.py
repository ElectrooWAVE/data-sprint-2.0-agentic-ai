"""Environment-driven settings. Every path is relative to the project root unless overridden."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Project root = parent of the app/ package. Computed at runtime, never hard-coded.
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _parse_origins(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str
    port: int
    cors_origins: tuple[str, ...]
    playbook_dir: Path
    threads_csv: Path
    sample_threads_csv: Path
    sample_answers_csv: Path
    submission_final_csv: Path
    ui_dir: Path
    max_batch_bytes: int
    max_batch_rows: int
    job_ttl_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        root = Path(env.get("PROCUREMENT_ROOT") or _DEFAULT_ROOT)

        def path(key: str, default: str) -> Path:
            p = Path(env.get(key) or default)
            return p if p.is_absolute() else (root / p)

        return cls(
            root=root,
            host=env.get("HOST", "127.0.0.1"),
            port=int(env.get("PORT", "8000")),
            cors_origins=_parse_origins(env.get("CORS_ORIGINS")),  # default: same-origin only
            playbook_dir=path("PROCUREMENT_PLAYBOOK_DIR", "playbook/playbook"),
            threads_csv=path("PROCUREMENT_THREADS_CSV", "threads.csv"),
            sample_threads_csv=path("PROCUREMENT_SAMPLE_THREADS_CSV", "sample_threads.csv"),
            sample_answers_csv=path("PROCUREMENT_SAMPLE_ANSWERS_CSV", "sample_answers.csv"),
            submission_final_csv=path("PROCUREMENT_SUBMISSION_FINAL_CSV", "submission_final.csv"),
            ui_dir=path("PROCUREMENT_UI_DIR", "ui"),
            max_batch_bytes=int(env.get("MAX_BATCH_BYTES", str(5 * 1024 * 1024))),
            max_batch_rows=int(env.get("MAX_BATCH_ROWS", "5000")),
            job_ttl_seconds=int(env.get("JOB_TTL_SECONDS", "3600")),
        )

    def rel(self, p: Path) -> str:
        """Display a path relative to the project root (never leak absolute host paths)."""
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return p.name


settings = Settings.from_env()
