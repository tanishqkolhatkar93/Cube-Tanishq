"use strict";

const stages = [
  { id: "receiving", label: "Receiving", blurb: "Inbound checks", accepts: "image/*" },
  { id: "prep", label: "Prep", blurb: "Prep and labelling", accepts: "image/*,.json,.csv,.pdf" },
  { id: "pack", label: "Pack", blurb: "Pack and ship", accepts: "image/*" },
  { id: "returns", label: "Returns", blurb: "Return grading", accepts: "image/*,.json,.csv,.pdf" },
  { id: "recovery", label: "Recovery", blurb: "Fee recovery", accepts: ".json,.csv,.pdf,text/csv,application/json" },
];

const state = {
  session: null,
  health: null,
  healthError: null,
  healthAt: null,
  workflow: null,
  evidence: null,
  selectedStage: 0,
  stagedUnit: null,
  stagedFiles: Object.fromEntries(stages.map((stage) => [stage.id, []])),
  serverInputs: Object.fromEntries(stages.map((stage) => [stage.id, []])),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));
const isSignedIn = () => Boolean(state.session?.authenticated);

/* ---------- formatting helpers ---------- */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

function pretty(value) {
  return value == null || value === "" ? "-" : String(value).replace(/_/g, " ");
}

function money(value) {
  const number = Number(value);
  return value == null || Number.isNaN(number) ? "-" : `$${number.toFixed(2)}`;
}

function icon(name, extra = "") {
  return `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

const TONES = {
  ok: ["ok", "pass", "completed", "clean", "healthy"],
  bad: ["fail", "failed", "error", "exception", "down"],
  warn: ["uncertain", "blocked", "warning", "needs_review", "incomplete", "recovery_required", "pending", "degraded"],
  info: ["running", "in_progress"],
  accent: ["claim_recommended"],
};

function toneOf(value) {
  const key = String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "_");
  for (const [tone, keys] of Object.entries(TONES)) if (keys.includes(key)) return tone;
  return "mute";
}

const TONE_ICON = { ok: "check", bad: "x", warn: "alert", info: "clock", accent: "check", mute: "minus" };

function pill(value, label) {
  return `<span class="pill tone-${toneOf(value)}"><span class="dot"></span>${esc(label ?? pretty(value).toLowerCase())}</span>`;
}

function emptyState(iconName, title, hint) {
  return `<div class="empty">${icon(iconName)}<strong>${esc(title)}</strong><span>${esc(hint)}</span></div>`;
}

function readRecent() {
  try {
    const parsed = JSON.parse(localStorage.getItem("cube_recent_workflows") || "[]");
    return Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

/* ---------- feedback ---------- */

function showToast(message, type = "info") {
  const node = document.createElement("div");
  node.className = `toast toast-${type}`;
  node.setAttribute("role", type === "error" ? "alert" : "status");
  node.innerHTML = `${icon(type === "error" ? "alert" : type === "success" ? "check" : "clock")}<span>${esc(message)}</span>`;
  node.addEventListener("click", () => node.remove());
  $("#toasts").append(node);
  window.setTimeout(() => node.remove(), type === "error" ? 7000 : 3800);
}

function setDebug(value) {
  $("#debugJson").textContent = JSON.stringify(value || {}, null, 2);
}

async function withBusy(button, task) {
  if (!button || button.dataset.busy) return undefined;
  button.dataset.busy = "1";
  button.setAttribute("aria-busy", "true");
  button.disabled = true;
  button.classList.add("is-busy");
  try {
    return await task();
  } finally {
    delete button.dataset.busy;
    button.removeAttribute("aria-busy");
    button.classList.remove("is-busy");
    syncControls();
  }
}

/* ---------- network ---------- */

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { detail: text };
  }
  if (!response.ok) {
    if (response.status === 401 && isSignedIn()) handleSessionExpired();
    const detail = typeof body.detail === "string" ? body.detail : body.error;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

async function uploadFile(unitId, stageId, file) {
  const response = await fetch(`/inputs/${encodeURIComponent(unitId)}/${encodeURIComponent(stageId)}?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": file.type || "application/octet-stream" },
    body: await file.arrayBuffer(),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || body.error || `${response.status} ${response.statusText}`);
  return body;
}

function handleSessionExpired() {
  state.session = { authenticated: false };
  state.workflow = null;
  state.evidence = null;
  renderAll();
  showToast("Your session expired. Please sign in again.", "error");
}

/* ---------- derived state ---------- */

function currentUnit() {
  return $("#unitId").value.trim();
}

function workflowRecords() {
  const records = new Map();
  for (const result of state.workflow?.stage_results || []) {
    if (result.record_id) records.set(result.record_id, pretty(result.stage));
  }
  for (const record of Object.values(state.evidence?.evidence || {})) {
    if (record.record_id && !records.has(record.record_id)) records.set(record.record_id, pretty(record.stage));
  }
  return records;
}

function syncControls() {
  const signedIn = isSignedIn();
  const workflow = state.workflow;
  const busy = (selector) => Boolean($(selector)?.dataset.busy);
  if (!busy("#runWorkflow")) $("#runWorkflow").disabled = !signedIn;
  if (!busy("#loadEvidence")) $("#loadEvidence").disabled = !workflow;
  if (!busy("#resumeWorkflow")) {
    $("#resumeWorkflow").disabled = !workflow || !["FAILED", "BLOCKED", "RECOVERY_REQUIRED", "IN_PROGRESS"].includes(workflow.status);
  }
  if (!busy("#submitOverride")) $("#submitOverride").disabled = !workflow;
  $$("[data-upload-stage]").forEach((button) => {
    if (button.dataset.busy) return;
    button.disabled = !signedIn || !state.stagedFiles[button.dataset.uploadStage]?.length;
  });
}

/* ---------- rendering: session + health ---------- */

function renderSession() {
  const signedIn = isSignedIn();
  $("#gate").classList.toggle("hidden", signedIn);
  $("#authed").classList.toggle("hidden", !signedIn);
  $("#sessionCard").classList.toggle("hidden", !signedIn);
  $$("[data-auth]").forEach((link) => {
    link.classList.toggle("is-locked", !signedIn);
    link.setAttribute("aria-disabled", String(!signedIn));
  });
  if (signedIn) {
    const actor = state.session.actor || "operator";
    $("#sessionActor").textContent = actor;
    $("#sessionOrg").textContent = state.session.org_id;
    $("#avatar").textContent = actor.slice(0, 1).toUpperCase();
    $("#orgId").value = state.session.org_id;
    $("#workflowSummary").textContent = state.workflow
      ? `${state.workflow.workflow_id} · ${pretty(state.workflow.status_reason)}`
      : `Signed in as ${actor}. Ready to run ${state.session.org_id} workflows.`;
  }
  renderUploadTarget();
  syncControls();
}

function renderHealth() {
  const grid = $("#agentGrid");
  const badge = $("#flowBadge");
  const health = state.health;
  if (!health) {
    if (state.healthError) {
      $("#healthSummary").textContent = `Can't reach the orchestrator: ${state.healthError}`;
      badge.className = "pill tone-bad";
      badge.innerHTML = `<span class="dot"></span>Unreachable`;
      grid.innerHTML = emptyState("alert", "No health data", "Press refresh to try again.");
    } else {
      grid.innerHTML = stages.map(() => `<div class="skeleton agent-skeleton"></div>`).join("");
    }
    return;
  }
  const build = health.build;
  $("#buildInfo").textContent = build ? `v${build.version} · ${build.commit}` : "";
  const checked = state.healthAt ? ` · checked ${state.healthAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "";
  $("#healthSummary").textContent = `Orchestrator is ${pretty(health.status)}${health.flow ? ` · flow ${health.flow}` : ""}${checked}`;
  badge.className = `pill tone-${health.status === "ok" ? "ok" : toneOf(health.status) === "mute" ? "warn" : toneOf(health.status)}`;
  badge.innerHTML = `<span class="dot"></span>${health.status === "ok" ? "All systems normal" : pretty(health.status)}`;
  const demoStages = new Set(health.build?.demo_mode || []);
  grid.innerHTML = stages
    .map((stage, index) => {
      const agent = health.agents?.[stage.id] || {};
      const detail = [agent.mode, agent.owner].filter(Boolean).join(" · ");
      const demo = demoStages.has(stage.id)
        ? `<span class="pill tone-info" title="Runs on bundled sample data with local deterministic providers, not a live model"><span class="dot"></span>demo mode</span>`
        : "";
      return `<article class="agent-card tone-${toneOf(agent.status)}">
        <div class="agent-top">
          <span class="agent-num">${index + 1}</span>
          <span class="agent-pills">${demo}${pill(agent.status || "unknown")}</span>
        </div>
        <h3>${esc(stage.label)}</h3>
        <p class="agent-blurb">${esc(stage.blurb)}</p>
        <dl class="agent-meta">
          <div><dt>Agent</dt><dd>${esc(agent.agent_id || "-")}</dd></div>
          ${detail ? `<div><dt>Mode</dt><dd>${esc(detail)}</dd></div>` : ""}
          ${agent.implementation || agent.error ? `<div><dt>${agent.error ? "Error" : "Impl"}</dt><dd class="clamp" title="${esc(agent.implementation || agent.error)}">${esc(agent.implementation || agent.error)}</dd></div>` : ""}
        </dl>
      </article>`;
    })
    .join("");
}

/* ---------- rendering: recent + workflow ---------- */

function renderRecent() {
  const container = $("#recentWorkflows");
  const ids = readRecent();
  $("#clearRecent").classList.toggle("hidden", !ids.length);
  if (!ids.length) {
    container.innerHTML = emptyState("clock", "Nothing yet", "Workflows you run appear here.");
    return;
  }
  const signedIn = isSignedIn();
  container.innerHTML = ids
    .map((id) => `<div class="recent-item${state.workflow?.workflow_id === id ? " is-current" : ""}">
      <span class="mono">${esc(id)}</span>
      <button class="btn btn-secondary btn-sm" type="button" data-load="${esc(id)}" ${signedIn ? "" : "disabled"}>Load</button>
    </div>`)
    .join("");
}

function rememberWorkflow(id) {
  const ids = readRecent();
  try {
    localStorage.setItem("cube_recent_workflows", JSON.stringify([id, ...ids.filter((item) => item !== id)].slice(0, 8)));
  } catch {
    /* storage unavailable: recents are a convenience only */
  }
  renderRecent();
}

function defaultStageSelection(results) {
  const firstOpen = results.findIndex((result) => !["completed", "skipped"].includes(result.state));
  return firstOpen >= 0 ? firstOpen : Math.max(results.length - 1, 0);
}

function renderWorkflow() {
  const workflow = state.workflow;
  syncControls();
  renderRecent();
  renderOverrideOptions();
  if (!workflow) {
    $("#outcomePanel").innerHTML = "";
    $("#timeline").innerHTML = emptyState("flow", "No workflow loaded", "Pick a demo unit above and press Run workflow, or load a recent one.");
    $("#stageDetail").innerHTML = "";
    return;
  }
  const results = workflow.stage_results || [];
  if (state.selectedStage >= results.length) state.selectedStage = defaultStageSelection(results);
  $("#workflowSummary").textContent = `${workflow.workflow_id} · ${pretty(workflow.status_reason)}`;
  renderOutcome(workflow, results);
  renderPipeline(results);
  renderStageDetail(results);
  setDebug(workflow);
}

function renderOutcome(workflow, results) {
  const outcome = workflow.final_outcome || {};
  const label = outcome.outcome || workflow.status;
  const handled = results.filter((result) => ["completed", "skipped"].includes(result.state)).length;
  const blockers = results.filter((result) => result.state === "error");
  const reason = outcome.reason || workflow.status_reason || "";
  $("#outcomePanel").innerHTML = `<div class="outcome tone-${toneOf(label)}">
      <div class="outcome-main">
        <span class="eyebrow">Final outcome</span>
        <h3>${esc(pretty(outcome.outcome || "No final outcome yet"))}</h3>
        ${reason ? `<p>${esc(pretty(reason))}</p>` : ""}
        <div class="chips">
          ${pill(workflow.status)}
          ${outcome.needs_human ? `<span class="pill tone-warn"><span class="dot"></span>Human review needed</span>` : `<span class="pill tone-mute"><span class="dot"></span>No human action needed</span>`}
          ${outcome.provisional ? `<span class="pill tone-info"><span class="dot"></span>Provisional</span>` : ""}
        </div>
      </div>
      <dl class="outcome-stats">
        <div><dt>Stages handled</dt><dd>${handled}<small>/${results.length || stages.length}</small></dd></div>
        <div><dt>Blockers</dt><dd>${blockers.length}</dd></div>
        <div><dt>Claimable</dt><dd>${esc(money(outcome.claimable_usd))}</dd></div>
      </dl>
    </div>
    ${blockers.length ? `<div class="callout tone-bad" role="alert">${icon("alert")}<div><strong>${blockers.length} blocker${blockers.length > 1 ? "s" : ""} need attention</strong><ul>${blockers
      .map((stage) => `<li><b>${esc(pretty(stage.stage))}:</b> ${esc(stage.error?.message || stage.next_step_recommendation?.reason || "error")}</li>`)
      .join("")}</ul></div></div>` : ""}`;
}

function renderPipeline(results) {
  $("#timeline").innerHTML = `<ol class="pipeline" aria-label="Workflow stages">${results
    .map((result, index) => {
      const tone = toneOf(result.state);
      const selected = index === state.selectedStage;
      return `<li class="step tone-${tone}${selected ? " is-selected" : ""}" style="--i:${index}">
        <button type="button" class="step-btn" data-step="${index}" aria-pressed="${selected}" aria-label="${esc(pretty(result.stage))}: ${esc(pretty(result.state))}">
          <span class="step-node">${icon(TONE_ICON[tone])}</span>
          <span class="step-label">${esc(pretty(result.stage))}</span>
          <span class="step-state">${esc(pretty(result.state))}</span>
        </button>
      </li>`;
    })
    .join("")}</ol>`;
}

function renderStageDetail(results) {
  const result = results[state.selectedStage];
  const panel = $("#stageDetail");
  if (!result) {
    panel.innerHTML = "";
    return;
  }
  const reason = result.error?.message || result.skipped_reason || result.next_step_recommendation?.reason || "No further detail recorded.";
  panel.innerHTML = `<div class="card detail-card">
    <div class="detail-head">
      <h3>${esc(pretty(result.stage))}</h3>
      ${pill(result.state)}
    </div>
    <dl class="facts">
      <div><dt>Verdict</dt><dd>${result.verdict ? pill(result.verdict) : "-"}</dd></div>
      <div><dt>Outcome</dt><dd>${esc(pretty(result.outcome))}</dd></div>
      <div><dt>Record</dt><dd>${result.record_id
        ? `<span class="mono">${esc(result.record_id)}</span> <button class="icon-btn icon-btn-sm" type="button" data-copy="${esc(result.record_id)}" title="Copy record ID" aria-label="Copy record ID">${icon("copy")}</button>`
        : "-"}</dd></div>
      <div class="fact-wide"><dt>Reason</dt><dd>${esc(reason)}</dd></div>
    </dl>
    ${result.record_id ? `<div class="detail-actions"><button class="btn btn-secondary btn-sm" type="button" data-override-record="${esc(result.record_id)}">${icon("edit")}Override this record</button></div>` : ""}
  </div>`;
}

/* ---------- rendering: uploads ---------- */

function renderUploadTarget() {
  const unit = currentUnit() || "no unit";
  $("#uploadTarget").innerHTML = `<span class="dot"></span>Target: ${esc(unit)}`;
}

function renderUploads() {
  $("#uploadGrid").innerHTML = stages
    .map((stage) => `<article class="card upload-card" data-stage="${stage.id}">
      <div class="upload-head">
        <h3>${esc(stage.label)}</h3>
        <span class="count" id="count-${stage.id}"></span>
      </div>
      <label class="drop-zone" data-drop="${stage.id}">
        <input type="file" multiple accept="${esc(stage.accepts)}" data-file="${stage.id}">
        ${icon("upload")}
        <strong>Drop files or browse</strong>
        <span>${esc(stage.accepts.replace(/,/g, " · "))}</span>
      </label>
      <div class="file-list" id="files-${stage.id}">${renderFileRows(stage.id)}</div>
      <button class="btn btn-secondary btn-sm btn-block" type="button" data-upload-stage="${stage.id}" disabled>Upload staged files</button>
      <div class="file-list" id="server-${stage.id}">${renderServerInputRows(stage.id)}</div>
    </article>`)
    .join("");
  stages.forEach((stage) => updateCount(stage.id));
  syncControls();
}

function updateCount(stageId) {
  const node = $(`#count-${stageId}`);
  if (!node) return;
  const staged = state.stagedFiles[stageId].length;
  const saved = (state.serverInputs[stageId] || []).length;
  node.textContent = staged ? `${staged} staged` : saved ? `${saved} saved` : "";
  node.classList.toggle("has-staged", staged > 0);
}

function renderFileRows(stageId) {
  const files = state.stagedFiles[stageId] || [];
  if (!files.length) return "";
  return files
    .map((file, index) => `<div class="file-row">
      <span class="file-name">${icon("file")}<span><b>${esc(file.name)}</b><small>${Math.max(1, Math.ceil(file.size / 1024))} KB · staged</small></span></span>
      <button class="icon-btn icon-btn-sm" type="button" data-remove-file="${stageId}:${index}" title="Remove from staging" aria-label="Remove ${esc(file.name)} from staging">${icon("x")}</button>
    </div>`)
    .join("");
}

function renderServerInputRows(stageId) {
  const inputs = state.serverInputs[stageId] || [];
  if (!inputs.length) return `<p class="muted small">No saved inputs for this unit.</p>`;
  return inputs
    .map((input) => {
      const filename = String(input.ref || "").split("/").pop();
      return `<div class="file-row is-saved">
        <span class="file-name">${icon("check")}<span><b>${esc(filename)}</b><small>${esc(input.kind)} · ${esc(String(input.sha256 || "").slice(0, 12))}</small></span></span>
        <button class="icon-btn icon-btn-sm danger" type="button" data-delete-input="${esc(stageId)}:${esc(filename)}" title="Delete saved input" aria-label="Delete ${esc(filename)}">${icon("trash")}</button>
      </div>`;
    })
    .join("");
}

function refreshStageLists(stageId) {
  const staged = $(`#files-${stageId}`);
  if (staged) staged.innerHTML = renderFileRows(stageId);
  const saved = $(`#server-${stageId}`);
  if (saved) saved.innerHTML = renderServerInputRows(stageId);
  updateCount(stageId);
  syncControls();
}

function addFiles(stageId, files) {
  const unit = currentUnit();
  if (!unit) {
    showToast("Enter a Unit ID before staging files.", "error");
    return;
  }
  if (state.stagedUnit && state.stagedUnit !== unit) clearStaged(false);
  state.stagedUnit = unit;
  state.stagedFiles[stageId].push(...Array.from(files));
  refreshStageLists(stageId);
}

function clearStaged(announce) {
  const had = stages.some((stage) => state.stagedFiles[stage.id].length);
  stages.forEach((stage) => {
    state.stagedFiles[stage.id] = [];
    refreshStageLists(stage.id);
  });
  state.stagedUnit = null;
  if (announce && had) showToast("Staged files cleared because the unit changed.", "info");
}

async function refreshInputs() {
  const unitId = currentUnit();
  state.serverInputs = Object.fromEntries(stages.map((stage) => [stage.id, []]));
  if (unitId && isSignedIn()) {
    const listing = await request(`/inputs/${encodeURIComponent(unitId)}`);
    state.serverInputs = { ...state.serverInputs, ...(listing.stages || {}) };
  }
  stages.forEach((stage) => refreshStageLists(stage.id));
}

function handleUnitChange() {
  renderUploadTarget();
  if (state.stagedUnit && state.stagedUnit !== currentUnit()) clearStaged(true);
  refreshInputs().catch(() => {});
}

/* ---------- rendering: evidence + override ---------- */

function confidenceCell(value) {
  const number = Number(value);
  if (value == null || Number.isNaN(number)) return "-";
  const percent = Math.round((number <= 1 ? number : number / 100) * 100);
  return `<span class="meter" role="img" aria-label="${percent}% confidence"><span style="width:${Math.min(percent, 100)}%"></span></span><span class="meter-num">${percent}%</span>`;
}

function renderEvidence() {
  const container = $("#evidenceList");
  const bundle = state.evidence;
  if (!bundle?.evidence) {
    container.innerHTML = emptyState("evidence", "Evidence not loaded", state.workflow ? "Press Load evidence to trace the decision chain." : "Run or load a workflow first.");
    return;
  }
  const currentIds = new Set((bundle.workflow?.stage_results || []).map((stage) => stage.record_id).filter(Boolean));
  const records = Object.values(bundle.evidence).filter((record) => currentIds.has(record.record_id));
  container.innerHTML = records
    .map((record, index) => {
      const checks = (record.checks || [])
        .map((check) => `<tr>
          <td data-label="Check"><span class="mono">${esc(check.check_key)}</span></td>
          <td data-label="Verdict">${pill(check.verdict)}</td>
          <td data-label="Confidence" class="conf">${confidenceCell(check.confidence)}</td>
          <td data-label="Reason">${esc(check.uncertain_reason || check.detail || "-")}</td>
          <td data-label="Refs">${esc((check.evidence_refs || []).join(", ") || "-")}</td>
        </tr>`)
        .join("");
      const inputs = (record.inputs || []).map((input) => `<li><span class="mono">${esc(input.ref)}</span> <small>${esc(String(input.sha256 || "").slice(0, 16))}</small></li>`).join("");
      const upstream = (record.upstream_refs || []).map((ref) => `<li class="mono">${esc(ref)}</li>`).join("");
      return `<details class="evidence-card" ${index === records.length - 1 ? "open" : ""}>
        <summary>
          <span class="ev-stage">${esc(pretty(record.stage))}</span>
          <span class="mono ev-id">${esc(record.record_id)}</span>
          ${pill(record.decision?.verdict)}
        </summary>
        <div class="ev-body">
          <p>${esc(record.decision?.reason || "No reason recorded.")}</p>
          <div class="ev-cols">
            <div><h4>Inputs</h4><ul class="ref-list">${inputs || "<li class='muted'>None</li>"}</ul></div>
            <div><h4>Upstream</h4><ul class="ref-list">${upstream || "<li class='muted'>None</li>"}</ul></div>
          </div>
          <div class="table-wrap">
            <table class="checks-table">
              <thead><tr><th>Check</th><th>Verdict</th><th>Confidence</th><th>Reason</th><th>Refs</th></tr></thead>
              <tbody>${checks || `<tr><td colspan="5" class="muted">No checks recorded.</td></tr>`}</tbody>
            </table>
          </div>
          <div class="detail-actions"><button class="btn btn-secondary btn-sm" type="button" data-override-record="${esc(record.record_id)}">${icon("edit")}Override this record</button></div>
        </div>
      </details>`;
    })
    .join("");
  setDebug(bundle);
}

function renderOverrideOptions() {
  const select = $("#recordId");
  const records = workflowRecords();
  const previous = select.value;
  if (!records.size) {
    select.disabled = true;
    select.innerHTML = `<option value="">Run or load a workflow first</option>`;
    return;
  }
  select.disabled = false;
  select.innerHTML = `<option value="">Choose a record…</option>${[...records]
    .map(([id, stage]) => `<option value="${esc(id)}">${esc(stage)} · ${esc(id)}</option>`)
    .join("")}`;
  if (records.has(previous)) select.value = previous;
}

function goToOverride(recordId) {
  $("#recordId").value = recordId;
  $("#override").scrollIntoView({ behavior: "smooth", block: "start" });
  window.setTimeout(() => $("#overrideReason").focus({ preventScroll: true }), 350);
}

/* ---------- data actions ---------- */

function renderAll() {
  renderSession();
  renderHealth();
  renderRecent();
  renderWorkflow();
  renderEvidence();
  renderUploads();
}

async function refreshSession() {
  state.session = await request("/auth/session");
  if (!isSignedIn()) {
    state.workflow = null;
    state.evidence = null;
  }
  renderAll();
  if (isSignedIn()) refreshInputs().catch(() => {});
}

async function refreshHealth() {
  try {
    state.health = await request("/health");
    state.healthError = null;
    state.healthAt = new Date();
  } catch (error) {
    state.health = null;
    state.healthError = error.message;
  }
  renderHealth();
}

async function loadWorkflow(id) {
  state.workflow = await request(`/workflows/${encodeURIComponent(id)}`);
  state.evidence = null;
  state.selectedStage = defaultStageSelection(state.workflow.stage_results || []);
  rememberWorkflow(state.workflow.workflow_id);
  renderWorkflow();
  renderEvidence();
}

async function loadEvidenceBundle() {
  if (!state.workflow) return;
  state.evidence = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/evidence`);
  renderEvidence();
  renderOverrideOptions();
}

function showRunning() {
  $("#outcomePanel").innerHTML = `<div class="skeleton outcome-skeleton"></div>`;
  $("#timeline").innerHTML = `<ol class="pipeline is-running" aria-label="Workflow running">${stages
    .map((stage, index) => `<li class="step tone-info" style="--i:${index}"><span class="step-btn"><span class="step-node">${icon("clock")}</span><span class="step-label">${esc(stage.label)}</span><span class="step-state">Running…</span></span></li>`)
    .join("")}</ol>`;
  $("#stageDetail").innerHTML = "";
  $("#workflowSummary").textContent = "Running the unit through every stage. This can take a moment.";
}

/* ---------- events ---------- */

function bindEvents() {
  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#sessionMessage");
    message.textContent = "";
    const token = $("#token").value;
    if (!token) {
      message.textContent = "Enter your organization access token.";
      $("#token").focus();
      return;
    }
    await withBusy($("#loginSubmit"), async () => {
      try {
        await request("/auth/session", { method: "POST", body: JSON.stringify({ org_id: $("#orgId").value, token }) });
        $("#token").value = "";
        await refreshSession();
        showToast("Signed in.", "success");
        $("#run").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        message.textContent = error.message;
      }
    });
  });

  $("#signOut").addEventListener("click", () =>
    withBusy($("#signOut"), async () => {
      try {
        await request("/auth/session", { method: "DELETE" });
        state.session = { authenticated: false };
        state.workflow = null;
        state.evidence = null;
        clearStaged(false);
        renderAll();
        window.scrollTo({ top: 0, behavior: "smooth" });
        showToast("Signed out.", "info");
      } catch (error) {
        showToast(error.message, "error");
      }
    }),
  );

  $("#refreshHealth").addEventListener("click", () =>
    withBusy($("#refreshHealth"), async () => {
      await refreshHealth();
      if (state.health) showToast("Health refreshed.", "success");
    }),
  );

  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("cube_theme", next);
    } catch {
      /* theme still applies for this visit */
    }
  });

  $("#workflowForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      org_id: state.session?.org_id || $("#orgId").value,
      unit_id: currentUnit(),
      returned: $("#returned").checked,
    };
    if ($("#route").value) body.route = $("#route").value;
    if (!body.unit_id) {
      showToast("Unit ID is required.", "error");
      $("#unitId").focus();
      return;
    }
    await withBusy($("#runWorkflow"), async () => {
      showRunning();
      $("#workflow").scrollIntoView({ behavior: "smooth", block: "start" });
      try {
        state.workflow = await request("/workflows", { method: "POST", body: JSON.stringify(body) });
        state.evidence = null;
        state.selectedStage = defaultStageSelection(state.workflow.stage_results || []);
        rememberWorkflow(state.workflow.workflow_id);
        renderWorkflow();
        renderEvidence();
        showToast("Workflow complete. Review the outcome.", "success");
      } catch (error) {
        state.workflow = null;
        renderWorkflow();
        showToast(error.message, "error");
      }
    });
  });

  $("#resumeWorkflow").addEventListener("click", () =>
    withBusy($("#resumeWorkflow"), async () => {
      if (!state.workflow) return;
      try {
        state.workflow = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/resume`, { method: "POST", body: "{}" });
        renderWorkflow();
        showToast("Workflow resumed.", "success");
      } catch (error) {
        showToast(error.message, "error");
      }
    }),
  );

  $("#loadEvidence").addEventListener("click", () =>
    withBusy($("#loadEvidence"), async () => {
      try {
        await loadEvidenceBundle();
      } catch (error) {
        showToast(error.message, "error");
      }
    }),
  );

  $("#overrideForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.workflow) return;
    const body = {
      record_id: $("#recordId").value.trim(),
      new_verdict: ($("#overrideForm").elements.new_verdict.value || "").trim(),
      reason: $("#overrideReason").value.trim(),
    };
    if (!body.record_id || !body.reason) {
      showToast("Choose a record and give a reason.", "error");
      (body.record_id ? $("#overrideReason") : $("#recordId")).focus();
      return;
    }
    await withBusy($("#submitOverride"), async () => {
      try {
        state.workflow = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/overrides`, {
          method: "POST",
          body: JSON.stringify(body),
        });
        $("#overrideReason").value = "";
        renderWorkflow();
        if (state.evidence) await loadEvidenceBundle();
        showToast("Override recorded and audited.", "success");
      } catch (error) {
        showToast(error.message, "error");
      }
    });
  });

  $("#clearRecent").addEventListener("click", () => {
    try {
      localStorage.removeItem("cube_recent_workflows");
    } catch {
      /* nothing to clear */
    }
    renderRecent();
  });

  $("#copyDebug").addEventListener("click", () => copyText($("#debugJson").textContent, "Debug JSON copied."));

  $$("[data-auth]").forEach((link) =>
    link.addEventListener("click", (event) => {
      if (isSignedIn()) return;
      event.preventDefault();
      showToast("Sign in to use this section.", "info");
      $("#gate").scrollIntoView({ behavior: "smooth", block: "start" });
      $("#token").focus({ preventScroll: true });
    }),
  );

  document.addEventListener("click", (event) => {
    const target = event.target.closest("button, [data-step]");
    if (!target) return;
    const data = target.dataset;
    if (data.load) return void handleLoad(data.load, target);
    if (data.step != null) return void selectStage(Number(data.step));
    if (data.copy) return void copyText(data.copy, "Record ID copied.");
    if (data.overrideRecord) return void goToOverride(data.overrideRecord);
    if (data.removeFile) return void removeStaged(data.removeFile);
    if (data.uploadStage) return void handleUpload(data.uploadStage, target);
    if (data.deleteInput) return void handleDelete(data.deleteInput, target);
    if (data.demoUnit) return void pickDemoUnit(target);
  });

  document.addEventListener("change", (event) => {
    const stageId = event.target.dataset?.file;
    if (stageId) {
      addFiles(stageId, event.target.files);
      event.target.value = "";
    }
    if (event.target.id === "unitId") handleUnitChange();
  });

  $("#unitId").addEventListener("input", renderUploadTarget);

  // Stop the browser navigating away when a file is dropped outside a drop zone.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());

  document.addEventListener("dragover", (event) => {
    const zone = event.target.closest?.("[data-drop]");
    if (!zone) return;
    event.preventDefault();
    zone.classList.add("dragging");
  });

  document.addEventListener("dragleave", (event) => {
    const zone = event.target.closest?.("[data-drop]");
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("dragging");
  });

  document.addEventListener("drop", (event) => {
    const zone = event.target.closest?.("[data-drop]");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("dragging");
    addFiles(zone.dataset.drop, event.dataTransfer.files);
  });
}

async function handleLoad(id, button) {
  if (!isSignedIn()) return showToast("Sign in before loading workflows.", "error");
  await withBusy(button, async () => {
    try {
      await loadWorkflow(id);
      $("#workflow").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      showToast(error.message, "error");
    }
  });
}

function selectStage(index) {
  state.selectedStage = index;
  const results = state.workflow?.stage_results || [];
  renderPipeline(results);
  renderStageDetail(results);
}

function removeStaged(spec) {
  const [stageId, index] = spec.split(":");
  state.stagedFiles[stageId].splice(Number(index), 1);
  if (!stages.some((stage) => state.stagedFiles[stage.id].length)) state.stagedUnit = null;
  refreshStageLists(stageId);
}

async function handleUpload(stageId, button) {
  const unitId = currentUnit();
  const files = state.stagedFiles[stageId] || [];
  if (!isSignedIn()) return showToast("Sign in before uploading files.", "error");
  if (!unitId) return showToast("Unit ID is required before uploading.", "error");
  if (state.stagedUnit && state.stagedUnit !== unitId) {
    clearStaged(false);
    return showToast(`Those files were staged for ${state.stagedUnit}. Stage them again for ${unitId}.`, "error");
  }
  if (!files.length) return showToast("Choose files before uploading.", "error");
  await withBusy(button, async () => {
    let uploaded = 0;
    try {
      for (const file of [...files]) {
        await uploadFile(unitId, stageId, file);
        state.stagedFiles[stageId].shift();
        uploaded += 1;
      }
      showToast(`${uploaded} file${uploaded === 1 ? "" : "s"} uploaded to ${unitId}.`, "success");
    } catch (error) {
      showToast(`${error.message}${uploaded ? ` (${uploaded} uploaded before the error)` : ""}`, "error");
    }
    if (!stages.some((stage) => state.stagedFiles[stage.id].length)) state.stagedUnit = null;
    try {
      await refreshInputs();
    } catch {
      refreshStageLists(stageId);
    }
  });
}

async function handleDelete(spec, button) {
  if (!isSignedIn()) return showToast("Sign in before deleting inputs.", "error");
  if (button.dataset.armed !== "1") {
    button.dataset.armed = "1";
    button.classList.add("is-armed");
    button.title = "Click again to confirm delete";
    window.setTimeout(() => {
      delete button.dataset.armed;
      button.classList.remove("is-armed");
    }, 3000);
    return showToast("Click the bin again to confirm deleting this input.", "info");
  }
  const unitId = currentUnit();
  const [stageId, filename] = spec.split(":");
  await withBusy(button, async () => {
    try {
      await request(`/inputs/${encodeURIComponent(unitId)}/${encodeURIComponent(stageId)}/${encodeURIComponent(filename)}`, { method: "DELETE" });
      await refreshInputs();
      showToast("Input deleted.", "success");
    } catch (error) {
      showToast(error.message, "error");
    }
  });
}

function pickDemoUnit(button) {
  $("#unitId").value = button.dataset.demoUnit;
  $("#route").value = button.dataset.demoRoute || "";
  $("#returned").checked = button.dataset.demoReturned === "true";
  $$(".demo-unit").forEach((node) => node.classList.toggle("is-selected", node === button));
  handleUnitChange();
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(message, "success");
  } catch {
    showToast("Copy isn't available in this browser context.", "error");
  }
}

function watchSections() {
  if (!("IntersectionObserver" in window)) return;
  const links = new Map($$("[data-spy]").map((link) => [link.dataset.spy, link]));
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        links.forEach((link, id) => link.classList.toggle("active", id === entry.target.id));
      }
    },
    { rootMargin: "-30% 0px -60% 0px" },
  );
  links.forEach((_, id) => {
    const section = document.getElementById(id);
    if (section) observer.observe(section);
  });
}

async function boot() {
  renderAll();
  bindEvents();
  watchSections();
  await Promise.all([
    refreshSession().catch((error) => showToast(error.message, "error")),
    refreshHealth(),
  ]);
  window.setInterval(() => {
    if (!document.hidden) refreshHealth();
  }, 30000);
}

boot();
