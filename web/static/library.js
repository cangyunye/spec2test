"use strict";
/* ═══════════════════════════════════════════════════════════════
   DevFlow · 业务 Checklist 库浏览页（独立文档页 /library）
   只读：库全树 + 分节条目 + 检索/优先级筛选；主题与主界面共用
   ═══════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);
const PRI_ORDER = ["P0", "P1", "P2", "OTHER"];
const PRI_LABEL = { P0: "P0", P1: "P1", P2: "P2", OTHER: "—" };

const S = {
  tree: [],
  flat: [],
  active: "__all__",
  mode: "library_root",
  root: "",
  search: "",
  pri: new Set(["P0", "P1", "P2", "OTHER"]),
  theme: document.documentElement.dataset.theme || "dark",
};

/* ── 小工具（与主界面同款轻量渲染，防 XSS）────────────── */

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
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    let m;
    if ((m = line.match(/^\s{0,3}(#{1,6})\s+(.*)$/))) {
      const el = h(/^###/.test(m[1]) ? "h4" : "h3", "md-h");
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
    const p = h("div", "md-p");
    const buf = [];
    while (i < lines.length && lines[i].trim() && !isHead(lines[i]) && !isUl(lines[i])) {
      buf.push(lines[i]); i += 1;
    }
    p.innerHTML = buf.map(mdInline).join("<br>");
    frag.appendChild(p);
  }
  return frag;
}

async function api(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    let msg = await resp.text().catch(() => "");
    try { msg = JSON.parse(msg).detail || msg; } catch { }
    throw new Error(msg.slice(0, 300) || `HTTP ${resp.status}`);
  }
  return resp.json();
}

/* ── 初始化 ──────────────────────────────────────────── */

function boot() {
  const q = new URLSearchParams(location.search);
  const qLib = q.get("library_root");
  const qProj = q.get("project_root");
  if (qProj) { S.mode = "project_root"; $("libRoot").value = qProj; }
  else if (qLib) { S.mode = "library_root"; $("libRoot").value = qLib; }
  else {
    S.mode = localStorage.getItem("df-lib-mode") || "library_root";
    $("libRoot").value = localStorage.getItem("df-lib-root") || "";
  }
  $("libMode").value = S.mode;

  $("libTheme").onclick = toggleTheme;
  $("libLoad").onclick = () => loadLibrary(true);
  $("libRoot").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLibrary(true); });
  $("libMode").onchange = () => { S.mode = $("libMode").value; saveRoot(); };
  $("libSearch").addEventListener("input", () => { S.search = $("libSearch").value; render(); });
  document.querySelectorAll(".lib-pri-chip").forEach((btn) => {
    btn.onclick = () => {
      const key = btn.dataset.pri;
      if (S.pri.has(key)) S.pri.delete(key); else S.pri.add(key);
      btn.classList.toggle("on", S.pri.has(key));
      render();
    };
  });

  loadRoots();
  loadLibrary(false);
}

function saveRoot() {
  localStorage.setItem("df-lib-mode", S.mode);
  localStorage.setItem("df-lib-root", $("libRoot").value.trim());
}

async function loadRoots() {
  try {
    const data = await api("/api/library/roots");
    const list = $("libRootList");
    (data.roots || []).forEach((r) => list.appendChild(h("option", null, r)));
    if (data.default && !$("libRoot").value) {
      $("libRoot").placeholder = `留空 = ${data.default}`;
    }
  } catch { /* 下拉仅是便利项，失败不影响主流程 */ }
}

async function loadLibrary(explicit) {
  const value = $("libRoot").value.trim();
  const params = new URLSearchParams();
  if (value) params.set(S.mode, value);
  const url = "/api/library" + (params.toString() ? "?" + params.toString() : "");
  const doc = $("libDoc");
  doc.innerHTML = "";
  doc.appendChild(h("div", "cl-empty", "加载中…"));
  try {
    const data = await api(url);
    S.root = data.root || "";
    S.tree = data.tree || [];
    S.flat = flatten(S.tree);
    if (explicit) saveRoot();
    if (!S.tree.length) {
      $("libTree").innerHTML = "";
      $("libTree").appendChild(emptyLibNote(data));
      doc.innerHTML = "";
      doc.appendChild(h("div", "cl-empty",
        data.exists === false
          ? `路径不存在：${data.root}（换成 project_root 或先运行 devflow checklist init）`
          : "清单库还是空的：运行 `devflow checklist init`，或在主界面清单路由卡上传文档入库。"));
      $("libRootNote").textContent = "库根：" + S.root;
      $("libStats").textContent = "";
      return;
    }
    if (!S.flat.some((n) => n.rel_dir === S.active)) S.active = "__all__";
    render();
  } catch (err) {
    doc.innerHTML = "";
    doc.appendChild(h("div", "cl-empty", "加载失败：" + err.message));
  }
}

function flatten(tree) {
  const out = [];
  tree.forEach((biz) => {
    out.push(biz);
    (biz.children || []).forEach((sub) => out.push(sub));
  });
  return out;
}

function emptyLibNote(data) {
  const box = h("div", "cl-empty");
  box.appendChild(h("p", null, data.exists === false ? "路径不存在" : "库为空"));
  box.appendChild(h("span", "cl-dir mono", data.root || ""));
  return box;
}

/* ── 筛选 ────────────────────────────────────────────── */

function filterActive() {
  return !!S.search.trim() || S.pri.size < PRI_ORDER.length;
}

function matchItem(it) {
  const key = it.priority || "OTHER";
  if (!S.pri.has(key)) return false;
  const q = S.search.trim().toLowerCase();
  if (q && !(it.text || "").toLowerCase().includes(q)) return false;
  return true;
}

function filteredSections(node) {
  const active = filterActive();
  const out = [];
  (node.sections || []).forEach((sec) => {
    const items = (sec.items || []).filter(matchItem);
    if (active && !items.length) return;
    out.push({ category: sec.category, items: active ? items : (sec.items || []) });
  });
  return out;
}

function matchCount(node) {
  let n = 0;
  (node.sections || []).forEach((sec) => {
    (sec.items || []).forEach((it) => { if (matchItem(it)) n += 1; });
  });
  return n;
}

/* ── 渲染 ────────────────────────────────────────────── */

function render() {
  renderTree();
  const total = S.flat.reduce((a, n) => a + (n.item_count || 0), 0);
  const shown = filterActive() ? S.flat.reduce((a, n) => a + matchCount(n), 0) : total;
  $("libStats").textContent = filterActive()
    ? `${shown} / ${total} 条命中`
    : `${S.tree.length} 业务 · ${total} 条`;
  $("libRootNote").textContent = "库根：" + S.root;
  renderDoc();
}

function renderTree() {
  const box = $("libTree");
  box.innerHTML = "";
  box.appendChild(navRow({ rel_dir: "__all__", name: "全部业务", item_count: S.flat.reduce((a, n) => a + (n.item_count || 0), 0) }, 0, true));
  S.tree.forEach((biz) => {
    box.appendChild(navRow(biz, 0, false));
    (biz.children || []).forEach((sub) => box.appendChild(navRow(sub, 1, false)));
  });
}

function navRow(node, depth, isAll) {
  const active = S.active === node.rel_dir;
  const btn = h("button", "lib-nav-row" + (depth ? " sub" : "") + (active ? " on" : ""));
  const name = h("span", "lib-nav-name", node.name || node.rel_dir);
  btn.appendChild(name);
  if (!isAll) btn.appendChild(h("span", "cl-dir mono", node.rel_dir));
  const count = filterActive() && !isAll ? matchCount(node) : (node.item_count || 0);
  btn.appendChild(h("span", "lib-nav-count", isAll ? `${count} 条` : (node.has_checklist ? `${count} 条` : "无清单")));
  btn.onclick = () => { S.active = node.rel_dir; render(); scrollDocTop(); };
  return btn;
}

function scrollDocTop() {
  const doc = $("libDoc");
  if (doc) doc.scrollTop = 0;
}

function renderDoc() {
  const doc = $("libDoc");
  doc.innerHTML = "";
  if (filterActive()) { doc.appendChild(renderResults()); return; }
  if (S.active === "__all__") {
    if (!S.tree.length) { doc.appendChild(h("div", "cl-empty", "库为空。")); return; }
    S.tree.forEach((biz) => {
      doc.appendChild(renderNode(biz));
      (biz.children || []).forEach((sub) => doc.appendChild(renderNode(sub, true)));
    });
    return;
  }
  const node = S.flat.find((n) => n.rel_dir === S.active);
  if (!node) { doc.appendChild(h("div", "cl-empty", "未找到该业务。")); return; }
  doc.appendChild(renderNode(node));
}

function renderResults() {
  const wrap = h("div", "lib-results");
  const q = S.search.trim();
  wrap.appendChild(h("h1", "lib-res-title", "检索结果"));
  const desc = h("p", "lib-res-desc mono");
  desc.textContent = `${q ? `“${q}” · ` : ""}${[...S.pri].join("/")} · ${S.flat.reduce((a, n) => a + matchCount(n), 0)} 条命中`;
  wrap.appendChild(desc);
  let any = false;
  S.flat.forEach((node) => {
    const secs = filteredSections(node);
    if (!secs.length) return;
    any = true;
    const block = h("section", "lib-res-block");
    const head = h("div", "lib-res-head");
    head.appendChild(h("b", null, node.name || node.rel_dir));
    head.appendChild(h("span", "cl-dir mono", node.rel_dir));
    head.appendChild(h("span", "lib-nav-count", `${matchCount(node)} 条`));
    block.appendChild(head);
    secs.forEach((sec) => block.appendChild(sectionBlock(sec)));
    wrap.appendChild(block);
  });
  if (!any) wrap.appendChild(h("div", "cl-empty", "没有匹配的检查点，换个关键词或放宽优先级筛选。"));
  return wrap;
}

function renderNode(node, isSub) {
  const art = h("article", "lib-article" + (isSub ? " sub" : ""));
  const head = h("header", "lib-art-head");
  const title = h("div", "lib-art-title");
  title.appendChild(h("h1", null, node.name || node.rel_dir));
  title.appendChild(h("span", "cl-dir mono", node.rel_dir));
  if (node.updated) title.appendChild(h("span", "lib-badge mono", "updated " + node.updated));
  head.appendChild(title);
  if (node.description) head.appendChild(h("p", "lib-desc", node.description));

  if (node.keywords?.length) {
    const kw = h("div", "lib-kw");
    node.keywords.forEach((k) => kw.appendChild(h("span", "lib-kw-chip mono", k)));
    head.appendChild(kw);
  }
  if (node.references?.length) {
    const refs = h("div", "lib-refs");
    node.references.forEach((r) => {
      refs.appendChild(h("span", "lib-ref mono", `↳ ${r.path}${r.desc ? " · " + r.desc : ""}`));
    });
    head.appendChild(refs);
  }
  if (node.usage) {
    const usage = h("section", "lib-usage");
    usage.appendChild(h("h3", "lib-sec-title", "使用场景"));
    usage.appendChild(mdBlock(node.usage));
    head.appendChild(usage);
  }
  if (node.sources?.length) {
    head.appendChild(h("div", "lib-meta mono", "溯源：" + node.sources.join("、")));
  }
  art.appendChild(head);

  if (!node.has_checklist) {
    art.appendChild(h("div", "cl-empty", "该目录暂无 checklist.md（仅路由标签 scenario.md）。"));
    return art;
  }
  const sections = filteredSections(node);
  if (!sections.length) {
    art.appendChild(h("div", "cl-empty", "无匹配检查点。"));
    return art;
  }
  sections.forEach((sec) => art.appendChild(sectionBlock(sec)));
  return art;
}

function sectionBlock(sec) {
  const wrap = h("section", "lib-sec");
  const head = h("h2", "lib-sec-title");
  head.appendChild(h("span", null, sec.category));
  head.appendChild(h("span", "lib-sec-count", `${sec.items.length} 条`));
  wrap.appendChild(head);
  const ul = h("ul", "lib-items");
  sec.items.forEach((it) => {
    const li = h("li", "lib-item");
    const pri = it.priority || "OTHER";
    li.appendChild(h("span", "lib-pri-badge " + pri.toLowerCase(), PRI_LABEL[pri] || "—"));
    const txt = h("span", "lib-item-text");
    txt.innerHTML = mdInline(it.text || "");
    li.appendChild(txt);
    ul.appendChild(li);
  });
  wrap.appendChild(ul);
  return wrap;
}

/* ── 主题 ────────────────────────────────────────────── */

function toggleTheme() {
  S.theme = S.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = S.theme;
  localStorage.setItem("df-theme", S.theme);
}

boot();
