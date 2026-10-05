# Jan Aushadhi — API

Read-only FastAPI layer over `data/database/medicines.db`.

Match results are data-derived candidates, not medical substitutions or prescriptions.
Price difference is arithmetic on stored values; pack sizes are not normalized.

## Start

From the project root:

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Then:

- API: http://127.0.0.1:8000
- Docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

## Tests

```
.venv\Scripts\python.exe -m pytest -q
```

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/medicines/search?q=` | Brand name search, max 50 |
| GET | `/medicines/{brand_id}` | Brand detail + components |
| GET | `/medicines/{brand_id}/matches` | Strong matches only |
| GET | `/pmbi/{drug_code}` | PMBI detail + stored components |
| GET | `/compare/{brand_id}/{drug_code}` | Strong-match comparison only |
