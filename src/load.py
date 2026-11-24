"""Load playbook rules, supplier contracts, amendments, precedents, and CSV threads."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tqdm import tqdm


@dataclass(frozen=True)
class PackConversion:
    quoted_unit: str
    multiplier: Decimal
    description: str


@dataclass(frozen=True)
class CategorySpec:
    slug: str
    name: str
    normalized_unit: str
    billing_period: str | None  # 'month', 'year', or None
    gst_rate: Decimal
    target_price: Decimal
    walkaway_price: Decimal
    rounds: int
    beta: int
    approval_limit: Decimal
    pack_conversion: PackConversion | None = None


@dataclass(frozen=True)
class Amendment:
    amd_id: str
    category_slug: str
    effective_from: date
    field_name: str  # 'target_price', 'walkaway_price', or 'approval_limit'
    new_value: Decimal
    raw_text: str


@dataclass(frozen=True)
class SupplierSpec:
    name: str
    slug: str
    status: str  # 'Approved', 'BLACKLISTED', 'PROBATION'
    price_caps: dict[str, Decimal] = field(default_factory=dict)
    fx_rates: dict[str, Decimal] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class Precedent:
    precedent_id: str
    category_name: str
    body: str


@dataclass
class Playbook:
    categories: dict[str, CategorySpec]
    name_to_slug: dict[str, str]
    amendments: list[Amendment]
    suppliers: dict[str, SupplierSpec]
    precedents: list[Precedent]
    base_fx_rates: dict[str, Decimal]
    dozen_size: Decimal
    max_upfront_pct: Decimal


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    thread_date: date
    supplier: str
    category: str
    quantity: int
    round_num: int
    our_last_counter: Decimal | None
    supplier_message: str


@dataclass
class EffectiveParams:
    category: CategorySpec
    supplier: SupplierSpec
    thread_date: date
    target_price: Decimal
    walkaway_price: Decimal
    approval_limit: Decimal
    rounds: int
    beta: int
    gst_rate: Decimal
    billing_period: str | None
    pack_conversion: PackConversion | None
    fx_rates: dict[str, Decimal]
    dozen_size: Decimal
    max_upfront_pct: Decimal
    applied_amendments: list[str]
    applied_contracts: list[str]


def _parse_decimal(num_str: str) -> Decimal:
    """Parse an Indian/Western formatted number string into Decimal."""
    return Decimal(num_str.replace(",", "").strip())


def _singularize(word: str) -> str:
    w = word.strip().lower()
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _parse_policies(policies_dir: Path) -> tuple[dict[str, Decimal], Decimal, Decimal]:
    """Extract currency FX table, dozen size, and max allowed upfront percentage from policies/."""
    units_text = (policies_dir / "units-and-currency.md").read_text(encoding="utf-8")
    fx_rates: dict[str, Decimal] = {"INR": Decimal("1")}
    for match in re.finditer(r"^\|\s*([A-Z]{3})\s*\|\s*([\d,]+(?:\.\d+)?)\s*\|", units_text, re.M):
        fx_rates[match.group(1)] = _parse_decimal(match.group(2))

    dozen_match = re.search(r"A\s+dozen\s+is\s+(\d+)\s+units", units_text, re.I)
    if not dozen_match:
        raise ValueError("Could not parse dozen conversion from units-and-currency.md")
    dozen_size = Decimal(dozen_match.group(1))

    banned_text = (policies_dir / "banned-terms.md").read_text(encoding="utf-8")
    upfront_match = re.search(
        r"Upfront\s+or\s+advance\s+payment\s+above\s+(\d+(?:\.\d+)?)%",
        banned_text,
        re.I,
    )
    if not upfront_match:
        raise ValueError("Could not parse upfront payment limit from banned-terms.md")
    max_upfront_pct = Decimal(upfront_match.group(1))

    return fx_rates, dozen_size, max_upfront_pct


def _parse_pack_conversion(line: str, normalized_unit: str) -> PackConversion | None:
    """Parse pack conversion rules relative to the category's normalized unit."""
    norm_lower = normalized_unit.lower()
    # Pattern 1: "a pack holds 4 fixtures."
    m_holds = re.search(r"\ba\s+(\w+)\s+holds\s+(\d+)\s+(\w+)", line, re.I)
    if m_holds:
        outer = _singularize(m_holds.group(1))
        count = Decimal(m_holds.group(2))
        inner = _singularize(m_holds.group(3))
        if inner in norm_lower:
            return PackConversion(
                quoted_unit=outer,
                multiplier=Decimal("1") / count,
                description=line.strip(),
            )
        if outer in norm_lower:
            return PackConversion(
                quoted_unit=inner,
                multiplier=count,
                description=line.strip(),
            )

    # Pattern 2: "one box = 10 reams."
    m_eq = re.search(r"\b(?:one|1)\s+(\w+)\s*=\s*(\d+)\s+(\w+)", line, re.I)
    if m_eq:
        outer = _singularize(m_eq.group(1))
        count = Decimal(m_eq.group(2))
        inner = _singularize(m_eq.group(3))
        if outer in norm_lower:
            return PackConversion(
                quoted_unit=inner,
                multiplier=count,
                description=line.strip(),
            )
        if inner in norm_lower:
            return PackConversion(
                quoted_unit=outer,
                multiplier=Decimal("1") / count,
                description=line.strip(),
            )
    raise ValueError(f"Unrecognized pack conversion line: {line}")


def _parse_category(path: Path) -> CategorySpec:
    text = path.read_text(encoding="utf-8")
    slug = path.stem
    name_m = re.search(r"^#\s*Category:\s*(.+)$", text, re.M)
    if not name_m:
        raise ValueError(f"Missing category header in {path}")
    name = name_m.group(1).strip()

    unit_m = re.search(r"\*\*Normalized unit:\*\*\s*(.+)$", text, re.M)
    gst_m = re.search(r"\*\*GST rate:\*\*\s*(\d+(?:\.\d+)?)%", text, re.M)
    target_m = re.search(r"\*\*Target price:\*\*\s*₹([\d,]+(?:\.\d+)?)", text, re.M)
    walk_m = re.search(r"\*\*Walk-away price:\*\*\s*₹([\d,]+(?:\.\d+)?)", text, re.M)
    rounds_m = re.search(r"\*\*Negotiation rounds \(R\):\*\*\s*(\d+)", text, re.M)
    beta_m = re.search(r"\*\*Concession exponent \(β\):\*\*\s*(\d+)", text, re.M)
    limit_m = re.search(r"\*\*Approval limit:\*\*\s*₹([\d,]+(?:\.\d+)?)", text, re.M)

    if not all([unit_m, gst_m, target_m, walk_m, rounds_m, beta_m, limit_m]):
        raise ValueError(f"Missing required fields in category file {path}")

    normalized_unit = unit_m.group(1).strip()
    if "per month" in normalized_unit.lower():
        billing_period: str | None = "month"
    elif "per year" in normalized_unit.lower():
        billing_period = "year"
    else:
        billing_period = None

    pack_m = re.search(r"\*\*Pack conversion:\*\*\s*(.+)$", text, re.M)
    pack_conv = _parse_pack_conversion(pack_m.group(1), normalized_unit) if pack_m else None

    return CategorySpec(
        slug=slug,
        name=name,
        normalized_unit=normalized_unit,
        billing_period=billing_period,
        gst_rate=_parse_decimal(gst_m.group(1)) / Decimal("100"),
        target_price=_parse_decimal(target_m.group(1)),
        walkaway_price=_parse_decimal(walk_m.group(1)),
        rounds=int(rounds_m.group(1)),
        beta=int(beta_m.group(1)),
        approval_limit=_parse_decimal(limit_m.group(1)),
        pack_conversion=pack_conv,
    )


def _resolve_category_slug(cat_ref: str, name_to_slug: dict[str, str]) -> str:
    key = cat_ref.strip().lower()
    if key in name_to_slug:
        return name_to_slug[key]
    slug_guess = key.replace(" ", "-")
    if slug_guess in name_to_slug:
        return name_to_slug[slug_guess]
    raise ValueError(f"Unknown category reference in playbook: {cat_ref!r}")


def _parse_amendment(path: Path, name_to_slug: dict[str, str]) -> Amendment:
    text = path.read_text(encoding="utf-8")
    hdr_m = re.search(r"^#\s*(AMD-\d+):\s*(.+)$", text, re.M)
    eff_m = re.search(r"\*\*Effective from:\*\*\s*(\d{4}-\d{2}-\d{2})", text, re.M)
    chg_m = re.search(r"\*\*Change:\*\*\s*(.+)$", text, re.M)
    if not all([hdr_m, eff_m, chg_m]):
        raise ValueError(f"Malformed amendment file {path}")

    amd_id = hdr_m.group(1).strip()
    cat_name = hdr_m.group(2).strip()
    effective_from = date.fromisoformat(eff_m.group(1).strip())
    change_text = chg_m.group(1).strip()

    body_m = re.search(
        r"the\s+(target price|walk-away price|approval limit)\s+for\s+(.+?)\s+is\s+now\s+₹([\d,]+(?:\.\d+)?)",
        change_text,
        re.I,
    )
    if not body_m:
        raise ValueError(f"Unrecognized amendment change text in {path}: {change_text}")

    raw_field = body_m.group(1).lower()
    field_map = {
        "target price": "target_price",
        "walk-away price": "walkaway_price",
        "approval limit": "approval_limit",
    }
    cat_slug = _resolve_category_slug(body_m.group(2), name_to_slug)
    if cat_slug != _resolve_category_slug(cat_name, name_to_slug):
        raise ValueError(f"Category mismatch in {path}: {cat_name} vs {body_m.group(2)}")

    return Amendment(
        amd_id=amd_id,
        category_slug=cat_slug,
        effective_from=effective_from,
        field_name=field_map[raw_field],
        new_value=_parse_decimal(body_m.group(3)),
        raw_text=change_text,
    )


def _parse_supplier(path: Path, name_to_slug: dict[str, str]) -> SupplierSpec:
    text = path.read_text(encoding="utf-8")
    name_m = re.search(r"^#\s*Supplier:\s*(.+)$", text, re.M)
    status_m = re.search(r"\*\*Status:\*\*\s*(.+)$", text, re.M)
    if not name_m or not status_m:
        raise ValueError(f"Malformed supplier file {path}")

    name = name_m.group(1).strip()
    raw_status = status_m.group(1).strip()
    upper_status = raw_status.upper()
    if upper_status.startswith("BLACKLISTED"):
        status = "BLACKLISTED"
    elif upper_status.startswith("PROBATION"):
        status = "PROBATION"
    elif upper_status.startswith("APPROVED"):
        status = "Approved"
    else:
        raise ValueError(f"Unknown supplier status in {path}: {raw_status}")

    price_caps: dict[str, Decimal] = {}
    fx_rates: dict[str, Decimal] = {}
    for contract_m in re.finditer(r"\*\*Contract:\*\*\s*(.+)$", text, re.M):
        contract_line = contract_m.group(1).strip()
        cap_m = re.search(
            r"price\s+cap\s+for\s+(.+?)\s+of\s+₹([\d,]+(?:\.\d+)?)\s+per\s+",
            contract_line,
            re.I,
        )
        fx_m = re.search(
            r"invoices\s+in\s+([A-Z]{3})\s+are\s+converted\s+at\s+₹([\d,]+(?:\.\d+)?)\s+per\s+\1",
            contract_line,
            re.I,
        )
        if cap_m:
            cat_slug = _resolve_category_slug(cap_m.group(1), name_to_slug)
            price_caps[cat_slug] = _parse_decimal(cap_m.group(2))
        elif fx_m:
            fx_rates[fx_m.group(1).upper()] = _parse_decimal(fx_m.group(2))
        else:
            raise ValueError(f"Unrecognized supplier contract in {path}: {contract_line}")

    notes_m = re.search(r"\*\*Notes:\*\*\s*(.+)$", text, re.M)
    notes = notes_m.group(1).strip() if notes_m else ""

    return SupplierSpec(
        name=name,
        slug=path.stem,
        status=status,
        price_caps=price_caps,
        fx_rates=fx_rates,
        notes=notes,
    )


def _parse_precedent(path: Path) -> Precedent:
    text = path.read_text(encoding="utf-8")
    hdr_m = re.search(r"^#\s*Precedent\s+(P-\d+)\s*[—\-]\s*(.+)$", text, re.M)
    if not hdr_m:
        raise ValueError(f"Malformed precedent file {path}")
    body = text[hdr_m.end() :].strip()
    return Precedent(
        precedent_id=hdr_m.group(1).strip(),
        category_name=hdr_m.group(2).strip(),
        body=body,
    )


def load_playbook(playbook_dir: str | Path = "playbook/playbook") -> Playbook:
    """Load the entire procurement playbook from markdown files."""
    root = Path(playbook_dir)
    base_fx_rates, dozen_size, max_upfront_pct = _parse_policies(root / "policies")

    categories: dict[str, CategorySpec] = {}
    name_to_slug: dict[str, str] = {}
    cat_files = sorted((root / "categories").glob("*.md"))
    for p in tqdm(cat_files, desc="Loading categories", unit="file", leave=False):
        spec = _parse_category(p)
        categories[spec.slug] = spec
        name_to_slug[spec.slug.lower()] = spec.slug
        name_to_slug[spec.name.lower()] = spec.slug

    amd_files = sorted((root / "amendments").glob("*.md"))
    amendments: list[Amendment] = []
    for p in tqdm(amd_files, desc="Loading amendments", unit="file", leave=False):
        amendments.append(_parse_amendment(p, name_to_slug))
    amendments.sort(key=lambda a: (a.effective_from, a.amd_id))

    sup_files = sorted((root / "suppliers").glob("*.md"))
    suppliers: dict[str, SupplierSpec] = {}
    for p in tqdm(sup_files, desc="Loading suppliers", unit="file", leave=False):
        sup = _parse_supplier(p, name_to_slug)
        suppliers[sup.name] = sup
        suppliers[sup.name.lower()] = sup

    prec_files = sorted((root / "precedents").glob("*.md"))
    precedents: list[Precedent] = []
    for p in tqdm(prec_files, desc="Loading precedents", unit="file", leave=False):
        precedents.append(_parse_precedent(p))

    return Playbook(
        categories=categories,
        name_to_slug=name_to_slug,
        amendments=amendments,
        suppliers=suppliers,
        precedents=precedents,
        base_fx_rates=base_fx_rates,
        dozen_size=dozen_size,
        max_upfront_pct=max_upfront_pct,
    )


def get_effective_params(
    playbook: Playbook,
    category_slug: str,
    supplier_name: str,
    thread_date: date,
) -> EffectiveParams:
    """Resolve effective category numbers, amendments (<= thread_date), and supplier overrides."""
    cat_key = category_slug.strip().lower()
    if cat_key not in playbook.categories:
        cat_key = playbook.name_to_slug[cat_key]
    cat = playbook.categories[cat_key]

    sup = playbook.suppliers.get(supplier_name) or playbook.suppliers[supplier_name.strip().lower()]

    target = cat.target_price
    walkaway = cat.walkaway_price
    approval_limit = cat.approval_limit
    applied_amendments: list[str] = []

    for amd in playbook.amendments:
        if amd.category_slug == cat.slug and amd.effective_from <= thread_date:
            if amd.field_name == "target_price":
                target = amd.new_value
            elif amd.field_name == "walkaway_price":
                walkaway = amd.new_value
            elif amd.field_name == "approval_limit":
                approval_limit = amd.new_value
            applied_amendments.append(f"{amd.amd_id}({amd.effective_from.isoformat()})")

    fx_rates = dict(playbook.base_fx_rates)
    applied_contracts: list[str] = []

    if cat.slug in sup.price_caps:
        walkaway = sup.price_caps[cat.slug]
        applied_contracts.append(f"price_cap[{cat.slug}]=₹{walkaway}")

    for curr, rate in sup.fx_rates.items():
        fx_rates[curr] = rate
        applied_contracts.append(f"fx[{curr}]=₹{rate}")

    return EffectiveParams(
        category=cat,
        supplier=sup,
        thread_date=thread_date,
        target_price=target,
        walkaway_price=walkaway,
        approval_limit=approval_limit,
        rounds=cat.rounds,
        beta=cat.beta,
        gst_rate=cat.gst_rate,
        billing_period=cat.billing_period,
        pack_conversion=cat.pack_conversion,
        fx_rates=fx_rates,
        dozen_size=playbook.dozen_size,
        max_upfront_pct=playbook.max_upfront_pct,
        applied_amendments=applied_amendments,
        applied_contracts=applied_contracts,
    )


def load_threads(csv_path: str | Path) -> list[ThreadRow]:
    """Load negotiation threads CSV with a tqdm progress bar."""
    path = Path(csv_path)
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    threads: list[ThreadRow] = []
    for row in tqdm(rows, desc=f"Reading {path.name}", unit="row", leave=False):
        olc_raw = (row.get("our_last_counter") or "").strip()
        threads.append(
            ThreadRow(
                thread_id=row["thread_id"].strip(),
                thread_date=date.fromisoformat(row["date"].strip()),
                supplier=row["supplier"].strip(),
                category=row["category"].strip(),
                quantity=int(row["quantity"].strip()),
                round_num=int(row["round"].strip()),
                our_last_counter=_parse_decimal(olc_raw) if olc_raw else None,
                supplier_message=row["supplier_message"],
            )
        )
    return threads


def load_answers(csv_path: str | Path) -> dict[str, str]:
    """Load thread_id -> expected label mapping."""
    path = Path(csv_path)
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    answers: dict[str, str] = {}
    for row in tqdm(rows, desc=f"Reading {path.name}", unit="row", leave=False):
        answers[row["thread_id"].strip()] = row["label"].strip()
    return answers
