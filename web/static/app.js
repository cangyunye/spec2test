"use strict";
/* ═══════════════════════════════════════════════════════════════
   DevFlow Web Shell · 前端交互
   聊天流 + 步骤条 + 门禁评审 + 产物卡片（逻辑图/测试表/变更/检索）
   ═══════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const S = {
  tid: null, running: false, gate: null, gatePayload: null,
  stage: "clarify", startedAt: 0, lastNodeAt: 0, timer: null,
  graph: null, report: null, health: null, noCode: false, testCardEl: null,
  autoScroll: true, live: new Map(), theme: document.documentElement.dataset.theme || "dark",
  mermaidReady: false,
};

/* 是否提供项目代码：true=代码模式（检索/生成）；false=仅需求模式（直接出端到端用例） */
function reqNoCode(req) {
  req = req || {};
  return !(req.existing_code_accessible || String(req.project_root || "").trim());
}

/* ── 小工具 ──────────────────────────────────────────── */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function h(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function fmtDur(ms) { return (ms / 1000).toFixed(1) + "s"; }
function stageName(s) {
  return ({
    clarify: "需求澄清", doc_review: "文档确认", graph: "逻辑制图", graph_review: "制图评审",
    search: "代码检索", code: "代码生成", test: "测试设计", review: "人工验收", done: "完成",
  })[s] || s;
}
const TIER_NAME = { functional: "功能", performance: "性能", security: "安全" };

async function api(url, opts) {
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    let msg = await resp.text().catch(() => "");
    try { msg = JSON.parse(msg).detail || msg; } catch { }
    throw new Error(msg.slice(0, 300) || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function toast(title, body, kind) {
  const box = $("toasts");
  while (box.children.length >= 3) box.firstChild.remove(); // 最多同时 3 条
  const t = h("div", "toast" + (kind === "err" ? " err" : ""));
  t.appendChild(h("b", null, title));
  if (body) t.appendChild(h("p", null, body));
  t.onclick = () => t.remove();
  box.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 250); }, 4500);
}

/* 错误分类：给用户可懂的标题 + 下一步建议 */
function classifyError(text) {
  const t = String(text || "");
  if (/LLM\.|api[_ ]?key|API key|401|Unauthorized|quota|余额/i.test(t))
    return { title: "LLM 调用失败", adv: "检查 .env 中的 API Key / 额度 / 网络；可运行 `python -m devflow.cli check-providers` 自检。" };
  if (/CLARIFY\.LOOP/i.test(t))
    return { title: "澄清轮次用尽", adv: "需求信息长期不完整已终止流程；建议新建会话并在开头给出更完整的需求。" };
  if (/LINT/i.test(t))
    return { title: "生成代码未通过 Lint", adv: "流程会自动重试修复；若多次失败可驳回图后在需求中补充约束。" };
  if (/CODEGRAPH|index/i.test(t))
    return { title: "代码索引问题", adv: "CODE_SEARCH=codegraph 时需先在项目根目录执行 `codegraph init` 建索引。" };
  if (/graph_review|review/i.test(t))
    return { title: "评审回退", adv: "", info: true };
  return { title: "节点执行出错", adv: "流程已记录错误并停止；可修复后重试，或新建会话。" };
}

/* ── 主题 ────────────────────────────────────────────── */

const MERMAID_VARS = {
  dark: {
    primaryColor: "#1f1f24", primaryTextColor: "#f2f2f0", primaryBorderColor: "#3a3a44",
    lineColor: "#6e6e78", secondaryColor: "#17171b", tertiaryColor: "#17171b",
    mainBkg: "#1f1f24", nodeBorder: "#3a3a44", fontSize: "13px",
  },
  light: {
    primaryColor: "#f1f1ee", primaryTextColor: "#161613", primaryBorderColor: "#b9b7af",
    lineColor: "#8c8c85", secondaryColor: "#ffffff", tertiaryColor: "#ffffff",
    mainBkg: "#f1f1ee", nodeBorder: "#b9b7af", fontSize: "13px",
  },
};

function initMermaid() {
  if (typeof mermaid === "undefined") return;
  mermaid.initialize({
    startOnLoad: false, theme: "base", securityLevel: "loose",
    themeVariables: MERMAID_VARS[S.theme] || MERMAID_VARS.dark,
    flowchart: { curve: "basis", htmlLabels: true },
  });
  S.mermaidReady = true;
}

function toggleTheme() {
  S.theme = S.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = S.theme;
  localStorage.setItem("df-theme", S.theme);
  initMermaid();
  if (S.graph?.mermaid_source) renderGraphInto($("gCanvasBody"), S.graph.mermaid_source, null);
}

/* ── Mermaid 渲染 ────────────────────────────────────── */

async function renderMermaid(el, src) {
  if (!S.mermaidReady) initMermaid();
  if (!S.mermaidReady) throw new Error("mermaid 未加载");
  el.removeAttribute("data-processed");
  el.textContent = src;
  await mermaid.run({ nodes: [el] });
}

/* ── 步骤条 ──────────────────────────────────────────── */

const STEPS = [
  ["clarify", "需求澄清"], ["graph", "逻辑制图"], ["graph_review", "制图评审"],
  ["search", "代码检索"], ["code", "代码生成"], ["test", "测试设计"],
  ["review", "人工验收"], ["done", "完成"],
];
const STAGE_ORDER = { clarify: 0, graph: 1, search: 3, code: 4, test: 5, review: 6, done: 7 };

function buildStepper() {
  const nav = $("stepper");
  nav.innerHTML = "";
  STEPS.forEach(([id, label], i) => {
    const step = h("div", "step"); step.dataset.s = id;
    const dot = h("span", "step-dot mono"); dot.textContent = String(i + 1).padStart(2, "0");
    step.appendChild(dot);
    step.appendChild(h("span", "step-label", label));
    nav.appendChild(step);
  });
}

function setStep(idx, running) {
  // 仅需求模式：代码检索/代码生成两步显示为跳过，不参与 done/active 标记
  const skipped = (id) => S.noCode && (id === "search" || id === "code");
  document.querySelectorAll("#stepper .step").forEach((el, i) => {
    if (skipped(el.dataset.s)) {
      el.classList.add("skip");
      el.classList.remove("done", "active", "running");
      return;
    }
    el.classList.remove("skip");
    el.classList.toggle("done", i < idx || (idx === 7 && i === 7));
    el.classList.toggle("active", i === idx && idx !== 7);
    el.classList.toggle("running", !!(running && i === idx));
  });
}
function stepIdxForStage(stage) {
  if (stage === "graph_review") return 2;
  if (stage === "human_review") return 6;  // 门禁 id（openGate/submitGate 直接传入），漏映射会被兜底回 0
  if (stage in STAGE_ORDER) return STAGE_ORDER[stage];
  return 0;
}

/* ── 动态流 ──────────────────────────────────────────── */

function feedInner() {
  let inner = $("feed").querySelector(".feed-inner");
  if (!inner) {
    inner = h("div", "feed-inner");
    $("feed").appendChild(inner);
  }
  $("feedEmpty")?.remove();
  return inner;
}
function feedAppend(el) {
  feedInner().appendChild(el);
  if (S.autoScroll) $("feed").scrollTop = $("feed").scrollHeight;
}
$("feed")?.addEventListener("scroll", () => {
  const f = $("feed");
  S.autoScroll = f.scrollHeight - f.scrollTop - f.clientHeight < 90;
});

function addUserMsg(text) {
  const m = h("div", "msg me");
  m.appendChild(h("span", "who", "YOU"));
  const b = h("div", "bubble", text);
  m.appendChild(b);
  feedAppend(m);
}
function addAiMsg(text, label) {
  const m = h("div", "msg ai");
  m.appendChild(h("span", "who", label ? `AI · ${label}` : "AI"));
  m.appendChild(h("div", "bubble", text));
  feedAppend(m);
}
function addDivider(label, sub, info) {
  const d = h("div", "divider" + (info ? " info" : ""));
  const span = h("span", "mono", label + (sub ? `  ·  ${sub}` : ""));
  d.appendChild(span);
  feedAppend(d);
  return d;
}
function addAsk(items) {
  const card = h("div", "ask");
  card.appendChild(h("div", "who", "NEED INPUT · 需要补充"));
  const ul = h("ul");
  items.forEach((q) => ul.appendChild(h("li", null, q)));
  card.appendChild(ul);
  feedAppend(card);
}
/* 澄清方式选择卡：普通澄清首轮追问后出现一次，点按钮即以该指令继续会话 */
function addModeChoice() {
  const card = h("div", "mode-choice");
  card.appendChild(h("div", "mc-title", "换一种澄清方式？（也可以不选，直接在下方输入补充内容）"));
  const btns = h("div", "mc-btns");
  const pick = (label, text) => {
    const b = h("button", "mc-btn", label);
    b.onclick = () => {
      if (S.running || S.gate) return;
      card.classList.add("picked");
      btns.querySelectorAll(".mc-btn").forEach((x) => { x.disabled = true; });
      addUserMsg(text);
      setRunning(true);
      startStream({ op: "message", text });
    };
    return b;
  };
  btns.appendChild(pick("🧠 头脑风暴 · 逐条探讨", "头脑风暴"));
  btns.appendChild(pick("🔥 拷问 · 逐题深挖", "拷问"));
  card.appendChild(btns);
  feedAppend(card);
}
function addError(text) {
  const c = classifyError(text);
  const card = h("div", "err-card");
  card.appendChild(h("div", "err-title", c.title));
  if (c.adv) card.appendChild(h("div", "err-adv", c.adv));
  card.appendChild(h("div", "err-detail", text));
  feedAppend(card);
}

/* LLM 实时输出气泡（token 流） */
function liveEnsure(node, label) {
  if (S.live.has(node)) return S.live.get(node);
  const show = /clarify/.test(node); // 制图等节点输出为 JSON，默认折叠
  const box = h("div", "live");
  const head = h("div", "live-head");
  const caret = h("span", "caret");
  caret.appendChild(h("i")); caret.appendChild(h("i")); caret.appendChild(h("i"));
  head.appendChild(caret);
  head.appendChild(h("span", null, `AI · ${label || node} 正在输出`));
  head.appendChild(h("span", "live-count mono", "0"));
  box.appendChild(head);
  const body = h("div", "live-body" + (show ? "" : " raw"));
  if (!show) body.style.display = "none";
  box.appendChild(body);
  head.onclick = () => {
    const vis = body.style.display !== "none";
    body.style.display = vis ? "none" : "block";
  };
  feedAppend(box);
  const rec = { el: box, body, count: head.querySelector(".live-count"), text: "", show };
  S.live.set(node, rec);
  return rec;
}
function liveAppend(node, label, chunk) {
  const rec = liveEnsure(node, label);
  rec.text += chunk;
  rec.body.textContent = rec.text;
  rec.count.textContent = rec.text.length + " 字";
  if (S.autoScroll) $("feed").scrollTop = $("feed").scrollHeight;
}
let pendingLive = null; // token 流的暂存文本：canonical messages 未到时才落盘
function liveFinalize(node) {
  const rec = S.live.get(node);
  if (!rec) return;
  if (rec.show && rec.text.trim()) pendingLive = rec.text.trim();
  rec.el.remove();
  S.live.delete(node);
}
function flushPendingLive() {
  if (pendingLive) { addAiMsg(pendingLive, "实时"); pendingLive = null; }
}
function liveClearAll() {
  S.live.forEach((r) => r.el.remove());
  S.live.clear();
}

/* ── 产物卡片 ────────────────────────────────────────── */

function cardShell(title) {
  const card = h("div", "card");
  const head = h("div", "card-head");
  head.appendChild(h("span", "card-title", title));
  const sp = h("span", "spacer");
  head.appendChild(sp);
  const actions = h("div", "card-actions");
  head.appendChild(actions);
  card.appendChild(head);
  return { card, actions };
}
function miniBtn(label, onClick, title) {
  const b = h("button", "mini-btn", label);
  if (title) b.title = title;
  b.onclick = onClick;
  return b;
}

function download(name, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* 逻辑图卡片：缩放 / 平移 / 节点检查器 */
let gState = { s: 1, tx: 0, ty: 0 };
function gApply(canvas, zoomTag) {
  canvas.style.transform = `translate(${gState.tx}px, ${gState.ty}px) scale(${gState.s})`;
  if (zoomTag) zoomTag.textContent = Math.round(gState.s * 100) + "%";
}

function renderGraphCard(graph) {
  S.graph = graph;
  const gLabel = GRAPH_TYPE_LABELS[graph.graph_type] || "流程图";
  const { card, actions } = cardShell(
    `LOGIC GRAPH · 逻辑图${graph.graph_type && graph.graph_type !== "flowchart" ? " · " + gLabel : ""}`);
  actions.appendChild(miniBtn("−", () => { gState.s = Math.max(.3, gState.s - .15); gApply($("gCanvas"), $("gZoom")); }, "缩小"));
  actions.appendChild(miniBtn("＋", () => { gState.s = Math.min(3, gState.s + .15); gApply($("gCanvas"), $("gZoom")); }, "放大"));
  actions.appendChild(miniBtn("1:1", () => { gState = { s: 1, tx: 0, ty: 0 }; gApply($("gCanvas"), $("gZoom")); }, "重置视图"));
  actions.appendChild(miniBtn("⧉ 复制源码", () => copyText(graph.mermaid_source, "Mermaid 源码已复制")));
  actions.appendChild(miniBtn("⭳ .mmd", () => download(`logic-${graph.graph_id || S.tid}.mmd`, graph.mermaid_source)));

  const vp = h("div", "graph-vp");
  const canvas = h("div", "graph-canvas"); canvas.id = "gCanvas";
  const mEl = h("div", "mermaid"); mEl.id = "gCanvasBody";
  canvas.appendChild(mEl);
  vp.appendChild(canvas);
  const zoomTag = h("span", "zoom-tag mono", "100%"); zoomTag.id = "gZoom";
  vp.appendChild(zoomTag);
  card.appendChild(vp);

  // 平移 / 缩放
  let drag = null;
  vp.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, tx: gState.tx, ty: gState.ty, moved: false };
    vp.setPointerCapture(e.pointerId);
  });
  vp.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    gState.tx = drag.tx + dx; gState.ty = drag.ty + dy;
    gApply(canvas, zoomTag);
  });
  vp.addEventListener("pointerup", () => { setTimeout(() => { drag = null; }, 0); });
  vp.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = vp.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const old = gState.s;
    gState.s = Math.min(3, Math.max(.3, gState.s * (e.deltaY < 0 ? 1.12 : 0.89)));
    const k = gState.s / old;
    gState.tx = mx - (mx - gState.tx) * k;
    gState.ty = my - (my - gState.ty) * k;
    gApply(canvas, zoomTag);
  }, { passive: false });

  // 节点点击检查器
  const inspect = h("div", "g-inspect hidden");
  mEl.addEventListener("click", (e) => {
    if (drag?.moved) return;
    const g = e.target.closest("g.node");
    const nodes = graph.nodes || [];
    const nid = g?.id || g?.dataset?.id;
    const node = nid && nodes.find((n) => (n.node_id || "") === nid.replace(/^flowchart-/, ""));
    if (!node) { inspect.classList.add("hidden"); return; }
    inspect.innerHTML = "";
    inspect.classList.remove("hidden");
    const row = (k, v, mono) => {
      const d = h("div");
      d.appendChild(h("span", "k", k));
      const vv = h("span", "v" + (mono ? " mono" : ""), v || "—");
      d.appendChild(vv);
      return d;
    };
    inspect.appendChild(row("NODE", node.node_id, true));
    inspect.appendChild(row("名称", node.label));
    inspect.appendChild(row("类型", node.node_type, true));
    const cm = h("div");
    cm.appendChild(h("span", "k", "改动"));
    cm.appendChild(h("span", "chip-mod" + (node.is_modified ? " yes" : ""), node.is_modified ? "本次修改" : "复用"));
    inspect.appendChild(cm);
    const cr = node.code_ref;
    if (cr && (cr.file_path || typeof cr === "string")) {
      const ref = typeof cr === "string" ? cr : `${cr.file_path}${cr.line_start ? `:${cr.line_start}-${cr.line_end}` : ""}${cr.symbol ? ` · ${cr.symbol}` : ""}`;
      inspect.appendChild(row("代码", ref, true));
    }
  });
  card.appendChild(inspect);

  feedAppend(card);
  gState = { s: 1, tx: 0, ty: 0 };
  renderGraphInto(mEl, graph.mermaid_source, zoomTag);
  return card;
}

async function renderGraphInto(el, src, zoomTag) {
  try {
    await renderMermaid(el, src);
    gApply($("gCanvas") || el.parentElement, zoomTag);
  } catch (err) {
    const box = el.closest(".card, .gate-panel") || el.parentElement;
    const fb = h("div", "mmd-fallback");
    fb.appendChild(h("div", "err-adv", "Mermaid 渲染失败（语法问题），已回退为源码视图"));
    const pre = h("pre", null, src);
    fb.appendChild(pre);
    const wrap = el.parentElement;
    wrap.replaceChildren(fb);
  }
}

/* 代码检索卡片 */
function renderContextCard(items) {
  const { card } = cardShell(`CODE CONTEXT · 代码检索 · ${items.length}`);
  const body = h("div", "card-body");
  const list = h("div", "ctx-list");
  items.forEach((it) => {
    const file = it.file_path || it.path || it.source || "未知文件";
    const item = h("div", "ctx-item");
    const head = h("div", "ctx-file");
    head.appendChild(h("span", null, "▸ " + file));
    if (it.symbol) head.appendChild(h("span", "chip-mod", String(it.symbol)));
    if (it.score !== undefined) head.appendChild(h("span", "mono", ` score ${Number(it.score).toFixed(2)}`));
    head.appendChild(h("span", "arrow", "▶"));
    const pre = h("pre", "ctx-snippet", it.snippet || it.code || it.content || JSON.stringify(it, null, 2));
    head.onclick = () => item.classList.toggle("open");
    item.appendChild(head);
    item.appendChild(pre);
    list.appendChild(item);
  });
  body.appendChild(list);
  card.appendChild(body);
  feedAppend(card);
}

/* 代码变更卡片 */
function renderChangesCard(changes) {
  const { card } = cardShell(`CODE CHANGES · 代码变更 · ${changes.length}`);
  const body = h("div", "card-body");
  changes.forEach((ch) => {
    const box = h("div", "diff-file");
    const head = h("div", "diff-head");
    head.appendChild(h("span", null, ch.file_path || "?"));
    head.appendChild(h("span", "act", ch.action || "update"));
    if (ch.lint_passed !== undefined && ch.lint_passed !== null)
      head.appendChild(h("span", "act", ch.lint_passed ? "lint ✓" : "lint ✗"));
    box.appendChild(head);
    const diff = String(ch.diff || "（无 diff 内容）");
    const pre = h("pre", "diff-pre");
    diff.split("\n").forEach((line) => {
      const cls = line.startsWith("+") && !line.startsWith("+++") ? "add"
        : line.startsWith("-") && !line.startsWith("---") ? "del" : null;
      const span = h("span", cls, line || " ");
      pre.appendChild(span);
      pre.appendChild(document.createTextNode("\n"));
    });
    box.appendChild(pre);
    body.appendChild(box);
  });
  card.appendChild(body);
  feedAppend(card);
}

/* 测试场景卡片：筛选 + 导出 */
const tFilter = { tier: "all", prio: "all", q: "" };

/* 兼容两种 case 形态：LLM 分级场景 / 代码级测试（mock provider） */
function normCase(c) {
  if (c.title) {
    return {
      case_id: c.case_id || "",
      tier: c.tier || "functional",
      priority: c.priority || "P2",
      case_type: c.case_type || "",
      title: c.title,
      target: c.target || "",
      precondition: c.precondition || "",
      steps: c.steps || "",
      expected: c.expected || "",
      data_requirement: c.data_requirement || "",
      rationale: c.rationale || "",
    };
  }
  return {
    case_id: c.case_id || "",
    tier: c.tier || "functional",
    priority: c.priority || "P2",
    case_type: c.case_type || "",
    title: c.test_symbol || c.symbol || "(未命名测试)",
    target: c.test_file || c.target || "",
    precondition: c.precondition || "",
    steps: c.code_snippet || c.steps || "",
    expected: c.expected || "",
    data_requirement: "",
    rationale: c.covered_edges ? `覆盖边：${c.covered_edges.join("、")}` : (c.rationale || ""),
  };
}

function renderTestCard(report) {
  S.report = report;
  const cases = (report.test_cases || []).map(normCase);
  const { card, actions } = cardShell(`TEST DESIGN · 测试场景 · ${cases.length}`);

  // 测试概述（总-分结构的总文档；方法论来自 doc-based/functional testcase-generator skills）
  if (report.overview || (report.self_check || []).length) {
    const ov = h("div", "card-note t-overview");
    if (report.overview) ov.appendChild(h("div", null, "📋 " + report.overview));
    (report.self_check || []).forEach((s) => ov.appendChild(h("div", null, "✓ " + s)));
    card.appendChild(ov);
  }

  // 过滤器
  const filters = h("div", "t-filters");
  const tierChip = (key, label) => {
    const b = miniBtn(label, () => {
      tFilter.tier = key;
      filters.querySelectorAll(".mini-btn").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      renderRows();
    }, `筛选：${label}`);
    if (key === "all") b.classList.add("on");
    return b;
  };
  filters.appendChild(tierChip("all", "全部"));
  filters.appendChild(tierChip("functional", "功能"));
  filters.appendChild(tierChip("performance", "性能"));
  filters.appendChild(tierChip("security", "安全"));
  const sel = document.createElement("select");
  sel.className = "mini-btn";
  sel.style.appearance = "auto";
  [["all", "优先级"], ["P0", "P0"], ["P1", "P1"], ["P2", "P2"], ["P3", "P3"]].forEach(([v, t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t; sel.appendChild(o);
  });
  sel.onchange = () => { tFilter.prio = sel.value; renderRows(); };
  filters.appendChild(sel);
  const search = h("input", "t-search");
  search.placeholder = "搜索标题 / 步骤…";
  search.oninput = () => { tFilter.q = search.value.trim().toLowerCase(); renderRows(); };
  filters.appendChild(search);
  card.appendChild(filters);

  // 表格
  const wrap = h("div", "t-wrap");
  const table = h("table", "t-table");
  table.appendChild(h("thead", null, "")).innerHTML =
    "<tr><th>标识</th><th>层级</th><th>优先级</th><th>类型</th><th>标题</th><th>所属模块</th><th>前置</th><th>步骤</th><th>预期</th><th>依据</th></tr>";
  const tbody = h("tbody");
  table.appendChild(tbody);
  wrap.appendChild(table);
  card.appendChild(wrap);

  const count = h("div", "card-note", "");
  card.appendChild(count);

  function pass(c) {
    if (tFilter.tier !== "all" && c.tier !== tFilter.tier) return false;
    if (tFilter.prio !== "all" && String(c.priority).toUpperCase() !== tFilter.prio) return false;
    if (tFilter.q) {
      const blob = [c.case_id, c.case_type, c.title, c.target, c.steps, c.expected, c.precondition].join(" ").toLowerCase();
      if (!blob.includes(tFilter.q)) return false;
    }
    return true;
  }
  function renderRows() {
    tbody.innerHTML = "";
    const shown = cases.filter(pass);
    if (!shown.length) {
      tbody.appendChild(h("tr", null, "")).appendChild(
        Object.assign(document.createElement("td"), { colSpan: 10, className: "t-empty", textContent: "没有匹配的测试场景" }));
      count.textContent = "0 条";
      return;
    }
    shown.forEach((c) => {
      const tr = h("tr");
      tr.appendChild(h("td", "mono", c.case_id || "—"));
      const tdTier = h("td");
      tdTier.appendChild(h("span", `tier ${c.tier || ""}`, TIER_NAME[c.tier] || c.tier || "—"));
      tr.appendChild(tdTier);
      const tdP = h("td");
      tdP.appendChild(h("span", `prio ${String(c.priority).toUpperCase()}`, String(c.priority || "—").toUpperCase()));
      tr.appendChild(tdP);
      tr.appendChild(h("td", null, c.case_type || "—"));
      tr.appendChild(h("td", "t-title", c.title || "—"));
      [c.target, c.precondition, c.steps, c.expected, c.rationale].forEach((v) =>
        tr.appendChild(h("td", null, v || "—")));
      tbody.appendChild(tr);
    });
    count.textContent = `显示 ${shown.length} / ${cases.length} 条 · 通过 ${report.run?.passed ?? "—"} · 失败 ${report.run?.failed ?? "—"}`;
  }
  renderRows();

  // 导出
  actions.appendChild(miniBtn("⭳ CSV", () => exportTestCSV(cases), "导出 CSV（Excel 可开）"));
  actions.appendChild(miniBtn("⭳ MD", () => exportTestMD(cases, report), "导出 Markdown（总-分结构）"));
  actions.appendChild(miniBtn("⧉ 复制", () => copyText(testToMD(cases, report), "测试用例文档已复制")));

  // test_run 会把同一份报告再推一次（回填执行统计）：已有卡片就原地替换，不重复插卡
  if (S.testCardEl && S.testCardEl.isConnected) S.testCardEl.replaceWith(card);
  else feedAppend(card);
  S.testCardEl = card;
  renderExportBar();
  return card;
}

function testToCSV(cases) {
  const head = ["标识", "层级", "优先级", "类型", "标题", "所属模块", "前置条件", "步骤", "预期结果", "数据要求", "设计依据"];
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = cases.map((c) =>
    [c.case_id, TIER_NAME[c.tier] || c.tier, c.priority, c.case_type, c.title, c.target,
     c.precondition, c.steps, c.expected, c.data_requirement, c.rationale].map(q).join(","));
  return "\uFEFF" + [head.map(q).join(","), ...rows].join("\r\n");
}

/* 总-分结构 Markdown（方法论来自 doc-based/functional testcase-generator skills）：
   概述（INDEX：范围/统计/口径）→ 分模块用例清单 → 质量自检结论 */
function testToMD(cases, report) {
  report = report || {};
  const lines = ["# 测试用例文档", ""];

  // ── 总文档：概述 + 公共口径 ──
  lines.push("## 1. 概述（INDEX）", "");
  if (report.overview) lines.push(report.overview, "");
  const byTier = {};
  cases.forEach((c) => { const t = TIER_NAME[c.tier] || c.tier || "其他"; byTier[t] = (byTier[t] || 0) + 1; });
  const tierStat = Object.entries(byTier).map(([t, n]) => `${t} ${n}`).join(" · ");
  const p0 = cases.filter((c) => String(c.priority).toUpperCase() === "P0").length;
  lines.push(
    `- 用例总数：${cases.length}（${tierStat || "—"}；P0 ${p0} 条）`,
    "- 优先级口径：P0 核心路径与关键校验 · P1 边界与重要异常 · P2 次要异常与体验",
    "- 类型口径：正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法 / 性能 / 安全",
    "",
  );

  // ── 分文档：按所属模块分组的用例清单 ──
  const groups = new Map();
  cases.forEach((c) => {
    const mod = c.target || "通用";
    if (!groups.has(mod)) groups.set(mod, []);
    groups.get(mod).push(c);
  });
  lines.push("## 2. 分模块用例", "");
  const cell = (v) => String(v ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");
  let gi = 0;
  groups.forEach((modCases, mod) => {
    gi += 1;
    lines.push(`### 2.${gi} ${mod}`, "",
      "| 标识 | 层级 | 优先级 | 类型 | 标题 | 前置 | 步骤 | 预期 | 依据 |",
      "|---|---|---|---|---|---|---|---|---|");
    modCases.forEach((c) => lines.push(
      "| " + [c.case_id, TIER_NAME[c.tier] || c.tier, c.priority, c.case_type, c.title,
       c.precondition, c.steps, c.expected, c.rationale].map(cell).join(" | ") + " |"));
    lines.push("");
  });

  // ── 质量自检 ──
  const checks = report.self_check || [];
  if (checks.length) {
    lines.push("## 3. 质量自检", "");
    checks.forEach((s) => lines.push(`- ${s}`));
    lines.push("");
  }
  return lines.join("\n");
}
function exportTestCSV(cases) { download(`devflow-tests-${S.tid || "export"}.csv`, testToCSV(cases), "text/csv;charset=utf-8"); toast("已导出 CSV", `${cases.length} 条测试场景`); }
function exportTestMD(cases, report) { download(`devflow-tests-${S.tid || "export"}.md`, testToMD(cases, report)); toast("已导出 Markdown", `${cases.length} 条测试场景`); }

function copyText(text, okMsg) {
  const done = () => toast("已复制", okMsg);
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("复制失败", "请手动选择复制", "err"); }
  ta.remove();
}

/* 全量导出条 */
function renderExportBar() {
  if (document.querySelector(".exportbar") || !S.tid) return;
  const { card } = cardShell("EXPORT · 导出交付物");
  const bar = h("div", "exportbar");
  bar.appendChild(h("span", "t", "把本会话全部产物（需求 / 逻辑图 / 变更 / 测试场景）打包带走："));
  bar.appendChild(miniBtn("⭳ Markdown 汇总", async () => {
    const resp = await fetch(`/api/sessions/${S.tid}/export?format=md`);
    download(`devflow-${S.tid}.md`, await resp.text(), "text/markdown;charset=utf-8");
    toast("已导出", "Markdown 汇总");
  }));
  bar.appendChild(miniBtn("⭳ JSON", async () => {
    const resp = await fetch(`/api/sessions/${S.tid}/export?format=json`);
    download(`devflow-${S.tid}.json`, JSON.stringify(await resp.json(), null, 2), "application/json");
    toast("已导出", "结构化 JSON");
  }));
  if (S.report) bar.appendChild(miniBtn("⭳ 测试 CSV", () => exportTestCSV((S.report.test_cases || []).map(normCase))));
  card.appendChild(bar);
  feedAppend(card);
}

/* ── SSE ─────────────────────────────────────────────── */

let es = null, sawEnd = false;

function startStream(params) {
  stopStream();
  sawEnd = false;
  const qs = new URLSearchParams(params).toString();
  es = new EventSource(`/api/sessions/${S.tid}/stream?${qs}`);
  es.onmessage = (ev) => {
    let e; try { e = JSON.parse(ev.data); } catch { return; }
    if (e.type === "stream_end") { stopStream(); onStreamEnd(e); return; }
    onEvent(e);
  };
  es.onerror = () => {
    stopStream();
    if (!sawEnd) {
      setRunning(false);
      toast("连接中断", "与服务的连接断开，最后一步可能未完成；可重发上一条消息继续。", "err");
      updateComposer();
    }
  };
}
function stopStream() { if (es) { es.close(); es = null; } }

function setRunning(on) {
  S.running = on;
  $("flowbar").hidden = !on;
  if (on) {
    S.startedAt = Date.now(); S.lastNodeAt = Date.now();
    clearInterval(S.timer);
    S.timer = setInterval(() => {
      $("composerTimer").textContent = fmtDur(Date.now() - S.startedAt);
    }, 200);
  } else {
    clearInterval(S.timer);
    $("composerTimer").textContent = "";
  }
  updateComposer();
}

function onEvent(e) {
  if (e.type !== "messages") flushPendingLive();
  switch (e.type) {
    case "stage": {
      const prev = S.stage;
      S.stage = e.stage;
      if (e.stage !== prev && e.stage !== "done") {
        setStep(stepIdxForStage(e.stage), true);
        addDivider(`进入 ${stageName(e.stage)}`, null, true);
      }
      if (e.stage === "done" && !S.gate) setStep(7, true);
      updateComposer();
      break;
    }
    case "node_done": {
      const now = Date.now();
      const dur = fmtDur(now - S.lastNodeAt);
      S.lastNodeAt = now;
      liveFinalize(e.node);
      if (e.node !== "compress_messages") {
        addDivider(`${e.label} ✓`, dur);
      }
      break;
    }
    case "token":
      if (e.content) liveAppend(e.node || "ai", e.label, e.content);
      break;
    case "messages":
      liveClearAll();
      pendingLive = null; // canonical 内容已到，丢弃 token 暂存避免重复
      (e.messages || []).forEach((m) => {
        const c = String(m.content || "").trim();
        if (!c) return;
        if (m.type === "human") addUserMsg(c);
        else addAiMsg(c);
      });
      break;
    case "question":
      addAsk(e.missing || e.questions || []);
      break;
    case "mode_choice":
      addModeChoice();
      break;
    case "artifact":
      liveClearAll();
      renderArtifact(e.kind, e.payload);
      break;
    case "gate":
      liveClearAll();
      openGate(e.gate, e.payload);
      break;
    case "error": {
      const c = classifyError(e.error);
      if (c.info) addDivider("评审驳回 · 自动回退", null, true);
      else { addError(e.error); toast(c.title, c.adv || e.error.slice(0, 120), "err"); }
      break;
    }
  }
}

function renderArtifact(kind, payload) {
  if (kind === "logic_graph") renderGraphCard(payload);
  else if (kind === "code_context") renderContextCard(Array.isArray(payload) ? payload : []);
  else if (kind === "code_changes") renderChangesCard(Array.isArray(payload) ? payload : []);
  else if (kind === "test_report") renderTestCard(payload);
}

function onStreamEnd(e) {
  sawEnd = true;
  setRunning(false);
  liveClearAll();
  flushPendingLive();
  refreshSessions();
  if (S.gate) {
    const gateStage = S.gate === "graph_review" ? "graph_review"
      : S.gate === "graph_type_select" ? "graph" : "review";
    setStep(stepIdxForStage(gateStage), false);
    updateComposer();
  } else if (S.stage === "done") {
    setStep(7, false);
    addDivider("全流程完成 ✦", "产物可导出");
    toast("全流程完成",
      S.noCode ? "逻辑图与端到端测试用例已就绪，可在导出条打包带走。" : "逻辑图、代码与测试场景已就绪，可在导出条打包带走。");
    updateComposer();
  } else {
    updateComposer();
  }
}

/* ── 门禁 ────────────────────────────────────────────── */

const GATE_META = {
  graph_type_select: { n: 0, title: "制图前 · 选择逻辑图种类" },
  graph_review: { n: 1, title: "制图评审：逻辑图与需求对齐了吗？" },
  human_review: { n: 2, title: "人工验收：产物达到验收标准了吗？" },
};

const GRAPH_TYPE_ICONS = { flowchart: "⎯>", sequence: "⇄", state: "◉", er: "▤" };
const GRAPH_TYPE_LABELS = { flowchart: "流程图", sequence: "时序图", state: "状态图", er: "ER 图" };

function gateSubText(gate) {
  if (gate === "graph_type_select") {
    return "选择将决定制图视角与结构化产物形态 · 选定后本次需求内不再重复询问";
  }
  if (gate === "graph_review") {
    return S.noCode
      ? "未提供项目代码 · APPROVE 将直接进入端到端测试用例设计"
      : "REJECT 将回传意见并自动重制图";
  }
  return S.noCode
    ? "REJECT 将回传意见并重新设计测试用例"
    : "REJECT 将回传意见并回到代码生成";
}

function openGate(gate, payload) {
  S.gate = gate;
  S.gatePayload = payload || {};
  if (S.gatePayload.mode) S.noCode = S.gatePayload.mode === "no_code";
  const meta = GATE_META[gate] || { n: "?", title: "确认" };
  $("gateTag").textContent = meta.n ? `GATE ${meta.n}/2` : "GATE · 制图选项";
  $("gateTitle").textContent = meta.title;
  $("gateSub").textContent = gateSubText(gate);
  $("gateComment").value = "";
  $("gateCommentWrap").classList.add("hidden");
  $("btnGateSubmit").classList.add("hidden");
  $("btnGatePeek").classList.add("hidden");

  const body = $("gateBody");
  body.innerHTML = "";
  if (gate === "graph_type_select") {
    $("btnReject").classList.add("hidden");
    $("btnApprove").classList.add("hidden");
    body.appendChild(gateTypeBody(S.gatePayload));
  } else {
    $("btnReject").classList.remove("hidden");
    $("btnApprove").classList.remove("hidden");
    $("btnGatePeek").classList.remove("hidden");
    if (gate === "graph_review") body.appendChild(gateGraphBody(S.gatePayload));
    else body.appendChild(gateReviewBody(S.gatePayload));
  }

  $("gateModal").classList.remove("hidden");
  $("gatePill").classList.add("hidden");
  setStep(stepIdxForStage(gate === "graph_type_select" ? "graph" : gate), false);
  updateComposer();
}

/* 图种类选择卡：候选 + 推荐/推断标记，点卡片即选定并继续制图 */
function gateTypeBody(p) {
  const wrap = h("div", "gt-list");
  const req = p.requirement_context || "";
  if (req) {
    const ctx = h("div", "gt-ctx", `需求背景：${req.slice(0, 80)}${req.length > 80 ? "…" : ""}`);
    wrap.appendChild(ctx);
  }
  (p.candidates || []).forEach((c) => {
    const b = h("button", "gt-card" + (c.recommended ? " recommended" : ""));
    const head = h("div", "gt-head");
    head.appendChild(h("span", "gt-icon mono", GRAPH_TYPE_ICONS[c.id] || "▪"));
    head.appendChild(h("span", "gt-label", c.label || c.id));
    if (c.recommended) {
      head.appendChild(h("span", "gt-badge", c.id === "flowchart" ? "默认" : "推荐"));
    }
    b.appendChild(head);
    b.appendChild(h("div", "gt-desc", c.desc || ""));
    if (c.reason && c.id !== "flowchart") {
      b.appendChild(h("div", "gt-reason mono", "↳ " + c.reason));
    }
    b.onclick = () => pickGraphType(c.id, c.label || c.id);
    wrap.appendChild(b);
  });
  return wrap;
}

function pickGraphType(typeId, label) {
  if (!S.gate || S.running) return;
  $("gateModal").classList.add("hidden");
  $("gatePill").classList.add("hidden");
  S.gate = null;
  addDivider(`已选图种类 · ${label}`, typeId, true);
  setStep(1, true);
  setRunning(true);
  startStream({ op: "gate", decision: typeId, comment: "" });
  toast("图种类已选定", `开始按「${label}」制图`);
}

function gateGraphBody(p) {
  const grid = h("div", "g-grid");
  // 左：需求对照
  const left = h("div", "g-req");
  left.appendChild(h("h4", null, "需求对照"));
  const dl = h("div", "g-dl");
  const row = (k, vals) => {
    const d = h("div");
    d.appendChild(h("dt", null, k));
    const dd = h("dd");
    (vals.length ? vals : ["—"]).forEach((v) => dd.appendChild(h("span", "li", v)));
    d.appendChild(dd);
    return d;
  };
  const req = p.requirement || {};
  dl.appendChild(row("项目背景", req.project_context ? [req.project_context] : []));
  dl.appendChild(row("目标模块", req.target_modules || []));
  dl.appendChild(row("边界场景", req.edge_cases || []));
  dl.appendChild(row("验收标准", req.acceptance_criteria || []));
  dl.appendChild(row("图规模", [`${p.node_count ?? "?"} 节点 · ${p.edge_count ?? "?"} 边 · 改动 ${p.modified_count ?? "?"}`]));
  left.appendChild(dl);
  grid.appendChild(left);
  // 右：图预览
  const right = h("div", "g-sec");
  right.appendChild(h("h4", null, "逻辑图预览"));
  const prev = h("div", "g-preview");
  const canvas = h("div", "graph-canvas");
  const mEl = h("div", "mermaid");
  canvas.appendChild(mEl);
  prev.appendChild(canvas);
  prev.appendChild(h("span", "g-preview-tip", "点击节点可查看详情（主视图）"));
  right.appendChild(prev);
  grid.appendChild(right);
  setTimeout(() => renderGraphInto(mEl, p.mermaid_source || p.summary || "", null), 30);

  // 预览里也可点节点看信息
  mEl.addEventListener("click", (e) => {
    const g = e.target.closest("g.node");
    if (!g) return;
    const node = (p.nodes || []).find((n) => n.node_id === g.id);
    if (node) toast(node.label, `${node.node_type || ""} · ${node.is_modified ? "本次修改" : "复用"}${node.code_ref?.file_path ? " · " + node.code_ref.file_path : ""}`);
  });
  return grid;
}

function gateReviewBody(p) {
  const wrap = h("div");
  const ts = p.test_summary || {};
  const stats = h("div", "g-stats");
  const stat = (label, val, bad) => {
    const d = h("div", "g-stat" + (bad ? " bad" : ""));
    d.appendChild(h("b", null, String(val)));
    d.appendChild(h("span", null, label));
    return d;
  };
  stats.appendChild(stat("变更文件", p.code_changes_count ?? (p.code_changes || []).length));
  stats.appendChild(stat("测试通过", ts.passed ?? "—"));
  stats.appendChild(stat("测试失败", ts.failed ?? "—", Number(ts.failed) > 0));
  stats.appendChild(stat("覆盖率", ts.coverage_pct !== undefined ? ts.coverage_pct + "%" : "—"));
  stats.appendChild(stat("测试场景", ts.case_count ?? "—"));
  // 执行闭环：测试数字是否来自真实 pytest（仅需求模式下不执行属预期，不标红）
  stats.appendChild(
    stat(
      "测试执行",
      ts.executed ? `pytest ✓ ${ts.duration_sec ?? 0}s` : `未执行${ts.skip_reason ? " · " + ts.skip_reason : ""}`,
      ts.executed ? Number(ts.failed) > 0 || Number(ts.failure_count) > 0 : !S.noCode,
    ),
  );
  wrap.appendChild(stats);

  const changes = p.code_changes || [];
  if (changes.length) {
    const sec = h("div", "g-sec");
    sec.appendChild(h("h4", null, "代码变更"));
    const list = h("div", "ctx-list");
    changes.forEach((ch) => {
      const item = h("div", "ctx-item open");
      const head = h("div", "ctx-file");
      head.appendChild(h("span", null, ch.file_path || "?"));
      head.appendChild(h("span", "act", ch.action || ""));
      head.appendChild(h("span", "act", ch.lint_passed ? "lint ✓" : "lint ✗"));
      head.appendChild(h("span", "act", ch.test_passed === true ? "test ✓" : ch.test_passed === false ? "test ✗" : "test ?"));
      list.appendChild(item);
      item.appendChild(head);
    });
    sec.appendChild(list);
    wrap.appendChild(sec);
  }
  if (!changes.length && p.summary) {
    const pre = h("pre", "ctx-snippet", p.summary);
    pre.style.display = "block";
    wrap.appendChild(pre);
  }
  return wrap;
}

function closeGateToPeek() {
  $("gateModal").classList.add("hidden");
  $("gatePillText").textContent =
    S.gate === "graph_type_select" ? "图种类待选择" :
    S.gate === "graph_review" ? "制图评审待决策" : "人工验收待决策";
  $("gatePill").classList.remove("hidden");
}
function reopenGate() {
  $("gatePill").classList.add("hidden");
  if (S.gate) openGate(S.gate, S.gatePayload);
}
function submitGate(decision, comment) {
  if (!S.gate) return;
  const gate = S.gate;
  $("gateModal").classList.add("hidden");
  $("gatePill").classList.add("hidden");
  S.gate = null;
  addDivider(decision === "approve" ? "评审通过 · 继续推进" : "已驳回 · 意见回传",
    comment ? `「${comment.slice(0, 40)}${comment.length > 40 ? "…" : ""}」` : null, true);
  if (decision === "approve") setStep(stepIdxForStage(gate) + 1, true);
  setRunning(true);
  startStream({ op: "gate", decision, comment: comment || "" });
  toast(decision === "approve" ? "已通过" : "意见已回传",
    decision === "approve" ? "流程继续推进" : "正在按意见重新执行");
}

/* ── 会话 ────────────────────────────────────────────── */

async function refreshSessions() {
  try {
    const data = await api("/api/sessions");
    const ul = $("sessions");
    ul.innerHTML = "";
    $("sessCount").textContent = data.length ? String(data.length) : "";
    if (!data.length) {
      ul.appendChild(h("li", "sess-empty", "暂无会话"));
      return;
    }
    data.forEach((s) => {
      const li = h("li", "sess" + (s.thread_id === S.tid ? " active" : ""));
      li.appendChild(h("div", "sess-title", s.title || "（未命名需求）"));
      const meta = h("div", "sess-meta");
      meta.appendChild(h("span", "sess-stage" + (s.thread_id === S.tid ? " on" : ""), stageName(s.stage) || s.stage));
      if (s.has_graph) meta.appendChild(h("span", "sess-graph", "◆ 已有逻辑图"));
      li.appendChild(meta);
      const del = h("button", "sess-del", "✕");
      del.title = "删除会话";
      del.onclick = (e) => {
        e.stopPropagation();
        if (!del.classList.contains("confirm")) {
          del.classList.add("confirm");
          del.textContent = "确认删除";
          setTimeout(() => { del.classList.remove("confirm"); del.textContent = "✕"; }, 2600);
          return;
        }
        api(`/api/sessions/${s.thread_id}`, { method: "DELETE" })
          .then(() => { toast("已删除会话", s.title || s.thread_id); refreshSessions(); })
          .catch((err) => toast("删除失败", err.message, "err"));
      };
      li.appendChild(del);
      li.onclick = () => openSession(s.thread_id);
      ul.appendChild(li);
    });
  } catch (err) {
    $("sessions").innerHTML = "";
    $("sessions").appendChild(h("li", "sess-empty", "会话列表加载失败"));
  }
}

async function openSession(tid) {
  if (S.running) { toast("请等待当前流程结束", "会话切换将在流程暂停后可用", "err"); return; }
  try {
    const snap = await api(`/api/sessions/${tid}`);
    resetFeed();
    S.tid = tid;
    updateCfgBtn();
    S.gate = null; S.graph = null; S.report = null;
    const vals = snap.values || {};
    // 回放对话
    (vals.messages || []).forEach((m) => {
      const c = String(m.content || "").trim();
      if (!c) return;
      if (m.type === "human") addUserMsg(c); else addAiMsg(c);
    });
    // 回放产物
    if (vals.code_context?.length) renderContextCard(vals.code_context);
    if (vals.logic_graph) renderGraphCard(vals.logic_graph);
    if (vals.code_changes?.length) renderChangesCard(vals.code_changes);
    if (vals.test_report) renderTestCard(vals.test_report);
    // 阶段（先定模式再画步骤条，仅需求模式跳过检索/生成两步）
    S.noCode = reqNoCode(vals.requirement);
    S.stage = snap.stage || "clarify";
    setStep(stepIdxForStage(S.stage), false);
    if (S.stage === "done" && !(snap.next || []).length) setStep(7, false);
    // 挂起的门禁
    const next = snap.next || [];
    if (next.includes("graph_type_select")) {
      // 图种类选择门禁：候选由服务端按同一套规则推断重算（interrupt 载荷不落 checkpoint）
      try {
        const cand = await api(`/api/sessions/${tid}/graph-type-candidates`);
        if (cand.pending) openGate("graph_type_select", cand);
      } catch { /* 恢复候选失败不阻塞会话打开 */ }
    } else if (next.includes("graph_review")) {
      const g = vals.logic_graph || {};
      const req = vals.requirement || {};
      openGate("graph_review", {
        mode: S.noCode ? "no_code" : "with_code",
        mermaid_source: g.mermaid_source || "",
        nodes: (g.nodes || []).map((n) => ({ node_id: n.node_id, label: n.label, node_type: n.node_type, is_modified: !!n.is_modified, code_ref: n.code_ref })),
        node_count: (g.nodes || []).length, edge_count: (g.edges || []).length,
        modified_count: (g.nodes || []).filter((n) => n.is_modified).length,
        requirement: {
          project_context: req.project_context || "", target_modules: req.target_modules || [],
          edge_cases: req.edge_cases || [], acceptance_criteria: req.acceptance_criteria || [],
        },
      });
    } else if (next.includes("review")) {
      const run = (vals.test_report || {}).run || {};
      openGate("human_review", {
        mode: S.noCode ? "no_code" : "with_code",
        code_changes: vals.code_changes || [],
        code_changes_count: (vals.code_changes || []).length,
        test_summary: {
          passed: run.passed ?? "—", failed: run.failed ?? "—",
          coverage_pct: run.coverage_pct ?? 0,
          case_count: (vals.test_report?.test_cases || []).length,
          executed: !!run.executed,
          skip_reason: run.skip_reason,
          duration_sec: run.duration_sec ?? 0,
          failure_count: (Number(run.failed) || 0) + (Number(run.errors) || 0),
        },
      });
    }
    addDivider(`已恢复会话 ${tid}`, stageName(S.stage), true);
    refreshSessions();
    closeSidebar();
    updateComposer();
  } catch (err) {
    toast("打开会话失败", err.message, "err");
  }
}

/* ── 输入区 ──────────────────────────────────────────── */

function updateComposer() {
  const input = $("chatInput");
  const hint = $("composerHint");
  const send = $("btnSend");
  if (S.running) {
    hint.textContent = "流程推进中…";
    send.disabled = true;
    input.disabled = true;
  } else if (S.gate) {
    hint.textContent = S.gate === "graph_type_select"
      ? "图种类待选择 — 请在弹窗中挑选本次逻辑图的种类"
      : S.gate === "graph_review"
      ? "制图评审待决策 — 请在评审窗中通过或驳回"
      : "人工验收待决策 — 请在验收窗中通过或驳回";
    send.disabled = true;
    input.disabled = true;
  } else if (!S.tid) {
    hint.textContent = "描述需求开始全链路 · Enter 发送 · ⚙ 运行配置 · ⇪ 导入文档";
    send.disabled = !input.value.trim();
    input.disabled = false;
  } else if (S.stage === "done") {
    hint.textContent = "全流程已完成 · 可导出产物，或点左侧「＋ 新会话」开启下一轮";
    send.disabled = true;
    input.disabled = false;
  } else {
    hint.textContent = S.stage === "clarify"
      ? "当前阶段：需求澄清 · 回答或补充，Enter 发送 · 可回复「头脑风暴」或「拷问」切换澄清方式"
      : `当前阶段：${stageName(S.stage)} · 回答或补充，Enter 发送`;
    send.disabled = !input.value.trim();
    input.disabled = false;
  }
}

function autoresize() {
  const t = $("chatInput");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, Math.round(window.innerHeight * 0.4)) + "px";
}

function sendChat() {
  const t = $("chatInput");
  const text = t.value.trim();
  if (!text || S.running || S.gate) return;
  if (!S.tid) { createAndStart(text); return; }  // 单一入口：无会话时发送 = 创建会话
  t.value = ""; autoresize();
  addUserMsg(text);
  setRunning(true);
  startStream({ op: "message", text });
}

async function createAndStart(text) {
  try {
    const { thread_id } = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ set_fields: buildSetFields() }),
    });
    S.tid = thread_id;
    S.stage = "clarify";
    S.graph = null; S.report = null; S.gate = null;
    $("chatInput").value = ""; autoresize();
    resetFeed();
    setStep(0, true);
    addUserMsg(text);
    setRunning(true);
    startStream({ op: "message", text });
    renderCfgChips();
    updateCfgBtn();
    refreshSessions();
    closeSidebar();
  } catch (err) {
    toast("创建会话失败", err.message, "err");
  }
}

/* ＋ 新会话：回到空态，等待 composer 输入 */
function newSession() {
  if (S.running) { toast("请等待当前流程结束", "流程暂停后再新建会话", "err"); return; }
  stopStream();
  S.tid = null; S.gate = null; S.graph = null; S.report = null; S.stage = "clarify";
  $("gateModal").classList.add("hidden");
  $("gatePill").classList.add("hidden");
  resetFeed();
  showEmptyState();
  setStep(0, false);
  updateComposer();
  updateCfgBtn();
  refreshSessions();
  closeSidebar();
  $("chatInput").focus();
}

function buildSetFields() {
  const fields = [];
  const root = $("fRoot").value.trim();
  if (root && root !== ".") fields.push(`project_root=${root}`);
  const mods = tagValues($("tagModules"));
  if (mods.length) fields.push(`target_modules=${JSON.stringify(mods)}`);
  const edges = tagValues($("tagEdges"));
  if (edges.length) fields.push(`edge_cases=${JSON.stringify(edges)}`);
  const acc = tagValues($("tagAccept"));
  if (acc.length) fields.push(`acceptance_criteria=${JSON.stringify(acc)}`);
  const ctx = $("fCtx").value.trim();
  if (ctx) fields.push(`project_context=${ctx}`);
  const ioIn = $("fIoIn").value.trim(), ioOut = $("fIoOut").value.trim();
  if (ioIn || ioOut) fields.push(`io_constraints=${JSON.stringify({ input: ioIn, output: ioOut })}`);
  return fields;
}

function resetFeed() {
  const feed = $("feed");
  feed.innerHTML = "";
  const inner = h("div", "feed-inner");
  feed.appendChild(inner);
  S.autoScroll = true;
  liveClearAll();
  S.stage = "clarify";
  S.noCode = false;
  setStep(0, false);
}

/* 空态引导（无活动会话时） */
function showEmptyState() {
  if (S.tid || $("feedEmpty")) return;
  const d = h("div", "feed-empty");
  d.id = "feedEmpty";
  d.appendChild(h("div", "fe-mark"));
  d.appendChild(h("h2", null, "从一段需求，到一组测试场景"));
  d.appendChild(h("p", null, "DevFlow 会澄清需求、生成可机读逻辑图与测试场景；提供项目代码时还会检索代码、生成代码并真实执行测试，不提供代码则直接基于需求设计端到端测试用例。关键节点由你把关。在下方输入框描述需求即可开始。"));
  const ol = h("ol", "fe-steps");
  [["01", "输入需求，回答 AI 追问"], ["02", "评审逻辑图，通过或驳回"], ["03", "验收产物，导出测试场景"]]
    .forEach(([n, t]) => {
      const li = h("li");
      li.appendChild(h("b", null, n));
      li.appendChild(document.createTextNode(t));
      ol.appendChild(li);
    });
  d.appendChild(ol);
  const actions = h("div", "fe-actions");
  const btn = h("button", "chip-btn", "✦ 用示例需求试试");
  btn.id = "feSample";
  btn.onclick = () => { fillSample(); $("chatInput").focus(); };
  actions.appendChild(btn);
  d.appendChild(actions);
  feedInner().appendChild(d);
}

/* ── 运行配置弹层 + 配置 chips ───────────────────────── */

const CFG_DEFAULTS = { fRoot: ".", fCtx: "桌面 GUI 计算器应用（tkinter）", fIoIn: "按钮点击与表达式", fIoOut: "结果或错误提示" };

function cfgIsCustom() {
  if ($("fRoot").value.trim() && $("fRoot").value.trim() !== ".") return true;
  if (tagValues($("tagModules")).length || tagValues($("tagEdges")).length || tagValues($("tagAccept")).length) return true;
  return Object.keys(CFG_DEFAULTS).some((id) =>
    id !== "fRoot" && $(id).value.trim() !== CFG_DEFAULTS[id]);
}

function cfgGroups() {
  const mods = tagValues($("tagModules"));
  const edges = tagValues($("tagEdges"));
  const acc = tagValues($("tagAccept"));
  const root = $("fRoot").value.trim();
  const ctx = $("fCtx").value.trim();
  const ioIn = $("fIoIn").value.trim(), ioOut = $("fIoOut").value.trim();
  const groups = [];
  if (root && root !== ".") groups.push({ key: "root", label: `project_root=${root}`, clear: () => { $("fRoot").value = "."; } });
  if (mods.length) groups.push({ key: "mods", label: `目标模块 ${mods.length}`, clear: () => initTags($("tagModules"), []) });
  if (edges.length) groups.push({ key: "edges", label: `边界场景 ${edges.length}`, clear: () => initTags($("tagEdges"), []) });
  if (acc.length) groups.push({ key: "acc", label: `验收标准 ${acc.length}`, clear: () => initTags($("tagAccept"), []) });
  if (ctx && ctx !== CFG_DEFAULTS.fCtx) groups.push({ key: "ctx", label: `背景：${ctx.slice(0, 14)}${ctx.length > 14 ? "…" : ""}`, clear: () => { $("fCtx").value = CFG_DEFAULTS.fCtx; } });
  if ((ioIn && ioIn !== CFG_DEFAULTS.fIoIn) || (ioOut && ioOut !== CFG_DEFAULTS.fIoOut))
    groups.push({ key: "io", label: "IO 约束", clear: () => { $("fIoIn").value = CFG_DEFAULTS.fIoIn; $("fIoOut").value = CFG_DEFAULTS.fIoOut; } });
  return groups;
}

function renderCfgChips() {
  const box = $("cfgChips");
  box.innerHTML = "";
  const groups = cfgGroups();
  $("btnCfg").classList.toggle("on", groups.length > 0);
  if (!groups.length || S.tid) { box.classList.add("hidden"); return; }
  groups.forEach((g) => {
    const chip = h("span", "cfg-chip mono");
    chip.appendChild(document.createTextNode(g.label + " "));
    const x = h("i", null, "×");
    x.onclick = () => { g.clear(); renderCfgChips(); };
    chip.appendChild(x);
    box.appendChild(chip);
  });
  box.classList.remove("hidden");
}

function toggleCfgPop(force) {
  const pop = $("cfgPop");
  const show = force !== undefined ? force : pop.classList.contains("hidden");
  if (show && S.tid) { toast("配置仅在新会话创建时生效", "本会话已按创建时配置运行；点「＋ 新会话」可重新配置"); return; }
  pop.classList.toggle("hidden", !show);
}

/* ⚙ 仅新会话创建前可开配置；已有会话时置灰（仍可点击以提示原因），chips 一并隐藏 */
function updateCfgBtn() {
  renderCfgChips();
  $("btnCfg").setAttribute("aria-disabled", String(!!S.tid));
  if (S.tid) $("btnCfg").classList.remove("on");
}

/* ── 标签输入组件 ────────────────────────────────────── */

function tagAdd(el, input, text) {
  text = String(text || "").trim();
  if (!text) return;
  if ([...el.querySelectorAll(".tag span")].some((s) => s.textContent === text)) return;
  const tag = h("span", "tag");
  tag.appendChild(h("span", null, text));
  const x = h("i", null, "×");
  x.onclick = (e) => { e.stopPropagation(); tag.remove(); };
  tag.appendChild(x);
  el.insertBefore(tag, input);
}
function tagValues(el) {
  return [...el.querySelectorAll(".tag span")].map((s) => s.textContent);
}
function initTags(el, initial) {
  let input = el.querySelector("input");
  if (!input) {
    input = document.createElement("input");
    input.placeholder = "添加后回车";
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") { e.preventDefault(); tagAdd(el, input, input.value); input.value = ""; }
      else if (e.key === "Backspace" && !input.value) {
        const tags = el.querySelectorAll(".tag");
        if (tags.length) tags[tags.length - 1].remove();
      }
    });
    el.addEventListener("click", () => input.focus());
    el.appendChild(input);
  }
  el.querySelectorAll(".tag").forEach((t) => t.remove());
  (initial || []).forEach((v) => tagAdd(el, input, v));
}

/* ── 配置状态 ────────────────────────────────────────── */

// 模型池条目的命名约定（见 devflow/config.py）：激活模型叫 "name"，池内其余叫 "name:model"。
// 这里把展开后的条目聚合回 provider 分组，避免多模型时把页面挤爆。
function groupProviders(providers) {
  const groups = new Map();
  (providers || []).forEach((p) => {
    const i = p.name.indexOf(":");
    const base = i > 0 ? p.name.slice(0, i) : p.name;
    const model = i > 0 ? p.name.slice(i + 1) : p.model;
    if (!groups.has(base)) groups.set(base, { name: base, key: p.key, models: [] });
    groups.get(base).models.push(model || p.name);
  });
  return [...groups.values()];
}

async function loadHealth() {
  try {
    S.health = await api("/api/health");
    const llm = S.health.llm || {};
    const groups = groupProviders(llm.providers);
    const realKey = groups.some((g) => g.key);
    const dot = $("healthDot");
    dot.className = "dot" + (realKey ? " ok" : llm.mock_fallback ? " mock" : " bad");
    $("healthText").textContent = realKey
      ? groups.map((g) => g.name).join(" · ") + (llm.providers.length > 1 ? " ▾" : "")
      : llm.mock_fallback ? "Mock 模式" : "未配置";
  } catch {
    $("healthDot").className = "dot bad";
    $("healthText").textContent = "服务异常";
  }
}

function toggleHealthPop() {
  const pop = $("healthPop");
  if (!pop.classList.contains("hidden")) { pop.classList.add("hidden"); return; }
  if (!S.health) return;
  pop.innerHTML = "";
  const h4 = h("h4", null, "LLM 提供商");
  pop.appendChild(h4);
  const row = (k, v) => {
    const r = h("div", "row");
    r.appendChild(h("span", null, k));
    r.appendChild(h("span", "mono", String(v)));
    return r;
  };
  const groups = groupProviders(S.health.llm?.providers);
  if (!groups.length) pop.appendChild(row("LLM", "未配置提供商"));
  groups.forEach((g) => {
    const keyTxt = g.key || "key 未配置";
    pop.appendChild(row(`LLM · ${g.name}`,
      g.models.length > 1 ? `${keyTxt} · ${g.models.length} 个模型` : `${g.models[0]} · ${keyTxt}`));
    if (g.models.length > 1) {
      const box = h("div", "models");
      g.models.forEach((m) => box.appendChild(h("span", "m-chip", m)));
      pop.appendChild(box);
    }
  });
  const pipe = S.health.pipeline || {};
  const sec = h("div", "sec");
  sec.appendChild(row("Mock 兜底", S.health.llm?.mock_fallback ? "开启" : "关闭"));
  sec.appendChild(row("代码检索", pipe.code_search || "mock"));
  sec.appendChild(row("代码生成", pipe.code_edit || "mock"));
  sec.appendChild(row("测试生成", pipe.test_gen || "mock"));
  sec.appendChild(row("Checkpoint", S.health.checkpoint_db?.path || ""));
  pop.appendChild(sec);
  if (!($('healthDot').className || "").includes("ok")) {
    pop.appendChild(h("div", "warn", "当前 LLM 输出为 Mock 演示数据；复制 .env.example 为 .env 并填入真实 Key 即可获得真实结果。"));
  }
  pop.classList.remove("hidden");
}

/* ── 文档导入 ────────────────────────────────────────── */

async function importDoc(file) {
  if (!/\.(md|txt|docx)$/i.test(file.name)) { toast("不支持的格式", "仅支持 .md / .txt / .docx", "err"); return; }
  const fd = new FormData();
  fd.append("file", file);
  toast("正在解析文档", file.name);
  try {
    const { text, chars } = await api("/api/doc/extract", { method: "POST", body: fd });
    $("chatInput").value = text;
    autoresize();
    updateComposer();
    toast("文档已导入", `${file.name} · ${chars} 字符，检查后回车开始全链路`);
  } catch (err) {
    toast("解析失败", err.message, "err");
  }
}

/* ── 侧栏（移动端） ──────────────────────────────────── */

function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("scrim").hidden = true;
}

/* ── 启动 ────────────────────────────────────────────── */

const SAMPLE_REQ = "开发一个带图形界面的 Python 计算器（GUI，使用 tkinter），支持四则运算、连续运算、除零报错、负数与小数的输入，输入非法字符时给出错误提示。";

function fillSample() {
  $("chatInput").value = SAMPLE_REQ;
  autoresize();
  // 配置恢复为示例值
  $("fRoot").value = ".";
  $("fCtx").value = "桌面 GUI 计算器应用（tkinter）";
  $("fIoIn").value = "按钮点击与表达式";
  $("fIoOut").value = "结果或错误提示";
  initTags($("tagModules"), ["calculator/calc.py"]);
  initTags($("tagEdges"), ["除零", "连续运算", "负数", "小数"]);
  initTags($("tagAccept"), ["四则运算结果正确", "除零给出错误提示", "GUI 可启动"]);
  renderCfgChips();
  updateComposer();
}

function boot() {
  initMermaid();
  buildStepper();
  setStep(0, false);

  initTags($("tagModules"), ["calculator/calc.py"]);
  initTags($("tagEdges"), ["除零", "连续运算", "负数", "小数"]);
  initTags($("tagAccept"), ["四则运算结果正确", "除零给出错误提示", "GUI 可启动"]);

  // 顶栏
  $("btnTheme").onclick = toggleTheme;
  $("btnHealth").onclick = toggleHealthPop;
  document.addEventListener("click", (e) => {
    if (!$("healthPop").classList.contains("hidden") &&
        !$("healthPop").contains(e.target) && !$("btnHealth").contains(e.target)) {
      $("healthPop").classList.add("hidden");
    }
    if (!$("cfgPop").classList.contains("hidden") &&
        !$("cfgPop").contains(e.target) && !$("btnCfg").contains(e.target)) {
      toggleCfgPop(false);
    }
  });

  // 侧栏（仅历史）+ 新会话
  $("btnSidebar").onclick = () => {
    $("sidebar").classList.add("open");
    $("scrim").hidden = false;
  };
  $("scrim").onclick = closeSidebar;
  $("btnNewChat").onclick = newSession;

  // 运行配置弹层
  $("btnCfg").onclick = (e) => { e.stopPropagation(); toggleCfgPop(); };
  $("btnCfgSample").onclick = () => { fillSample(); };
  $("btnCfgClear").onclick = () => {
    $("fRoot").value = "."; $("fCtx").value = ""; $("fIoIn").value = ""; $("fIoOut").value = "";
    initTags($("tagModules"), []); initTags($("tagEdges"), []); initTags($("tagAccept"), []);
    renderCfgChips();
  };
  $("cfgPop").addEventListener("input", renderCfgChips);

  // 文档导入：按钮 + 拖到输入框
  $("btnDoc").onclick = () => $("fileDoc").click();
  $("fileDoc").addEventListener("change", (e) => { if (e.target.files[0]) importDoc(e.target.files[0]); e.target.value = ""; });
  const box = $("composerBox");
  ["dragover", "dragenter"].forEach((ev) => box.addEventListener(ev, (e) => {
    e.preventDefault(); box.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((ev) => box.addEventListener(ev, (e) => {
    e.preventDefault(); box.classList.remove("dragover");
    if (ev === "drop" && e.dataTransfer.files[0]) importDoc(e.dataTransfer.files[0]);
  }));

  // 聊天输入（唯一入口）
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (!e.shiftKey || e.metaKey || e.ctrlKey)) { e.preventDefault(); sendChat(); }
  });
  $("chatInput").addEventListener("input", () => { autoresize(); updateComposer(); });
  $("btnSend").onclick = sendChat;

  // 门禁
  $("btnApprove").onclick = () => submitGate("approve", null);
  $("btnReject").onclick = () => {
    $("gateCommentWrap").classList.remove("hidden");
    $("btnReject").classList.add("hidden");
    $("btnGateSubmit").classList.remove("hidden");
    $("btnGatePeek").classList.add("hidden");
    $("gateComment").focus();
  };
  $("btnGateSubmit").onclick = () => {
    const c = $("gateComment").value.trim();
    submitGate("reject", c || null);
  };
  $("btnGatePeek").onclick = closeGateToPeek;
  $("gatePill").onclick = reopenGate;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("gateModal").classList.contains("hidden")) closeGateToPeek();
  });

  updateComposer();
  renderCfgChips();
  updateCfgBtn();
  loadHealth();
  refreshSessions();
  showEmptyState();
}

boot();
