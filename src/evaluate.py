"""Evaluate the deterministic rule engine against sample_threads.csv and sample_answers.csv."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tqdm import tqdm

from load import load_answers, load_playbook, load_threads
from rules import DecisionResult, evaluate_thread

SAMPLE_THREADS_PATH = Path("sample_threads.csv")
SAMPLE_ANSWERS_PATH = Path("sample_answers.csv")
SAMPLE_CHECKPOINT_PATH = Path("checkpoints/sample_decisions.jsonl")
STATE_PATH = Path("checkpoints/state.json")
WARNINGS_PATH = Path("checkpoints/warnings.jsonl")


def _compute_code_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _load_sample_checkpoint(path: Path, expected_code_hash: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    cached: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("code_hash") == expected_code_hash:
                cached[rec["thread_id"]] = rec
    return cached


def _update_state(accuracy: float, correct: int, total: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {"completed_stages": [], "current_stage": "stage1_core_rules"}

    if correct == total and "stage1_core_rules" not in state["completed_stages"]:
        state["completed_stages"].append("stage1_core_rules")
    state["current_stage"] = "stage2_threads_audit" if correct == total else "stage1_core_rules"
    state["sample_evaluation"] = {
        "correct": correct,
        "total": total,
        "accuracy": accuracy,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate rule engine on sample_threads.csv")
    parser.add_argument("--fresh", action="store_true", help="Ignore existing sample checkpoint")
    args = parser.parse_args()

    playbook = load_playbook()
    threads = load_threads(SAMPLE_THREADS_PATH)
    answers = load_answers(SAMPLE_ANSWERS_PATH)

    code_hash = _compute_code_hash([Path("src/load.py"), Path("src/rules.py")])
    SAMPLE_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if args.fresh and SAMPLE_CHECKPOINT_PATH.exists():
        SAMPLE_CHECKPOINT_PATH.unlink()

    cached = _load_sample_checkpoint(SAMPLE_CHECKPOINT_PATH, code_hash)
    if cached and len(cached) > 0:
        # Ensure checkpoint file only contains entries for current code_hash
        pass
    elif SAMPLE_CHECKPOINT_PATH.exists():
        # Rules changed -> reset stale sample checkpoint
        SAMPLE_CHECKPOINT_PATH.unlink()

    results: list[dict[str, Any]] = []
    resumed_count = 0

    with SAMPLE_CHECKPOINT_PATH.open("a", encoding="utf-8") as ckpt_file:
        for thread in tqdm(threads, desc="Evaluating samples", unit="thread"):
            if thread.thread_id in cached:
                rec = cached[thread.thread_id]
                resumed_count += 1
            else:
                decision: DecisionResult = evaluate_thread(thread, playbook, warnings_path=WARNINGS_PATH)
                rec = decision.to_dict()
                rec["code_hash"] = code_hash
                ckpt_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ckpt_file.flush()
            results.append(rec)

    correct = 0
    mismatches: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    for rec in tqdm(results, desc="Scoring samples", unit="thread", leave=False):
        tid = rec["thread_id"]
        pred = rec["label"]
        gold = answers[tid]
        if pred == gold:
            correct += 1
        else:
            mismatches.append((tid, gold, pred, rec["trace"]))

    total = len(results)
    accuracy = (correct / total * 100.0) if total else 0.0
    print(f"\nSample Accuracy: {correct}/{total} ({accuracy:.2f}%) | Resumed from checkpoint: {resumed_count}")

    if mismatches:
        print("\n=== MISMATCH TABLE ===")
        print(f"{'Thread':<8} | {'Expected':<30} | {'Predicted':<30}")
        print("-" * 74)
        for tid, gold, pred, trace in mismatches:
            print(f"{tid:<8} | {gold:<30} | {pred:<30}")
            print(f"  Trace: {json.dumps(trace, ensure_ascii=False)}")
    else:
        print("0 mismatches — all 40 worked examples match exactly.")

    _update_state(accuracy, correct, total)


if __name__ == "__main__":
    main()
