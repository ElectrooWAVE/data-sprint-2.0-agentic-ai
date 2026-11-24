/* Procurement Desk UI — pure vanilla JS, zero CDNs, zero innerHTML on untrusted message text. */
(() => {
  "use strict";

  const state = {
    source: "sample",
    preset: "all",
    search: "",
    threads: [],
    selectedId: null,
    meta: null,
    expandedSteps: new Set(),
    batchJobId: null,
    batchPollTimer: null,
    batchSortBy: "thread_id",
    batchSortDir: "asc",
    batchOffset: 0,
    batchLimit: 25,
    batchTotal: 0,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- Indian Rupee & Number Formatting ----------
  function formatINR(val, decimals = 2, withSymbol = true) {
    if (val === null || val === undefined || val === "") return "—";
    const num = Number(val);
    if (Number.isNaN(num)) return String(val);
    const neg = num < 0 ? "-" : "";
    const fixed = Math.abs(num).toFixed(decimals);
    const [ip, fp] = fixed.split(".");
    let grouped = ip;
    if (ip.length > 3) {
      const tail = ip.slice(-3);
      let head = ip.slice(0, -3);
      const parts = [];
      while (head.length > 2) {
        parts.unshift(head.slice(-2));
        head = head.slice(0, -2);
      }
      if (head) parts.unshift(head);
      grouped = parts.concat(tail).join(",");
    }
    return `${neg}${withSymbol ? "₹" : ""}${grouped}${fp !== undefined ? "." + fp : ""}`;
  }

  // ---------- Accessible SVG Icons (local inline DOM) ----------
  function makeActionChip(action, small = false) {
    const span = document.createElement("span");
    span.className = `status-chip chip-${action}` + (small ? " status-chip-sm" : "");

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", small ? "12" : "15");
    svg.setAttribute("height", small ? "12" : "15");
    svg.setAttribute("aria-hidden", "true");

    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");

    if (action === "ACCEPT") {
      path.setAttribute("d", "M3 8.5 L6.5 12 L13 4.5");
    } else if (action === "COUNTER") {
      path.setAttribute("d", "M2.5 5.5 H13 M10 2.5 L13 5.5 L10 8.5 M13.5 11 H3 M6 8 L3 11 L6 14");
    } else if (action === "REJECT") {
      path.setAttribute("d", "M4 4 L12 12 M12 4 L4 12");
    } else {
      path.setAttribute("d", "M8 2.5 L14 13 H2 Z M8 6.5 V9.5 M8 11.2 V11.5");
    }
    svg.appendChild(path);

    const txt = document.createElement("span");
    txt.textContent = action;
    span.appendChild(svg);
    span.appendChild(txt);
    return span;
  }

  // ---------- Safe API helper ----------
  async function apiJSON(url, options = {}) {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data && data.detail;
      const msg = (detail && detail.message) || data.message || `Request failed (${res.status})`;
      const errs = (detail && Array.isArray(detail.errors) && detail.errors.length)
        ? " " + detail.errors.slice(0, 4).join(" | ")
        : "";
      throw new Error(msg + errs);
    }
    return data;
  }

  // ---------- Tab Navigation ----------
  function switchTab(tabName) {
    const isExplain = tabName === "explain";
    $("tab-explain").classList.toggle("active", isExplain);
    $("tab-explain").setAttribute("aria-selected", String(isExplain));
    $("tab-batch").classList.toggle("active", !isExplain);
    $("tab-batch").setAttribute("aria-selected", String(!isExplain));
    $("panel-explain").hidden = !isExplain;
    $("panel-batch").hidden = isExplain;
  }

  $("tab-explain").addEventListener("click", () => switchTab("explain"));
  $("tab-batch").addEventListener("click", () => switchTab("batch"));

  // WAI-ARIA ArrowLeft / ArrowRight keyboard navigation between tabs
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const next = btn.id === "tab-explain" ? $("tab-batch") : $("tab-explain");
        next.click();
        next.focus();
      }
    });
  });

  // ---------- Health & Meta Init ----------
  async function initHealthAndMeta() {
    try {
      const [h, meta] = await Promise.all([apiJSON("/api/health"), apiJSON("/api/meta")]);
      state.meta = meta;
      const sc = h.sample_selfcheck;
      $("health-summary").textContent =
        `${h.playbook.categories} categories • ${h.playbook.suppliers} suppliers • ${h.playbook.amendments} amendments • Self-check ${sc.correct}/${sc.total} (${sc.accuracy}%) • 0 LLM calls`;
      populateMetaSelects(meta);
    } catch (err) {
      $("health-summary").textContent = `Backend unavailable: ${err.message}`;
    }
  }

  function populateMetaSelects(meta) {
    const cSup = $("c-supplier");
    const cCat = $("c-category");
    const resCat = $("res-category");
    const resSup = $("res-supplier");

    cSup.replaceChildren();
    meta.suppliers.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.status === "Approved" ? s.name : `${s.name} [${s.status}]`;
      cSup.appendChild(opt);
    });

    cCat.replaceChildren();
    meta.categories.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.slug;
      opt.textContent = `${c.name} (${c.slug})`;
      cCat.appendChild(opt);
    });

    meta.categories.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.slug;
      opt.textContent = c.name;
      resCat.appendChild(opt);
    });

    meta.suppliers.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.name;
      opt.textContent = s.name;
      resSup.appendChild(opt);
    });
  }

  // ---------- Tab 1: Thread Picker & Presets ----------
  const PRESET_QUERIES = {
    all: { source: null, params: "" },
    injection: { source: "sample", params: "action=ESCALATE", filterFn: (it) => it.predicted_label === "ESCALATE:PROMPT_INJECTION:" },
    banned: { source: "sample", params: "action=ESCALATE", filterFn: (it) => it.predicted_label === "ESCALATE:BANNED_TERM:" },
    blacklist: { source: "sample", params: "action=REJECT", filterFn: (it) => it.predicted_label === "REJECT:BLACKLISTED_SUPPLIER:" },
    missing: { source: "real", params: "action=ESCALATE", filterFn: (it) => it.predicted_label === "ESCALATE:MISSING_INFO:" },
    counter: { source: "sample", params: "action=COUNTER" },
    probation: { source: null, params: "", filterFn: (it) => it.supplier_status === "PROBATION" },
    fx: { source: "sample", params: "", filterFn: (it) => ["T00016", "T00019", "T00030", "T00037"].includes(it.thread_id) },
    amendment: { source: "sample", params: "", filterFn: (it) => ["T00012", "T00014", "T00038"].includes(it.thread_id) },
  };

  async function loadThreadList() {
    $("thread-list-skeleton").hidden = false;
    $("thread-list").hidden = true;
    $("thread-list-empty").hidden = true;

    const preset = PRESET_QUERIES[state.preset] || PRESET_QUERIES.all;
    const effectiveSource = state.source;
    const limit = state.preset === "all" ? 100 : 250;
    const qs = new URLSearchParams({ source: effectiveSource, q: state.search, limit: String(limit) });
    if (preset.params) {
      const [k, v] = preset.params.split("=");
      qs.set(k, v);
    }

    try {
      const data = await apiJSON(`/api/threads?${qs.toString()}`);
      let items = data.items;
      if (preset.filterFn) {
        items = items.filter(preset.filterFn);
      }
      if (state.preset === "fx" && effectiveSource === "real") {
        items = data.items.filter((it) => ["Kestrel Learning", "Arvind Office Supplies", "Aster Cleaning Co", "Falcon Security Services"].includes(it.supplier));
      }
      if (state.preset === "amendment" && effectiveSource === "real") {
        items = data.items.filter((it) => ["catering", "courier", "headsets", "mobile-phones", "led-lighting"].includes(it.category));
      }
      state.threads = items;
      renderThreadList(items, data.total);

      if (items.length && (!state.selectedId || !items.some((i) => i.thread_id === state.selectedId))) {
        selectThread(items[0].thread_id, effectiveSource);
      }
    } catch (err) {
      $("thread-list-skeleton").hidden = true;
      $("picker-meta").textContent = `Error: ${err.message}`;
    }
  }

  function renderThreadList(items, totalInSource) {
    $("thread-list-skeleton").hidden = true;
    const list = $("thread-list");
    list.replaceChildren();
    $("picker-meta").textContent = `Showing ${items.length} thread(s) (${totalInSource} matching in ${state.source})`;

    if (!items.length) {
      list.hidden = true;
      $("thread-list-empty").hidden = false;
      return;
    }

    $("thread-list-empty").hidden = true;
    list.hidden = false;

    items.slice(0, 80).forEach((it) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "thread-item-btn" + (it.thread_id === state.selectedId ? " selected" : "");
      btn.dataset.threadId = it.thread_id;

      const top = document.createElement("div");
      top.className = "thread-item-top";
      const idSpan = document.createElement("span");
      idSpan.textContent = `${it.thread_id} • ${it.category}`;
      const action = it.predicted_label.split(":")[0];
      top.appendChild(idSpan);
      top.appendChild(makeActionChip(action, true));

      const sub = document.createElement("div");
      sub.className = "thread-item-sub";
      const supSpan = document.createElement("span");
      supSpan.textContent = it.supplier + (it.supplier_status !== "Approved" ? ` [${it.supplier_status}]` : "");
      const metaSpan = document.createElement("span");
      metaSpan.textContent = `R${it.round} • ${it.date}`;
      sub.appendChild(supSpan);
      sub.appendChild(metaSpan);

      btn.appendChild(top);
      btn.appendChild(sub);
      btn.addEventListener("click", () => selectThread(it.thread_id, state.source));
      li.appendChild(btn);
      list.appendChild(li);
    });
  }

  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.classList.contains("active")));
    btn.addEventListener("click", () => {
      document.querySelectorAll(".seg-btn").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-pressed", "true");
      state.source = btn.dataset.source;
      state.selectedId = null;
      loadThreadList();
    });
  });

  document.querySelectorAll(".chip-filter").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.classList.contains("active")));
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chip-filter").forEach((b) => {
        b.classList.remove("active");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("active");
      btn.setAttribute("aria-pressed", "true");
      state.preset = btn.dataset.preset;
      // Switch source automatically if preset specifically targets missing_info (only in real) or sample
      const targetSource = PRESET_QUERIES[state.preset]?.source;
      if (targetSource && targetSource !== state.source) {
        state.source = targetSource;
        document.querySelectorAll(".seg-btn").forEach((b) => {
          const isMatch = b.dataset.source === targetSource;
          b.classList.toggle("active", isMatch);
          b.setAttribute("aria-pressed", String(isMatch));
        });
      }
      state.selectedId = null;
      loadThreadList();
    });
  });

  let searchDebounce = null;
  $("thread-search").addEventListener("input", (e) => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
      state.search = e.target.value;
      loadThreadList();
    }, 180);
  });

  $("toggle-custom-btn").addEventListener("click", () => {
    const wrap = $("custom-form-wrap");
    const open = wrap.hidden;
    wrap.hidden = !open;
    $("toggle-custom-btn").setAttribute("aria-expanded", String(open));
  });

  $("custom-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("custom-error").hidden = true;
    const olcRaw = $("c-olc").value.trim();
    const payload = {
      supplier: $("c-supplier").value,
      category: $("c-category").value,
      quantity: Number($("c-qty").value),
      round: Number($("c-round").value),
      date: $("c-date").value,
      our_last_counter: olcRaw ? Number(olcRaw) : null,
      supplier_message: $("c-msg").value,
    };
    await runDecide(payload);
  });

  async function selectThread(threadId, source) {
    state.selectedId = threadId;
    document.querySelectorAll(".thread-item-btn").forEach((b) => {
      b.classList.toggle("selected", b.dataset.threadId === threadId);
    });
    await runDecide({ thread_id: threadId, source });
  }

  async function runDecide(payload) {
    $("explain-error").hidden = true;
    $("explain-skeleton").hidden = false;
    $("explain-content").hidden = true;
    try {
      const data = await apiJSON("/api/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      renderDecision(data);
    } catch (err) {
      $("explain-skeleton").hidden = true;
      $("explain-error").textContent = err.message;
      $("explain-error").hidden = false;
      if ($("custom-form-wrap") && !payload.thread_id) {
        $("custom-error").textContent = err.message;
        $("custom-error").hidden = false;
      }
    }
  }

  // ---------- Tab 1: Render Decision & Safe Message Highlighting ----------
  function renderDecision(data) {
    $("explain-skeleton").hidden = true;
    $("explain-content").hidden = false;

    const { thread, decision, fired_step, steps, message_analysis, numbers } = data;

    // Decision header
    const chipSlot = $("dec-chip");
    chipSlot.replaceWith(makeActionChip(decision.action, false));
    // Re-tag the newly inserted chip with id="dec-chip"
    document.querySelector(".decision-badges .status-chip").id = "dec-chip";

    const mb = $("dec-match-badge");
    if (decision.matches_expected === true) {
      mb.textContent = "Matches Expected";
      mb.hidden = false;
    } else {
      mb.hidden = true;
    }

    $("dec-label-code").textContent = decision.label;
    $("dec-thread-title").textContent = `${thread.thread_id} — ${thread.supplier} (${thread.category_name})`;
    $("dec-reason-plain").textContent = decision.reason_plain;

    if (decision.counter_price !== null && decision.counter_price !== undefined) {
      $("dec-counter-value").textContent = formatINR(decision.counter_price, 0);
      $("dec-counter-box").hidden = false;
    } else {
      $("dec-counter-box").hidden = true;
    }

    const metaStrip = $("dec-meta-strip");
    metaStrip.replaceChildren();
    const metaPairs = [
      ["Date", thread.date],
      ["Quantity", `${thread.quantity.toLocaleString("en-IN")} units`],
      ["Round", `${thread.round} / ${thread.max_rounds}`],
      ["Our Last Counter", thread.our_last_counter ? formatINR(thread.our_last_counter, 0) : "None (Round 1)"],
      ["Supplier Status", numbers.supplier_status],
      ["Unit", numbers.normalized_unit],
    ];
    metaPairs.forEach(([k, v]) => {
      const div = document.createElement("div");
      div.className = "meta-kv";
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      div.appendChild(dt);
      div.appendChild(dd);
      metaStrip.appendChild(div);
    });

    // Message banner & XSS-safe DOM slicing
    const histBanner = $("msg-history-banner");
    if (message_analysis.has_earlier_history) {
      histBanner.textContent = `Multi-message thread (${message_analysis.segment_count} messages): only the latest offer counts for price & banned terms; the entire thread is scanned for prompt injection.`;
      histBanner.hidden = false;
    } else {
      histBanner.hidden = true;
    }

    renderHighlightedMessage($("msg-body"), thread.supplier_message, message_analysis.highlights || []);

    // 6-step vertical stepper
    renderStepper($("trace-stepper"), steps, fired_step);

    // Numbers, horizontal gauge, breakdown & overrides
    renderPriceGauge($("numbers-gauge"), numbers);
    renderSpendBar($("spend-gauge"), numbers);
    renderBreakdown($("breakdown-wrap"), numbers.price_breakdown || []);
    renderOverrides($("overrides-wrap"), numbers);
  }

  function renderHighlightedMessage(container, rawText, highlights) {
    container.replaceChildren();
    let cursor = 0;
    const sorted = [...highlights].sort((a, b) => a.start - b.start);
    sorted.forEach((h) => {
      if (h.start > cursor) {
        container.appendChild(document.createTextNode(rawText.slice(cursor, h.start)));
      }
      const mark = document.createElement(h.kind === "header" ? "span" : "mark");
      mark.className = `hl-${h.kind}`;
      mark.title = h.label;
      mark.textContent = rawText.slice(h.start, h.end);
      container.appendChild(mark);
      cursor = h.end;
    });
    if (cursor < rawText.length) {
      container.appendChild(document.createTextNode(rawText.slice(cursor)));
    }
  }

  function renderStepper(ol, steps, firedStep) {
    ol.replaceChildren();
    steps.forEach((st) => {
      const li = document.createElement("li");
      li.className = `step-item step-${st.status}`;
      const isExpanded = st.step === firedStep || state.expandedSteps.has(st.step);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "step-trigger";
      btn.setAttribute("aria-expanded", String(isExpanded));

      const left = document.createElement("div");
      left.className = "step-left";
      const num = document.createElement("span");
      num.className = "step-num";
      num.textContent = st.status === "passed" ? "✓" : String(st.step);

      const textWrap = document.createElement("div");
      const title = document.createElement("div");
      title.className = "step-title";
      title.textContent = `${st.step}. ${st.name}`;
      const summary = document.createElement("p");
      summary.className = "step-summary";
      summary.textContent = st.summary;
      textWrap.appendChild(title);
      textWrap.appendChild(summary);
      left.appendChild(num);
      left.appendChild(textWrap);

      const badge = document.createElement("span");
      badge.className = `step-badge badge-${st.status}`;
      badge.textContent = st.status === "passed" ? "Passed" : st.status === "fired" ? "Fired" : "Not reached";

      btn.appendChild(left);
      btn.appendChild(badge);

      const detailDiv = document.createElement("div");
      detailDiv.className = "step-detail";
      detailDiv.hidden = !isExpanded;
      const pre = document.createElement("pre");
      pre.textContent = st.detail ? JSON.stringify(st.detail, null, 2) : (st.outcome || "No additional detail.");
      detailDiv.appendChild(pre);

      btn.addEventListener("click", () => {
        const next = detailDiv.hidden;
        detailDiv.hidden = !next;
        btn.setAttribute("aria-expanded", String(next));
        if (next) state.expandedSteps.add(st.step);
        else state.expandedSteps.delete(st.step);
      });

      li.appendChild(btn);
      li.appendChild(detailDiv);
      ol.appendChild(li);
    });
  }

  function renderPriceGauge(container, numbers) {
    container.replaceChildren();
    const target = Number(numbers.effective_target);
    const Lr = Number(numbers.L_r);
    const walk = Number(numbers.effective_walkaway);
    const p = numbers.normalized_price !== null ? Number(numbers.normalized_price) : null;

    const vals = [target, Lr, walk];
    if (p !== null && !Number.isNaN(p)) vals.push(p);
    const minV = Math.min(...vals) * 0.92;
    const maxV = Math.max(...vals) * 1.08 || 1;
    const span = maxV - minV || 1;
    const toX = (v) => 36 + ((v - minV) / span) * 448;

    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 520 96");
    svg.setAttribute("class", "gauge-svg");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Price position relative to target, L(r), and walk-away");

    const addEl = (tag, attrs, text) => {
      const el = document.createElementNS(svgNS, tag);
      Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v)));
      if (text !== undefined) el.textContent = text;
      svg.appendChild(el);
      return el;
    };

    // Track
    addEl("rect", { x: 36, y: 42, width: 448, height: 10, rx: 5, fill: "#e3d6ea" });
    const xT = toX(target);
    const xL = toX(Lr);
    const xW = toX(walk);
    // Acceptable zone (min to Lr)
    addEl("rect", { x: 36, y: 42, width: Math.max(4, xL - 36), height: 10, rx: 5, fill: "#8ec9a6" });

    // Markers
    const drawMarker = (x, label, val, yOff, color) => {
      addEl("line", { x1: x, y1: 35, x2: x, y2: 59, stroke: color, "stroke-width": 2.5 });
      addEl("text", { x, y: yOff, "text-anchor": "middle", fill: "#3d2b4f", "font-size": 10.5, "font-weight": 700 },
        `${label}: ${formatINR(val, 0)}`);
    };

    drawMarker(xT, "Target", target, 24, "#124a2b");
    if (Math.abs(xL - xT) > 60) {
      drawMarker(xL, `L(${numbers.round_used})`, Lr, 76, "#6b3d03");
    }
    if (Math.abs(xW - xL) > 50 || Math.abs(xW - xT) > 60) {
      drawMarker(xW, "Walk-away", walk, 24, "#781832");
    }

    if (p !== null && !Number.isNaN(p)) {
      const xp = toX(p);
      addEl("circle", { cx: xp, cy: 47, r: 7, fill: "#3d2b4f", stroke: "#ffffff", "stroke-width": 2 });
      addEl("text", { x: xp, y: 91, "text-anchor": "middle", fill: "#3d2b4f", "font-size": 11, "font-weight": 800 },
        `Quoted p: ${formatINR(p, 0)}`);
    }

    container.appendChild(svg);
  }

  function renderSpendBar(container, numbers) {
    container.replaceChildren();
    const limit = Number(numbers.effective_approval_limit);
    const total = numbers.total_value !== null ? Number(numbers.total_value) : null;
    const p = document.createElement("p");
    p.className = "picker-meta";
    p.textContent = total !== null
      ? `Total order value: ${formatINR(total, 2)} vs Approval limit: ${formatINR(limit, 0)}`
      : `Approval limit: ${formatINR(limit, 0)}`;
    container.appendChild(p);
  }

  function renderBreakdown(container, rows) {
    container.replaceChildren();
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "picker-meta";
      p.textContent = "No price breakdown available (missing or unsupported quote).";
      container.appendChild(p);
      return;
    }
    const table = document.createElement("table");
    table.className = "breakdown-table";
    const tbody = document.createElement("tbody");
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      if (r.final) tr.className = "final-row";
      const td1 = document.createElement("td");
      td1.textContent = r.stage;
      const td2 = document.createElement("td");
      td2.textContent = r.value;
      const td3 = document.createElement("td");
      td3.textContent = r.formula;
      tr.appendChild(td1);
      tr.appendChild(td2);
      tr.appendChild(td3);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderOverrides(container, numbers) {
    container.replaceChildren();

    const supPill = document.createElement("div");
    supPill.className = "override-pill";
    supPill.textContent = numbers.probation_lock
      ? `Supplier Status: PROBATION — L(r) locked to target (${formatINR(numbers.effective_target, 0)}) in all rounds.`
      : `Supplier Status: ${numbers.supplier_status}`;
    container.appendChild(supPill);

    (numbers.amendments || []).forEach((a) => {
      const div = document.createElement("div");
      div.className = "override-pill" + (a.active ? "" : " inactive");
      div.textContent = `${a.id} (${a.effective_from}) — ${a.active ? "APPLIED" : "Not yet effective"}: ${a.text}`;
      container.appendChild(div);
    });

    (numbers.contracts || []).forEach((c) => {
      const div = document.createElement("div");
      div.className = "override-pill" + (c.applies ? "" : " inactive");
      div.textContent = `Contract Override (${c.applies ? "APPLIED" : "Standing"}): ${c.text}`;
      container.appendChild(div);
    });
  }

  // ---------- Tab 2: Batch Upload, Progress, Charts & Table ----------
  const dropZone = $("drop-zone");
  const fileInput = $("csv-file-input");

  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragover");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    const file = e.dataTransfer.files?.[0];
    if (file) startUploadBatch(file);
  });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (file) startUploadBatch(file);
  });

  $("run-real-batch-btn").addEventListener("click", () => startBundledBatch("real"));
  $("run-sample-batch-btn").addEventListener("click", () => startBundledBatch("sample"));
  $("download-csv-btn").addEventListener("click", () => {
    if (state.batchJobId) {
      window.location.href = `/api/batch/${encodeURIComponent(state.batchJobId)}/download`;
    }
  });

  async function startUploadBatch(file) {
    const fd = new FormData();
    fd.append("file", file);
    await launchBatch(() => apiJSON("/api/batch", { method: "POST", body: fd }));
  }

  async function startBundledBatch(dataset) {
    await launchBatch(() => apiJSON(`/api/batch?dataset=${encodeURIComponent(dataset)}`, { method: "POST" }));
  }

  async function launchBatch(starterFn) {
    clearInterval(state.batchPollTimer);
    $("batch-error").hidden = true;
    $("batch-empty").hidden = true;
    $("batch-dashboard").hidden = true;
    $("batch-skeleton").hidden = false;
    $("batch-progress-wrap").hidden = false;
    $("download-csv-btn").disabled = true;

    try {
      const start = await starterFn();
      state.batchJobId = start.job_id;
      state.batchOffset = 0;
      pollBatch(start.job_id);
      state.batchPollTimer = setInterval(() => pollBatch(start.job_id), 150);
    } catch (err) {
      $("batch-skeleton").hidden = true;
      $("batch-progress-wrap").hidden = true;
      $("batch-error").textContent = err.message;
      $("batch-error").hidden = false;
    }
  }

  async function pollBatch(jobId) {
    try {
      const prog = await apiJSON(`/api/batch/${encodeURIComponent(jobId)}`);
      $("batch-progress-fill").style.width = `${prog.percent}%`;
      $("batch-progress-bar").setAttribute("aria-valuenow", String(Math.round(prog.percent)));
      $("batch-pct-text").textContent = `${prog.percent}%`;
      $("batch-status-text").textContent = `${prog.source} — ${prog.done.toLocaleString("en-IN")} / ${prog.total.toLocaleString("en-IN")} threads (${prog.elapsed_ms} ms)`;

      if (prog.status === "completed") {
        clearInterval(state.batchPollTimer);
        $("download-csv-btn").disabled = false;
        await loadBatchDashboard(jobId);
      } else if (prog.status === "failed") {
        clearInterval(state.batchPollTimer);
        $("batch-skeleton").hidden = true;
        $("batch-error").textContent = prog.error || "Batch job failed.";
        $("batch-error").hidden = false;
      }
    } catch (err) {
      clearInterval(state.batchPollTimer);
      $("batch-skeleton").hidden = true;
      $("batch-error").textContent = err.message;
      $("batch-error").hidden = false;
    }
  }

  async function loadBatchDashboard(jobId) {
    const analytics = await apiJSON(`/api/analytics/${encodeURIComponent(jobId)}`);
    $("batch-skeleton").hidden = true;
    $("batch-dashboard").hidden = false;

    // Populate reason filter dropdown
    const reasonSel = $("res-reason");
    const curReason = reasonSel.value;
    reasonSel.replaceChildren();
    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = "All Reasons";
    reasonSel.appendChild(allOpt);
    Object.keys(analytics.counts_by_reason || {}).forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r;
      opt.textContent = r;
      reasonSel.appendChild(opt);
    });
    reasonSel.value = curReason;

    renderKPIs(analytics);
    renderBarChart($("chart-actions"), [
      { label: "ACCEPT", val: analytics.counts_by_action.ACCEPT || 0, color: "#58a67c" },
      { label: "COUNTER", val: analytics.counts_by_action.COUNTER || 0, color: "#d4953b" },
      { label: "REJECT", val: analytics.counts_by_action.REJECT || 0, color: "#c95b77" },
      { label: "ESCALATE", val: analytics.counts_by_action.ESCALATE || 0, color: "#6d68c4" },
    ], analytics.total);

    const reasonItems = Object.entries(analytics.counts_by_reason || {}).map(([k, v]) => ({
      label: k,
      val: v,
      color: "#6b4e8a",
    }));
    renderBarChart($("chart-reasons"), reasonItems, analytics.total);
    renderCategoryChart($("chart-categories"), analytics.by_category || {});
    renderRoundChart($("chart-rounds"), analytics.counter_rate_by_round || []);

    await loadBatchResults();
  }

  function renderKPIs(a) {
    const kpis = $("batch-kpis");
    kpis.replaceChildren();
    const gap = a.price_gap?.gap_to_target?.avg;
    const cards = [
      ["Total Threads", a.total.toLocaleString("en-IN"), "Processed offline"],
      ["ACCEPT", (a.counts_by_action.ACCEPT || 0).toLocaleString("en-IN"), `${pct(a.counts_by_action.ACCEPT, a.total)}%`],
      ["COUNTER", (a.counts_by_action.COUNTER || 0).toLocaleString("en-IN"), `${pct(a.counts_by_action.COUNTER, a.total)}%`],
      ["REJECT", (a.counts_by_action.REJECT || 0).toLocaleString("en-IN"), `${pct(a.counts_by_action.REJECT, a.total)}%`],
      ["ESCALATE", (a.counts_by_action.ESCALATE || 0).toLocaleString("en-IN"), `${pct(a.counts_by_action.ESCALATE, a.total)}%`],
      ["Warnings", (a.warnings_count || 0).toLocaleString("en-IN"), "Missing / currency / round"],
      ["Avg Counter Gap", gap !== null && gap !== undefined ? formatINR(gap, 0) : "—", "Quoted p − target"],
    ];
    cards.forEach(([label, val, sub]) => {
      const div = document.createElement("div");
      div.className = "neu-card kpi-card";
      const l = document.createElement("span");
      l.className = "kpi-label";
      l.textContent = label;
      const v = document.createElement("strong");
      v.className = "kpi-value";
      v.textContent = val;
      const s = document.createElement("span");
      s.className = "kpi-sub";
      s.textContent = sub;
      div.appendChild(l);
      div.appendChild(v);
      div.appendChild(s);
      kpis.appendChild(div);
    });
  }

  function pct(part, total) {
    return total ? ((100 * (part || 0)) / total).toFixed(1) : "0.0";
  }

  function renderBarChart(container, items, total) {
    container.replaceChildren();
    const svgNS = "http://www.w3.org/2000/svg";
    const rowH = 30;
    const h = Math.max(100, items.length * rowH + 20);
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 520 ${h}`);
    const maxV = Math.max(1, ...items.map((i) => i.val));

    items.forEach((it, idx) => {
      const y = 14 + idx * rowH;
      const w = Math.max(4, (it.val / maxV) * 230);

      const lbl = document.createElementNS(svgNS, "text");
      lbl.setAttribute("x", "6");
      lbl.setAttribute("y", String(y + 14));
      lbl.setAttribute("fill", "#3d2b4f");
      lbl.setAttribute("font-size", "11.5");
      lbl.setAttribute("font-weight", "700");
      lbl.textContent = it.label;
      svg.appendChild(lbl);

      const bar = document.createElementNS(svgNS, "rect");
      bar.setAttribute("x", "185");
      bar.setAttribute("y", String(y + 2));
      bar.setAttribute("width", String(w));
      bar.setAttribute("height", "16");
      bar.setAttribute("rx", "6");
      bar.setAttribute("fill", it.color);
      svg.appendChild(bar);

      const valTxt = document.createElementNS(svgNS, "text");
      valTxt.setAttribute("x", String(192 + w));
      valTxt.setAttribute("y", String(y + 14));
      valTxt.setAttribute("fill", "#3d2b4f");
      valTxt.setAttribute("font-size", "11.5");
      valTxt.setAttribute("font-weight", "700");
      valTxt.textContent = `${it.val.toLocaleString("en-IN")} (${pct(it.val, total)}%)`;
      svg.appendChild(valTxt);
    });
    container.appendChild(svg);
  }

  function renderCategoryChart(container, byCat) {
    container.replaceChildren();
    const cats = Object.entries(byCat);
    const svgNS = "http://www.w3.org/2000/svg";
    const rowH = 24;
    const h = Math.max(120, cats.length * rowH + 34);
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 520 ${h}`);
    const maxTot = Math.max(1, ...cats.map(([, b]) => b.total));
    const colors = { ACCEPT: "#58a67c", COUNTER: "#d4953b", REJECT: "#c95b77", ESCALATE: "#6d68c4" };

    cats.forEach(([slug, b], idx) => {
      const y = 10 + idx * rowH;
      const lbl = document.createElementNS(svgNS, "text");
      lbl.setAttribute("x", "6");
      lbl.setAttribute("y", String(y + 13));
      lbl.setAttribute("fill", "#3d2b4f");
      lbl.setAttribute("font-size", "11");
      lbl.setAttribute("font-weight", "600");
      lbl.textContent = slug;
      svg.appendChild(lbl);

      let x = 135;
      ["ACCEPT", "COUNTER", "REJECT", "ESCALATE"].forEach((act) => {
        const w = (b[act] / maxTot) * 320;
        if (w > 0) {
          const rect = document.createElementNS(svgNS, "rect");
          rect.setAttribute("x", String(x));
          rect.setAttribute("y", String(y + 2));
          rect.setAttribute("width", String(w));
          rect.setAttribute("height", "13");
          rect.setAttribute("fill", colors[act]);
          svg.appendChild(rect);
          x += w;
        }
      });

      const totTxt = document.createElementNS(svgNS, "text");
      totTxt.setAttribute("x", String(x + 6));
      totTxt.setAttribute("y", String(y + 13));
      totTxt.setAttribute("fill", "#3d2b4f");
      totTxt.setAttribute("font-size", "10.5");
      totTxt.setAttribute("font-weight", "700");
      totTxt.textContent = String(b.total);
      svg.appendChild(totTxt);
    });
    container.appendChild(svg);
  }

  function renderRoundChart(container, rounds) {
    container.replaceChildren();
    const items = rounds.map((r) => ({
      label: `Round ${r.round} (${r.counter_rate_pct}% counter)`,
      val: r.COUNTER,
      color: "#d4953b",
    }));
    const tot = rounds.reduce((acc, r) => acc + r.total, 0);
    renderBarChart(container, items, tot);
  }

  async function loadBatchResults() {
    if (!state.batchJobId) return;
    const qs = new URLSearchParams({
      q: $("res-search").value.trim(),
      action: $("res-action").value,
      reason: $("res-reason").value,
      category: $("res-category").value,
      supplier: $("res-supplier").value,
      sort_by: state.batchSortBy,
      sort_dir: state.batchSortDir,
      limit: String(state.batchLimit),
      offset: String(state.batchOffset),
    });
    // Strip empty filters
    for (const [k, v] of [...qs.entries()]) {
      if (!v) qs.delete(k);
    }

    const data = await apiJSON(`/api/batch/${encodeURIComponent(state.batchJobId)}/results?${qs.toString()}`);
    state.batchTotal = data.total;
    $("results-count").textContent = `(${data.total.toLocaleString("en-IN")} matching)`;

    const tbody = $("results-tbody");
    tbody.replaceChildren();
    data.rows.forEach((r) => {
      const tr = document.createElement("tr");
      tr.tabIndex = 0;
      const cells = [
        r.thread_id,
        r.date,
        r.supplier,
        r.category,
        `R${r.round}`,
        r.p ? formatINR(r.p, 0) : "—",
        r.L_r ? formatINR(r.L_r, 0) : "—",
      ];
      cells.forEach((c) => {
        const td = document.createElement("td");
        td.textContent = c;
        tr.appendChild(td);
      });
      const tdLbl = document.createElement("td");
      tdLbl.appendChild(makeActionChip(r.action, true));
      const lblSpan = document.createElement("span");
      lblSpan.textContent = ` ${r.label}`;
      lblSpan.style.fontFamily = "var(--font-mono)";
      lblSpan.style.fontSize = "0.78rem";
      tdLbl.appendChild(lblSpan);
      tr.appendChild(tdLbl);

      const openRow = async () => {
        switchTab("explain");
        await runDecide({ thread_id: r.thread_id });
      };
      tr.addEventListener("click", openRow);
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openRow();
        }
      });
      tbody.appendChild(tr);
    });

    const start = data.total === 0 ? 0 : state.batchOffset + 1;
    const end = Math.min(state.batchOffset + data.rows.length, data.total);
    $("page-info").textContent = `Showing ${start}–${end} of ${data.total.toLocaleString("en-IN")}`;
    $("prev-page-btn").disabled = state.batchOffset === 0;
    $("next-page-btn").disabled = state.batchOffset + state.batchLimit >= data.total;
  }

  ["res-action", "res-reason", "res-category", "res-supplier"].forEach((id) => {
    $(id).addEventListener("change", () => {
      state.batchOffset = 0;
      loadBatchResults();
    });
  });

  let resSearchDebounce = null;
  $("res-search").addEventListener("input", () => {
    clearTimeout(resSearchDebounce);
    resSearchDebounce = setTimeout(() => {
      state.batchOffset = 0;
      loadBatchResults();
    }, 180);
  });

  document.querySelectorAll(".sort-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const col = btn.dataset.sort;
      if (state.batchSortBy === col) {
        state.batchSortDir = state.batchSortDir === "asc" ? "desc" : "asc";
      } else {
        state.batchSortBy = col;
        state.batchSortDir = "asc";
      }
      document.querySelectorAll(".sort-btn").forEach((b) => {
        const th = b.closest("th");
        if (th) {
          th.setAttribute("aria-sort", b === btn ? (state.batchSortDir === "asc" ? "ascending" : "descending") : "none");
        }
      });
      state.batchOffset = 0;
      loadBatchResults();
    });
  });

  $("prev-page-btn").addEventListener("click", () => {
    state.batchOffset = Math.max(0, state.batchOffset - state.batchLimit);
    loadBatchResults();
  });

  $("next-page-btn").addEventListener("click", () => {
    state.batchOffset += state.batchLimit;
    loadBatchResults();
  });

  // Boot
  initHealthAndMeta();
  loadThreadList();
})();
