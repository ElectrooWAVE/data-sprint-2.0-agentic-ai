"""Run the procurement decision engine on all 2,000 threads and write submission.csv."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm_extract import get_llm_call_count  # noqa: E402
from load import load_playbook, load_threads  # noqa: E402
from rules import DecisionResult, evaluate_thread  # noqa: E402

THREADS_PATH = Path("threads.csv")
SAMPLE_SUBMISSION_PATH = Path("sample_submission.csv")
SUBMISSION_PATH = Path("submission.csv")
SUBMISSION_FINAL_PATH = Path("submission_final.csv")
DECISIONS_PATH = Path("checkpoints/decisions.jsonl")
WARNINGS_PATH = Path("checkpoints/warnings.jsonl")
STATE_PATH = Path("checkpoints/state.json")


def _load_existing_decisions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    done: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            done[rec["thread_id"]] = rec
    return done


def _load_sample_submission_ids(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [row["thread_id"].strip() for row in tqdm(rows, desc="Reading sample_submission.csv", unit="row", leave=False)]


def _count_warnings(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            counts[rec.get("reason", "unknown")] += 1
    return counts


def _update_state(total: int, resumed: int, action_counts: Counter[str], reason_counts: Counter[str], llm_calls: int, total_warnings: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {"completed_stages": []}

    if "stage3_predict" not in state["completed_stages"]:
        state["completed_stages"].append("stage3_predict")
    state["current_stage"] = "stage3_validate"
    state["prediction"] = {
        "total_threads": total,
        "resumed_from_checkpoint": resumed,
        "llm_calls": llm_calls,
        "total_warnings": total_warnings,
        "action_counts": dict(action_counts),
        "reason_counts": dict(reason_counts),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run procurement rules on threads.csv and produce submission.csv")
    parser.add_argument("--fresh", action="store_true", help="Clear existing decisions.jsonl and warnings.jsonl")
    args = parser.parse_args()

    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        if DECISIONS_PATH.exists():
            DECISIONS_PATH.unlink()
        if WARNINGS_PATH.exists():
            WARNINGS_PATH.unlink()

    playbook = load_playbook()
    threads = load_threads(THREADS_PATH)
    expected_ids = _load_sample_submission_ids(SAMPLE_SUBMISSION_PATH)

    done = _load_existing_decisions(DECISIONS_PATH)
    resumed_count = 0

    with DECISIONS_PATH.open("a", encoding="utf-8") as out_f:
        for thread in tqdm(threads, desc="Predicting threads.csv", unit="thread"):
            if thread.thread_id in done:
                resumed_count += 1
                continue
            decision: DecisionResult = evaluate_thread(thread, playbook, warnings_path=WARNINGS_PATH)
            rec = decision.to_dict()
            rec["used_llm"] = False
            out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out_f.flush()
            done[thread.thread_id] = rec

    with SUBMISSION_PATH.open("w", encoding="utf-8", newline="") as sub_f:
        writer = csv.writer(sub_f)
        writer.writerow(["thread_id", "label"])
        for tid in tqdm(expected_ids, desc="Writing submission.csv", unit="row"):
            writer.writerow([tid, done[tid]["label"]])

    SUBMISSION_FINAL_PATH.write_bytes(SUBMISSION_PATH.read_bytes())

    action_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    llm_used_threads = 0

    for tid in expected_ids:
        rec = done[tid]
        action_counts[rec["action"]] += 1
        reason_counts[rec["reason"]] += 1
        pair_counts[f"{rec['action']}:{rec['reason']}"] += 1
        if rec.get("used_llm"):
            llm_used_threads += 1

    warning_counts = _count_warnings(WARNINGS_PATH)
    total_warnings = sum(warning_counts.values())
    llm_calls = get_llm_call_count()

    _update_state(len(expected_ids), resumed_count, action_counts, reason_counts, llm_calls, total_warnings)

    print(
        f"\nDone: {len(expected_ids)} threads | Resumed from checkpoint: {resumed_count} "
        f"| LLM threads/calls: {llm_used_threads}/{llm_calls}"
    )
    print(f"ACTION counts: {dict(action_counts.most_common())}")
    print(f"REASON counts: {dict(reason_counts.most_common())}")
    print(f"ACTION:REASON counts: {dict(pair_counts.most_common())}")
    print(f"Warnings logged ({total_warnings} total): {dict(warning_counts.most_common())}")
    print("\nFirst 10 rows of submission.csv:")
    for tid in expected_ids[:10]:
        print(f"  {tid},{done[tid]['label']}")


if __name__ == "__main__":
    main()
