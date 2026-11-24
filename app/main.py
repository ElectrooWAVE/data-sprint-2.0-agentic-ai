"""FastAPI app: stateless JSON API under /api and the static UI (ui/) served from the same process.

Run locally:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.engine_wrapper import CsvValidationError, get_engine
from app.schemas import BatchChunkRequest, DecideRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_engine()
    yield


app = FastAPI(title="Procurement Desk", version="1.0.0", lifespan=lifespan)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


def _bad_request(message: str, errors: list[str] | None = None) -> HTTPException:
    return HTTPException(status_code=400, detail={"message": message, "errors": errors or []})


@app.get("/api/health")
def health() -> dict[str, Any]:
    eng = get_engine()
    pb = eng.playbook
    return {
        "status": "ok" if eng.sample_selfcheck["passed"] else "degraded",
        "engine": "frozen deterministic rule engine (src/), 0 LLM calls",
        "mode": "stateless",
        "playbook": {
            "categories": len(pb.categories),
            "suppliers": len({s.name for s in pb.suppliers.values()}),
            "amendments": len(pb.amendments),
            "precedents": len(pb.precedents),
        },
        "datasets": {"sample_threads": len(eng.sample_threads), "real_threads": len(eng.real_threads)},
        "sample_selfcheck": eng.sample_selfcheck,
        "submission_consistency": eng.submission_consistency,
        "paths": {
            "playbook_dir": settings.rel(settings.playbook_dir),
            "threads_csv": settings.rel(settings.threads_csv),
            "sample_threads_csv": settings.rel(settings.sample_threads_csv),
        },
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    pb = get_engine().playbook
    sups = {s.name: s for s in pb.suppliers.values()}
    return {
        "categories": [
            {"slug": c.slug, "name": c.name, "normalized_unit": c.normalized_unit, "rounds": c.rounds,
             "gst_rate": str(c.gst_rate), "target": str(c.target_price), "walkaway": str(c.walkaway_price),
             "approval_limit": str(c.approval_limit), "billing_period": c.billing_period}
            for c in sorted(pb.categories.values(), key=lambda c: c.slug)
        ],
        "suppliers": [
            {"name": s.name, "status": s.status,
             "price_caps": {k: str(v) for k, v in s.price_caps.items()},
             "fx_rates": {k: str(v) for k, v in s.fx_rates.items()}}
            for s in sorted(sups.values(), key=lambda s: s.name)
        ],
        "amendments": [
            {"id": a.amd_id, "category": a.category_slug, "effective_from": a.effective_from.isoformat(),
             "field": a.field_name, "new_value": str(a.new_value), "text": a.raw_text}
            for a in pb.amendments
        ],
    }


@app.get("/api/threads")
def list_threads(
    source: str = Query("sample", pattern="^(sample|real)$"),
    q: str = Query("", max_length=100),
    category: str | None = Query(None, max_length=100),
    action: str | None = Query(None, pattern="^(ACCEPT|COUNTER|REJECT|ESCALATE)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    eng = get_engine()
    threads = eng.sample_threads if source == "sample" else eng.real_threads
    ql = q.strip().lower()
    items = []
    for t in threads:
        label = eng.labels[t.thread_id]
        if ql and ql not in t.thread_id.lower() and ql not in t.supplier.lower() and ql not in t.category.lower():
            continue
        if category and t.category != category:
            continue
        if action and not label.startswith(action + ":"):
            continue
        expected = eng.sample_answers.get(t.thread_id) if source == "sample" else None
        items.append({
            "thread_id": t.thread_id, "date": t.thread_date.isoformat(), "supplier": t.supplier,
            "supplier_status": eng.supplier_status(t.supplier), "category": t.category,
            "quantity": t.quantity, "round": t.round_num, "predicted_label": label,
            "expected_label": expected, "matches_expected": (label == expected) if expected else None,
        })
    return {"source": source, "total": len(items), "offset": offset, "limit": limit, "items": items[offset:offset + limit]}


@app.post("/api/decide")
def decide(req: DecideRequest) -> dict[str, Any]:
    eng = get_engine()
    if req.is_custom():
        missing = req.missing_custom_fields()
        if missing:
            raise _bad_request("Custom thread is missing required field(s).", [f"missing {m}" for m in missing])
        try:
            thread = eng.build_custom_thread(req.model_dump())
        except CsvValidationError as e:
            raise _bad_request(e.message, e.errors)
        return eng.explain(thread, source=None)
    if not req.thread_id:
        raise _bad_request("Provide either thread_id or a full custom thread.",
                           ["thread_id, or: supplier, category, quantity, round, date, supplier_message"])
    hit = eng.find_thread(req.thread_id, req.source)
    if hit is None:
        raise HTTPException(status_code=404, detail={"message": f"Thread {req.thread_id!r} not found.", "errors": []})
    thread, source = hit
    return eng.explain(thread, source=source)


@app.post("/api/batch/chunk")
def batch_chunk(req: BatchChunkRequest) -> dict[str, Any]:
    """Stateless batch chunk evaluator: accepts up to 250 rows and returns full row summaries."""
    eng = get_engine()
    try:
        rows = eng.process_chunk(req.rows, max_chunk_rows=settings.max_chunk_rows)
    except CsvValidationError as e:
        raise _bad_request(e.message, e.errors)
    return {"total": len(rows), "rows": rows}


if settings.ui_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(settings.ui_dir), html=True), name="ui")
