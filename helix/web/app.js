// Helix web UI: solve dashboard, chat, diff review, memory.

const $ = (sel) => document.querySelector(sel);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
});

function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("#tab-" + tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "diffs") loadDiffs();
    if (tab.dataset.tab === "memory") loadMemory();
  });
});

// ---------- health ----------
async function loadHealth() {
  try {
    const h = await api("/api/health");
    $("#health").className = "dot ok";
    $("#modelLabel").textContent = `${h.model.model} · ${h.workspace}`;
  } catch (e) {
    $("#health").className = "dot bad";
    $("#modelLabel").textContent = "offline";
  }
}

// ---------- solve ----------
let criteriaSeq = 0;
function addCriterion(kind = "shell", value = "", description = "") {
  const id = ++criteriaSeq;
  const row = document.createElement("div");
  row.className = "crit-row";
  row.dataset.id = id;
  row.innerHTML = `
    <select>
      <option value="shell">shell</option>
      <option value="python">python</option>
      <option value="file_exists">file_exists</option>
      <option value="contains">contains</option>
    </select>
    <input placeholder="command / code / path" />
    <button class="mini danger">✕</button>`;
  row.querySelector("select").value = kind;
  row.querySelector("input").value = value;
  row.querySelector("button").addEventListener("click", () => row.remove());
  $("#criteriaList").appendChild(row);
}
$("#addCriterion").addEventListener("click", () => addCriterion());
addCriterion("shell", "python -m pytest -q", "tests pass");

function collectCriteria() {
  return [...document.querySelectorAll(".crit-row")].map((row) => {
    const kind = row.querySelector("select").value;
    const value = row.querySelector("input").value.trim();
    const crit = { description: `${kind}: ${value}`, kind, value };
    if (kind === "contains") {
      const parts = value.split("::");
      crit.path = parts[0];
      crit.pattern = parts[1] || value;
      crit.value = crit.pattern;
    }
    return crit;
  }).filter((c) => c.value);
}

$("#solveBtn").addEventListener("click", async () => {
  const statement = $("#problemStatement").value.trim();
  if (!statement) return toast("Enter a problem statement");
  const body = {
    statement,
    criteria: collectCriteria(),
    constraints: $("#constraints").value.split(",").map((s) => s.trim()).filter(Boolean),
    force_mock: $("#solveMock").checked,
  };
  $("#solveBtn").disabled = true;
  $("#solveStatus").textContent = "Planning…";
  $("#planDag").innerHTML = "";
  $("#verifyBox").innerHTML = "";
  $("#solveLog").textContent = "";
  try {
    const result = await api("/api/problems", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderSolve(result);
  } catch (e) {
    $("#solveStatus").textContent = "Error: " + e.message;
  } finally {
    $("#solveBtn").disabled = false;
  }
});

function renderSolve(result) {
  const p = result.problem, plan = result.plan;
  $("#solveStatus").innerHTML = result.solved
    ? `<span class="crit-pass">✔ Solved</span> in ${result.iterations} iteration(s) · ${plan.steps.length} steps`
    : `<span class="crit-fail">✘ Not solved</span> · status: ${p.status} · ${result.iterations} iteration(s)`;

  const dag = $("#planDag");
  dag.innerHTML = "";
  plan.steps.forEach((s) => {
    const el = document.createElement("div");
    el.className = "step " + s.status;
    const deps = s.depends_on.length ? ` · after ${s.depends_on.join(", ")}` : "";
    el.innerHTML = `
      <div class="title">${s.title}<span class="badge">${s.kind}</span><span class="badge">${s.status}</span></div>
      <div class="meta">${s.id}${deps}${s.diff_ids.length ? " · diffs: " + s.diff_ids.join(", ") : ""}</div>
      ${s.hypothesis ? `<div class="hyp">hypothesis: ${escapeHtml(s.hypothesis)}</div>` : ""}
      ${s.result ? `<div class="meta">${escapeHtml(s.result.slice(0, 300))}</div>` : ""}`;
    dag.appendChild(el);
  });

  const vb = $("#verifyBox");
  vb.innerHTML = `<h3>Acceptance criteria</h3>`;
  p.criteria.forEach((c) => {
    const ok = c.satisfied;
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<div class="t ${ok ? "crit-pass" : "crit-fail"}">${ok ? "✔" : "✘"} ${escapeHtml(c.description)}</div>
      <pre>${escapeHtml(c.evidence || "")}</pre>`;
    vb.appendChild(el);
  });
  if (!p.criteria.length) vb.innerHTML += `<div class="empty">No criteria were given — the solve cannot be verified.</div>`;

  const log = $("#solveLog");
  log.textContent = plan.log.map((e) => `[${e.type}] ` + JSON.stringify({ ...e, type: undefined }, null, 0)).join("\n");
  loadDiffs();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------- chat ----------
$("#sendBtn").addEventListener("click", sendChat);
$("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });

function pushMsg(cls, text) {
  const el = document.createElement("div");
  el.className = "msg " + cls;
  el.textContent = text;
  $("#messages").appendChild(el);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return el;
}

async function sendChat() {
  const text = $("#chatInput").value.trim();
  if (!text) return;
  $("#chatInput").value = "";
  pushMsg("user", text);
  const mock = $("#chatMock").checked;
  try {
    const res = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, force_mock: mock }),
    });
    (res.tool_calls || []).forEach((tc) => pushMsg("tool", `→ ${tc.name}(${JSON.stringify(tc.arguments)})`));
    pushMsg("agent", res.error ? "error: " + res.error : res.final || "(no response)");
    if (res.diff_ids?.length) { toast(`Created ${res.diff_ids.length} diff(s)`); loadDiffs(); }
  } catch (e) {
    pushMsg("agent", "error: " + e.message);
  }
}

// ---------- diffs ----------
let activeDiff = null;
async function loadDiffs() {
  try {
    const { diffs } = await api("/api/diffs");
    $("#diffCount").textContent = diffs.length;
    const list = $("#diffList");
    list.innerHTML = "";
    if (!diffs.length) { list.innerHTML = `<div class="empty">No diffs yet.</div>`; return; }
    diffs.forEach((d) => {
      const el = document.createElement("div");
      el.className = "diffitem" + (d.id === activeDiff ? " active" : "");
      el.innerHTML = `<div class="t">${escapeHtml(d.title)}</div>
        <div class="m"><span class="stat-add">+${d.total_added}</span> <span class="stat-del">-${d.total_removed}</span> · ${d.status}</div>`;
      el.addEventListener("click", () => openDiff(d.id));
      list.appendChild(el);
    });
  } catch (e) { /* ignore */ }
}

async function openDiff(id) {
  activeDiff = id;
  const d = await api(`/api/diffs/${id}`);
  const view = $("#diffView");
  view.innerHTML = "";
  const head = document.createElement("div");
  head.className = "diffhead";
  head.innerHTML = `<strong>${escapeHtml(d.title)}</strong>
    <span class="stat-add">+${d.total_added}</span><span class="stat-del">-${d.total_removed}</span>
    <span class="badge">${d.status}</span>`;
  const applyBtn = document.createElement("button");
  applyBtn.className = "mini ok"; applyBtn.textContent = "Apply accepted hunks";
  const rollbackBtn = document.createElement("button");
  rollbackBtn.className = "mini danger"; rollbackBtn.textContent = "Rollback";
  head.appendChild(applyBtn); head.appendChild(rollbackBtn);
  view.appendChild(head);

  if (d.reason) {
    const r = document.createElement("div");
    r.className = "hint"; r.textContent = "Why: " + d.reason;
    view.appendChild(r);
  }

  const accepted = new Set();
  d.files.forEach((f) => {
    const block = document.createElement("div");
    block.className = "fileblock";
    const fh = document.createElement("div");
    fh.className = "filehead";
    fh.innerHTML = `<span>${escapeHtml(f.path)}</span>
      <span class="stat-add">+${f.added}</span><span class="stat-del">-${f.removed}</span>
      ${f.is_new_file ? '<span class="badge">new</span>' : ""}${f.is_delete ? '<span class="badge">delete</span>' : ""}`;
    block.appendChild(fh);
    f.hunks.forEach((h) => {
      accepted.add(h.id);
      const hk = document.createElement("div");
      hk.className = "hunk";
      const hh = document.createElement("div");
      hh.className = "hunkhead";
      hh.innerHTML = `<span>${h.header}</span>`;
      const toggle = document.createElement("label");
      toggle.className = "checkbox";
      toggle.innerHTML = `<input type="checkbox" checked /> accept`;
      toggle.querySelector("input").addEventListener("change", (e) => {
        if (e.target.checked) accepted.add(h.id); else accepted.delete(h.id);
        hk.classList.toggle("rejected", !e.target.checked);
      });
      hh.appendChild(toggle);
      hk.appendChild(hh);
      h.lines.forEach((ln) => {
        const el = document.createElement("div");
        el.className = "diffline " + ln.kind;
        const prefix = { add: "+", remove: "-", context: " " }[ln.kind];
        el.textContent = prefix + ln.text;
        hk.appendChild(el);
      });
      block.appendChild(hk);
    });
    view.appendChild(block);
  });

  applyBtn.addEventListener("click", async () => {
    const acceptedHunks = {};
    d.files.forEach((f) => { acceptedHunks[f.path] = [...accepted].filter((hid) => f.hunks.some((h) => h.id === hid)); });
    try {
      await api(`/api/diffs/${id}/apply`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accepted_hunk_ids: acceptedHunks }),
      });
      toast("Diff applied"); loadDiffs(); openDiff(id);
    } catch (e) { toast("Apply failed: " + e.message); }
  });
  rollbackBtn.addEventListener("click", async () => {
    try { await api(`/api/diffs/${id}/rollback`, { method: "POST" }); toast("Rolled back"); loadDiffs(); openDiff(id); }
    catch (e) { toast("Rollback failed: " + e.message); }
  });
}

// ---------- memory ----------
async function loadMemory() {
  const { memory } = await api("/api/memory");
  const list = $("#memoryList"); list.innerHTML = "";
  if (!memory.length) list.innerHTML = `<div class="empty">No notes yet.</div>`;
  memory.forEach((m) => {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<div class="t">${escapeHtml(m.title)}</div>`;
    list.appendChild(el);
  });
  const { skills } = await api("/api/skills");
  const sl = $("#skillList"); sl.innerHTML = "";
  if (!skills.length) sl.innerHTML = `<div class="empty">No skills learned yet.</div>`;
  skills.forEach((s) => {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<div class="t">${escapeHtml(s.name)}</div><pre>${escapeHtml(s.body.slice(0, 800))}</pre>`;
    sl.appendChild(el);
  });
}
$("#memBtn").addEventListener("click", async () => {
  const title = $("#memTitle").value.trim(), body = $("#memBody").value.trim();
  if (!title || !body) return toast("Need a title and body");
  await api("/api/memory", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, body }) });
  $("#memTitle").value = ""; $("#memBody").value = "";
  toast("Saved"); loadMemory();
});

// ---------- init ----------
loadHealth();
loadDiffs();
setInterval(loadHealth, 15000);
