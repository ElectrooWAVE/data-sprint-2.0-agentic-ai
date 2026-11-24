# The Procurement Desk — Execution Plan (`PLAN.md`)

**Status:** **FROZEN (`2026-11-23`)** — Final submission: `submission_final.csv` (and `submission.csv`).

## Stages

- **Stage 0: Playbook & Sample Analysis (`NOTES.md`)** — **COMPLETED & APPROVED**
  - Read entire playbook (`README.md`, `policies/`, `categories/`, `suppliers/`, `amendments/`, `precedents/`) and 40 worked samples.
  - Documented ordered procedure, codes, normalization, amendment precedence, supplier overrides, precedents, formula/rounding, walkthroughs, and confirmations in `NOTES.md`.

- **Stage 1: Core Deterministic Rule Engine & Sample Evaluation** — **COMPLETED (`40/40 = 100.00%`)**
  - `src/load.py`: Dynamic parser for playbook files (`Decimal` values, `effective_date` sorting, contract overrides, precedents) and CSV threads/answers with `tqdm` progress bars.
  - `src/rules.py`: Ordered checks 1–6 (one function per step, returning decision or `"continue"` with full trace), `Decimal` + `ROUND_HALF_UP` counter pricing, `round > R` clamping + logging, and `checkpoints/warnings.jsonl` logging for any parsing/rule uncertainty.
  - `src/evaluate.py`: Resumable evaluation on `sample_threads.csv` vs `sample_answers.csv` with `tqdm`, `checkpoints/sample_decisions.jsonl`, accuracy + mismatch trace table.

- **Stage 2: Profiling of `threads.csv`, Pattern Expansion & Invariant Self-Check** — **COMPLETED**
  - `src/profile_threads.py` -> `PROFILE.md`: Profiled all 2,000 rows of `threads.csv`.
  - Extended `src/rules.py` strictly from `PROFILE.md` and the playbook (word-number prices + lakhs, `k` suffix, volume-discount thresholds, Hindi/HTML-comment/`P.S.`/`admin override`/policy-preapproval injections, `liability ... shall be unlimited`, `buy these items only from us`).
  - `src/tests_patterns.py`: **29/29** playbook-derived pattern tests pass.
  - `src/selfcheck.py`: **0 mismatches** across 30 sample + 1,423 `threads.csv` `round > 1` threads (`our_last_counter == round_half_up(L(r-1))`); **1,908/2,000** parsed positive prices + **92** explicit playbook `MISSING_INFO` triggers.

- **Stage 3: Prediction, Checkpointing & Validation** — **COMPLETED**
  - `src/llm_extract.py`: Gemini (`temperature=0`) fallback module with SHA-256 disk cache (`0` LLM calls needed; 100% deterministic coverage).
  - `src/predict.py`: Processes all 2,000 rows of `threads.csv` with `tqdm` and resumable `checkpoints/decisions.jsonl`, writes `submission.csv` and `submission_final.csv`.
  - `src/validate.py`: Passes all schema, ID-order, action/reason, integer counter-price, and trailing-colon checks.

- **Stage 4: Review Audit (`REVIEW.md`) & Project Freeze** — **COMPLETED**
  - `src/review_flags.py` -> `REVIEW.md`: Stratified review report across warnings, prompt injections + security keywords, banned terms + allowed decoys, near-threshold boundary cases, ±3-day amendment windows, and `MISSING_INFO`.
  - Saved baseline snapshot `submission_v1.csv` and frozen final output `submission_final.csv` (byte-identical to `submission.csv`).

- **Demo Stage 1: FastAPI Backend (`app/`) & Consistency Tests** — **COMPLETED (`8/8 pytest passed`)**
  - `app/config.py`: Env-driven relative-path settings (`HOST`, `PORT`, `CORS_ORIGINS` same-origin default, `MAX_BATCH_BYTES=5MB`, `MAX_BATCH_ROWS=5000`, `JOB_TTL_SECONDS=3600`).
  - `app/engine_wrapper.py`: Read-only wrapper around `src/load.py` and `src/rules.py` exposing 6-step traces, price normalization breakdown, numbers/amendments/contracts, and character-offset highlights (zero writes to `checkpoints/` or `submission*.csv`).
  - `app/batch_manager.py`: Thread-safe locked in-memory batch runner with 1-hour TTL cleanup and analytics aggregator.
  - `app/main.py`: FastAPI routes (`/api/health`, `/api/meta`, `/api/threads`, `/api/decide`, `/api/batch`, `/api/batch/{id}`, `/api/batch/{id}/results`, `/api/batch/{id}/download`, `/api/analytics/{id}`).
  - `app/tests/test_api.py`: `8/8` passed, including `2,000/2,000` wrapper-vs-`evaluate_thread()`-vs-`submission_final.csv` and `40/40` sample consistency checks.

- **Demo Stage 2: Neomorphic Offline Frontend (`ui/`)** — **COMPLETED**
  - `ui/index.html`, `ui/styles.css`, `ui/app.js`, `ui/assets/favicon.svg`: 100% offline peach-to-lavender neomorphic UI, deep plum `#3d2b4f` high-contrast text, icon+text status chips, skeleton loaders, Indian digit grouping (`₹1,05,042`), zero `innerHTML` on untrusted message text, inline SVG charts & price gauges.
  - Verified live in headless Chrome at `1366x768` and `390x844` (`0` console/page errors, `0` horizontal overflow), tested all 8 thread types (`T00010` counter, `T00028` injection, `T00022` banned term, `T00006` blacklisted, `T00012` probation, `T00019` foreign FX, `T00014` amendment+contract, `T10823` missing info) and full 2,000-thread batch (`0.90s` UI end-to-end), saving 11 screenshots to `ui_screenshots/`.

- **Demo Stage 3: Polish, Accessibility, End-to-End Verification & Screenshots** — **COMPLETED (verified alongside Stage 2)**
- **Demo Stage 4: Hosting Readiness & Demo Script** — **COMPLETED**
  - `requirements.txt` (compatible with Python 3.12–3.14), `Dockerfile` (`python:3.14-slim` + `/api/health` `HEALTHCHECK`), `.dockerignore`, `.env.example`, `README.md`, and `DEMO.md`.

- **Demo Stage 5: Stateless Vercel Serverless Refactor** — **COMPLETED**
  - `scripts/precompute.py` -> `ui/data/` (`real.json`, `sample.json`, `real_submission.csv`, `sample_submission.csv`): precomputed static payloads for instant default dataset loading.
  - Replaced stateful in-memory job polling with stateless `POST /api/batch/chunk` (up to 250 rows/request) + client-side CSV parsing, chunked progress, analytics, filtering, sorting, pagination, and CSV download in `ui/app.js`.
  - Added `api/index.py` and `vercel.json` (`maxDuration: 60`, `includeFiles`, static `ui/` + `/data/` routing) and routed temp directories to `/tmp` when present.
  - Verified with `pytest`, `evaluate.py`, unchanged `submission_final.csv` SHA-256, and a 2-worker `uvicorn --workers 2` Playwright batch upload test.

---

## Final Output & Verification Summary
- **Sample accuracy (`evaluate.py`):** `40/40 (100.00%)`, `0` mismatches.
- **Pattern unit tests (`tests_patterns.py`):** `29/29` passed.
- **Multi-round invariant (`selfcheck.py`):** `30/30` (sample) and `1,423/1,423` (`threads.csv`), `0` mismatches.
- **Submission validation (`validate.py`):** `2,000` rows, exact `sample_submission.csv` ID order, `0` format errors.
- **LLM API usage:** `0` threads / `0` calls.
- **Label distribution (`2,000` total):**
  - `ESCALATE`: **680** (`OVER_APPROVAL_LIMIT`: 284, `PROMPT_INJECTION`: 160, `BANNED_TERM`: 144, `MISSING_INFO`: 92)
  - `COUNTER`: **629** (`COUNTER_OFFER`: 629)
  - `ACCEPT`: **358** (`ACCEPTABLE_PRICE`: 358)
  - `REJECT`: **333** (`ABOVE_WALKAWAY`: 228, `BLACKLISTED_SUPPLIER`: 105)
- **Warnings logged (`checkpoints/warnings.jsonl`, `92` total):**
  - `no_price_found`: **55**
  - `unknown_currency`: **32** (`JPY`: 13, `SGD`: 10, `AED`: 9)
  - `missing_billing_period`: **5**

---

## Known Risks & Residual Edge Cases
1. **Step 1 (`MISSING_INFO`) precedence over Steps 2–4 (`BLACKLISTED_SUPPLIER`, `PROMPT_INJECTION`, `BANNED_TERM`):** Implemented literally per `policies/decision-procedure.md`. Audited across all 2,000 rows of `threads.csv` and confirmed **0** threads overlap between `MISSING_INFO` and Steps 2–4.
2. **Full-precision `p <= L(r)` vs rounded `L(r)` boundary:** Implemented literally per `policies/units-and-currency.md` (compare full precision, round `L(r)` with `ROUND_HALF_UP` only for `COUNTER_OFFER` output). Audited across all 2,000 rows and confirmed **0** boundary flips, **0** threads within ₹1 of `L(r)`, and **0** threads within ₹100 of `approval_limit` (closest thread is `0.6454%` from `L(r)`).
3. **Overlapping amendments (`AMD-08` `2026-12-04` vs `AMD-07` `2026-12-05`) and supplier contract overrides:** Resolved strictly by chronological `Effective from` date, with supplier contracts overriding both base category values and amendments. Verified against `our_last_counter` across all `1,423` multi-round threads with **0** mismatches.
4. **Generalization to unseen future phrasing:** The deterministic regex rule engine covers 100% of message templates observed in `sample_threads.csv` and `threads.csv`. Any novel unparsed phrasing on future unseen inputs triggers a logged warning in `checkpoints/warnings.jsonl` and can optionally be routed to `src/llm_extract.py` (Gemini, `temperature=0`, SHA-256 disk cache) when an API key is configured.
