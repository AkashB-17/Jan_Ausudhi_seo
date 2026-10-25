"""
Build SQLite medicines.db from processed pipeline outputs.

LOAD/STRUCTURE only. Does not parse medicines, add synonyms, infer
strengths/forms, or alter match classifications.

Inputs (read-only):
  data/processed/brands_clean.csv
  data/processed/pmbi_clean.csv
  data/processed/matches_validated_candidates.csv
  data/processed/verified_synonyms.json

Output:
  data/database/medicines.db
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
DB_DIR = ROOT / "data" / "database"
DB_PATH = DB_DIR / "medicines.db"

BRANDS_CLEAN_PATH = PROCESSED / "brands_clean.csv"
PMBI_CLEAN_PATH = PROCESSED / "pmbi_clean.csv"
MATCHES_PATH = PROCESSED / "matches_validated_candidates.csv"
SYNONYM_PATH = PROCESSED / "verified_synonyms.json"

# Pipeline serialization: "ingredient (strength)" joined by " + "
COMPONENT_PART_RE = re.compile(r"^(.+?) \(([^()]+)\)\s*$")


def _is_missing(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def nullable_text(value) -> str | None:
    if _is_missing(value):
        return None
    text = str(value)
    return None if text == "" else text


def required_text(value) -> str:
    if _is_missing(value):
        return ""
    return str(value)


def as_int(value) -> int:
    return int(value)


def as_float(value) -> float:
    return float(value)


def as_flag(value) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes"} else 0
    return 1 if bool(value) else 0


def try_deserialize_components(norm_composition, ingredient_count: int) -> list[tuple[str, str]] | None:
    """
    Unmarshal the existing 'ingredient (strength)' serialization.

    Returns:
      [] if ingredient_count is 0 and composition is empty (Sterodin-style).
      list of (ingredient, strength) if lossless.
      None if unsafe — caller must create ZERO component rows.
    """
    if _is_missing(norm_composition) or str(norm_composition).strip() == "":
        return [] if ingredient_count == 0 else None

    parts = str(norm_composition).split(" + ")
    rows: list[tuple[str, str]] = []
    for part in parts:
        match = COMPONENT_PART_RE.match(part.strip())
        if not match:
            return None
        rows.append((match.group(1).strip(), match.group(2).strip()))
    if len(rows) != ingredient_count:
        return None
    return rows


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE brand_medicines (
    brand_id INTEGER NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    manufacturer_name TEXT NOT NULL,
    price REAL NOT NULL CHECK (price >= 0),
    pack_size_label TEXT NOT NULL,
    type TEXT NOT NULL,
    short_composition1 TEXT NOT NULL,
    short_composition2 TEXT,
    salt_composition TEXT,
    brand_composition_raw TEXT NOT NULL,
    norm_composition TEXT,
    norm_dosage_form TEXT,
    ingredient_count INTEGER NOT NULL,
    incomplete_composition INTEGER NOT NULL CHECK (incomplete_composition IN (0, 1)),
    incompleteness_reason TEXT,
    parse_notes TEXT
);

CREATE TABLE brand_components (
    component_id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    ingredient TEXT NOT NULL,
    strength TEXT,
    FOREIGN KEY (brand_id) REFERENCES brand_medicines(brand_id) ON DELETE CASCADE,
    UNIQUE (brand_id, seq),
    UNIQUE (brand_id, ingredient, strength)
);

CREATE TABLE pmbi_products (
    drug_code INTEGER NOT NULL PRIMARY KEY,
    sr_no INTEGER NOT NULL UNIQUE,
    generic_name TEXT NOT NULL,
    unit_size TEXT NOT NULL,
    mrp REAL NOT NULL CHECK (mrp >= 0),
    group_name TEXT NOT NULL,
    norm_composition TEXT NOT NULL,
    norm_dosage_form TEXT,
    ingredient_count INTEGER NOT NULL,
    parse_notes TEXT
);

CREATE TABLE pmbi_components (
    component_id INTEGER PRIMARY KEY AUTOINCREMENT,
    drug_code INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    ingredient TEXT NOT NULL,
    strength TEXT,
    FOREIGN KEY (drug_code) REFERENCES pmbi_products(drug_code) ON DELETE CASCADE,
    UNIQUE (drug_code, seq),
    UNIQUE (drug_code, ingredient, strength)
);

CREATE TABLE match_candidates (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    drug_code INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence REAL NOT NULL CHECK (match_confidence >= 0 AND match_confidence <= 1),
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('Strong', 'Moderate', 'Ambiguous', 'Rejected')
    ),
    rejection_reason TEXT,
    audit_flags TEXT,
    FOREIGN KEY (brand_id) REFERENCES brand_medicines(brand_id) ON DELETE RESTRICT,
    FOREIGN KEY (drug_code) REFERENCES pmbi_products(drug_code) ON DELETE RESTRICT,
    UNIQUE (brand_id, drug_code)
);

CREATE TABLE ingredient_synonyms (
    synonym_id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_name TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX idx_brand_name_normalized ON brand_medicines(name_normalized);
CREATE INDEX idx_brand_components_ingredient ON brand_components(ingredient);
CREATE INDEX idx_pmbi_generic_name ON pmbi_products(generic_name);
CREATE INDEX idx_pmbi_components_ingredient ON pmbi_components(ingredient);
CREATE INDEX idx_match_brand_status ON match_candidates(brand_id, validation_status);
CREATE INDEX idx_match_pmbi_status ON match_candidates(drug_code, validation_status);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("PRAGMA foreign_keys = OFF;")
    conn.executescript(
        """
        DROP TABLE IF EXISTS match_candidates;
        DROP TABLE IF EXISTS brand_components;
        DROP TABLE IF EXISTS pmbi_components;
        DROP TABLE IF EXISTS brand_medicines;
        DROP TABLE IF EXISTS pmbi_products;
        DROP TABLE IF EXISTS ingredient_synonyms;
        """
    )
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA foreign_keys = ON")


def load_brands(conn: sqlite3.Connection) -> tuple[int, int]:
    df = pd.read_csv(BRANDS_CLEAN_PATH)
    brand_rows = []
    component_rows = []
    skipped_component_parents = 0

    for rec in df.itertuples(index=False):
        brand_id = as_int(rec.brand_id)
        name = required_text(rec.name)
        brand_rows.append(
            (
                brand_id,
                name,
                name.lower().strip(),
                required_text(rec.manufacturer_name),
                as_float(rec.price),
                required_text(rec.pack_size_label),
                required_text(rec.type),
                required_text(rec.short_composition1),
                nullable_text(rec.short_composition2),
                nullable_text(rec.salt_composition),
                required_text(rec.brand_composition_raw),
                nullable_text(rec.norm_composition),
                nullable_text(rec.norm_dosage_form),
                as_int(rec.ingredient_count),
                as_flag(rec.incomplete_composition),
                nullable_text(rec.incompleteness_reason),
                nullable_text(rec.parse_notes),
            )
        )
        deserialized = try_deserialize_components(rec.norm_composition, as_int(rec.ingredient_count))
        if deserialized is None:
            skipped_component_parents += 1
            continue
        for seq, (ingredient, strength) in enumerate(deserialized, start=1):
            component_rows.append((brand_id, seq, ingredient, strength))

    conn.executemany(
        """
        INSERT INTO brand_medicines (
            brand_id, name, name_normalized, manufacturer_name, price,
            pack_size_label, type, short_composition1, short_composition2,
            salt_composition, brand_composition_raw, norm_composition,
            norm_dosage_form, ingredient_count, incomplete_composition,
            incompleteness_reason, parse_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        brand_rows,
    )
    conn.executemany(
        """
        INSERT INTO brand_components (brand_id, seq, ingredient, strength)
        VALUES (?, ?, ?, ?)
        """,
        component_rows,
    )
    return len(brand_rows), skipped_component_parents


def load_pmbi(conn: sqlite3.Connection) -> tuple[int, int]:
    df = pd.read_csv(PMBI_CLEAN_PATH)
    product_rows = []
    component_rows = []
    skipped_component_parents = 0

    for rec in df.itertuples(index=False):
        drug_code = as_int(rec.drug_code)
        product_rows.append(
            (
                drug_code,
                as_int(rec.sr_no),
                required_text(rec.generic_name),
                required_text(rec.unit_size),
                as_float(rec.mrp),
                required_text(rec.group_name),
                required_text(rec.norm_composition),
                nullable_text(rec.norm_dosage_form),
                as_int(rec.ingredient_count),
                nullable_text(rec.parse_notes),
            )
        )
        deserialized = try_deserialize_components(rec.norm_composition, as_int(rec.ingredient_count))
        if deserialized is None:
            skipped_component_parents += 1
            continue
        for seq, (ingredient, strength) in enumerate(deserialized, start=1):
            component_rows.append((drug_code, seq, ingredient, strength))

    conn.executemany(
        """
        INSERT INTO pmbi_products (
            drug_code, sr_no, generic_name, unit_size, mrp, group_name,
            norm_composition, norm_dosage_form, ingredient_count, parse_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        product_rows,
    )
    conn.executemany(
        """
        INSERT INTO pmbi_components (drug_code, seq, ingredient, strength)
        VALUES (?, ?, ?, ?)
        """,
        component_rows,
    )
    return len(product_rows), skipped_component_parents


def load_matches(conn: sqlite3.Connection) -> int:
    df = pd.read_csv(MATCHES_PATH)
    rows = []
    for rec in df.itertuples(index=False):
        rows.append(
            (
                as_int(rec.brand_id),
                as_int(rec.pmbi_drug_code),
                required_text(rec.match_method),
                as_float(rec.match_confidence),
                required_text(rec.validation_status),
                nullable_text(rec.rejection_reason),
                nullable_text(rec.audit_flags),
            )
        )
    conn.executemany(
        """
        INSERT INTO match_candidates (
            brand_id, drug_code, match_method, match_confidence,
            validation_status, rejection_reason, audit_flags
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def load_synonyms(conn: sqlite3.Connection) -> int:
    with SYNONYM_PATH.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    synonyms = payload["synonyms"]
    rows = [(variant, canonical) for variant, canonical in synonyms.items()]
    conn.executemany(
        """
        INSERT INTO ingredient_synonyms (variant_name, canonical_name)
        VALUES (?, ?)
        """,
        rows,
    )
    return len(rows)


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0])


def run_validation(conn: sqlite3.Connection) -> dict:
    tables = [
        "brand_medicines",
        "brand_components",
        "pmbi_products",
        "pmbi_components",
        "match_candidates",
        "ingredient_synonyms",
    ]
    counts = {t: scalar(conn, f"SELECT COUNT(*) FROM {t}") for t in tables}
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    status_rows = conn.execute(
        """
        SELECT validation_status, COUNT(*) AS n
        FROM match_candidates
        GROUP BY validation_status
        """
    ).fetchall()
    status_counts = {r["validation_status"]: r["n"] for r in status_rows}

    results = {
        "row_counts": counts,
        "integrity_check": integrity,
        "foreign_key_check": [tuple(r) for r in fk_rows],
        "duplicate_brand_id": scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT brand_id FROM brand_medicines GROUP BY brand_id HAVING COUNT(*) > 1)",
        ),
        "duplicate_drug_code": scalar(
            conn,
            "SELECT COUNT(*) FROM (SELECT drug_code FROM pmbi_products GROUP BY drug_code HAVING COUNT(*) > 1)",
        ),
        "duplicate_match_pairs": scalar(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT brand_id, drug_code FROM match_candidates
                GROUP BY brand_id, drug_code HAVING COUNT(*) > 1
            )
            """,
        ),
        "orphan_brand_components": scalar(
            conn,
            """
            SELECT COUNT(*) FROM brand_components c
            LEFT JOIN brand_medicines b ON b.brand_id = c.brand_id
            WHERE b.brand_id IS NULL
            """,
        ),
        "orphan_pmbi_components": scalar(
            conn,
            """
            SELECT COUNT(*) FROM pmbi_components c
            LEFT JOIN pmbi_products p ON p.drug_code = c.drug_code
            WHERE p.drug_code IS NULL
            """,
        ),
        "orphan_match_candidates": scalar(
            conn,
            """
            SELECT COUNT(*) FROM match_candidates m
            LEFT JOIN brand_medicines b ON b.brand_id = m.brand_id
            LEFT JOIN pmbi_products p ON p.drug_code = m.drug_code
            WHERE b.brand_id IS NULL OR p.drug_code IS NULL
            """,
        ),
        "match_status_counts": status_counts,
        "brand_parents_with_zero_components": scalar(
            conn,
            """
            SELECT COUNT(*) FROM brand_medicines b
            LEFT JOIN brand_components c ON c.brand_id = b.brand_id
            WHERE c.component_id IS NULL
            """,
        ),
        "pmbi_parents_with_zero_components": scalar(
            conn,
            """
            SELECT COUNT(*) FROM pmbi_products p
            LEFT JOIN pmbi_components c ON c.drug_code = p.drug_code
            WHERE c.component_id IS NULL
            """,
        ),
        "synonym_count": counts["ingredient_synonyms"],
    }
    return results


def run_sanity_tests(conn: sqlite3.Connection) -> dict:
    brand = conn.execute(
        """
        SELECT brand_id, name, price, pack_size_label, norm_composition
        FROM brand_medicines
        WHERE name = ?
        """,
        ("A Clo P 100mg/325mg Tablet",),
    ).fetchone()
    brand_components = []
    if brand is not None:
        brand_components = conn.execute(
            """
            SELECT seq, ingredient, strength
            FROM brand_components
            WHERE brand_id = ?
            ORDER BY seq
            """,
            (brand["brand_id"],),
        ).fetchall()

    pmbi = conn.execute(
        """
        SELECT drug_code, generic_name, mrp, unit_size, norm_composition
        FROM pmbi_products
        WHERE drug_code = 1
        """
    ).fetchone()
    pmbi_components = conn.execute(
        """
        SELECT seq, ingredient, strength
        FROM pmbi_components
        WHERE drug_code = 1
        ORDER BY seq
        """
    ).fetchall()

    match = conn.execute(
        """
        SELECT brand_id, drug_code, match_method, match_confidence, validation_status
        FROM match_candidates
        WHERE brand_id = 9644 AND drug_code = 1
        """
    ).fetchone()

    price_diff = None
    if brand is not None and pmbi is not None:
        price_diff = float(brand["price"]) - float(pmbi["mrp"])

    synonym = conn.execute(
        """
        SELECT variant_name, canonical_name
        FROM ingredient_synonyms
        WHERE variant_name = 'aciclovir'
        """
    ).fetchone()
    synonym_acyclovir = conn.execute(
        """
        SELECT variant_name, canonical_name
        FROM ingredient_synonyms
        WHERE variant_name = 'acyclovir'
        """
    ).fetchone()

    return {
        "brand": dict(brand) if brand else None,
        "brand_components": [dict(r) for r in brand_components],
        "pmbi": dict(pmbi) if pmbi else None,
        "pmbi_components": [dict(r) for r in pmbi_components],
        "match": dict(match) if match else None,
        "price_difference": price_diff,
        "synonym_aciclovir": dict(synonym) if synonym else None,
        "synonym_acyclovir": dict(synonym_acyclovir) if synonym_acyclovir else None,
    }


def print_report(
    validation: dict,
    sanity: dict,
    load_meta: dict,
) -> None:
    print("=== Load complete ===")
    print(f"Database: {DB_PATH}")
    print(f"Brand parents loaded: {load_meta['brand_parents']}")
    print(f"Brand parents with no deserialized components (load-time): {load_meta['brand_skip_components']}")
    print(f"PMBI parents loaded: {load_meta['pmbi_parents']}")
    print(f"PMBI parents with no deserialized components (load-time): {load_meta['pmbi_skip_components']}")
    print(f"Match rows loaded: {load_meta['matches']}")
    print(f"Synonym rows loaded: {load_meta['synonyms']}")
    print()
    print("=== Validation ===")
    print("Row counts:")
    for table, n in validation["row_counts"].items():
        print(f"  {table}: {n:,}")
    print(f"PRAGMA integrity_check: {validation['integrity_check']}")
    print(f"PRAGMA foreign_key_check: {validation['foreign_key_check'] or '[] (no violations)'}")
    print(f"Duplicate brand_id groups: {validation['duplicate_brand_id']}")
    print(f"Duplicate drug_code groups: {validation['duplicate_drug_code']}")
    print(f"Duplicate (brand_id, drug_code) pairs: {validation['duplicate_match_pairs']}")
    print(f"Orphan brand_components: {validation['orphan_brand_components']}")
    print(f"Orphan pmbi_components: {validation['orphan_pmbi_components']}")
    print(f"Orphan match_candidates: {validation['orphan_match_candidates']}")
    print("Match status counts:")
    for status in ("Strong", "Moderate", "Ambiguous", "Rejected"):
        print(f"  {status}: {validation['match_status_counts'].get(status, 0):,}")
    print(f"brand_components rows: {validation['row_counts']['brand_components']:,}")
    print(f"pmbi_components rows: {validation['row_counts']['pmbi_components']:,}")
    print(f"Brand parents with zero components: {validation['brand_parents_with_zero_components']}")
    print(f"PMBI parents with zero components: {validation['pmbi_parents_with_zero_components']}")
    print(f"Synonym entries: {validation['synonym_count']}")
    print()
    print("=== Sanity tests ===")
    brand = sanity["brand"]
    print("A. Brand lookup 'A Clo P 100mg/325mg Tablet':")
    if brand:
        print(f"   brand_id={brand['brand_id']} name={brand['name']!r} price={brand['price']}")
        print(f"   pack_size_label={brand['pack_size_label']!r}")
    else:
        print("   FAIL: brand not found")
    print("B. PMBI lookup drug_code=1:")
    pmbi = sanity["pmbi"]
    if pmbi:
        print(f"   drug_code={pmbi['drug_code']} generic_name={pmbi['generic_name']!r}")
        print(f"   mrp={pmbi['mrp']} unit_size={pmbi['unit_size']!r}")
    else:
        print("   FAIL: PMBI product not found")
    print("C. Match brand_id=9644, drug_code=1:")
    match = sanity["match"]
    if match:
        print(
            f"   method={match['match_method']} confidence={match['match_confidence']} "
            f"status={match['validation_status']}"
        )
    else:
        print("   FAIL: match not found")
    print("D. Components:")
    print("   brand:")
    for row in sanity["brand_components"]:
        print(f"     seq={row['seq']} {row['ingredient']} — {row['strength']}")
    print("   pmbi:")
    for row in sanity["pmbi_components"]:
        print(f"     seq={row['seq']} {row['ingredient']} — {row['strength']}")
    print("E. Price comparison (derived, not stored):")
    if brand and pmbi:
        print(
            f"   brand_price={brand['price']} pack={brand['pack_size_label']!r} | "
            f"pmbi_mrp={pmbi['mrp']} unit={pmbi['unit_size']!r} | "
            f"price_difference={sanity['price_difference']}"
        )
        print(
            "   Note: difference is arithmetic on stored prices; packs are not "
            "normalized, so this is not a guaranteed saving."
        )
    print("F. Synonyms:")
    print(f"   aciclovir -> {sanity['synonym_aciclovir']}")
    print(f"   acyclovir -> {sanity['synonym_acyclovir']}")


def main() -> int:
    for path in (BRANDS_CLEAN_PATH, PMBI_CLEAN_PATH, MATCHES_PATH, SYNONYM_PATH):
        if not path.exists():
            print(f"Missing required input: {path}", file=sys.stderr)
            return 1

    DB_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        create_schema(conn)
        conn.commit()

        conn.execute("BEGIN")
        brand_n, brand_skip = load_brands(conn)
        pmbi_n, pmbi_skip = load_pmbi(conn)
        match_n = load_matches(conn)
        syn_n = load_synonyms(conn)
        conn.commit()

        conn.executescript(INDEX_SQL)
        conn.commit()

        validation = run_validation(conn)
        sanity = run_sanity_tests(conn)
        print_report(
            validation,
            sanity,
            {
                "brand_parents": brand_n,
                "brand_skip_components": brand_skip,
                "pmbi_parents": pmbi_n,
                "pmbi_skip_components": pmbi_skip,
                "matches": match_n,
                "synonyms": syn_n,
            },
        )

        ok = (
            validation["integrity_check"] == "ok"
            and not validation["foreign_key_check"]
            and validation["duplicate_brand_id"] == 0
            and validation["duplicate_drug_code"] == 0
            and validation["duplicate_match_pairs"] == 0
            and validation["orphan_brand_components"] == 0
            and validation["orphan_pmbi_components"] == 0
            and validation["orphan_match_candidates"] == 0
            and validation["synonym_count"] == 63
            and sanity["brand"] is not None
            and sanity["brand"]["brand_id"] == 9644
            and sanity["pmbi"] is not None
            and sanity["pmbi"]["generic_name"] == "Aceclofenac 100mg and Paracetamol 325mg Tablets"
            and sanity["match"] is not None
            and sanity["synonym_aciclovir"] == {"variant_name": "aciclovir", "canonical_name": "acyclovir"}
        )
        if not ok:
            print("\nVALIDATION/SANITY FAILED", file=sys.stderr)
            return 1
        print("\nVALIDATION AND SANITY TESTS PASSED")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
