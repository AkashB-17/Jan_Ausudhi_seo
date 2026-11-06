from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str


class Component(BaseModel):
    seq: int
    ingredient: str
    strength: str | None = None


class BrandSearchItem(BaseModel):
    brand_id: int
    name: str
    manufacturer_name: str
    price: float
    pack_size_label: str
    norm_composition: str | None = None
    norm_dosage_form: str | None = None


class BrandSearchResponse(BaseModel):
    query: str
    count: int
    results: list[BrandSearchItem]


class BrandDetail(BaseModel):
    brand_id: int
    name: str
    manufacturer_name: str
    price: float
    pack_size_label: str
    type: str
    short_composition1: str
    short_composition2: str | None = None
    norm_composition: str | None = None
    norm_dosage_form: str | None = None
    ingredient_count: int
    incomplete_composition: bool
    components: list[Component]


class StrongMatch(BaseModel):
    drug_code: int
    generic_name: str
    unit_size: str
    mrp: float
    group_name: str
    norm_composition: str
    norm_dosage_form: str | None = None
    match_method: str
    match_confidence: float = Field(
        description="Pipeline matching confidence only; not clinical or substitution confidence."
    )
    validation_status: str


class BrandMatchesResponse(BaseModel):
    brand_id: int
    matches: list[StrongMatch]


class PmbiDetail(BaseModel):
    drug_code: int
    sr_no: int
    generic_name: str
    unit_size: str
    mrp: float
    group_name: str
    norm_composition: str
    norm_dosage_form: str | None = None
    ingredient_count: int
    components: list[Component]


class CompareBrand(BaseModel):
    brand_id: int
    name: str
    manufacturer_name: str
    price: float
    pack_size_label: str
    type: str
    norm_composition: str | None = None
    norm_dosage_form: str | None = None
    ingredient_count: int
    incomplete_composition: bool
    components: list[Component]


class ComparePmbi(BaseModel):
    drug_code: int
    generic_name: str
    unit_size: str
    mrp: float
    group_name: str
    norm_composition: str
    norm_dosage_form: str | None = None
    ingredient_count: int
    components: list[Component]


class CompareMatch(BaseModel):
    match_method: str
    match_confidence: float
    validation_status: str


class PriceComparison(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": (
                "Arithmetic price difference from stored brand price and PMBI MRP. "
                "Pack/unit sizes are not normalized; this is not a guaranteed saving."
            )
        }
    )

    brand_price: float
    pmbi_mrp: float
    difference: float
    brand_pack: str
    pmbi_unit: str


class CompareResponse(BaseModel):
    brand: CompareBrand
    pmbi: ComparePmbi
    match: CompareMatch
    price_comparison: PriceComparison


class PrescriptionLine(BaseModel):
    page: int
    text: str
    ocr_confidence: float = Field(description="OCR engine confidence, 0-1. 1.0 for PDF text.")
    note: str | None = Field(
        default=None,
        description="Vision readers only: the line as written and its dosing, for display.",
    )


class DetectedBrand(BaseModel):
    brand_id: int
    name: str
    manufacturer_name: str
    price: float
    pack_size_label: str
    norm_composition: str | None = None
    norm_dosage_form: str | None = None


class GenericOption(BaseModel):
    drug_code: int
    generic_name: str
    unit_size: str
    mrp: float
    group_name: str
    norm_composition: str
    norm_dosage_form: str | None = None
    match_source: Literal["validated_match", "composition_match", "ingredient_lookup"] = Field(
        description=(
            "validated_match: Strong match from the validation pipeline. "
            "composition_match: identical ingredients, strengths and form, not pipeline-validated. "
            "ingredient_lookup: prescription named the generic; matched by ingredient."
        )
    )
    match_confidence: float | None = None
    validation_status: str | None = None


class DetectedMedicine(BaseModel):
    line: PrescriptionLine
    kind: Literal["brand", "generic"]
    match_score: float = Field(
        description="Text similarity between the line and the matched name, 0-100."
    )
    needs_review: bool
    review_reasons: list[str]
    brand: DetectedBrand | None = None
    alternatives: list[DetectedBrand] = []
    ingredients: list[str] = []
    generics: list[GenericOption]
    cheapest_generic_mrp: float | None = None
    price_difference: float | None = Field(
        default=None,
        description="Brand price minus cheapest Jan Aushadhi MRP; pack sizes not normalized.",
    )


class PrescriptionScanResponse(BaseModel):
    engine: str
    pages: int
    lines: list[PrescriptionLine]
    medicines: list[DetectedMedicine]
    unmatched_lines: list[PrescriptionLine]
    warning: Literal["not_a_prescription"] | None = Field(
        default=None,
        description="Set when the upload has no prescription cues (Rx, Tab/Cap, mg, 1-0-1, BD).",
    )
    disclaimer: str


class PrescriptionTextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
