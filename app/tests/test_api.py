"""Tests for the stateless FastAPI backend, precomputed ui/data files, and wrapper consistency."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from api.index import app as vercel_app
from app.config import Settings, settings
from app.engine_wrapper import get_engine
from app.main import app


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


def test_precomputed_static_data_matches_truth():
    """Precomputed ui/data/* files must match submission_final.csv and sample_answers.csv 100%."""
    data_dir = settings.ui_dir / "data"
    real_json = json.loads((data_dir / "real.json").read_text(encoding="utf-8"))
    sample_json = json.loads((data_dir / "sample.json").read_text(encoding="utf-8"))

    assert real_json["total"] == 2000
    assert len(real_json["rows"]) == 2000
    assert sample_json["total"] == 40
    assert len(sample_json["rows"]) == 40

    with settings.submission_final_csv.open("r", encoding="utf-8", newline="") as f:
        sub_final = {row["thread_id"]: row["label"] for row in csv.DictReader(f)}
    assert {r["thread_id"]: r["label"] for r in real_json["rows"]} == sub_final
    assert (data_dir / "real_submission.csv").read_bytes() == settings.submission_final_csv.read_bytes()

    eng = get_engine()
    assert {r["thread_id"]: r["label"] for r in sample_json["rows"]} == eng.sample_answers


def test_health_and_meta():
    assert vercel_app is app
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        h = r.json()
        assert h["status"] == "ok"
        assert h["mode"] == "stateless"
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

        rc = client.post("/api/decide", json={"thread_id": "T00010", "source": "sample"})
        assert rc.status_code == 200
        dc = rc.json()
        assert dc["decision"]["label"] == "COUNTER:COUNTER_OFFER:85000"
        assert dc["decision"]["counter_price_formatted"] == "₹85,000"
        assert dc["fired_step"] == 5

        rp = client.post("/api/decide", json={"thread_id": "T00028", "source": "sample"})
        assert rp.status_code == 200
        dp = rp.json()
        assert dp["decision"]["label"] == "ESCALATE:PROMPT_INJECTION:"
        assert dp["fired_step"] == 3
        assert any(h["kind"] == "injection" for h in dp["message_analysis"]["highlights"])

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


def test_batch_chunk_stateless_and_validation():
    with TestClient(app) as client:
        # 1. Sample 40 threads via /api/batch/chunk
        with settings.sample_threads_csv.open("r", encoding="utf-8", newline="") as f:
            sample_rows = list(csv.DictReader(f))
        r_sample = client.post("/api/batch/chunk", json={"rows": sample_rows})
        assert r_sample.status_code == 200
        got_sample = {r["thread_id"]: r["label"] for r in r_sample.json()["rows"]}
        assert got_sample == get_engine().sample_answers

        # 2. All 2,000 real threads via 8 chunks of 250
        with settings.threads_csv.open("r", encoding="utf-8", newline="") as f:
            real_rows = list(csv.DictReader(f))
        with settings.submission_final_csv.open("r", encoding="utf-8", newline="") as f:
            sub_final = {row["thread_id"]: row["label"] for row in csv.DictReader(f)}

        got_real: dict[str, str] = {}
        for i in range(0, len(real_rows), 250):
            resp = client.post("/api/batch/chunk", json={"rows": real_rows[i : i + 250]})
            assert resp.status_code == 200
            for row in resp.json()["rows"]:
                got_real[row["thread_id"]] = row["label"]
        assert got_real == sub_final

        # 3. Validation errors: >250 rows, empty chunk, bad row
        assert client.post("/api/batch/chunk", json={"rows": real_rows[:251]}).status_code == 422
        assert client.post("/api/batch/chunk", json={"rows": []}).status_code == 422
        bad = {
            "thread_id": "BAD1",
            "date": "not-a-date",
            "supplier": "Fake Supplier",
            "category": "catering",
            "quantity": "10",
            "round": "1",
            "our_last_counter": "",
            "supplier_message": "Hello",
        }
        r_bad = client.post("/api/batch/chunk", json={"rows": [bad]})
        assert r_bad.status_code == 400
        assert "invalid row" in r_bad.json()["detail"]["message"].lower()


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
