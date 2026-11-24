"""Tests for the FastAPI backend and wrapper-vs-frozen-engine consistency."""

from __future__ import annotations

import csv
import hashlib
import io
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.batch_manager import BatchManager
from app.config import Settings, settings
from app.engine_wrapper import get_engine
from app.main import app


def _poll_batch(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/batch/{job_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("completed", "failed"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"Batch {job_id} did not finish within {timeout}s")


def _snapshot_hashes() -> dict[str, str]:
    targets = [settings.root / "submission.csv", settings.root / "submission_final.csv"]
    ckpt_dir = settings.root / "checkpoints"
    if ckpt_dir.is_dir():
        targets.extend(sorted(p for p in ckpt_dir.iterdir() if p.is_file()))
    return {
        str(p.relative_to(settings.root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in targets
        if p.exists()
    }


def test_consistency_all_2000_real_and_40_samples():
    """MOST IMPORTANT: wrapper labels must match evaluate_thread(), submission_final.csv, and sample_answers.csv 100%."""
    import sys
    sys.path.insert(0, str(settings.root / "src"))
    from rules import evaluate_thread

    eng = get_engine()
    assert len(eng.real_threads) == 2000
    assert len(eng.sample_threads) == 40

    with (settings.root / "submission_final.csv").open("r", encoding="utf-8", newline="") as f:
        sub_final = {row["thread_id"]: row["label"] for row in csv.DictReader(f)}
    assert len(sub_final) == 2000

    with tempfile.TemporaryDirectory() as td:
        wpath = Path(td) / "warnings.jsonl"
        for t in eng.real_threads:
            direct = evaluate_thread(t, eng.playbook, warnings_path=wpath).label
            explained = eng.explain(t, source="real")
            assert explained["decision"]["label"] == direct, f"Wrapper drifted from evaluate_thread on {t.thread_id}"
            assert explained["decision"]["label"] == sub_final[t.thread_id], f"Mismatch vs submission_final on {t.thread_id}"
            assert len(explained["steps"]) == 6

        for t in eng.sample_threads:
            direct = evaluate_thread(t, eng.playbook, warnings_path=wpath).label
            explained = eng.explain(t, source="sample")
            assert explained["decision"]["label"] == direct
            assert explained["decision"]["label"] == eng.sample_answers[t.thread_id]
            assert explained["decision"]["matches_expected"] is True


def test_health_and_meta():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        h = r.json()
        assert h["status"] == "ok"
        assert h["sample_selfcheck"]["passed"] is True
        assert h["sample_selfcheck"]["correct"] == 40
        assert h["sample_selfcheck"]["total"] == 40
        assert h["submission_consistency"]["passed"] is True
        assert h["submission_consistency"]["matches"] == 2000

        rm = client.get("/api/meta")
        assert rm.status_code == 200
        m = rm.json()
        assert len(m["categories"]) == 15
        assert len(m["suppliers"]) == 40
        assert len(m["amendments"]) == 9


def test_threads_listing():
    with TestClient(app) as client:
        r = client.get("/api/threads?source=sample&limit=50")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 40
        assert len(data["items"]) == 40
        assert all(item["matches_expected"] is True for item in data["items"])

        r2 = client.get("/api/threads?source=real&q=Monsoon&action=COUNTER&limit=10")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["total"] > 0
        assert len(d2["items"]) <= 10
        assert all(it["predicted_label"].startswith("COUNTER:") for it in d2["items"])


def test_decide_all_40_samples_and_special_cases():
    with TestClient(app) as client:
        eng = get_engine()
        for t in eng.sample_threads:
            r = client.post("/api/decide", json={"thread_id": t.thread_id, "source": "sample"})
            assert r.status_code == 200
            body = r.json()
            assert body["decision"]["label"] == eng.sample_answers[t.thread_id]
            assert body["decision"]["matches_expected"] is True
            assert len(body["steps"]) == 6

        # COUNTER case with breakdown & numbers
        rc = client.post("/api/decide", json={"thread_id": "T00010", "source": "sample"})
        assert rc.status_code == 200
        dc = rc.json()
        assert dc["decision"]["label"] == "COUNTER:COUNTER_OFFER:85000"
        assert dc["decision"]["counter_price_formatted"] == "₹85,000"
        assert dc["fired_step"] == 5
        assert dc["steps"][5]["status"] == "skipped"
        assert len(dc["numbers"]["price_breakdown"]) >= 2

        # PROMPT_INJECTION case with highlights & earlier history
        rp = client.post("/api/decide", json={"thread_id": "T00028", "source": "sample"})
        assert rp.status_code == 200
        dp = rp.json()
        assert dp["decision"]["label"] == "ESCALATE:PROMPT_INJECTION:"
        assert dp["fired_step"] == 3
        assert dp["message_analysis"]["has_earlier_history"] is True
        assert any(h["kind"] == "injection" for h in dp["message_analysis"]["highlights"])

        # Custom thread
        rcust = client.post(
            "/api/decide",
            json={
                "supplier": "Coastline Caterers",
                "category": "catering",
                "quantity": 10,
                "round": 1,
                "date": "2026-10-01",
                "supplier_message": "Hello, our quote is INR 265 per meal.",
            },
        )
        assert rcust.status_code == 200
        assert rcust.json()["decision"]["label"] == "ACCEPT:ACCEPTABLE_PRICE:"

        # Bad custom thread
        rbad = client.post(
            "/api/decide",
            json={
                "supplier": "Unknown Co",
                "category": "catering",
                "quantity": 10,
                "round": 1,
                "date": "2026-10-01",
                "supplier_message": "INR 265 per meal",
            },
        )
        assert rbad.status_code == 400
        assert client.post("/api/decide", json={"thread_id": "DOES_NOT_EXIST"}).status_code == 404


def test_batch_sample_upload_results_download_and_analytics():
    with TestClient(app) as client:
        raw = settings.sample_threads_csv.read_bytes()
        r = client.post("/api/batch", files={"file": ("sample_threads.csv", raw, "text/csv")})
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        status = _poll_batch(client, job_id)
        assert status["status"] == "completed"
        assert status["done"] == 40
        assert status["total"] == 40

        res = client.get(f"/api/batch/{job_id}/results?sort_by=thread_id&limit=100")
        assert res.status_code == 200
        rows = res.json()["rows"]
        assert len(rows) == 40

        dl = client.get(f"/api/batch/{job_id}/download")
        assert dl.status_code == 200
        parsed_dl = {r["thread_id"]: r["label"] for r in csv.DictReader(io.StringIO(dl.text))}
        assert parsed_dl == get_engine().sample_answers

        an = client.get(f"/api/analytics/{job_id}")
        assert an.status_code == 200
        aj = an.json()
        assert aj["total"] == 40
        assert sum(aj["counts_by_action"].values()) == 40
        assert "by_category" in aj
        assert "by_supplier_status" in aj
        assert "price_gap" in aj
        assert "counter_rate_by_round" in aj


def test_batch_real_2000_threads():
    with TestClient(app) as client:
        r = client.post("/api/batch?dataset=real")
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        status = _poll_batch(client, job_id, timeout=30.0)
        assert status["status"] == "completed"
        assert status["done"] == 2000

        dl = client.get(f"/api/batch/{job_id}/download")
        assert dl.status_code == 200
        assert dl.content == settings.submission_final_csv.read_bytes()

        an = client.get(f"/api/analytics/{job_id}")
        assert an.status_code == 200
        aj = an.json()
        assert aj["counts_by_action"] == {"ACCEPT": 358, "COUNTER": 629, "REJECT": 333, "ESCALATE": 680}
        assert aj["warnings_count"] == 92


def test_batch_validation_limits_and_ttl():
    with TestClient(app) as client:
        # Missing columns
        r1 = client.post("/api/batch", files={"file": ("bad.csv", b"thread_id,date\nT1,2026-10-01\n", "text/csv")})
        assert r1.status_code == 400
        assert "Missing required column" in r1.json()["detail"]["message"]

        # Bad row (unknown supplier + bad date)
        bad_row = (
            "thread_id,date,supplier,category,quantity,round,our_last_counter,supplier_message\n"
            "T1,not-a-date,Fake Supplier,catering,10,1,,Hello\n"
        ).encode("utf-8")
        r2 = client.post("/api/batch", files={"file": ("bad_row.csv", bad_row, "text/csv")})
        assert r2.status_code == 400
        assert len(r2.json()["detail"]["errors"]) == 1

        # Too many rows (> 5000)
        header = "thread_id,date,supplier,category,quantity,round,our_last_counter,supplier_message\n"
        line = "T{i},2026-10-01,Coastline Caterers,catering,10,1,,INR 250 per meal\n"
        too_many = (header + "".join(line.format(i=i) for i in range(5001))).encode("utf-8")
        r3 = client.post("/api/batch", files={"file": ("many.csv", too_many, "text/csv")})
        assert r3.status_code == 400
        assert "Too many rows" in r3.json()["detail"]["message"]

        # Too large (> 5 MB)
        oversized = b"x" * (settings.max_batch_bytes + 10)
        r4 = client.post("/api/batch", files={"file": ("big.csv", oversized, "text/csv")})
        assert r4.status_code == 400
        assert "too large" in r4.json()["detail"]["message"].lower()

    # TTL cleanup
    eng = get_engine()
    mgr = BatchManager(eng, ttl_seconds=1)
    job = mgr.create(eng.sample_threads[:2], "ttl-test")
    deadline = time.time() + 5.0
    while time.time() < deadline and job.status != "completed":
        time.sleep(0.02)
    assert job.status == "completed"
    assert mgr.cleanup(now=job.created_at + 2.0) == 1
    assert mgr.get(job.job_id) is None


def test_config_defaults_and_no_disk_mutations():
    before = _snapshot_hashes()
    cfg_default = Settings.from_env({})
    assert cfg_default.cors_origins == ()
    assert not str(cfg_default.rel(cfg_default.playbook_dir)).startswith("E:")

    cfg_cors = Settings.from_env({"CORS_ORIGINS": "http://localhost:3000, https://demo.example.com/"})
    assert cfg_cors.cors_origins == ("http://localhost:3000", "https://demo.example.com")

    with TestClient(app) as client:
        r = client.get("/api/health", headers={"Origin": "http://evil.example.com"})
        assert "access-control-allow-origin" not in r.headers

    after = _snapshot_hashes()
    assert before == after, "API endpoints must NEVER modify checkpoints/ or submission*.csv!"
