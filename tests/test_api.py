from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_search_a_clo_p():
    response = client.get("/medicines/search", params={"q": "A Clo P"})
    assert response.status_code == 200
    payload = response.json()
    names = [item["name"] for item in payload["results"]]
    assert any("A Clo P" in name for name in names)
    assert payload["count"] <= 50


def test_search_missing_and_short_query():
    assert client.get("/medicines/search").status_code == 400
    assert client.get("/medicines/search", params={"q": " "}).status_code == 400
    assert client.get("/medicines/search", params={"q": "A"}).status_code == 400


def test_search_no_results():
    response = client.get("/medicines/search", params={"q": "zzzxnotabrandname999"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["results"] == []


def test_brand_details():
    response = client.get("/medicines/9644")
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "A Clo P 100mg/325mg Tablet"
    assert payload["brand_id"] == 9644
    ingredients = {(c["ingredient"], c["strength"]) for c in payload["components"]}
    assert ("aceclofenac", "100mg") in ingredients
    assert ("paracetamol", "325mg") in ingredients


def test_brand_matches_strong_only():
    response = client.get("/medicines/9644/matches")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brand_id"] == 9644
    codes = [m["drug_code"] for m in payload["matches"]]
    assert 1 in codes
    assert all(m["validation_status"] == "Strong" for m in payload["matches"])


def test_pmbi_details():
    response = client.get("/pmbi/1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["generic_name"] == "Aceclofenac 100mg and Paracetamol 325mg Tablets"
    ingredients = {(c["ingredient"], c["strength"]) for c in payload["components"]}
    assert ("aceclofenac", "100mg") in ingredients
    assert ("paracetamol", "325mg") in ingredients


def test_compare_strong_match():
    response = client.get("/compare/9644/1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["brand"]["price"] == 35.0
    assert payload["pmbi"]["mrp"] == 10.32
    assert payload["price_comparison"]["difference"] == 24.68
    assert payload["match"]["validation_status"] == "Strong"
    assert payload["price_comparison"]["brand_pack"] == "strip of 10 tablets"
    assert payload["price_comparison"]["pmbi_unit"] == "10's"


def test_not_found():
    assert client.get("/medicines/999999999").status_code == 404
    assert client.get("/medicines/999999999/matches").status_code == 404
    assert client.get(" /pmbi/999999999".strip()).status_code == 404
    assert client.get("/compare/999999999/1").status_code == 404
    assert client.get("/compare/9644/999999999").status_code == 404


def test_unsafe_pmbi_calamine_has_empty_components():
    response = client.get("/pmbi/115")
    assert response.status_code == 200
    payload = response.json()
    assert payload["drug_code"] == 115
    assert payload["norm_composition"] == "calamine"
    assert payload["components"] == []


def test_non_strong_match_cannot_be_compared():
    # Existing Rejected pair from the pipeline; must not be presented as comparison.
    response = client.get("/compare/1465/5")
    assert response.status_code == 400
    assert response.json()["detail"]
