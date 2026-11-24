"""Thin, read-only wrapper around the frozen engine in src/.

Invariant: the decision (label, action, reason, counter_price) is ALWAYS taken verbatim from
rules.evaluate_thread(). Everything else returned here (6-step view, price breakdown, numbers,
highlights) is explanatory and recomputed by calling the same engine functions. Nothing here can
change a decision. All engine warnings go to temporary files; nothing is written to checkpoints/.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import tempfile
import threading
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import Settings, settings

_SRC_DIR = settings.root / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from load import (  # noqa: E402
    EffectiveParams,
    Playbook,
    ThreadRow,
    _parse_decimal,
    get_effective_params,
    load_answers,
    load_playbook,
    load_threads,
)
from rules import (  # noqa: E402
    CURR_AMT_PAT,
    CURR_AMT_RE,
    WORD_CURR_AMT_RE,
    DecisionResult,
    ParsedOffer,
    compute_concession_level,
    evaluate_thread,
    extract_and_normalize_offer,
    split_thread_messages,
)

REQUIRED_COLUMNS = (
    "thread_id",
    "date",
    "supplier",
    "category",
    "quantity",
    "round",
    "our_last_counter",
    "supplier_message",
)

STEP_NAMES = {
    1: ("Missing info", "missing_info"),
    2: ("Blacklist", "blacklisted_supplier"),
    3: ("Prompt injection", "prompt_injection"),
    4: ("Banned terms", "banned_terms"),
    5: ("Price check", "price_decision"),
    6: ("Approval limit", "approval_limit"),
}

REASON_PLAIN = {
    "MISSING_INFO": "The latest offer has no usable price (no number, unsupported currency, or a subscription price without a billing period).",
    "BLACKLISTED_SUPPLIER": "The supplier is blacklisted: Acme does not do business with them.",
    "PROMPT_INJECTION": "The thread contains text aimed at an AI, bot, system or reviewer, so a human must review it.",
    "BANNED_TERM": "The latest message contains a banned contract term, so Legal must review it.",
    "ABOVE_WALKAWAY": "The price is above the walk-away level on the final negotiation round.",
    "COUNTER_OFFER": "The price is above this round's acceptable level, so Acme counters at that level.",
    "OVER_APPROVAL_LIMIT": "The price is acceptable, but the total order value is above the approval limit.",
    "ACCEPTABLE_PRICE": "The price is acceptable and the total order value is within the approval limit.",
}

PATTERN_PLAIN = {
    "standard_unit_price": "Per-unit price",
    "correction": "Correction replaces the earlier price",
    "range_upper_bound": "Price range: upper bound used (precedents P-01/P-04)",
    "tiered_price": "Tiered price: tier matching the thread quantity",
    "list_price_discount": "Discount off list price",
    "free_unit_offer": "Free units: effective price (precedents P-02/P-05)",
    "total_for_all_units": "Total price divided by units",
    "word_unit_price": "Price written in words",
}

MISSING_PLAIN = {
    "no_price_found": "No usable price was found in the latest offer.",
    "missing_billing_period": "Subscription price is quoted without a billing period (month/year).",
    "no_previous_quote_for_discount": "A discount off a previous quote was offered, but no previous supplier quote exists.",
    "prev_quote_unusable": "The previous supplier quote referenced by a discount has no usable price.",
}

INJECTION_PLAIN = {
    "addressed_to_ai_or_reviewer": "note addressed to an AI / model / reviewer",
    "hindi_devanagari_ai_directive": "instruction to an AI assistant (Hindi)",
    "ignore_or_override_instructions": "asks to ignore or override instructions",
    "bot_or_system_screening_appeal": "appeal to a bot or automated system",
    "admin_or_system_override": "fake admin / system override",
    "html_comment_or_tag_injection": "hidden HTML comment or tag aimed at an assistant",
    "claim_preapproval_or_reviewer_directive": "claims approval was already granted",
    "force_approve_without_escalation": "demands approval without escalation",
    "forwarded_ai_directive": "forwarded message carrying an AI directive",
}

BANNED_PLAIN = {
    "automatic_renewal": "automatic renewal",
    "upfront_over_limit": "upfront / advance payment above the limit",
    "unlimited_liability": "unlimited liability for Acme",
    "exclusivity": "exclusivity (Acme must buy only from this supplier)",
}

ACTIONS = ("ACCEPT", "COUNTER", "REJECT", "ESCALATE")


def format_inr(value: Decimal | int | str | None, decimals: int = 2, symbol: str = "₹") -> str | None:
    """Format with Indian digit grouping (lakh/crore), e.g. 1234567.5 -> ₹12,34,567.50."""
    if value is None:
        return None
    d = Decimal(str(value))
    q = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ""
    s = f"{abs(d):.{decimals}f}"
    ip, _, fp = s.partition(".")
    if len(ip) > 3:
        head, tail = ip[:-3], ip[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        ip = ",".join(groups + [tail])
    return f"{sign}{symbol}{ip}" + (f".{fp}" if fp else "")


def _num(d: Decimal | None) -> str | None:
    """Exact Decimal as a plain string (no exponent)."""
    if d is None:
        return None
    return format(d.normalize(), "f") if d == d.to_integral() else format(d, "f")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out.append({"thread_id": rec.get("thread_id"), "reason": rec.get("reason"), "details": rec.get("details", {})})
    return out


class CsvValidationError(ValueError):
    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def parse_threads_csv(raw: bytes, playbook: Playbook, max_bytes: int, max_rows: int) -> list[ThreadRow]:
    if len(raw) > max_bytes:
        raise CsvValidationError(f"File is too large: {len(raw):,} bytes (limit {max_bytes:,} bytes).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvValidationError("File is not valid UTF-8 text. Please upload a UTF-8 encoded CSV.")
    if not text.strip():
        raise CsvValidationError("The CSV file is empty.")

    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CsvValidationError(
            f"Missing required column(s): {', '.join(missing)}. Expected columns: {', '.join(REQUIRED_COLUMNS)}.",
        )
    reader.fieldnames = header

    errors: list[str] = []
    threads: list[ThreadRow] = []
    seen: set[str] = set()
    for idx, row in enumerate(reader, start=1):
        if idx > max_rows:
            raise CsvValidationError(f"Too many rows: more than {max_rows:,} data rows (limit {max_rows:,}).")
        row_errs: list[str] = []
        tid = (row.get("thread_id") or "").strip()
        if not tid:
            row_errs.append("thread_id is empty")
        elif tid in seen:
            row_errs.append(f"duplicate thread_id {tid!r}")
        try:
            tdate = date.fromisoformat((row.get("date") or "").strip())
        except ValueError:
            row_errs.append(f"date {row.get('date')!r} is not YYYY-MM-DD")
            tdate = None
        supplier = (row.get("supplier") or "").strip()
        if supplier not in playbook.suppliers and supplier.lower() not in playbook.suppliers:
            row_errs.append(f"unknown supplier {supplier!r}")
        category = (row.get("category") or "").strip()
        if category.lower() not in playbook.categories and category.lower() not in playbook.name_to_slug:
            row_errs.append(f"unknown category {category!r}")
        try:
            qty = int((row.get("quantity") or "").strip())
            if qty < 1:
                raise ValueError
        except ValueError:
            row_errs.append(f"quantity {row.get('quantity')!r} is not a positive integer")
            qty = 0
        try:
            rnd = int((row.get("round") or "").strip())
            if rnd < 1:
                raise ValueError
        except ValueError:
            row_errs.append(f"round {row.get('round')!r} is not a positive integer")
            rnd = 0
        olc_raw = (row.get("our_last_counter") or "").strip()
        olc = None
        if olc_raw:
            try:
                olc = _parse_decimal(olc_raw)
            except InvalidOperation:
                row_errs.append(f"our_last_counter {olc_raw!r} is not a number")
        msg = row.get("supplier_message")
        if msg is None:
            row_errs.append("supplier_message is missing")

        if row_errs:
            errors.append(f"Row {idx} ({tid or 'no id'}): " + "; ".join(row_errs))
            if len(errors) >= 20:
                break
            continue
        seen.add(tid)
        threads.append(
            ThreadRow(
                thread_id=tid,
                thread_date=tdate,
                supplier=supplier,
                category=category,
                quantity=qty,
                round_num=rnd,
                our_last_counter=olc,
                supplier_message=msg,
            )
        )

    if errors:
        raise CsvValidationError(f"{len(errors)} invalid row(s) found. Fix them and upload again.", errors)
    if not threads:
        raise CsvValidationError("The CSV has a header but no data rows.")
    return threads


class Engine:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.playbook: Playbook = load_playbook(cfg.playbook_dir)
        self.sample_threads: list[ThreadRow] = load_threads(cfg.sample_threads_csv) if cfg.sample_threads_csv.exists() else []
        self.sample_answers: dict[str, str] = load_answers(cfg.sample_answers_csv) if cfg.sample_answers_csv.exists() else {}
        self.real_threads: list[ThreadRow] = load_threads(cfg.threads_csv) if cfg.threads_csv.exists() else []
        self._by_id: dict[str, tuple[ThreadRow, str]] = {}
        for t in self.real_threads:
            self._by_id[t.thread_id] = (t, "real")
        for t in self.sample_threads:
            self._by_id[t.thread_id] = (t, "sample")

        self.labels: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="pd_startup_") as td:
            wpath = Path(td) / "warnings.jsonl"
            for t in self.sample_threads + self.real_threads:
                self.labels[t.thread_id] = evaluate_thread(t, self.playbook, warnings_path=wpath).label

        mism = [
            {"thread_id": t.thread_id, "expected": self.sample_answers.get(t.thread_id), "got": self.labels[t.thread_id]}
            for t in self.sample_threads
            if self.labels[t.thread_id] != self.sample_answers.get(t.thread_id)
        ]
        total = len(self.sample_threads)
        self.sample_selfcheck = {
            "passed": total > 0 and not mism,
            "correct": total - len(mism),
            "total": total,
            "accuracy": round(100.0 * (total - len(mism)) / total, 2) if total else 0.0,
            "mismatches": mism,
        }

        self.submission_consistency: dict[str, Any] = {"available": False}
        if cfg.submission_final_csv.exists() and self.real_threads:
            with cfg.submission_final_csv.open("r", encoding="utf-8", newline="") as f:
                sub = {r["thread_id"]: r["label"] for r in csv.DictReader(f)}
            matches = sum(1 for t in self.real_threads if sub.get(t.thread_id) == self.labels[t.thread_id])
            self.submission_consistency = {
                "available": True,
                "checked": len(self.real_threads),
                "matches": matches,
                "passed": matches == len(self.real_threads) == len(sub),
            }

    def find_thread(self, thread_id: str, source: str | None = None) -> tuple[ThreadRow, str] | None:
        hit = self._by_id.get(thread_id.strip())
        if hit is None or (source and hit[1] != source):
            return None
        return hit

    def supplier_status(self, supplier: str) -> str:
        sup = self.playbook.suppliers.get(supplier) or self.playbook.suppliers.get(supplier.lower())
        return sup.status if sup else "UNKNOWN"

    def build_custom_thread(self, data: dict[str, Any]) -> ThreadRow:
        errs = []
        supplier = str(data["supplier"]).strip()
        if supplier not in self.playbook.suppliers and supplier.lower() not in self.playbook.suppliers:
            errs.append(f"unknown supplier {supplier!r}")
        category = str(data["category"]).strip()
        if category.lower() not in self.playbook.categories and category.lower() not in self.playbook.name_to_slug:
            errs.append(f"unknown category {category!r}")
        try:
            tdate = date.fromisoformat(str(data["date"]).strip())
        except ValueError:
            errs.append(f"date {data['date']!r} is not YYYY-MM-DD")
            tdate = None
        if errs:
            raise CsvValidationError("Invalid custom thread.", errs)
        olc = data.get("our_last_counter")
        return ThreadRow(
            thread_id=(str(data.get("thread_id") or "CUSTOM-001")).strip() or "CUSTOM-001",
            thread_date=tdate,
            supplier=supplier,
            category=category,
            quantity=int(data["quantity"]),
            round_num=int(data["round"]),
            our_last_counter=Decimal(str(olc)) if olc is not None else None,
            supplier_message=str(data["supplier_message"]),
        )

    def decide(self, thread: ThreadRow, warnings_path: Path) -> DecisionResult:
        """Pure pass-through to the frozen engine."""
        return evaluate_thread(thread, self.playbook, warnings_path=warnings_path)

    def explain(self, thread: ThreadRow, source: str | None = None) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="pd_explain_") as td:
            wpath = Path(td) / "warnings.jsonl"
            res = evaluate_thread(thread, self.playbook, warnings_path=wpath)  # THE decision
            warnings = _read_jsonl(wpath)
            params = get_effective_params(self.playbook, thread.category, thread.supplier, thread.thread_date)
            segments, latest, earlier = split_thread_messages(thread.supplier_message)
            parsed = extract_and_normalize_offer(
                latest, params, thread.quantity, earlier, thread.thread_id, Path(td) / "discard.jsonl"
            )

        expected = self.sample_answers.get(thread.thread_id) if source == "sample" else None
        decision = {
            "label": res.label,
            "action": res.action,
            "reason": res.reason,
            "reason_plain": REASON_PLAIN.get(res.reason, res.reason),
            "counter_price": res.counter_price,
            "counter_price_formatted": format_inr(res.counter_price, 0) if res.counter_price is not None else None,
            "expected_label": expected,
            "matches_expected": (res.label == expected) if expected is not None else None,
        }

        fired_step = next(s["step"] for s in res.trace if s["outcome"] != "continue")
        msg = thread.supplier_message
        latest_start = msg.rfind(latest) if latest else 0
        latest_start = max(latest_start, 0)

        return {
            "thread": {
                "thread_id": thread.thread_id,
                "source": source or "custom",
                "date": thread.thread_date.isoformat(),
                "supplier": thread.supplier,
                "category": params.category.slug,
                "category_name": params.category.name,
                "quantity": thread.quantity,
                "round": thread.round_num,
                "max_rounds": params.rounds,
                "our_last_counter": _num(thread.our_last_counter),
                "supplier_message": msg,
            },
            "decision": decision,
            "fired_step": fired_step,
            "steps": self._steps(res, params, parsed, thread, fired_step),
            "message_analysis": {
                "has_earlier_history": len(segments) > 1,
                "segment_count": len(segments),
                "segments": [{"kind": s.kind, "sender": s.sender, "date": s.msg_date, "body": s.body} for s in segments],
                "latest_message_range": {"start": latest_start, "end": latest_start + len(latest)},
                "highlights": self._highlights(msg, latest, latest_start, earlier, res, parsed),
            },
            "numbers": self._numbers(thread, params, parsed),
            "warnings": warnings,
        }

    def _steps(self, res: DecisionResult, params: EffectiveParams, parsed: ParsedOffer, thread: ThreadRow, fired: int) -> list[dict[str, Any]]:
        by_step = {s["step"]: s for s in res.trace}
        out = []
        for n in range(1, 7):
            name, key = STEP_NAMES[n]
            tr = by_step.get(n)
            if tr is None:
                out.append({"step": n, "key": key, "name": name, "status": "skipped",
                            "summary": f"Not reached: the decision was made at step {fired}.", "outcome": None, "detail": None})
                continue
            status = "passed" if tr["outcome"] == "continue" else "fired"
            out.append({"step": n, "key": key, "name": name, "status": status,
                        "summary": self._step_summary(n, status, tr, params, parsed, res, thread),
                        "outcome": tr["outcome"], "detail": tr.get("detail")})
        return out

    def _step_summary(self, n, status, tr, params, parsed, res, thread) -> str:
        d = tr.get("detail")
        if n == 1:
            if status == "fired":
                reason = str(d or "")
                if reason.startswith("unknown_currency:"):
                    return f"Quoted in an unsupported currency ({reason.split(':', 1)[1]}); only INR, USD, EUR and GBP are allowed."
                return MISSING_PLAIN.get(reason, f"No usable price ({reason}).")
            pat = d.get("pattern") or ""
            pat_txt = (f"Further {pat.split(':', 1)[1]} off the supplier's own previous quote"
                       if pat.startswith("relative_discount:") else PATTERN_PLAIN.get(pat, pat))
            return f"Usable offer found ({pat_txt}); normalized price {format_inr(Decimal(d['p']))} per unit."
        if n == 2:
            if status == "fired":
                return f"{params.supplier.name} is BLACKLISTED (failed audit): do not do business."
            return f"Supplier status is {params.supplier.status}" + (
                " (probation: no concessions, the acceptable level stays at target)." if params.supplier.status == "PROBATION" else ".")
        if n == 3:
            if status == "fired":
                return f"Prompt injection ({INJECTION_PLAIN.get(d['rule'], d['rule'])}): \u201c{d['match'].strip()[:100]}\u201d."
            return "No text aimed at an AI, bot, system or reviewer anywhere in the thread."
        if n == 4:
            if status == "fired":
                return f"Banned term ({BANNED_PLAIN.get(d['term'], d['term'])}): \u201c{d['match'].strip()[:100]}\u201d."
            return "No banned contract terms in the latest message."
        if n == 5:
            p, L = Decimal(d["p"]), Decimal(d["L_r"])
            probation = " (probation: L = target)" if d.get("probation") else ""
            if status == "passed":
                return f"Price {format_inr(p)} \u2264 round-{d['r']} acceptable level L({d['r']}) = {format_inr(L)}{probation}."
            if res.action == "REJECT":
                return f"Price {format_inr(p)} > L({d['r']}) = {format_inr(L)} on the final round ({d['r']} of {d['R']}){probation}: reject."
            return f"Price {format_inr(p)} > L({d['r']}) = {format_inr(L)}{probation}; counter at {format_inr(res.counter_price, 0)} (rounded half-up)."
        if n == 6:
            tot, lim = Decimal(d["total_spend"]), Decimal(d["approval_limit"])
            if res.action == "ESCALATE":
                return f"Order value {format_inr(tot)} > approval limit {format_inr(lim, 0)}: escalate."
            return f"Order value {format_inr(tot)} \u2264 approval limit {format_inr(lim, 0)}: accept."
        return ""

    def _numbers(self, thread: ThreadRow, params: EffectiveParams, parsed: ParsedOffer) -> dict[str, Any]:
        cat = params.category
        r_eff = min(thread.round_num, params.rounds)
        L = compute_concession_level(params, r_eff)
        p = None if parsed.missing_info else parsed.unit_price_inr
        total = p * Decimal(thread.quantity) if p is not None else None

        amendments = []
        for a in self.playbook.amendments:
            if a.category_slug != cat.slug:
                continue
            delta = (thread.thread_date - a.effective_from).days
            amendments.append({
                "id": a.amd_id,
                "effective_from": a.effective_from.isoformat(),
                "field": a.field_name,
                "new_value": _num(a.new_value),
                "new_value_display": format_inr(a.new_value, 0),
                "text": a.raw_text,
                "active": delta >= 0,
                "days_from_thread": delta,
            })

        sup = params.supplier
        contracts = []
        if cat.slug in sup.price_caps:
            contracts.append({"type": "price_cap", "category": cat.slug, "value": _num(sup.price_caps[cat.slug]),
                              "value_display": format_inr(sup.price_caps[cat.slug], 0), "applies": True,
                              "text": f"Contract price cap {format_inr(sup.price_caps[cat.slug], 0)} replaces the walk-away price."})
        for cur, rate in sup.fx_rates.items():
            contracts.append({"type": "fx_rate", "currency": cur, "value": _num(rate), "value_display": format_inr(rate),
                              "applies": parsed.currency == cur,
                              "text": f"Contract rate {format_inr(rate)} per {cur} replaces the table rate {format_inr(self.playbook.base_fx_rates.get(cur))}."})

        return {
            "normalized_unit": cat.normalized_unit,
            "pattern": parsed.pattern_used,
            "quoted_currency": parsed.currency,
            "gst_included": parsed.gst_included,
            "gst_rate": _num(cat.gst_rate),
            "normalized_price": _num(p),
            "normalized_price_display": format_inr(p),
            "price_breakdown": self._breakdown(parsed, params) if p is not None else [],
            "round": thread.round_num,
            "round_used": r_eff,
            "max_rounds": params.rounds,
            "beta": params.beta,
            "base_target": _num(cat.target_price),
            "effective_target": _num(params.target_price),
            "base_walkaway": _num(cat.walkaway_price),
            "effective_walkaway": _num(params.walkaway_price),
            "L_r": _num(L),
            "L_r_display": format_inr(L),
            "L_r_rounded": int(L.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            "base_approval_limit": _num(cat.approval_limit),
            "effective_approval_limit": _num(params.approval_limit),
            "quantity": thread.quantity,
            "total_value": _num(total),
            "total_value_display": format_inr(total),
            "supplier_status": sup.status,
            "probation_lock": sup.status == "PROBATION",
            "amendments": amendments,
            "applied_amendments": [a for a in amendments if a["active"]],
            "contracts": contracts,
            "fx_rates_used": {k: _num(v) for k, v in params.fx_rates.items()},
        }

    @staticmethod
    def _breakdown(parsed: ParsedOffer, params: EffectiveParams) -> list[dict[str, Any]]:
        """Replay the engine's transformation notes to show quoted price -> normalized INR."""
        notes = list(parsed.notes)
        raw = parsed.raw_amount
        cur = parsed.currency or "INR"
        rows: list[dict[str, Any]] = []

        def fmt(v: Decimal, c: str) -> str:
            return format_inr(v) if c == "INR" else f"{c} {v.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"

        pattern_prefixes = ("total_div_", "list_disc=", "free_units=", "vol_disc_", "range_upper_bound", "tier_")
        pat_notes = [n for n in notes if n.startswith(pattern_prefixes)]
        orig = raw
        for n in reversed(pat_notes):
            if n.startswith("total_div_"):
                orig = orig * Decimal(n[len("total_div_"):])
            elif n.startswith("list_disc="):
                orig = orig / ((Decimal(100) - Decimal(n[len("list_disc="):-1])) / Decimal(100))
            elif n.startswith("free_units="):
                m = re.match(r"free_units=([\d.]+)/\(([\d.]+)\+([\d.]+)\)", n)
                if m:
                    orig = orig * (Decimal(m.group(2)) + Decimal(m.group(3))) / Decimal(m.group(1))
            elif n.startswith("vol_disc_") and "_applied_" in n:
                pct = Decimal(re.match(r"vol_disc_([\d.]+)%", n).group(1))
                orig = orig / ((Decimal(100) - pct) / Decimal(100))

        value = orig
        rows.append({"stage": "Quoted", "value": fmt(value, cur), "formula": PATTERN_PLAIN.get(parsed.pattern_used or "", parsed.pattern_used or "")})
        for n in notes:
            if n.startswith("total_div_"):
                k = Decimal(n[len("total_div_"):])
                value = value / k
                rows.append({"stage": "Total \u00f7 units", "value": fmt(value, cur), "formula": f"\u00f7 {k}"})
            elif n.startswith("list_disc="):
                pct = n[len("list_disc="):]
                value = value * (Decimal(100) - Decimal(pct[:-1])) / Decimal(100)
                rows.append({"stage": "List-price discount", "value": fmt(value, cur), "formula": f"\u2212 {pct}"})
            elif n.startswith("free_units="):
                m = re.match(r"free_units=([\d.]+)/\(([\d.]+)\+([\d.]+)\)", n)
                if m:
                    value = value * Decimal(m.group(1)) / (Decimal(m.group(2)) + Decimal(m.group(3)))
                    rows.append({"stage": "Free units", "value": fmt(value, cur), "formula": f"\u00d7 {m.group(1)} \u00f7 ({m.group(2)} + {m.group(3)})"})
            elif n.startswith("vol_disc_"):
                pct = re.match(r"vol_disc_([\d.]+)%", n).group(1)
                if "_applied_" in n:
                    value = value * (Decimal(100) - Decimal(pct)) / Decimal(100)
                    rows.append({"stage": "Volume discount", "value": fmt(value, cur), "formula": f"\u2212 {pct}% (quantity meets threshold)"})
                else:
                    rows.append({"stage": "Volume discount", "value": fmt(value, cur), "formula": f"{pct}% not applied (quantity below threshold)"})
            elif n == "range_upper_bound":
                rows.append({"stage": "Range", "value": fmt(value, cur), "formula": "upper bound used (never the cheaper end)"})
            elif n.startswith("tier_"):
                rows.append({"stage": "Tier", "value": fmt(value, cur), "formula": n.replace("tier_le_", "quantity \u2264 ").replace("tier_gt_", "quantity > ")})
            elif n.startswith("fx_"):
                c, rate = n[3:].split("*")
                value = value * Decimal(rate)
                cur = "INR"
                rows.append({"stage": f"FX {c} \u2192 INR", "value": fmt(value, cur), "formula": f"\u00d7 {rate}"})
            elif n.startswith("dozen_div_"):
                k = Decimal(n[len("dozen_div_"):])
                value = value / k
                rows.append({"stage": "Dozen \u2192 unit", "value": fmt(value, cur), "formula": f"\u00f7 {k}"})
            elif n.startswith("pack_conv_"):
                unit, mult = n[len("pack_conv_"):].split("*")
                value = value * Decimal(mult)
                rows.append({"stage": f"{unit.capitalize()} conversion", "value": fmt(value, cur), "formula": f"\u00d7 {Decimal(mult).normalize()}"})
            elif n == "period_month_to_year*12":
                value = value * 12
                rows.append({"stage": "Monthly \u2192 yearly", "value": fmt(value, cur), "formula": "\u00d7 12"})
            elif n == "period_year_to_month/12":
                value = value / 12
                rows.append({"stage": "Yearly \u2192 monthly", "value": fmt(value, cur), "formula": "\u00f7 12"})
            elif n.startswith("gst_excl/"):
                value = value / (Decimal(1) + params.gst_rate)
                rows.append({"stage": "Remove GST", "value": fmt(value, cur), "formula": f"\u00f7 (1 + {params.gst_rate.normalize()})"})
            elif n.startswith("rel_disc="):
                pct = n[len("rel_disc="):]
                value = value * (Decimal(100) - Decimal(pct[:-1])) / Decimal(100)
                rows.append({"stage": "Further discount off previous quote", "value": fmt(value, cur), "formula": f"\u2212 {pct}"})
            elif n.startswith("ambiguous_unit:"):
                rows.append({"stage": "Unit note", "value": fmt(value, cur), "formula": f"unit {n.split(':', 1)[1]!r} flagged"})
        p = parsed.unit_price_inr
        consistent = cur == "INR" and abs(value - p) <= max(Decimal("0.000001"), abs(p) * Decimal("1e-12"))
        rows.append({"stage": "Normalized price p", "value": format_inr(p),
                     "formula": f"INR excl. GST, {params.category.normalized_unit.replace('INR excl. GST ', '')}",
                     "final": True, "replay_consistent": consistent})
        return rows

    @staticmethod
    def _locate(hay: str, needle: str, base: int = 0) -> tuple[int, int] | None:
        needle = (needle or "").strip()
        if not needle:
            return None
        i = hay.find(needle)
        if i >= 0:
            return base + i, base + i + len(needle)
        toks = needle.split()
        if toks:
            m = re.search(r"\s+".join(re.escape(t) for t in toks), hay, re.I)
            if m:
                return base + m.start(), base + m.end()
        return None

    def _highlights(self, msg: str, latest: str, latest_start: int, earlier: list[str], res: DecisionResult, parsed: ParsedOffer) -> list[dict[str, Any]]:
        cands: list[tuple[int, dict[str, Any]]] = []

        def add(prio, span, kind, label):
            if span and span[1] > span[0]:
                cands.append((prio, {"start": span[0], "end": span[1], "kind": kind, "label": label}))

        by_step = {s["step"]: s for s in res.trace}
        s3 = by_step.get(3)
        if s3 and s3["outcome"] != "continue":
            add(0, self._locate(msg, s3["detail"]["match"]), "injection",
                f"Prompt injection: {INJECTION_PLAIN.get(s3['detail']['rule'], s3['detail']['rule'])}")
        s4 = by_step.get(4)
        if s4 and s4["outcome"] != "continue":
            d = s4["detail"]
            span = self._locate(latest, d["match"], latest_start)
            if span is None and d["term"] == "upfront_over_limit":
                m = re.search(r"[^.\n]*\b(?:upfront|up-front|advance)\b[^.\n]*", latest, re.I)
                span = (latest_start + m.start(), latest_start + m.end()) if m else None
            add(1, span, "banned_term", f"Banned term: {BANNED_PLAIN.get(d['term'], d['term'])}")

        pat = parsed.pattern_used or ""
        if parsed.missing_info:
            m = re.search(r"\b[A-Z]{3}\s+[\d,]+(?:\.\d+)?", latest)
            if m and (parsed.missing_reason or "").startswith("unknown_currency"):
                add(2, (latest_start + m.start(), latest_start + m.end()), "quoted_price", "Unsupported currency")
        elif pat == "correction":
            m = re.search(r"should\s+read\s+", latest, re.I)
            if m:
                a = CURR_AMT_RE.search(latest, m.end())
                if a:
                    add(2, (latest_start + a.start(), latest_start + a.end()), "quoted_price", "Corrected price (replaces the earlier one)")
        elif pat.startswith("relative_discount:"):
            m = re.search(r"(?:a\s+further\s+)?\d+(?:\.\d+)?%\s+(?:off|discount\s+on)\s+our\s+(?:previous|earlier)\s+quote", latest, re.I)
            if m:
                add(2, (latest_start + m.start(), latest_start + m.end()), "quoted_price", "Discount off the supplier's own previous quote")
            if earlier:
                eb = earlier[-1]
                es = msg.find(eb)
                a = CURR_AMT_RE.search(eb) if es >= 0 else None
                if a:
                    add(3, (es + a.start(), es + a.end()), "quoted_price", "Supplier's previous quote (discount base)")
        elif pat == "range_upper_bound":
            m = re.search(rf"between\s+{CURR_AMT_PAT}\s+and\s+{CURR_AMT_PAT}", latest, re.I)
            if m:
                add(2, (latest_start + m.start(), latest_start + m.end()), "quoted_price", "Price range (upper bound used)")
        elif pat == "word_unit_price":
            m = WORD_CURR_AMT_RE.search(latest)
            if m:
                add(2, (latest_start + m.start(1), latest_start + m.end(2)), "quoted_price", "Price written in words")
        else:
            for i, a in enumerate(CURR_AMT_RE.finditer(latest)):
                add(2, (latest_start + a.start(), latest_start + a.end()), "quoted_price", "Quoted price")
                if i >= 1 or pat != "tiered_price":
                    break

        for m in re.finditer(r"^\[(?:Earlier|Latest)\s+message[^\]\n]*\]", msg, re.M):
            add(4, (m.start(), m.end()), "header", "Latest message" if m.group(0).startswith("[Latest") else "Earlier message")

        accepted: list[dict[str, Any]] = []
        for _, span in sorted(cands, key=lambda x: (x[0], x[1]["start"])):
            if all(span["end"] <= a["start"] or span["start"] >= a["end"] for a in accepted):
                accepted.append(span)
        accepted.sort(key=lambda s: s["start"])
        for s in accepted:
            s["text"] = msg[s["start"]:s["end"]]
        return accepted

    def summarize(self, thread: ThreadRow, res: DecisionResult) -> dict[str, Any]:
        fired = next(s for s in res.trace if s["outcome"] != "continue")
        s5 = next((s for s in res.trace if s["step"] == 5), None)
        s6 = next((s for s in res.trace if s["step"] == 6), None)
        d5 = s5["detail"] if s5 else {}
        d6 = s6["detail"] if s6 else {}
        return {
            "thread_id": thread.thread_id,
            "date": thread.thread_date.isoformat(),
            "supplier": thread.supplier,
            "supplier_status": self.supplier_status(thread.supplier),
            "category": thread.category,
            "quantity": thread.quantity,
            "round": thread.round_num,
            "label": res.label,
            "action": res.action,
            "reason": res.reason,
            "counter_price": res.counter_price,
            "fired_step": fired["step"],
            "fired_step_name": STEP_NAMES[fired["step"]][0],
            "p": d5.get("p"),
            "target": d5.get("target"),
            "L_r": d5.get("L_r"),
            "walkaway": d5.get("walkaway"),
            "total_value": d6.get("total_spend"),
            "approval_limit": d6.get("approval_limit"),
            "expected_label": self.sample_answers.get(thread.thread_id),
            "warnings": [],
        }


def compute_analytics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    by_action = Counter(r["action"] for r in rows)
    by_reason = Counter(r["reason"] for r in rows)
    by_pair = Counter(f"{r['action']}:{r['reason']}" for r in rows)

    def bucket(key: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in rows:
            b = out.setdefault(r[key], {"total": 0, **{a: 0 for a in ACTIONS}})
            b["total"] += 1
            b[r["action"]] += 1
        for b in out.values():
            b["share_pct"] = round(100.0 * b["total"] / total, 2) if total else 0.0
        return dict(sorted(out.items()))

    gaps_t, gaps_l, gaps_pct = [], [], []
    for r in rows:
        if r["action"] == "COUNTER" and r["p"] and r["target"] and r["L_r"]:
            p, t, L = Decimal(r["p"]), Decimal(r["target"]), Decimal(r["L_r"])
            gaps_t.append(p - t)
            gaps_l.append(p - L)
            gaps_pct.append((p - t) / t * 100)

    def stats(vals: list[Decimal]) -> dict[str, Any]:
        if not vals:
            return {"count": 0, "avg": None, "min": None, "max": None, "median": None}
        s = sorted(vals)
        mid = len(s) // 2
        med = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        q = Decimal("0.01")
        return {"count": len(s), "avg": float((sum(s) / len(s)).quantize(q)), "min": float(s[0].quantize(q)),
                "max": float(s[-1].quantize(q)), "median": float(med.quantize(q))}

    rounds: dict[int, dict[str, Any]] = {}
    for r in rows:
        b = rounds.setdefault(r["round"], {"round": r["round"], "total": 0, **{a: 0 for a in ACTIONS}})
        b["total"] += 1
        b[r["action"]] += 1
    for b in rounds.values():
        b["counter_rate_pct"] = round(100.0 * b["COUNTER"] / b["total"], 2) if b["total"] else 0.0

    warn = Counter(w for r in rows for w in r.get("warnings", []))
    return {
        "total": total,
        "counts_by_action": {a: by_action.get(a, 0) for a in ACTIONS},
        "counts_by_reason": dict(by_reason.most_common()),
        "counts_by_action_reason": dict(by_pair.most_common()),
        "by_category": bucket("category"),
        "by_supplier_status": bucket("supplier_status"),
        "price_gap": {
            "description": "COUNTER threads only: normalized price p minus target, and p minus L(r), in INR.",
            "gap_to_target": stats(gaps_t),
            "gap_to_L_r": stats(gaps_l),
            "gap_to_target_pct": stats(gaps_pct),
        },
        "counter_rate_by_round": [rounds[k] for k in sorted(rounds)],
        "fired_step_counts": dict(sorted(Counter(r["fired_step"] for r in rows).items())),
        "warnings_count": sum(warn.values()),
        "warnings_by_reason": dict(warn.most_common()),
    }


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = Engine(settings)
    return _engine
