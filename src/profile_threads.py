"""Profile all 2,000 rows of threads.csv and write a detailed report to PROFILE.md."""

from __future__ import annotations

import re
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load import get_effective_params, load_playbook, load_threads  # noqa: E402
from rules import (  # noqa: E402
    _detect_billing_period,
    compute_concession_level,
    extract_and_normalize_offer,
    split_thread_messages,
)

SUBSCRIPTION_CATEGORIES = {
    "cleaning",
    "security-guards",
    "cloud-servers",
    "crm-licenses",
    "design-software",
}

EXPECTED_PUNCT_OR_SYMBOLS = {
    "₹",
    "—",
    "–",
    "’",
    "‘",
    "“",
    "”",
    "…",
    "€",
    "£",
    "×",
    "÷",
    "°",
    "•",
    "\u00a0",
}

WORD_NUM_RE = re.compile(
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakh|lakhs|"
    r"lac|lacs|crore|crores|million|half|quarter)\b",
    re.I,
)


def _add_example(
    buckets: dict[str, list[tuple[str, str]]],
    counts: Counter[str],
    key: str,
    tid: str,
    snippet: str,
    max_examples: int = 3,
) -> None:
    counts[key] += 1
    ex_list = buckets[key]
    if len(ex_list) < max_examples and all(existing_tid != tid for existing_tid, _ in ex_list):
        clean_snip = " ".join(snippet.strip().split())
        if len(clean_snip) > 180:
            clean_snip = clean_snip[:177] + "..."
        ex_list.append((tid, clean_snip))


def _normalize_template(line: str) -> str:
    """Normalize numbers, currencies, dates, and supplier names to group similar sentence templates."""
    s = line.strip()
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", s)
    s = re.sub(r"(?:(?:₹|Rs\.?|INR|USD|US\$|\$|EUR|€|GBP|£|\b[A-Z]{3})\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|lacs?|crores?|k\b))?)", "<PRICE>", s, flags=re.I)
    s = re.sub(r"\b\d+(?:\.\d+)?%", "<PCT>", s)
    s = re.sub(r"\b\d[\d,]*(?:\.\d+)?\b", "<NUM>", s)
    return s


def _script_bucket(ch: str) -> str:
    name = unicodedata.name(ch, "UNKNOWN")
    if "DEVANAGARI" in name:
        return "Devanagari"
    if "ARABIC" in name:
        return "Arabic"
    if "CJK" in name or "HIRAGANA" in name or "KATAKANA" in name or "HANGUL" in name:
        return "CJK/Japanese/Korean"
    if "CYRILLIC" in name:
        return "Cyrillic"
    if "LATIN" in name:
        return "Accented Latin"
    return name.split()[0] if name else "Other"


def _fmt_examples(ex_list: list[tuple[str, str]]) -> str:
    if not ex_list:
        return "—"
    return "; ".join(f"`{tid}` (`{snip.replace('`', chr(39))}`)" for tid, snip in ex_list)


def main() -> None:
    playbook = load_playbook()
    sample_threads = load_threads("sample_threads.csv")
    threads = load_threads("threads.csv")

    # Collect sample normalized line templates for comparison in Section 10
    sample_templates: set[str] = set()
    for st in sample_threads:
        for line in st.supplier_message.splitlines():
            if line.strip():
                sample_templates.add(_normalize_template(line))

    # Tracking containers
    curr_counts: Counter[str] = Counter()
    curr_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    curr_latest_counts: Counter[str] = Counter()
    curr_latest_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    price_style_counts: Counter[str] = Counter()
    price_style_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    missing_info_counts: Counter[str] = Counter()
    missing_info_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    unit_counts: Counter[str] = Counter()
    unit_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    per_phrase_counts: Counter[str] = Counter()
    per_phrase_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    sub_no_period_counts: Counter[str] = Counter()
    sub_no_period_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    non_ascii_counts: Counter[str] = Counter()
    non_ascii_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    non_english_counts: Counter[str] = Counter()
    non_english_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    inj_benign_counts: Counter[str] = Counter()
    inj_benign_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    inj_suspect_counts: Counter[str] = Counter()
    inj_suspect_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    banned_latest_counts: Counter[str] = Counter()
    banned_latest_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    banned_earlier_counts: Counter[str] = Counter()
    banned_earlier_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    meta_counts: Counter[str] = Counter()
    meta_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    thread_fmt_counts: Counter[str] = Counter()
    thread_fmt_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    header_variants: Counter[str] = Counter()
    header_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)
    msg_count_dist: Counter[int] = Counter()

    unseen_templates: Counter[str] = Counter()
    unseen_template_examples: dict[str, list[tuple[str, str]]] = defaultdict(list)

    # Regexes
    curr_token_re = re.compile(
        r"(₹|Rs\.?|\$|€|£|¥|₩|"
        r"\b(?:INR|USD|US\$|EUR|GBP|AED|AUD|CAD|CHF|CNY|RMB|SGD|JPY|HKD|NZD|SAR|QAR|MYR|THB|SEK|NOK|DKK|ZAR|KRW|RUB|BRL|MXN|PKR|BDT|LKR|NPR)\b|"
        r"\b(?:[Rr]upees?|[Dd]ollars?|[Ee]uros?|[Pp]ounds?|[Yy]en|[Dd]irhams?|[Ff]rancs?|[Yy]uan)\b)"
    )
    any_3letter_curr_re = re.compile(r"\b([A-Z]{3})\s+[\d,]+(?:\.\d+)?|\b[\d,]+(?:\.\d+)?\s+([A-Z]{3})\b")
    ignore_3letter = {"GST", "ERP", "CRM", "LED", "USD", "EUR", "GBP", "INR", "AND", "FOR", "THE", "ALL", "PER", "NOT", "OFF", "ONE", "TWO", "TEN", "MAX", "MIN", "REF"}

    non_english_re = re.compile(
        r"\b(?:hola|gracias|por\s+favor|precio|descuento|aprobar|ignorar|instrucciones|idioma|modelo|"
        r"bonjour|merci|prix|remise|approuver|ignorer|modèle|"
        r"hallo|danke|preis|rabatt|genehmigen|ignorieren|anweisung|"
        r"namaste|kripya|dhanyavad|krupa)\b",
        re.I,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_warn = Path(tmpdir) / "profile_warnings.jsonl"

        for th in tqdm(threads, desc="Profiling threads.csv", unit="thread"):
            tid = th.thread_id
            full_msg = th.supplier_message
            segments, latest_msg, supplier_earlier = split_thread_messages(full_msg)

            # --- 8. Metadata checks ---
            cat_exists = th.category.lower() in playbook.categories or th.category.lower() in playbook.name_to_slug
            sup_exists = th.supplier in playbook.suppliers or th.supplier.lower() in playbook.suppliers
            if not cat_exists:
                _add_example(meta_examples, meta_counts, f"unknown_category:{th.category}", tid, th.category)
            if not sup_exists:
                _add_example(meta_examples, meta_counts, f"unknown_supplier:{th.supplier}", tid, th.supplier)

            if cat_exists and sup_exists:
                params = get_effective_params(playbook, th.category, th.supplier, th.thread_date)
                if th.round_num > params.rounds:
                    _add_example(
                        meta_examples,
                        meta_counts,
                        f"round_gt_R ({th.category}: r={th.round_num} > R={params.rounds})",
                        tid,
                        f"cat={th.category}, r={th.round_num}, R={params.rounds}, olc={th.our_last_counter}",
                    )
                if th.round_num < 1:
                    _add_example(meta_examples, meta_counts, f"round_lt_1 (r={th.round_num})", tid, f"r={th.round_num}")
                if th.round_num > 1 and th.our_last_counter is None:
                    _add_example(meta_examples, meta_counts, "missing_our_last_counter_when_r>1", tid, f"r={th.round_num}")
                if th.round_num == 1 and th.our_last_counter is not None:
                    _add_example(meta_examples, meta_counts, "present_our_last_counter_when_r==1", tid, f"olc={th.our_last_counter}")
                if th.round_num > 1 and th.our_last_counter is not None:
                    prev_r = min(th.round_num - 1, params.rounds)
                    expected_olc = compute_concession_level(params, prev_r).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    if expected_olc != th.our_last_counter:
                        _add_example(
                            meta_examples,
                            meta_counts,
                            "our_last_counter_differs_from_L(r-1)",
                            tid,
                            f"r={th.round_num}, R={params.rounds}, olc={th.our_last_counter}, expected_L({prev_r})={expected_olc}",
                        )

                # --- Run current extractor (with isolated temp warnings file) ---
                parsed = extract_and_normalize_offer(
                    latest_msg,
                    params,
                    th.quantity,
                    supplier_earlier,
                    tid,
                    tmp_warn,
                )
                if parsed.missing_info:
                    _add_example(
                        missing_info_examples,
                        missing_info_counts,
                        parsed.missing_reason or "unknown_missing",
                        tid,
                        latest_msg,
                    )

            # --- 1. Currencies ---
            for scope_name, text_scope, cnt_map, ex_map in [
                ("full", full_msg, curr_counts, curr_examples),
                ("latest", latest_msg, curr_latest_counts, curr_latest_examples),
            ]:
                seen_in_thread: set[str] = set()
                for m in curr_token_re.finditer(text_scope):
                    tok = m.group(1)
                    norm_tok = tok.upper() if tok.isalpha() and len(tok) == 3 else tok
                    if norm_tok.lower().startswith("rs"):
                        norm_tok = "Rs."
                    if norm_tok not in seen_in_thread:
                        seen_in_thread.add(norm_tok)
                        # grab line
                        line = text_scope[max(0, text_scope.rfind("\n", 0, m.start()) + 1) : text_scope.find("\n", m.end()) if "\n" in text_scope[m.end():] else len(text_scope)]
                        _add_example(ex_map, cnt_map, norm_tok, tid, line)
                for m in any_3letter_curr_re.finditer(text_scope):
                    code = (m.group(1) or m.group(2) or "").upper()
                    if code and code not in ignore_3letter and code not in seen_in_thread:
                        seen_in_thread.add(f"3LTR:{code}")
                        _add_example(ex_map, cnt_map, f"3LTR:{code}", tid, m.group(0))

            # --- 2. Price styles (latest message) ---
            price_checks = [
                ("lakh_crore", r"\b(?:lakhs?|lacs?|crores?)\b"),
                ("k_suffix", r"\b\d+(?:\.\d+)?k\b"),
                ("words_number_in_latest", WORD_NUM_RE.pattern),
                ("no_digits_in_latest", None),
                ("range_between_and", r"\bbetween\s+[^\n]+?\band\b"),
                ("range_from_to_or_dash", r"\bfrom\s+(?:₹|Rs\.?|INR|USD|EUR|GBP|\$|€|£)\s*[\d,.]+\s+to\b|(?:₹|Rs\.?|INR|USD|EUR|GBP|\$|€|£)\s*[\d,.]+\s*[-–]\s*(?:₹|Rs\.?|INR|USD|EUR|GBP|\$|€|£)?\s*[\d,.]+"),
                ("total_for_all_units", r"\bin\s+total\s+for\b|\btotal\s+of\b|\blump\s*sum\b|\bfor\s+all\s+\d+\b|\bentire\s+(?:batch|order|lot)\b"),
                ("gst_included", r"\b(?:including|inclusive\s+of|incl\.?|with)\s+gst\b|\bgst\s+included\b|\bgst\s+incl\.?"),
                ("gst_excluded", r"\b(?:excluding|excl\.?|exclusive\s+of|plus|without|before)\s+gst\b|\+\s*gst\b|\bgst\s+extra\b"),
                ("tiered_quantity", r"\bfor\s+orders?\s+of\s+up\s+to\b|\bfor\s+larger\s+orders\b|\btier\b"),
                ("volume_discount_threshold", r"\bvolume\s+discount\b|\borders?\s+of\s+(?:at\s+least|\d+\s+or\s+more|over|above)\b|\b\d+\+\s+(?:units?|meals?|seats?|laptops?|monitors?|phones?|headsets?|chairs?|boxes?|fixtures?|participants?|guards?|sites?|shipments?|servers?)\b|\bif\s+you\s+order\b"),
                ("free_units_bonus", r"\bplus\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+free\b|\bbuy\s+\d+\s+get\b|\bfree\s+\w+\s+for\s+every\b"),
                ("relative_discount_prev_quote", r"\boff\s+our\s+(?:previous|earlier|last)\s+quote\b|\bdiscount\s+on\s+our\s+(?:previous|earlier)\b|\breduce\s+our\s+(?:previous|earlier)\b"),
                ("correction", r"\bCORRECTION\b|\bshould\s+read\b|\bmeant\s+to\s+quote\b|\btypo\b"),
            ]
            for label, pat in price_checks:
                if pat is None:
                    if not re.search(r"\d", latest_msg):
                        _add_example(price_style_examples, price_style_counts, label, tid, latest_msg)
                else:
                    m = re.search(pat, latest_msg, re.I)
                    if m:
                        # grab containing line
                        start_l = latest_msg.rfind("\n", 0, m.start()) + 1
                        end_l = latest_msg.find("\n", m.end())
                        line = latest_msg[start_l : (end_l if end_l != -1 else len(latest_msg))]
                        # avoid counting "one-year" or "on-site" noise for words_number unless not just "1 year" warranty / "thirty days" validity
                        if label == "words_number_in_latest":
                            # filter out lines that are only "Prices valid for thirty days" or "one year warranty" if possible, still keep track separately
                            non_offer_words = [w for w in WORD_NUM_RE.findall(line) if w.lower() not in {"one"}]
                            if not non_offer_words and "warranty" in line.lower():
                                continue
                        _add_example(price_style_examples, price_style_counts, label, tid, line)

            # --- 3. Units & Billing Periods ---
            unit_pats = [
                ("pack", r"\bpacks?\b"),
                ("box", r"\bbox(?:es)?\b"),
                ("ream", r"\breams?\b"),
                ("dozen", r"\bdozens?\b"),
                ("pair", r"\bpairs?\b"),
                ("carton", r"\bcartons?\b"),
                ("case", r"\bcases?\b"),
                ("bundle", r"\bbundles?\b"),
                ("per_month_monthly", r"\bper\s+month\b|\bmonthly\b|/\s*month\b|/\s*mo\b|\bp\.m\.\b"),
                ("per_year_yearly_annual", r"\bper\s+year\b|\byearly\b|\bannual(?:ly)?\b|\bper\s+annum\b|/\s*year\b|/\s*yr\b|\bp\.a\.\b"),
                ("per_quarter_quarterly", r"\bper\s+quarter\b|\bquarterly\b|/\s*quarter\b|\bqtr\b"),
                ("per_week_weekly", r"\bper\s+week\b|\bweekly\b|/\s*week\b"),
                ("per_day_daily", r"\bper\s+day\b|\bdaily\b|/\s*day\b"),
                ("per_hour_hourly", r"\bper\s+hour\b|\bhourly\b|/\s*hr\b|/\s*hour\b"),
            ]
            for ulabel, upat in unit_pats:
                m = re.search(upat, latest_msg, re.I)
                if m:
                    start_l = latest_msg.rfind("\n", 0, m.start()) + 1
                    end_l = latest_msg.find("\n", m.end())
                    line = latest_msg[start_l : (end_l if end_l != -1 else len(latest_msg))]
                    _add_example(unit_examples, unit_counts, ulabel, tid, line)

            for m in re.finditer(r"\bper\s+([a-z]+(?:\s+per\s+[a-z]+)?)", latest_msg, re.I):
                phrase = m.group(0).lower()
                _add_example(per_phrase_examples, per_phrase_counts, phrase, tid, latest_msg[max(0, m.start()-30):m.end()+30])

            # --- 4. Subscription categories with NO billing period ---
            if th.category in SUBSCRIPTION_CATEGORIES:
                bp_latest = _detect_billing_period(latest_msg)
                bp_earlier = _detect_billing_period(supplier_earlier[-1]) if supplier_earlier else None
                has_rel = bool(re.search(r"\boff\s+our\s+(?:previous|earlier)\s+quote\b", latest_msg, re.I))
                effective_bp = bp_latest or (bp_earlier if has_rel else None)
                if effective_bp is None:
                    # Check if there is any non-month/year time unit
                    other_t = re.search(r"\b(?:quarter(?:ly)?|week(?:ly)?|day|daily|hour(?:ly)?)\b", latest_msg, re.I)
                    reason_key = f"{th.category}:non_std({other_t.group(0)})" if other_t else f"{th.category}:no_billing_period"
                    _add_example(sub_no_period_examples, sub_no_period_counts, reason_key, tid, latest_msg)

            # --- 5. Non-English / Non-ASCII ---
            other_non_ascii = [ch for ch in full_msg if ord(ch) > 127 and ch not in EXPECTED_PUNCT_OR_SYMBOLS]
            if other_non_ascii:
                buckets_seen = sorted({_script_bucket(ch) for ch in other_non_ascii})
                chars_preview = "".join(sorted(set(other_non_ascii)))[:30]
                for b in buckets_seen:
                    _add_example(
                        non_ascii_examples,
                        non_ascii_counts,
                        f"{b} ({chars_preview})",
                        tid,
                        next((l for l in full_msg.splitlines() if any(ord(c) > 127 and c not in EXPECTED_PUNCT_OR_SYMBOLS for c in l)), full_msg),
                    )
            m_ne = non_english_re.search(full_msg)
            if m_ne:
                _add_example(non_english_examples, non_english_counts, f"word:{m_ne.group(0).lower()}", tid, full_msg[max(0, m_ne.start()-40):m_ne.end()+80])

            # --- 6. Injection-like phrases (anywhere in thread) ---
            inj_kw = re.compile(
                r"\b(?:ai|llm|language\s+model|model|assistant|bot|automated|system|reviewer|agent|"
                r"ignore|disregard|forget|bypass|override|pre-?approved|already\s+approved|"
                r"without\s+escalation|do\s+not\s+escalate|approve|prompt|instruction|directive|"
                r"note\s+for|note\s+to|forwarded\s+message)\b|\[system|<system",
                re.I,
            )
            benign_line_re = re.compile(
                r"^\s*(?:Our\s+AI-powered|Our\s+system\s+integration|The\s+invoice\s+will\s+be\s+pushed\s+straight\s+into\s+your\s+ERP\s+system|"
                r"Our\s+assistant\s+manager|.*?assistant\s+(?:manager|director)|.*?ERP\s+system)\b",
                re.I,
            )
            for line in full_msg.splitlines():
                ls = line.strip()
                if not ls:
                    continue
                if inj_kw.search(ls):
                    if benign_line_re.search(ls):
                        _add_example(inj_benign_examples, inj_benign_counts, _normalize_template(ls), tid, ls)
                    else:
                        _add_example(inj_suspect_examples, inj_suspect_counts, _normalize_template(ls), tid, ls)

            # --- 7. Banned-term phrases (latest vs earlier) ---
            banned_kw = re.compile(
                r"\b(?:renew\w*|auto-?renew\w*|year\s+to\s+year|evergreen|unless\s+cancelled|mutual\s+agreement|"
                r"upfront|up-front|advance|deposit|prepay\w*|half|50/50|"
                r"liabilit\w*|liable|indemnit\w*|indemnif\w*|"
                r"exclusiv\w*|sole\s+supplier|only\s+from)\b",
                re.I,
            )
            for line in latest_msg.splitlines():
                ls = line.strip()
                if ls and banned_kw.search(ls):
                    _add_example(banned_latest_examples, banned_latest_counts, _normalize_template(ls), tid, ls)
            for ebody in supplier_earlier:
                for line in ebody.splitlines():
                    ls = line.strip()
                    if ls and banned_kw.search(ls):
                        _add_example(banned_earlier_examples, banned_earlier_counts, _normalize_template(ls), tid, ls)

            # --- 9. Thread formatting & headers ---
            msg_count_dist[len(segments)] += 1
            if len(segments) == 1 and segments[0].kind == "Single":
                _add_example(thread_fmt_examples, thread_fmt_counts, "single_message_no_headers", tid, latest_msg.splitlines()[0] if latest_msg.splitlines() else "")
            else:
                _add_example(thread_fmt_examples, thread_fmt_counts, f"multi_message_bracketed ({len(segments)} segments)", tid, full_msg.splitlines()[0])

            for line in full_msg.splitlines():
                ls = line.strip()
                if re.match(r"^\[.+\]$", ls):
                    norm_h = re.sub(r"—\s*[^,\]]+(?:,\s*\d{4}-\d{2}-\d{2})?", "— <SENDER/DATE>", ls)
                    _add_example(header_examples, header_variants, f"bracket:{norm_h}", tid, ls)
                elif re.match(r"^(?:-{3,}|={3,}|From:|Sent:|Subject:|On\s+.+\s+wrote:|>)", ls, re.I):
                    _add_example(header_examples, header_variants, f"other_marker:{_normalize_template(ls)}", tid, ls)

            # --- 10. Unseen line templates vs sample_threads.csv ---
            for line in full_msg.splitlines():
                ls = line.strip()
                if not ls:
                    continue
                if re.match(r"^\[(?:Earlier|Latest)\s+message\s*[—\-]", ls):
                    continue
                if re.match(r"^(?:Dear Acme Procurement,|Hi team,|Hi Priya,|Good morning,|Hello,|Namaste team,|Regards,|Warm regards,|Thanks and regards,|Cheers,|Best,|Our quote is|Our earlier quote was|We can offer|We can do )", ls):
                    # Skip pure boilerplate unless it has non-sample normalized structure
                    pass
                tmpl = _normalize_template(ls)
                if tmpl not in sample_templates:
                    _add_example(unseen_template_examples, unseen_templates, tmpl, tid, ls)

    # Write PROFILE.md
    lines: list[str] = []
    lines.append("# Profile of `threads.csv` (All 2,000 Rows)")
    lines.append("")
    lines.append(f"- Total threads scanned: **{len(threads)}**")
    lines.append(f"- Categories in playbook: **{len(playbook.categories)}** | Suppliers in playbook: **{len(playbook.suppliers) // 2}**")
    lines.append("")

    def add_section(title: str, counts: Counter[str], examples: dict[str, list[tuple[str, str]]], limit: int | None = None) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not counts:
            lines.append("_None found._")
            lines.append("")
            return
        lines.append("| Pattern / Value | Count | 3 Example Thread IDs & Snippets |")
        lines.append("|---|---:|---|")
        items = counts.most_common(limit)
        for key, cnt in items:
            safe_key = key.replace("|", "\\|").replace("`", "'")
            ex_str = _fmt_examples(examples[key]).replace("|", "\\|")
            lines.append(f"| `{safe_key}` | {cnt} | {ex_str} |")
        lines.append("")

    add_section("1a. Currencies & Symbols Mentioned (Full Thread)", curr_counts, curr_examples)
    add_section("1b. Currencies & Symbols Mentioned (Latest Message Only)", curr_latest_counts, curr_latest_examples)
    add_section("2a. Price Styles in Latest Message", price_style_counts, price_style_examples)
    add_section("2b. Current `rules.py` Extractor Missing-Info Breakdown", missing_info_counts, missing_info_examples)
    add_section("3a. Unit & Billing Period Keywords (Latest Message)", unit_counts, unit_examples)
    add_section("3b. All Distinct `per <unit>` Phrases (Latest Message)", per_phrase_counts, per_phrase_examples)
    add_section("4. Subscription Categories with NO Standard Billing Period", sub_no_period_counts, sub_no_period_examples)
    add_section("5a. Non-ASCII Scripts / Unexpected Characters (Full Thread)", non_ascii_counts, non_ascii_examples)
    add_section("5b. Non-English Words Detected (Full Thread)", non_english_counts, non_english_examples)
    add_section("6a. Suspect / Actual Injection-Like Lines (Full Thread)", inj_suspect_counts, inj_suspect_examples)
    add_section("6b. Benign Lines Containing Security Keywords (Full Thread)", inj_benign_counts, inj_benign_examples)
    add_section("7a. Banned-Term Keyword Lines in Latest Message", banned_latest_counts, banned_latest_examples)
    add_section("7b. Banned-Term Keyword Lines in Supplier Earlier Messages", banned_earlier_counts, banned_earlier_examples)
    add_section("8. Metadata Checks (`round > R`, Unknown Suppliers/Categories, `our_last_counter`)", meta_counts, meta_examples)
    add_section("9a. Thread Structure Distribution", thread_fmt_counts, thread_fmt_examples)
    add_section("9b. Header & Marker Variants", header_variants, header_examples)

    lines.append("## 9c. Segment Count Distribution")
    lines.append("")
    lines.append("| Segment Count | Threads |")
    lines.append("|---:|---:|")
    for seg_n, cnt in sorted(msg_count_dist.items()):
        lines.append(f"| {seg_n} | {cnt} |")
    lines.append("")

    add_section("10. Normalized Line Templates Not Seen in `sample_threads.csv`", unseen_templates, unseen_template_examples, limit=100)

    out_path = Path("PROFILE.md")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(lines)} lines).")
    print(f"Missing-info summary under current extractor: {dict(missing_info_counts)}")
    print(f"Metadata anomalies: {dict(meta_counts)}")
    print(f"Non-ASCII buckets: {dict(non_ascii_counts)}")
    print(f"Non-English words: {dict(non_english_counts)}")
    print(f"Subscription no-period: {dict(sub_no_period_counts)}")
    print(f"Suspect injection templates: {len(inj_suspect_counts)} | Unseen line templates: {len(unseen_templates)}")


if __name__ == "__main__":
    main()
