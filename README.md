# The Procurement Desk — Decision Engine & Demo Console

**Live Demo:** https://data-sprint-2-0-agentic-ai.vercel.app

Deterministic procurement negotiation engine (`src/`) scoring **40/40 (100%)** on worked examples and **1.00** on the competition dataset, wrapped read-only by a **stateless** FastAPI backend (`app/`, `api/index.py`) and an offline neomorphic UI (`ui/`).

- **Zero runtime LLM or internet calls:** 100% deterministic `Decimal` arithmetic (`ROUND_HALF_UP`).
- **100% Stateless Serverless Architecture:**
  - Default datasets (`threads.csv` and `sample_threads.csv`) are precomputed into static JSON in `ui/data/` via `scripts/precompute.py` for instant loading.
  - Uploaded CSVs are parsed in-browser and evaluated in 250-row stateless chunks via `POST /api/batch/chunk`; analytics, filtering, sorting, pagination, and CSV export run client-side.
- **Offline-ready UI:** Plain HTML/CSS/JS with local SVG icons and charts (zero CDNs, XSS-safe DOM text rendering).
- **Read-only wrapper:** API never writes to `checkpoints/` or `submission*.csv`; any temporary files use `/tmp`.

---

## Run locally

Install dependencies and launch the app with one command:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser.

### Precompute static datasets & verify

```bash
python scripts/precompute.py
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
  "mode": "stateless",
  "playbook": {"categories": 15, "suppliers": 40, "amendments": 9, "precedents": 8},
  "datasets": {"sample_threads": 40, "real_threads": 2000},
  "sample_selfcheck": {"passed": true, "correct": 40, "total": 40, "accuracy": 100.0},
  "submission_consistency": {"available": true, "checked": 2000, "matches": 2000, "passed": true}
}
```

---

## Deploy

All configuration is environment-driven (see `.env.example`). Paths are relative to the project root, and CORS defaults to **same-origin only** unless `CORS_ORIGINS` is set.

### 1. Vercel (Serverless)

**Live Deployment:** https://data-sprint-2-0-agentic-ai.vercel.app

`api/index.py` and `vercel.json` are preconfigured for zero-config Vercel deployment:
- `/api/*` routes to the FastAPI serverless function (`api/index.py`, `maxDuration: 60`, `includeFiles` bundling `app/**`, `src/**`, `playbook/**`, and CSVs).
- `ui/` and precomputed `ui/data/` are served as static assets.
- Compatible with Vercel's default **Python 3.12** as well as **Python 3.13** and **3.14**.

Simply import the GitHub repo at **https://vercel.com/new** and click **Deploy**.

### 2. Docker

```bash
docker build -t procurement-desk .
docker run -d --name procurement-desk -p 8000:8000 procurement-desk
```

Verify health: `curl http://localhost:8000/api/health`

### 3. Render

1. Connect the repository in Render → **New Web Service** → **Docker** (auto-detects `Dockerfile` and `$PORT`).
2. Set **Health Check Path** to `/api/health`.

### 4. Plain VM (Ubuntu / Debian)

```bash
git clone <repo-url> procurement-desk && cd procurement-desk
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

Place Nginx or Caddy in front with TLS (`proxy_pass http://127.0.0.1:8000;`) and run under `systemd`.
