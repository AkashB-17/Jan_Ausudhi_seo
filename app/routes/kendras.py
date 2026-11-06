"""Find Jan Aushadhi Kendras (PMBI stores) by PIN code or by state and district."""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.database import KENDRA_DB_PATH, db_session
from app.schemas import Kendra, KendraArea, KendraSearchResponse

router = APIRouter(prefix="/kendras", tags=["kendras"])

PIN_RE = re.compile(r"^[1-9]\d{5}$")
KENDRA_COLUMNS = "kendra_code, state, district, pincode, address"


def _source(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT source, imported_on FROM kendra_source").fetchone()
    return f"{row['source']}, imported {row['imported_on']}" if row else "unknown"


def _open():
    if not KENDRA_DB_PATH.exists():
        raise HTTPException(
            status_code=503,
            detail="The Kendra list has not been imported. Run scripts/import_kendras.py.",
        )
    return db_session(KENDRA_DB_PATH)


@router.get("/states", response_model=list[KendraArea])
def list_states() -> list[KendraArea]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT state AS name, COUNT(*) AS count FROM kendras GROUP BY state ORDER BY state"
        ).fetchall()
    return [KendraArea(**dict(r)) for r in rows]


@router.get("/districts", response_model=list[KendraArea])
def list_districts(state: str = Query(min_length=1)) -> list[KendraArea]:
    with _open() as conn:
        rows = conn.execute(
            """
            SELECT district AS name, COUNT(*) AS count FROM kendras
            WHERE state = ? GROUP BY district ORDER BY district
            """,
            (state,),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No Kendras listed for state '{state}'.")
    return [KendraArea(**dict(r)) for r in rows]


@router.get("/search", response_model=KendraSearchResponse)
def search_kendras(
    pincode: str | None = Query(default=None, description="6-digit PIN code"),
    state: str | None = None,
    district: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> KendraSearchResponse:
    """
    By PIN: Kendras in that PIN first, then the closest PINs in the same postal sorting
    district (same first three digits). There are no map coordinates in the official
    list, so "closest" means closest PIN number, which usually means nearby.
    """
    if pincode is not None:
        pincode = pincode.strip()
        if not PIN_RE.match(pincode):
            raise HTTPException(status_code=400, detail="Enter a valid 6-digit PIN code.")
        with _open() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM kendras WHERE substr(pincode, 1, 3) = ?", (pincode[:3],)
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT {KENDRA_COLUMNS},
                       CASE WHEN pincode = :pin THEN 'same_pin' ELSE 'nearby_pin' END AS match
                FROM kendras
                WHERE substr(pincode, 1, 3) = :prefix
                ORDER BY abs(CAST(pincode AS INTEGER) - CAST(:pin AS INTEGER)), kendra_code
                LIMIT :limit OFFSET :offset
                """,
                {"pin": pincode, "prefix": pincode[:3], "limit": limit, "offset": offset},
            ).fetchall()
            source = _source(conn)
        return KendraSearchResponse(
            query=pincode, total=total, kendras=[Kendra(**dict(r)) for r in rows], source=source
        )

    if not state or not district:
        raise HTTPException(
            status_code=400, detail="Give a PIN code, or both a state and a district."
        )
    with _open() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM kendras WHERE state = ? AND district = ?", (state, district)
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT {KENDRA_COLUMNS}, 'district' AS match FROM kendras
            WHERE state = ? AND district = ?
            ORDER BY pincode = '', pincode, kendra_code
            LIMIT ? OFFSET ?
            """,
            (state, district, limit, offset),
        ).fetchall()
        source = _source(conn)
    return KendraSearchResponse(
        query=f"{district}, {state}", total=total,
        kendras=[Kendra(**dict(r)) for r in rows], source=source,
    )
