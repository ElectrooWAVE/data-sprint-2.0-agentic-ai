"""Verify our_last_counter == round_half_up(L(round-1)) invariant and full offer parse coverage."""

from __future__ import annotations

import sys
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load import get_effective_params, load_playbook, load_threads  # noqa: E402
from rules import (  # noqa: E402
    compute_concession_level,
    extract_and_normalize_offer,
    split_thread_messages,
)


def check_dataset(name: str, csv_path: Path, playbook) -> tuple[int, list[dict]]:
    threads = load_threads(csv_path)
    checked = 0
    mismatches: list[dict] = []
    regime_counts: Counter[str] = Counter()

    for th in tqdm(threads, desc=f"Self-check {name}", unit="thread"):
        if th.round_num <= 1:
            continue
        checked += 1
        params = get_effective_params(playbook, th.category, th.supplier, th.thread_date)
        prev_r = min(th.round_num - 1, params.rounds)
        expected = int(compute_concession_level(params, prev_r).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        actual = int(th.our_last_counter) if th.our_last_counter is not None else None

        if params.applied_amendments:
            regime_counts["with_amendments"] += 1
        if params.applied_contracts:
            regime_counts["with_contracts"] += 1
        if params.supplier.status == "PROBATION":
            regime_counts["probation"] += 1

        if actual != expected:
            mismatches.append(
                {
                    "thread_id": th.thread_id,
                    "category": th.category,
                    "supplier": th.supplier,
                    "date": th.thread_date.isoformat(),
                    "round": th.round_num,
                    "R": params.rounds,
                    "expected_olc": expected,
                    "actual_olc": actual,
                    "amendments": params.applied_amendments,
                    "contracts": params.applied_contracts,
                    "status": params.supplier.status,
                }
            )

    print(
        f"[{name}] Checked round > 1 threads: {checked} | Mismatches: {len(mismatches)} "
        f"| Regimes covered: {dict(regime_counts)}"
    )
    return checked, mismatches


def check_offer_coverage(csv_path: Path, playbook) -> None:
    """Verify every thread in threads.csv either yields a positive price or a genuine playbook MISSING_INFO."""
    threads = load_threads(csv_path)
    missing_reasons: Counter[str] = Counter()
    parsed_count = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        warn_path = Path(tmpdir) / "selfcheck_warn.jsonl"
        for th in tqdm(threads, desc="Dry-run offer parsing", unit="thread"):
            params = get_effective_params(playbook, th.category, th.supplier, th.thread_date)
            _, latest, earlier = split_thread_messages(th.supplier_message)
            offer = extract_and_normalize_offer(
                latest, params, th.quantity, earlier, th.thread_id, warn_path
            )
            if offer.missing_info or offer.unit_price_inr is None:
                missing_reasons[offer.missing_reason or "unknown"] += 1
            else:
                parsed_count += 1
                if offer.unit_price_inr <= 0:
                    raise ValueError(f"Non-positive parsed price on {th.thread_id}: {offer.unit_price_inr}")

    print(
        f"[{csv_path.name}] Parsed positive price: {parsed_count}/{len(threads)} "
        f"| Genuine missing_info breakdown: {dict(missing_reasons)}"
    )


def main() -> None:
    playbook = load_playbook()
    all_mismatches: list[dict] = []

    for label, path in [("sample_threads.csv", Path("sample_threads.csv")), ("threads.csv", Path("threads.csv"))]:
        _, mismatches = check_dataset(label, path, playbook)
        all_mismatches.extend(mismatches)

    if all_mismatches:
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for m in all_mismatches:
            grouped[(m["category"], m["supplier"])].append(m)
        print("\n=== MISMATCHES GROUPED BY (CATEGORY, SUPPLIER) ===")
        for (cat, sup), rows in sorted(grouped.items()):
            print(f"  ({cat}, {sup}): {len(rows)} mismatches; first={rows[0]}")
        raise SystemExit(1)

    check_offer_coverage(Path("threads.csv"), playbook)
    print("\nAll our_last_counter invariants and offer-parsing coverage checks passed with 0 unexplained mismatches.")


if __name__ == "__main__":
    main()
