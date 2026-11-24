"""Validate submission.csv against sample_submission.csv and playbook output format."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

SUBMISSION_PATH = Path("submission.csv")
SAMPLE_SUBMISSION_PATH = Path("sample_submission.csv")
WARNINGS_PATH = Path("checkpoints/warnings.jsonl")
DECISIONS_PATH = Path("checkpoints/decisions.jsonl")
STATE_PATH = Path("checkpoints/state.json")

VALID_ACTION_REASONS = {
    ("ESCALATE", "MISSING_INFO"),
    ("REJECT", "BLACKLISTED_SUPPLIER"),
    ("ESCALATE", "PROMPT_INJECTION"),
    ("ESCALATE", "BANNED_TERM"),
    ("REJECT", "ABOVE_WALKAWAY"),
    ("COUNTER", "COUNTER_OFFER"),
    ("ESCALATE", "OVER_APPROVAL_LIMIT"),
    ("ACCEPT", "ACCEPTABLE_PRICE"),
}
VALID_ACTIONS = {a for a, _ in VALID_ACTION_REASONS}
VALID_REASONS = {r for _, r in VALID_ACTION_REASONS}


def main() -> None:
    if not SUBMISSION_PATH.exists():
        raise FileNotFoundError(f"Missing {SUBMISSION_PATH}")

    with SUBMISSION_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        sub_rows = list(reader)

    with SAMPLE_SUBMISSION_PATH.open("r", encoding="utf-8", newline="") as f:
        sample_reader = csv.reader(f)
        sample_header = next(sample_reader, None)
        sample_rows = list(sample_reader)

    assert header == ["thread_id", "label"], f"Invalid header: {header!r}"
    assert sample_header == ["thread_id", "label"], f"Unexpected sample header: {sample_header!r}"
    assert len(sub_rows) == 2000, f"Expected 2000 rows, got {len(sub_rows)}"
    assert len(sub_rows) == len(sample_rows), "Row count mismatch vs sample_submission.csv"

    action_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()

    for idx, (sub_row, sample_row) in enumerate(
        tqdm(zip(sub_rows, sample_rows), total=len(sub_rows), desc="Validating submission.csv", unit="row")
    ):
        assert len(sub_row) == 2, f"Row {idx} column count != 2: {sub_row!r}"
        tid, label = sub_row[0], sub_row[1]
        expected_tid = sample_row[0]
        assert tid == expected_tid, f"Row {idx} thread_id mismatch: {tid!r} != {expected_tid!r}"

        parts = label.split(":")
        assert len(parts) == 3, f"Row {tid} label must have exactly 2 colons: {label!r}"
        action, reason, counter_price = parts

        assert action in VALID_ACTIONS, f"Row {tid} invalid ACTION: {action!r}"
        assert reason in VALID_REASONS, f"Row {tid} invalid REASON: {reason!r}"
        assert (action, reason) in VALID_ACTION_REASONS, f"Row {tid} invalid ACTION/REASON pair: {(action, reason)!r}"

        if action == "COUNTER":
            assert re.fullmatch(r"\d+", counter_price), f"Row {tid} COUNTER price must be digits only: {counter_price!r}"
            assert int(counter_price) > 0, f"Row {tid} COUNTER price must be > 0: {counter_price!r}"
        else:
            assert counter_price == "", f"Row {tid} non-COUNTER must have empty COUNTER_PRICE after trailing colon: {label!r}"

        action_counts[action] += 1
        reason_counts[reason] += 1
        pair_counts[f"{action}:{reason}"] += 1

    warning_counts: Counter[str] = Counter()
    if WARNINGS_PATH.exists():
        for line in WARNINGS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                warning_counts[json.loads(line).get("reason", "unknown")] += 1

    llm_threads = 0
    if DECISIONS_PATH.exists():
        for line in DECISIONS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("used_llm"):
                llm_threads += 1

    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if "stage3_validate" not in state.get("completed_stages", []):
            state["completed_stages"].append("stage3_validate")
        state["current_stage"] = "completed"
        state["validation"] = {
            "passed": True,
            "rows": len(sub_rows),
            "action_counts": dict(action_counts),
            "reason_counts": dict(reason_counts),
            "total_warnings": sum(warning_counts.values()),
            "llm_threads": llm_threads,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    print("\nVALIDATION PASSED: 2,000 rows, exact ID order, valid ACTION/REASON, integer COUNTER_PRICE, trailing colons intact.")
    print(f"ACTION counts ({sum(action_counts.values())} total): {dict(action_counts.most_common())}")
    print(f"REASON counts ({sum(reason_counts.values())} total): {dict(reason_counts.most_common())}")
    print(f"ACTION:REASON pairs: {dict(pair_counts.most_common())}")
    print(f"Warnings logged ({sum(warning_counts.values())} total): {dict(warning_counts.most_common())}")
    print(f"Threads using LLM: {llm_threads}")
    print("\nFirst 10 rows of submission.csv:")
    print("thread_id,label")
    for tid, label in sub_rows[:10]:
        print(f"{tid},{label}")


if __name__ == "__main__":
    main()
