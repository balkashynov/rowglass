"use strict";
(() => {
  const data = window.ROWGLASS_DATA;
  const $ = (s) => document.querySelector(s);
  const escape = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  if (!data?.audits?.length) {
    for (const selector of [
      ".hero",
      ".metrics",
      "#mechanism",
      "#evidence",
      ".try-it",
    ])
      $(selector).hidden = true;
    document.querySelectorAll(".topbar nav a").forEach((a) => {
      if (a.hash !== "#investigate") a.hidden = true;
    });
    $("#investigate").innerHTML =
      '<div class="error">The recorded evidence could not be loaded. Keep data.js beside this page, or inspect data/report.json directly.</div>';
    return;
  }
  const isDemo = !!data.extra?.key_replay;
  $("#engineering-link").hidden = !isDemo;
  if (!isDemo) {
    $(".workspace-foot").textContent =
      "A live mismatch may be lag. Controlled cuts require independently established quiescence and drain evidence; an LSN alone does not establish them.";
    for (const selector of [
      ".hero",
      ".metrics",
      "#mechanism",
      "#evidence",
      ".try-it",
    ])
      $(selector).hidden = true;
    document.querySelectorAll(".topbar nav a").forEach((a) => {
      if (a.hash !== "#investigate") a.hidden = true;
    });
    $("#investigate .section-heading h2").textContent =
      "Investigate the captured state.";
    $("#investigate .section-heading>p").textContent =
      "Choose an observation, inspect a row, and follow its recorded history.";
    document.querySelector("footer span:last-child").textContent =
      "Recorded evidence · Review source values before sharing";
  }
  const names = {
    "01-baseline": [
      "Baseline",
      "A consistent starting point",
      "The independent source snapshot and ClickHouse contain the same observed rows. This initial observation is labeled live.",
    ],
    "02-paused-lag": [
      "Lag control",
      "A mismatch is not a loss verdict",
      "The PeerDB worker was deliberately paused while source transactions committed. The differences here are expected lag, not evidence of permanent loss.",
    ],
    "03-key-change": [
      "Key change",
      "Caught up, with an old key left behind",
      "The source changed id 60 to 2060. PeerDB normalized past that commit, but the old key remained visible in ClickHouse. This is documented default behavior, reproduced again using the extracted case.",
    ],
    "04-aligned": [
      "Aligned",
      "The key discrepancy is accounted for",
      "The lab explicitly inserted a tombstone for the stale key. All observed source and destination rows match at the controlled cut. This was a lab correction, not an automatic repair.",
    ],
    "05-injected-divergence": [
      "Injected faults",
      "Three deliberate changes. Three precise findings.",
      "The lab changed an exact decimal, hid a row with a tombstone, and inserted an extra key directly into ClickHouse. These are controlled diagnostic tests, not upstream bug discoveries.",
    ],
    "06-restored": [
      "Restored",
      "The values line up again",
      "The lab restored its injected changes from a saved pre-fault destination snapshot. Source writes remain locked and the replica is normalized through the captured commit.",
    ],
  };
  let current = Math.max(
    0,
    data.audits.findIndex((a) => a.scenario === "03-key-change"),
  );
  let selected = null,
    filter = "all",
    search = "",
    view = "compare",
    historyIndex = 0;
  const friendly = (k) =>
    ({
      matched: "Matched",
      changed: "Changed",
      missing: "Missing",
      unexpected: "Extra",
      unverifiable: "Unknown",
    })[k] || k;
  const status = (s) =>
    ({
      live_observation: "Live observation",
      diverged_at_cut: "Discrepancy at controlled cut",
      matched_at_cut: "Matched at controlled cut",
      inconclusive: "Inconclusive",
    })[s] || s;
  function rowId(m) {
    const row = m.source || m.target;
    if (row?.id?.kind === "text") return row.id.value;
    try {
      return JSON.parse(m.key)
        .map((s) => s.replace(/^[^:]+:/, ""))
        .join(", ");
    } catch {
      return m.key;
    }
  }
  function value(v) {
    if (!v) return '<span class="null">row absent</span>';
    if (v.kind === "null") return '<span class="null">NULL</span>';
    if (v.kind !== "text")
      return '<span class="null">' + escape(v.kind) + "</span>";
    return escape(v.value);
  }
  function bundle(m) {
    return data.replays[m.table + "\0" + m.key];
  }
  function writeHash() {
    const p = new URLSearchParams({ case: data.audits[current].scenario });
    if (selected) p.set("key", selected.key);
    p.set("view", view);
    history.replaceState(null, "", "#" + p.toString());
  }
  function table(source, target, fields = [], unknown = []) {
    const cols = [
      ...new Set([...Object.keys(source || {}), ...Object.keys(target || {})]),
    ];
    return (
      '<div class="table-scroll"><table class="values"><thead><tr><th>FIELD</th><th>POSTGRES / CAPTURED</th><th>CLICKHOUSE / OBSERVED</th></tr></thead><tbody>' +
      cols
        .map(
          (c) =>
            '<tr class="' +
            (fields.includes(c) || !source || !target ? "changed" : "") +
            '"><td>' +
            escape(c) +
            (unknown.includes(c) ? " ?" : "") +
            "</td><td>" +
            value(source?.[c]) +
            "</td><td>" +
            value(target?.[c]) +
            "</td></tr>",
        )
        .join("") +
      "</tbody></table></div>"
    );
  }
  function selectRow(m) {
    selected = m;
    historyIndex = Math.max(0, (m.history || []).length - 1);
    renderRows();
    renderDetail();
    writeHash();
  }
  function renderRows() {
    const audit = data.audits[current];
    const list = audit.mismatches.filter(
      (m) =>
        (filter === "all" || m.kind === filter) &&
        (m.table + " " + rowId(m) + " " + m.key)
          .toLowerCase()
          .includes(search.toLowerCase()),
    );
    $("#row-count").textContent = list.length;
    $("#rows").replaceChildren();
    if (selected && !list.includes(selected)) {
      selected = list[0] || null;
      historyIndex = Math.max(0, (selected?.history || []).length - 1);
      renderDetail();
    }
    if (!selected && list.length) {
      selected = list[0];
      historyIndex = Math.max(0, (selected.history || []).length - 1);
      renderDetail();
    }
    if (!list.length) {
      $("#rows").innerHTML =
        '<p class="list-empty">' +
        (audit.mismatches.length
          ? "No rows match this filter."
          : "No discrepancies in this observation.") +
        "</p>";
      return;
    }
    for (const m of list) {
      const b = document.createElement("button");
      b.className = "row-button" + (m === selected ? " active" : "");
      b.setAttribute("aria-pressed", String(m === selected));
      b.innerHTML =
        '<span class="row-button-top"><b>id ' +
        escape(rowId(m)) +
        '</b><span class="kind">' +
        escape(friendly(m.kind)) +
        "</span></span><small>" +
        escape(m.table) +
        "</small>";
      b.onclick = () => selectRow(m);
      $("#rows").append(b);
    }
  }
  function renderDetail() {
    const audit = data.audits[current];
    $("#empty-state").hidden = !!selected;
    $("#row-detail").hidden = !selected;
    if (!selected) {
      $("#empty-state").innerHTML =
        '<div class="big-check">✓</div><h3>' +
        (audit.mismatches.length
          ? "No row selected."
          : (audit.totals.unverifiable || 0) > 0
            ? "Some values remain unverifiable."
            : "Every observed row matches.") +
        "</h3><p>" +
        (audit.mismatches.length
          ? "Choose another filter or search to inspect a captured discrepancy."
          : audit.status === "matched_at_cut"
            ? isDemo
              ? "The source is quiescent and the pipeline has normalized through the captured commit."
              : "Rows match under the caller-supplied controlled-cut conditions."
            : "This is a live observation, not an established shared snapshot.") +
        "</p>";
      return;
    }
    const m = selected;
    $("#detail-table").textContent = m.table;
    $("#detail-key").textContent = "id " + rowId(m);
    $("#detail-kind").textContent = friendly(m.kind);
    $("#detail-kind").className = "status bad";
    $("#compare-panel").innerHTML =
      table(m.source, m.target, m.fields, m.unknown_fields) +
      '<p class="panel-note">' +
      (m.fields?.length
        ? "<strong>Changed fields:</strong> " +
          escape(m.fields.join(", ")) +
          ". Values are compared with source type semantics; exact decimals never pass through a JavaScript float."
        : m.kind === "unexpected"
          ? "This key is absent from the source state at this cut. The source history shows whether it was deleted or moved to a new key."
          : m.kind === "missing"
            ? "This source key has no visible live row in the destination observation. A live observation may still reflect replication lag."
            : "Unsupported or unobserved values cannot establish equality.") +
      "</p>";
    renderHistory();
    renderReplay();
    showView(view, false);
  }
  function renderHistory() {
    const m = selected;
    const events = m.history || [];
    const panel = $("#history-panel");
    if (!events.length) {
      panel.innerHTML =
        '<p class="panel-note">No committed history exists for this key in the captured source window. It may have been created only at the destination.</p>';
      return;
    }
    historyIndex = Math.min(historyIndex, events.length - 1);
    const event = events[historyIndex];
    panel.innerHTML =
      '<div class="history-controls"><label for="history-range" class="eyebrow">SOURCE STATE</label><input id="history-range" type="range" min="0" max="' +
      (events.length - 1) +
      '" value="' +
      historyIndex +
      '" aria-label="Recorded row history position"><output id="history-position">' +
      (historyIndex + 1) +
      " / " +
      events.length +
      "</output></div>" +
      events
        .map(
          (e, i) =>
            '<div class="history-step ' +
            (i === historyIndex ? "selected" : "") +
            '"><span class="history-dot"></span><div><b>' +
            escape(
              e.op === "snapshot"
                ? "Consistent baseline"
                : e.op === "delete"
                  ? "Key removed from source"
                  : e.op === "update"
                    ? "Committed update"
                    : e.op,
            ) +
            "</b><code>" +
            escape(
              e.xid
                ? "XID " + e.xid + " · " + e.lsn
                : "Exported source snapshot",
            ) +
            '</code></div><span class="status ' +
            (i === historyIndex ? "good" : "") +
            '">' +
            (i === historyIndex ? "Selected" : "Step " + (i + 1)) +
            "</span></div>",
        )
        .join("") +
      '<p class="panel-note"><strong>Selected source state</strong> compared with the destination observation:</p>' +
      table(event.row, m.target);
    $("#history-range").oninput = (e) => {
      historyIndex = Number(e.target.value);
      const focused = document.activeElement === e.target;
      renderHistory();
      if (focused) $("#history-range").focus();
    };
  }
  function renderReplay() {
    const m = selected,
      b = bundle(m),
      panel = $("#replay-panel");
    if (!b || b.unavailable) {
      panel.innerHTML =
        '<p class="panel-note">' +
        escape(
          b?.unavailable ||
            "This downloaded report has no prebuilt replay for this row. Generate one locally with rowglass replay using the canonical key from the audit.",
        ) +
        "</p>";
      return;
    }
    panel.innerHTML =
      '<div class="replay-numbers"><div><strong>' +
      b.selected_transactions +
      ' <span class="null">/ ' +
      b.total_transactions +
      "</span></strong><small>TRANSACTIONS RETAINED</small></div><div><strong>" +
      b.baseline_rows +
      "</strong><small>BASELINE ROWS</small></div><div><strong>" +
      b.dependency_keys +
      '</strong><small>CONNECTED KEYS</small></div></div><p class="panel-note">The selected row brings its companion writes with it. Every retained transaction remains atomic, even when it touches another table.</p>' +
      b.transactions
        .map(
          (tx) =>
            '<div class="replay-transaction"><b>BEGIN</b> · XID ' +
            escape(tx.xid) +
            " · " +
            escape(tx.lsn) +
            "<br>" +
            tx.changes
              .map(
                (c) =>
                  "<span>" +
                  escape(c.op.toUpperCase()) +
                  "</span> " +
                  escape(c.table) +
                  (c.key ? " · " + escape(c.key) : ""),
              )
              .join("<br>") +
            "<br><b>COMMIT</b></div>",
        )
        .join("") +
      '<div class="replay-actions"><a class="button dark" href="' +
      escape(b.download) +
      '" download>Download SQL <span>↓</span></a><a href="' +
      escape(b.manifest) +
      '" download>Replay manifest ↗</a></div><p class="panel-note">Replay spans this journal’s complete captured history, which may extend beyond the selected observation. Creates a fresh scratch schema and fails if it exists. Original constraints, triggers and application statement predicates are not reconstructed. This is a backward lineage slice, not a minimum failure proof.</p>';
  }
  function showView(next, update = true) {
    view = next;
    for (const name of ["compare", "history", "replay"]) {
      $("#" + name + "-panel").hidden = name !== view;
      $("#tab-" + name).setAttribute("aria-selected", String(name === view));
    }
    if (update) writeHash();
  }
  function renderCase() {
    const audit = data.audits[current],
      meta = (isDemo ? names[audit.scenario] : null) || [
        audit.scenario,
        audit.scenario,
        "Recorded audit from an independent source journal.",
      ];
    $("#cases").replaceChildren();
    data.audits.forEach((a, i) => {
      const b = document.createElement("button");
      b.role = "tab";
      b.setAttribute("aria-selected", String(i === current));
      b.className = i === current ? "active" : "";
      b.innerHTML =
        "<span>" +
        String(i + 1).padStart(2, "0") +
        "</span>" +
        escape(((isDemo ? names[a.scenario] : null) || [a.scenario])[0]);
      b.onclick = () => {
        current = i;
        filter = "all";
        search = "";
        $("#search").value = "";
        selected = a.mismatches[0] || null;
        historyIndex = Math.max(0, (selected?.history || []).length - 1);
        renderCase();
        writeHash();
      };
      $("#cases").append(b);
    });
    $("#case-title").textContent = meta[1];
    $("#case-description").textContent = meta[2];
    $("#case-status").textContent = status(audit.status);
    $("#case-status").className =
      "status " +
      (audit.status === "matched_at_cut"
        ? "good"
        : audit.status === "diverged_at_cut"
          ? "bad"
          : "");
    $("#audit-download").href = audit.download;
    $("#cut").textContent = audit.source.source_cut;
    $("#cut-mode").textContent =
      audit.consistency.mode === "controlled_quiescent"
        ? isDemo
          ? "SOURCE LOCKED · REPLICA NORMALIZED"
          : "CONTROLLED CUT · CALLER-SUPPLIED CONDITIONS"
        : "LIVE OBSERVATION · NO SHARED SNAPSHOT";
    $("#counts").innerHTML = [
      "matched",
      "changed",
      "missing",
      "unexpected",
      "unverifiable",
    ]
      .map(
        (k) =>
          '<div class="' +
          (k !== "matched" && audit.totals[k] ? "bad" : "") +
          '"><b>' +
          (audit.totals[k] || 0).toLocaleString() +
          "</b>" +
          friendly(k) +
          "</div>",
      )
      .join("");
    document
      .querySelectorAll("[data-filter]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.filter === filter),
      );
    renderRows();
    renderDetail();
  }
  $("#search").oninput = (e) => {
    search = e.target.value;
    renderRows();
  };
  document.querySelectorAll("[data-filter]").forEach(
    (b) =>
      (b.onclick = () => {
        filter = b.dataset.filter;
        document
          .querySelectorAll("[data-filter]")
          .forEach((el) => el.classList.toggle("active", el === b));
        renderRows();
      }),
  );
  document
    .querySelectorAll("[data-view]")
    .forEach((b) => (b.onclick = () => showView(b.dataset.view)));
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    const found = data.audits.findIndex((a) => a.scenario === p.get("case"));
    if (found >= 0) current = found;
    selected =
      data.audits[current].mismatches.find((m) => m.key === p.get("key")) ||
      data.audits[current].mismatches[0] ||
      null;
    const requested = p.get("view");
    if (["compare", "history", "replay"].includes(requested)) view = requested;
    historyIndex = Math.max(0, (selected?.history || []).length - 1);
    renderCase();
  }
  $("#metric-transactions").textContent =
    data.source.transactions.toLocaleString();
  $("#metric-changes").textContent = data.source.changes.toLocaleString();
  const keyReport = data.audits.find((a) => a.scenario === "03-key-change");
  const keyBundle = keyReport?.mismatches[0] && bundle(keyReport.mismatches[0]);
  $("#metric-replay").textContent = keyBundle?.selected_transactions ?? "-";
  if (keyBundle?.manifest) $("#key-replay-download").href = keyBundle.manifest;
  const restored =
    data.audits.find((a) => a.scenario === "06-restored") || data.audits.at(-1);
  $("#metric-match").textContent = (
    restored.totals.matched || 0
  ).toLocaleString();
  if (data.extra.benchmark_summary) {
    $("#bench-title").textContent = data.extra.benchmark_summary.title;
    $("#bench-copy").textContent = data.extra.benchmark_summary.description;
  }
  window.addEventListener("hashchange", () => {
    if (location.hash.includes("case=")) readHash();
  });
  readHash();
})();
