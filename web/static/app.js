"use strict";
/* ═══════════════════════════════════════════════════════════════
   DevFlow Web Shell · 前端交互
   聊天流 + 步骤条 + 门禁评审 + 产物卡片（逻辑图/测试表/变更/检索）
   ═══════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const S = {
  tid: null, running: false, gate: null, gatePayload: null,
  stage: "clarify", startedAt: 0, lastNodeAt: 0, timer: null,
  graph: null, report: null, health: null,
  autoScroll: true, live: new Map(), theme: document.documentElement.dataset.theme || "dark",
  mermaidReady: false,
};

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
  document.querySelectorAll("#stepper .step").forEach((el, i) => {
    el.classList.toggle("done", i < idx || (idx === 7 && i === 7));
    el.classList.toggle("active", i === idx && idx !== 7);
    el.classList.toggle("running", !!(running && i === idx));
  });
}
function stepIdxForStage(stage) {
  if (stage === "graph_review") return 2;
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
  const { card, actions } = cardShell("LOGIC GRAPH · 逻辑图");
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
  if (c.title) return c;
  return {
    tier: c.tier || "functional",
    priority: c.priority || "P2",
    title: c.test_symbol || c.symbol || "(未命名测试)",
    target: c.test_file || c.target || "",
    precondition: c.precondition || "",
    steps: c.code_snippet || c.steps || "",
    expected: c.expected || "",
    rationale: c.covered_edges ? `覆盖边：${c.covered_edges.join("、")}` : (c.rationale || ""),
  };
}

function renderTestCard(report) {
  S.report = report;
  const cases = (report.test_cases || []).map(normCase);
  const { card, actions } = cardShell(`TEST DESIGN · 测试场景 · ${cases.length}`);

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
    "<tr><th>层级</th><th>优先级</th><th>标题</th><th>目标</th><th>前置</th><th>步骤</th><th>预期</th><th>依据</th></tr>";
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
      const blob = [c.title, c.target, c.steps, c.expected, c.precondition].join(" ").toLowerCase();
      if (!blob.includes(tFilter.q)) return false;
    }
    return true;
  }
  function renderRows() {
    tbody.innerHTML = "";
    const shown = cases.filter(pass);
    if (!shown.length) {
      tbody.appendChild(h("tr", null, "")).appendChild(
        Object.assign(document.createElement("td"), { colSpan: 8, className: "t-empty", textContent: "没有匹配的测试场景" }));
      count.textContent = "0 条";
      return;
    }
    shown.forEach((c) => {
      const tr = h("tr");
      const tdTier = h("td");
      tdTier.appendChild(h("span", `tier ${c.tier || ""}`, TIER_NAME[c.tier] || c.tier || "—"));
      tr.appendChild(tdTier);
      const tdP = h("td");
      tdP.appendChild(h("span", `prio ${String(c.priority).toUpperCase()}`, String(c.priority || "—").toUpperCase()));
      tr.appendChild(tdP);
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
  actions.appendChild(miniBtn("⭳ MD", () => exportTestMD(cases), "导出 Markdown"));
  actions.appendChild(miniBtn("⧉ 复制", () => copyText(testToMD(cases), "测试场景 Markdown 已复制")));

  feedAppend(card);
  renderExportBar();
  return card;
}

function testToCSV(cases) {
  const head = ["层级", "优先级", "标题", "目标", "前置条件", "步骤", "预期结果", "设计依据"];
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = cases.map((c) =>
    [TIER_NAME[c.tier] || c.tier, c.priority, c.title, c.target, c.precondition, c.steps, c.expected, c.rationale].map(q).join(","));
  return "\uFEFF" + [head.map(q).join(","), ...rows].join("\r\n");
}
function testToMD(cases) {
  const lines = ["| 层级 | 优先级 | 标题 | 目标 | 前置 | 步骤 | 预期 | 依据 |",
    "|---|---|---|---|---|---|---|---|"];
  const cell = (v) => String(v ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");
  cases.forEach((c) => lines.push(
    "| " + [TIER_NAME[c.tier] || c.tier, c.priority, c.title, c.target, c.precondition, c.steps, c.expected, c.rationale].map(cell).join(" | ") + " |"));
  return lines.join("\n");
}
function exportTestCSV(cases) { download(`devflow-tests-${S.tid || "export"}.csv`, testToCSV(cases), "text/csv;charset=utf-8"); toast("已导出 CSV", `${cases.length} 条测试场景`); }
function exportTestMD(cases) { download(`devflow-tests-${S.tid || "export"}.md`, testToMD(cases)); toast("已导出 Markdown", `${cases.length} 条测试场景`); }

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
  $("btnNew").disabled = on;
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
    setStep(stepIdxForStage(S.gate === "graph_review" ? "graph_review" : "review"), false);
    updateComposer();
  } else if (S.stage === "done") {
    setStep(7, false);
    addDivider("全流程完成 ✦", "产物可导出");
    toast("全流程完成", "逻辑图、代码与测试场景已就绪，可在导出条打包带走。");
    updateComposer();
  } else {
    updateComposer();
  }
}

/* ── 门禁 ────────────────────────────────────────────── */

const GATE_META = {
  graph_review: { n: 1, title: "制图评审：逻辑图与需求对齐了吗？", sub: "REJECT 将回传意见并自动重制图" },
  human_review: { n: 2, title: "人工验收：产物达到验收标准了吗？", sub: "REJECT 将回传意见并回到代码生成" },
};

function openGate(gate, payload) {
  S.gate = gate;
  S.gatePayload = payload || {};
  const meta = GATE_META[gate] || { n: "?", title: "确认", sub: "" };
  $("gateTag").textContent = `GATE ${meta.n}/2`;
  $("gateTitle").textContent = meta.title;
  $("gateSub").textContent = meta.sub;
  $("gateComment").value = "";
  $("gateCommentWrap").classList.add("hidden");
  $("btnGateSubmit").classList.add("hidden");
  $("btnReject").classList.remove("hidden");
  $("btnApprove").classList.remove("hidden");
  $("btnGatePeek").classList.remove("hidden");

  const body = $("gateBody");
  body.innerHTML = "";
  if (gate === "graph_review") body.appendChild(gateGraphBody(S.gatePayload));
  else body.appendChild(gateReviewBody(S.gatePayload));

  $("gateModal").classList.remove("hidden");
  $("gatePill").classList.add("hidden");
  setStep(stepIdxForStage(gate), false);
  updateComposer();
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
  // 执行闭环：测试数字是否来自真实 pytest
  stats.appendChild(
    stat(
      "测试执行",
      ts.executed ? `pytest ✓ ${ts.duration_sec ?? 0}s` : `未执行${ts.skip_reason ? " · " + ts.skip_reason : ""}`,
      ts.executed ? Number(ts.failed) > 0 || Number(ts.failure_count) > 0 : true,
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
  $("gatePillText").textContent = S.gate === "graph_review" ? "制图评审待决策" : "人工验收待决策";
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
    // 阶段
    S.stage = snap.stage || "clarify";
    setStep(stepIdxForStage(S.stage), false);
    if (S.stage === "done" && !(snap.next || []).length) setStep(7, false);
    // 挂起的门禁
    const next = snap.next || [];
    if (next.includes("graph_review")) {
      const g = vals.logic_graph || {};
      const req = vals.requirement || {};
      openGate("graph_review", {
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
        code_changes: vals.code_changes || [],
        code_changes_count: (vals.code_changes || []).length,
        test_summary: {
          passed: run.passed ?? "—", failed: run.failed ?? "—",
          coverage_pct: run.coverage_pct ?? 0,
          case_count: (vals.test_report?.test_cases || []).length,
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
    hint.textContent = S.gate === "graph_review"
      ? "制图评审待决策 — 请在评审窗中通过或驳回"
      : "人工验收待决策 — 请在验收窗中通过或驳回";
    send.disabled = true;
    input.disabled = true;
  } else if (!S.tid) {
    hint.textContent = "输入需求，或点「开始全链路」· Enter 发送";
    send.disabled = !input.value.trim();
    input.disabled = false;
  } else if (S.stage === "done") {
    hint.textContent = "全流程已完成 · 可导出产物，或新建会话开启下一轮";
    send.disabled = true;
    input.disabled = false;
  } else {
    hint.textContent = `当前阶段：${stageName(S.stage)} · 回答或补充，Enter 发送`;
    send.disabled = !input.value.trim();
    input.disabled = false;
  }
}

function autoresize() {
  const t = $("chatInput");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}

function sendChat() {
  const t = $("chatInput");
  const text = t.value.trim();
  if (!text || !S.tid || S.running || S.gate) return;
  t.value = ""; autoresize();
  addUserMsg(text);
  setRunning(true);
  startStream({ op: "message", text });
}

async function startFlow() {
  const text = $("reqText").value.trim();
  if (!text) { toast("先输入需求", "可以直接粘贴需求文本，或点「示例需求」", "err"); return; }
  if (S.running) return;
  resetFeed();
  try {
    const { thread_id } = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ set_fields: buildSetFields() }),
    });
    S.tid = thread_id;
    S.stage = "clarify";
    S.graph = null; S.report = null; S.gate = null;
    setStep(0, true);
    addUserMsg(text);
    setRunning(true);
    startStream({ op: "message", text });
    refreshSessions();
    closeSidebar();
  } catch (err) {
    toast("创建会话失败", err.message, "err");
  }
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
  setStep(0, false);
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

async function loadHealth() {
  try {
    S.health = await api("/api/health");
    const llm = S.health.llm || {};
    const providers = llm.providers || [];
    const realKey = providers.some((p) => p.key);
    const dot = $("healthDot");
    dot.className = "dot" + (realKey ? " ok" : llm.mock_fallback ? " mock" : " bad");
    $("healthText").textContent = realKey
      ? providers.map((p) => p.name).join(" · ")
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
  const h4 = h("h4", null, "PIPELINE 配置");
  pop.appendChild(h4);
  const row = (k, v) => {
    const r = h("div", "row");
    r.appendChild(h("span", null, k));
    r.appendChild(h("span", "mono", String(v)));
    return r;
  };
  (S.health.llm?.providers || []).forEach((p) => {
    pop.appendChild(row(`LLM · ${p.name}`, `${p.model}${p.key ? " · " + p.key : " · key 未配置"}`));
  });
  if (!(S.health.llm?.providers || []).length) pop.appendChild(row("LLM", "未配置提供商"));
  pop.appendChild(row("Mock 兜底", S.health.llm?.mock_fallback ? "开启" : "关闭"));
  const pipe = S.health.pipeline || {};
  const sec = h("div", "sec");
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
    $("reqText").value = text;
    toast("文档已导入", `${file.name} · ${chars} 字符，检查后点「开始全链路」`);
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
  $("reqText").value = SAMPLE_REQ;
  // 高级字段恢复为示例值
  $("fRoot").value = ".";
  $("fCtx").value = "桌面 GUI 计算器应用（tkinter）";
  $("fIoIn").value = "按钮点击与表达式";
  $("fIoOut").value = "结果或错误提示";
  initTags($("tagModules"), ["calculator/calc.py"]);
  initTags($("tagEdges"), ["除零", "连续运算", "负数", "小数"]);
  initTags($("tagAccept"), ["四则运算结果正确", "除零给出错误提示", "GUI 可启动"]);
}

function boot() {
  initMermaid();
  buildStepper();
  setStep(0, false);

  [["tagModules"], ["tagEdges"], ["tagAccept"]].forEach(([id]) => initTags($(id), []));
  fillSample();

  // 顶栏
  $("btnTheme").onclick = toggleTheme;
  $("btnHealth").onclick = toggleHealthPop;
  document.addEventListener("click", (e) => {
    if (!$("healthPop").classList.contains("hidden") &&
        !$("healthPop").contains(e.target) && e.target !== $("btnHealth")) {
      $("healthPop").classList.add("hidden");
    }
  });

  // 侧栏
  $("btnSidebar").onclick = () => {
    $("sidebar").classList.add("open");
    $("scrim").hidden = false;
  };
  $("scrim").onclick = closeSidebar;

  // 新会话
  $("btnNew").onclick = startFlow;
  $("btnSample").onclick = fillSample;
  $("feSample").onclick = () => { fillSample(); $("reqText").focus(); };
  $("reqText").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startFlow(); }
  });
  $("fileDoc").addEventListener("change", (e) => { if (e.target.files[0]) importDoc(e.target.files[0]); });
  const reqWrap = document.querySelector(".req-wrap");
  ["dragover", "dragenter"].forEach((ev) => reqWrap.addEventListener(ev, (e) => {
    e.preventDefault(); $("reqText").classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((ev) => reqWrap.addEventListener(ev, (e) => {
    e.preventDefault(); $("reqText").classList.remove("dragover");
    if (ev === "drop" && e.dataTransfer.files[0]) importDoc(e.dataTransfer.files[0]);
  }));

  // 聊天输入
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
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
  loadHealth();
  refreshSessions();
}

boot();
