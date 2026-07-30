const userSelect = document.getElementById("user-select");
const projectInput = document.getElementById("project-input");
const promptInput = document.getElementById("prompt-input");
const routeForm = document.getElementById("route-form");
const resultEl = document.getElementById("result");
const budgetsList = document.getElementById("budgets-list");
const auditBody = document.querySelector("#audit-table tbody");
const refreshAuditBtn = document.getElementById("refresh-audit");

let usersCache = [];

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail || res.statusText;
    throw new Error(detail);
  }
  return body;
}

function budgetBarClass(budget) {
  if (budget.over_hard_limit) return "block";
  if (budget.over_soft_limit) return "warn";
  return "";
}

function renderBudgets(users) {
  budgetsList.innerHTML = "";
  if (users.length === 0) {
    budgetsList.innerHTML = '<p class="empty">No users seeded.</p>';
    return;
  }
  for (const user of users) {
    const pct = Math.min(100, (user.budget.spent_usd / user.budget.limit_usd) * 100);
    const item = document.createElement("div");
    item.className = "budget-item";
    item.innerHTML = `
      <div class="budget-item-head">
        <span><strong>${user.user_id}</strong> <span class="muted">(${user.team}, max ${user.max_tier})</span></span>
        <span class="muted">$${user.budget.spent_usd.toFixed(2)} / $${user.budget.limit_usd.toFixed(2)}</span>
      </div>
      <div class="budget-bar"><div class="budget-bar-fill ${budgetBarClass(user.budget)}" style="width:${pct}%"></div></div>
    `;
    budgetsList.appendChild(item);
  }
}

function populateUserSelect(users) {
  const previousValue = userSelect.value;
  userSelect.innerHTML = "";
  for (const user of users) {
    const opt = document.createElement("option");
    opt.value = user.user_id;
    const projectNote = user.allowed_projects.length
      ? ` (projects: ${user.allowed_projects.join(", ")})`
      : "";
    opt.textContent = `${user.user_id} — ${user.team}${projectNote}`;
    userSelect.appendChild(opt);
  }
  if (previousValue && users.some((u) => u.user_id === previousValue)) {
    userSelect.value = previousValue;
  }
}

async function loadUsers() {
  usersCache = await fetchJSON("/users");
  populateUserSelect(usersCache);
  renderBudgets(usersCache);
}

async function loadProjects() {
  const projects = await fetchJSON("/projects");
  const hintEl = document.getElementById("restricted-projects-hint");
  if (projects.length === 0) {
    hintEl.textContent = "";
    return;
  }
  const summary = projects
    .map((p) => `${p.project_id} (${p.authorized_users.join(", ") || "no one"})`)
    .join("; ");
  hintEl.textContent = `Restricted projects — only listed users may use them: ${summary}`;
}

function renderResult(response) {
  resultEl.classList.remove("hidden");
  const badgeClass = response.allowed ? "allowed" : "blocked";
  const badgeText = response.allowed ? "Allowed" : "Blocked";

  const tierRow = response.allowed
    ? `<div class="result-row"><span class="label">Routed to:</span><strong>${response.final_tier}</strong> tier (${response.model_name})</div>`
    : "";

  const costRow = response.allowed
    ? `<div class="result-row"><span class="label">Cost:</span>$${response.cost_usd.toFixed(4)} &nbsp; <span class="label">Latency:</span>${response.latency_ms.toFixed(0)}ms</div>`
    : "";

  const feedbackRow = response.allowed
    ? `<div class="feedback-row">
         <button class="secondary small" data-signal="escalate" data-audit-id="${response.audit_id}">Router under-shot</button>
         <button class="secondary small" data-signal="downgrade_ok" data-audit-id="${response.audit_id}">Router over-shot</button>
         <span class="feedback-status" id="feedback-status"></span>
       </div>`
    : "";

  resultEl.innerHTML = `
    <div class="result-row">
      <span class="badge ${badgeClass}">${badgeText}</span>
      <span class="label" style="margin-left:0.5rem">audit_id ${response.audit_id}</span>
    </div>
    ${tierRow}
    ${costRow}
    <div class="result-row"><span class="label">Classifier:</span>${response.classifier.complexity} complexity &rarr; suggested ${response.classifier.suggested_tier}</div>
    <div class="reasoning">${response.reason}</div>
    ${feedbackRow}
  `;

  if (response.allowed) {
    for (const btn of resultEl.querySelectorAll("[data-signal]")) {
      btn.addEventListener("click", () => submitFeedback(btn));
    }
  }
}

async function submitFeedback(btn) {
  const auditId = Number(btn.dataset.auditId);
  const signal = btn.dataset.signal;
  const statusEl = document.getElementById("feedback-status");
  for (const b of resultEl.querySelectorAll("[data-signal]")) b.disabled = true;
  try {
    await fetchJSON("/feedback", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ audit_id: auditId, signal }),
    });
    statusEl.textContent = "Feedback recorded — thanks.";
    loadAudit();
  } catch (err) {
    statusEl.textContent = `Feedback failed: ${err.message}`;
  }
}

routeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submitBtn = routeForm.querySelector("button[type=submit]");
  submitBtn.disabled = true;
  resultEl.classList.add("hidden");
  try {
    const response = await fetchJSON("/route", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        user_id: userSelect.value,
        prompt: promptInput.value,
        project: projectInput.value || null,
      }),
    });
    renderResult(response);
    loadAudit();
    loadUsers();
  } catch (err) {
    resultEl.classList.remove("hidden");
    resultEl.innerHTML = `<div class="result-row"><span class="badge blocked">Error</span> ${err.message}</div>`;
  } finally {
    submitBtn.disabled = false;
  }
});

function renderAudit(entries) {
  auditBody.innerHTML = "";
  if (entries.length === 0) {
    auditBody.innerHTML = '<tr><td colspan="7" class="empty">No requests yet.</td></tr>';
    return;
  }
  for (const entry of entries) {
    const tr = document.createElement("tr");
    const time = new Date(entry.timestamp).toLocaleTimeString();
    const outcome = entry.allowed
      ? '<span class="badge allowed">Allowed</span>'
      : '<span class="badge blocked">Blocked</span>';
    const tierPath = `${entry.classifier.complexity} → ${entry.final_tier || "—"}`;
    tr.innerHTML = `
      <td>${time}</td>
      <td>${entry.user_id}</td>
      <td>${entry.project || "—"}</td>
      <td>${tierPath}</td>
      <td>${outcome}</td>
      <td class="reason-cell" title="${entry.reason}">${entry.reason}</td>
      <td>$${entry.cost_usd.toFixed(4)}</td>
    `;
    auditBody.appendChild(tr);
  }
}

async function loadAudit() {
  const entries = await fetchJSON("/audit?n=25");
  renderAudit(entries);
}

refreshAuditBtn.addEventListener("click", loadAudit);

loadUsers();
loadProjects();
loadAudit();
