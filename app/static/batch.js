/* M6 批次复核页：拨款/追回登记、更正登记与发布、四列台账与同批次导出。 */
(function () {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  const result = $("#action-result");
  let current = null;
  let lastCorrectionId = null;

  function money(v) { return (v === "" || v == null) ? "—" : String(v); }
  function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  async function call(url, options) {
    const res = await fetch(url, Object.assign({ headers: { "content-type": "application/json" } }, options));
    const text = await res.text();
    let body;
    try { body = JSON.parse(text); } catch (_) { body = text; }
    if (!res.ok) {
      const msg = body && body.message ? `${body.error}: ${body.message}` : `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return body;
  }

  function log(msg, ok) {
    result.textContent = typeof msg === "string" ? msg : JSON.stringify(msg, null, 2);
    result.style.borderLeftColor = ok === false ? "var(--bad)" : "var(--accent)";
  }

  async function loadBatches(selectId) {
    const data = await call("/api/batches");
    const sel = $("#batch-select");
    sel.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = ""; placeholder.textContent = "— 选择批次 —";
    sel.appendChild(placeholder);
    data.batches.forEach(b => {
      const opt = document.createElement("option");
      opt.value = b.batch_id;
      opt.textContent = `${b.batch_id} · v${b.current_version} · 批复 ${b.approved}`;
      sel.appendChild(opt);
    });
    if (selectId) sel.value = selectId;
    return data.batches;
  }

  function render(view) {
    current = view;
    const t = view.ledger.totals;
    $("#batch-meta").textContent = `${view.batch.batch_id} · 当前 v${view.current.version} · ${view.current.reason_code}`;
    $("#version-meta").textContent = view.previous ? `v${view.previous.version} → v${view.current.version}` : `v${view.current.version}（初始）`;
    const kpis = { "k-approved": t.approved, "k-entitled": t.entitled_total, "k-paid": t.paid_total,
      "k-topup": t.topup_total, "k-outstanding": t.outstanding_total, "k-gap": t.funding_gap };
    Object.entries(kpis).forEach(([id, v]) => { $("#" + id).textContent = v; });

    const tbody = $("#ledger-table tbody");
    tbody.innerHTML = view.ledger.rows.map(r => `<tr>
      <td>${esc(r.project_id)}</td><td>${r.eligible ? "有效" : "无效"}</td>
      <td class="num">${money(r.entitled_before)}</td><td class="num">${money(r.entitled)}</td>
      <td class="num">${money(r.paid)}</td><td class="num">${money(r.recovered)}</td>
      <td class="num">${money(r.topup)}</td><td class="num">${money(r.clawback)}</td>
      <td class="num">${money(r.outstanding)}</td><td>${esc(r.reason)}</td></tr>`).join("")
      || '<tr><td colspan="10" class="empty">无台账行</td></tr>';

    $("#checks").innerHTML = view.ledger.checks.map(c =>
      `<span class="check ${c.passed ? "check--ok" : "check--bad"}">${c.passed ? "✓" : "✕"} ${esc(c.code)}</span>`).join("");

    const prev = Object.fromEntries((view.previous ? view.previous.rows : []).map(r => [r.project_id, r.entitled]));
    $("#compare-table tbody").innerHTML = view.current.rows.map(r => {
      const before = prev[r.project_id] ?? "";
      const diff = before === "" ? "—" : (Number(r.entitled) - Number(before)).toFixed(2);
      return `<tr><td>${esc(r.project_id)}</td><td class="num">${money(before)}</td>
        <td class="num">${money(r.entitled)}</td><td class="num">${diff}</td><td>${esc(r.reason)}</td></tr>`;
    }).join("");

    $("#correction-table tbody").innerHTML = view.corrections.map(c =>
      `<tr ${c.status === "PENDING" ? 'class="row--pending"' : ""}>
        <td>${esc(c.correction_id.slice(0, 22))}</td><td>v${c.base_version}</td>
        <td>${esc(c.reason_code)}</td><td>${esc(c.status)}${c.published_version != null ? " v" + c.published_version : ""}</td>
      </tr>`).join("");
    const pending = view.corrections.filter(c => c.status === "PENDING").pop();
    lastCorrectionId = pending ? pending.correction_id : null;

    $("#export-link").href = `/api/batches/${encodeURIComponent(view.batch.batch_id)}/export.csv`;
  }

  async function refresh() {
    const id = $("#batch-select").value;
    if (!id) return;
    render(await call(`/api/batches/${encodeURIComponent(id)}`));
  }

  function formJson(form) {
    const data = {};
    new FormData(form).forEach((v, k) => { data[k] = v; });
    ["items", "changes"].forEach(k => {
      if (k in data) { try { data[k] = JSON.parse(data[k]); } catch (e) { throw new Error(`${k} 不是合法 JSON`); } }
    });
    return data;
  }

  async function submitForm(form, url, selectId) {
    try {
      const body = formJson(form);
      const data = await call(url, { method: "POST", body: JSON.stringify(body) });
      log(data);
      const pick = selectId ? selectId(data) : $("#batch-select").value;
      await loadBatches(pick);
      await refresh();
    } catch (e) { log(e.message, false); }
  }

  $("#batch-select").addEventListener("change", refresh);

  $("#form-batch").addEventListener("submit", (e) => {
    e.preventDefault();
    submitForm(e.target, "/api/batches", (data) => data.batch_id);
  });

  $("#form-pay").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("#batch-select").value;
    if (!id) return log("请先选择批次", false);
    submitForm(e.target, `/api/batches/${encodeURIComponent(id)}/disbursements`);
  });

  $("#form-recover").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("#batch-select").value;
    if (!id) return log("请先选择批次", false);
    submitForm(e.target, `/api/batches/${encodeURIComponent(id)}/recoveries`);
  });

  $("#form-correction").addEventListener("submit", (e) => {
    e.preventDefault();
    const id = $("#batch-select").value;
    if (!id) return log("请先选择批次", false);
    submitForm(e.target, `/api/batches/${encodeURIComponent(id)}/corrections`);
  });

  $("#btn-publish").addEventListener("click", async () => {
    if (!lastCorrectionId) return log("没有待发布的更正（发布中断可对已存更正再次续跑）", false);
    try {
      const data = await call(`/api/corrections/${encodeURIComponent(lastCorrectionId)}/publish`,
        { method: "POST", body: JSON.stringify({}) });
      log({ published: "ok", version: data.version, reason: data.reason_code });
      await loadBatches($("#batch-select").value);
      await refresh();
    } catch (e) { log(e.message, false); }
  });

  loadBatches().then(batches => { if (batches.length) { $("#batch-select").value = batches[0].batch_id; return refresh(); } })
    .catch(e => log(e.message, false));
})();
