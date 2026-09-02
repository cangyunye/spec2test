"use strict";

// ── 状态 ──────────────────────────────────────────────
const state = { tid: null, running: false, gate: null };

const $ = (id) => document.getElementById(id);
const evBox = $("events");
const sessionList = $("sessions");

// ── 工具 ──────────────────────────────────────────────
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function nl2br(s) { return esc(s).replace(/\n/g, "<br>"); }

function evHTML(cls, html) {
  const div = document.createElement("div");
  div.className = "ev " + cls;
  div.innerHTML = html;
  evBox.appendChild(div);
  evBox.scrollTop = evBox.scrollHeight;
}

function setStage(s) {
  document.querySelectorAll("#timeline span").forEach((el) => {
    el.classList.toggle("active", el.dataset.s === s);
    el.classList.toggle("done", stageOrder(el.dataset.s) < stageOrder(s));
  });
}
function stageOrder(s) {
  return ["clarify", "doc_review", "graph", "graph_review", "search", "code", "test", "review"].indexOf(s);
}

function openGate(gate, payload) {
  state.gate = gate;
  const titles = {
    graph_review: "制图评审：请确认逻辑图与需求对齐",
    human_review: "人工验收：请验收最终产物",
  };
  $("gateTitle").textContent = titles[gate] || "确认";
  $("gateSummary").textContent = payload.summary || JSON.stringify(payload, null, 2);
  $("gateModal").classList.remove("hidden");
  setStage(gate === "graph_review" ? "graph_review" : "review");
}

function closeGate() { $("gateModal").classList.add("hidden"); state.gate = null; }

// ── 事件渲染 ──────────────────────────────────────────
function onEvent(e) {
  switch (e.type) {
    case "stage":
      setStage(e.stage);
      break;
    case "messages":
      e.messages.forEach((m) => evHTML("ai", `<b>AI</b>：${nl2br(m.content)}`));
      break;
    case "question": {
      const items = e.missing || e.questions || [];
      evHTML("q", `<b>需要补充</b>：${esc(items.join("；"))}`);
      break;
    }
    case "artifact":
      renderArtifact(e.kind, e.payload);
      break;
    case "gate":
      openGate(e.gate, e.payload);
      break;
    case "error":
      evHTML("err", `<b>节点错误</b>：${esc(e.error)}`);
      break;
    case "stream_end":
      state.running = false;
      $("btnNew").disabled = false;
      break;
  }
}

function renderArtifact(kind, payload) {
  $("artifactBox").classList.remove("hidden");
  const box = $("artifacts");
  const add = (title, body) => {
    const d = document.createElement("div");
    d.className = "artifact";
    d.innerHTML = `<h3>${title}</h3><pre>${nl2br(body)}</pre>`;
    box.appendChild(d);
    box.scrollTop = box.scrollHeight;
  };
  if (kind === "logic_graph") {
    $("graphBox").classList.remove("hidden");
    renderMermaid(payload.mermaid_source || "");
    add("逻辑图", `graph_id=${payload.graph_id} · ${(payload.nodes || []).length} 节点 / ${(payload.edges || []).length} 边`);
  } else if (kind === "code_context") {
    add("代码检索", `${payload.length} 条结果`);
  } else if (kind === "code_changes") {
    add("代码变更", payload.map((c) => `${c.file_path} [${c.action}]`).join("\n"));
  } else if (kind === "test_report") {
    renderTestTable(payload);
  }
}

async function renderMermaid(src) {
  await new Promise((r) => setTimeout(r, 50)); // 等 mermaid ready
  const el = $("mermaid");
  el.removeAttribute("data-processed");
  el.textContent = src;
  try { await mermaid.run({ nodes: [el] }); } catch (err) {
    evHTML("err", "Mermaid 渲染失败: " + esc(err.message));
  }
}

function renderTestTable(report) {
  $("testBox").classList.remove("hidden");
  const tbody = $("testTable").querySelector("tbody");
  tbody.innerHTML = "";
  const tierBg = { functional: "#e8f5e9", performance: "#e3f2fd", security: "#fce4ec" };
  for (const c of report.test_cases || []) {
    const tr = document.createElement("tr");
    tr.style.background = tierBg[c.tier] || "";
    tr.innerHTML = [
      c.tier, c.priority, c.title, c.target || "", c.precondition || "",
      c.steps || "", c.expected || "", c.rationale || "",
    ].map((x) => `<td>${nl2br(x)}</td>`).join("");
    tbody.appendChild(tr);
  }
  const run = report.run || {};
  evHTML("ok", `测试场景：passed=${run.passed} failed=${run.failed} · ${esc(run.logs || "")}`);
}

// ── API ───────────────────────────────────────────────
async function postSSE(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`${resp.status}: ${t}`);
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        try { onEvent(JSON.parse(line.slice(6))); } catch (err) { /* 忽略坏帧 */ }
      }
    }
  }
}

async function api(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
  return resp.json();
}

// ── 流程 ──────────────────────────────────────────────
function buildSetFields() {
  return [
    `project_root=${$("fRoot").value}`,
    `target_modules=${$("fModules").value}`,
    `edge_cases=${$("fEdges").value}`,
    `acceptance_criteria=${$("fAccept").value}`,
  ];
}

async function startFlow() {
  const text = $("reqText").value.trim();
  if (!text) { alert("请先输入需求文本"); return; }
  if (state.running) return;
  state.running = true;
  $("btnNew").disabled = true;
  evBox.innerHTML = "";
  $("artifacts").innerHTML = "";
  $("testTable").querySelector("tbody").innerHTML = "";
  $("graphBox").classList.add("hidden");
  $("artifactBox").classList.add("hidden");
  $("testBox").classList.add("hidden");
  closeGate();
  try {
    const { thread_id } = await api("/api/sessions", { set_fields: buildSetFields() });
    state.tid = thread_id;
    evHTML("me", `<b>需求</b>：${nl2br(text)}`);
    await postSSE(`/api/sessions/${thread_id}/messages`, { text });
    refreshSessions();
  } catch (err) {
    evHTML("err", "失败: " + esc(err.message));
    state.running = false;
    $("btnNew").disabled = false;
  }
}

async function sendAnswer() {
  const t = $("reqText").value.trim();
  if (!t || !state.tid) return;
  evHTML("me", `<b>补充</b>：${nl2br(t)}`);
  $("reqText").value = "";
  await postSSE(`/api/sessions/${state.tid}/messages`, { text: t });
}

async function refreshSessions() {
  const data = await (await fetch("/api/sessions")).json();
  sessionList.innerHTML = "";
  for (const s of data.slice(0, 20)) {
    const li = document.createElement("li");
    li.innerHTML = `${esc(s.thread_id)} · ${esc(s.stage || "—")}`;
    li.onclick = async () => {
      state.tid = s.thread_id;
      const snap = await (await fetch(`/api/sessions/${s.thread_id}`)).json();
      evBox.innerHTML = "";
      setStage(snap.current_stage || "clarify");
      if (snap.logic_graph && snap.logic_graph.mermaid_source) {
        $("graphBox").classList.remove("hidden");
        await renderMermaid(snap.logic_graph.mermaid_source);
      }
      evHTML("q", `已加载会话 ${esc(s.thread_id)}（阶段 ${esc(snap.current_stage || "—")}），可在下方输入继续。`);
    };
    sessionList.appendChild(li);
  }
}

// ── 事件绑定 ──────────────────────────────────────────
$("btnSample").onclick = () => {
  $("reqText").value = "开发一个带图形界面的 Python 计算器（GUI，使用 tkinter），支持四则运算、连续运算、除零报错、负数与小数的输入，输入非法字符时给出错误提示。";
};
$("btnNew").onclick = startFlow;
$("btnApprove").onclick = async () => {
  if (!state.tid || !state.gate) return;
  await postSSE(`/api/sessions/${state.tid}/gates`, { decision: "approve" });
  refreshSessions();
};
$("btnReject").onclick = async () => {
  if (!state.tid || !state.gate) return;
  await postSSE(`/api/sessions/${state.tid}/gates`, { decision: "reject" });
  refreshSessions();
};
$("reqText").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendAnswer();
});

refreshSessions();
setStage("clarify");