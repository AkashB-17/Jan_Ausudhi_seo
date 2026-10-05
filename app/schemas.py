from __future__ import annotations

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
