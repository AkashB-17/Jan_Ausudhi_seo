from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import KENDRA_DB_PATH
from app.main import app

pytestmark = pytest.mark.skipif(
    not KENDRA_DB_PATH.exists(),
    reason="Run scripts/import_kendras.py to build data/database/kendras.db.",
)

client = TestClient(app)


def test_pin_search_lists_same_pin_first_then_nearby():
    body = client.get("/kendras/search", params={"pincode": "110029"}).json()
    kendras = body["kendras"]
    assert kendras, "expected Kendras around Safdarjung Hospital"
    matches = [k["match"] for k in kendras]
    # All same-PIN results come before any nearby ones.
    assert matches == sorted(matches, key=lambda m: m != "same_pin")
    assert kendras[0]["pincode"] == "110029"
    assert all(k["pincode"].startswith("110") for k in kendras)
    assert body["total"] >= len(kendras)
    assert "janaushadhi.gov.in" in body["source"]


def test_pin_search_pages_without_repeats():
    first = client.get("/kendras/search", params={"pincode": "691506", "limit": 5}).json()
    second = client.get(
        "/kendras/search", params={"pincode": "691506", "limit": 5, "offset": 5}
    ).json()
    codes = [k["kendra_code"] for k in first["kendras"] + second["kendras"]]
    assert len(codes) == len(set(codes)) == 10


def test_browse_by_state_and_district():
    states = client.get("/kendras/states").json()
    names = {s["name"] for s in states}
    assert {"Kerala", "Delhi", "Uttar Pradesh"} <= names
    districts = client.get("/kendras/districts", params={"state": "Kerala"}).json()
    kollam = next(d for d in districts if d["name"] == "Kollam")
    body = client.get(
        "/kendras/search", params={"state": "Kerala", "district": "Kollam", "limit": 100}
    ).json()
    assert body["total"] == kollam["count"]
    assert all(k["district"] == "Kollam" and k["match"] == "district" for k in body["kendras"])


def test_kendra_search_validation():
    assert client.get("/kendras/search", params={"pincode": "12ab"}).status_code == 400
    assert client.get("/kendras/search", params={"pincode": "012345"}).status_code == 400
    assert client.get("/kendras/search").status_code == 400
    assert client.get("/kendras/search", params={"state": "Kerala"}).status_code == 400
    assert client.get("/kendras/districts", params={"state": "Atlantis"}).status_code == 404
