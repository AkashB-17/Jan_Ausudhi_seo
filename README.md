# Jan Aushadhi — API

Read-only FastAPI layer over `data/database/medicines.db`.

Match results are data-derived candidates, not medical substitutions or prescriptions.
Price difference is arithmetic on stored values; pack sizes are not normalized.

## Start

From the project root (Python 3.11 recommended):

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The `torch` line installs the CUDA build for NVIDIA GPUs; skip it to get the CPU build
from `requirements.txt` (OCR then takes a few seconds per page instead of ~1 s).

Then:

- API: http://127.0.0.1:8000
- Docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

Frontend: serve the project root on a CORS-allowed port, e.g.
`.venv\Scripts\python.exe -m http.server 5500` and open http://127.0.0.1:5500.
If the API runs on another port, add `?api=http://127.0.0.1:8001` to the page URL.

## Tests

```
.venv\Scripts\python.exe -m pytest -q
```

The real-OCR test is skipped by default; set `RUN_OCR_TESTS=1` to run it.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/medicines/search?q=` | Brand name search, max 50 |
| GET | `/medicines/{brand_id}` | Brand detail + components |
| GET | `/medicines/{brand_id}/matches` | Strong matches only |
| GET | `/pmbi/{drug_code}` | PMBI detail + stored components |
| GET | `/compare/{brand_id}/{drug_code}` | Strong-match comparison only |
| POST | `/prescriptions/scan` | Upload image/PDF (multipart `file`, ≤10 MB, ≤5 pages) |
| POST | `/prescriptions/match-text` | `{"text": "..."}` — re-match corrected text |

## Prescription scanning

`app/prescription/` turns a prescription photo or PDF into Jan Aushadhi options:

1. **`documents.py`** — checks the file by magic bytes. PDFs with a text layer are read
   directly (no OCR); scanned PDFs are rendered at 200 dpi; photos are EXIF-rotated.
2. **`ocr.py`** — the OCR engine returns text lines with a confidence. Engines are pluggable
   (see below); the default is pretrained EasyOCR.
3. **`matcher.py`** — each line is parsed into name words, strengths and a dosage form,
   dropping dosing (`1-0-1`, `x 5 days`, `BD`, `10ml`) and letterhead lines. The name is
   fuzzy-matched against all ~246k brand names (so OCR errors like "Augmentn" still
   match), scored on strength and form. Lines that name a generic ("Paracetamol 650")
   are matched to PMBI products by ingredient instead.
4. **`service.py`** — finds Jan Aushadhi options for each medicine and returns why a line
   needs review (`ambiguous_name`, `strength_not_written`, `low_ocr_confidence`, …).

Each Jan Aushadhi option carries a `match_source`:

| `match_source` | Meaning |
|---|---|
| `validated_match` | Strong match from the validation pipeline (`match_candidates`) |
| `composition_match` | Same ingredients, strengths and dosage form; used when the pipeline has no Strong match |
| `ingredient_lookup` | Prescription named the generic; matched by ingredient (and strength if written) |

PMBI products whose stored composition has fewer ingredients than their name lists
(69 products, e.g. "Telmisartan 40mg + Cilnidipine 10mg + Metoprolol…" stored as
telmisartan only) are excluded from `composition_match` and `ingredient_lookup`.

Sample files for trying it: `samples/` (regenerate with
`.venv\Scripts\python.exe scripts\make_sample_prescriptions.py`).

### OCR engines and plugging in a fine-tuned model

Choose the engine with environment variables before starting the API:

| Variable | Values |
|---|---|
| `PRESCRIPTION_OCR_ENGINE` | `easyocr` (default), `trocr`, or `package.module:factory` |
| `PRESCRIPTION_TROCR_MODEL` | Hugging Face id or local folder (default `microsoft/trocr-base-handwritten`) |
| `PRESCRIPTION_OCR_GPU` | `1` / `0` to force GPU on/off (default: auto) |

To use a fine-tuned handwriting model:

1. `pip install transformers`
2. Fine-tune a TrOCR `VisionEncoderDecoderModel` on prescription word/line crops and save it
   with `model.save_pretrained(dir)` and `processor.save_pretrained(dir)`.
3. Start the API with `PRESCRIPTION_OCR_ENGINE=trocr` and `PRESCRIPTION_TROCR_MODEL=dir`.
   EasyOCR still finds the text regions; your model reads them.

Start fine-tuning from `microsoft/trocr-base-handwritten` (verified to work). With
transformers 5.x, `microsoft/trocr-small-*` loads with a mismatched tokenizer and
outputs nonsense, so avoid the small checkpoints.

For a completely different model, write a class with a `name` attribute and a
`read(image: numpy.ndarray) -> list[OcrLine]` method, and set
`PRESCRIPTION_OCR_ENGINE=yourpackage.yourmodule:YourClass`.
