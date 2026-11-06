"""Glue between OCR output, the matcher and the database rows the API returns."""

from __future__ import annotations

import sqlite3

from app.prescription.documents import PageInput, load_document
from app.prescription.matcher import LineMatch, MedicineDictionary, get_dictionary, parse_line
from app.prescription.ocr import OcrLine, get_engine
from app.schemas import (
    DetectedBrand,
    DetectedMedicine,
    GenericOption,
    PrescriptionLine,
    PrescriptionScanResponse,
)

DISCLAIMER = (
    "Medicines were read automatically and may be wrong. Check every line against the "
    "original prescription. Jan Aushadhi matches are composition-based data matches, not "
    "substitution advice; confirm with your doctor or pharmacist before switching."
)

BRAND_COLUMNS = (
    "brand_id, name, manufacturer_name, price, pack_size_label, "
    "norm_composition, norm_dosage_form"
)
PMBI_COLUMNS = (
    "drug_code, generic_name, unit_size, mrp, group_name, norm_composition, norm_dosage_form"
)


def scan_document(conn: sqlite3.Connection, data: bytes) -> PrescriptionScanResponse:
    pages = load_document(data)
    lines: list[PrescriptionLine] = []
    alternatives: list[list[str]] = []
    used_ocr = False
    engine_name = "pdf-text"
    for page in pages:
        for ocr_line in _read_page(page):
            lines.append(
                PrescriptionLine(
                    page=page.page, text=ocr_line.text,
                    ocr_confidence=ocr_line.confidence, note=ocr_line.note,
                )
            )
            alternatives.append(ocr_line.alternatives)
        if not page.has_text_layer:
            used_ocr = True
    if used_ocr:
        engine_name = get_engine().name
    return match_lines(
        conn, lines, engine=engine_name, pages=len(pages),
        require_prescription=True, alternatives=alternatives,
    )


def _read_page(page: PageInput) -> list[OcrLine]:
    if page.has_text_layer:
        return [OcrLine(text=t, confidence=1.0) for t in page.text_lines]
    return get_engine().read(page.image)


def match_lines(
    conn: sqlite3.Connection,
    lines: list[PrescriptionLine],
    engine: str,
    pages: int,
    require_prescription: bool = False,
    alternatives: list[list[str]] | None = None,
) -> PrescriptionScanResponse:
    """
    ``require_prescription`` is for uploads: a document with no line that looks like a
    prescription line (a CV, an invoice) returns no medicines rather than chance
    brand-name matches. Text the user typed or corrected is matched as given.

    ``alternatives`` holds other readings of each line from a vision reader; the one
    that matches the dictionary best is kept.
    """
    dictionary = get_dictionary(conn)
    if require_prescription and not any(parse_line(line.text).strong_cue for line in lines):
        return PrescriptionScanResponse(
            engine=engine,
            pages=pages,
            lines=lines,
            medicines=[],
            unmatched_lines=lines,
            warning="not_a_prescription",
            disclaimer=DISCLAIMER,
        )
    medicines: list[DetectedMedicine] = []
    unmatched: list[PrescriptionLine] = []
    for i, line in enumerate(lines):
        others = alternatives[i] if alternatives else []
        match = _best_reading(dictionary, line, others)
        if match is None:
            unmatched.append(line)
            continue
        medicines.append(_build_medicine(conn, dictionary, line, match))
    return PrescriptionScanResponse(
        engine=engine,
        pages=pages,
        lines=lines,
        medicines=medicines,
        unmatched_lines=unmatched,
        disclaimer=DISCLAIMER,
    )


def _best_reading(
    dictionary: MedicineDictionary, line: PrescriptionLine, others: list[str]
) -> LineMatch | None:
    best = dictionary.match_line(line.text, line.ocr_confidence)
    for text in others:
        match = dictionary.match_line(text, line.ocr_confidence)
        # An alternative must beat the primary reading clearly, not tie it.
        if match and (best is None or match.score > best.score + 2):
            best = match
            best.needs_review = True
            best.review_reasons.append("alternative_reading")
    return best


def _build_medicine(
    conn: sqlite3.Connection,
    dictionary: MedicineDictionary,
    line: PrescriptionLine,
    match: LineMatch,
) -> DetectedMedicine:
    if match.kind == "generic":
        generics = _pmbi_options(conn, match.generic.drug_codes, "ingredient_lookup")
        return DetectedMedicine(
            line=line,
            kind="generic",
            match_score=match.score,
            needs_review=match.needs_review,
            review_reasons=match.review_reasons,
            ingredients=match.generic.ingredients,
            generics=generics,
            cheapest_generic_mrp=min((g.mrp for g in generics), default=None),
        )

    brand_rows = _brand_rows(conn, [b.brand_id for b in match.brands])
    top, alternatives = brand_rows[0], brand_rows[1:]
    generics = strong_generics(conn, top.brand_id)
    if not generics:
        # The validated match table misses many common brands (e.g. Dolo 650), so fall
        # back to PMBI products with an identical composition, strength and form.
        generics = _pmbi_options(
            conn, dictionary.same_composition(conn, top.brand_id), "composition_match"
        )
    reasons = list(match.review_reasons)
    if not generics:
        reasons.append("no_jan_aushadhi_match")
    cheapest = min((g.mrp for g in generics), default=None)
    return DetectedMedicine(
        line=line,
        kind="brand",
        match_score=match.score,
        needs_review=match.needs_review,
        review_reasons=reasons,
        brand=top,
        alternatives=alternatives,
        generics=generics,
        cheapest_generic_mrp=cheapest,
        price_difference=round(top.price - cheapest, 2) if cheapest is not None else None,
    )


def _brand_rows(conn: sqlite3.Connection, brand_ids: list[int]) -> list[DetectedBrand]:
    placeholders = ",".join("?" * len(brand_ids))
    rows = conn.execute(
        f"SELECT {BRAND_COLUMNS} FROM brand_medicines WHERE brand_id IN ({placeholders})",
        brand_ids,
    ).fetchall()
    by_id = {r["brand_id"]: DetectedBrand(**dict(r)) for r in rows}
    return [by_id[i] for i in brand_ids if i in by_id]


def strong_generics(conn: sqlite3.Connection, brand_id: int) -> list[GenericOption]:
    rows = conn.execute(
        """
        SELECT
            p.drug_code, p.generic_name, p.unit_size, p.mrp, p.group_name,
            p.norm_composition, p.norm_dosage_form,
            m.match_confidence, m.validation_status
        FROM match_candidates AS m
        JOIN pmbi_products AS p ON p.drug_code = m.drug_code
        WHERE m.brand_id = ?
          AND m.validation_status = 'Strong'
        ORDER BY m.match_confidence DESC, p.mrp ASC
        """,
        (brand_id,),
    ).fetchall()
    return [GenericOption(match_source="validated_match", **dict(r)) for r in rows]


def _pmbi_options(
    conn: sqlite3.Connection, drug_codes: list[int], source: str
) -> list[GenericOption]:
    if not drug_codes:
        return []
    placeholders = ",".join("?" * len(drug_codes))
    rows = conn.execute(
        f"SELECT {PMBI_COLUMNS} FROM pmbi_products WHERE drug_code IN ({placeholders})",
        drug_codes,
    ).fetchall()
    by_code = {r["drug_code"]: r for r in rows}
    return [
        GenericOption(match_source=source, **dict(by_code[c]))
        for c in drug_codes
        if c in by_code
    ]
