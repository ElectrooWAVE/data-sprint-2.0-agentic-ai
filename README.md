# The Procurement Desk — Decision Engine & Demo Console

Deterministic procurement negotiation engine (`src/`) scoring **40/40 (100%)** on worked examples and **1.00** on the competition dataset, wrapped read-only by a FastAPI backend (`app/`) and an offline neomorphic UI (`ui/`).

- **Zero runtime LLM or internet calls:** 100% deterministic `Decimal` arithmetic (`ROUND_HALF_UP`).
- **Offline-ready UI:** Plain HTML/CSS/JS with local SVG icons and charts (zero CDNs, XSS-safe DOM text rendering).
- **Read-only wrapper:** API and batch jobs never write to `checkpoints/` or `submission*.csv`.

---

## Run locally

Install dependencies (Python 3.14 recommended) and launch the app with one command:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser.

### Verify engine & API tests

```bash
python -m pytest app/tests/test_api.py -v
python src/evaluate.py
python src/predict.py --fresh && python src/validate.py
```

---

## Health Check Endpoint

`GET /api/health` returns `200 OK` when the playbook is loaded and the 40-sample self-check passes:

```json
{
  "status": "ok",
  "engine": "frozen deterministic rule engine (src/), 0 LLM calls",
  "playbook": {"categories": 15, "suppliers": 40, "amendments": 9, "precedents": 8},
  "datasets": {"sample_threads": 40, "real_threads": 2000},
  "sample_selfcheck": {"passed": true, "correct": 40, "total": 40, "accuracy": 100.0},
  "submission_consistency": {"available": true, "checked": 2000, "matches": 2000, "passed": true}
}
```

---

## Deploy

All configuration is environment-driven (see `.env.example`). Paths are relative to the project root, and CORS defaults to **same-origin only** unless `CORS_ORIGINS` is set.

### 1. Docker

```bash
docker build -t procurement-desk .
docker run -d --name procurement-desk -p 8000:8000 procurement-desk
```

Verify health: `curl http://localhost:8000/api/health`

### 2. Render

1. Push this repository to GitHub/GitLab.
2. In Render, create a **New Web Service** and choose **Docker** (it will automatically detect `Dockerfile` and inject `$PORT`).
   - *Or* choose **Python** runtime with:
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Set **Health Check Path** to `/api/health`.
4. (Optional) Set `CORS_ORIGINS` only if hosting a separate frontend domain.

### 3. Plain VM (Ubuntu / Debian)

```bash
git clone <repo-url> procurement-desk && cd procurement-desk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

Place Nginx or Caddy in front with TLS (`proxy_pass http://127.0.0.1:8000;`) and run `uvicorn` under `systemd` with `Restart=always`.
