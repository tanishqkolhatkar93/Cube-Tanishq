const stages = [
  { id: "receiving", label: "Receiving", accepts: "image/*" },
  { id: "prep", label: "Prep", accepts: "image/*,.json,.csv,.pdf" },
  { id: "pack", label: "Pack", accepts: "image/*" },
  { id: "returns", label: "Returns", accepts: "image/*,.json,.csv,.pdf" },
  { id: "recovery", label: "Recovery", accepts: ".json,.csv,.pdf,text/csv,application/json" },
];

const state = {
  session: null,
  health: null,
  workflow: null,
  evidence: null,
  stagedFiles: Object.fromEntries(stages.map((stage) => [stage.id, []])),
  serverInputs: Object.fromEntries(stages.map((stage) => [stage.id, []])),
};

const $ = (selector) => document.querySelector(selector);

function classFor(value) {
  return String(value || "neutral").toLowerCase().replace(/[^a-z0-9]+/g, "_");
}

function pretty(value) {
  return value == null ? "-" : String(value).replace(/_/g, " ");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 3200);
}

function setDebug(value) {
  $("#debugJson").textContent = JSON.stringify(value || {}, null, 2);
}

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
    const message = body.detail || body.error || `${response.status} ${response.statusText}`;
    throw new Error(message);
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

function renderSession() {
  const signedIn = Boolean(state.session?.authenticated);
  $("#sessionPanel").classList.toggle("hidden", signedIn);
  $("#signOut").classList.toggle("hidden", !signedIn);
  $("#runWorkflow").disabled = !signedIn;
  $("#workflowSummary").textContent = signedIn
    ? `Signed in as ${state.session.actor}. Ready to run ${state.session.org_id} workflows.`
    : "Sign in to run or inspect workflows.";
  if (signedIn) $("#orgId").value = state.session.org_id;
  document.querySelectorAll("[data-upload-stage], [data-delete-input]").forEach((button) => {
    button.disabled = !signedIn;
    button.title = signedIn ? "" : "Sign in before managing local inputs";
  });
}

function renderHealth() {
  const grid = $("#agentGrid");
  const health = state.health;
  if (!health) {
    grid.innerHTML = "";
    return;
  }
  $("#healthSummary").textContent = `Orchestrator is ${health.status}.`;
  $("#flowBadge").textContent = health.flow || "flow unknown";
  $("#flowBadge").className = `status-pill ${classFor(health.status)}`;
  grid.innerHTML = stages
    .map((stage) => {
      const agent = health.agents?.[stage.id] || {};
      return `<article class="agent-card">
        <div class="stage-top">
          <strong>${stage.label}</strong>
          <span class="status-pill ${classFor(agent.status)}">${pretty(agent.status)}</span>
        </div>
        <div class="meta">${agent.agent_id || "No agent id"}</div>
        <div class="meta">${agent.mode || ""} ${agent.owner ? `· ${agent.owner}` : ""}</div>
        <div class="meta">${agent.implementation || agent.error || ""}</div>
      </article>`;
    })
    .join("");
}

function renderRecent() {
  const container = $("#recentWorkflows");
  const ids = JSON.parse(localStorage.getItem("cube_recent_workflows") || "[]");
  const signedIn = Boolean(state.session?.authenticated);
  if (!ids.length) {
    container.className = "recent-list empty";
    container.textContent = "No recent workflows in this browser.";
    return;
  }
  container.className = "recent-list";
  container.innerHTML = ids
    .map((id) => `<div class="recent-item"><span class="meta">${id}</span><button class="secondary" data-load="${id}" ${signedIn ? "" : "disabled"}>Load</button></div>`)
    .join("");
}

function rememberWorkflow(id) {
  const ids = JSON.parse(localStorage.getItem("cube_recent_workflows") || "[]");
  localStorage.setItem("cube_recent_workflows", JSON.stringify([id, ...ids.filter((item) => item !== id)].slice(0, 8)));
  renderRecent();
}

function renderWorkflow() {
  const workflow = state.workflow;
  $("#loadEvidence").disabled = !workflow;
  $("#submitOverride").disabled = !workflow;
  $("#resumeWorkflow").disabled = !workflow || !["FAILED", "BLOCKED", "RECOVERY_REQUIRED", "IN_PROGRESS"].includes(workflow.status);
  if (!workflow) {
    $("#timeline").className = "timeline empty";
    $("#timeline").textContent = "Run or load a workflow to see the five-stage timeline.";
    $("#outcomePanel").className = "outcome empty";
    $("#outcomePanel").textContent = "Final outcome will appear here.";
    return;
  }
  $("#workflowSummary").textContent = `${workflow.workflow_id} · ${workflow.status_reason}`;
  $("#timeline").className = "timeline";
  $("#timeline").innerHTML = workflow.stage_results
    .map((result, index) => `<article class="stage-card">
      <div class="stage-index">${index + 1}</div>
      <div class="stage-top">
        <strong>${pretty(result.stage)}</strong>
        <span class="status-pill ${classFor(result.state)}">${pretty(result.state)}</span>
      </div>
      <p class="meta">Verdict: <b>${pretty(result.verdict)}</b></p>
      <p class="meta">Outcome: ${pretty(result.outcome)}</p>
      <p class="meta">Record: ${result.record_id || "-"}</p>
      <p class="meta">Reason: ${result.error?.message || result.skipped_reason || result.next_step_recommendation?.reason || "-"}</p>
    </article>`)
    .join("");
  const outcome = workflow.final_outcome || {};
  $("#outcomePanel").className = `outcome ${classFor(outcome.outcome || workflow.status)}`;
  $("#outcomePanel").innerHTML = `<div class="stage-top">
    <strong>${pretty(outcome.outcome || "No final outcome")}</strong>
    <span class="status-pill ${classFor(workflow.status)}">${pretty(workflow.status)}</span>
  </div>
  <p>${outcome.reason || workflow.status_reason || ""}</p>
  <p class="meta">Needs human: ${Boolean(outcome.needs_human)} · Provisional: ${Boolean(outcome.provisional)} · Claimable: ${outcome.claimable_usd ?? "-"}</p>`;
  renderDemoReadiness(workflow);
  setDebug(workflow);
}

function renderDemoReadiness(workflow) {
  const done = new Set((workflow.stage_results || []).filter((stage) => stage.state === "completed" || stage.state === "skipped").map((stage) => stage.stage));
  const blockers = (workflow.stage_results || []).filter((stage) => stage.state === "error").map((stage) => `${pretty(stage.stage)}: ${stage.error?.message || stage.next_step_recommendation?.reason || "error"}`);
  const panel = $("#demoReadiness");
  if (!panel) return;
  panel.innerHTML = `<div class="readiness-grid">
    <div><strong>${done.size}/5</strong><span>Stages handled</span></div>
    <div><strong>${pretty(workflow.final_outcome?.outcome || workflow.status)}</strong><span>Final outcome</span></div>
    <div><strong>${blockers.length}</strong><span>Visible blockers</span></div>
  </div>
  ${blockers.length ? `<p class="meta">Blockers: ${blockers.join(" · ")}</p>` : `<p class="meta">Ready to trace evidence and explain uncertainty.</p>`}`;
}

function renderUploads() {
  const signedIn = Boolean(state.session?.authenticated);
  $("#uploadGrid").innerHTML = stages
    .map((stage) => `<article class="upload-card" data-stage="${stage.id}">
      <strong>${stage.label}</strong>
      <div class="drop-zone" data-drop="${stage.id}">Drop files here or choose files</div>
      <input type="file" multiple accept="${stage.accepts}" data-file="${stage.id}">
      <div class="file-list" id="files-${stage.id}">${renderFileRows(stage.id)}</div>
      <button class="secondary" type="button" data-upload-stage="${stage.id}" ${signedIn ? "" : "disabled"}>Upload staged files</button>
      <div class="file-list" id="server-${stage.id}">${renderServerInputRows(stage.id)}</div>
      <p class="meta">Uploads are saved under data/input/{unit_id}/${stage.id} and picked up by the orchestrator.</p>
    </article>`)
    .join("");
}

function renderFileRows(stageId) {
  const files = state.stagedFiles[stageId] || [];
  if (!files.length) return `<span class="meta">No files staged.</span>`;
  return files
    .map((file, index) => `<div class="file-row"><span>${file.name}<br><span class="meta">${Math.ceil(file.size / 1024)} KB</span></span><button class="secondary" data-remove-file="${stageId}:${index}">Remove</button></div>`)
    .join("");
}

function renderServerInputRows(stageId) {
  const inputs = state.serverInputs[stageId] || [];
  if (!inputs.length) return `<span class="meta">No server inputs for this unit/stage.</span>`;
  return inputs
    .map((input) => {
      const filename = input.ref.split("/").pop();
      return `<div class="file-row"><span>${filename}<br><span class="meta">${input.kind} · ${input.sha256}</span></span><button class="secondary" data-delete-input="${stageId}:${filename}">Delete</button></div>`;
    })
    .join("");
}

function addFiles(stageId, files) {
  state.stagedFiles[stageId].push(...Array.from(files));
  $(`#files-${stageId}`).innerHTML = renderFileRows(stageId);
}

async function refreshInputs() {
  const unitId = $("#unitId").value.trim();
  if (!unitId || !state.session?.authenticated) return;
  const listing = await request(`/inputs/${encodeURIComponent(unitId)}`);
  state.serverInputs = { ...state.serverInputs, ...(listing.stages || {}) };
  stages.forEach((stage) => {
    const target = $(`#server-${stage.id}`);
    if (target) target.innerHTML = renderServerInputRows(stage.id);
  });
}

function renderEvidence() {
  const container = $("#evidenceList");
  const bundle = state.evidence;
  if (!bundle?.evidence) {
    container.className = "evidence-list empty";
    container.textContent = "Evidence bundle not loaded.";
    return;
  }
  container.className = "evidence-list";
  const records = Object.values(bundle.evidence);
  container.innerHTML = records
    .map((record) => {
      const checks = (record.checks || [])
        .map((check) => `<tr>
          <td>${check.check_key}</td>
          <td><span class="status-pill ${classFor(check.verdict)}">${check.verdict}</span></td>
          <td>${check.confidence ?? "-"}</td>
          <td>${check.uncertain_reason || check.detail || "-"}</td>
          <td>${(check.evidence_refs || []).join(", ") || "-"}</td>
        </tr>`)
        .join("");
      return `<details class="evidence-card">
        <summary>${record.record_id} · ${record.stage} · ${record.decision?.verdict}</summary>
        <p class="meta">${record.decision?.reason || ""}</p>
        <p class="meta">Inputs: ${(record.inputs || []).map((input) => `${input.ref} (${input.sha256})`).join(", ") || "-"}</p>
        <p class="meta">Upstream: ${(record.upstream_refs || []).join(", ") || "-"}</p>
        <table class="checks-table">
          <thead><tr><th>Check</th><th>Verdict</th><th>Confidence</th><th>Reason</th><th>Refs</th></tr></thead>
          <tbody>${checks || `<tr><td colspan="5">No checks recorded.</td></tr>`}</tbody>
        </table>
      </details>`;
    })
    .join("");
  setDebug(bundle);
}

async function refreshSession() {
  state.session = await request("/auth/session");
  if (!state.session?.authenticated) {
    state.workflow = null;
    state.evidence = null;
  }
  renderSession();
  renderRecent();
  renderWorkflow();
  renderEvidence();
}

async function refreshHealth() {
  state.health = await request("/health");
  renderHealth();
}

async function loadWorkflow(id) {
  state.workflow = await request(`/workflows/${encodeURIComponent(id)}`);
  state.evidence = null;
  rememberWorkflow(state.workflow.workflow_id);
  renderWorkflow();
  renderEvidence();
}

async function loadEvidenceBundle() {
  if (!state.workflow) return;
  state.evidence = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/evidence`);
  renderEvidence();
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("#sessionMessage").textContent = "";
    try {
      const body = { org_id: $("#orgId").value, token: $("#token").value };
      await request("/auth/session", { method: "POST", body: JSON.stringify(body) });
      $("#token").value = "";
      await refreshSession();
      showToast("Signed in.");
    } catch (error) {
      $("#sessionMessage").textContent = error.message;
    }
  });

  $("#signOut").addEventListener("click", async () => {
    await request("/auth/session", { method: "DELETE" });
    state.session = null;
    state.workflow = null;
    state.evidence = null;
    await refreshSession();
    renderWorkflow();
    renderEvidence();
    showToast("Signed out.");
  });

  $("#refreshHealth").addEventListener("click", () => refreshHealth().catch((error) => showToast(error.message)));

  $("#workflowForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      org_id: state.session?.org_id || $("#orgId").value,
      unit_id: $("#unitId").value.trim(),
      returned: $("#returned").checked,
    };
    if ($("#route").value) body.route = $("#route").value;
    if (!body.unit_id) {
      showToast("Unit ID is required.");
      return;
    }
    try {
      state.workflow = await request("/workflows", { method: "POST", body: JSON.stringify(body) });
      state.evidence = null;
      rememberWorkflow(state.workflow.workflow_id);
      renderWorkflow();
      renderEvidence();
      showToast("Workflow complete. Review the outcome.");
    } catch (error) {
      showToast(error.message);
    }
  });

  $("#resumeWorkflow").addEventListener("click", async () => {
    if (!state.workflow) return;
    try {
      state.workflow = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/resume`, { method: "POST", body: "{}" });
      renderWorkflow();
      showToast("Workflow resumed.");
    } catch (error) {
      showToast(error.message);
    }
  });

  $("#loadEvidence").addEventListener("click", () => loadEvidenceBundle().catch((error) => showToast(error.message)));

  $("#overrideForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.workflow) return;
    const body = {
      record_id: $("#recordId").value.trim(),
      new_verdict: $("#newVerdict").value,
      reason: $("#overrideReason").value.trim(),
    };
    if (!body.record_id || !body.reason) {
      showToast("Record ID and reason are required.");
      return;
    }
    try {
      state.workflow = await request(`/workflows/${encodeURIComponent(state.workflow.workflow_id)}/overrides`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      renderWorkflow();
      showToast("Override recorded.");
    } catch (error) {
      showToast(error.message);
    }
  });

  document.addEventListener("click", async (event) => {
    const loadId = event.target.dataset?.load;
    const removeSpec = event.target.dataset?.removeFile;
    const uploadStage = event.target.dataset?.uploadStage;
    const deleteSpec = event.target.dataset?.deleteInput;
    if (loadId) {
      if (!state.session?.authenticated) {
        showToast("Sign in before loading workflows.");
        return;
      }
      try {
        await loadWorkflow(loadId);
      } catch (error) {
        showToast(error.message);
      }
    }
    if (removeSpec) {
      const [stageId, index] = removeSpec.split(":");
      state.stagedFiles[stageId].splice(Number(index), 1);
      $(`#files-${stageId}`).innerHTML = renderFileRows(stageId);
    }
    if (uploadStage) {
      const unitId = $("#unitId").value.trim();
      const files = state.stagedFiles[uploadStage] || [];
      if (!state.session?.authenticated) return showToast("Sign in before uploading files.");
      if (!unitId) return showToast("Unit ID is required before uploading.");
      if (!files.length) return showToast("Choose files before uploading.");
      try {
        for (const file of files) await uploadFile(unitId, uploadStage, file);
        state.stagedFiles[uploadStage] = [];
        $(`#files-${uploadStage}`).innerHTML = renderFileRows(uploadStage);
        await refreshInputs();
        showToast("Files uploaded.");
      } catch (error) {
        showToast(error.message);
      }
    }
    if (deleteSpec) {
      const unitId = $("#unitId").value.trim();
      const [stageId, filename] = deleteSpec.split(":");
      if (!state.session?.authenticated) return showToast("Sign in before deleting inputs.");
      try {
        await request(`/inputs/${encodeURIComponent(unitId)}/${encodeURIComponent(stageId)}/${encodeURIComponent(filename)}`, {
          method: "DELETE",
        });
        await refreshInputs();
        showToast("Input deleted.");
      } catch (error) {
        showToast(error.message);
      }
    }
  });

  $("#clearRecent").addEventListener("click", () => {
    localStorage.removeItem("cube_recent_workflows");
    renderRecent();
  });

  document.addEventListener("change", (event) => {
    const stageId = event.target.dataset?.file;
    if (stageId) addFiles(stageId, event.target.files);
    if (event.target.id === "unitId") refreshInputs().catch(() => {});
  });

  document.addEventListener("dragover", (event) => {
    const stageId = event.target.dataset?.drop;
    if (!stageId) return;
    event.preventDefault();
    event.target.classList.add("dragging");
  });

  document.addEventListener("dragleave", (event) => {
    if (event.target.dataset?.drop) event.target.classList.remove("dragging");
  });

  document.addEventListener("drop", (event) => {
    const stageId = event.target.dataset?.drop;
    if (!stageId) return;
    event.preventDefault();
    event.target.classList.remove("dragging");
    addFiles(stageId, event.dataTransfer.files);
  });
}

async function boot() {
  renderUploads();
  renderRecent();
  bindEvents();
  try {
    await Promise.all([refreshSession(), refreshHealth()]);
  } catch (error) {
    showToast(error.message);
  }
}

boot();
