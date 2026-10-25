"""
Jan Aushadhi data preparation + match validation pipeline.

Stages covered:
  RAW (read-only) → CLEANING → NORMALIZATION/PARSING → CANDIDATE MATCHING → VALIDATION

Does NOT create SQLite, FastAPI, frontend, ML models, or deployment assets.

Outputs under data/processed/:
  - brands_clean.csv
  - pmbi_clean.csv
  - matches_validated_candidates.csv
  - matching_report.txt
  - verified_synonyms.json  (auditable synonym map; may already exist)
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PMBI_PATH = ROOT / "Product List_2_10_2026 @ 9_3_36.csv"
BRAND_PATH = ROOT / "updated_indian_medicine_data.csv"
OUT_DIR = ROOT / "data" / "processed"
SYNONYM_PATH = OUT_DIR / "verified_synonyms.json"

BRANDS_CLEAN_PATH = OUT_DIR / "brands_clean.csv"
PMBI_CLEAN_PATH = OUT_DIR / "pmbi_clean.csv"
MATCHES_PATH = OUT_DIR / "matches_validated_candidates.csv"
REPORT_PATH = OUT_DIR / "matching_report.txt"

# ---------------------------------------------------------------------------
# Dosage forms / regex (compiled once)
# ---------------------------------------------------------------------------

DOSAGE_FORMS: list[tuple[str, str]] = [
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
    ("prolonged-release tablet", "tablet"),
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

BRAND_PART_RE = re.compile(
    r"^\s*(?P<name>.+?)\s*\((?P<strength>[^)]+)\)\s*$",
    re.IGNORECASE,
)

STRENGTH_TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>mcg|µg|ug|mg|g|ml|iu|%|w/w|w/v|million|billion)",
    re.IGNORECASE,
)

# Brand-name hint that product may have N strengths / actives, e.g. 100mg/325mg/15mg
NAME_STRENGTH_RATIO_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*(?:mg|mcg|g|iu|%))",
    re.IGNORECASE,
)

NOISE_WORDS_RE = re.compile(
    r"\b("
    r"paediatric|pediatric|oral|flavou?red|base|prophylactic|"
    r"for\s+intravenous|intravenous|im|iv|sc|"
    r"prolonged[- ]?release|sustained[- ]?release|extended[- ]?release|"
    r"delayed[- ]?release|gastro[- ]?resistant|modified[- ]?release|"
    r"dispersible|chewable|effervescent|soft\s+gelatin|softgel|"
    r"ip|bp|usp|nfi|who\s+formula|"
    r"dihydrate|monohydrate|trihydrate"
    r")\b",
    re.IGNORECASE,
)

RELEASE_PREFIX_RE = re.compile(
    r"\b(prolonged[- ]?release|sustained[- ]?release|extended[- ]?release|"
    r"delayed[- ]?release|gastro[- ]?resistant|modified[- ]?release)\b",
    re.IGNORECASE,
)

# Salt/ester suffixes stripped only after synonym canonicalization of full phrases.
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
    " stearate",
    " proxetil",
    " axetil",
    " dipropionate",
    " propionate",
)


@dataclass(frozen=True)
class Component:
    ingredient: str
    strength: str  # normalized comparable token, e.g. "100mg", "125mg/5ml"


@dataclass
class ParsedMedicine:
    components: list[Component] = field(default_factory=list)
    dosage_form: str | None = None
    incomplete_composition: bool = False
    incompleteness_reason: str = ""
    parse_notes: str = ""

    @property
    def ingredient_key(self) -> frozenset[str]:
        return frozenset(c.ingredient for c in self.components if c.ingredient)

    @property
    def composition_key(self) -> frozenset[tuple[str, str]]:
        return frozenset((c.ingredient, c.strength) for c in self.components if c.ingredient)

    @property
    def strength_summary(self) -> str:
        return " + ".join(c.strength for c in self.components if c.strength)

    @property
    def composition_summary(self) -> str:
        parts = []
        for c in self.components:
            if c.strength:
                parts.append(f"{c.ingredient} ({c.strength})")
            else:
                parts.append(c.ingredient)
        return " + ".join(parts)


# ---------------------------------------------------------------------------
# Synonyms / normalization helpers
# ---------------------------------------------------------------------------


def load_synonyms(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("synonyms", data)
    return {str(k).lower().strip(): str(v).lower().strip() for k, v in raw.items() if not str(k).startswith("_")}


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def normalize_strength_string(raw: str) -> str:
    """Normalize strength to a compact comparable string; convert plain g→mg."""
    s = normalize_whitespace(raw).lower()
    s = s.replace("µ", "u").replace("μ", "u")
    s = s.replace(" per ", "/")
    s = re.sub(r"\s*/\s*", "/", s)
    s = s.replace("% w/w", "%w/w").replace("% w/v", "%w/v")
    s = re.sub(r"(\d)\s+(mg|mcg|ug|g|ml|iu|%)", r"\1\2", s)
    s = re.sub(r"(\d)\s*%\s*w/w", r"\1%w/w", s)
    s = re.sub(r"(\d)\s*%\s*w/v", r"\1%w/v", s)
    # drop trailing parenthetical contamination
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"[^\w./%]+", "", s)

    # Convert standalone grams to mg for mass comparison (1g -> 1000mg),
    # but not when already a ratio involving ml, or percent.
    m = re.fullmatch(r"(\d+(?:\.\d+)?)g", s)
    if m:
        mg = float(m.group(1)) * 1000
        if mg.is_integer():
            s = f"{int(mg)}mg"
        else:
            s = f"{mg}mg"
    return s


def normalize_ingredient(name: str, synonyms: dict[str, str]) -> str:
    n = normalize_whitespace(name).lower()
    n = n.replace("&", " and ")
    # Keep decimal points so leaked strength fragments (e.g. 28.5mg) stay intact.
    n = re.sub(r"[^\w\s\-+/.]", " ", n)
    n = normalize_whitespace(n)
    n = NOISE_WORDS_RE.sub(" ", n)
    n = normalize_whitespace(n)

    # Full-phrase synonym first
    if n in synonyms:
        n = synonyms[n]

    # Strip common salt suffixes once or twice
    for _ in range(2):
        stripped = False
        for suf in SALT_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 2:
                n = n[: -len(suf)].strip()
                stripped = True
                break
        if not stripped:
            break

    if n in synonyms:
        n = synonyms[n]

    # Drop residual form words if any leaked in
    for pattern, _ in DOSAGE_FORMS:
        n = re.sub(rf"\b{re.escape(pattern)}s?\b", " ", n)
    n = normalize_whitespace(n)

    if n in synonyms:
        n = synonyms[n]
    return n


def extract_dosage_form(text: str | None) -> str | None:
    if text is None or (isinstance(text, float) and pd.isna(text)):
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


def strengths_implied_by_name(name: str) -> int:
    """Count mass/percent strength tokens in a brand name (e.g. 100mg/325mg/15mg → 3)."""
    if not name:
        return 0
    return len(NAME_STRENGTH_RATIO_RE.findall(str(name)))


# ---------------------------------------------------------------------------
# Brand parsing
# ---------------------------------------------------------------------------


def parse_brand_parts(parts: list[str], synonyms: dict[str, str]) -> list[Component]:
    components: list[Component] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = BRAND_PART_RE.match(part)
        if m:
            ing = normalize_ingredient(m.group("name"), synonyms)
            strength = normalize_strength_string(m.group("strength"))
            if ing:
                components.append(Component(ing, strength))
            continue
        tokens = STRENGTH_TOKEN_RE.findall(part)
        name_only = STRENGTH_TOKEN_RE.sub(" ", part)
        ing = normalize_ingredient(name_only, synonyms)
        strength = ""
        if tokens:
            num, unit = tokens[0]
            strength = normalize_strength_string(f"{num}{unit}")
        if ing:
            components.append(Component(ing, strength))

    seen: set[tuple[str, str]] = set()
    unique: list[Component] = []
    for c in components:
        key = (c.ingredient, c.strength)
        if c.ingredient and key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def parse_brand_record(
    short1,
    short2,
    salt,
    name: str,
    pack_size_label: str,
    synonyms: dict[str, str],
) -> ParsedMedicine:
    short_parts: list[str] = []
    for val in (short1, short2):
        if pd.notna(val) and str(val).strip():
            short_parts.append(str(val).strip())

    salt_parts: list[str] = []
    if pd.notna(salt) and str(salt).strip():
        salt_parts = [p.strip() for p in str(salt).split("+") if p.strip()]

    incomplete = False
    reason = ""
    notes = []

    # Prefer salt when it exposes more ingredients than shorts.
    if len(salt_parts) > len(short_parts):
        use_parts = salt_parts
        notes.append("composition_source=salt_enriched")
        if len(short_parts) < len(salt_parts):
            notes.append("shorts_incomplete_vs_salt")
    else:
        use_parts = short_parts
        notes.append("composition_source=short")

    components = parse_brand_parts(use_parts, synonyms)
    form = extract_dosage_form(name) or extract_dosage_form(pack_size_label)

    # Safety: brand name implies more actives than exposed composition fields.
    implied = strengths_implied_by_name(name)
    if implied >= 3 and len(components) < implied:
        incomplete = True
        reason = f"name_implies_{implied}_strengths_but_composition_has_{len(components)}"
    elif len(salt_parts) == 0 and implied >= 3 and len(short_parts) < implied:
        incomplete = True
        reason = f"name_implies_{implied}_actives_shorts_only_{len(short_parts)}"

    # Also: if shorts used and salt exists with more parts but we somehow didn't switch
    if len(use_parts) == len(short_parts) and len(salt_parts) > len(short_parts):
        incomplete = True
        reason = "short_fields_incomplete_relative_to_salt"

    return ParsedMedicine(
        components=components,
        dosage_form=form,
        incomplete_composition=incomplete,
        incompleteness_reason=reason,
        parse_notes=";".join(notes),
    )


# ---------------------------------------------------------------------------
# PMBI parsing
# ---------------------------------------------------------------------------


def strip_trailing_form(text: str) -> str:
    """
    Remove trailing dosage-form phrases, including cases where a shared
    volume qualifier follows the form: 'Oral Suspension IP per 5ml'.
    """
    return re.sub(
        r"(?:,?\s*)?(?:Gastro[- ]?resistant|Prolonged[- ]?Release|Sustained[- ]?Release|"
        r"Extended[- ]?Release|Delayed[- ]?Release|Dispersible|Chewable|Soft\s+Gelatin|Softgel)?\s*"
        r"(?:Tablets?|Capsules?|Injections?|Infusions?|Suspensions?|Syrups?|Solutions?|"
        r"Gels?|Creams?|Ointments?|Lotions?|Drops?|Sprays?|Powders?|Granules?|Sachets?|"
        r"Inhalers?|Inhalations?|Respules?|Rotacaps?|Vaccines?|Emulsions?|"
        r"Mouthwash(?:es)?|Gargles?|Patches?)\s*(?:IP|BP|USP|NFI)?"
        r"(?:\s*(?:per|/)\s*\d+(?:\.\d+)?\s*(?:ml|g))?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" ,")


def expand_pmbi_parenthetical_combo(generic: str) -> str:
    """
    Co-trimoxazole (Sulphamethoxazole 800mg and Trimethoprim 160mg) Tablets IP
    → Sulphamethoxazole 800mg and Trimethoprim 160mg Tablets IP
    """
    m = re.match(
        r"^[^()]+\(([^)]+\d[^)]*)\)\s*(.*)$",
        generic.strip(),
        flags=re.IGNORECASE,
    )
    if m and re.search(r"\band\b|,", m.group(1), re.I):
        return normalize_whitespace(f"{m.group(1)} {m.group(2)}")
    return generic


def split_pmbi_clauses(generic_name: str) -> tuple[list[str], str | None]:
    text = normalize_whitespace(generic_name)
    text = expand_pmbi_parenthetical_combo(text)
    # Drop alternate-salt parentheticals that are NOT combo lists
    text = re.sub(r"\(([^)]*)\)", lambda m: "" if not re.search(r"\d", m.group(1)) else m.group(0), text)
    text = normalize_whitespace(text)

    form = extract_dosage_form(text)
    work = strip_trailing_form(text)

    # Trailing modifier in parentheses e.g. (Dispersible)
    work = re.sub(r"\((dispersible|chewable|sr|er|pr|mr)\)\s*$", "", work, flags=re.I).strip()

    if re.search(r"\band\b", work, re.I) or ("," in work and STRENGTH_TOKEN_RE.search(work)):
        clauses = re.split(r"\s+and\s+|,\s*", work, flags=re.IGNORECASE)
        clauses = [c.strip(" ,") for c in clauses if c.strip(" ,")]
        return clauses, form
    return [work if work else text], form


def parse_pmbi_clause(clause: str, synonyms: dict[str, str]) -> Component | None:
    clause = normalize_whitespace(clause)
    if not clause:
        return None

    # Move release modifiers out of the way for strength detection
    clause_wo_release = RELEASE_PREFIX_RE.sub(" ", clause)
    clause_wo_release = normalize_whitespace(clause_wo_release)

    # Pattern: Name ... Form IP Strength
    m = re.match(
        r"^(?P<head>.+?)\s+(?:"
        r"(?:Gastro[- ]?resistant|Prolonged[- ]?Release|Sustained[- ]?Release|"
        r"Extended[- ]?Release|Delayed[- ]?Release|Dispersible|Chewable|"
        r"Soft\s+Gelatin|Softgel)?\s*"
        r"(?:Tablets?|Capsules?|Injections?|Infusions?|Suspensions?|Syrups?|"
        r"Solutions?|Gels?|Creams?|Ointments?|Lotions?|Drops?|Sprays?|"
        r"Powders?|Granules?|Sachets?|Inhalers?|Inhalations?|Respules?|"
        r"Rotacaps?|Vaccines?|Emulsions?|Mouthwash(?:es)?|Gargles?|Patches?)"
        r")\s*(?:IP|BP|USP|NFI)?\s*(?P<strength>.+)$",
        clause_wo_release,
        flags=re.IGNORECASE,
    )
    if m:
        head = m.group("head")
        strength_raw = m.group("strength")
        # If captured 'strength' is only a volume qualifier, recover mass from head.
        if re.match(r"^(?:per|/)", strength_raw, re.I) and not re.search(
            r"\d\s*(?:mg|mcg|g|iu|%)", strength_raw, re.I
        ):
            m_head = re.match(
                r"^(?P<iname>.+?)\s+(?P<s>\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|g|iu|%))"
                r"(?:\s*(?:w/w|w/v))?$",
                head,
                flags=re.IGNORECASE,
            )
            if m_head:
                ing = normalize_ingredient(m_head.group("iname"), synonyms)
                strength = normalize_strength_string(
                    m_head.group("s") + "/" + re.sub(r"^(?:per|/)\s*", "", strength_raw, flags=re.I)
                )
                if ing:
                    return Component(ing, strength)
        embedded = STRENGTH_TOKEN_RE.search(head)
        if embedded and re.match(r"^(?:per|/)", strength_raw, re.I):
            ing = normalize_ingredient(STRENGTH_TOKEN_RE.sub(" ", head), synonyms)
            strength = normalize_strength_string(embedded.group(0))
            per = re.search(r"(?:per|/)\s*(\d+(?:\.\d+)?\s*(?:ml|g))", strength_raw, re.I)
            if per and "/" not in strength:
                strength = normalize_strength_string(strength + "/" + per.group(1))
            if ing:
                return Component(ing, strength)
        ing = normalize_ingredient(head, synonyms)
        strength = normalize_strength_string(strength_raw)
        if ing:
            return Component(ing, strength)

    # Pattern: Ingredient Strength [per X]
    m2 = re.match(
        r"^(?P<head>.+?)\s+(?P<strength>\d+(?:\.\d+)?\s*(?:mcg|µg|ug|mg|g|ml|iu|%)"
        r"(?:\s*(?:w/w|w/v))?(?:\s*(?:per|/)\s*\d+(?:\.\d+)?\s*(?:ml|g))?)",
        clause_wo_release,
        flags=re.IGNORECASE,
    )
    if m2:
        head = m2.group("head")
        # If head ends with leftover strength fragments from bad splits, clean
        ing = normalize_ingredient(head, synonyms)
        strength = normalize_strength_string(m2.group("strength"))
        # Attach trailing "per 5ml" left on clause after the matched strength if present
        rest = clause_wo_release[m2.end() :].strip()
        per = re.match(r"^(?:IP\s+)?(?:per|/)\s*(\d+(?:\.\d+)?\s*(?:ml|g))", rest, re.I)
        if per and "/" not in strength:
            strength = normalize_strength_string(strength + "/" + per.group(1))
        if ing:
            return Component(ing, strength)

    # Ingredient only
    ing = normalize_ingredient(clause_wo_release, synonyms)
    if ing and len(ing) > 2:
        return Component(ing, "")
    return None


def parse_pmbi_generic(generic_name: str, synonyms: dict[str, str]) -> ParsedMedicine:
    clauses, form = split_pmbi_clauses(str(generic_name))
    components: list[Component] = []
    notes = []

    for clause in clauses:
        # Handle "Amoxycillin 200mg ... Oral Suspension IP per 5ml" where per 5ml is shared
        comp = parse_pmbi_clause(clause, synonyms)
        if comp:
            components.append(comp)

    # Shared trailing per-volume on combo suspensions:
    # "Amoxycillin 200mg and Potassium Clavulanate 28.5mg Oral Suspension IP per 5ml"
    shared_per = re.search(
        r"(?:per|/)\s*(\d+(?:\.\d+)?\s*ml)\s*$",
        str(generic_name),
        flags=re.IGNORECASE,
    )
    if shared_per and components:
        per_tok = normalize_strength_string(shared_per.group(1))
        fixed: list[Component] = []
        for c in components:
            if c.strength and "/" not in c.strength and re.search(r"(mg|mcg|g)$", c.strength):
                fixed.append(Component(c.ingredient, f"{c.strength}/{per_tok}"))
                notes.append("shared_per_volume_applied")
            else:
                fixed.append(c)
        components = fixed

    seen: set[tuple[str, str]] = set()
    unique: list[Component] = []
    for c in components:
        key = (c.ingredient, c.strength)
        if c.ingredient and key not in seen:
            seen.add(key)
            unique.append(c)

    return ParsedMedicine(
        components=unique,
        dosage_form=form,
        incomplete_composition=False,
        incompleteness_reason="",
        parse_notes=";".join(dict.fromkeys(notes)),
    )


# ---------------------------------------------------------------------------
# Matching / classification
# ---------------------------------------------------------------------------


def pairwise_strength_agreement(pmbi: list[Component], brand: list[Component]) -> tuple[int, int]:
    """Return (matched_pairs, comparable_pairs)."""
    matched = 0
    comparable = 0
    brand_by_ing = defaultdict(list)
    for b in brand:
        brand_by_ing[b.ingredient].append(b)
    used = set()
    for p in pmbi:
        candidates = brand_by_ing.get(p.ingredient, [])
        best = None
        for b in candidates:
            if id(b) in used:
                continue
            if p.strength and b.strength:
                comparable += 1
                if p.strength == b.strength:
                    matched += 1
                    best = b
                    break
            elif not p.strength or not b.strength:
                # missing strength on one side — not a comparable contradiction
                best = b
                break
        if best is not None:
            used.add(id(best))
    return matched, comparable


def classify_match(pmbi: ParsedMedicine, brand: ParsedMedicine) -> dict:
    """
    Returns match_method, match_confidence, validation_status, rejection_reason.
    Statuses: Strong | Moderate | Ambiguous | Rejected
    """
    if not pmbi.components or not brand.components:
        return {
            "match_method": "none",
            "match_confidence": 0.0,
            "validation_status": "Rejected",
            "rejection_reason": "missing_parsed_composition",
            "audit_flags": "",
        }

    flags: list[str] = []
    if brand.incomplete_composition:
        flags.append(f"brand_incomplete:{brand.incompleteness_reason}")

    pmbi_ings = pmbi.ingredient_key
    brand_ings = brand.ingredient_key
    if pmbi_ings != brand_ings:
        return {
            "match_method": "none",
            "match_confidence": 0.0,
            "validation_status": "Rejected",
            "rejection_reason": "ingredient_set_mismatch",
            "audit_flags": ";".join(flags),
        }

    # Incomplete brand composition must never be Strong against a smaller/equal exposed set
    if brand.incomplete_composition:
        return {
            "match_method": "incomplete_composition_blocked",
            "match_confidence": 0.25,
            "validation_status": "Rejected",
            "rejection_reason": brand.incompleteness_reason or "incomplete_brand_composition",
            "audit_flags": ";".join(flags),
        }

    matched, comparable = pairwise_strength_agreement(pmbi.components, brand.components)
    all_strengths_present = all(c.strength for c in pmbi.components) and all(
        c.strength for c in brand.components
    )
    full_key_match = (
        all_strengths_present and pmbi.composition_key == brand.composition_key
    )

    form_pmbi = pmbi.dosage_form
    form_brand = brand.dosage_form
    form_ok = bool(form_pmbi and form_brand and form_pmbi == form_brand)
    form_mismatch = bool(form_pmbi and form_brand and form_pmbi != form_brand)
    form_unknown = not form_pmbi or not form_brand

    if form_mismatch:
        flags.append(f"form_mismatch:{form_pmbi}!={form_brand}")

    # Strength contradiction: same ingredients, comparable strengths that disagree
    if comparable > 0 and matched < comparable and not full_key_match:
        # Partial strength agreement
        if matched == 0:
            return {
                "match_method": "ingredient_set_strength_conflict",
                "match_confidence": 0.15,
                "validation_status": "Rejected",
                "rejection_reason": "strength_conflict",
                "audit_flags": ";".join(flags),
            }

    if full_key_match and form_ok:
        return {
            "match_method": "exact_composition_strength_form",
            "match_confidence": 0.95,
            "validation_status": "Strong",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    if full_key_match and form_unknown:
        return {
            "match_method": "exact_composition_strength",
            "match_confidence": 0.88,
            "validation_status": "Strong",
            "rejection_reason": "",
            "audit_flags": ";".join(flags + ["form_unknown"]),
        }

    if full_key_match and form_mismatch:
        return {
            "match_method": "exact_composition_strength_form_mismatch",
            "match_confidence": 0.55,
            "validation_status": "Ambiguous",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    if (
        all_strengths_present
        and matched == len(pmbi.components) == len(brand.components)
        and form_ok
    ):
        return {
            "match_method": "ingredient_strength_pairwise_form",
            "match_confidence": 0.90,
            "validation_status": "Strong",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    if matched == len(pmbi.components) and matched == len(brand.components) and all_strengths_present:
        status = "Moderate" if form_mismatch or form_unknown else "Strong"
        conf = 0.70 if form_mismatch else 0.85
        return {
            "match_method": "ingredient_strength_pairwise",
            "match_confidence": conf,
            "validation_status": status if status != "Strong" or not form_mismatch else "Moderate",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    # Ingredients agree; strengths incomplete
    if not all_strengths_present:
        if form_ok:
            return {
                "match_method": "ingredient_set_form_partial_strength",
                "match_confidence": 0.50,
                "validation_status": "Moderate",
                "rejection_reason": "",
                "audit_flags": ";".join(flags + ["partial_strength"]),
            }
        return {
            "match_method": "ingredient_set_partial_strength",
            "match_confidence": 0.40,
            "validation_status": "Ambiguous",
            "rejection_reason": "",
            "audit_flags": ";".join(flags + ["partial_strength"]),
        }

    # Ingredients agree; some strengths match
    if matched > 0:
        return {
            "match_method": "ingredient_partial_strength",
            "match_confidence": 0.45,
            "validation_status": "Ambiguous",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    # Ingredients only
    if form_ok:
        return {
            "match_method": "ingredient_set_form_only",
            "match_confidence": 0.35,
            "validation_status": "Ambiguous",
            "rejection_reason": "",
            "audit_flags": ";".join(flags),
        }

    return {
        "match_method": "ingredient_set_only",
        "match_confidence": 0.25,
        "validation_status": "Ambiguous",
        "rejection_reason": "",
        "audit_flags": ";".join(flags),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def profile_lines(df: pd.DataFrame, title: str) -> list[str]:
    lines = [f"=== {title} ===", f"Row count: {len(df):,}", f"Columns: {list(df.columns)}", ""]
    lines.append("Missing-value counts:")
    for col, cnt in df.isna().sum().items():
        lines.append(f"  - {col}: {int(cnt):,} ({cnt / max(len(df), 1) * 100:.2f}%)")
    lines.append(f"Fully duplicate rows: {int(df.duplicated().sum()):,}")
    lines.append("")
    return lines


def run_sample_sanity(synonyms: dict[str, str]) -> list[str]:
    """Quick parser sanity checks before full run."""
    lines = ["=== Sample parser sanity checks ==="]
    cases = [
        "Aceclofenac 100mg and Paracetamol 325mg Tablets",
        "Aceclofenac Tablets IP 100 mg",
        "Aciclovir Tablets IP 400 mg",
        "Cephalexin Capsules IP 250mg",
        "Amoxycillin 200mg and Potassium Clavulanate 28.5mg Oral Suspension IP per 5ml",
        "Co-trimoxazole (Sulphamethoxazole 800mg and Trimethoprim 160mg) Tablets IP",
        "Paracetamol Paediatric Oral Suspension IP 125 mg per 5 ml",
        "Metformin Hydrochloride Prolonged-release 500mg and Glimepiride 2mg Tablets IP",
        "Diclofenac Gel IP 1.16%w/w (Diclofenac Diethylamine)",
        "Ceftriaxone 1g and Tazobactam 125mg Injection",
    ]
    for g in cases:
        p = parse_pmbi_generic(g, synonyms)
        lines.append(f"  PMBI: {g}")
        lines.append(f"    -> {p.composition_summary} | form={p.dosage_form}")

    brand_cases = [
        ("Augmentin 625 Duo Tablet", "Amoxycillin  (500mg) ", " Clavulanic Acid (125mg)", None, "strip of 10 tablets"),
        ("A Pain Plus 100mg/325mg/250mg Tablet", "Aceclofenac (100mg)", " Paracetamol (325mg)", None, "strip of 10 tablets"),
        ("Azithral 500 Tablet", "Azithromycin (500mg)", None, None, "strip of 5 tablets"),
        ("Acivir 400 DT Tablet", "Acyclovir (400mg)", None, None, "strip of 5 tablets"),
    ]
    for name, s1, s2, salt, pack in brand_cases:
        b = parse_brand_record(s1, s2, salt, name, pack, synonyms)
        lines.append(
            f"  BRAND: {name} -> {b.composition_summary} | form={b.dosage_form}"
            f" | incomplete={b.incomplete_composition} ({b.incompleteness_reason})"
        )

    # Classification sanity: incomplete brand vs 2-ing PMBI must be Rejected
    pmbi = parse_pmbi_generic("Aceclofenac 100mg and Paracetamol 325mg Tablets", synonyms)
    brand_bad = parse_brand_record(
        "Aceclofenac (100mg)",
        " Paracetamol (325mg)",
        None,
        "A Pain Plus 100mg/325mg/250mg Tablet",
        "strip of 10 tablets",
        synonyms,
    )
    decision = classify_match(pmbi, brand_bad)
    lines.append(
        f"  SAFETY: incomplete 3-hint brand vs 2-ing PMBI => {decision['validation_status']}"
        f" ({decision['rejection_reason'] or decision['match_method']})"
    )
    assert decision["validation_status"] == "Rejected", decision

    brand_good = parse_brand_record(
        "Aceclofenac (100mg)",
        " Paracetamol (325mg)",
        None,
        "A Clo P 100mg/325mg Tablet",
        "strip of 10 tablets",
        synonyms,
    )
    decision2 = classify_match(pmbi, brand_good)
    lines.append(
        f"  SAFETY: complete 2-ing brand vs 2-ing PMBI => {decision2['validation_status']}"
        f" ({decision2['match_method']})"
    )
    assert decision2["validation_status"] == "Strong", decision2

    # Synonym resolve
    pmbi_ac = parse_pmbi_generic("Aciclovir Tablets IP 400 mg", synonyms)
    brand_ac = parse_brand_record("Acyclovir (400mg)", None, None, "Acivir 400 Tablet", "strip of 5 tablets", synonyms)
    decision3 = classify_match(pmbi_ac, brand_ac)
    lines.append(
        f"  SYNONYM: Aciclovir/Acyclovir 400mg => {decision3['validation_status']}"
        f" ({decision3['match_method']})"
    )
    lines.append("")
    return lines


def _log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    synonyms = load_synonyms(SYNONYM_PATH)
    report: list[str] = []
    report.append("Jan Aushadhi — Data Preparation & Match Validation Report")
    report.append("=" * 70)
    report.append("")
    report.append("Scope: cleaning → normalization → candidate matching → validation.")
    report.append("Out of scope: SQLite, FastAPI, frontend, ML, deployment.")
    report.append("")
    report.append(
        "Interpretation: unmatched ≠ medically unavailable brand equivalent; "
        "it means no record matched under current rules/data."
    )
    report.append(
        "Candidate matches are data-derived candidates, not medical recommendations."
    )
    report.append("")

    # Sample sanity first
    _log("Running sample sanity checks...")
    sanity = run_sample_sanity(synonyms)
    report.extend(sanity)
    for line in sanity:
        _log(line)

    _log("Loading source CSVs (read-only)...")
    t0 = time.time()
    pmbi_raw = pd.read_csv(PMBI_PATH)
    brand_raw = pd.read_csv(BRAND_PATH)
    report.extend(profile_lines(pmbi_raw, "PMBI source (read-only)"))
    report.extend(profile_lines(brand_raw, "Brand source (read-only)"))

    disc = int(brand_raw["Is_discontinued"].fillna(False).astype(bool).sum())
    brand_active = brand_raw.loc[~brand_raw["Is_discontinued"].fillna(False).astype(bool)].copy()
    report.append("=== Discontinued filter ===")
    report.append(f"Total brand records: {len(brand_raw):,}")
    report.append(f"Discontinued excluded: {disc:,}")
    report.append(f"Active brand records: {len(brand_active):,}")
    report.append(f"Verified synonyms loaded: {len(synonyms):,}")
    report.append("")

    # ---- Normalize PMBI once ----
    _log("Parsing/normalizing PMBI...")
    pmbi_parsed: list[ParsedMedicine] = []
    pmbi_rows: list[dict] = []
    drug_codes = pmbi_raw["Drug Code"].tolist()
    sr_nos = pmbi_raw["Sr No"].tolist()
    generics = pmbi_raw["Generic Name"].astype(str).tolist()
    unit_sizes = pmbi_raw["Unit Size"].tolist()
    mrps = pmbi_raw["MRP"].tolist()
    groups = pmbi_raw["Group Name"].tolist()
    for i, generic_name in enumerate(generics):
        parsed = parse_pmbi_generic(generic_name, synonyms)
        pmbi_parsed.append(parsed)
        pmbi_rows.append(
            {
                "drug_code": drug_codes[i],
                "sr_no": sr_nos[i],
                "generic_name": generic_name,
                "unit_size": unit_sizes[i],
                "mrp": mrps[i],
                "group_name": groups[i],
                "norm_ingredients": " | ".join(sorted(parsed.ingredient_key)),
                "norm_composition": parsed.composition_summary,
                "norm_strengths": parsed.strength_summary,
                "norm_dosage_form": parsed.dosage_form or "",
                "ingredient_count": len(parsed.components),
                "parse_notes": parsed.parse_notes,
            }
        )
    pmbi_clean = pd.DataFrame(pmbi_rows)
    pmbi_clean.to_csv(PMBI_CLEAN_PATH, index=False)
    _log(f"Wrote {PMBI_CLEAN_PATH} ({len(pmbi_clean):,} rows)")

    # ---- Normalize brands once (column arrays; no DataFrame iloc in hot loops) ----
    _log("Parsing/normalizing active brands...")
    b_ids = brand_active["id"].tolist()
    b_names = brand_active["name"].astype(str).tolist()
    b_prices = brand_active["price"].tolist()
    b_mans = brand_active["manufacturer_name"].astype(str).tolist()
    b_types = brand_active["type"].astype(str).tolist()
    b_packs = brand_active["pack_size_label"].astype(str).tolist()
    b_s1 = brand_active["short_composition1"].tolist()
    b_s2 = brand_active["short_composition2"].tolist()
    b_salt = brand_active["salt_composition"].tolist()

    brand_parsed: list[ParsedMedicine] = []
    brand_rows: list[dict] = []
    n_brands = len(b_ids)
    for i in range(n_brands):
        if i and i % 50000 == 0:
            _log(f"  ...parsed {i:,}/{n_brands:,} brands")
        parsed = parse_brand_record(b_s1[i], b_s2[i], b_salt[i], b_names[i], b_packs[i], synonyms)
        brand_parsed.append(parsed)
        s1 = "" if pd.isna(b_s1[i]) else str(b_s1[i]).strip()
        s2 = "" if pd.isna(b_s2[i]) else str(b_s2[i]).strip()
        salt = "" if pd.isna(b_salt[i]) else str(b_salt[i]).strip()
        brand_comp_raw = " + ".join(x for x in [s1, s2] if x)
        brand_rows.append(
            {
                "brand_id": b_ids[i],
                "name": b_names[i],
                "price": b_prices[i],
                "manufacturer_name": b_mans[i],
                "type": b_types[i],
                "pack_size_label": b_packs[i],
                "short_composition1": s1,
                "short_composition2": s2,
                "salt_composition": salt,
                "brand_composition_raw": brand_comp_raw,
                "norm_ingredients": " | ".join(sorted(parsed.ingredient_key)),
                "norm_composition": parsed.composition_summary,
                "norm_strengths": parsed.strength_summary,
                "norm_dosage_form": parsed.dosage_form or "",
                "ingredient_count": len(parsed.components),
                "incomplete_composition": parsed.incomplete_composition,
                "incompleteness_reason": parsed.incompleteness_reason,
                "parse_notes": parsed.parse_notes,
            }
        )
    brands_clean = pd.DataFrame(brand_rows)
    brands_clean.to_csv(BRANDS_CLEAN_PATH, index=False)
    _log(f"Wrote {BRANDS_CLEAN_PATH} ({len(brands_clean):,} rows)")

    incomplete_brand_n = int(brands_clean["incomplete_composition"].sum())
    report.append("=== Normalization summary ===")
    report.append(f"PMBI parsed with ≥1 ingredient: {int((pmbi_clean['ingredient_count'] > 0).sum()):,}")
    report.append(f"PMBI with dosage form detected: {int((pmbi_clean['norm_dosage_form'] != '').sum()):,}")
    report.append(f"Brands parsed with ≥1 ingredient: {int((brands_clean['ingredient_count'] > 0).sum()):,}")
    report.append(f"Brands flagged incomplete composition: {incomplete_brand_n:,}")
    report.append("")

    # ---- Indexed candidate matching (NO Cartesian product) ----
    _log("Building ingredient-set index (candidate-based matching)...")
    brand_by_ing: dict[frozenset[str], list[int]] = defaultdict(list)
    for i, parsed in enumerate(brand_parsed):
        if parsed.ingredient_key:
            brand_by_ing[parsed.ingredient_key].append(i)

    _log("Matching via ingredient-set lookup...")
    match_rows: list[dict] = []
    status_counter = Counter()
    method_counter = Counter()
    pmbi_best_status: dict[int, str] = {}

    STATUS_RANK = {"Strong": 3, "Moderate": 2, "Ambiguous": 1, "Rejected": 0}
    MAX_KEEP_PER_PMBI = 50

    for p_idx, p in enumerate(pmbi_parsed):
        prow = pmbi_rows[p_idx]
        drug_code = int(prow["drug_code"])
        if not p.ingredient_key:
            pmbi_best_status[drug_code] = "none"
            continue

        candidate_idxs = brand_by_ing.get(p.ingredient_key, [])
        if not candidate_idxs:
            pmbi_best_status[drug_code] = "none"
            continue

        local: list[dict] = []
        for b_idx in candidate_idxs:
            b = brand_parsed[b_idx]
            brow = brand_rows[b_idx]
            decision = classify_match(p, b)
            status = decision["validation_status"]
            status_counter[status] += 1
            method_counter[decision["match_method"]] += 1

            local.append(
                {
                    "brand_id": int(brow["brand_id"]),
                    "brand_name": brow["name"],
                    "brand_composition": brow["brand_composition_raw"],
                    "brand_norm_composition": brow["norm_composition"],
                    "brand_strength": brow["norm_strengths"],
                    "brand_dosage_form": brow["norm_dosage_form"],
                    "brand_price": brow["price"],
                    "brand_manufacturer": brow["manufacturer_name"],
                    "brand_pack_size": brow["pack_size_label"],
                    "brand_incomplete_composition": bool(brow["incomplete_composition"]),
                    "pmbi_drug_code": drug_code,
                    "pmbi_generic_name": prow["generic_name"],
                    "pmbi_norm_composition": prow["norm_composition"],
                    "pmbi_strength": prow["norm_strengths"],
                    "pmbi_dosage_form": prow["norm_dosage_form"],
                    "pmbi_unit_size": prow["unit_size"],
                    "pmbi_mrp": prow["mrp"],
                    "pmbi_group_name": prow["group_name"],
                    "match_method": decision["match_method"],
                    "match_confidence": decision["match_confidence"],
                    "validation_status": status,
                    "rejection_reason": decision["rejection_reason"],
                    "audit_flags": decision["audit_flags"],
                }
            )

            prev = pmbi_best_status.get(drug_code, "none")
            if STATUS_RANK.get(status, -1) > STATUS_RANK.get(prev, -1):
                pmbi_best_status[drug_code] = status

        local.sort(
            key=lambda r: (
                -STATUS_RANK.get(r["validation_status"], -1),
                -r["match_confidence"],
                r["brand_name"],
            )
        )
        match_rows.extend(local[:MAX_KEEP_PER_PMBI])

    matches_df = pd.DataFrame(match_rows)
    matches_df.to_csv(MATCHES_PATH, index=False)
    _log(f"Wrote {MATCHES_PATH} ({len(matches_df):,} rows)")

    # ---- Statistics ----
    n_pmbi = len(pmbi_raw)
    n_brand = len(brand_raw)
    n_active = len(brand_active)
    n_candidates = len(matches_df)

    def count_status(status: str) -> int:
        if n_candidates == 0:
            return 0
        return int((matches_df["validation_status"] == status).sum())

    n_strong = count_status("Strong")
    n_moderate = count_status("Moderate")
    n_ambiguous = count_status("Ambiguous")
    n_rejected = count_status("Rejected")

    pmbi_with_strong = {c for c, s in pmbi_best_status.items() if s == "Strong"}
    pmbi_with_moderate_only = {
        c for c, s in pmbi_best_status.items() if s in {"Moderate", "Ambiguous"}
    }
    pmbi_with_any_non_rejected = pmbi_with_strong | pmbi_with_moderate_only
    pmbi_no_usable = set(int(x) for x in drug_codes) - pmbi_with_any_non_rejected

    report.append("=== Matching statistics (validation phase) ===")
    report.append(f"Total PMBI records: {n_pmbi:,}")
    report.append(f"Total brand records: {n_brand:,}")
    report.append(f"Discontinued brand records: {disc:,}")
    report.append(f"Active brand records: {n_active:,}")
    report.append(f"Candidate match rows written (≤{MAX_KEEP_PER_PMBI}/PMBI): {n_candidates:,}")
    report.append(f"Strong match rows: {n_strong:,}")
    report.append(f"Moderate match rows: {n_moderate:,}")
    report.append(f"Ambiguous match rows: {n_ambiguous:,}")
    report.append(f"Rejected match rows (retained for audit): {n_rejected:,}")
    report.append(
        f"PMBI with ≥1 Strong candidate: {len(pmbi_with_strong):,}"
        f" ({len(pmbi_with_strong)/n_pmbi*100:.1f}%)"
    )
    report.append(
        f"PMBI with only Moderate/Ambiguous candidates: {len(pmbi_with_moderate_only):,}"
        f" ({len(pmbi_with_moderate_only)/n_pmbi*100:.1f}%)"
    )
    report.append(
        f"PMBI with no usable candidate: {len(pmbi_no_usable):,}"
        f" ({len(pmbi_no_usable)/n_pmbi*100:.1f}%)"
    )
    report.append("")
    report.append("Match method distribution:")
    for k, v in method_counter.most_common():
        report.append(f"  - {k}: {v:,}")
    report.append("")

    report.append("=== Comparison vs initial experiment ===")
    report.append("Initial experiment:")
    report.append("  - Strong candidate rows: 33,364")
    report.append("  - Moderate candidate rows: 19,856")
    report.append("  - PMBI with ≥1 strong: 1,241 (50.9%)")
    report.append("  - PMBI moderate/ambiguous only: 139")
    report.append("  - PMBI with no candidate: 1,059 (43.4%)")
    report.append("  - Known issue: ~125 incomplete-composition false strong rows")
    report.append("Validation phase:")
    report.append(f"  - Strong rows: {n_strong:,}")
    report.append(f"  - Moderate rows: {n_moderate:,}")
    report.append(f"  - Ambiguous rows: {n_ambiguous:,}")
    report.append(f"  - Rejected rows: {n_rejected:,}")
    report.append(
        f"  - PMBI with ≥1 Strong: {len(pmbi_with_strong):,}"
        f" ({len(pmbi_with_strong)/n_pmbi*100:.1f}%)"
    )
    report.append(f"  - PMBI Moderate/Ambiguous only: {len(pmbi_with_moderate_only):,}")
    report.append(
        f"  - PMBI no usable candidate: {len(pmbi_no_usable):,}"
        f" ({len(pmbi_no_usable)/n_pmbi*100:.1f}%)"
    )
    report.append(f"  - Brands flagged incomplete composition: {incomplete_brand_n:,}")
    report.append("")

    def add_examples(title: str, df: pd.DataFrame, n: int = 8) -> None:
        report.append(title)
        if df.empty:
            report.append("  (none)")
            report.append("")
            return
        for _, r in df.head(n).iterrows():
            report.append(
                f"  - brand[{r['brand_id']}] {r['brand_name']} | {r['brand_composition']}"
                f"  <->  PMBI[{r['pmbi_drug_code']}] {r['pmbi_generic_name']}"
                f" | {r['validation_status']} {r['match_method']} conf={r['match_confidence']}"
                f" flags={r['audit_flags'] or r['rejection_reason'] or '-'}"
            )
        report.append("")

    if n_candidates:
        add_examples(
            "Example Strong matches:",
            matches_df[matches_df["validation_status"] == "Strong"].sort_values(
                "match_confidence", ascending=False
            ),
        )
        syn_mask = matches_df["pmbi_norm_composition"].str.contains(
            "acyclovir|cefalexin|clavulanic acid|sulfamethoxazole",
            case=False,
            na=False,
        ) & (matches_df["validation_status"] == "Strong")
        add_examples("Example newly resolved matches (synonym-sensitive):", matches_df[syn_mask])
        add_examples(
            "Example Moderate matches:",
            matches_df[matches_df["validation_status"] == "Moderate"],
        )
        add_examples(
            "Example Ambiguous matches:",
            matches_df[matches_df["validation_status"] == "Ambiguous"],
        )
        add_examples(
            "Example Rejected matches (incl. incomplete-composition safety):",
            matches_df[matches_df["validation_status"] == "Rejected"],
        )

    report.append("Example remaining unmatched PMBI products (up to 25):")
    unmatched = pmbi_clean[pmbi_clean["drug_code"].astype(int).isin(pmbi_no_usable)].head(25)
    for _, r in unmatched.iterrows():
        report.append(
            f"  - [{r['drug_code']}] {r['generic_name']} | parsed={r['norm_composition']!r}"
            f" form={r['norm_dosage_form']}"
        )
    report.append("")
    report.append(
        "NOTE: Unmatched means no matching record was found in the current datasets "
        "using the current matching rules — not that no branded equivalent exists."
    )
    report.append("")

    report.append("=== Remaining data-quality problems ===")
    report.append("  - Brand short_composition1/2 still expose at most 2 actives; salt rarely filled.")
    report.append("  - Incomplete-composition heuristic uses name strength-token counts; may miss some cases.")
    report.append("  - Modified-release distinctions collapsed into base dosage form.")
    report.append("  - Pack size / unit size not yet used for match strength.")
    report.append("  - Some PMBI multi-vitamin / vaccine / ORS strings remain hard to structure.")
    report.append("  - Ingredient synonym list is intentionally small; more verified aliases may help coverage.")
    report.append("  - Percent (w/w, w/v) and mcg inhaler strengths need careful unit equality rules.")
    report.append("")

    report.append("=== Recommendation: what the final database should store ===")
    report.append("  1. Raw-preserving entities:")
    report.append("     - pmbi_products (drug_code PK, generic_name, unit_size, mrp, group_name)")
    report.append("     - brand_medicines (brand_id PK, name, price, manufacturer, pack_size, type,")
    report.append("       short_composition1/2, salt_composition, is_discontinued)")
    report.append("  2. Normalized derived tables (rebuildable from raw):")
    report.append("     - pmbi_normalized / brand_normalized (ingredients JSON, strengths, dosage_form,")
    report.append("       incomplete_composition flag, parse_notes)")
    report.append("     - medicine_components (entity_type, entity_id, ingredient_canonical, strength_norm, seq)")
    report.append("  3. Match audit tables:")
    report.append("     - match_candidates (brand_id, drug_code, method, confidence, validation_status,")
    report.append("       rejection_reason, audit_flags, created_at)")
    report.append("     - Prefer storing Strong (+ optionally Moderate) for app use; keep Ambiguous/Rejected")
    report.append("       in an audit table, not in user-facing price comparison results.")
    report.append("  4. Reference data:")
    report.append("     - ingredient_synonyms (explicit audited map)")
    report.append("  5. Do NOT treat match_candidates as clinical equivalence authority.")
    report.append("")

    elapsed = time.time() - t0
    report.append(f"Runtime: {elapsed:.1f}s")
    report.append("Outputs:")
    for p in [BRANDS_CLEAN_PATH, PMBI_CLEAN_PATH, MATCHES_PATH, REPORT_PATH, SYNONYM_PATH]:
        report.append(f"  - {p}")

    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")
    _log(f"Wrote {REPORT_PATH}")
    _log(f"Done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
