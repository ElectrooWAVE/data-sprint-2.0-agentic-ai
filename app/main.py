"""FastAPI app: JSON API under /api and the static UI (ui/) served from the same process.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.batch_manager import BatchManager
from app.config import settings
from app.engine_wrapper import CsvValidationError, get_engine, parse_threads_csv
from app.schemas import DecideRequest

_batch: BatchManager | None = None


def get_batch() -> BatchManager:
    global _batch
    if _batch is None:
        _batch = BatchManager(get_engine(), settings.job_ttl_seconds)
    return _batch


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


@app.post("/api/batch")
async def create_batch(
    request: Request,
    dataset: str | None = Query(None, description="Use a bundled dataset instead of uploading: real | sample"),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    eng = get_engine()
    mgr = get_batch()
    if file is not None:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_batch_bytes + 64 * 1024:
            raise _bad_request(f"Upload is too large (limit {settings.max_batch_bytes:,} bytes).")
        raw = await file.read(settings.max_batch_bytes + 1)
        try:
            threads = parse_threads_csv(raw, eng.playbook, settings.max_batch_bytes, settings.max_batch_rows)
        except CsvValidationError as e:
            raise _bad_request(e.message, e.errors)
        name = (file.filename or "upload.csv")[:100]
    elif dataset in ("real", "sample"):
        threads = list(eng.real_threads if dataset == "real" else eng.sample_threads)
        name = settings.rel(settings.threads_csv if dataset == "real" else settings.sample_threads_csv)
        if not threads:
            raise _bad_request(f"Bundled dataset {dataset!r} is not available on this server.")
    else:
        raise _bad_request("Upload a CSV file (multipart field 'file') or pass ?dataset=real or ?dataset=sample.")
    job = mgr.create(threads, name)
    return {"job_id": job.job_id, "status": job.status, "total": job.total, "source": name}


def _job_or_404(job_id: str):
    job = get_batch().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"message": f"Batch job {job_id!r} not found or expired.", "errors": []})
    return job


def _completed_job(job_id: str):
    job = _job_or_404(job_id)
    if job.status != "completed":
        raise HTTPException(status_code=409, detail={"message": f"Batch job is {job.status}.", "errors": [job.error] if job.error else []})
    return job


@app.get("/api/batch/{job_id}")
def batch_status(job_id: str) -> dict[str, Any]:
    _job_or_404(job_id)
    return get_batch().progress(job_id)


SORTABLE = {"thread_id", "date", "supplier", "supplier_status", "category", "quantity", "round", "label",
            "action", "reason", "counter_price", "fired_step", "p", "L_r", "total_value"}
NUMERIC = {"quantity", "round", "counter_price", "fired_step", "p", "L_r", "total_value"}


@app.get("/api/batch/{job_id}/results")
def batch_results(
    job_id: str,
    q: str = Query("", max_length=100),
    action: str | None = Query(None),
    reason: str | None = Query(None),
    category: str | None = Query(None),
    supplier: str | None = Query(None),
    round: int | None = Query(None, ge=1),
    sort_by: str = Query("thread_id"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    job = _completed_job(job_id)
    if sort_by not in SORTABLE:
        raise _bad_request(f"Cannot sort by {sort_by!r}.", [f"allowed: {', '.join(sorted(SORTABLE))}"])
    ql = q.strip().lower()
    rows = [
        r for r in job.rows
        if (not ql or any(ql in str(r[k]).lower() for k in ("thread_id", "supplier", "category", "label")))
        and (not action or r["action"] == action)
        and (not reason or r["reason"] == reason)
        and (not category or r["category"] == category)
        and (not supplier or r["supplier"] == supplier)
        and (round is None or r["round"] == round)
    ]

    def key(r):
        v = r.get(sort_by)
        if v is None:
            return (1, 0)
        return (0, float(v) if sort_by in NUMERIC else str(v).lower())

    present = [r for r in rows if r.get(sort_by) is not None]
    absent = [r for r in rows if r.get(sort_by) is None]
    present.sort(key=key, reverse=(sort_dir == "desc"))
    rows = present + absent
    return {"job_id": job_id, "total": len(rows), "offset": offset, "limit": limit, "rows": rows[offset:offset + limit]}


@app.get("/api/batch/{job_id}/download")
def batch_download(job_id: str) -> Response:
    job = _completed_job(job_id)
    return Response(
        content=BatchManager.submission_csv(job),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="submission_{job_id}.csv"'},
    )


@app.get("/api/analytics/{job_id}")
def analytics(job_id: str) -> dict[str, Any]:
    job = _completed_job(job_id)
    return {"job_id": job_id, "source": job.source_name, **job.analytics}


if settings.ui_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(settings.ui_dir), html=True), name="ui")
