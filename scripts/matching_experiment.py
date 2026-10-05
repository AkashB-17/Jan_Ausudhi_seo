"""
Jan Aushadhi ↔ Brand medicine dataset matching experiment.

Read-only on source CSVs. Produces:
  - data/processed/matches_candidate.csv
  - data/processed/matching_report.txt

First-pass matching prioritizes composition + strength (+ dosage form),
not fuzzy product-name matching.
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PMBI_PATH = ROOT / "Product List_2_10_2026 @ 9_3_36.csv"
BRAND_PATH = ROOT / "updated_indian_medicine_data.csv"
OUT_DIR = ROOT / "data" / "processed"
MATCHES_PATH = OUT_DIR / "matches_candidate.csv"
REPORT_PATH = OUT_DIR / "matching_report.txt"

# Conservative ingredient aliases only (same molecule, common spelling variants).
INGREDIENT_ALIASES = {
    "amoxycillin": "amoxicillin",
    "paracetamol": "paracetamol",
    "acetaminophen": "paracetamol",
    "vit b1": "thiamine",
    "vitamin b1": "thiamine",
    "vit b2": "riboflavin",
    "vitamin b2": "riboflavin",
    "vit b3": "nicotinamide",
    "vitamin b3": "nicotinamide",
    "vit b6": "pyridoxine",
    "vitamin b6": "pyridoxine",
    "vit b12": "cyanocobalamin",
    "vitamin b12": "cyanocobalamin",
    "folic acid": "folic acid",
    "vitamin c": "ascorbic acid",
    "ascorbic acid": "ascorbic acid",
}

# Light salt/ester suffix stripping — only trailing tokens that rarely change identity.
SALT_SUFFIXES = (
    " sodium",
    " potassium",
    " hydrochloride",
    " hydrobromide",
    " sulphate",
    " sulfate",
    " phosphate",
    " maleate",
    " mesylate",
    " nitrate",
    " acetate",
    " citrate",
    " tartrate",
    " succinate",
    " fumarate",
    " besylate",
    " diethylamine",
    " monohydrate",
    " dihydrate",
    " trihydrate",
    " hcl",
)

DOSAGE_FORMS = [
    ("rotacap", "rotacap"),
    ("respule", "respule"),
    ("inhaler", "inhaler"),
    ("inhalation", "inhaler"),
    ("nebuliser", "nebule"),
    ("nebulizer", "nebule"),
    ("eye drop", "eye drop"),
    ("ear drop", "ear drop"),
    ("nasal drop", "nasal drop"),
    ("nasal spray", "nasal spray"),
    ("mouth wash", "mouthwash"),
    ("mouthwash", "mouthwash"),
    ("soft gelatin capsule", "capsule"),
    ("softgel capsule", "capsule"),
    ("softgel", "capsule"),
    ("gastro-resistant tablet", "tablet"),
    ("gastro resistant tablet", "tablet"),
    ("prolonged release tablet", "tablet"),
    ("sustained release tablet", "tablet"),
    ("extended release tablet", "tablet"),
    ("delayed release tablet", "tablet"),
    ("dispersible tablet", "tablet"),
    ("chewable tablet", "tablet"),
    ("effervescent tablet", "tablet"),
    ("tablet", "tablet"),
    ("capsule", "capsule"),
    ("injection", "injection"),
    ("infusion", "infusion"),
    ("suspension", "suspension"),
    ("syrup", "syrup"),
    ("solution", "solution"),
    ("granule", "granule"),
    ("powder", "powder"),
    ("sachet", "sachet"),
    ("ointment", "ointment"),
    ("cream", "cream"),
    ("gel", "gel"),
    ("lotion", "lotion"),
    ("spray", "spray"),
    ("drop", "drop"),
    ("patch", "patch"),
    ("gargle", "gargle"),
    ("emulsion", "emulsion"),
    ("vaccine", "vaccine"),
]

FORM_PATTERN = re.compile(
    r"("
    + "|".join(re.escape(k) for k, _ in sorted(DOSAGE_FORMS, key=lambda x: -len(x[0])))
    + r")s?\b",
    re.IGNORECASE,
)

# Ingredient + strength patterns
BRAND_PART_RE = re.compile(
    r"^\s*(?P<name>.+?)\s*\((?P<strength>[^)]+)\)\s*$",
    re.IGNORECASE,
)
STRENGTH_TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mcg|µg|ug|mg|g|ml|iu|%|w/w|w/v|million|billion)",
    re.IGNORECASE,
)
PMBI_IP_STRENGTH_RE = re.compile(
    r"^(?P<head>.+?)\s+(?P<form>"
    r"(?:Gastro[- ]?resistant|Prolonged\s+Release|Sustained\s+Release|"
    r"Extended\s+Release|Delayed\s+Release|Dispersible|Chewable|"
    r"Soft\s+Gelatin|Softgel)?\s*"
    r"(?:Tablets?|Capsules?|Injections?|Infusions?|Suspensions?|Syrups?|"
    r"Solutions?|Gels?|Creams?|Ointments?|Lotions?|Drops?|Sprays?|"
    r"Powders?|Granules?|Sachets?|Inhalers?|Inhalations?|Respules?|"
    r"Rotacaps?|Vaccines?|Emulsions?|Mouthwash(?:es)?|Gargles?|Patches?)"
    r")\s*(?:IP|BP|USP|NFI)?\s*(?P<strength>.+)$",
    re.IGNORECASE,
)
PMBI_LEADING_STRENGTH_RE = re.compile(
    r"^(?P<head>.+?)\s+(?P<strength>\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|g|ml|iu|%)(?:\s*(?:w/w|w/v))?"
    r"(?:\s*(?:per|/)\s*\d+(?:\.\d+)?\s*(?:ml|g))?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Component:
    ingredient: str
    strength: str  # normalized, e.g. "100mg", "1.16%w/w", "125mg/5ml"


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize_unit(unit: str) -> str:
    u = unit.lower().replace("µ", "u").replace("μ", "u")
    if u in {"ug", "µg", "μg"}:
        return "mcg"
    if u == "w/w":
        return "%w/w"
    if u == "w/v":
        return "%w/v"
    return u


def normalize_strength_string(raw: str) -> str:
    """Normalize a strength fragment to a compact comparable string."""
    s = normalize_whitespace(raw).lower()
    s = s.replace("µ", "u").replace("μ", "u")
    s = s.replace(" per ", "/")
    s = re.sub(r"\s*/\s*", "/", s)
    s = s.replace("% w/w", "%w/w").replace("%w/w", "%w/w")
    s = s.replace("% w/v", "%w/v").replace("%w/v", "%w/v")
    s = re.sub(r"(\d)\s+(mg|mcg|g|ml|iu|%)", r"\1\2", s)
    s = re.sub(r"(\d)\s*%\s*w/w", r"\1%w/w", s)
    s = re.sub(r"(\d)\s*%\s*w/v", r"\1%w/v", s)
    s = re.sub(r"[^\w./%]+", "", s)
    return s


def normalize_ingredient(name: str) -> str:
    n = normalize_whitespace(name).lower()
    n = n.replace("&", " and ")
    n = re.sub(r"[^\w\s\-+/]", " ", n)
    n = normalize_whitespace(n)
    # drop pharmacopoeia markers stuck to names
    n = re.sub(r"\b(ip|bp|usp|nfi)\b", " ", n)
    n = normalize_whitespace(n)
    if n in INGREDIENT_ALIASES:
        n = INGREDIENT_ALIASES[n]
    # strip common salt suffixes once
    changed = True
    while changed:
        changed = False
        for suf in SALT_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 2:
                n = n[: -len(suf)].strip()
                changed = True
                break
    if n in INGREDIENT_ALIASES:
        n = INGREDIENT_ALIASES[n]
    return n


def extract_dosage_form(text: str) -> str | None:
    if not text or (isinstance(text, float) and pd.isna(text)):
        return None
    low = str(text).lower()
    m = FORM_PATTERN.search(low)
    if not m:
        return None
    key = m.group(1).lower()
    for pattern, canon in DOSAGE_FORMS:
        if key == pattern or key.rstrip("s") == pattern:
            return canon
    return key.rstrip("s")


def parse_brand_composition(short1, short2, salt) -> list[Component]:
    """
    Prefer short_composition1/2; enrich with salt_composition when it has
    additional parts (e.g. 3-ingredient combos not fully captured by shorts).
    """
    parts: list[str] = []
    for val in (short1, short2):
        if pd.notna(val) and str(val).strip():
            parts.append(str(val).strip())

    salt_parts: list[str] = []
    if pd.notna(salt) and str(salt).strip():
        salt_parts = [p.strip() for p in str(salt).split("+") if p.strip()]

    # Use salt if it provides more ingredient parts than shorts alone.
    if len(salt_parts) > len(parts):
        use_parts = salt_parts
        source = "salt"
    else:
        use_parts = parts
        source = "short"

    components: list[Component] = []
    for part in use_parts:
        m = BRAND_PART_RE.match(part)
        if m:
            ing = normalize_ingredient(m.group("name"))
            strength = normalize_strength_string(m.group("strength"))
            if ing:
                components.append(Component(ing, strength))
        else:
            # fallback: try trailing strength
            tokens = STRENGTH_TOKEN_RE.findall(part)
            name_only = STRENGTH_TOKEN_RE.sub(" ", part)
            ing = normalize_ingredient(name_only)
            strength = ""
            if tokens:
                num, unit = tokens[0]
                strength = normalize_strength_string(f"{num}{unit}")
            if ing:
                components.append(Component(ing, strength))
    # dedupe while preserving order
    seen = set()
    unique: list[Component] = []
    for c in components:
        key = (c.ingredient, c.strength)
        if key not in seen and c.ingredient:
            seen.add(key)
            unique.append(c)
    _ = source  # documented choice above
    return unique


def split_pmbi_combo_clauses(generic_name: str) -> tuple[list[str], str | None]:
    """Split combo names on ' and ' / commas; return clauses + trailing form if any."""
    text = normalize_whitespace(generic_name)
    form = extract_dosage_form(text)

    # Remove trailing dosage-form phrase for clause splitting
    work = text
    if form:
        # strip last form-like phrase and optional IP + leftover strength if form is trailing
        work = re.sub(
            r"(?:,?\s*)?(?:Gastro[- ]?resistant|Prolonged\s+Release|Sustained\s+Release|"
            r"Extended\s+Release|Delayed\s+Release|Dispersible|Chewable|Soft\s+Gelatin|Softgel)?\s*"
            r"(?:Tablets?|Capsules?|Injections?|Infusions?|Suspensions?|Syrups?|Solutions?|"
            r"Gels?|Creams?|Ointments?|Lotions?|Drops?|Sprays?|Powders?|Granules?|Sachets?|"
            r"Inhalers?|Inhalations?|Respules?|Rotacaps?|Vaccines?|Emulsions?|"
            r"Mouthwash(?:es)?|Gargles?|Patches?)\s*(?:IP|BP|USP|NFI)?\s*$",
            "",
            work,
            flags=re.IGNORECASE,
        ).strip(" ,")

    # Split combos: "A 100mg and B 325mg" or "A 500mg, B 50mg and C 325mg"
    if re.search(r"\band\b", work, re.I) or (
        "," in work and STRENGTH_TOKEN_RE.search(work)
    ):
        clauses = re.split(r"\s+and\s+|,\s*", work, flags=re.IGNORECASE)
        clauses = [c.strip(" ,") for c in clauses if c.strip(" ,")]
        return clauses, form
    return [work if work else text], form


def parse_pmbi_clause(clause: str) -> Component | None:
    clause = normalize_whitespace(clause)
    if not clause:
        return None

    # Pattern: Ingredient Form IP Strength  e.g. "Aceclofenac Tablets IP 100 mg"
    m = PMBI_IP_STRENGTH_RE.match(clause)
    if m:
        ing = normalize_ingredient(m.group("head"))
        strength = normalize_strength_string(m.group("strength"))
        if ing:
            return Component(ing, strength)

    # Pattern: Ingredient Strength ... e.g. "Aceclofenac 100mg"
    m2 = PMBI_LEADING_STRENGTH_RE.match(clause)
    if m2:
        # avoid treating form words as ingredient heads when strength is after form
        head = m2.group("head")
        if extract_dosage_form(head) and not STRENGTH_TOKEN_RE.search(head):
            # e.g. "Diclofenac Sodium Injection IP 25mg per ml" already handled above usually
            pass
        ing = normalize_ingredient(head)
        # strip trailing form words accidentally left in head
        for pattern, _ in DOSAGE_FORMS:
            ing = re.sub(rf"\b{re.escape(pattern)}s?\b", " ", ing).strip()
        ing = normalize_whitespace(ing)
        strength = normalize_strength_string(m2.group("strength"))
        if ing:
            return Component(ing, strength)

    # No clear strength — ingredient-only (weak)
    ing = normalize_ingredient(clause)
    for pattern, _ in DOSAGE_FORMS:
        ing = re.sub(rf"\b{re.escape(pattern)}s?\b", " ", ing).strip()
    ing = normalize_whitespace(ing)
    if ing and len(ing) > 2:
        return Component(ing, "")
    return None


def parse_pmbi_generic(generic_name: str) -> tuple[list[Component], str | None]:
    clauses, form = split_pmbi_combo_clauses(generic_name)
    components: list[Component] = []
    for clause in clauses:
        # If single-clause still looks like "Name Form Strength"
        comp = parse_pmbi_clause(clause)
        if comp:
            components.append(comp)

    # Parenthetical alternate salt names: keep primary only; ignore paren content for matching key
    # e.g. "Diclofenac Gel IP 1.16%w/w (Diclofenac Diethylamine)" already handled via strength/form

    seen = set()
    unique: list[Component] = []
    for c in components:
        key = (c.ingredient, c.strength)
        if key not in seen and c.ingredient:
            seen.add(key)
            unique.append(c)
    return unique, form


def composition_key(components: list[Component], with_strength: bool = True) -> frozenset:
    if with_strength:
        return frozenset((c.ingredient, c.strength) for c in components if c.ingredient)
    return frozenset(c.ingredient for c in components if c.ingredient)


def ingredients_only(components: list[Component]) -> frozenset:
    return frozenset(c.ingredient for c in components if c.ingredient)


def format_components(components: list[Component]) -> str:
    return " + ".join(f"{c.ingredient} ({c.strength})" if c.strength else c.ingredient for c in components)


def profile_dataframe(df: pd.DataFrame, name: str) -> list[str]:
    lines = [
        f"=== {name} ===",
        f"Row count: {len(df):,}",
        f"Columns ({len(df.columns)}): {list(df.columns)}",
        "",
        "Data types:",
    ]
    for col, dtype in df.dtypes.items():
        lines.append(f"  - {col}: {dtype}")
    lines.append("")
    lines.append("Missing-value counts:")
    missing = df.isna().sum()
    for col, cnt in missing.items():
        lines.append(f"  - {col}: {int(cnt):,} ({cnt / len(df) * 100:.2f}%)")
    lines.append("")
    lines.append("Duplicate counts:")
    lines.append(f"  - fully duplicate rows: {int(df.duplicated().sum()):,}")
    return lines


def classify_match(
    pmbi_comps: list[Component],
    brand_comps: list[Component],
    pmbi_form: str | None,
    brand_form: str | None,
) -> tuple[str, str, float]:
    """
    Returns (match_method, match_quality, confidence).
    quality: strong | moderate | ambiguous_signal (used later for aggregation)
    """
    if not pmbi_comps or not brand_comps:
        return "none", "none", 0.0

    pmbi_full = composition_key(pmbi_comps, True)
    brand_full = composition_key(brand_comps, True)
    pmbi_ings = ingredients_only(pmbi_comps)
    brand_ings = ingredients_only(brand_comps)

    form_ok = False
    form_mismatch = False
    if pmbi_form and brand_form:
        form_ok = pmbi_form == brand_form
        form_mismatch = pmbi_form != brand_form

    # Exact ingredient+strength set match
    if pmbi_full == brand_full and all(c.strength for c in pmbi_comps):
        if form_ok:
            return "exact_composition_strength_form", "strong", 0.95
        if form_mismatch:
            return "exact_composition_strength_form_mismatch", "moderate", 0.75
        return "exact_composition_strength", "strong", 0.90

    # Same ingredients; strengths match as multisets ignoring empty
    if pmbi_ings == brand_ings and len(pmbi_ings) > 0:
        pmbi_strengths = sorted(c.strength for c in pmbi_comps)
        brand_strengths = sorted(c.strength for c in brand_comps)
        if pmbi_strengths == brand_strengths and all(pmbi_strengths):
            if form_ok:
                return "ingredient_set_strength_form", "strong", 0.92
            return "ingredient_set_strength", "strong", 0.88

        # Ingredients match, some strengths missing or differ
        matched_strength_pairs = 0
        for pc in pmbi_comps:
            for bc in brand_comps:
                if pc.ingredient == bc.ingredient and pc.strength and pc.strength == bc.strength:
                    matched_strength_pairs += 1
                    break
        if matched_strength_pairs == len(pmbi_comps) and matched_strength_pairs == len(brand_comps):
            return "ingredient_strength_pairwise", "strong", 0.87

        if matched_strength_pairs >= max(1, len(pmbi_comps) - 1) and form_ok:
            return "ingredient_partial_strength_form", "moderate", 0.65

        if matched_strength_pairs > 0:
            return "ingredient_partial_strength", "moderate", 0.55

        # ingredients only
        if form_ok:
            return "ingredient_set_form_only", "moderate", 0.50
        return "ingredient_set_only", "moderate", 0.40

    return "none", "none", 0.0


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    report: list[str] = []
    report.append("Jan Aushadhi ↔ Brand Medicine Matching Experiment")
    report.append("=" * 60)
    report.append("")
    report.append("Purpose: assess whether the two datasets contain enough overlapping")
    report.append("composition/strength information for a brand → Jan Aushadhi match workflow.")
    report.append("Method: deterministic composition matching (no ML, no primary fuzzy names).")
    report.append("")

    # ------------------------------------------------------------------
    # Step 1: load & profile
    # ------------------------------------------------------------------
    print("Loading CSVs...")
    pmbi = pd.read_csv(PMBI_PATH)
    brand = pd.read_csv(BRAND_PATH)

    report.extend(profile_dataframe(pmbi, "PMBI / Jan Aushadhi catalogue"))
    report.append("")
    # Drug Code uniqueness
    report.append(f"  - duplicate Drug Code values: {int(pmbi['Drug Code'].duplicated().sum()):,}")
    report.append(f"  - duplicate Generic Name values: {int(pmbi['Generic Name'].duplicated().sum()):,}")
    report.append("")

    report.extend(profile_dataframe(brand, "Indian brand medicine dataset"))
    report.append("")
    report.append(f"  - duplicate id values: {int(brand['id'].duplicated().sum()):,}")
    report.append(f"  - duplicate name values: {int(brand['name'].duplicated().sum()):,}")
    report.append("")

    # ------------------------------------------------------------------
    # Step 2: exclude discontinued
    # ------------------------------------------------------------------
    disc_counts = brand["Is_discontinued"].value_counts(dropna=False).to_dict()
    brand_active = brand.loc[~brand["Is_discontinued"]].copy()
    report.append("=== Discontinued filter ===")
    report.append(f"Is_discontinued distribution: {disc_counts}")
    report.append(f"Active brand records retained: {len(brand_active):,}")
    report.append(f"Discontinued excluded: {len(brand) - len(brand_active):,}")
    report.append("")

    # ------------------------------------------------------------------
    # Steps 3–5: normalize
    # ------------------------------------------------------------------
    print("Normalizing PMBI generics...")
    pmbi_parsed = []
    pmbi_pattern_counter = Counter()
    for _, row in pmbi.iterrows():
        comps, form = parse_pmbi_generic(str(row["Generic Name"]))
        n = len(comps)
        has_strength = sum(1 for c in comps if c.strength)
        if n == 0:
            pmbi_pattern_counter["parse_failed"] += 1
        elif n == 1 and has_strength == 1:
            pmbi_pattern_counter["single_ingredient_with_strength"] += 1
        elif n == 1 and has_strength == 0:
            pmbi_pattern_counter["single_ingredient_no_strength"] += 1
        elif n > 1 and has_strength == n:
            pmbi_pattern_counter["combo_all_strengths"] += 1
        elif n > 1:
            pmbi_pattern_counter["combo_partial_strengths"] += 1
        else:
            pmbi_pattern_counter["other"] += 1
        if form:
            pmbi_pattern_counter[f"form:{form}"] += 1
        else:
            pmbi_pattern_counter["form:missing"] += 1
        pmbi_parsed.append(
            {
                "drug_code": row["Drug Code"],
                "generic_name": row["Generic Name"],
                "mrp": row["MRP"],
                "unit_size": row["Unit Size"],
                "group_name": row["Group Name"],
                "components": comps,
                "form": form,
                "comp_key": composition_key(comps, True) if comps else frozenset(),
                "ing_key": ingredients_only(comps) if comps else frozenset(),
                "norm_composition": format_components(comps),
            }
        )

    report.append("=== PMBI Generic Name parse patterns ===")
    for k, v in pmbi_pattern_counter.most_common():
        report.append(f"  - {k}: {v:,}")
    report.append("")
    report.append("Sample PMBI normalizations (first 12):")
    for item in pmbi_parsed[:12]:
        report.append(
            f"  - [{item['drug_code']}] {item['generic_name']}"
            f"  ->  {item['norm_composition']} | form={item['form']}"
        )
    report.append("")

    print("Normalizing brand compositions...")
    brand_records = []
    brand_parse_counter = Counter()
    for _, row in brand_active.iterrows():
        comps = parse_brand_composition(
            row.get("short_composition1"),
            row.get("short_composition2"),
            row.get("salt_composition"),
        )
        form = extract_dosage_form(str(row.get("name", ""))) or extract_dosage_form(
            str(row.get("pack_size_label", ""))
        )
        if not comps:
            brand_parse_counter["parse_failed"] += 1
        elif len(comps) == 1:
            brand_parse_counter["single"] += 1
        else:
            brand_parse_counter["combo"] += 1
        if form:
            brand_parse_counter[f"form:{form}"] += 1
        else:
            brand_parse_counter["form:missing"] += 1

        brand_records.append(
            {
                "id": row["id"],
                "name": row["name"],
                "price": row["price"],
                "manufacturer_name": row["manufacturer_name"],
                "pack_size_label": row["pack_size_label"],
                "short_composition1": row["short_composition1"],
                "short_composition2": row["short_composition2"] if pd.notna(row["short_composition2"]) else "",
                "salt_composition": row["salt_composition"] if pd.notna(row["salt_composition"]) else "",
                "components": comps,
                "form": form,
                "comp_key": composition_key(comps, True) if comps else frozenset(),
                "ing_key": ingredients_only(comps) if comps else frozenset(),
                "norm_composition": format_components(comps),
                "brand_composition_raw": " + ".join(
                    x
                    for x in [
                        str(row["short_composition1"]).strip()
                        if pd.notna(row["short_composition1"])
                        else "",
                        str(row["short_composition2"]).strip()
                        if pd.notna(row["short_composition2"])
                        else "",
                    ]
                    if x
                ),
            }
        )

    report.append("=== Brand composition parse summary (active only) ===")
    for k, v in brand_parse_counter.most_common(20):
        report.append(f"  - {k}: {v:,}")
    report.append("")
    report.append("Sample brand normalizations (first 10):")
    for item in brand_records[:10]:
        report.append(
            f"  - [{item['id']}] {item['name']}"
            f"  raw={item['brand_composition_raw']!r}"
            f"  ->  {item['norm_composition']} | form={item['form']}"
        )
    report.append("")

    # ------------------------------------------------------------------
    # Step 6: first-pass matching via inverted index on ingredient sets
    # ------------------------------------------------------------------
    print("Building brand indexes and matching...")
    brand_by_ing: dict[frozenset, list[dict]] = defaultdict(list)
    brand_by_comp: dict[frozenset, list[dict]] = defaultdict(list)
    for rec in brand_records:
        if rec["ing_key"]:
            brand_by_ing[rec["ing_key"]].append(rec)
        if rec["comp_key"]:
            brand_by_comp[rec["comp_key"]].append(rec)

    candidate_rows: list[dict] = []
    pmbi_match_stats = Counter()
    pmbi_with_any: set = set()
    pmbi_with_strong: set = set()
    pmbi_with_ambiguous: set = set()

    for p in pmbi_parsed:
        if not p["ing_key"]:
            pmbi_match_stats["pmbi_unparsed"] += 1
            continue

        # Candidate pool: same ingredient set (order-insensitive)
        pool = brand_by_ing.get(p["ing_key"], [])
        if not pool:
            pmbi_match_stats["no_ingredient_set_match"] += 1
            continue

        local_matches: list[dict] = []
        for b in pool:
            method, quality, conf = classify_match(
                p["components"], b["components"], p["form"], b["form"]
            )
            if quality == "none":
                continue
            local_matches.append(
                {
                    "brand_medicine_id": b["id"],
                    "brand_medicine_name": b["name"],
                    "brand_composition": b["brand_composition_raw"],
                    "brand_norm_composition": b["norm_composition"],
                    "brand_form": b["form"] or "",
                    "brand_price": b["price"],
                    "brand_pack_size_label": b["pack_size_label"],
                    "brand_manufacturer": b["manufacturer_name"],
                    "pmbi_drug_code": p["drug_code"],
                    "pmbi_generic_name": p["generic_name"],
                    "pmbi_mrp": p["mrp"],
                    "pmbi_unit_size": p["unit_size"],
                    "pmbi_group_name": p["group_name"],
                    "pmbi_norm_composition": p["norm_composition"],
                    "pmbi_form": p["form"] or "",
                    "match_method": method,
                    "match_quality": quality,
                    "match_confidence": conf,
                }
            )

        if not local_matches:
            pmbi_match_stats["ingredient_set_but_no_classified_match"] += 1
            continue

        pmbi_with_any.add(p["drug_code"])
        strong = [m for m in local_matches if m["match_quality"] == "strong"]
        moderate = [m for m in local_matches if m["match_quality"] == "moderate"]

        # Ambiguous: multiple distinct brand products at strong level,
        # or only many moderate matches with no clear unique composition+strength winner.
        if len(strong) > 1:
            # still keep them; mark ambiguity at PMBI level
            pmbi_with_ambiguous.add(p["drug_code"])
            for m in strong:
                m["match_quality"] = "strong"  # keep strong, ambiguity is many-to-one expected for brands
            # Many brands for one generic is expected — not "ambiguous match" in a bad sense.
            # Treat as ambiguous only when methods/quality disagree or strengths conflict.
        elif len(strong) == 1:
            pmbi_with_strong.add(p["drug_code"])
        elif len(moderate) > 0:
            # no strong; moderate only
            if len({(m["brand_norm_composition"], m["brand_form"]) for m in moderate}) > 3:
                pmbi_with_ambiguous.add(p["drug_code"])
            else:
                pmbi_with_ambiguous.add(p["drug_code"])

        # Cap explosion: for exact composition keys, keep all strong matches
        # (one generic → many brands is the expected commercial reality).
        # For reporting file size, keep up to 50 brand candidates per PMBI product,
        # preferring higher confidence.
        local_matches.sort(key=lambda m: (-m["match_confidence"], m["brand_medicine_name"]))
        kept = local_matches[:50]
        if len(strong) >= 1:
            pmbi_with_strong.add(p["drug_code"])
        candidate_rows.extend(kept)
        pmbi_match_stats["pmbi_with_candidates"] += 1

    matches_df = pd.DataFrame(candidate_rows)
    if len(matches_df) == 0:
        matches_df = pd.DataFrame(
            columns=[
                "brand_medicine_id",
                "brand_medicine_name",
                "brand_composition",
                "brand_norm_composition",
                "brand_form",
                "brand_price",
                "brand_pack_size_label",
                "brand_manufacturer",
                "pmbi_drug_code",
                "pmbi_generic_name",
                "pmbi_mrp",
                "pmbi_unit_size",
                "pmbi_group_name",
                "pmbi_norm_composition",
                "pmbi_form",
                "match_method",
                "match_quality",
                "match_confidence",
            ]
        )

    matches_df.to_csv(MATCHES_PATH, index=False)
    print(f"Wrote {len(matches_df):,} candidate rows -> {MATCHES_PATH}")

    # ------------------------------------------------------------------
    # Steps 7–8: summary statistics
    # ------------------------------------------------------------------
    n_pmbi = len(pmbi)
    n_brand_active = len(brand_active)
    n_candidates = len(matches_df)
    n_strong_rows = int((matches_df["match_quality"] == "strong").sum()) if n_candidates else 0
    n_moderate_rows = int((matches_df["match_quality"] == "moderate").sum()) if n_candidates else 0

    # PMBI-level: strong = has ≥1 strong candidate; ambiguous = multiple competing methods
    # or only moderate; none = no candidates
    pmbi_codes = {p["drug_code"] for p in pmbi_parsed}
    pmbi_no_match = pmbi_codes - pmbi_with_any

    # Refine ambiguity: PMBI product with strong matches from conflicting strength norms
    # is rare; primary "ambiguous" = has candidates but zero strong matches
    pmbi_strong_only = set()
    pmbi_moderate_only = set()
    if n_candidates:
        for code, grp in matches_df.groupby("pmbi_drug_code"):
            if (grp["match_quality"] == "strong").any():
                pmbi_strong_only.add(code)
            else:
                pmbi_moderate_only.add(code)

    n_strong_pmbi = len(pmbi_strong_only)
    n_ambiguous_pmbi = len(pmbi_moderate_only)
    n_no_match = len(pmbi_no_match)

    # Coverage by method
    method_counts = (
        matches_df["match_method"].value_counts().to_dict() if n_candidates else {}
    )

    # Example strong matches
    report.append("=== Matching summary statistics ===")
    report.append(f"Active brand records examined: {n_brand_active:,}")
    report.append(f"PMBI products examined: {n_pmbi:,}")
    report.append(f"Candidate match rows (brand↔PMBI pairs, capped ≤50 brands/PMBI): {n_candidates:,}")
    report.append(f"Strong match rows: {n_strong_rows:,}")
    report.append(f"Moderate match rows: {n_moderate_rows:,}")
    report.append(f"PMBI products with ≥1 strong candidate: {n_strong_pmbi:,}")
    report.append(f"PMBI products with only moderate/ambiguous candidates: {n_ambiguous_pmbi:,}")
    report.append(f"PMBI products with no candidate brand match: {n_no_match:,}")
    report.append(
        f"PMBI coverage (any candidate): {len(pmbi_with_any):,} / {n_pmbi:,}"
        f" ({len(pmbi_with_any)/n_pmbi*100:.1f}%)"
    )
    report.append(
        f"PMBI strong coverage: {n_strong_pmbi:,} / {n_pmbi:,}"
        f" ({n_strong_pmbi/n_pmbi*100:.1f}%)"
    )
    report.append("")
    report.append("Match method distribution (candidate rows):")
    for k, v in sorted(method_counts.items(), key=lambda x: -x[1]):
        report.append(f"  - {k}: {v:,}")
    report.append("")
    report.append("Internal match funnel:")
    for k, v in pmbi_match_stats.most_common():
        report.append(f"  - {k}: {v:,}")
    report.append("")

    # Feasibility verdict
    strong_pct = n_strong_pmbi / n_pmbi * 100 if n_pmbi else 0
    report.append("=== Feasibility interpretation ===")
    if strong_pct >= 40:
        verdict = (
            "PROMISING: A substantial share of PMBI products have strong "
            "composition+strength overlaps with active brand records. "
            "A brand → Jan Aushadhi workflow looks feasible with further refinement."
        )
    elif strong_pct >= 15:
        verdict = (
            "PARTIAL OVERLAP: Some PMBI products match well on composition+strength, "
            "but coverage is incomplete. Expect to need better form handling, "
            "ingredient synonym expansion, and pack-size logic before production use."
        )
    else:
        verdict = (
            "WEAK OVERLAP under conservative rules: either compositions normalize "
            "poorly across datasets, or naming/strength conventions differ enough "
            "that exact composition matching alone is insufficient."
        )
    report.append(verdict)
    report.append("")
    report.append("Notes / limitations of this first pass:")
    report.append("  - Brand short_composition1/2 capture at most 2 ingredients; 3+ combos")
    report.append("    may be incomplete unless salt_composition is present.")
    report.append("  - Salt suffixes stripped lightly (e.g. sodium/HCl); may over-merge some salts.")
    report.append("  - Dosage-form matching is secondary; modified-release distinctions are collapsed.")
    report.append("  - Many brands per generic is expected and counted as strong, not failure.")
    report.append("  - No fuzzy trade-name matching was used as the primary method.")
    report.append("")

    if n_candidates:
        report.append("Example strong matches (up to 15):")
        strong_examples = matches_df[matches_df["match_quality"] == "strong"].head(15)
        for _, r in strong_examples.iterrows():
            report.append(
                f"  - brand [{r['brand_medicine_id']}] {r['brand_medicine_name']}"
                f" ({r['brand_composition']})  <->  PMBI [{r['pmbi_drug_code']}]"
                f" {r['pmbi_generic_name']} | method={r['match_method']}"
                f" conf={r['match_confidence']}"
            )
        report.append("")
        report.append("Example unmatched PMBI products (up to 20):")
        unmatched_items = [p for p in pmbi_parsed if p["drug_code"] in pmbi_no_match][:20]
        for p in unmatched_items:
            report.append(
                f"  - [{p['drug_code']}] {p['generic_name']}"
                f" | parsed={p['norm_composition']!r} form={p['form']}"
            )

    report.append("")
    report.append(f"Outputs written:")
    report.append(f"  - {MATCHES_PATH}")
    report.append(f"  - {REPORT_PATH}")

    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")
    print(f"Wrote report -> {REPORT_PATH}")
    print()
    print("\n".join(report[-40:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
