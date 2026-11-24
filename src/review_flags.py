"""Generate REVIEW.md with stratified examples (max 15 per group) for manual inspection."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load import ThreadRow, load_playbook, load_threads  # noqa: E402
from rules import split_thread_messages  # noqa: E402

DECISIONS_PATH = Path("checkpoints/decisions.jsonl")
WARNINGS_PATH = Path("checkpoints/warnings.jsonl")
REVIEW_PATH = Path("REVIEW.md")

SEC_WORDS_RE = re.compile(
    r"\b(?:ai|llm|language\s+model|model|assistant|bot|automated|system|reviewer|override|pre-?approved|already\s+approved)\b",
    re.I,
)
BANNED_WORDS_RE = re.compile(
    r"\b(?:renew\w*|auto-?renew\w*|upfront|up-front|advance|exclusiv\w*|liabilit\w*|liable)\b",
    re.I,
)


def select_spread(
    items: list[dict[str, Any]],
    max_n: int,
    bucket_fn: Callable[[dict[str, Any]], tuple[str, str]],
) -> list[dict[str, Any]]:
    """Round-robin across (subtype, category) buckets to spread selections across categories and subtypes."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        buckets[bucket_fn(item)].append(item)

    # Sort bucket keys so subtypes and categories cycle evenly
    keys = sorted(buckets.keys(), key=lambda k: (k[0], k[1]))
    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_cats_first_pass: set[str] = set()

    # Pass 1: prioritize distinct categories & subtypes
    for k in keys:
        if len(selected) >= max_n:
            break
        sub, cat = k
        if cat not in seen_cats_first_pass and buckets[k]:
            cand = buckets[k].pop(0)
            tid = cand["thread"].thread_id
            if tid not in seen_ids:
                selected.append(cand)
                seen_ids.add(tid)
                seen_cats_first_pass.add(cat)

    # Pass 2: round-robin remaining items across all buckets
    idx = 0
    while len(selected) < max_n and any(buckets[k] for k in keys):
        k = keys[idx % len(keys)]
        if buckets[k]:
            cand = buckets[k].pop(0)
            tid = cand["thread"].thread_id
            if tid not in seen_ids:
                selected.append(cand)
                seen_ids.add(tid)
        idx += 1
        if idx > len(keys) * (max_n + 50):
            break

    return selected


def _format_entry(item: dict[str, Any], extra_note: str | None = None) -> list[str]:
    th: ThreadRow = item["thread"]
    dec: dict[str, Any] = item["decision"]
    warns: list[dict[str, Any]] = item.get("warnings", [])

    out: list[str] = []
    out.append(
        f"### `{th.thread_id}` — `{dec['label']}` "
        f"| `{th.thread_date.isoformat()}` | `{th.supplier}` | `{th.category}` | qty=`{th.quantity}` | round=`{th.round_num}` | our_last_counter=`{th.our_last_counter}`"
    )
    if extra_note:
        out.append(f"- **Review Note:** {extra_note}")
    if warns:
        out.append(f"- **Warnings:** `{json.dumps(warns, ensure_ascii=False)}`")
    out.append("- **Message Text:**")
    out.append("```text")
    out.append(th.supplier_message)
    out.append("```")
    out.append("- **Decision Trace:**")
    out.append("```json")
    out.append(json.dumps(dec["trace"], indent=2, ensure_ascii=False))
    out.append("```")
    out.append("")
    return out


def main() -> None:
    playbook = load_playbook()
    threads = load_threads("threads.csv")
    thread_map = {t.thread_id: t for t in threads}

    decisions: dict[str, dict[str, Any]] = {}
    with DECISIONS_PATH.open("r", encoding="utf-8") as f:
        for line in tqdm(f.readlines(), desc="Reading decisions.jsonl", unit="line", leave=False):
            if line.strip():
                rec = json.loads(line)
                decisions[rec["thread_id"]] = rec

    warnings_by_tid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if WARNINGS_PATH.exists():
        with WARNINGS_PATH.open("r", encoding="utf-8") as f:
            for line in tqdm(f.readlines(), desc="Reading warnings.jsonl", unit="line", leave=False):
                if line.strip():
                    w = json.loads(line)
                    warnings_by_tid[w["thread_id"]].append(w)

    # Attach readable date_str on wrapper dict without modifying load.py
    g1_warnings: list[dict[str, Any]] = []
    g2_inj: list[dict[str, Any]] = []
    g2_not_inj: list[dict[str, Any]] = []
    g3_banned: list[dict[str, Any]] = []
    g3_not_banned: list[dict[str, Any]] = []
    g4_near_thresh: list[dict[str, Any]] = []
    g4_other_side_count = 0
    g5_near_amd: list[dict[str, Any]] = []
    g6_missing: list[dict[str, Any]] = []

    for th in tqdm(threads, desc="Scanning review groups", unit="thread"):
        tid = th.thread_id
        dec = decisions[tid]
        warns = warnings_by_tid.get(tid, [])
        base = {"thread": th, "decision": dec, "warnings": warns}

        # Group 1: Every thread with a logged warning
        if warns:
            w_reason = warns[0].get("reason", "unknown")
            g1_warnings.append({**base, "subtype": w_reason, "note": f"warning_reason={w_reason}"})

        # Group 2: Prompt injection decisions AND non-flagged AI/assistant/model/system words
        if dec["reason"] == "PROMPT_INJECTION":
            step3_entry = next((s for s in dec["trace"] if s.get("step") == 3), {})
            rule_name = (step3_entry.get("detail") or {}).get("rule", "unknown")
            g2_inj.append({**base, "subtype": rule_name, "note": f"matched_rule={rule_name}"})
        else:
            m_sec = SEC_WORDS_RE.search(th.supplier_message)
            if m_sec:
                kw = m_sec.group(0).lower()
                g2_not_inj.append({**base, "subtype": kw, "note": f"unflagged_keyword={kw!r}"})

        # Group 3: Banned-term decisions AND non-flagged renewal/advance/exclusive/liability words
        if dec["reason"] == "BANNED_TERM":
            step4_entry = next((s for s in dec["trace"] if s.get("step") == 4), {})
            term_name = (step4_entry.get("detail") or {}).get("term", "unknown")
            g3_banned.append({**base, "subtype": term_name, "note": f"banned_term={term_name}"})
        else:
            _, latest_msg, _ = split_thread_messages(th.supplier_message)
            m_ban = BANNED_WORDS_RE.search(latest_msg)
            if m_ban:
                kw = m_ban.group(0).lower()
                g3_not_banned.append({**base, "subtype": kw, "note": f"unflagged_keyword_in_latest={kw!r}"})

        # Group 4: Counters or accepts within 1% of a threshold (p close to L(r) or approval limit)
        step5 = next((s for s in dec["trace"] if s.get("step") == 5), None)
        step6 = next((s for s in dec["trace"] if s.get("step") == 6), None)
        if step5 and isinstance(step5.get("detail"), dict):
            d5 = step5["detail"]
            p = Decimal(d5["p"])
            L_r = Decimal(d5["L_r"])
            rel_L = abs(p - L_r) / L_r
            rel_lim: Decimal | None = None
            if step6 and isinstance(step6.get("detail"), dict):
                d6 = step6["detail"]
                spend = Decimal(d6["total_spend"])
                lim = Decimal(d6["approval_limit"])
                rel_lim = abs(spend - lim) / lim

            near_L = rel_L <= Decimal("0.01")
            near_lim = rel_lim is not None and rel_lim <= Decimal("0.01")
            if near_L or near_lim:
                if dec["action"] in {"COUNTER", "ACCEPT"}:
                    kind = "near_approval_limit" if near_lim else "near_L(r)"
                    pct_L = (rel_L * Decimal("100")).quantize(Decimal("0.0001"))
                    pct_lim_str = (
                        f", dist_to_limit={(rel_lim * Decimal('100')).quantize(Decimal('0.0001'))}%"
                        if rel_lim is not None
                        else ""
                    )
                    g4_near_thresh.append(
                        {
                            **base,
                            "subtype": kind,
                            "note": f"{kind}: p={p}, L(r)={L_r} (dist={pct_L}%){pct_lim_str}",
                        }
                    )
                else:
                    g4_other_side_count += 1

        # Group 5: Threads dated within 3 days of an amendment's effective date for their category
        for amd in playbook.amendments:
            if amd.category_slug == th.category:
                delta_days = (th.thread_date - amd.effective_from).days
                if abs(delta_days) <= 3:
                    status_str = "ACTIVE" if delta_days >= 0 else "NOT_YET_ACTIVE"
                    g5_near_amd.append(
                        {
                            **base,
                            "subtype": amd.amd_id,
                            "note": f"{amd.amd_id} effective {amd.effective_from.isoformat()} (delta={delta_days:+d} days, {status_str})",
                        }
                    )

        # Group 6: MISSING_INFO decisions
        if dec["reason"] == "MISSING_INFO":
            step1_entry = next((s for s in dec["trace"] if s.get("step") == 1), {})
            miss_detail = str(step1_entry.get("detail", "unknown"))
            g6_missing.append({**base, "subtype": miss_detail, "note": f"missing_detail={miss_detail}"})

    # Write REVIEW.md
    lines: list[str] = []
    lines.append("# Manual Review Flags (`REVIEW.md`)")
    lines.append("")
    lines.append("Up to **15** threads per group, spread across categories and sub-types. No rules or outputs were modified.")
    lines.append("")

    sections = [
        ("1. Threads with Logged Warnings", g1_warnings, lambda x: (x["subtype"], x["thread"].category)),
        ("2a. Prompt-Injection Decisions (`ESCALATE:PROMPT_INJECTION:`)", g2_inj, lambda x: (x["subtype"], x["thread"].category)),
        ("2b. Unflagged Threads Containing Security Keywords (`AI`/`assistant`/`model`/`system`/`bot`/`reviewer`/`override`)", g2_not_inj, lambda x: (x["subtype"], x["thread"].category)),
        ("3a. Banned-Term Decisions (`ESCALATE:BANNED_TERM:`)", g3_banned, lambda x: (x["subtype"], x["thread"].category)),
        ("3b. Unflagged Threads Containing Banned-Term Keywords (`renewal`/`advance`/`upfront`/`exclusiv*`/`liabilit*`)", g3_not_banned, lambda x: (x["subtype"], x["thread"].category)),
        ("4. Counters & Accepts Within 1% of a Threshold (`L(r)` or `approval_limit`)", g4_near_thresh, lambda x: (x["subtype"], x["thread"].category)),
        ("5. Threads Dated Within ±3 Days of Category Amendment Effective Date", g5_near_amd, lambda x: (x["subtype"], x["thread"].category)),
        ("6. `MISSING_INFO` Decisions (`ESCALATE:MISSING_INFO:`)", g6_missing, lambda x: (x["subtype"], x["thread"].category)),
    ]

    summary_rows: list[tuple[str, int, int, str]] = []
    for title, items, bfn in sections:
        sub_counts = Counter(it["subtype"] for it in items)
        selected = select_spread(items, max_n=15, bucket_fn=bfn)
        summary_rows.append((title, len(items), len(selected), ", ".join(f"{k}={v}" for k, v in sub_counts.most_common(6))))

        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- **Total matching threads:** {len(items)} | **Shown below:** {len(selected)}")
        if sub_counts:
            lines.append(f"- **Sub-type breakdown:** `{dict(sub_counts.most_common())}`")
        if title.startswith("4.") and g4_other_side_count:
            lines.append(f"- **Note:** Additionally, `{g4_other_side_count}` `REJECT`/`ESCALATE:OVER_APPROVAL_LIMIT` threads sit within 1% on the over-threshold side.")
        lines.append("")

        if not selected:
            lines.append("_None found._")
            lines.append("")
            continue

        for item in selected:
            lines.extend(_format_entry(item, extra_note=item.get("note")))

    # Prepend summary table at top
    summary_md = [
        "| Group | Total Matching | Shown | Top Sub-types |",
        "|---|---:|---:|---|",
    ]
    for title, tot, shown, top_sub in summary_rows:
        summary_md.append(f"| {title} | {tot} | {shown} | {top_sub or '—'} |")
    summary_md.append("")

    final_lines = lines[:4] + summary_md + lines[4:]
    REVIEW_PATH.write_text("\n".join(final_lines), encoding="utf-8")
    print(f"Wrote {REVIEW_PATH} ({len(final_lines)} lines).")
    for title, tot, shown, top_sub in summary_rows:
        print(f"  {title}: total={tot}, shown={shown} ({top_sub})")
    if g4_other_side_count:
        print(f"  [Group 4 note] Over-threshold REJECT/ESCALATE within 1%: {g4_other_side_count}")


if __name__ == "__main__":
    main()
