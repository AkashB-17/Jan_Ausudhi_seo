# Jan Aushadhi Finder

Find cheaper **Jan Aushadhi (PMBI) generic medicines** for the branded medicines a doctor
prescribes. Search a brand by name, or **upload a photo or PDF of a prescription** and
get the Jan Aushadhi option and listed price difference for every medicine on it.

Match results are data-derived candidates, not medical substitutions or prescriptions.
Price differences are arithmetic on stored MRPs; pack sizes are not always comparable.
Always confirm with a doctor or pharmacist before switching medicines.

## Project layout

| Path | What it is |
|---|---|
| `updated_indian_medicine_data.csv` | Source: ~254k Indian branded medicines (name, price, composition, pack) |
| `Product List_2_10_2026 @ 9_3_36.csv` | Source: 2,439 PMBI Jan Aushadhi products (generic name, unit size, MRP) |
| `scripts/match_validation.py` | Cleans both datasets, parses compositions, matches and validates brand↔PMBI pairs |
| `scripts/build_database.py` | Loads the pipeline outputs into `data/database/medicines.db` (SQLite) |
| `scripts/matching_experiment.py` | Earlier first-pass matching experiment (not needed to build the DB) |
| `scripts/make_sample_prescriptions.py` | Generates the sample prescriptions in `samples/` |
| `scripts/import_kendras.py` | Loads PMBI's Kendra list (PDF export) into `data/database/kendras.db` |
| `app/` | FastAPI backend (read-only over the SQLite DB) |
| `app/prescription/` | Prescription scanning: file loading, OCR, brand matching |
| `index.html`, `frontend/` | Vanilla JS frontend |
| `tests/` | pytest suite |
| `report.md` | API verification report |

`data/` is git-ignored, so a fresh clone has to build the database first (below).

## Setup

Python 3.11 is recommended. From the project root:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The `torch` line installs the CUDA build for NVIDIA GPUs. Skip it to get the CPU build
from `requirements.txt`; OCR then takes a few seconds per page instead of ~1 s.

On the first prescription scan EasyOCR downloads its models (~95 MB) to `~/.EasyOCR`.

### Build the database

Skip this if you already have `data/database/medicines.db`.

```
.venv\Scripts\python.exe scripts\match_validation.py
.venv\Scripts\python.exe scripts\build_database.py
```

For the **Kendra finder**, download the store list: open
https://janaushadhi.gov.in/locate-kendra, click **Download PDF** (all ~21,700 Kendras),
save it in `data/raw/`, then import it into `data/database/kendras.db`:

```
.venv\Scripts\python.exe scripts\import_kendras.py data\raw\kendra_list_2026-11-06.pdf
```

It is a separate database so rebuilding `medicines.db` keeps it. Owner names in the PDF
are not imported. Re-run with a newer export to refresh the list.

`match_validation.py` writes `data/processed/` (cleaned CSVs, validated matches, a report).
It reads hand-checked ingredient synonyms from `data/processed/verified_synonyms.json`
if present; without that file, spelling variants such as aciclovir/acyclovir are not merged.

## Run

```
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

- API: http://127.0.0.1:8000
- Docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

Frontend, in a second terminal (port 5500 is allowed by the API's CORS settings):

```
.venv\Scripts\python.exe -m http.server 5500
```

Open http://127.0.0.1:5500. If the API runs on a port other than 8000, add
`?api=http://127.0.0.1:8001` (with your port) to the page URL.

## Using the website

- **Search**: type a brand name, open it, and see its validated Jan Aushadhi matches and a
  side-by-side price comparison.
- **Scan prescription**: upload a JPG/PNG/WEBP/BMP/TIFF photo or a PDF (≤10 MB, first 5
  pages). Each recognised medicine gets a card with:
  - the line it was read from, and a **Check this** badge with reasons when the match is
    uncertain (similar names, no strength written, hard-to-read text);
  - the Jan Aushadhi options, with a per-tablet price comparison when pack sizes allow;
  - **Not this one?** buttons to switch to a similar brand.

  The recognised text is editable: fix misread names and press **Match again**.
- **Find Kendra**: enter a PIN code to list Jan Aushadhi Kendras in that PIN and the
  nearest PINs around it, or browse by state and district. Each store links to Google
  Maps. The official list has no map coordinates, so "nearest" means the closest PIN
  number within the same postal sorting district (the first three digits).

## Tests

```
.venv\Scripts\python.exe -m pytest -q
```

The test that loads the real EasyOCR model is skipped by default; set
`RUN_OCR_TESTS=1` to run it.

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
| POST | `/prescriptions/match-text` | `{"text": "..."}` — match text (≤200 lines), e.g. corrected OCR output |
| GET | `/kendras/search?pincode=` | Kendras in a PIN, then nearby PINs (`limit` ≤100, `offset`) |
| GET | `/kendras/search?state=&district=` | Kendras in a district |
| GET | `/kendras/states`, `/kendras/districts?state=` | Areas with Kendra counts |

## How prescription scanning works

`app/prescription/` turns a prescription photo or PDF into Jan Aushadhi options:

1. **`documents.py`** checks the file type by its contents, not its extension. PDFs with a
   text layer are read directly (no OCR); scanned PDFs are rendered at 200 dpi; photos are
   rotated upright using their EXIF data.
2. **`ocr.py`** runs the OCR engine, which returns text lines with a confidence. Engines are
   pluggable (see below); the default is pretrained EasyOCR.
3. **`matcher.py`** parses each line into name words, strengths and a dosage form, dropping
   dosing instructions (`1-0-1`, `x 5 days`, `BD`, `10ml`) and letterhead lines (doctor,
   clinic, patient, date). The name is fuzzy-matched against all ~246k brand names, so OCR
   errors like "Augmentn" still match, then scored on strength and form. Lines that name a
   generic ("Paracetamol 650") are matched to PMBI products by ingredient instead.
   To avoid matching ordinary text, the brand must account for at least half the line's
   name words, lines with more than six name words are skipped, and lines without a form
   word, unit strength (`500mg`) or dosing (`1-0-1`, `BD`) need a near-exact name. An
   upload with no such line at all (a CV, an invoice) is reported as
   `"warning": "not_a_prescription"` with no medicines.
4. **`service.py`** finds the Jan Aushadhi options for each medicine and reports why a line
   needs review (`ambiguous_name`, `strength_not_written`, `low_ocr_confidence`, …).

Each Jan Aushadhi option carries a `match_source`:

| `match_source` | Meaning |
|---|---|
| `validated_match` | Strong match from the validation pipeline (`match_candidates`) |
| `composition_match` | Same ingredients, strengths and dosage form; used when the pipeline has no Strong match |
| `ingredient_lookup` | Prescription named the generic; matched by ingredient (and strength if written) |

PMBI products whose stored composition has fewer ingredients than their name lists (e.g.
"Telmisartan 40mg + Cilnidipine 10mg + Metoprolol…" stored as telmisartan only) are left
out of `composition_match` and `ingredient_lookup`, so a combination is never offered for a
single-ingredient brand.

Try it with the files in `samples/` (regenerate them with
`.venv\Scripts\python.exe scripts\make_sample_prescriptions.py`; needs Windows fonts).

### Accuracy

Tested on synthetic samples only: a digital PDF, a printed image and a handwriting-style
image. All six medicines were found in each. Real handwritten prescriptions are much
harder, and accuracy on them has not been measured; neat or printed prescriptions work
best. Uncertain lines are flagged for review rather than guessed.

### OCR engines and plugging in a fine-tuned model

Choose the engine with environment variables before starting the API:

| Variable | Values |
|---|---|
| `PRESCRIPTION_OCR_ENGINE` | `easyocr` (default), `trocr`, `claude`, `local-vlm`, or `package.module:factory` |
| `PRESCRIPTION_TROCR_MODEL` | Hugging Face id or local folder (default `microsoft/trocr-base-handwritten`) |
| `PRESCRIPTION_OCR_GPU` | `1` / `0` to force GPU on/off (default: auto) |

#### Reading doctor's handwriting (vision-language models)

EasyOCR and TrOCR read printed text and neat handwriting, but return noise on a typical
doctor's scrawl. The two vision readers in `app/prescription/vlm.py` read the whole
page the way a pharmacist does, using layout, Tab/Cap prefixes, dosing patterns and
knowledge of drug names. Each returns one line per medicine with up to three
alternative readings, and the dictionary matcher keeps whichever reading names a real
brand or generic. The model's guess is never shown as a medicine unless it matches the
database. Letterhead, patient details and advice such as physiotherapy are left out.

| Engine | Setup | Notes |
|---|---|---|
| `claude` | `pip install anthropic`, set `ANTHROPIC_API_KEY` | Best on messy handwriting. **The image is sent to Anthropic's API.** Each scan is billed; check usage in the Anthropic Console. `PRESCRIPTION_CLAUDE_MODEL` (default `claude-opus-5-5`) and `PRESCRIPTION_CLAUDE_EFFORT` (default `high`) override the defaults. |
| `local-vlm` | `pip install transformers accelerate` | **Experimental.** Runs `Qwen/Qwen3-VL-2B-Instruct` on this machine (~4 GB download, fits a 6 GB GPU, ~15–70 s per page); nothing leaves the machine. On real handwritten prescriptions it misread most names and **invented medicines that were not written** (e.g. an injection on a physiotherapy note), so every line it returns is flagged for review. `PRESCRIPTION_VLM_MODEL` takes another Hugging Face id or a local folder; set `HF_HOME` to choose where the model is stored. |

```
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:PRESCRIPTION_OCR_ENGINE = "claude"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Each result card shows what the reader saw ("Written: …") and the dosing. Lines matched
through an alternative reading, or read with low legibility, are flagged **Check this**.

To use a fine-tuned handwriting model:

1. `pip install transformers`
2. Fine-tune a TrOCR `VisionEncoderDecoderModel` on prescription word/line crops and save
   it with `model.save_pretrained(dir)` and `processor.save_pretrained(dir)`.
3. Start the API with `PRESCRIPTION_OCR_ENGINE=trocr` and `PRESCRIPTION_TROCR_MODEL=dir`.
   EasyOCR still finds the text regions; your model reads them.

Start fine-tuning from `microsoft/trocr-base-handwritten` (verified to work; downloads
~1.3 GB). With transformers 5.x, `microsoft/trocr-small-*` loads with a mismatched
tokenizer and outputs nonsense, so avoid the small checkpoints.

For a completely different model, write a class with a `name` attribute and a
`read(image: numpy.ndarray) -> list[OcrLine]` method, and set
`PRESCRIPTION_OCR_ENGINE=yourpackage.yourmodule:YourClass`.
