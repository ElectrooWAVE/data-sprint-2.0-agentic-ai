"""Deterministic procurement decision engine following policies/decision-procedure.md."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from load import EffectiveParams, Playbook, ThreadRow, get_effective_params

DEFAULT_WARNINGS_PATH = Path("checkpoints/warnings.jsonl")

KNOWN_CURRENCY_MAP = {
    "₹": "INR",
    "rs.": "INR",
    "rs": "INR",
    "inr": "INR",
    "rupees": "INR",
    "rupee": "INR",
    "usd": "USD",
    "us$": "USD",
    "$": "USD",
    "dollars": "USD",
    "dollar": "USD",
    "eur": "EUR",
    "€": "EUR",
    "euros": "EUR",
    "euro": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "pounds": "GBP",
    "pound": "GBP",
}

UNKNOWN_CURRENCY_RE = re.compile(
    r"(?<![A-Za-z])(AUD|CAD|SGD|JPY|CHF|CNY|RMB|AED|SAR|MYR|THB|NZD|HKD|KRW|ZAR|SEK|NOK|DKK|PLN|RUB|BRL|MXN|¥|₩|CHF)(?![A-Za-z])"
    r"|(?<![A-Za-z])([A-Z]{3})\s+[\d,]+(?:\.\d+)?",
)

CURRENCY_TOKEN_PAT = r"(?:₹|Rs\.?|INR|USD|US\$|\$|EUR|€|GBP|£)"
AMOUNT_PAT = r"(?:\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|lacs?|crores?|k\b))?)"
CURR_AMT_PAT = rf"({CURRENCY_TOKEN_PAT})\s*({AMOUNT_PAT})"
CURR_AMT_RE = re.compile(CURR_AMT_PAT, re.I)

START_NUM_WORD_PAT = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
    r"lakhs?|lacs?|crores?|million)"
)
CONT_NUM_WORD_PAT = rf"(?:{START_NUM_WORD_PAT}|and)"
WORD_CURR_AMT_RE = re.compile(
    rf"\b({START_NUM_WORD_PAT}(?:[\s\-]+{CONT_NUM_WORD_PAT})*)\s+(rupees?|dollars?|euros?|pounds?)\b([^\n]*)",
    re.I,
)

WORD_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
WORD_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
WORD_SCALES = {
    "hundred": 100,
    "thousand": 1000,
    "lakh": 100000,
    "lakhs": 100000,
    "lac": 100000,
    "lacs": 100000,
    "million": 1000000,
    "crore": 10000000,
    "crores": 10000000,
}


@dataclass
class MessageSegment:
    kind: str  # 'Earlier' | 'Latest' | 'Single'
    sender: str
    msg_date: str | None
    body: str


@dataclass
class ParsedOffer:
    missing_info: bool
    missing_reason: str | None = None
    unit_price_inr: Decimal | None = None
    currency: str | None = None
    raw_amount: Decimal | None = None
    gst_included: bool = False
    billing_period_found: str | None = None  # 'month' | 'year' | None
    pattern_used: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class DecisionContext:
    thread: ThreadRow
    params: EffectiveParams
    segments: list[MessageSegment]
    latest_message: str
    supplier_earlier_messages: list[str]
    parsed_offer: ParsedOffer
    warnings_path: Path


@dataclass
class DecisionResult:
    thread_id: str
    action: str
    reason: str
    counter_price: int | None
    label: str
    trace: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "action": self.action,
            "reason": self.reason,
            "counter_price": self.counter_price,
            "label": self.label,
            "trace": self.trace,
        }


def log_warning(
    thread_id: str,
    reason: str,
    details: dict[str, Any] | None = None,
    warnings_path: Path = DEFAULT_WARNINGS_PATH,
) -> None:
    """Append a structured parsing or rule uncertainty warning to checkpoints/warnings.jsonl."""
    warnings_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "thread_id": thread_id,
        "reason": reason,
        "details": details or {},
    }
    with warnings_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _words_to_decimal(phrase: str) -> Decimal | None:
    """Convert English number words (e.g. 'thirty five', 'two lakh fifty thousand') to Decimal."""
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", phrase.lower())
    if not tokens:
        return None
    if len(tokens) == 1 and re.fullmatch(r"\d+(?:\.\d+)?", tokens[0]):
        return Decimal(tokens[0])

    total = Decimal("0")
    current = Decimal("0")
    matched_any = False

    for tok in tokens:
        if tok in {"and", "a", "an"}:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            current += Decimal(tok)
            matched_any = True
        elif tok in WORD_UNITS:
            current += Decimal(WORD_UNITS[tok])
            matched_any = True
        elif tok in WORD_TENS:
            current += Decimal(WORD_TENS[tok])
            matched_any = True
        elif tok == "hundred":
            current = (current or Decimal("1")) * Decimal("100")
            matched_any = True
        elif tok in WORD_SCALES:
            scale = Decimal(WORD_SCALES[tok])
            total += (current or Decimal("1")) * scale
            current = Decimal("0")
            matched_any = True
        else:
            return None

    return (total + current) if matched_any else None


def _parse_amount_str(raw: str) -> Decimal:
    """Parse numeric amount possibly having commas and k/lakh/crore suffix."""
    s = raw.strip().lower()
    mult = Decimal("1")
    if re.search(r"\b(?:lakhs?|lacs?)\b", s):
        mult = Decimal("100000")
        s = re.sub(r"\b(?:lakhs?|lacs?)\b", "", s)
    elif re.search(r"\bcrores?\b", s):
        mult = Decimal("10000000")
        s = re.sub(r"\bcrores?\b", "", s)
    elif re.search(r"k\b", s):
        mult = Decimal("1000")
        s = re.sub(r"k\b", "", s)
    return Decimal(s.replace(",", "").strip()) * mult


def _normalize_currency(token: str) -> str | None:
    return KNOWN_CURRENCY_MAP.get(token.strip().lower())


def split_thread_messages(supplier_message: str) -> tuple[list[MessageSegment], str, list[str]]:
    """Split bracketed thread history into segments, returning latest_message and supplier earlier bodies."""
    header_re = re.compile(r"^\[(Earlier|Latest)\s+message\s*[—\-]\s*([^\]]+)\]\s*$", re.M)
    matches = list(header_re.finditer(supplier_message))
    if not matches:
        seg = MessageSegment(kind="Single", sender="Supplier", msg_date=None, body=supplier_message)
        return [seg], supplier_message, []

    segments: list[MessageSegment] = []
    for idx, m in enumerate(matches):
        kind = m.group(1)
        meta = m.group(2).strip()
        if "," in meta:
            sender_part, date_part = meta.rsplit(",", 1)
            sender = sender_part.strip()
            msg_date: str | None = date_part.strip()
        else:
            sender = meta
            msg_date = None
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(supplier_message)
        body = supplier_message[start:end].strip()
        segments.append(MessageSegment(kind=kind, sender=sender, msg_date=msg_date, body=body))

    latest_segs = [s for s in segments if s.kind == "Latest"]
    if latest_segs:
        latest_message = latest_segs[-1].body
    else:
        non_acme = [s for s in segments if "acme" not in s.sender.lower()]
        latest_message = non_acme[-1].body if non_acme else segments[-1].body

    supplier_earlier = [
        s.body
        for s in segments
        if s.kind == "Earlier" and "acme" not in s.sender.lower()
    ]
    return segments, latest_message, supplier_earlier


def _detect_gst_included(text: str) -> bool:
    """Return True if text states GST is included (and not excluded)."""
    if re.search(r"\b(?:excluding|excl\.?|exclusive\s+of|plus)\s+gst\b", text, re.I):
        return False
    return bool(
        re.search(
            r"\b(?:including|inclusive\s+of|incl\.?|with)\s+gst\b|\bgst\s+included\b",
            text,
            re.I,
        )
    )


def _detect_billing_period(text: str) -> str | None:
    """Detect monthly vs yearly billing period wording in a message."""
    has_month = bool(
        re.search(
            r"\bper\s+month\b|\bmonthly\b|/\s*month\b|/\s*mo\b|\bp\.m\.\b",
            text,
            re.I,
        )
    )
    has_year = bool(
        re.search(
            r"\bper\s+year\b|\byearly\b|\bper\s+annum\b|\bannual(?:ly)?\b|/\s*year\b|/\s*yr\b|\bp\.a\.\b",
            text,
            re.I,
        )
    )
    if has_month and not has_year:
        return "month"
    if has_year and not has_month:
        return "year"
    return None


def _check_unknown_currency(text: str) -> str | None:
    """Detect any unsupported currency token or 3-letter currency code preceding an amount."""
    for m in UNKNOWN_CURRENCY_RE.finditer(text):
        code = (m.group(1) or m.group(2) or "").upper()
        if code in {"INR", "USD", "EUR", "GBP"}:
            continue
        if code:
            return code
    return None


def _apply_pack_or_unit_conversion(
    price: Decimal,
    context_text: str,
    params: EffectiveParams,
    thread_id: str,
    warnings_path: Path,
) -> tuple[Decimal, list[str]]:
    """Convert dozen / pack / ream units to the category normalized unit if quoted in sub/super-units."""
    notes: list[str] = []
    lower = context_text.lower()

    if re.search(r"\b(?:per|a)\s+dozen\b|\bfor\s+all\s+\d+\s+dozens?\b", lower):
        price = price / params.dozen_size
        notes.append(f"dozen_div_{params.dozen_size}")
        return price, notes

    if params.pack_conversion is not None:
        q_unit = re.escape(params.pack_conversion.quoted_unit)
        if re.search(rf"\b(?:per|a)\s+{q_unit}s?\b|\bfor\s+all\s+\d+\s+{q_unit}s?\b", lower):
            price = price * params.pack_conversion.multiplier
            notes.append(f"pack_conv_{params.pack_conversion.quoted_unit}*{params.pack_conversion.multiplier}")
            return price, notes

    # Check if 'per <unit>' specifies something unexpected (excluding billing units)
    m_unit = re.search(r"\bper\s+([a-z]+)", lower)
    if m_unit:
        unit_word = m_unit.group(1).rstrip("s")
        norm_lower = params.category.normalized_unit.lower()
        allowed_words = {
            "unit",
            "month",
            "year",
            "annum",
            "pack",
            "ream",
            "box",
            "dozen",
        }
        if unit_word not in norm_lower and unit_word not in allowed_words:
            log_warning(
                thread_id,
                "ambiguous_unit",
                {"quoted_unit": m_unit.group(1), "expected": params.category.normalized_unit},
                warnings_path,
            )
            notes.append(f"ambiguous_unit:{m_unit.group(1)}")

    return price, notes


def _apply_volume_discount(raw_amt: Decimal, text: str, quantity: int, notes: list[str]) -> Decimal:
    """Apply conditional volume discount if the thread's quantity meets the stated threshold."""
    vol_m = re.search(
        r"(?:with\s+)?(?:a\s+)?(?:further\s+)?(\d+(?:\.\d+)?)%\s+(?:(?:volume\s+)?discount|off)\s+"
        r"(?:for|on)\s+orders?\s+of\s+(?:at\s+least\s+|>=?\s*)?(\d+)\b[^.\n]*?(?:\bor\s+more\b|\+)?",
        text,
        re.I,
    )
    if vol_m:
        disc_pct = Decimal(vol_m.group(1))
        min_qty = int(vol_m.group(2))
        if quantity >= min_qty:
            raw_amt = raw_amt * (Decimal("100") - disc_pct) / Decimal("100")
            notes.append(f"vol_disc_{disc_pct}%_applied_qty>={min_qty}")
        else:
            notes.append(f"vol_disc_{disc_pct}%_skipped_qty<{min_qty}")
    return raw_amt


def extract_and_normalize_offer(
    text: str,
    params: EffectiveParams,
    quantity: int,
    earlier_supplier_messages: list[str],
    thread_id: str,
    warnings_path: Path,
    allow_previous_ref: bool = True,
) -> ParsedOffer:
    """Extract and normalize the supplier's latest offer to full-precision INR excl. GST."""
    unknown_curr = _check_unknown_currency(text)
    if unknown_curr is not None:
        log_warning(thread_id, "unknown_currency", {"currency": unknown_curr}, warnings_path)
        return ParsedOffer(missing_info=True, missing_reason=f"unknown_currency:{unknown_curr}")

    notes: list[str] = []
    gst_included = _detect_gst_included(text)
    billing_found = _detect_billing_period(text)

    # 1. Correction of previous email price
    corr_m = re.search(
        r"CORRECTION\s*[—\-:]\s*the\s+price\s+in\s+my\s+previous\s+email\s+should\s+read\s+(.+?)(?=,\s*not\b|\n|$)",
        text,
        re.I,
    )
    if corr_m:
        corr_span = corr_m.group(1)
        amt_m = CURR_AMT_RE.search(corr_span)
        if amt_m:
            curr = _normalize_currency(amt_m.group(1))
            raw_amt = _parse_amount_str(amt_m.group(2))
            pattern_used = "correction"
            unit_ctx = corr_span
            return _finalize_offer(
                raw_amt, curr, gst_included, billing_found, unit_ctx, pattern_used, notes, params, thread_id, warnings_path
            )

    # 2. Relative discount off supplier's own previous quote
    rel_m = re.search(
        r"(?:a\s+further\s+)?(\d+(?:\.\d+)?)%\s+(?:off|discount\s+on)\s+our\s+(?:previous|earlier)\s+quote",
        text,
        re.I,
    )
    if rel_m:
        if not allow_previous_ref or not earlier_supplier_messages:
            log_warning(thread_id, "no_previous_quote_for_discount", {}, warnings_path)
            return ParsedOffer(missing_info=True, missing_reason="no_previous_quote_for_discount")
        disc_pct = Decimal(rel_m.group(1))
        prev_offer = extract_and_normalize_offer(
            earlier_supplier_messages[-1],
            params,
            quantity,
            earlier_supplier_messages[:-1],
            thread_id,
            warnings_path,
            allow_previous_ref=False,
        )
        if prev_offer.missing_info or prev_offer.unit_price_inr is None:
            return ParsedOffer(missing_info=True, missing_reason=prev_offer.missing_reason or "prev_quote_unusable")
        factor = (Decimal("100") - disc_pct) / Decimal("100")
        discounted = prev_offer.unit_price_inr * factor
        # If latest message explicitly states GST included while earlier didn't, adjust
        if gst_included and not prev_offer.gst_included:
            discounted = discounted / (Decimal("1") + params.gst_rate)
        return ParsedOffer(
            missing_info=False,
            unit_price_inr=discounted,
            currency=prev_offer.currency,
            raw_amount=prev_offer.raw_amount,
            gst_included=gst_included or prev_offer.gst_included,
            billing_period_found=billing_found or prev_offer.billing_period_found,
            pattern_used=f"relative_discount:{disc_pct}%",
            notes=prev_offer.notes + [f"rel_disc={disc_pct}%"],
        )

    # 3. Quoted range ("between X and Y ...") -> upper bound Y (Precedents P-01, P-04)
    range_m = re.search(
        rf"between\s+{CURR_AMT_PAT}\s+and\s+{CURR_AMT_PAT}([^\n]*)",
        text,
        re.I,
    )
    if range_m:
        curr = _normalize_currency(range_m.group(3))
        raw_amt = _parse_amount_str(range_m.group(4))
        notes.append("range_upper_bound")
        return _finalize_offer(
            raw_amt,
            curr,
            gst_included,
            billing_found,
            range_m.group(5),
            "range_upper_bound",
            notes,
            params,
            thread_id,
            warnings_path,
        )

    # 4. Tiered pricing ("X for orders of up to K ..., and Y for larger orders")
    tier_m = re.search(
        rf"{CURR_AMT_PAT}[^.\n]*?\bfor\s+orders?\s+of\s+up\s+to\s+(\d+)\s+[^,\n]+,?\s+and\s+{CURR_AMT_PAT}[^.\n]*?\bfor\s+larger\s+orders?",
        text,
        re.I,
    )
    if tier_m:
        threshold = int(tier_m.group(3))
        if quantity <= threshold:
            curr = _normalize_currency(tier_m.group(1))
            raw_amt = _parse_amount_str(tier_m.group(2))
            notes.append(f"tier_le_{threshold}")
        else:
            curr = _normalize_currency(tier_m.group(4))
            raw_amt = _parse_amount_str(tier_m.group(5))
            notes.append(f"tier_gt_{threshold}")
        return _finalize_offer(
            raw_amt, curr, gst_included, billing_found, tier_m.group(0), "tiered_price", notes, params, thread_id, warnings_path
        )

    # 5. List-price discount ("D% discount on / off our list price of X")
    list_m = re.search(
        rf"(\d+(?:\.\d+)?)%\s+(?:discount|off)\s+(?:on|from)\s+our\s+list\s+price\s+of\s+{CURR_AMT_PAT}([^\n]*)",
        text,
        re.I,
    )
    if list_m:
        disc_pct = Decimal(list_m.group(1))
        curr = _normalize_currency(list_m.group(2))
        list_amt = _parse_amount_str(list_m.group(3))
        raw_amt = list_amt * (Decimal("100") - disc_pct) / Decimal("100")
        notes.append(f"list_disc={disc_pct}%")
        return _finalize_offer(
            raw_amt,
            curr,
            gst_included,
            billing_found,
            list_m.group(4),
            "list_price_discount",
            notes,
            params,
            thread_id,
            warnings_path,
        )

    # 6. Free-unit bonus ("X per ..., plus K free ... for every N you pay for") (Precedents P-02, P-05)
    free_m = re.search(
        rf"{CURR_AMT_PAT}([^.\n]*?),\s*plus\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+free\s+[^.\n]*?\bfor\s+every\s+(\d+)\s+you\s+pay\s+for",
        text,
        re.I,
    )
    if free_m:
        curr = _normalize_currency(free_m.group(1))
        base_amt = _parse_amount_str(free_m.group(2))
        free_cnt = _words_to_decimal(free_m.group(4))
        paid_cnt = Decimal(free_m.group(5))
        if free_cnt is None:
            log_warning(thread_id, "unparsed_free_unit_count", {"token": free_m.group(4)}, warnings_path)
            return ParsedOffer(missing_info=True, missing_reason="unparsed_free_unit_count")
        raw_amt = base_amt * paid_cnt / (paid_cnt + free_cnt)
        notes.append(f"free_units={paid_cnt}/({paid_cnt}+{free_cnt})")
        return _finalize_offer(
            raw_amt,
            curr,
            gst_included,
            billing_found,
            free_m.group(3),
            "free_unit_offer",
            notes,
            params,
            thread_id,
            warnings_path,
        )

    # 7. Total price for all N units ("X in total for all N ...")
    total_m = re.search(
        rf"{CURR_AMT_PAT}\s+in\s+total\s+for\s+(?:all\s+)?(\d+)([^\n]*)",
        text,
        re.I,
    )
    if total_m:
        curr = _normalize_currency(total_m.group(1))
        total_amt = _parse_amount_str(total_m.group(2))
        total_qty = Decimal(total_m.group(3))
        if total_qty <= 0:
            log_warning(thread_id, "invalid_total_quantity", {"qty": str(total_qty)}, warnings_path)
            return ParsedOffer(missing_info=True, missing_reason="invalid_total_quantity")
        raw_amt = total_amt / total_qty
        notes.append(f"total_div_{total_qty}")
        return _finalize_offer(
            raw_amt,
            curr,
            gst_included,
            billing_found,
            total_m.group(4),
            "total_for_all_units",
            notes,
            params,
            thread_id,
            warnings_path,
        )

    # 8. Standard per-unit currency amount (optionally with conditional volume discount)
    std_m = re.search(rf"{CURR_AMT_PAT}([^\n]*)", text, re.I)
    if std_m:
        curr = _normalize_currency(std_m.group(1))
        raw_amt = _parse_amount_str(std_m.group(2))
        trailing = std_m.group(3)
        raw_amt = _apply_volume_discount(raw_amt, text, quantity, notes)
        return _finalize_offer(
            raw_amt, curr, gst_included, billing_found, trailing, "standard_unit_price", notes, params, thread_id, warnings_path
        )

    # 9. Word-number currency price ("two hundred sixty-six rupees per meal", "one lakh ... rupees")
    word_m = WORD_CURR_AMT_RE.search(text)
    if word_m:
        raw_amt = _words_to_decimal(word_m.group(1).replace("-", " "))
        curr = _normalize_currency(word_m.group(2))
        if raw_amt is not None and curr is not None:
            trailing = word_m.group(3)
            raw_amt = _apply_volume_discount(raw_amt, text, quantity, notes)
            return _finalize_offer(
                raw_amt, curr, gst_included, billing_found, trailing, "word_unit_price", notes, params, thread_id, warnings_path
            )

    # 10. No price matched
    log_warning(thread_id, "no_price_found", {"snippet": text[:200]}, warnings_path)
    return ParsedOffer(missing_info=True, missing_reason="no_price_found")


def _finalize_offer(
    raw_amt: Decimal,
    curr: str | None,
    gst_included: bool,
    billing_found: str | None,
    unit_context: str,
    pattern_used: str,
    notes: list[str],
    params: EffectiveParams,
    thread_id: str,
    warnings_path: Path,
) -> ParsedOffer:
    """Apply FX rate, pack/unit conversion, billing-period normalization, and GST stripping."""
    if curr is None or curr not in params.fx_rates:
        log_warning(thread_id, "unknown_currency", {"currency": curr}, warnings_path)
        return ParsedOffer(missing_info=True, missing_reason=f"unknown_currency:{curr}")

    # Subscription billing period check
    target_period = params.billing_period
    if target_period is not None:
        if billing_found is None:
            log_warning(
                thread_id,
                "missing_billing_period",
                {"category": params.category.slug, "expected": target_period},
                warnings_path,
            )
            return ParsedOffer(missing_info=True, missing_reason="missing_billing_period")

    price = raw_amt * params.fx_rates[curr]
    if curr != "INR":
        notes.append(f"fx_{curr}*{params.fx_rates[curr]}")

    price, unit_notes = _apply_pack_or_unit_conversion(price, unit_context, params, thread_id, warnings_path)
    notes.extend(unit_notes)

    if target_period == "year" and billing_found == "month":
        price = price * Decimal("12")
        notes.append("period_month_to_year*12")
    elif target_period == "month" and billing_found == "year":
        price = price / Decimal("12")
        notes.append("period_year_to_month/12")

    if gst_included:
        price = price / (Decimal("1") + params.gst_rate)
        notes.append(f"gst_excl/(1+{params.gst_rate})")

    return ParsedOffer(
        missing_info=False,
        unit_price_inr=price,
        currency=curr,
        raw_amount=raw_amt,
        gst_included=gst_included,
        billing_period_found=billing_found,
        pattern_used=pattern_used,
        notes=notes,
    )


def _format_label(action: str, reason: str, counter_price: int | None = None) -> str:
    if action == "COUNTER":
        if counter_price is None:
            raise ValueError("COUNTER action requires counter_price")
        return f"{action}:{reason}:{counter_price}"
    return f"{action}:{reason}:"


def _make_decision(
    thread_id: str,
    action: str,
    reason: str,
    trace: list[dict[str, Any]],
    counter_price: int | None = None,
) -> DecisionResult:
    return DecisionResult(
        thread_id=thread_id,
        action=action,
        reason=reason,
        counter_price=counter_price,
        label=_format_label(action, reason, counter_price),
        trace=trace,
    )


# ---------------------------------------------------------------------------
# Ordered decision steps (1 through 6)
# ---------------------------------------------------------------------------


def step1_missing_info(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult | str:
    """Step 1: Check if latest offer lacks usable price, billing period, or supported currency."""
    if ctx.parsed_offer.missing_info or ctx.parsed_offer.unit_price_inr is None:
        trace.append(
            {
                "step": 1,
                "check": "missing_info",
                "outcome": "ESCALATE:MISSING_INFO:",
                "detail": ctx.parsed_offer.missing_reason,
            }
        )
        return _make_decision(ctx.thread.thread_id, "ESCALATE", "MISSING_INFO", trace)
    trace.append(
        {
            "step": 1,
            "check": "missing_info",
            "outcome": "continue",
            "detail": {
                "pattern": ctx.parsed_offer.pattern_used,
                "p": str(ctx.parsed_offer.unit_price_inr),
                "notes": ctx.parsed_offer.notes,
            },
        }
    )
    return "continue"


def step2_blacklisted_supplier(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult | str:
    """Step 2: Reject if supplier status is BLACKLISTED."""
    status = ctx.params.supplier.status
    if status == "BLACKLISTED":
        trace.append(
            {
                "step": 2,
                "check": "blacklisted_supplier",
                "outcome": "REJECT:BLACKLISTED_SUPPLIER:",
                "detail": f"supplier={ctx.params.supplier.name}",
            }
        )
        return _make_decision(ctx.thread.thread_id, "REJECT", "BLACKLISTED_SUPPLIER", trace)
    trace.append(
        {
            "step": 2,
            "check": "blacklisted_supplier",
            "outcome": "continue",
            "detail": f"status={status}",
        }
    )
    return "continue"


def _scrub_benign_security_phrases(text: str) -> str:
    """Remove ordinary business wording explicitly allowed by security-policy.md."""
    benign = [
        r"\bAI-powered\b",
        r"\bsystem\s+integration\s+team\b",
        r"\bERP\s+system\b",
        r"\bassistant\s+(?:manager|director|vice\s+president|vp|buyer|coordinator)\b",
        r"\b(?:executive|administrative|office|personal)\s+assistant\b",
    ]
    cleaned = text
    for pat in benign:
        cleaned = re.sub(pat, " ", cleaned, flags=re.I)
    return cleaned


def step3_prompt_injection(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult | str:
    """Step 3: Detect prompt injection anywhere in the entire thread."""
    cleaned = _scrub_benign_security_phrases(ctx.thread.supplier_message)
    injection_patterns = [
        (
            "addressed_to_ai_or_reviewer",
            r"\b(?:system\s+note|note|instructions?|message|prompt|command|directive|attention)\s+(?:for|to)\s+(?:the\s+)?(?:ai(?:\s+agent)?|llm|language\s+model|model|bot|assistant|automated\s+system|system|reviewer|agent)\b",
        ),
        (
            "hindi_devanagari_ai_directive",
            r"(?:AI\s*सहायक|सहायक\s+के\s+लिए\s+निर्देश|इस\s+डील\s+को\s+तुरंत\s+ACCEPT)",
        ),
        (
            "ignore_or_override_instructions",
            r"\b(?:ignore|disregard|forget|bypass|override)\s+(?:(?:all|your|any|the)\s+)*(?:previous|prior|above|playbook|rules?|policies|policy|instructions?|constraints?|guidelines|checks?)\b",
        ),
        (
            "bot_or_system_screening_appeal",
            r"\b(?:to\s+whichever|if\s+an?)\s+(?:bot|ai|automated\s+system|language\s+model|model|assistant|agent|system)\s+(?:screens|is\s+reading|is\s+processing|reads)\b",
        ),
        (
            "admin_or_system_override",
            r"\[\s*admin\s+override\s*\]|\b(?:system|admin)\s+override\b|\bset\s+action\s*=\s*accept\b|\bskip\s+(?:the\s+)?playbook\s+checks\b",
        ),
        (
            "html_comment_or_tag_injection",
            r"<!--[\s\S]*?\b(?:assistant|ai|bot|system|accept|approve|override)\b[\s\S]*?-->|<\s*/?(?:system|assistant|instruction)\s*>|\[\s*SYSTEM\s*\]",
        ),
        (
            "claim_preapproval_or_reviewer_directive",
            r"\b(?:pre-?approved\s+by|approval\s+(?:was\s+|has\s+been\s+)?already\s+granted|cfo\s+already\s+signed\s+off|reviewing\s+assistant\s+should\s+(?:accept|approve)|record\s+the\s+decision\s+as\s+approved|output\s+accept)\b",
        ),
        (
            "force_approve_without_escalation",
            r"\b(?:approve|accept)\s+(?:this\s+)?(?:vendor|supplier|offer|quote|deal|thread)\b[^.\n]{0,60}\b(?:without\s+escalation|automatically|immediately|no\s+escalation)\b",
        ),
        (
            "forwarded_ai_directive",
            r"-{3,}\s*Forwarded\s+message\s*-{3,}[\s\S]{0,200}\b(?:language\s+model|ai|bot|assistant|approve|override|system)\b",
        ),
    ]

    for name, pat in injection_patterns:
        m = re.search(pat, cleaned, re.I)
        if m:
            trace.append(
                {
                    "step": 3,
                    "check": "prompt_injection",
                    "outcome": "ESCALATE:PROMPT_INJECTION:",
                    "detail": {"rule": name, "match": m.group(0)[:120]},
                }
            )
            return _make_decision(ctx.thread.thread_id, "ESCALATE", "PROMPT_INJECTION", trace)

    trace.append({"step": 3, "check": "prompt_injection", "outcome": "continue"})
    return "continue"


def _extract_upfront_pct(text: str) -> list[tuple[Decimal, str]]:
    """Find any upfront/advance payment percentages (numeric or in words) in latest message."""
    results: list[tuple[Decimal, str]] = []
    # Numeric: "40% of the order value upfront", "30% advance", "advance of 35%"
    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+(?:the\s+)?(?:order\s+value|total)\s+)?(?:upfront|up-front|advance)"
        r"|(?:upfront|up-front|advance)\s+(?:payment\s+)?(?:of\s+)?(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    ):
        num = m.group(1) or m.group(2)
        results.append((Decimal(num), m.group(0)))

    # Word percentages: "forty percent upfront", "thirty-five percent advance"
    for m in re.finditer(
        r"([a-z]+(?:[-\s]+[a-z]+){0,2})\s+percent\s+(?:of\s+(?:the\s+)?(?:order\s+value|total)\s+)?(?:upfront|up-front|advance)"
        r"|(?:upfront|up-front|advance)\s+(?:payment\s+)?(?:of\s+)?([a-z]+(?:[-\s]+[a-z]+){0,2})\s+percent",
        text,
        re.I,
    ):
        phrase = (m.group(1) or m.group(2)).replace("-", " ")
        val = _words_to_decimal(phrase)
        if val is not None:
            results.append((val, m.group(0)))

    # Special fractional upfront phrases: "half upfront" (50%), "full payment upfront" (100%)
    if re.search(r"\b(?:half|50/50)\b[^.\n]{0,30}\b(?:upfront|advance)\b", text, re.I):
        results.append((Decimal("50"), "half upfront"))
    if re.search(r"\b(?:100%|full\s+(?:payment|amount))\b[^.\n]{0,30}\b(?:upfront|advance)\b", text, re.I):
        results.append((Decimal("100"), "full upfront"))

    return results


def step4_banned_terms(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult | str:
    """Step 4: Detect banned contract terms anywhere in the latest message."""
    msg = ctx.latest_message
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", msg) if s.strip()]

    for sent in sentences:
        # 1. Automatic renewal (ignoring explicit "no automatic renewal" / "renewal only if both parties sign")
        if not re.search(
            r"\bno\s+automatic\s+renewal\b|\bnot\s+auto-?renew|\brenewal\s+only\s+(?:if\s+both\s+parties|by\s+mutual)",
            sent,
            re.I,
        ):
            m_renew = re.search(
                r"\bauto-?renews?\b|\bautomatically\s+renews?\b|\bautomatic\s+renewal\b"
                r"|\bcontinues?\s+(?:from\s+)?year\s+to\s+year\s+unless\b"
                r"|\brenews?\s+on\s+its\s+own\b|\bauto\s+renewal\b",
                sent,
                re.I,
            )
            if m_renew:
                trace.append(
                    {
                        "step": 4,
                        "check": "banned_terms",
                        "outcome": "ESCALATE:BANNED_TERM:",
                        "detail": {"term": "automatic_renewal", "match": sent},
                    }
                )
                return _make_decision(ctx.thread.thread_id, "ESCALATE", "BANNED_TERM", trace)

        # 2. Upfront or advance payment strictly > 30%
        for pct, raw_phrase in _extract_upfront_pct(sent):
            if pct > ctx.params.max_upfront_pct:
                trace.append(
                    {
                        "step": 4,
                        "check": "banned_terms",
                        "outcome": "ESCALATE:BANNED_TERM:",
                        "detail": {"term": "upfront_over_limit", "pct": str(pct), "match": raw_phrase},
                    }
                )
                return _make_decision(ctx.thread.thread_id, "ESCALATE", "BANNED_TERM", trace)

        # 3. Unlimited liability
        if not re.search(r"\bno\s+unlimited\s+liability\b", sent, re.I):
            if re.search(r"\bunlimited\s+liability\b|\bliability\b[^.\n]{0,60}\b(?:shall\s+be\s+|is\s+|remains?\s+)?unlimited\b", sent, re.I):
                trace.append(
                    {
                        "step": 4,
                        "check": "banned_terms",
                        "outcome": "ESCALATE:BANNED_TERM:",
                        "detail": {"term": "unlimited_liability", "match": sent},
                    }
                )
                return _make_decision(ctx.thread.thread_id, "ESCALATE", "BANNED_TERM", trace)

        # 4. Exclusivity (ignoring "not asking for exclusivity", "no exclusivity", "non-exclusive")
        if not re.search(r"\bnot\s+asking\s+for\s+exclusivity\b|\bno\s+exclusivity\b|\bwithout\s+exclusivity\b|\bnon-?exclusive\b", sent, re.I):
            m_excl = re.search(
                r"\bexclusive\s+supplier\b|\bexclusively\s+(?:from|with)\s+us\b|\bbuy\b[^.\n]{0,40}\bonly\s+from\s+(?:us|this\s+supplier)\b|\bsole\s+supplier\b|\bexclusivity\b",
                sent,
                re.I,
            )
            if m_excl:
                trace.append(
                    {
                        "step": 4,
                        "check": "banned_terms",
                        "outcome": "ESCALATE:BANNED_TERM:",
                        "detail": {"term": "exclusivity", "match": sent},
                    }
                )
                return _make_decision(ctx.thread.thread_id, "ESCALATE", "BANNED_TERM", trace)

    trace.append({"step": 4, "check": "banned_terms", "outcome": "continue"})
    return "continue"


def compute_concession_level(params: EffectiveParams, round_num: int) -> Decimal:
    """Compute exact Decimal L(r), respecting PROBATION (always target)."""
    if params.supplier.status == "PROBATION":
        return params.target_price
    r = round_num
    R = params.rounds
    if r <= 1:
        return params.target_price
    if r >= R:
        return params.walkaway_price
    ratio = Decimal(r - 1) / Decimal(R - 1)
    return params.target_price + (params.walkaway_price - params.target_price) * (ratio ** params.beta)


def step5_price_decision(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult | str:
    """Step 5: Compare full-precision p against L(r); accept->step 6, reject on final round, or counter."""
    p = ctx.parsed_offer.unit_price_inr
    assert p is not None

    R = ctx.params.rounds
    r = ctx.thread.round_num
    if r > R:
        log_warning(
            ctx.thread.thread_id,
            "round_exceeds_R",
            {"round": r, "R": R},
            ctx.warnings_path,
        )
        r = R

    L_r = compute_concession_level(ctx.params, r)
    detail = {
        "p": str(p),
        "r": r,
        "R": R,
        "target": str(ctx.params.target_price),
        "walkaway": str(ctx.params.walkaway_price),
        "L_r": str(L_r),
        "probation": ctx.params.supplier.status == "PROBATION",
        "amendments": ctx.params.applied_amendments,
        "contracts": ctx.params.applied_contracts,
    }

    if p <= L_r:
        trace.append({"step": 5, "check": "price_decision", "outcome": "continue", "detail": detail})
        return "continue"

    if r == R:
        trace.append({"step": 5, "check": "price_decision", "outcome": "REJECT:ABOVE_WALKAWAY:", "detail": detail})
        return _make_decision(ctx.thread.thread_id, "REJECT", "ABOVE_WALKAWAY", trace)

    counter_int = int(L_r.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    trace.append(
        {
            "step": 5,
            "check": "price_decision",
            "outcome": f"COUNTER:COUNTER_OFFER:{counter_int}",
            "detail": detail,
        }
    )
    return _make_decision(ctx.thread.thread_id, "COUNTER", "COUNTER_OFFER", trace, counter_price=counter_int)


def step6_approval_limit(ctx: DecisionContext, trace: list[dict[str, Any]]) -> DecisionResult:
    """Step 6: Check if p * quantity exceeds effective approval limit."""
    p = ctx.parsed_offer.unit_price_inr
    assert p is not None
    total_spend = p * Decimal(ctx.thread.quantity)
    limit = ctx.params.approval_limit
    detail = {
        "p": str(p),
        "quantity": ctx.thread.quantity,
        "total_spend": str(total_spend),
        "approval_limit": str(limit),
    }

    if total_spend > limit:
        trace.append(
            {"step": 6, "check": "approval_limit", "outcome": "ESCALATE:OVER_APPROVAL_LIMIT:", "detail": detail}
        )
        return _make_decision(ctx.thread.thread_id, "ESCALATE", "OVER_APPROVAL_LIMIT", trace)

    trace.append({"step": 6, "check": "approval_limit", "outcome": "ACCEPT:ACCEPTABLE_PRICE:", "detail": detail})
    return _make_decision(ctx.thread.thread_id, "ACCEPT", "ACCEPTABLE_PRICE", trace)


def evaluate_thread(
    thread: ThreadRow,
    playbook: Playbook,
    warnings_path: Path = DEFAULT_WARNINGS_PATH,
) -> DecisionResult:
    """Run the ordered decision procedure (Steps 1-6) on a single thread."""
    params = get_effective_params(playbook, thread.category, thread.supplier, thread.thread_date)
    segments, latest_message, supplier_earlier = split_thread_messages(thread.supplier_message)
    parsed_offer = extract_and_normalize_offer(
        latest_message,
        params,
        thread.quantity,
        supplier_earlier,
        thread.thread_id,
        warnings_path,
    )

    ctx = DecisionContext(
        thread=thread,
        params=params,
        segments=segments,
        latest_message=latest_message,
        supplier_earlier_messages=supplier_earlier,
        parsed_offer=parsed_offer,
        warnings_path=warnings_path,
    )

    trace: list[dict[str, Any]] = []
    for fn in (
        step1_missing_info,
        step2_blacklisted_supplier,
        step3_prompt_injection,
        step4_banned_terms,
        step5_price_decision,
    ):
        res = fn(ctx, trace)
        if isinstance(res, DecisionResult):
            return res

    return step6_approval_limit(ctx, trace)
