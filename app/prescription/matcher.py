"""
Match noisy prescription lines against the brand and PMBI dictionaries.

OCR rarely gets a brand name exactly right ("Augmentn 625", "Azithra1"), so
each line is parsed into name words, strengths and a dosage-form hint, then
fuzzy-matched against all brand names in the database. Lines written as a
generic name ("Paracetamol 650") are matched to PMBI products by ingredient.

Scores are text-similarity scores (0-100), not clinical confidence.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

FORM_ALIASES = {
    "tab": "tablet", "tabs": "tablet", "tb": "tablet", "tablet": "tablet", "tablets": "tablet",
    "cap": "capsule", "caps": "capsule", "capsule": "capsule", "capsules": "capsule",
    "syp": "syrup", "syr": "syrup", "syrup": "syrup",
    "inj": "injection", "injection": "injection",
    "susp": "suspension", "suspension": "suspension",
    "oint": "ointment", "ointment": "ointment",
    "cream": "cream", "gel": "gel", "lotion": "lotion",
    "drop": "drop", "drops": "drop", "gtt": "drop",
    "sol": "solution", "soln": "solution", "solution": "solution",
    "spray": "spray", "inhaler": "inhaler", "powder": "powder", "sachet": "sachet",
    "respule": "respule", "respules": "respule", "rotacap": "rotacap", "rotacaps": "rotacap",
    "infusion": "infusion", "mouthwash": "mouthwash", "gargle": "gargle", "patch": "patch",
}
# Words that describe the pack or form inside brand names but don't identify the brand.
BRAND_FORM_WORDS = set(FORM_ALIASES) | {
    "oral", "dry", "eye", "ear", "nasal", "kit", "soap", "shampoo", "liquid",
    "expectorant", "vaccine", "granules", "granule", "nebule", "emulsion", "wash",
}
UNITS = {"mg", "ml", "mcg", "gm", "g", "iu", "kg", "w", "v", "l", "lac", "mega"}
# Dosing instructions and prescription boilerplate.
NOISE = {
    "rx", "od", "bd", "bid", "tds", "tid", "qid", "qds", "hs", "sos", "prn", "stat", "ac", "pc",
    "days", "day", "wk", "wks", "week", "weeks", "month", "months", "daily", "once",
    "twice", "thrice", "times", "morning", "noon", "afternoon", "evening", "night", "bedtime",
    "after", "before", "food", "meal", "meals", "with", "water", "empty", "stomach", "take",
    "apply", "for", "and", "then", "continue", "till", "to", "of", "the", "at", "in", "on",
    "doctor", "name", "male", "female", "no", "dx", "complaints", "advice", "adv", "follow",
    "review", "sign", "wt", "temp", "yrs", "years", "yr", "sig",
}
# Labels that only appear in the letterhead / patient block, never on a medicine line.
HEADER_MARKERS = {
    "mbbs", "patient", "age", "sex", "date", "address", "phone", "mobile", "mob", "tel",
    "reg", "regd", "registration", "clinic", "hospital", "diagnosis", "signature",
    "weight", "bp", "pulse", "email",
}

SCHEDULE_RE = re.compile(r"\b\d\s*[-+]\s*\d\s*[-+]\s*\d(?:\s*[-+]\s*\d)?\b")
DURATION_RE = re.compile(r"\b\d+\s*(?:days?|d|weeks?|wks?|months?|times?)\b")
TIMES_RE = re.compile(r"\bx\s*\d+\b")
DATE_RE = re.compile(r"\b\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{2,4}\b")
# "10ml" on a syrup line is the dose volume; "125mg/5ml" is a strength and is kept.
DOSE_VOLUME_RE = re.compile(r"(?<![/\d.])\d+(?:\.\d+)?\s*(?:ml|tsp|drops?)\b")
LIST_MARKER_RE = re.compile(r"^\s*(?:℞|rx\b\.?|\d{1,2}\s*[.)\]:-](?!\d))\s*")
TOKEN_RE = re.compile(r"[a-z]+|\d+(?:\.\d+)?")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

KEY_CUTOFF = 72
# The brand's leading word must closely match one written on the prescription.
MIN_KEY_SCORE = 75
CONFIDENT_KEY_SCORE = 85
GENERIC_CUTOFF = 88
CONFIDENT_SCORE = 88
REVIEW_SCORE = 75
# Lines without a form word or strength are often names, addresses or notes.
NO_CUE_MIN_SCORE = 92
LOW_OCR_CONFIDENCE = 0.5


@dataclass
class ParsedLine:
    raw: str
    words: list[str]
    numbers: set[float]
    form: str | None
    has_cue: bool


def _to_number(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_line(text: str) -> ParsedLine:
    lowered = text.lower()
    if HEADER_MARKERS & set(TOKEN_RE.findall(lowered)):
        return ParsedLine(raw=text, words=[], numbers=set(), form=None, has_cue=False)
    has_cue = bool(re.match(r"^\s*(?:℞|rx\b)", lowered))
    cleaned = DATE_RE.sub(" ", lowered)
    cleaned = LIST_MARKER_RE.sub(" ", cleaned)
    # TIMES_RE first: "x 5 days" must go as a whole, not leave a stray "x".
    for pattern in (SCHEDULE_RE, TIMES_RE, DURATION_RE, DOSE_VOLUME_RE):
        cleaned = pattern.sub(" ", cleaned)

    words: list[str] = []
    numbers: set[float] = set()
    form = None
    for token in TOKEN_RE.findall(cleaned):
        if token[0].isdigit():
            value = _to_number(token)
            if value is not None:
                numbers.add(value)
            continue
        if token in FORM_ALIASES:
            form = form or FORM_ALIASES[token]
            continue
        if token in UNITS or token in NOISE:
            continue
        words.append(token)
    has_cue = has_cue or form is not None or bool(numbers)
    return ParsedLine(raw=text, words=words, numbers=numbers, form=form, has_cue=has_cue)


def _norm_strength(strength: str | None) -> str:
    return re.sub(r"\s+", "", (strength or "").lower())


def _brand_words(name: str) -> list[str]:
    return [
        t for t in TOKEN_RE.findall(name.lower())
        if not t[0].isdigit() and t not in BRAND_FORM_WORDS and t not in UNITS
    ]


@dataclass
class BrandEntry:
    brand_id: int
    name: str
    words: list[str]
    name_numbers: set[float]
    # Name numbers plus ingredient strengths ("Pan-D" carries 40mg + 30mg).
    numbers: set[float]
    form: str | None
    has_strong_match: bool


@dataclass
class PmbiEntry:
    drug_code: int
    form: str | None
    mrp: float
    ingredients: dict[str, set[float]]


@dataclass
class BrandCandidate:
    brand_id: int
    name: str
    # Bonuses can push the raw score past 100; it is kept for ranking and capped for display.
    raw_score: float
    key_score: float
    strength_matched: bool | None
    form_matched: bool | None

    @property
    def score(self) -> float:
        return round(min(self.raw_score, 100.0), 1)


@dataclass
class GenericCandidate:
    ingredients: list[str]
    score: float
    drug_codes: list[int]
    strength_matched: bool | None


@dataclass
class LineMatch:
    parsed: ParsedLine
    kind: str  # "brand" | "generic"
    score: float
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)
    brands: list[BrandCandidate] = field(default_factory=list)
    generic: GenericCandidate | None = None


class MedicineDictionary:
    """In-memory index over brand names and PMBI ingredients."""

    def __init__(self, conn: sqlite3.Connection):
        strong = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT brand_id FROM match_candidates WHERE validation_status = 'Strong'"
            )
        }
        component_numbers: dict[int, set[float]] = defaultdict(set)
        for brand_id, strength in conn.execute(
            "SELECT brand_id, strength FROM brand_components WHERE strength IS NOT NULL"
        ):
            component_numbers[brand_id].update(
                v for v in (_to_number(n) for n in NUMBER_RE.findall(strength)) if v
            )

        self.brands: list[BrandEntry] = []
        self.key_index: dict[str, list[int]] = defaultdict(list)
        for brand_id, name, form in conn.execute(
            "SELECT brand_id, name, norm_dosage_form FROM brand_medicines ORDER BY brand_id"
        ):
            words = _brand_words(name)
            if not words:
                continue
            name_numbers = {v for v in (_to_number(n) for n in NUMBER_RE.findall(name)) if v}
            numbers = name_numbers | component_numbers.get(brand_id, set())
            idx = len(self.brands)
            self.brands.append(
                BrandEntry(brand_id, name, words, name_numbers, numbers, form, brand_id in strong)
            )
            self.key_index[words[0]].append(idx)
            # Short leading words ("A Clo P") are too ambiguous alone; also index the join.
            if len(words[0]) < 3 and len(words) > 1:
                self.key_index[words[0] + words[1]].append(idx)
        self.keys = list(self.key_index)

        self.pmbi: list[PmbiEntry] = []
        products: dict[int, PmbiEntry] = {}
        for drug_code, generic_name, form, mrp, ingredient_count in conn.execute(
            "SELECT drug_code, generic_name, norm_dosage_form, mrp, ingredient_count FROM pmbi_products"
        ):
            # Some "A + B + C" products were parsed with ingredients missing; offering
            # them for a single-ingredient brand would be wrong, so leave them out.
            if generic_name.count("+") + 1 > ingredient_count:
                continue
            products[drug_code] = PmbiEntry(drug_code, form, float(mrp), {})
        signatures: dict[int, set[tuple[str, str]]] = defaultdict(set)
        for drug_code, ingredient, strength in conn.execute(
            "SELECT drug_code, ingredient, strength FROM pmbi_components"
        ):
            if drug_code not in products:
                continue
            nums = {v for v in (_to_number(n) for n in NUMBER_RE.findall(strength or "")) if v}
            products[drug_code].ingredients.setdefault(ingredient, set()).update(nums)
            signatures[drug_code].add((ingredient, _norm_strength(strength)))
        self.pmbi = list(products.values())

        # (ingredient+strength set, dosage form) -> PMBI drug codes, cheapest first.
        self.composition_index: dict[tuple[frozenset, str | None], list[int]] = defaultdict(list)
        for entry in sorted(self.pmbi, key=lambda e: e.mrp):
            if entry.drug_code in signatures:
                key = (frozenset(signatures[entry.drug_code]), entry.form)
                self.composition_index[key].append(entry.drug_code)

        self.ingredient_vocab: dict[str, str] = {}
        for entry in self.pmbi:
            for ingredient in entry.ingredients:
                self.ingredient_vocab[ingredient] = ingredient
        for variant, canonical in conn.execute(
            "SELECT variant_name, canonical_name FROM ingredient_synonyms"
        ):
            if canonical in self.ingredient_vocab:
                self.ingredient_vocab.setdefault(variant, canonical)
        self.ingredient_names = list(self.ingredient_vocab)

    # ── Brand matching ──────────────────────────────────────────────
    def _query_keys(self, words: list[str]) -> list[tuple[int, str]]:
        keys = [(i, w) for i, w in enumerate(words[:2]) if len(w) >= 2]
        if len(words) >= 2:
            # OCR often splits one word in two ("Augmen tin").
            keys.append((0, words[0] + words[1]))
        return keys

    def match_brands(self, parsed: ParsedLine, limit: int = 5) -> list[BrandCandidate]:
        if not parsed.words:
            return []
        best_key: dict[int, float] = {}
        for pos, key in self._query_keys(parsed.words):
            if len(key) < 3:
                hits = [(key, 100.0)] if key in self.key_index else []
            else:
                hits = [
                    (k, s) for k, s, _ in process.extract(
                        key, self.keys, scorer=fuzz.ratio, limit=25, score_cutoff=KEY_CUTOFF
                    )
                ]
            # A key found from the second word means the first was OCR noise.
            penalty = 10.0 * pos
            for k, s in hits:
                for idx in self.key_index[k]:
                    adjusted = s - penalty
                    if idx not in best_key or adjusted > best_key[idx]:
                        best_key[idx] = adjusted

        query_text = " ".join(parsed.words)
        scored: list[BrandCandidate] = []
        for idx, key_score in best_key.items():
            brand = self.brands[idx]
            brand_text = " ".join(brand.words)
            full = 0.5 * fuzz.token_set_ratio(query_text, brand_text) + 0.5 * fuzz.token_sort_ratio(
                query_text, brand_text
            )
            score = 0.55 * key_score + 0.45 * full

            strength_matched = None
            if parsed.numbers and brand.numbers:
                strength_matched = bool(parsed.numbers & brand.numbers)
                if parsed.numbers & brand.name_numbers:
                    score += 10
                else:
                    score += 6 if strength_matched else -8
            elif parsed.numbers:
                score -= 5
            elif brand.numbers:
                score -= 6

            form_matched = None
            if parsed.form and brand.form:
                form_matched = parsed.form in brand.form
                score += 4 if form_matched else -10

            if brand.has_strong_match:
                score += 2
            scored.append(
                BrandCandidate(
                    brand.brand_id, brand.name, score, key_score,
                    strength_matched, form_matched,
                )
            )

        scored.sort(key=lambda c: (-c.raw_score, c.brand_id))
        unique: list[BrandCandidate] = []
        seen_names: set[str] = set()
        for cand in scored:
            name_key = cand.name.lower()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            unique.append(cand)
            if len(unique) >= limit:
                break
        return unique

    # ── Generic (ingredient) matching ───────────────────────────────
    def match_generic(self, parsed: ParsedLine) -> GenericCandidate | None:
        words = parsed.words
        found: list[str] = []
        scores: list[float] = []
        consumed = 0
        i = 0
        while i < len(words):
            best = None
            for size in (3, 2, 1):
                if i + size > len(words):
                    continue
                window = " ".join(words[i:i + size])
                if len(window) < 5:
                    continue
                hit = process.extractOne(
                    window, self.ingredient_names, scorer=fuzz.ratio, score_cutoff=GENERIC_CUTOFF
                )
                if hit and (best is None or hit[1] > best[1]):
                    best = (hit[0], hit[1], size)
            if best:
                canonical = self.ingredient_vocab[best[0]]
                if canonical not in found:
                    found.append(canonical)
                    scores.append(best[1])
                consumed += best[2]
                i += best[2]
            else:
                i += 1
        # Most of the line must be ingredient names, otherwise it is a brand.
        if not found or consumed < 0.5 * len(words):
            return None

        wanted = set(found)
        ranked = []
        for entry in self.pmbi:
            if not wanted <= set(entry.ingredients):
                continue
            exact = len(entry.ingredients) == len(wanted)
            strength = None
            if parsed.numbers:
                product_numbers = set().union(*(entry.ingredients[i] for i in wanted))
                # Combinations are often written by total strength (500 + 125 = "625").
                if all(len(entry.ingredients[i]) == 1 for i in wanted) and len(wanted) > 1:
                    product_numbers.add(sum(next(iter(entry.ingredients[i])) for i in wanted))
                strength = bool(parsed.numbers & product_numbers)
            form = parsed.form is not None and entry.form is not None and parsed.form in entry.form
            ranked.append((not exact, strength is False, not form, entry.mrp, entry.drug_code, strength))
        if not ranked:
            return None
        ranked.sort()
        # Prefer exactly the written ingredients; combinations only when nothing else exists.
        if any(not r[0] for r in ranked):
            ranked = [r for r in ranked if not r[0]]
        # When some product matches the written strength, hide the ones that don't.
        if any(r[5] for r in ranked):
            ranked = [r for r in ranked if r[5]]
        return GenericCandidate(
            ingredients=found,
            score=round(sum(scores) / len(scores), 1),
            drug_codes=[r[4] for r in ranked[:5]],
            strength_matched=ranked[0][5],
        )

    def same_composition(self, conn: sqlite3.Connection, brand_id: int) -> list[int]:
        """PMBI products with exactly the brand's ingredients, strengths and dosage form."""
        brand = conn.execute(
            "SELECT norm_dosage_form, incomplete_composition FROM brand_medicines WHERE brand_id = ?",
            (brand_id,),
        ).fetchone()
        if brand is None or brand["incomplete_composition"]:
            return []
        components = conn.execute(
            "SELECT ingredient, strength FROM brand_components WHERE brand_id = ?",
            (brand_id,),
        ).fetchall()
        if not components:
            return []
        signature = frozenset((c["ingredient"], _norm_strength(c["strength"])) for c in components)
        return list(self.composition_index.get((signature, brand["norm_dosage_form"]), []))

    # ── Line classification ─────────────────────────────────────────
    def match_line(self, text: str, ocr_confidence: float = 1.0) -> LineMatch | None:
        parsed = parse_line(text)
        if not parsed.words:
            return None
        brands = self.match_brands(parsed)
        generic = self.match_generic(parsed)
        brand_score = brands[0].score if brands else 0.0

        reasons: list[str] = []
        if generic and generic.score >= 90 and generic.score >= brand_score - 5:
            kind, score = "generic", generic.score
            if generic.strength_matched is False:
                reasons.append("strength_not_found")
            brands = []
        elif brands and brands[0].key_score >= MIN_KEY_SCORE:
            kind, score = "brand", brand_score
            top = brands[0]
            if top.key_score < CONFIDENT_KEY_SCORE:
                reasons.append("low_name_similarity")
            if top.strength_matched is False:
                reasons.append("strength_not_found")
            if not parsed.numbers and self.brands_differ_in_strength(brands):
                reasons.append("strength_not_written")
            if len(brands) > 1 and brands[1].raw_score >= top.raw_score - 3:
                reasons.append("ambiguous_name")
            generic = None
        else:
            return None

        min_score = REVIEW_SCORE if parsed.has_cue else NO_CUE_MIN_SCORE
        if score < min_score:
            return None
        if score < CONFIDENT_SCORE:
            reasons.insert(0, "low_text_similarity")
        if ocr_confidence < LOW_OCR_CONFIDENCE:
            reasons.append("low_ocr_confidence")
        return LineMatch(
            parsed=parsed, kind=kind, score=score, needs_review=bool(reasons),
            review_reasons=reasons, brands=brands, generic=generic,
        )

    @staticmethod
    def brands_differ_in_strength(brands: list[BrandCandidate]) -> bool:
        # Same leading word, several strengths ("Dolo 500" vs "Dolo 650").
        first = brands[0].name.split()[0].lower()
        same_family = [b for b in brands if b.name.split()[0].lower() == first]
        return len(same_family) > 1


_dictionary: MedicineDictionary | None = None
_dictionary_lock = threading.Lock()


def get_dictionary(conn: sqlite3.Connection) -> MedicineDictionary:
    """Build the index on first use (a few seconds over ~246k brands)."""
    global _dictionary
    if _dictionary is None:
        with _dictionary_lock:
            if _dictionary is None:
                _dictionary = MedicineDictionary(conn)
    return _dictionary
