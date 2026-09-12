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
  lastSeq: 0,        // 事件游标：断线重连/换会话回来按此补齐丢失进度
  req: {},           // 当前会话 requirement（含 project_root，清单库跳转用）
  history: [],       // 回退锚点（GET /history，新→旧）
  msgIds: new Set(), // 已渲染的消息 id：compress 截断会整批重发保留消息，据此去重
  pendingEdit: null, // 「编辑重发」流程中：回退完成后放回输入框的原文
  adoptSel: new Set(),      // 测试卡勾选采纳的用例 id（采纳 = 评审通过）
  pendingDistill: null,     // 评审通过后要弹的沉淀建议卡：null=不弹，数组=预勾选的采纳集
  distillPromptEl: null,    // 已插出的沉淀建议卡 DOM（防重复）
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
/* 轻量 markdown 渲染：先 esc() 转义原文，再只注入自己生成的受控标签，防 XSS */
function mdInline(s) {
  let out = esc(s);
  out = out.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return out;
}
function mdBlock(md) {
  const frag = document.createDocumentFragment();
  const lines = String(md ?? "").replace(/\r\n?/g, "\n").split("\n");
  const isHead = (l) => /^\s{0,3}#{1,6}\s+/.test(l);
  const isUl = (l) => /^\s*[-*+]\s+/.test(l);
  const isOl = (l) => /^\s*\d+[.)]\s+/.test(l);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    let m;
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/))) {
      const el = h("h4", "md-h");
      el.innerHTML = mdInline(m[2].replace(/\s+#+\s*$/, ""));
      frag.appendChild(el); i += 1; continue;
    }
    if (isUl(line)) {
      const ul = h("ul", "md-list");
      while (i < lines.length && (m = lines[i].match(/^\s*[-*+]\s+(.*)$/))) {
        const li = h("li"); li.innerHTML = mdInline(m[1]); ul.appendChild(li); i += 1;
      }
      frag.appendChild(ul); continue;
    }
    if (isOl(line)) {
      const ol = h("ol", "md-list");
      while (i < lines.length && (m = lines[i].match(/^\s*\d+[.)]\s+(.*)$/))) {
        const li = h("li"); li.innerHTML = mdInline(m[1]); ol.appendChild(li); i += 1;
      }
      frag.appendChild(ol); continue;
    }
    const p = h("div", "md-p");
    const buf = [];
    while (i < lines.length && lines[i].trim() && !isHead(lines[i]) && !isUl(lines[i]) && !isOl(lines[i])) {
      buf.push(lines[i]); i += 1;
    }
    p.innerHTML = buf.map(mdInline).join("<br>");
    frag.appendChild(p);
  }
  return frag;
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
    if (STAGE_REVERT_NODE[id]) {
      step.title = "点击回退到此步骤并重跑下游";
      step.onclick = () => {
        if (S.running) { toast("流程推进中", "等当前步骤暂停后再回退", "err"); return; }
        const anchor = anchorForNode(STAGE_REVERT_NODE[id]);
        if (!anchor) { toast("该步骤还没有可回退的存档", "流程执行到这一步后即可回退", "err"); return; }
        openRevertModal(anchor);
      };
    }
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
  if (stage === "requirement_review") return 1;   // 制图前门禁：停在「逻辑制图」步
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

function addUserMsg(text, msgId) {
  const m = h("div", "msg me");
  if (msgId) m.dataset.msgId = msgId;
  m.appendChild(h("span", "who", "YOU"));
  m.appendChild(h("div", "bubble", text));
  attachMsgActions(m, text, true);
  feedAppend(m);
}
function addAiMsg(text, label, msgId) {
  const m = h("div", "msg ai");
  if (msgId) m.dataset.msgId = msgId;
  m.appendChild(h("span", "who", label ? `AI · ${label}` : "AI"));
  m.appendChild(h("div", "bubble", text));
  attachMsgActions(m, text, false);
  feedAppend(m);
}
/* 气泡下方操作行（常显，长内容也不必往上翻找）：
   用户 → 编辑重发（回退+原文回填）/ 回到此处；AI → 回到此处（该回复作废重生成） */
function attachMsgActions(m, text, isUser) {
  const row = h("div", "msg-actions");
  if (isUser) {
    const edit = h("button", "msg-act", "✎ 编辑重发");
    edit.title = "回退到这条消息发出前，并把原文放回输入框供修改后重发";
    edit.onclick = (e) => { e.stopPropagation(); editAndResend(m, text); };
    row.appendChild(edit);
  }
  const rev = h("button", "msg-act", "⟲ 回到此处");
  rev.title = isUser
    ? "回退到这条消息发出前（不改内容），其后的产出作废并重跑"
    : "回退到这条回复之前并重新执行，该回复会重新生成";
  rev.onclick = (e) => { e.stopPropagation(); revertToMessage(m, isUser); };
  row.appendChild(rev);
  m.appendChild(row);
}
function addDivider(label, sub, info) {
  const d = h("div", "divider" + (info ? " info" : ""));
  const span = h("span", "mono", label + (sub ? `  ·  ${sub}` : ""));
  d.appendChild(span);
  feedAppend(d);
  return d;
}
/* 系统细行：provider 切换 / 使用等可见化信息 */
function addSysLine(text, tone) {
  const d = h("div", "sys-line" + (tone ? ` ${tone}` : ""));
  d.appendChild(h("span", "mono", text));
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
      startRun({ op: "message", text });
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
  actions.appendChild(miniBtn("↓ .mmd", () => download(`logic-${graph.graph_id || S.tid}.mmd`, graph.mermaid_source)));
  actions.appendChild(miniBtn("⟲ 重制图", () => openRevertForNode("graph_type_select"),
    "回退到选定图种类的时点（可顺带修改需求），重新生成逻辑图"));

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
  const { card, actions } = cardShell(`CODE CHANGES · 代码变更 · ${changes.length}`);
  actions.appendChild(miniBtn("⟲ 重新生成", () => openRevertForNode("graph_render"),
    "回退到图表渲染完成的时点，重新生成代码"));
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
  actions.appendChild(miniBtn("⟲ 重新设计", () => openRevertForNode("checklist_route_gate"),
    "回退到清单确认后的时点，重新设计测试用例"));

  // 测试概述（总-分结构的总文档；方法论来自 doc-based/functional testcase-generator skills）
  if (report.overview || (report.self_check || []).length) {
    const ov = h("div", "card-note t-overview");
    if (report.overview) {
      const row = h("div", "t-ov-row");
      row.appendChild(h("span", "t-ov-mark", "📋"));
      row.appendChild(mdBlock(report.overview));
      ov.appendChild(row);
    }
    (report.self_check || []).forEach((s) => {
      const row = h("div", "t-ov-row");
      row.appendChild(h("span", "t-ov-mark", "✓"));
      row.appendChild(mdBlock(s));
      ov.appendChild(row);
    });
    // 本卡用例若注入了业务检查清单，标注来源（checklist 库 rel_dir）
    (report.checklist_refs || []).forEach((r) => {
      if (r) ov.appendChild(h("div", "t-clref mono", "☰ 业务清单：" + r));
    });
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

  // 采纳工具条：逐条勾选采纳，采纳 = 评审通过（终审门禁弹出后可一键提交）
  const adoptBar = h("div", "t-adopt");
  const adoptCount = h("span", "t-adopt-count mono", "");
  const submitReviewBtn = h("button", "mini-btn adopt-submit", "✓ 提交评审");
  submitReviewBtn.id = "btnSubmitReview";
  submitReviewBtn.onclick = submitAdoptReview;
  const adoptAllBtn = miniBtn("全选/清空", () => {
    if (S.adoptSel.size >= cases.length) cases.forEach((c) => S.adoptSel.delete(c.case_id));
    else cases.forEach((c) => c.case_id && S.adoptSel.add(c.case_id));
    renderRows();
    refreshAdoptUI();
  }, "全选或清空采纳勾选");
  adoptBar.append(adoptCount, adoptAllBtn, submitReviewBtn);
  card.appendChild(adoptBar);

  // 表格
  const wrap = h("div", "t-wrap");
  const table = h("table", "t-table");
  table.appendChild(h("thead", null, "")).innerHTML =
    "<tr><th>采纳</th><th>标识</th><th>层级</th><th>优先级</th><th>类型</th><th>标题</th><th>所属模块</th><th>前置</th><th>步骤</th><th>预期</th><th>依据</th></tr>";
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
        Object.assign(document.createElement("td"), { colSpan: 11, className: "t-empty", textContent: "没有匹配的测试场景" }));
      count.textContent = "0 条";
      return;
    }
    shown.forEach((c) => {
      const tr = h("tr");
      const tdAdopt = h("td");
      const adopt = h("input", "t-adopt-cb");
      adopt.type = "checkbox";
      adopt.title = "勾选采纳（= 评审通过）";
      adopt.checked = S.adoptSel.has(c.case_id);
      adopt.onchange = () => {
        if (adopt.checked) S.adoptSel.add(c.case_id);
        else S.adoptSel.delete(c.case_id);
        refreshAdoptUI();
      };
      tdAdopt.appendChild(adopt);
      tr.appendChild(tdAdopt);
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
  refreshAdoptUI();

  // 导出
  actions.appendChild(miniBtn("↓ CSV", () => exportTestCSV(cases), "导出 CSV（Excel 可开）"));
  actions.appendChild(miniBtn("↓ MD", () => exportTestMD(cases, report), "导出 Markdown（总-分结构）"));
  actions.appendChild(miniBtn("⧉ 复制", () => copyText(testToMD(cases, report), "测试用例文档已复制")));
  if (cases.length) {
    actions.appendChild(miniBtn("☰ 沉淀", () => openDistill(report), "勾选有效用例，AI 归纳为业务检查清单入库"));
  }

  // test_run 会把同一份报告再推一次（回填执行统计）：已有卡片就原地替换，不重复插卡
  if (S.testCardEl && S.testCardEl.isConnected) S.testCardEl.replaceWith(card);
  else feedAppend(card);
  S.testCardEl = card;
  renderExportBar();
  return card;
}

/* 采纳工具条状态：计数 + 提交按钮只在终审门禁待决时可点 */
function refreshAdoptUI() {
  const bar = document.querySelector(".t-adopt");
  if (!bar) return;
  const total = (S.report?.test_cases || []).filter((c) => c && c.case_id).length;
  bar.querySelector(".t-adopt-count").textContent = `已采纳 ${S.adoptSel.size} / ${total}`;
  const btn = bar.querySelector(".adopt-submit");
  const ready = S.gate === "human_review";
  btn.disabled = !ready || !S.adoptSel.size;
  btn.title = ready
    ? "勾选采纳的用例即视为评审通过，一键提交"
    : "终审门禁就绪后（测试执行完成）可提交";
}

/* 提交采纳 = 终审自动通过：服务端校验正停在 human_review 门禁才接受 */
async function submitAdoptReview() {
  if (!S.tid || !S.report) return;
  const adopted = (S.report.test_cases || [])
    .map((c) => c.case_id).filter((id) => id && S.adoptSel.has(id));
  if (!adopted.length) { toast("请先勾选要采纳的用例", "采纳即评审通过；要整体驳回请用终审门禁的「驳回」"); return; }
  if (S.gate !== "human_review") { toast("终审门禁尚未就绪", "等测试执行完成、人工验收卡弹出后即可一键提交"); return; }
  try {
    const resp = await api(`/api/sessions/${S.tid}/review/adopt`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case_ids: adopted }),
    });
    $("gateModal").classList.add("hidden");
    $("gatePill").classList.add("hidden");
    S.gate = null;
    refreshAdoptUI();
    addDivider(`评审通过 · 已采纳 ${resp.adopted.length} 条用例`, "可继续沉淀为业务清单", true);
    S.pendingDistill = resp.adopted || [];
    setRunning(true);
    attachEvents();
    toast("评审已通过", `采纳 ${resp.adopted.length} 条用例；完成后可沉淀为业务清单`);
  } catch (err) {
    toast("提交失败", err.message, "err");
  }
}

/* 沉淀建议卡：终审通过后服务端主动询问（AI 归纳 / 手写入库 / 暂不） */
function addDistillPrompt(adoptedIds) {
  if (S.distillPromptEl && S.distillPromptEl.isConnected) return;
  const card = h("div", "card dp-card");
  const head = h("div", "card-head");
  head.appendChild(h("span", "card-title", "ASSET · 沉淀建议"));
  card.appendChild(head);
  const bodyEl = h("div", "dp-body");
  const n = (adoptedIds || []).length;
  bodyEl.appendChild(h("div", null, n
    ? `本轮已采纳 ${n} 条用例。要把它们归纳为业务检查清单入库吗？入库后下次同类需求会自动路由参照。`
    : "评审已通过。可以把本轮用例归纳为业务检查清单入库，供日后同类需求参照；也支持手写清单。"));
  const actions = h("div", "dp-actions");
  actions.appendChild(miniBtn("✦ AI 归纳入库", () => { removeDistillPrompt(); openDistill(S.report, n ? adoptedIds : null); },
    n ? "预勾选已采纳的用例，AI 归纳后预览入库" : "AI 归纳全部用例后预览入库"));
  actions.appendChild(miniBtn("✍ 手写清单入库", () => { removeDistillPrompt(); openManualModal(); }, "按库规范手写，AI 规范化后入库"));
  actions.appendChild(miniBtn("暂不", async () => {
    removeDistillPrompt();
    try { await api(`/api/sessions/${S.tid}/distill/dismiss`, { method: "POST" }); } catch { /* 忽略：仅影响恢复后是否再提示 */ }
  }, "本会话不再提示"));
  bodyEl.appendChild(actions);
  card.appendChild(bodyEl);
  feedAppend(card);
  S.distillPromptEl = card;
}

function removeDistillPrompt() {
  if (S.distillPromptEl && S.distillPromptEl.isConnected) S.distillPromptEl.remove();
  S.distillPromptEl = null;
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
  bar.appendChild(miniBtn("↓ Markdown 汇总", async () => {
    const resp = await fetch(`/api/sessions/${S.tid}/export?format=md`);
    download(`devflow-${S.tid}.md`, await resp.text(), "text/markdown;charset=utf-8");
    toast("已导出", "Markdown 汇总");
  }));
  bar.appendChild(miniBtn("↓ JSON", async () => {
    const resp = await fetch(`/api/sessions/${S.tid}/export?format=json`);
    download(`devflow-${S.tid}.json`, JSON.stringify(await resp.json(), null, 2), "application/json");
    toast("已导出", "结构化 JSON");
  }));
  if (S.report) bar.appendChild(miniBtn("↓ 测试 CSV", () => exportTestCSV((S.report.test_cases || []).map(normCase))));
  card.appendChild(bar);
  feedAppend(card);
}

/* ── 运行提交 + 事件订阅 ───────────────────────────────
   POST 提交立即返回；进度统一走 GET /events 订阅：
   服务端按游标(seq)回放缓冲 + 实时 tail，断线自动重连补齐，浏览器关闭后
   回来打开会话也能接上正在推进的流程（run 在服务端继续，与客户端无关）。 */

let evtAbort = null, sawEnd = false, evtBackoff = 350, evtGotAny = false;

function stopEvents() {
  if (evtAbort) { evtAbort.abort(); evtAbort = null; }
}

/* 提交一次推进（用户消息 / 门禁决策）。成功后立即挂事件流。 */
async function startRun(params) {
  if (!S.tid) return false;
  const isGate = params.op === "gate";
  let fields = null;
  if (params.fields) { try { fields = JSON.parse(params.fields); } catch { fields = null; } }
  const url = isGate ? `/api/sessions/${S.tid}/gates` : `/api/sessions/${S.tid}/messages`;
  const body = JSON.stringify(isGate
    ? {
        decision: params.decision,
        comment: params.comment || null,
        selected: params.selected ? params.selected.split(",").filter(Boolean) : null,
        fields,
      }
    : { text: params.text || "" });
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (resp.status === 409) {
      // 服务端该会话已有 run 在跑（如另一端提交/重启续跑）：直接接上实时进度
      toast("该会话正在推进中", "已接上正在进行的实时进度");
      setRunning(true);
      attachEvents();
      return false;
    }
    if (!resp.ok) {
      let detail = "";
      try { detail = (await resp.text()).slice(0, 140); } catch { /* 忽略 */ }
      toast(`提交失败（HTTP ${resp.status}）`, detail || "服务返回错误", "err");
      setRunning(false); updateComposer();
      return false;
    }
  } catch (err) {
    toast("连接中断", "与服务器的连接失败，请检查服务是否在运行", "err");
    setRunning(false); updateComposer();
    return false;
  }
  attachEvents();
  return true;
}

/* 订阅当前会话事件流；连接断开自动按游标重连续看，直到收到 stream_end。 */
function attachEvents() {
  stopEvents();
  sawEnd = false;
  const ctrl = new AbortController();
  evtAbort = ctrl;

  const deliverFrame = (frame) => {
    const line = frame.split("\n").find((l) => l.startsWith("data:"));
    if (!line) return;  // ": ping" 保活注释行
    let e; try { e = JSON.parse(line.slice(5).trim()); } catch { return; }
    if (e.type === "stream_meta") { S.serverRunning = !!e.running; return; }
    if (typeof e.seq === "number") S.lastSeq = Math.max(S.lastSeq, e.seq);
    evtGotAny = true;
    if (e.type === "stream_end") { sawEnd = true; stopEvents(); onStreamEnd(e); return; }
    onEvent(e);
  };

  (async () => {
    while (!ctrl.signal.aborted) {
      evtGotAny = false;
      try {
        const resp = await fetch(`/api/sessions/${S.tid}/events?after=${S.lastSeq}`, { signal: ctrl.signal });
        if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
        evtBackoff = 350;
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            const frame = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            if (frame.trim()) deliverFrame(frame);
          }
        }
      } catch (err) {
        if (err && err.name === "AbortError") return;  // 主动 detach（换会话/收尾）
      }
      if (sawEnd || ctrl.signal.aborted) return;
      if (!evtGotAny && S.serverRunning === false) {
        // 服务端无进行中的 run，回放也已完整：不再空转重连
        setRunning(false); updateComposer();
        return;
      }
      // 断线：指数退避重连，服务端按游标补发丢失事件
      await new Promise((r) => setTimeout(r, evtBackoff));
      evtBackoff = Math.min(8000, evtBackoff * 2);
    }
  })();
}

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
        const d = addDivider(`${e.label} ✓`, dur);
        d.dataset.node = e.node;
        attachRevertBtn(d);
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
        if (m.id) {
          if (S.msgIds.has(m.id)) return;  // 截断时整批重发保留消息：按 id 去重
          S.msgIds.add(m.id);
        }
        if (m.type === "human") addUserMsg(c, m.id);
        else addAiMsg(c, null, m.id);
      });
      break;
    case "question":
      addAsk(e.missing || e.questions || []);
      break;
    case "provider":
      // 供应商切换 / 使用 / 停用可见化：失效兜底一目了然，不再只有服务端日志知道
      if (e.status === "skip") {
        addSysLine(`↯ 供应商 ${e.provider} ${e.ctx || ""}失败（${e.code}）→ 自动切换下一个`, "warn");
      } else if (e.status === "disabled") {
        addSysLine(`⛔ 供应商 ${e.provider} 鉴权失败已停用（${String(e.reason || "").slice(0, 60)}）；检测通过后自动恢复`, "warn");
      } else {
        addSysLine(`✓ 本次调用由 ${e.provider} · ${e.model} 完成`, "ok");
      }
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
  loadHistory();  // 刷新回退锚点（本轮新落盘的步骤变为可回退点）
  if (S.gate) {
    const gateStage = S.gate === "graph_review" ? "graph_review"
      : S.gate === "graph_type_select" ? "graph"
      : S.gate === "requirement_review" ? "requirement_review"
      : S.gate === "checklist_route" ? "test" : "review";
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
  // 终审通过后服务端主动询问沉淀（采纳提交与门禁通过两条路径都汇到这里）
  if (S.pendingDistill !== null) {
    const adopted = S.pendingDistill;
    S.pendingDistill = null;
    if (S.stage === "done" && S.testCardEl && S.testCardEl.isConnected) addDistillPrompt(adopted);
  }
}

/* ── 门禁 ────────────────────────────────────────────── */

const GATE_META = {
  requirement_review: { n: 0, tag: "GATE · 需求确认", title: "需求确认：这些字段准确吗？" },
  graph_type_select: { n: 0, title: "制图前 · 选择逻辑图种类" },
  graph_review: { n: 1, title: "制图评审：逻辑图与需求对齐了吗？" },
  human_review: { n: 2, title: "人工验收：产物达到验收标准了吗？" },
  checklist_route: { n: 0, tag: "GATE · 清单路由", title: "业务清单路由：确认要注入的检查清单" },
};

const GRAPH_TYPE_ICONS = { flowchart: "⎯>", sequence: "⇄", state: "◉", er: "▤", journey: "☺" };
const GRAPH_TYPE_LABELS = { flowchart: "流程图", sequence: "时序图", state: "状态图", er: "ER 图", journey: "用户旅程图" };

function gateSubText(gate) {
  if (gate === "requirement_review") {
    return "AI 从描述中提炼的字段已标出 · 可直接修改后确认 · 确认后才会进入制图；驳回则先补充需求";
  }
  if (gate === "graph_type_select") {
    return "选择将决定制图视角与结构化产物形态 · 选定后本次需求内不再重复询问";
  }
  if (gate === "checklist_route") {
    if (!(S.gatePayload.candidates || []).length) {
      const n = S.gatePayload.business_count || 0;
      return n
        ? `清单库有 ${n} 个业务，但都与本需求不匹配 · 可上传清单文档入库，或跳过直接生成`
        : "清单库还是空的 · 可上传清单文档入库，或跳过直接生成用例";
    }
    return "AI 已按需求匹配业务清单 · 取消勾选即不加载 · 确认后清单作为用例设计依据";
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
  $("gateTag").textContent = meta.tag || (meta.n ? `GATE ${meta.n}/2` : "GATE · 制图选项");
  $("gateTitle").textContent = meta.title;
  $("gateSub").textContent = gateSubText(gate);
  $("gateComment").value = "";
  $("gateCommentWrap").classList.add("hidden");
  $("btnGateSubmit").classList.add("hidden");
  $("btnGatePeek").classList.add("hidden");

  const body = $("gateBody");
  body.innerHTML = "";
  if (gate === "requirement_review") {
    // 需求确认门禁：字段可编辑，「确认需求」进制图 /「驳回」退回补充需求
    $("btnApprove").textContent = "确认需求";
    $("btnReject").textContent = "驳回，先补充需求";
    $("btnReject").classList.remove("hidden");
    $("btnApprove").classList.remove("hidden");
    S.rrEdits = {};
    body.appendChild(gateRequirementBody(S.gatePayload));
  } else if (gate === "graph_type_select") {
    $("btnReject").classList.add("hidden");
    $("btnApprove").classList.add("hidden");
    body.appendChild(gateTypeBody(S.gatePayload));
  } else if (gate === "checklist_route") {
    // 清单路由确认：★预选勾选树，「通过」=确认加载所选，「驳回」=跳过注入
    const cands = S.gatePayload.candidates || [];
    if (!cands.length) {
      // 空库 / 无匹配：清单环节不再静默，给出上传入库入口
      $("btnApprove").textContent = "跳过，直接生成用例";
      $("btnReject").classList.add("hidden");
      body.appendChild(gateChecklistEmptyBody(S.gatePayload));
    } else {
      S.clSelected = new Set(collectSuggested(cands));
      $("btnApprove").textContent = `确认加载（${S.clSelected.size}）`;
      $("btnReject").textContent = "跳过，不注入清单";
      $("btnApprove").classList.remove("hidden");
      $("btnReject").classList.remove("hidden");
      body.appendChild(gateChecklistBody(S.gatePayload));
    }
  } else {
    $("btnReject").textContent = "驳回";
    $("btnApprove").textContent = "通过";
    $("btnReject").classList.remove("hidden");
    $("btnApprove").classList.remove("hidden");
    $("btnGatePeek").classList.remove("hidden");
    if (gate === "graph_review") body.appendChild(gateGraphBody(S.gatePayload));
    else body.appendChild(gateReviewBody(S.gatePayload));
    if (gate === "human_review") {
      body.appendChild(h("p", "cl-hint",
        "提示：可先在测试卡逐条勾选「采纳」，再点测试卡上的「✓ 提交评审」一键通过。"));
    }
  }

  $("gateModal").classList.remove("hidden");
  $("gatePill").classList.add("hidden");
  setStep(stepIdxForStage(gate === "graph_type_select" ? "graph" : gate), false);
  refreshAdoptUI();
  updateComposer();
}

/* 清单路由：收集 AI 预选（suggested）的 rel_dir（业务 + 子业务一起预选） */
function collectSuggested(candidates, out = []) {
  (candidates || []).forEach((c) => {
    if (c.suggested) out.push(c.rel_dir);
    collectSuggested(c.children || [], out);
  });
  return out;
}

/* 需求确认卡（制图前门禁）：逐字段可编辑，AI 推断字段高亮；只回传被改动的字段 */
function gateRequirementBody(p) {
  const wrap = h("div", "rr-wrap");
  const list = h("div", "rr-list");
  (p.fields || []).forEach((f) => {
    const row = h("div", "rr-row" + (f.inferred ? " inferred" : ""));
    const head = h("div", "rr-head");
    head.appendChild(h("span", "rr-label", f.label || f.key));
    if (f.inferred) head.appendChild(h("span", "rr-badge", "AI 推断"));
    head.appendChild(h("span", "rr-key mono", f.key));
    row.appendChild(head);
    row.appendChild(rrControl(f));
    list.appendChild(row);
  });
  wrap.appendChild(list);
  wrap.appendChild(h("p", "rr-hint",
    "标「AI 推断」的是模型从描述里提炼的内容，请重点核对；"
    + "修改任一字段后点「确认需求」，只有改动的字段会回传并覆盖。"));
  return wrap;
}

/* 字段控件：text 多行 / str 单行 / list 每行一项 / bool 勾选 */
function rrControl(f) {
  const kind = f.kind || "str";
  const norm = () => {
    if (kind === "bool") return f.value === true;
    if (kind === "list") return Array.isArray(f.value) ? f.value : [];
    return f.value == null ? "" : String(f.value);
  };
  const push = (v) => {
    if (JSON.stringify(v) === JSON.stringify(norm())) delete S.rrEdits[f.key];
    else S.rrEdits[f.key] = v;
  };
  if (kind === "bool") {
    const cb = h("input", "rr-bool");
    cb.type = "checkbox";
    cb.checked = norm();
    cb.onchange = () => push(cb.checked);
    return cb;
  }
  if (kind === "list") {
    const ta = h("textarea", "rr-input");
    ta.rows = Math.min(6, Math.max(2, norm().length || 2));
    ta.value = norm().join("\n");
    ta.placeholder = "每行一项";
    ta.oninput = () => push(ta.value.split("\n").map((s) => s.trim()).filter(Boolean));
    return ta;
  }
  if (kind === "text") {
    const ta = h("textarea", "rr-input");
    ta.rows = 3;
    ta.value = norm();
    ta.oninput = () => push(ta.value);
    return ta;
  }
  const inp = h("input", "rr-input");
  inp.type = "text";
  inp.value = norm();
  inp.oninput = () => push(inp.value);
  return inp;
}

/* 清单路由确认树：业务/子业务两级勾选，勾业务联动全选子业务 */
function gateChecklistBody(p) {
  const wrap = h("div", "cl-route");
  const root = p.root ? h("div", "cl-root mono", `清单库：${p.root}`) : null;
  if (root) wrap.appendChild(root);
  const list = h("div", "cl-list");
  (p.candidates || []).forEach((biz) => {
    list.appendChild(clCheckbox(biz, 0, (on) => {
      // 业务勾选联动子业务
      (biz.children || []).forEach((sub) => setClSelected(sub.rel_dir, on));
    }));
    (biz.children || []).forEach((sub) => list.appendChild(clCheckbox(sub, 1)));
  });
  wrap.appendChild(list);
  wrap.appendChild(h("p", "cl-hint", "勾选业务会联动其子业务；可单独取消某个子业务。确认后清单内容将注入测试设计，并在用例卡片标注来源。"));
  return wrap;
}

function clCheckbox(node, depth, onChange) {
  const row = h("label", "cl-item" + (depth ? " sub" : ""));
  const cb = h("input");
  cb.type = "checkbox";
  cb.checked = S.clSelected.has(node.rel_dir);
  cb.dataset.rel = node.rel_dir;
  cb.onchange = () => {
    setClSelected(node.rel_dir, cb.checked);
    if (onChange) onChange(cb.checked);
    $("btnApprove").textContent = `确认加载（${S.clSelected.size}）`;
  };
  row.appendChild(cb);
  const text = h("span", "cl-text");
  text.appendChild(h("b", null, node.name || node.rel_dir));
  text.appendChild(h("span", "cl-dir mono", node.rel_dir));
  row.appendChild(text);
  if (node.description) row.appendChild(h("span", "cl-desc", node.description));
  if (node.reason) row.appendChild(h("span", "cl-reason mono", "↳ " + node.reason));
  return row;
}

function setClSelected(rel, on) {
  if (on) S.clSelected.add(rel);
  else S.clSelected.delete(rel);
}

/* 清单路由空态（空库/无匹配）：不再静默，给上传入库与库浏览入口 */
function gateChecklistEmptyBody(p) {
  const wrap = h("div", "cl-route");
  if (p.root) wrap.appendChild(h("div", "cl-root mono", `清单库：${p.root}`));
  const note = h("div", "cl-empty");
  note.appendChild(h("p", null, (p.business_count || 0)
    ? `库中有 ${p.business_count} 个业务类型，但都与当前需求不匹配。`
    : "清单库中还没有业务清单。"));
  note.appendChild(h("p", null,
    "有现成的业务检查清单（内部 wiki 页面、验收清单、历史用例文档）？上传后 AI 会按库规范归纳入库，确认后即可注入本单用例设计。"));
  const actions = h("div", "cl-empty-actions");
  actions.appendChild(miniBtn("📤 上传清单文档入库", () => openImportModal("gate"), "支持 .md / .txt / .docx"));
  actions.appendChild(miniBtn("☰ 查看清单库", () => openLibraryTree(), "浏览当前清单库"));
  note.appendChild(actions);
  wrap.appendChild(note);
  wrap.appendChild(h("p", "cl-hint", "跳过也能继续：本轮按常规流程设计用例，跑完后仍可在测试卡上沉淀。"));
  return wrap;
}

/* 清单库浏览：业务 → 子业务、条目数、路由描述（只读） */
async function openLibraryTree() {
  if (!S.tid) return;
  const body = $("libraryBody");
  body.innerHTML = "";
  body.appendChild(h("div", "cl-empty", "加载中…"));
  $("libraryModal").classList.remove("hidden");
  try {
    const projectRoot = String((S.req || {}).project_root || "").trim();
    const data = await api(`/api/library?project_root=${encodeURIComponent(projectRoot)}`);
    body.innerHTML = "";
    const tree = data.tree || [];
    if (!tree.length) {
      body.appendChild(h("div", "cl-empty",
        "清单库还是空的。可在清单路由卡上传文档入库，或跑完用例后在测试卡上沉淀。"));
    }
    tree.forEach((biz) => {
      body.appendChild(libRow(biz, 0));
      (biz.children || []).forEach((sub) => body.appendChild(libRow(sub, 1)));
    });
    if (data.root) body.appendChild(h("div", "dl-root mono", "清单库：" + data.root));
    const link = h("a", "lib-open-link", "在新页面打开完整清单 →");
    link.href = "/library?library_root=" + encodeURIComponent(data.root || "");
    link.target = "_blank";
    link.rel = "noopener";
    body.appendChild(link);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(h("div", "cl-empty", "加载失败：" + err.message));
  }
}

function libRow(node, depth) {
  const row = h("div", "lib-row" + (depth ? " sub" : ""));
  const head = h("div", "lib-head");
  head.appendChild(h("b", null, node.name || node.rel_dir));
  head.appendChild(h("span", "cl-dir mono", node.rel_dir));
  head.appendChild(h("span", "lib-count", node.has_checklist ? `${node.item_count} 条` : "无 checklist"));
  row.appendChild(head);
  if (node.description) row.appendChild(h("span", "cl-desc", node.description));
  return row;
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
  startRun({ op: "gate", decision: typeId, comment: "" });
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
    S.gate === "requirement_review" ? "需求确认待决策" :
    S.gate === "graph_type_select" ? "图种类待选择" :
    S.gate === "checklist_route" ? "清单路由待确认" :
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
  const isRoute = gate === "checklist_route";
  const isReqReview = gate === "requirement_review";
  // 清单路由门禁：approve/reject 映射为 confirm/skip，携带勾选的业务路径；
  // 空态（空库/无匹配）卡上「跳过，直接生成用例」也映射为 skip
  const routeEmpty = isRoute && !(S.gatePayload.candidates || []).length;
  const routeDecision = isRoute ? (decision === "approve" && !routeEmpty ? "confirm" : "skip") : null;
  const selected = isRoute ? [...(S.clSelected || [])].join(",") : "";
  // 需求确认门禁：approve/reject 映射为 confirm/reject，携带就地修改的字段
  const reqEdits = isReqReview ? (S.rrEdits || {}) : {};
  const editCount = Object.keys(reqEdits).length;
  const fields = editCount ? JSON.stringify(reqEdits) : "";
  $("gateModal").classList.add("hidden");
  $("gatePill").classList.add("hidden");
  S.gate = null;
  refreshAdoptUI();
  if (isRoute) {
    const n = routeDecision === "confirm" ? (S.clSelected || []).length : 0;
    addDivider(
      routeDecision === "confirm" ? `清单已确认 · 注入 ${n} 项`
        : routeEmpty ? "清单库暂无匹配 · 已跳过注入" : "已跳过清单注入",
      null, true,
    );
  } else if (isReqReview) {
    if (decision === "approve") {
      addDivider(
        editCount ? `需求已确认 · 修改 ${editCount} 个字段` : "需求已确认 · 进入制图",
        editCount ? reqEdits : null, true,
      );
    } else {
      addDivider("需求未确认 · 退回补充", comment ? `「${comment.slice(0, 40)}」` : null, true);
    }
  } else {
    if (gate === "human_review" && decision === "approve") {
      // 终审通过（未走采纳提交）：之后也弹沉淀建议（无预勾选 = 全部用例）
      S.pendingDistill = [];
    }
    addDivider(decision === "approve" ? "评审通过 · 继续推进" : "已驳回 · 意见回传",
      comment ? `「${comment.slice(0, 40)}${comment.length > 40 ? "…" : ""}」` : null, true);
  }
  if (decision === "approve") setStep(stepIdxForStage(gate) + 1, true);
  setRunning(true);
  startRun(isRoute
    ? { op: "gate", decision: routeDecision, comment: "", selected }
    : isReqReview
      ? { op: "gate", decision: decision === "approve" ? "confirm" : "reject", comment: comment || "", fields }
      : { op: "gate", decision, comment: comment || "" });
  toast(
    isRoute ? (routeDecision === "confirm" ? "清单已加载" : "已跳过清单")
      : isReqReview ? (decision === "approve"
        ? (editCount ? `已确认并按修改后的清单制图（${editCount} 项）` : "需求已确认，开始制图")
        : "需求已退回，请补充后继续")
        : decision === "approve" ? "已通过" : "意见已回传",
    isRoute ? (routeDecision === "confirm" ? "业务检查清单将作为用例设计依据" : "按常规流程设计用例")
      : isReqReview ? (decision === "approve" ? "确认内容将作为制图与用例设计依据" : "补充需求后会重新澄清并再次请你确认")
        : decision === "approve" ? "流程继续推进" : "正在按意见重新执行",
  );
}

/* ── 沉淀 Checklist（用例 → 业务清单入库）────────────── */

const REL_DIR_RE = /^[A-Za-z0-9][A-Za-z0-9_-]*(\/[A-Za-z0-9][A-Za-z0-9_-]*)*$/;

/* 业务类型选择区（沉淀 / 文档导入 / 手写清单三种弹窗共用）：
   已有业务下拉（含子业务/条目数）或新建（英文目录 / 中文名 / 路由描述） */
function bizSectionEl(tree, rootLabel) {
  const bizSec = h("div", "dl-sec");
  bizSec.appendChild(h("h4", null, "业务类型（清单将登记到该目录）"));
  const sel = h("select", "dl-select");
  sel.appendChild(new Option("— 选择已有业务 —", ""));
  (tree || []).forEach((biz) => {
    sel.appendChild(new Option(`${biz.rel_dir} · ${biz.name}${biz.item_count ? `（${biz.item_count} 条）` : "（空）"}`, biz.rel_dir));
    (biz.children || []).forEach((sub) => {
      sel.appendChild(new Option(`  ↳ ${sub.rel_dir} · ${sub.name}${sub.item_count ? `（${sub.item_count} 条）` : "（空）"}`, sub.rel_dir));
    });
  });
  sel.appendChild(new Option("＋ 新建业务类型…", "__new__"));
  bizSec.appendChild(sel);
  const newWrap = h("div", "dl-new hidden");
  const dirIn = h("input", "dl-input mono");
  dirIn.placeholder = "英文目录名，如 refund 或 payment/chargeback";
  const nameIn = h("input", "dl-input");
  nameIn.placeholder = "业务中文名，如 退款子业务";
  const descIn = h("input", "dl-input");
  descIn.placeholder = "一句话路由描述：什么需求应路由到此（AI 也会辅助归纳）";
  newWrap.append(dirIn, nameIn, descIn);
  sel.onchange = () => newWrap.classList.toggle("hidden", sel.value !== "__new__");
  bizSec.appendChild(newWrap);
  if (rootLabel) bizSec.appendChild(h("div", "dl-root mono", "清单库：" + rootLabel));
  return { bizSec, sel };
}

/* 从弹窗表单收集业务归属：{business} 或 {err} */
function collectBusiness(tree, formEl) {
  const sel = formEl.querySelector(".dl-select");
  if (sel.value === "__new__") {
    const [dirIn, nameIn, descIn] = formEl.querySelectorAll(".dl-new .dl-input");
    const rel = dirIn.value.trim();
    if (!REL_DIR_RE.test(rel)) {
      return { err: "业务目录名非法：限英文/数字/连字符，可用 / 表示子业务（如 refund 或 payment/chargeback）" };
    }
    return { business: { rel_dir: rel, name: nameIn.value.trim(), description: descIn.value.trim() } };
  }
  if (sel.value) {
    const meta = (tree || []).flatMap((b) => [b, ...(b.children || [])]).find((x) => x.rel_dir === sel.value);
    return { business: { rel_dir: sel.value, name: meta?.name || "" } };
  }
  return { err: "请先选择业务类型，或新建一个" };
}

async function fetchChecklistTree() {
  const treeData = { root: "", tree: [] };
  try {
    const data = await api(`/api/sessions/${S.tid}/checklist-tree`);
    treeData.tree = data.tree || [];
    treeData.root = data.root || "";
  } catch { /* 库树拉取失败不阻塞：仍可新建业务 */ }
  return treeData;
}

async function openDistill(report, preselectIds = null) {
  const cases = (report.test_cases || []).filter((c) => c && c.case_id);
  if (!cases.length || !S.tid) return;
  S.distill = { report, cases, preview: null, formEl: null, prevEl: null, preselect: preselectIds };
  const treeData = await fetchChecklistTree();
  S.distill.tree = treeData.tree;
  S.distill.root = treeData.root;
  buildDistillBody();
  $("distillModal").classList.remove("hidden");
}

function closeDistill() {
  $("distillModal").classList.add("hidden");
  S.distill = null;
}

function buildDistillBody() {
  const d = S.distill;
  const body = $("distillBody");
  body.innerHTML = "";
  d.formEl = distillFormEl();
  d.prevEl = h("div");
  d.prevEl.classList.add("hidden");
  body.appendChild(d.formEl);
  body.appendChild(d.prevEl);
  distillShowForm(true);
}

function distillShowForm(form) {
  const d = S.distill;
  d.formEl.classList.toggle("hidden", !form);
  d.prevEl.classList.toggle("hidden", form);
  $("btnDistillGen").classList.toggle("hidden", !form);
  $("btnDistillBack").classList.toggle("hidden", form);
  $("btnDistillCommit").classList.toggle("hidden", form || !d.preview);
  $("distillHint").textContent = "";
}

function distillFormEl() {
  const d = S.distill;
  const wrap = h("div", "dl-form");

  // ── 业务类型 ──
  const { bizSec } = bizSectionEl(d.tree, d.root);
  wrap.appendChild(bizSec);

  // ── 用例勾选（有采纳集时只预勾选采纳的用例）──
  const pre = Array.isArray(d.preselect) ? d.preselect : null;
  const caseSec = h("div", "dl-sec");
  const caseHead = h("div", "dl-case-head");
  caseHead.appendChild(h("h4", null, pre
    ? `有效用例（${d.cases.length} · 已预勾选采纳的 ${pre.length} 条）`
    : `有效用例（${d.cases.length}）`));
  const allBtn = miniBtn("全选/清空", () => {
    const boxes = caseList.querySelectorAll("input");
    const target = ![...boxes].every((b) => b.checked);
    boxes.forEach((b) => { b.checked = target; });
  });
  caseHead.appendChild(allBtn);
  caseSec.appendChild(caseHead);
  const caseList = h("div", "dl-cases");
  d.cases.forEach((c) => {
    const row = h("label", "dl-case");
    const cb = h("input");
    cb.type = "checkbox";
    cb.checked = !pre || pre.includes(c.case_id);
    cb.value = c.case_id;
    row.appendChild(cb);
    row.appendChild(h("span", "mono", c.case_id));
    row.appendChild(h("span", "prio " + String(c.priority || "P2").toUpperCase(), String(c.priority || "P2").toUpperCase()));
    row.appendChild(h("span", "dl-case-title", c.title || "—"));
    caseList.appendChild(row);
  });
  caseSec.appendChild(caseList);
  wrap.appendChild(caseSec);
  return wrap;
}

function collectDistillRequest() {
  const d = S.distill;
  const biz = collectBusiness(d.tree, d.formEl);
  if (biz.err) return biz;
  const caseIds = [...d.formEl.querySelectorAll(".dl-cases input:checked")].map((b) => b.value);
  if (!caseIds.length) return { err: "至少勾选一条有效用例" };
  return { business: biz.business, case_ids: caseIds };
}

async function genDistill() {
  const req = collectDistillRequest();
  if (req.err) { $("distillHint").textContent = req.err; return; }
  const btn = $("btnDistillGen");
  btn.disabled = true;
  btn.textContent = "✦ 归纳中…";
  $("distillHint").textContent = "";
  try {
    const preview = await api(`/api/sessions/${S.tid}/checklist/distill`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    S.distill.preview = preview;
    renderDistillPreviewInto(S.distill.prevEl, preview);
    distillShowForm(false);
  } catch (err) {
    $("distillHint").textContent = "归纳失败：" + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "✦ 生成预览";
  }
}

/* 双栏预览（scenario.md / checklist.md）：沉淀、导入、手写三种弹窗共用 */
function renderDistillPreviewInto(el, p) {
  el.innerHTML = "";
  const head = h("div", "dl-pv-head");
  head.appendChild(h("span", "dl-mode", p.mode === "merge" ? "合并模式（保留已有条目并去重）" : "新建模式"));
  head.appendChild(h("span", "mono", p.case_count
    ? `${p.rel_dir} · ${p.case_count} 条用例`
    : `${p.rel_dir}`));
  el.appendChild(head);
  if (p.merge_notes) el.appendChild(h("div", "dl-notes", "合并说明：" + p.merge_notes));
  const grid = h("div", "dl-pv-grid");
  [["scenario.md（路由标签）", p.scenario_md], ["checklist.md（检查清单）", p.checklist_md]].forEach(([title, md]) => {
    const box = h("div", "dl-pv-box");
    box.appendChild(h("h4", null, title));
    const pre = h("pre", "dl-pv-pre mono", md);
    box.appendChild(pre);
    grid.appendChild(box);
  });
  el.appendChild(grid);
}

async function commitPreview(modalHintId, preview) {
  await api(`/api/sessions/${S.tid}/checklist/commit`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      rel_dir: preview.rel_dir,
      scenario_md: preview.scenario_md,
      checklist_md: preview.checklist_md,
    }),
  });
}

async function commitDistill() {
  const d = S.distill;
  if (!d?.preview) return;
  const btn = $("btnDistillCommit");
  btn.disabled = true;
  try {
    await commitPreview("distillHint", d.preview);
    toast("已登记到清单库", `${d.preview.rel_dir}/checklist.md（下次生成自动路由可用）`);
    closeDistill();
  } catch (err) {
    $("distillHint").textContent = "写入失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
}

/* ── 上传清单文档入库（wiki / 验收清单 / 用例文档 → 按库规范归纳）── */

async function openImportModal(from) {
  if (!S.tid) return;
  const treeData = await fetchChecklistTree();
  S.import = { from, preview: null, formEl: null, prevEl: null, file: null,
    tree: treeData.tree, root: treeData.root };
  buildImportBody();
  $("importModal").classList.remove("hidden");
}

function closeImportModal() {
  $("importModal").classList.add("hidden");
  S.import = null;
}

function buildImportBody() {
  const d = S.import;
  const body = $("importBody");
  body.innerHTML = "";
  d.formEl = importFormEl();
  d.prevEl = h("div");
  d.prevEl.classList.add("hidden");
  body.append(d.formEl, d.prevEl);
  importShowForm(true);
}

function importShowForm(form) {
  const d = S.import;
  d.formEl.classList.toggle("hidden", !form);
  d.prevEl.classList.toggle("hidden", form);
  $("btnImportGen").classList.toggle("hidden", !form);
  $("btnImportBack").classList.toggle("hidden", form);
  $("btnImportCommit").classList.toggle("hidden", form || !d.preview);
  $("importHint").textContent = "";
}

function importFormEl() {
  const d = S.import;
  const wrap = h("div", "dl-form");
  const { bizSec } = bizSectionEl(d.tree, d.root);
  wrap.appendChild(bizSec);

  // ── 来源文档 ──
  const fileSec = h("div", "dl-sec");
  fileSec.appendChild(h("h4", null, "清单来源文档"));
  const drop = h("div", "im-drop");
  const fileIn = h("input");
  fileIn.type = "file";
  fileIn.accept = ".md,.txt,.docx";
  fileIn.hidden = true;
  const fileName = h("span", "im-file mono", "点击选择或拖入文件（.md / .txt / .docx，≤5MB）");
  const pick = (f) => { d.file = f; fileName.textContent = `📄 ${f.name}（${Math.ceil(f.size / 1024)}KB）`; };
  drop.append(fileName, fileIn);
  drop.onclick = () => fileIn.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove("over");
    if (e.dataTransfer.files.length) pick(e.dataTransfer.files[0]);
  };
  fileIn.onchange = () => { if (fileIn.files.length) pick(fileIn.files[0]); };
  fileSec.appendChild(drop);
  wrap.appendChild(fileSec);
  wrap.appendChild(h("p", "cl-hint",
    "AI 只提取文档中「可作为测试检查点」的业务规则，按库规范（8 分节 + 优先级）归纳；预览确认后才写库。"));
  return wrap;
}

async function genImport() {
  const d = S.import;
  const biz = collectBusiness(d.tree, d.formEl);
  if (biz.err) { $("importHint").textContent = biz.err; return; }
  if (!d.file) { $("importHint").textContent = "请先选择清单来源文档"; return; }
  const btn = $("btnImportGen");
  btn.disabled = true;
  btn.textContent = "✦ 解析中…";
  $("importHint").textContent = "";
  try {
    const fd = new FormData();
    fd.append("file", d.file);
    fd.append("rel_dir", biz.business.rel_dir);
    fd.append("name", biz.business.name || "");
    fd.append("description", biz.business.description || "");
    const preview = await api(`/api/sessions/${S.tid}/checklist/import`, { method: "POST", body: fd });
    d.preview = preview;
    renderDistillPreviewInto(d.prevEl, preview);
    importShowForm(false);
  } catch (err) {
    $("importHint").textContent = "解析失败：" + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "✦ 解析预览";
  }
}

async function commitImport() {
  const d = S.import;
  if (!d?.preview) return;
  const btn = $("btnImportCommit");
  btn.disabled = true;
  try {
    await commitPreview("importHint", d.preview);
    toast("清单文档已入库", `${d.preview.rel_dir}/checklist.md（溯源 import:${d.file?.name || ""}）`);
    const rel = d.preview.rel_dir;
    const from = d.from;
    closeImportModal();
    if (from === "gate" && S.gate === "checklist_route") await mergeImportIntoGate(rel);
  } catch (err) {
    $("importHint").textContent = "写入失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
}

/* 门禁里导入成功后：把新业务并入路由候选树并预选，用户确认即注入本单 */
async function mergeImportIntoGate(preselectRel) {
  try {
    const data = await api(`/api/sessions/${S.tid}/checklist-tree`);
    const mark = (n) => ({ ...n, suggested: n.rel_dir === preselectRel, children: undefined });
    const tree = data.tree || [];
    const existing = S.gatePayload.candidates || [];
    const existRels = new Set(existing.map((c) => c.rel_dir));
    const imported = tree.find((b) => b.rel_dir === preselectRel
      || (b.children || []).some((s) => s.rel_dir === preselectRel));
    if (imported && !existRels.has(imported.rel_dir)) {
      existing.push({
        rel_dir: imported.rel_dir, name: imported.name, description: imported.description,
        suggested: true, reason: "刚从文档导入",
        children: (imported.children || []).map(mark),
      });
    }
    S.gatePayload.candidates = existing;
    S.gatePayload.status = "matched";
    openGate("checklist_route", S.gatePayload);
    toast("已并入当前清单路由", "确认勾选后即可注入本单用例设计");
  } catch { /* 刷新失败不阻塞：用户仍可跳过 */ }
}

/* ── 手写 Checklist 入库（AI 规范化 + 去重）──────────── */

async function openManualModal() {
  if (!S.tid) return;
  const treeData = await fetchChecklistTree();
  S.manual = { preview: null, formEl: null, prevEl: null, tree: treeData.tree, root: treeData.root };
  buildManualBody();
  $("manualModal").classList.remove("hidden");
}

function closeManualModal() {
  $("manualModal").classList.add("hidden");
  S.manual = null;
}

function buildManualBody() {
  const d = S.manual;
  const body = $("manualBody");
  body.innerHTML = "";
  d.formEl = manualFormEl();
  d.prevEl = h("div");
  d.prevEl.classList.add("hidden");
  body.append(d.formEl, d.prevEl);
  manualShowForm(true);
}

function manualShowForm(form) {
  const d = S.manual;
  d.formEl.classList.toggle("hidden", !form);
  d.prevEl.classList.toggle("hidden", form);
  $("btnManualGen").classList.toggle("hidden", !form);
  $("btnManualBack").classList.toggle("hidden", form);
  $("btnManualCommit").classList.toggle("hidden", form || !d.preview);
  $("manualHint").textContent = "";
}

function manualFormEl() {
  const d = S.manual;
  const wrap = h("div", "dl-form");
  const { bizSec } = bizSectionEl(d.tree, d.root);
  wrap.appendChild(bizSec);
  const sec = h("div", "dl-sec");
  sec.appendChild(h("h4", null, "清单内容（markdown）"));
  const ta = h("textarea", "dl-input manual-text");
  ta.rows = 12;
  ta.placeholder = "## 正向\n- [P0] 正常下单并用可用余额完成支付，订单状态流转为已支付\n\n## 反向\n- [P0] 余额不足时支付被拒绝并给出明确提示，不产生脏订单\n\n## 边界值\n- [P1] 恰好等于订单金额的余额可支付成功\n\n（合法分节：正向/反向/边界值/等价类/状态流转/场景法/安全/性能；条目格式 - [P0/P1/P2] 可验证的一句话检查点）";
  sec.appendChild(ta);
  wrap.appendChild(sec);
  wrap.appendChild(h("p", "cl-hint", "不必严格符合规范：AI 会把内容归一到 8 分节与 P0-P2 优先级，并去除与已有清单重复的条目。"));
  return wrap;
}

async function genManual() {
  const d = S.manual;
  const biz = collectBusiness(d.tree, d.formEl);
  if (biz.err) { $("manualHint").textContent = biz.err; return; }
  const ta = d.formEl.querySelector(".manual-text");
  if (!ta.value.trim()) { $("manualHint").textContent = "请先写点清单内容"; return; }
  const btn = $("btnManualGen");
  btn.disabled = true;
  btn.textContent = "✦ 规范化中…";
  $("manualHint").textContent = "";
  try {
    const preview = await api(`/api/sessions/${S.tid}/checklist/distill`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "manual", text: ta.value, business: biz.business }),
    });
    d.preview = preview;
    renderDistillPreviewInto(d.prevEl, preview);
    manualShowForm(false);
  } catch (err) {
    $("manualHint").textContent = "规范化失败：" + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "✦ 规范化预览";
  }
}

async function commitManual() {
  const d = S.manual;
  if (!d?.preview) return;
  const btn = $("btnManualCommit");
  btn.disabled = true;
  try {
    await commitPreview("manualHint", d.preview);
    toast("手写清单已入库", `${d.preview.rel_dir}/checklist.md（下次生成自动路由可用）`);
    closeManualModal();
  } catch (err) {
    $("manualHint").textContent = "写入失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
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
      const li = h("li", "sess" + (s.thread_id === S.tid ? " active" : "") + (s.running ? " running" : ""));
      li.appendChild(h("div", "sess-title", s.title || "（未命名需求）"));
      const meta = h("div", "sess-meta");
      meta.appendChild(h("span", "sess-stage" + (s.thread_id === S.tid ? " on" : ""), stageName(s.stage) || s.stage));
      if (s.has_graph) meta.appendChild(h("span", "sess-graph", "◆ 已有逻辑图"));
      if (s.running) meta.appendChild(h("span", "sess-run", "● 运行中"));
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
  // 换会话随时允许：旧会话若有 run 在推进，服务端会继续跑完，回来时按游标续看
  stopEvents();
  try {
    const snap = await api(`/api/sessions/${tid}`);
    resetFeed();
    // 先清掉上一状态的弹窗：回退/切会话后新状态可能没有挂起门禁，旧弹窗不能残留
    $("gateModal").classList.add("hidden");
    $("gatePill").classList.add("hidden");
    S.tid = tid;
    localStorage.setItem("df-last-tid", tid);
    updateCfgBtn();
    S.gate = null; S.graph = null; S.report = null;
    S.history = [];
    S.adoptSel = new Set();
    S.pendingDistill = null;
    S.distillPromptEl = null;
    S.lastSeq = Number(snap.last_seq || 0);
    const vals = snap.values || {};
    S.req = vals.requirement || {};
    // 先取回退锚点：气泡下方的「回到此处」要用锚点数据把消息定位到步骤
    await loadHistory();
    // 回放对话
    (vals.messages || []).forEach((m) => {
      const c = String(m.content || "").trim();
      if (!c) return;
      if (m.id) S.msgIds.add(m.id);
      if (m.type === "human") addUserMsg(c, m.id); else addAiMsg(c, null, m.id);
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
    if (next.includes("requirement_review")) {
      // 需求确认门禁：载荷由服务端按 state.requirement 重算（interrupt 载荷不落 checkpoint）
      try {
        const r = await api(`/api/sessions/${tid}/requirement-review`);
        if (r.pending) openGate("requirement_review", r);
      } catch { /* 载荷重算失败不阻塞会话打开 */ }
    } else if (next.includes("graph_type_select")) {
      // 图种类选择门禁：候选由服务端按同一套规则推断重算（interrupt 载荷不落 checkpoint）
      try {
        const cand = await api(`/api/sessions/${tid}/graph-type-candidates`);
        if (cand.pending) openGate("graph_type_select", cand);
      } catch { /* 恢复候选失败不阻塞会话打开 */ }
    } else if (next.includes("checklist_route_gate")) {
      // 清单路由确认门禁：候选树已由 match 节点落 checkpoint，恢复端点直接回放
      try {
        const cand = await api(`/api/sessions/${tid}/checklist-candidates`);
        if (cand.pending) openGate("checklist_route", cand);
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
    // 已完成且未「暂不」的会话：恢复时补一张沉淀建议卡（AI 归纳 / 手写 / 暂不）
    if (S.stage === "done" && vals.test_report && !vals.distill_dismissed && !(snap.next || []).length) {
      addDistillPrompt(vals.adopted_cases || []);
    }
    // 断线/误关浏览器恢复：服务端仍在推进 → 立即接上实时进度（历史由快照回放）
    if (snap.running) {
      setRunning(true);
      attachEvents();
      addSysLine("● 流程正在服务端推进，已接上实时进度", "ok");
    } else {
      setRunning(false);
    }
    refreshSessions();
    closeSidebar();
    updateComposer();
  } catch (err) {
    toast("打开会话失败", err.message, "err");
    if (S.tid === tid) {
      S.tid = null;
      localStorage.removeItem("df-last-tid");
      showEmptyState();
      updateComposer();
    }
  }
}

/* ── 步骤回退（time-travel）─────────────────────────── */

/* 顶部步骤条 → 代表性回退锚点（回退到该节点刚完成，重跑其下游） */
const STAGE_REVERT_NODE = {
  clarify: "clarify_extract", graph: "graph_type_select", graph_review: "graph_generate",
  search: "code_search", code: "graph_render", test: "checklist_route_gate", review: "test_run",
};

async function loadHistory() {
  if (!S.tid) { S.history = []; return; }
  try {
    const r = await api(`/api/sessions/${S.tid}/history`);
    S.history = r.steps || [];
  } catch { S.history = []; }
}

function anchorForNode(node) {
  return S.history.find((s) => s.node === node) || null;  // 列表新→旧，取最近一次
}

/* 步骤分隔线上的「⟲ 从此重来」按钮 */
function attachRevertBtn(d) {
  const btn = h("button", "revert-dot", "⟲ 从此重来");
  btn.title = "回退到该步骤刚完成的时点，重跑之后的流程";
  btn.onclick = (e) => {
    e.stopPropagation();
    openRevertForNode(d.dataset.node);
  };
  d.appendChild(btn);
}

/* 消息 → 回退目标：找到最早包含这条消息的锚点，再往前一步 =
   这条消息发出前的存档（回退后该消息及其之后作废、重新执行）。
   锚点是节点级存档，history 新→旧；「最早包含」= 从旧往新第一个命中的下标。 */
function revertTargetForMessage(msgId) {
  if (!msgId) return null;
  for (let i = S.history.length - 1; i >= 0; i--) {
    if (!(S.history[i].message_ids || []).includes(msgId)) continue;
    return S.history[i + 1] || null;  // +1 = 时间上更早一步；没有则无存档可回
  }
  return null;
}

/* 兜底：本轮实时发出的用户气泡还没有服务端 id（消息写入 checkpoint 才有），
   按「第 k 条用户消息 ↔ 第 k 个 compress_messages 锚点」的位置关系定位。 */
function ordinalUserTarget(msgEl) {
  const idx = [...document.querySelectorAll("#feed .msg.me")].indexOf(msgEl);
  if (idx < 0) return null;
  const compressAnchors = [...S.history].filter((s) => s.node === "compress_messages").reverse();
  const turn = compressAnchors[idx];
  if (!turn) return null;
  const i = S.history.findIndex((s) => s.checkpoint_id === turn.checkpoint_id);
  return S.history[i + 1] || null;
}

function messageRevertTarget(m) {
  return revertTargetForMessage(m?.dataset?.msgId) || ordinalUserTarget(m);
}

/* 气泡下方的「⟲ 回到此处」 */
function revertToMessage(m, isUser) {
  if (S.running) { toast("流程推进中", "等当前步骤暂停后再回退", "err"); return; }
  const target = messageRevertTarget(m);
  if (!target) {
    toast("这条消息之前没有可回退的存档",
      isUser ? "可用「编辑重发」把内容放回输入框继续" : "请在更靠后的对话位置回退",
      "err");
    return;
  }
  openRevertModal(target, isUser ? {} : { notice: "这条回复将作废并重新生成。" });
}

function openRevertForNode(node) {
  const anchor = node && anchorForNode(node);
  if (!anchor) {
    toast("该步骤暂无可回退的存档", "流程还没执行到这一步，或存档尚未生成", "err");
    return;
  }
  openRevertModal(anchor);
}

async function openRevertModal(anchor, opts = {}) {
  if (!anchor) {
    toast("找不到可回退的存档点", "这条消息之前没有已落盘的步骤", "err");
    return;
  }
  if (S.running) {
    toast("流程推进中", "等当前步骤暂停（门禁或完成）后再回退", "err");
    return;
  }
  S.revertCheckpointId = anchor.checkpoint_id;
  S.rrEdits = {};
  const body = $("revertBody");
  body.innerHTML = "";
  const head = h("div", "rv-anchor");
  head.appendChild(h("b", null, `⟲ ${anchor.label || anchor.node}`));
  if (anchor.ts) head.appendChild(h("span", "mono", new Date(anchor.ts).toLocaleString()));
  body.appendChild(head);

  // 将作废并重跑的下游步骤（历史里比锚点更新的步骤，倒序展示 = 执行顺序）
  const i = S.history.findIndex((s) => s.checkpoint_id === anchor.checkpoint_id);
  const down = i >= 0 ? [...S.history.slice(0, i)].reverse() : [];
  const wrap = h("div", "rv-down");
  wrap.appendChild(h("div", "rv-down-title", down.length
    ? `以下 ${down.length} 步的产出将作废并重跑：`
    : "该步骤之后暂无已落盘的产出，确认后直接从这一步继续。"));
  if (down.length) {
    const ul = h("ul");
    down.forEach((s) => ul.appendChild(h("li", null, s.label || s.node)));
    wrap.appendChild(ul);
  }
  body.appendChild(wrap);

  // 需求就地编辑：改完随回退生效，下游按新需求执行（改需求重制图的关键入口）
  try {
    const rr = await api(`/api/sessions/${S.tid}/requirement-review`);
    if ((rr.fields || []).length) {
      const sec = h("div", "rv-req");
      sec.appendChild(h("div", "rv-req-title", "顺带修改需求（可选）— 只有改动的字段会覆盖："));
      sec.appendChild(gateRequirementBody(rr));
      body.appendChild(sec);
    }
  } catch { /* 需求载荷拉取失败不阻塞回退 */ }

  if (opts.notice) body.appendChild(h("p", "rr-hint", opts.notice));
  $("revertHint").textContent = "";
  $("revertModal").classList.remove("hidden");
}

async function submitRevert() {
  if (!S.tid || !S.revertCheckpointId) return;
  const btn = $("btnRevertGo");
  btn.disabled = true;
  try {
    const fields = Object.keys(S.rrEdits || {}).length ? S.rrEdits : null;
    const resp = await api(`/api/sessions/${S.tid}/revert`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ checkpoint_id: S.revertCheckpointId, fields }),
    });
    $("revertModal").classList.add("hidden");
    S.revertCheckpointId = null;
    toast("已回退",
      `回到「${resp.label || resp.node}」${fields ? `（需求已改 ${Object.keys(fields).length} 处）` : ""}`
      + (resp.restored ? ` · ${resp.restored}` : " · 下游将重跑"));
    // 按回退后的存档重建对话流；若续跑已开始，openSession 会自动接上实时进度
    await openSession(S.tid);
    if (S.pendingEdit) {
      $("chatInput").value = S.pendingEdit;
      S.pendingEdit = null;
      autoresize(); updateComposer();
      $("chatInput").focus();
    }
  } catch (err) {
    $("revertHint").textContent = "回退失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
}

/* 「编辑重发」：回退到这条消息发出前，并把原文放回输入框供修改后重发。 */
function editAndResend(msgEl, text) {
  if (S.running) {  // 门禁挂起 = 图已暂停，允许编辑；只有推进中才拦
    toast("流程推进中", "等当前步骤暂停后再编辑历史消息", "err");
    return;
  }
  const backToComposer = () => {
    $("chatInput").value = text;
    autoresize(); updateComposer();
    $("chatInput").focus();
  };
  const pre = messageRevertTarget(msgEl);
  if (!pre) {
    backToComposer();
    toast("已放回输入框", "这条消息之前没有存档点；修改后直接发送即可");
    return;
  }
  S.pendingEdit = text;
  openRevertModal(pre, { notice: "回退到这条消息发出前；确认后原文会放回输入框，修改后重新发送。" });
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
    hint.textContent = S.gate === "requirement_review"
      ? "需求确认待决策 — 请在确认窗中核对字段并确认或驳回"
      : S.gate === "graph_type_select"
      ? "图种类待选择 — 请在弹窗中挑选本次逻辑图的种类"
      : S.gate === "checklist_route"
      ? "业务清单路由待确认 — 请在弹窗中勾选要注入的检查清单"
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
  startRun({ op: "message", text });
}

async function createAndStart(text) {
  try {
    const { thread_id } = await api("/api/sessions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ set_fields: buildSetFields() }),
    });
    S.tid = thread_id;
    localStorage.setItem("df-last-tid", thread_id);
    S.stage = "clarify";
    S.graph = null; S.report = null; S.gate = null;
    S.history = []; S.lastSeq = 0;
    $("chatInput").value = ""; autoresize();
    resetFeed();
    setStep(0, true);
    addUserMsg(text);
    setRunning(true);
    startRun({ op: "message", text });
    renderCfgChips();
    updateCfgBtn();
    refreshSessions();
    closeSidebar();
  } catch (err) {
    toast("创建会话失败", err.message, "err");
  }
}

/* ＋ 新会话：回到空态，等待 composer 输入（旧会话若有 run 在跑，服务端继续推进） */
function newSession() {
  stopEvents();
  S.tid = null; S.gate = null; S.graph = null; S.report = null; S.stage = "clarify";
  S.history = []; S.lastSeq = 0; S.pendingEdit = null;
  localStorage.removeItem("df-last-tid");
  $("gateModal").classList.add("hidden");
  $("gatePill").classList.add("hidden");
  resetFeed();
  resetCfgForm();  // 新会话不继承上一单的运行配置（需要示例可点「✦ 示例配置」）
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
  S.msgIds = new Set();
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

/* 默认 = 全空：预填示例值会把计算器字段合进任意需求（澄清合并「已有值优先」，
   用户没提到的字段永远保留旧值 → 制图被带偏与输入无关）。示例请点「✦ 示例配置」。 */
const CFG_DEFAULTS = { fRoot: ".", fCtx: "", fIoIn: "", fIoOut: "" };

/* 把配置表单重置回默认（新会话 = 干净配置，避免上一单的配置悄悄带进下一单） */
function resetCfgForm() {
  $("fRoot").value = CFG_DEFAULTS.fRoot;
  $("fCtx").value = CFG_DEFAULTS.fCtx;
  $("fIoIn").value = CFG_DEFAULTS.fIoIn;
  $("fIoOut").value = CFG_DEFAULTS.fIoOut;
  initTags($("tagModules"), []);
  initTags($("tagEdges"), []);
  initTags($("tagAccept"), []);
  renderCfgChips();
}

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
  pop.classList.remove("hidden");
  renderHealthPop();
}

/* 供应商面板：兜底链顺序（可改首选）+ 连通性检测 + mock 兜底状态 */
async function renderHealthPop() {
  const pop = $("healthPop");
  pop.innerHTML = "";
  pop.appendChild(h("div", "row", "加载中…"));

  let chain = [], mockFb = false;
  try {
    const prov = await api("/api/providers");
    chain = prov.chain || [];
    mockFb = !!prov.mock_fallback;
  } catch (err) {
    pop.innerHTML = "";
    pop.appendChild(h("div", "row", "读取失败: " + err.message));
    return;
  }

  pop.innerHTML = "";
  pop.appendChild(h("h4", null, "LLM 提供商 · 兜底链（自上而下优先）"));

  const checkOut = h("div", "sec");
  if (!chain.length) pop.appendChild(h("div", "row", "未配置提供商（Mock 兜底）"));
  chain.forEach((p, i) => {
    const rowEl = h("div", "prov-row" + (i === 0 && !p.disabled ? " active" : "") + (p.disabled ? " disabled" : ""));
    const head = h("div", "prov-head");
    head.appendChild(h("span", "mono", `${i + 1}.`));
    head.appendChild(h("b", null, p.name));
    head.appendChild(h("span", "mono prov-model", p.model));
    if (p.disabled) head.appendChild(h("span", "prov-badge bad", "已停用·鉴权失败"));
    else if (p.sticky) head.appendChild(h("span", "prov-badge", "使用中·命中缓存"));
    else if (i === 0) head.appendChild(h("span", "prov-badge", "首选"));
    rowEl.appendChild(head);

    const ops = h("div", "prov-ops");
    if (!p.disabled && i > 0) {
      const use = h("button", "prov-btn", "设为首选");
      use.onclick = async () => {
        try {
          await api("/api/providers/active", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: p.name, model: p.model }),
          });
          toast("已切换首选供应商", `${p.name} · ${p.model} 将优先处理后续请求（全局生效）`);
          renderHealthPop();
          loadHealth();
        } catch (err) { toast("切换失败", err.message, "err"); }
      };
      ops.appendChild(use);
    }
    const testLabel = p.disabled ? "检测并启用" : "检测";
    const test = h("button", "prov-btn", testLabel);
    test.onclick = async () => {
      test.disabled = true; test.textContent = "检测中…";
      try {
        const r = await api("/api/providers/check", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: p.name }),
        });
        const rep = (r.reports || [])[0] || {};
        test.classList.add(rep.ok ? "ok" : "bad");
        test.textContent = rep.ok
          ? `✓ ${rep.elapsed_ms}ms`
          : `✗ ${rep.error_code || "ERR"}: ${String(rep.error_message || "").slice(0, 60)}`;
        if (rep.ok || rep.error_code === "HTTP.AUTH") setTimeout(renderHealthPop, 600);
      } catch (err) {
        test.classList.add("bad");
        test.textContent = "✗ " + err.message;
      }
      test.disabled = false;
    };
    ops.appendChild(test);
    rowEl.appendChild(ops);
    pop.appendChild(rowEl);
  });

  const opRow = h("div", "prov-ops");
  const checkAll = h("button", "prov-btn", "一键检测全部连通性");
  checkAll.onclick = async () => {
    checkAll.disabled = true; checkAll.textContent = "检测中…（逐个调用，约 10-60s）";
    checkOut.innerHTML = "";
    try {
      const r = await api("/api/providers/check", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      (r.reports || []).forEach((rep) => {
        const line = h("div", "row");
        line.appendChild(h("span", null, rep.ok ? "✓" : "✗"));
        line.appendChild(h("span", "mono", rep.ok
          ? `${rep.name} · ${rep.model} — ${rep.elapsed_ms}ms — ${String(rep.reply || "").slice(0, 20)}`
          : `${rep.name} — [${rep.error_code}] ${String(rep.error_message || "").slice(0, 90)}`));
        checkOut.appendChild(line);
      });
    } catch (err) {
      checkOut.appendChild(h("div", "row", "检测失败: " + err.message));
    }
    checkAll.disabled = false; checkAll.textContent = "一键检测全部连通性";
  };
  opRow.appendChild(checkAll);
  pop.appendChild(opRow);
  pop.appendChild(checkOut);

  const row = (k, v) => {
    const r = h("div", "row");
    r.appendChild(h("span", null, k));
    r.appendChild(h("span", "mono", String(v)));
    return r;
  };
  const sec = h("div", "sec");
  sec.appendChild(row("Mock 兜底", mockFb ? "开启" : "关闭"));
  const pipe = S.health?.pipeline || {};
  sec.appendChild(row("代码检索", pipe.code_search || "mock"));
  sec.appendChild(row("代码生成", pipe.code_edit || "mock"));
  sec.appendChild(row("测试生成", pipe.test_gen || "mock"));
  sec.appendChild(row("Checkpoint", S.health?.checkpoint_db?.path || ""));
  pop.appendChild(sec);
  if (mockFb && chain.length === 0) {
    pop.appendChild(h("div", "warn", "当前 LLM 输出为 Mock 演示数据；复制 .env.example 为 .env 并填入真实 Key 即可获得真实结果。"));
  }
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

  // 配置默认全空（示例走「✦ 示例配置」按钮），避免示例值悄悄混进用户的任意需求
  initTags($("tagModules"), []);
  initTags($("tagEdges"), []);
  initTags($("tagAccept"), []);

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
  $("btnCfgClear").onclick = () => { resetCfgForm(); };
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
    // 清单路由门禁的「跳过」无需填意见，直接决策
    if (S.gate === "checklist_route") { submitGate("reject", null); return; }
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
  // 步骤回退（time-travel）
  $("btnRevertCancel").onclick = () => {
    $("revertModal").classList.add("hidden");
    S.revertCheckpointId = null;
    S.pendingEdit = null;
  };
  $("btnRevertGo").onclick = submitRevert;
  // 沉淀 Checklist
  $("btnDistillCancel").onclick = closeDistill;
  $("btnDistillGen").onclick = genDistill;
  $("btnDistillBack").onclick = () => distillShowForm(true);
  $("btnDistillCommit").onclick = commitDistill;
  // 上传清单文档入库 / 手写清单 / 清单库浏览
  $("btnImportCancel").onclick = closeImportModal;
  $("btnImportGen").onclick = genImport;
  $("btnImportBack").onclick = () => importShowForm(true);
  $("btnImportCommit").onclick = commitImport;
  $("btnManualCancel").onclick = closeManualModal;
  $("btnManualGen").onclick = genManual;
  $("btnManualBack").onclick = () => manualShowForm(true);
  $("btnManualCommit").onclick = commitManual;
  $("btnLibraryClose").onclick = () => $("libraryModal").classList.add("hidden");
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("importModal").classList.contains("hidden")) { closeImportModal(); return; }
    if (!$("manualModal").classList.contains("hidden")) { closeManualModal(); return; }
    if (!$("libraryModal").classList.contains("hidden")) { $("libraryModal").classList.add("hidden"); return; }
    if (!$("gateModal").classList.contains("hidden")) closeGateToPeek();
  });

  updateComposer();
  renderCfgChips();
  updateCfgBtn();
  loadHealth();
  refreshSessions();

  // 断线/误关浏览器恢复：自动回到上次会话（服务端 run 仍在推进时直接接上实时进度）
  const lastTid = localStorage.getItem("df-last-tid");
  if (lastTid) openSession(lastTid);
  else showEmptyState();
}

boot();
