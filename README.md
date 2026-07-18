# Real-door
The project for the hackathon

RealDoor is an application-readiness helper for a LIHTC applicant in the
Boston-Cambridge-Quincy, MA HMFA. It never decides eligibility — it only
returns `READY_TO_REVIEW` or `NEEDS_REVIEW` with citations, for a human
reviewer.

## Run it

```bash
pip install -r app/requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/household/HH-001` (or any household id). The
app builds on top of `realdoor-hackathon-starter-pack/` (data, rules,
synthetic documents, the reference `calculate.py`/`rules.py`/
`load_documents.py`, and the required output schema) without modifying it.

- `app/` — the FastAPI application (3 modules: Profile, Understanding,
  Preparation). See `app/main.py` for routes and `app/extraction.py` for
  how PDF fields are pulled out (font-style/position based, no OCR/LLM).
- `tests/` — end-to-end regression tests: all 6 household fixtures against
  the evaluation gold, adversarial/injection handling, and schema
  validation. Run with `python -m unittest discover -s tests -v`.

Local package data lives in `app_data/` (gitignored) and is only ever
viewed, downloaded, or deleted by the user — nothing is ever submitted
automatically.

