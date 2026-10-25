from __future__ import annotations

import sqlite3

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.database import db_session
from app.prescription.documents import MAX_UPLOAD_BYTES, DocumentError
from app.prescription.ocr import OcrUnavailableError
from app.prescription.service import match_lines, scan_document
from app.schemas import PrescriptionLine, PrescriptionScanResponse, PrescriptionTextRequest

router = APIRouter(prefix="/prescriptions", tags=["prescriptions"])

MAX_TEXT_LINES = 200


@router.post("/scan", response_model=PrescriptionScanResponse)
def scan_prescription(file: UploadFile = File(...)) -> PrescriptionScanResponse:
    # Read one byte past the limit so oversized uploads are rejected without buffering them.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        with db_session() as conn:
            return scan_document(conn, data)
    except DocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OcrUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc


@router.post("/match-text", response_model=PrescriptionScanResponse)
def match_prescription_text(body: PrescriptionTextRequest) -> PrescriptionScanResponse:
    """Re-match prescription text, e.g. after the user corrects OCR output."""
    texts = [t.strip() for t in body.text.splitlines() if t.strip()]
    if not texts:
        raise HTTPException(status_code=400, detail="No text lines to match.")
    if len(texts) > MAX_TEXT_LINES:
        raise HTTPException(
            status_code=400, detail=f"At most {MAX_TEXT_LINES} lines can be matched."
        )
    lines = [PrescriptionLine(page=1, text=t, ocr_confidence=1.0) for t in texts]
    try:
        with db_session() as conn:
            return match_lines(conn, lines, engine="manual-text", pages=1)
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail="Database query failed.") from exc
