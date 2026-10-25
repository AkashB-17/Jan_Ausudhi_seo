from __future__ import annotations

import io
import os
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.prescription import ocr
from app.prescription.matcher import parse_line
from app.prescription.ocr import OcrBox, OcrLine, group_into_lines

client = TestClient(app)
SAMPLES = Path(__file__).resolve().parents[1] / "samples"


class FakeEngine:
    name = "fake"

    def __init__(self, lines: list[str]):
        self.lines = lines

    def read(self, image):
        return [OcrLine(text=t, confidence=0.9) for t in self.lines]


@pytest.fixture
def fake_engine():
    def install(lines: list[str]) -> None:
        ocr.set_engine(FakeEngine(lines))

    yield install
    ocr.set_engine(None)


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((60, 72 + 20 * i), line, fontsize=12)
    return doc.tobytes()


def _match_text(text: str) -> dict:
    response = client.post("/prescriptions/match-text", json={"text": text})
    assert response.status_code == 200
    return response.json()


# ── Line parsing ────────────────────────────────────────────────────


def test_parse_line_strips_dosing_instructions():
    parsed = parse_line("1. Tab Augmentin 625 Duo  1-0-1 x 5 days after food")
    assert parsed.words == ["augmentin", "duo"]
    assert parsed.numbers == {625.0}
    assert parsed.form == "tablet"


def test_parse_line_keeps_syrup_strength_but_not_dose_volume():
    assert parse_line("Syp Ascoril LS 10ml TDS").numbers == set()
    assert parse_line("Syp Calpol 125mg/5ml").numbers == {125.0, 5.0}


def test_parse_line_ignores_letterhead():
    assert parse_line("Patient: Ravi Kumar  Age: 42  Date: 12/10/2026").words == []
    assert parse_line("City Care Clinic, Reg No. 45821").words == []


def test_group_into_lines_orders_boxes():
    boxes = [
        OcrBox("650", 0.9, 220, 102, 280, 128),
        OcrBox("Tab", 0.8, 10, 100, 60, 130),
        OcrBox("Dolo", 1.0, 70, 98, 200, 131),
        OcrBox("Telma 40", 0.7, 10, 200, 160, 230),
    ]
    lines = group_into_lines(boxes)
    assert [line.text for line in lines] == ["Tab Dolo 650", "Telma 40"]
    assert lines[0].confidence == pytest.approx(0.9)


# ── Matching ────────────────────────────────────────────────────────


def test_match_text_finds_brands_despite_ocr_typos():
    body = _match_text("Tab Augmentn 625\nTab Dolo 650 SOS")
    names = [m["brand"]["name"] for m in body["medicines"]]
    assert names == ["Augmentin 625 Duo Tablet", "Dolo 650 Tablet"]


def test_match_text_returns_jan_aushadhi_generic_with_matching_strength():
    body = _match_text("Tab Dolo 650")
    medicine = body["medicines"][0]
    assert medicine["generics"], "expected a Jan Aushadhi option"
    generic = medicine["generics"][0]
    assert "650" in generic["generic_name"]
    assert generic["match_source"] in {"validated_match", "composition_match"}
    assert medicine["price_difference"] == pytest.approx(
        medicine["brand"]["price"] - medicine["cheapest_generic_mrp"], abs=0.01
    )


def test_match_text_generic_name_uses_ingredient_lookup():
    body = _match_text("Tab Paracetamol 650")
    medicine = body["medicines"][0]
    assert medicine["kind"] == "generic"
    assert medicine["ingredients"] == ["paracetamol"]
    assert all(g["match_source"] == "ingredient_lookup" for g in medicine["generics"])
    # Exact single-ingredient products only; no paracetamol + caffeine combos.
    assert all(g["norm_composition"] == "paracetamol (650mg)" for g in medicine["generics"])


def test_match_text_flags_unclear_lines_for_review():
    medicine = _match_text("Dolo")["medicines"][0]
    assert medicine["needs_review"] is True
    assert "strength_not_written" in medicine["review_reasons"]


def test_match_text_leaves_non_medicines_unmatched():
    body = _match_text("Dr. Anil Mehta MBBS\nTab Zzqxwv 500")
    assert body["medicines"] == []
    assert len(body["unmatched_lines"]) == 2


def test_match_text_validation():
    assert client.post("/prescriptions/match-text", json={"text": ""}).status_code == 422
    assert client.post("/prescriptions/match-text", json={"text": "  \n "}).status_code == 400


# ── Upload endpoint ─────────────────────────────────────────────────


def test_scan_pdf_with_text_layer_skips_ocr(fake_engine):
    fake_engine(["this should not be used"])
    pdf = _pdf_bytes(["Dr. Test MBBS", "Rx", "1. Tab Telma 40  0-0-1"])
    response = client.post(
        "/prescriptions/scan", files={"file": ("rx.pdf", pdf, "application/pdf")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "pdf-text"
    assert [m["brand"]["name"] for m in body["medicines"]] == ["Telma 40 Tablet"]


def test_scan_image_uses_ocr_engine(fake_engine):
    fake_engine(["Tab Pan 40 1-0-0 before food"])
    response = client.post(
        "/prescriptions/scan", files={"file": ("rx.png", _png_bytes(), "image/png")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "fake"
    assert body["medicines"][0]["brand"]["name"] == "PAN 40 Tablet"
    assert body["medicines"][0]["line"]["ocr_confidence"] == pytest.approx(0.9)


def test_scan_rejects_unsupported_and_empty_files():
    bad = client.post("/prescriptions/scan", files={"file": ("a.txt", b"hello", "text/plain")})
    assert bad.status_code == 400
    empty = client.post("/prescriptions/scan", files={"file": ("a.png", b"", "image/png")})
    assert empty.status_code == 400


@pytest.mark.skipif(
    not os.environ.get("RUN_OCR_TESTS"),
    reason="Loads the real EasyOCR model; set RUN_OCR_TESTS=1 to run.",
)
def test_scan_sample_with_real_ocr():
    sample = SAMPLES / "prescription_printed.png"
    with sample.open("rb") as fh:
        response = client.post("/prescriptions/scan", files={"file": (sample.name, fh, "image/png")})
    assert response.status_code == 200
    names = {
        m["brand"]["name"] if m["brand"] else m["ingredients"][0]
        for m in response.json()["medicines"]
    }
    assert {"Augmentin 625 Duo Tablet", "Dolo 650 Tablet", "Telma 40 Tablet"} <= names
