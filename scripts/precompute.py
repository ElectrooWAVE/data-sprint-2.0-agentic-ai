"""Precompute static batch payloads & submission CSVs into ui/data/ for instant serverless loading."""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.engine_wrapper import _read_jsonl, compute_analytics, get_engine  # noqa: E402


def _build_dataset(eng, threads, source_name: str, out_json: Path, out_csv: Path) -> None:
    rows = []
    with tempfile.TemporaryDirectory(prefix="pd_precompute_") as td:
        wpath = Path(td) / "warnings.jsonl"
        for t in tqdm(threads, desc=f"Precomputing {source_name}", unit="thread"):
            res = eng.decide(t, wpath)
            rows.append(eng.summarize(t, res))
        warns = _read_jsonl(wpath)

    by_tid: dict[str, list[str]] = {}
    for w in warns:
        by_tid.setdefault(w["thread_id"], []).append(w["reason"])
    for r in rows:
        r["warnings"] = by_tid.get(r["thread_id"], [])

    analytics = compute_analytics(rows)
    payload = {
        "source": source_name,
        "total": len(rows),
        "rows": rows,
        "analytics": analytics,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["thread_id", "label"])
    for r in rows:
        writer.writerow([r["thread_id"], r["label"]])
    out_csv.write_bytes(buf.getvalue().encode("utf-8"))
    print(f"Wrote {out_json.relative_to(ROOT)} ({out_json.stat().st_size:,} bytes) and {out_csv.relative_to(ROOT)}")


def main() -> None:
    eng = get_engine()
    data_dir = ROOT / "ui" / "data"
    _build_dataset(eng, eng.real_threads, "threads.csv", data_dir / "real.json", data_dir / "real_submission.csv")
    _build_dataset(eng, eng.sample_threads, "sample_threads.csv", data_dir / "sample.json", data_dir / "sample_submission.csv")


if __name__ == "__main__":
    main()
