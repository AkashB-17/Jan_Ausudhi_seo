from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.database import db_session
from app.schemas import (
    BrandDetail,
    BrandMatchesResponse,
    BrandSearchItem,
    BrandSearchResponse,
    CompareBrand,
    CompareMatch,
    ComparePmbi,
    CompareResponse,
    Component,
    PmbiDetail,
    PriceComparison,
    StrongMatch,
)

router = APIRouter()

MIN_QUERY_LENGTH = 2


def _brand_components(conn: sqlite3.Connection, brand_id: int) -> list[Component]:
    rows = conn.execute(
        """
        SELECT seq, ingredient, strength
        FROM brand_components
        WHERE brand_id = ?
        ORDER BY seq
        """,
        (brand_id,),
    ).fetchall()
    return [
        Component(seq=r["seq"], ingredient=r["ingredient"], strength=r["strength"])
        for r in rows
    ]


def _pmbi_components(conn: sqlite3.Connection, drug_code: int) -> list[Component]:
    rows = conn.execute(
        """
        SELECT seq, ingredient, strength
        FROM pmbi_components
        WHERE drug_code = ?
        ORDER BY seq
        """,
        (drug_code,),
    ).fetchall()
    return [
        Component(seq=r["seq"], ingredient=r["ingredient"], strength=r["strength"])
        for r in rows
    ]


def _brand_exists(conn: sqlite3.Connection, brand_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM brand_medicines WHERE brand_id = ?",
        (brand_id,),
    ).fetchone()
    return row is not None


def _fetch_brand(conn: sqlite3.Connection, brand_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            brand_id, name, manufacturer_name, price, pack_size_label, type,
            short_composition1, short_composition2, norm_composition,
            norm_dosage_form, ingredient_count, incomplete_composition
        FROM brand_medicines
        WHERE brand_id = ?
        """,
        (brand_id,),
    ).fetchone()


def _fetch_pmbi(conn: sqlite3.Connection, drug_code: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            drug_code, sr_no, generic_name, unit_size, mrp, group_name,
            norm_composition, norm_dosage_form, ingredient_count
        FROM pmbi_products
        WHERE drug_code = ?
        """,
        (drug_code,),
    ).fetchone()


@router.get("/medicines/search", response_model=BrandSearchResponse)
def search_medicines(
    q: str | None = Query(default=None, description="Brand name search text"),
) -> BrandSearchResponse:
    if q is None or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required.")
    query = q.strip()
    if len(query) < MIN_QUERY_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Query must be at least {MIN_QUERY_LENGTH} characters.",
        )

    needle = query.lower()
    try:
        with db_session() as conn:
            rows = conn.execute(
                """
                SELECT
                    brand_id, name, manufacturer_name, price, pack_size_label,
                    norm_composition, norm_dosage_form
                FROM brand_medicines
                WHERE name_normalized LIKE '%' || ? || '%'
                ORDER BY name
                LIMIT 50
                """,
                (needle,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc

    results = [BrandSearchItem(**dict(row)) for row in rows]
    return BrandSearchResponse(query=query, count=len(results), results=results)


@router.get("/medicines/{brand_id}", response_model=BrandDetail)
def get_brand(brand_id: int) -> BrandDetail:
    try:
        with db_session() as conn:
            row = _fetch_brand(conn, brand_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Brand medicine not found.")
            components = _brand_components(conn, brand_id)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc

    data = dict(row)
    data["incomplete_composition"] = bool(data["incomplete_composition"])
    data["components"] = components
    return BrandDetail(**data)


@router.get("/medicines/{brand_id}/matches", response_model=BrandMatchesResponse)
def get_brand_matches(brand_id: int) -> BrandMatchesResponse:
    try:
        with db_session() as conn:
            if not _brand_exists(conn, brand_id):
                raise HTTPException(status_code=404, detail="Brand medicine not found.")
            rows = conn.execute(
                """
                SELECT
                    p.drug_code, p.generic_name, p.unit_size, p.mrp, p.group_name,
                    p.norm_composition, p.norm_dosage_form,
                    m.match_method, m.match_confidence, m.validation_status
                FROM match_candidates AS m
                JOIN pmbi_products AS p ON p.drug_code = m.drug_code
                WHERE m.brand_id = ?
                  AND m.validation_status = 'Strong'
                ORDER BY m.match_confidence DESC, p.mrp ASC
                """,
                (brand_id,),
            ).fetchall()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc

    return BrandMatchesResponse(
        brand_id=brand_id,
        matches=[StrongMatch(**dict(row)) for row in rows],
    )


@router.get("/pmbi/{drug_code}", response_model=PmbiDetail)
def get_pmbi(drug_code: int) -> PmbiDetail:
    try:
        with db_session() as conn:
            row = _fetch_pmbi(conn, drug_code)
            if row is None:
                raise HTTPException(status_code=404, detail="PMBI product not found.")
            components = _pmbi_components(conn, drug_code)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc

    data = dict(row)
    data["components"] = components
    return PmbiDetail(**data)


@router.get("/compare/{brand_id}/{drug_code}", response_model=CompareResponse)
def compare_brand_pmbi(brand_id: int, drug_code: int) -> CompareResponse:
    try:
        with db_session() as conn:
            brand = _fetch_brand(conn, brand_id)
            if brand is None:
                raise HTTPException(status_code=404, detail="Brand medicine not found.")
            pmbi = _fetch_pmbi(conn, drug_code)
            if pmbi is None:
                raise HTTPException(status_code=404, detail="PMBI product not found.")
            match = conn.execute(
                """
                SELECT match_method, match_confidence, validation_status
                FROM match_candidates
                WHERE brand_id = ? AND drug_code = ?
                """,
                (brand_id, drug_code),
            ).fetchone()
            if match is None:
                raise HTTPException(
                    status_code=404,
                    detail="No match candidate exists for this brand and PMBI product.",
                )
            if match["validation_status"] != "Strong":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This pair is not a Strong validated match and cannot be used "
                        "for application comparison."
                    ),
                )
            brand_components = _brand_components(conn, brand_id)
            pmbi_components = _pmbi_components(conn, drug_code)
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc

    brand_price = float(brand["price"])
    pmbi_mrp = float(pmbi["mrp"])
    difference = round(brand_price - pmbi_mrp, 2)

    return CompareResponse(
        brand=CompareBrand(
            brand_id=brand["brand_id"],
            name=brand["name"],
            manufacturer_name=brand["manufacturer_name"],
            price=brand_price,
            pack_size_label=brand["pack_size_label"],
            type=brand["type"],
            norm_composition=brand["norm_composition"],
            norm_dosage_form=brand["norm_dosage_form"],
            ingredient_count=brand["ingredient_count"],
            incomplete_composition=bool(brand["incomplete_composition"]),
            components=brand_components,
        ),
        pmbi=ComparePmbi(
            drug_code=pmbi["drug_code"],
            generic_name=pmbi["generic_name"],
            unit_size=pmbi["unit_size"],
            mrp=pmbi_mrp,
            group_name=pmbi["group_name"],
            norm_composition=pmbi["norm_composition"],
            norm_dosage_form=pmbi["norm_dosage_form"],
            ingredient_count=pmbi["ingredient_count"],
            components=pmbi_components,
        ),
        match=CompareMatch(
            match_method=match["match_method"],
            match_confidence=float(match["match_confidence"]),
            validation_status=match["validation_status"],
        ),
        price_comparison=PriceComparison(
            brand_price=brand_price,
            pmbi_mrp=pmbi_mrp,
            difference=difference,
            brand_pack=brand["pack_size_label"],
            pmbi_unit=pmbi["unit_size"],
        ),
    )
