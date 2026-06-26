// ═══════════════════ DOM refs ═══════════════════
const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("promptInput");
const actionBtn = document.getElementById("actionBtn");
const statusLabel = document.getElementById("statusLabel");
const graphCanvas = document.getElementById("graphCanvas");
const graphNodes = document.getElementById("graphNodes");
const graphEdges = document.getElementById("graphEdges");
const graphEmpty = document.getElementById("graphEmpty");
const graphStatus = document.getElementById("graphStatus");
const entityTypeEl = document.getElementById("entityType");
const entityNameEl = document.getElementById("entityName");
const toolPopup = document.getElementById("toolPopup");
const toolPopupTitle = document.getElementById("toolPopupTitle");
const toolPopupBody = document.getElementById("toolPopupBody");
const toolPopupClose = document.getElementById("toolPopupClose");
const skillsBar = document.getElementById("skillsBar");
const skillSuggestions = document.getElementById("skillSuggestions");

// ═══════════════════ Skills Registry (fetched from API) ═══════════════════
let SKILLS = [];
let skillIdx = new Map();
let skillsLoaded = false;
let activeSkills = [];

async function loadSkills() {
  try {
    const resp = await fetch("/api/skills");
    if (!resp.ok) { console.warn("Skills API returned", resp.status); return; }
    const data = await resp.json();
    SKILLS = data.map(s => ({
      id: s.id,
      label: s.name || s.id,
      desc: s.description || "",
      category: s.category || "other",
    }));
    skillIdx = new Map(SKILLS.map(s => [s.id, s]));
    skillsLoaded = true;
    console.log("Skills loaded:", SKILLS.length);
  } catch (e) {
    console.warn("Failed to load skills from API:", e);
  }
}
// Load skills at page init
loadSkills();

// ═══════════════════ State ═══════════════════
let sessionId = localStorage.getItem("diagnostics.sessionId") || null;
let controller = null;
let activeBubble = null;
let thinkSections = [];       // per-round: { sectionEl, bodyEl, textEl, labelEl, rawText, startTime, finalized }
let activeAnswerSection = null;
let activeAnswerBody = null;
let activeCursor = null;
let answerRawText = "";
let userScrolledUp = false;
let _lastActivity = Date.now();
let _stalenessTimer = null;
const STALE_THRESHOLD_MS = 25_000;  // warn after 25s of silence
let graphUserScrolled = false;  // track if user scrolled graph up

// ── Graph state ──
const GRAPH = {
  nodes: new Map(),       // id → {id, title, parentId, parentIds, status, nodeType, detail, description, el}
  edges: [],              // [{from, to}]
  nodeOrder: [],          // ordered list of node ids
  hasContent: false,
};

// ═══════════════════ Init ═══════════════════
toolPopupClose.addEventListener("click", () => toolPopup.classList.add("hidden"));
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
});

// ═══════════════════ History ═══════════════════
const historyBarList = document.getElementById("historyBarList");
const historyViewer = document.getElementById("historyViewer");
const historyBack = document.getElementById("historyBack");
const historyViewerTitle = document.getElementById("historyViewerTitle");
const historyReport = document.getElementById("historyReport");
const historyGraph = document.getElementById("historyGraph");
const historyNodes = document.getElementById("historyNodes");
const historyEdges = document.getElementById("historyEdges");

// Load history list on page load
loadHistoryList();

historyBack.addEventListener("click", () => {
  historyViewer.classList.add("hidden");
  _historyGraph = null;
  clearTimeout(_historyDrawTimer);
  graphCanvas.style.display = "";
  graphEmpty.style.display = GRAPH.hasContent ? "none" : "";
  // Refresh main graph edges after layout
  if (GRAPH.hasContent) {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => drawAllEdges());
    });
  }
});

async function loadHistoryList() {
  historyBarList.innerHTML = '<div class="history-item-placeholder">加载中...</div>';
  try {
    const resp = await fetch("/api/history");
    if (!resp.ok) throw new Error("Failed");
    const data = await resp.json();
    if (data.length === 0) {
      historyBarList.innerHTML = '<div class="history-item-placeholder">暂无历史记录</div>';
      return;
    }
    historyBarList.innerHTML = "";
    for (const item of data) {
      const div = document.createElement("div");
      div.className = "history-bar-item";
      const dur = item.duration_secs ? Math.round(item.duration_secs) + "s" : "";
      const tools = item.event_counts?.tool_start || 0;
      const toolsStr = item.duration_secs !== null ? ` · ${tools} tools` : "";
      const fname = (item.report_file || "").split("/").pop() || "";
      div.innerHTML = `
        <div class="history-bar-item-title" title="${esc(item.entity_type + '/' + item.entity_name + ' — ' + fname)}">${esc(fname)}</div>
        <div class="history-bar-item-meta">${esc(item.entity_type || "")}/${esc(item.entity_name || "")} · ${dur}${toolsStr}</div>
      `;
      div.addEventListener("click", () => openHistory(item));
      historyBarList.appendChild(div);
    }
  } catch (e) {
    historyBarList.innerHTML = '<div class="history-item-placeholder" style="color:var(--red)">加载失败</div>';
  }
}

async function openHistory(item) {
  console.log("openHistory called, report_file:", item.report_file);
  historyViewer.classList.remove("hidden");
  const durText = item.duration_secs ? ` · ${Math.round(item.duration_secs)}s` : "";
  historyViewerTitle.textContent = `${item.entity_type}/${item.entity_name}${durText}`;
  graphCanvas.style.display = "none";

  // Load report
  historyReport.innerHTML = '<p style="color:var(--text-3)">加载报告...</p>';
  try {
    console.log("Fetching report:", item.report_file);
    const url = `/api/report/${encodeURI(item.report_file || "")}`;
    const resp = await fetch(url);
    console.log("Report fetch status:", resp.status, resp.ok);
    if (resp.ok) {
      const data = await resp.json();
      console.log("Report loaded, chars:", (data.content || "").length);
      historyReport.innerHTML = renderMarkdown(data.content || "");
    } else {
      const errText = await resp.text().catch(() => "");
      console.warn("Report not found:", resp.status, errText);
      historyReport.innerHTML = '<p style="color:var(--text-3)">报告未生成</p>';
    }
  } catch (e) {
    console.error("Report fetch error:", e);
    historyReport.innerHTML = '<p style="color:var(--red)">加载失败: ' + esc(String(e).slice(0, 80)) + '</p>';
  }

  // Render graph from saved tree (or show empty state)
  console.log("tree steps:", item.tree?.steps?.length);
  if (item.tree?.steps?.length) {
    renderHistoryGraph(item.tree.steps);
  } else {
    historyNodes.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-3);font-size:12px"><p>无诊断过程数据<br>（此报告为功能上线前生成）</p></div>';
  }

  // Render hypothesis tree from saved ledger (if present)
  if (item.ledger) {
    currentLedger = item.ledger;
    renderHypothesisTree(item.ledger);
    const hypCount = Object.keys(item.ledger.hypotheses || {}).length;
    if (hypCount > 0 && hypothesisBadge) {
      hypothesisBadge.textContent = hypCount;
      hypothesisBadge.classList.remove("hidden");
    }
  }
}

function renderHistoryGraph(steps) {
  historyNodes.innerHTML = "";
  historyEdges.innerHTML = "";

  // Build a temporary graph from saved steps
  // Saved JSON uses Python snake_case; normalize to camelCase for
  // _buildLevelsFrom / createNodeDOM which expect parentId, nodeType, etc.
  const hg = { nodes: new Map(), edges: [], nodeOrder: [], hasContent: true };
  for (const s of steps) {
    const parentIds = s.parent_ids || (s.parent_id ? [s.parent_id] : []);
    hg.nodes.set(s.id, {
      id: s.id,
      title: s.title,
      parentId: s.parent_id || null,
      parentIds: parentIds,
      status: "completed",
      nodeType: s.node_type || "tool",
      description: s.description || "",
      detail: s.detail || "",
      toolName: s.tool_name || "",
      toolArgs: s.tool_args || "",
      el: null,
    });
    hg.nodeOrder.push(s.id);
    for (const pid of parentIds) {
      if (pid) hg.edges.push({ from: pid, to: s.id });
    }
  }

  // Swap GRAPH to the history graph so buildLevels/renderTree use it
  const origMap = GRAPH.nodes, origOrder = GRAPH.nodeOrder, origEdges = GRAPH.edges, origHas = GRAPH.hasContent;
  GRAPH.nodes = hg.nodes;
  GRAPH.nodeOrder = hg.nodeOrder;
  GRAPH.edges = hg.edges;
  GRAPH.hasContent = true;

  // Swap DOM refs so renderTree draws into history DOM
  const origNodes = graphNodes, origEdgesRef = graphEdges;

  // Temporarily override: closures capture the variable, need to use Object.defineProperty or a getter
  // Actually, since graphNodes/graphEdges are const DOM refs, we can't reassign them.
  // Use a different approach: call the render functions directly with the history graph.
  _renderTreeTo(hg.nodes, hg.nodeOrder, hg.edges, historyNodes, historyEdges);

  GRAPH.nodes = origMap;
  GRAPH.nodeOrder = origOrder;
  GRAPH.edges = origEdges;
  GRAPH.hasContent = origHas;

  // Draw edges into history SVG
  drawHistoryEdges(hg);
}

// Standalone tree renderer for history (doesn't rely on globals)
function _renderTreeTo(nodes, nodeOrder, edges, targetNodes, targetEdges) {
  targetNodes.innerHTML = "";
  const levels = buildLevels(nodes, nodeOrder);

  const container = document.createElement("div");
  container.className = "tree-container";

  for (let li = 0; li < levels.length; li++) {
    const level = levels[li];
    const levelEl = document.createElement("div");
    levelEl.className = "tree-level";

    for (const nodeId of level) {
      const node = nodes.get(nodeId);
      if (!node) continue;
      const el = createNodeDOM(node);
      levelEl.appendChild(el);
      node.el = el;
    }
    container.appendChild(levelEl);
    if (li < levels.length - 1) {
      const cn = document.createElement("div");
      cn.className = "tree-connector";
      container.appendChild(cn);
    }
  }
  targetNodes.appendChild(container);
  targetEdges.innerHTML = "";
}

function _buildLevelsFrom(nodes, nodeOrder, edges) {
  const levels = [];
  const visited = new Set();
  const roots = [];
  for (const id of nodeOrder) {
    const node = nodes.get(id);
    if (node && !node.parentId) roots.push(id);
  }
  if (roots.length === 0 && nodeOrder.length > 0) roots.push(nodeOrder[0]);
  const queue = [...roots];
  for (const r of roots) visited.add(r);
  levels.push([...roots]);

  while (queue.length > 0) {
    const currentLevel = [...queue];
    queue.length = 0;
    const nextLevel = [];
    for (const parentId of currentLevel) {
      for (const id of nodeOrder) {
        if (visited.has(id)) continue;
        const node = nodes.get(id);
        if (!node) continue;
        if ((node.parentIds || []).includes(parentId) || node.parentId === parentId) {
          nextLevel.push(id);
          visited.add(id);
        }
      }
    }
    if (nextLevel.length === 0) break;
    levels.push([...new Set(nextLevel)]);
    queue.push(...nextLevel);
  }
  return levels;
}

let _historyGraph = null;   // reference to current history graph for redraws
let _historyDrawTimer = null;

function drawHistoryEdges(g) {
  _historyGraph = g;
  clearTimeout(_historyDrawTimer);
  _historyDrawTimer = setTimeout(() => _scheduleHistoryEdgeRedraw(), 5);
}

function _redrawHistoryEdges() {
  const g = _historyGraph;
  if (!g) return;
  const svg = historyEdges;
  const graphEl = document.getElementById("historyGraph");
  const gr = graphEl.getBoundingClientRect();
  if (gr.width === 0 || gr.height === 0) {
    // DOM not laid out yet — retry next frame
    requestAnimationFrame(() => _redrawHistoryEdges());
    return;
  }
  const sx = graphEl.scrollLeft, sy = graphEl.scrollTop;
  svg.setAttribute("viewBox", `0 0 ${graphEl.scrollWidth} ${graphEl.scrollHeight}`);
  svg.setAttribute("width", graphEl.scrollWidth);
  svg.setAttribute("height", graphEl.scrollHeight);

  let totalW = 0, count = 0;
  for (const n of g.nodes.values()) {
    if (n.el) { totalW += n.el.getBoundingClientRect().width; count++; }
  }
  const avgW = count > 0 ? totalW / count : 200;
  const scale = avgW / 200;
  const arrowW = Math.round(5 * scale), arrowH = Math.round(4 * scale), refX = Math.round(4.2 * scale);
  const baseSw = Math.max(1.2, 1.6 * scale);

  let content = `<defs>
    <marker id="historyArrow" markerWidth="${arrowW}" markerHeight="${arrowH}" refX="${refX}" refY="${arrowH/2}" orient="auto">
      <polygon points="0,0 ${arrowW},${arrowH/2} 0,${arrowH}" fill="#4ade80"/>
    </marker>
  </defs>`;

  for (const edge of g.edges) {
    const fn = g.nodes.get(edge.from), tn = g.nodes.get(edge.to);
    if (!fn?.el || !tn?.el) continue;
    const fr = fn.el.getBoundingClientRect(), tr = tn.el.getBoundingClientRect();
    if (fr.width === 0 || tr.width === 0) continue;
    // For history, all nodes are "completed" → green edges
    const strokeColor = "#4ade80";
    const x1 = fr.left - gr.left + fr.width / 2 + sx;
    const y1 = fr.bottom - gr.top + sy;
    const x2 = tr.left - gr.left + tr.width / 2 + sx;
    const y2 = tr.top - gr.top + sy;
    const midY1 = y1 + Math.max(20, (y2 - y1) * 0.35);
    const midY2 = y2 - Math.max(20, (y2 - y1) * 0.35);
    content += `<path
      d="M${x1},${y1} C${x1},${midY1} ${x2},${midY2} ${x2},${y2}"
      stroke="${strokeColor}" stroke-width="${baseSw.toFixed(1)}"
      fill="none" stroke-linecap="round" marker-end="url(#historyArrow)"/>`;
  }
  svg.innerHTML = content;
}

// Debounced history edge redraw via RAF
let _historyDrawRaf = 0;
function _scheduleHistoryEdgeRedraw() {
  cancelAnimationFrame(_historyDrawRaf);
  _historyDrawRaf = requestAnimationFrame(() => _redrawHistoryEdges());
}
window.addEventListener("resize", () => {
  if (_historyGraph) {
    clearTimeout(_historyDrawTimer);
    _historyDrawTimer = setTimeout(() => _scheduleHistoryEdgeRedraw(), 8);
  }
});
document.getElementById("historyGraph")?.addEventListener("scroll", () => {
  if (_historyGraph) _scheduleHistoryEdgeRedraw();
});

// ── Resizer (draggable divider) ──
const chatPanel = document.getElementById("chatPanel");
const resizer = document.getElementById("resizer");
let isResizing = false;

resizer.addEventListener("mousedown", (e) => {
  isResizing = true;
  resizer.classList.add("active");
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
  e.preventDefault();
});

// ── History Resizer (draggable divider between report & graph) ──
const historyResizer = document.getElementById("historyResizer");
let isHistoryResizing = false;

if (historyResizer) {
  historyResizer.addEventListener("mousedown", (e) => {
    isHistoryResizing = true;
    historyResizer.classList.add("active");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
}

// ── Unified mousemove for both resizers ──
document.addEventListener("mousemove", (e) => {
  if (isHistoryResizing) {
    const bodyRect = document.querySelector(".history-viewer-body").getBoundingClientRect();
    const pct = ((e.clientX - bodyRect.left) / bodyRect.width) * 100;
    const clamped = Math.max(20, Math.min(80, pct));
    historyReport.style.flex = `0 0 ${clamped}%`;
    historyGraph.style.flex = `0 0 ${100 - clamped}%`;
  }
  if (isResizing) {
    const appRect = document.querySelector(".app").getBoundingClientRect();
    const pct = ((e.clientX - appRect.left) / appRect.width) * 100;
    const clamped = Math.max(15, Math.min(75, pct));
    chatPanel.style.width = clamped + "%";
  }
});

// ── Unified mouseup for both resizers ──
document.addEventListener("mouseup", () => {
  if (isHistoryResizing) {
    isHistoryResizing = false;
    historyResizer.classList.remove("active");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    clearTimeout(_historyDrawTimer);
    _historyDrawTimer = setTimeout(() => _scheduleHistoryEdgeRedraw(), 8);
  }
  if (isResizing) {
    isResizing = false;
    resizer.classList.remove("active");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    if (GRAPH.hasContent) _scheduleEdgeRedraw();
  }
});

// ═══════════════════ Skills Bar & / command ═══════════════════

function renderSkillsBar() {
  skillsBar.innerHTML = "";
  for (const sk of activeSkills) {
    const tag = document.createElement("span");
    tag.className = "skill-tag";
    tag.innerHTML = `<span>/${sk.id}</span><span class="skill-remove" data-sid="${sk.id}">✕</span>`;
    tag.querySelector(".skill-remove").addEventListener("click", (e) => {
      e.stopPropagation();
      removeSkill(sk.id);
    });
    skillsBar.appendChild(tag);
  }
  if (activeSkills.length === 0) {
    skillsBar.style.display = "none";
  } else {
    skillsBar.style.display = "flex";
  }
}

function addSkill(skillId) {
  const sk = skillIdx.get(skillId);
  if (!sk || activeSkills.find(s => s.id === skillId)) return;
  activeSkills.push(sk);
  renderSkillsBar();
  closeSuggestions();
}

function removeSkill(skillId) {
  activeSkills = activeSkills.filter(s => s.id !== skillId);
  renderSkillsBar();
}

let _activeIndex = -1;
let _filteredSkills = [];

function _selectSkillByIndex(idx) {
  if (idx >= 0 && idx < _filteredSkills.length) {
    const s = _filteredSkills[idx];
    addSkill(s.id);
    const val = inputEl.value;
    const pos = inputEl.selectionStart;
    const beforeSlash = val.lastIndexOf("/", pos - 1);
    if (beforeSlash >= 0) {
      _skillSelecting = true;
      inputEl.value = val.slice(0, beforeSlash) + val.slice(pos);
      inputEl.selectionStart = inputEl.selectionEnd = beforeSlash;
    }
    inputEl.focus();
    closeSuggestions();
  }
}

function _highlightItem(idx) {
  _activeIndex = idx;
  const items = skillSuggestions.querySelectorAll(".skill-item");
  items.forEach((el, i) => el.classList.toggle("active", i === idx));
  if (idx >= 0 && items[idx]) {
    items[idx].scrollIntoView({ block: "nearest" });
  }
}

function openSuggestions(filterText) {
  if (!skillsLoaded) {
    loadSkills().then(() => {
      if (skillsLoaded) openSuggestions(filterText);
    });
    return;
  }
  const ft = (filterText || "").toLowerCase();
  _filteredSkills = ft
    ? SKILLS.filter(s => s.id.includes(ft) || s.label.includes(ft) || s.desc.includes(ft))
    : SKILLS;
  _activeIndex = -1;
  skillSuggestions.innerHTML = "";
  if (_filteredSkills.length === 0 && ft) {
    skillSuggestions.classList.remove("open");
    return;
  }
  for (let i = 0; i < _filteredSkills.length; i++) {
    const s = _filteredSkills[i];
    const item = document.createElement("div");
    item.className = "skill-item";
    item.dataset.index = i;
    item.innerHTML = `<span class="skill-name">/${s.id}</span><span class="skill-desc">${esc(s.desc)}</span>`;
    item.addEventListener("mousedown", (e) => {
      e.preventDefault();
      _selectSkillByIndex(i);
    });
    // Highlight on hover
    item.addEventListener("mouseenter", () => _highlightItem(i));
    skillSuggestions.appendChild(item);
  }

  // Position just below the / line (textarea top + one line)
  const textareaRect = inputEl.getBoundingClientRect();
  const wrapperRect = inputEl.parentElement.getBoundingClientRect();
  const lineH = parseFloat(getComputedStyle(inputEl).lineHeight) || 20;
  skillSuggestions.style.top = (textareaRect.top - wrapperRect.top + lineH + 12) + "px";
  skillSuggestions.style.left = (textareaRect.left - wrapperRect.left) + "px";
  skillSuggestions.style.width = Math.min(textareaRect.width, 360) + "px";

  skillSuggestions.classList.add("open");
}

function closeSuggestions() {
  skillSuggestions.classList.remove("open");
  _activeIndex = -1;
}

// Handle / command: detect "/" in input and show skill suggestions
let _skillSelecting = false;

inputEl.addEventListener("input", () => {
  if (_skillSelecting) { _skillSelecting = false; return; }

  const val = inputEl.value;
  const pos = inputEl.selectionStart;
  // Find the last / before the cursor
  const lastSlash = val.lastIndexOf("/", pos - 1);
  if (lastSlash >= 0 && val[lastSlash] === "/") {
    const afterSlash = val.slice(lastSlash + 1, pos);
    if (!afterSlash.includes(" ")) {
      openSuggestions(afterSlash);
      return;
    }
  }
  closeSuggestions();
});

// Keyboard navigation: Up/Down/Enter/Escape
inputEl.addEventListener("keydown", (e) => {
  if (!skillSuggestions.classList.contains("open")) return;
  const len = _filteredSkills.length;
  if (len === 0) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    _highlightItem(Math.min(_activeIndex + 1, len - 1));
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    _highlightItem(Math.max(_activeIndex - 1, 0));
  } else if (e.key === "Enter") {
    if (_activeIndex >= 0 && _activeIndex < len) {
      e.preventDefault();
      _selectSkillByIndex(_activeIndex);
    }
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeSuggestions();
  }
});

document.addEventListener("click", (e) => {
  if (!skillSuggestions.contains(e.target) && e.target !== inputEl) {
    closeSuggestions();
  }
});

function buildSkillPrefix() {
  if (activeSkills.length === 0) return "";
  return activeSkills.map(s => `@skill:${s.id}`).join(" ") + " ";
}

// ═══════════════════ Form submit ═══════════════════
formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const rawPrompt = inputEl.value.trim();
  if (!rawPrompt || controller) return;

  const skillPrefix = buildSkillPrefix();
  const prompt = skillPrefix + rawPrompt;
  const displayText = activeSkills.length > 0
    ? activeSkills.map(s => `/${s.id}`).join(" ") + "\n" + rawPrompt
    : rawPrompt;

  appendMsg("user", displayText);
  inputEl.value = "";
  inputEl.style.height = "auto";
  activeSkills = [];
  renderSkillsBar();
  resetGraph();
  toolPopup.classList.add("hidden");
  thinkSections = [];
  answerRawText = "";
  activeAnswerSection = null;
  activeAnswerBody = null;
  activeCursor = null;

  // Assistant bubble with thinking section
  const article = document.createElement("div");
  article.className = "msg assistant";
  article.innerHTML = '<div class="msg-avatar">◈</div>';
  activeBubble = document.createElement("div");
  activeBubble.className = "msg-bubble";
  article.appendChild(activeBubble);
  messagesEl.appendChild(article);

  setRunning(true);

  controller = new AbortController();
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: prompt,
        session_id: sessionId,
        entity_type: entityTypeEl.value,
        entity_name: entityNameEl.value.trim(),
      }),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(await response.text());
    await readSSE(response.body);
  } catch (err) {
    if (err.name !== "AbortError") {
      appendEvent("错误: " + err.message);
      setStatus("error");
    }
  } finally {
    controller = null;
    // SSE handler (done/cancelled/error) already called finishStreamUI for cleanup.
    // Keep DOM refs alive so toggle click handlers continue to work.
    setRunning(false);
    if (GRAPH.hasContent) {
      setGraphStatus("done");
    }
  }
});

// ═══════════════════ Cancel ═══════════════════
actionBtn.addEventListener("click", async (e) => {
  if (!controller) return; // Not running — form submit handles it
  e.preventDefault();
  actionBtn.disabled = true;
  const sid = sessionId;
  controller.abort();
  setStatus("cancelling");
  if (sid) {
    await fetch(`/api/sessions/${sid}/cancel`, { method: "POST" }).catch(() => {});
  }
  appendEvent("已取消");
});

// ═══════════════════ SSE ═══════════════════
async function readSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx = buf.indexOf("\n\n");
    while (idx !== -1) {
      parseSSE(buf.slice(0, idx));
      buf = buf.slice(idx + 2);
      idx = buf.indexOf("\n\n");
    }
  }
}

function _touchActivity() {
  _lastActivity = Date.now();
  setGraphStatus("running");
}

function _startStalenessCheck() {
  if (_stalenessTimer) clearInterval(_stalenessTimer);
  _touchActivity();
  _stalenessTimer = setInterval(() => {
    const elapsed = Math.round((Date.now() - _lastActivity) / 1000);
    if (elapsed >= STALE_THRESHOLD_MS / 1000) {
      setGraphStatus("stale", `无新数据 ${elapsed}s`);
    }
  }, 3000);
}

function _stopStalenessCheck() {
  if (_stalenessTimer) { clearInterval(_stalenessTimer); _stalenessTimer = null; }
}

function parseSSE(raw) {
  _touchActivity();
  const lines = raw.split("\n");
  const ev = lines.find((l) => l.startsWith("event: "));
  const da = lines.find((l) => l.startsWith("data: "));
  const type = ev ? ev.slice(7) : "message";
  let payload = {};
  try { payload = da ? JSON.parse(da.slice(6)) : {}; } catch { payload = {}; }

  switch (type) {
    case "session":
      sessionId = payload.session_id;
      localStorage.setItem("diagnostics.sessionId", sessionId);
      break;
    case "text_delta":
      onTextDelta(payload);
      break;
    case "tool_start":
      // Tool start events are informational; tree_snapshot handles graph
      break;
    case "tool_end":
      // Tool end events are informational; tree_snapshot handles graph
      break;
    case "tree_snapshot":
      onTreeSnapshot(payload);
      break;
    case "tree_delta":
      onTreeDelta(payload);
      break;
    case "ledger_snapshot":
      onLedgerSnapshot(payload);
      break;
    case "agent_start":
      break;
    case "agent_end":
      break;
    case "heartbeat":
      // System alive — reset staleness counter, show round info
      _touchActivity();
      if (payload.round) {
        setGraphStatus("running", `第${payload.round}轮 · 等待中`);
      }
      break;
    case "done":
      _stopStalenessCheck();
      setStatus("ready");
      finalizeGraph();
      finishStreamUI("done");
      break;
    case "error":
      _stopStalenessCheck();
      appendEvent(payload.message);
      setStatus("error");
      setGraphStatus("idle");
      finishStreamUI("error");
      break;
    case "cancelled":
      _stopStalenessCheck();
      setStatus("ready");
      setGraphStatus("idle");
      finishStreamUI("cancelled");
      break;
  }
}

// ═══════════════════ Event Handlers ═══════════════════

function onTextDelta(payload) {
  const path = payload.path || [];
  if (path[0] !== "coordinator") return;
  if (!activeBubble) return;

  const phase = payload.phase || "answering";

  if (phase === "thinking") {
    // Each LLM round gets its own section: "第N轮智能分析"
    // Contains: LLM reasoning text + tool call traces + tool results.
    const round = payload.round || 0;
    let sec = currentThink();

    // Finalize previous round's section when a new round starts
    if (sec && sec.round !== round) {
      finalizeThink(sec);
      sec = null;
    }

    if (!sec) {
      sec = createThinkSection(round);
      sec.round = round;
    }

    sec.rawText += payload.text;
    sec.textEl.innerHTML = renderMarkdown(sec.rawText);
    if (!activeCursor) {
      activeCursor = document.createElement("span");
      activeCursor.className = "streaming-cursor";
    }
    sec.textEl.appendChild(activeCursor);
  } else {
    // Answering phase: final diagnosis report (answer-body).
    if (!activeAnswerSection) createAnswerSection();
    answerRawText += payload.text;
    if (activeAnswerBody) {
      activeAnswerBody.innerHTML = renderMarkdown(answerRawText);
      if (!activeCursor) {
        activeCursor = document.createElement("span");
        activeCursor.className = "streaming-cursor";
      }
      activeAnswerBody.appendChild(activeCursor);
    }
  }

  autoScroll();
}

// ── Current (latest unfinalized) think section ──
function currentThink() {
  for (let i = thinkSections.length - 1; i >= 0; i--) {
    if (!thinkSections[i].finalized) return thinkSections[i];
  }
  return null;
}

// ── Finalize a think section ──
function finalizeThink(sec) {
  sec.finalized = true;
  sec.sectionEl.classList.remove("open");
}

// ── Create a new think section: title "第N轮智能分析" ──
function createThinkSection(round) {
  if (!activeBubble) return null;

  const sectionEl = document.createElement("div");
  sectionEl.className = "think-section open";

  // Toggle bar: eye icon · "第N轮智能分析" · chevron
  const toggle = document.createElement("button");
  toggle.className = "think-toggle";
  toggle.innerHTML = `
    <svg class="think-eye" width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M8 6.6c.8 0 1.4.6 1.4 1.4S8.8 9.4 8 9.4 6.6 8.8 6.6 8 7.2 6.6 8 6.6z" fill="currentColor"/>
      <path fill-rule="evenodd" clip-rule="evenodd" d="M2.2 8c.5-.8 1.2-1.7 2.2-2.4C5.7 4.6 7 4 8 4s2.3.6 3.6 1.6c1 .7 1.7 1.6 2.2 2.4-1 1.8-2.6 3.3-4.3 3.9a5.3 5.3 0 01-3 0C4.8 11.3 3.2 9.8 2.2 8zm7.2 3.4A8.1 8.1 0 0013.4 8 8.1 8.1 0 009.4 4.6 6.9 6.9 0 008 4.4a6.9 6.9 0 00-1.4.2A8.1 8.1 0 002.6 8a8.1 8.1 0 006.8 3.4z" fill="currentColor"/>
    </svg>
  `;
  const labelEl = document.createElement("span");
  labelEl.className = "think-label";
  labelEl.textContent = `第${round}轮智能分析`;
  toggle.appendChild(labelEl);

  const chevron = document.createElement("span");
  chevron.className = "think-chevron";
  chevron.innerHTML = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none">
    <path d="M4.5 5.5L7 8L9.5 5.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
  toggle.appendChild(chevron);

  // Body: left accent line + content
  const bodyEl = document.createElement("div");
  bodyEl.className = "think-body";
  bodyEl.innerHTML = '<div class="think-body-line"></div><div class="think-body-text"></div>';
  const textEl = bodyEl.querySelector(".think-body-text");

  // Each section's toggle only controls its own section
  toggle.addEventListener("click", () => {
    sectionEl.classList.toggle("open");
  });

  sectionEl.appendChild(toggle);
  sectionEl.appendChild(bodyEl);

  // Insert before answer section (if exists), otherwise at end of bubble
  if (activeAnswerSection) {
    activeBubble.insertBefore(sectionEl, activeAnswerSection);
  } else {
    activeBubble.appendChild(sectionEl);
  }

  const sec = { sectionEl, bodyEl, textEl, labelEl, rawText: "", startTime: Date.now(), finalized: false };
  thinkSections.push(sec);
  return sec;
}

// ── Answer Section (diagnosis report, always visible, always last) ──
function createAnswerSection() {
  if (!activeBubble) return;
  activeAnswerSection = document.createElement("div");
  activeAnswerSection.className = "answer-section";
  if (thinkSections.length > 0) {
    activeAnswerSection.classList.add("has-think");
  }

  activeAnswerBody = document.createElement("div");
  activeAnswerBody.className = "answer-body";
  activeAnswerSection.appendChild(activeAnswerBody);

  activeBubble.appendChild(activeAnswerSection);
}

// ── Unified stream-end UI cleanup ──
function finishStreamUI(reason) {
  if (activeCursor) { activeCursor.remove(); activeCursor = null; }
  const sec = currentThink();
  if (sec) finalizeThink(sec);
  if (reason === "cancelled" && sec) {
    sec.labelEl.textContent = "第" + sec.round + "轮智能分析（已取消）";
  }
  activeAnswerSection = null;
  activeAnswerBody = null;
}

// ── Structured Response Handler ──
function onStructuredResponse(payload) {
  if (!activeBubble) return;
  if (!activeAnswerBody) createAnswerSection();

  // Build structured report from DiagnosisResult fields
  const parts = [];
  if (payload.symptoms) parts.push(`**症状**: ${payload.symptoms}`);
  if (payload.evidence) parts.push(`**证据**: ${payload.evidence}`);
  if (payload.root_cause) parts.push(`**根因**: ${payload.root_cause}`);
  if (payload.next_steps) parts.push(`**建议**: ${payload.next_steps}`);

  if (parts.length > 0) {
    const html = parts.join('\n\n');
    activeAnswerBody.innerHTML = renderMarkdown(html);
  }
}

// ═══════════════════ Tree Snapshot Handler ═══════════════════

function onTreeSnapshot(payload) {
  if (!payload || !payload.steps) return;

  GRAPH.hasContent = true;
  graphEmpty.style.display = "none";
  setGraphStatus("running");

  const newIds = new Set(payload.steps.map(s => s.id));

  // Remove nodes no longer in snapshot
  for (const id of [...GRAPH.nodeOrder]) {
    if (!newIds.has(id)) {
      const node = GRAPH.nodes.get(id);
      if (node?.el) node.el.remove();
      GRAPH.nodes.delete(id);
    }
  }

  // Incrementally update/add nodes; rebuild edges from scratch
  let structureChanged = false;
  GRAPH.edges = [];
  GRAPH.nodeOrder = [];

  for (const step of payload.steps) {
    GRAPH.nodeOrder.push(step.id);
    const parentIds = step.parent_ids || (step.parent_id ? [step.parent_id] : []);

    for (const pid of parentIds) {
      if (pid) GRAPH.edges.push({ from: pid, to: step.id });
    }

    const existing = GRAPH.nodes.get(step.id);
    if (existing) {
      // Update in-place (avoid full DOM recreation).
      // Must check ALL fields that finalize() or delta updates may change —
      // status, detail, description, AND title (finalize renames the last
      // phase to "诊断完成" via title mutation).
      if (existing.status !== step.status ||
          existing.detail !== (step.detail || "") ||
          existing.description !== (step.description || "") ||
          existing.title !== step.title) {
        existing.status = step.status;
        existing.detail = step.detail || "";
        existing.description = step.description || "";
        existing.title = step.title;
        if (existing.el) {
          const newEl = createNodeDOM(existing);
          existing.el.replaceWith(newEl);
          existing.el = newEl;
        }
      }
    } else {
      GRAPH.nodes.set(step.id, {
        id: step.id,
        title: step.title,
        parentId: step.parent_id,
        parentIds: parentIds,
        status: step.status,
        nodeType: step.node_type,
        detail: step.detail || "",
        description: step.description || "",
        toolName: step.tool_name || "",
        toolArgs: step.tool_args || "",
        el: null,
      });
      structureChanged = true;
    }
  }

  if (structureChanged) {
    renderTree();
  } else {
    // Only edges or status changed — redraw edges without full DOM rebuild
    requestAnimationFrame(() => {
      requestAnimationFrame(() => drawAllEdges());
    });
    _scheduleEdgeSafetyNet();
  }
}

// ═══════════════════ Tree Delta Handler (Incremental) ═══════════════════

function onTreeDelta(payload) {
  if (!payload) return;

  GRAPH.hasContent = true;
  graphEmpty.style.display = "none";
  setGraphStatus("running");

  let structureChanged = false;

  // Process added nodes
  if (payload.added) {
    for (const step of payload.added) {
      const parentIds = step.parent_ids || (step.parent_id ? [step.parent_id] : []);
      const alreadyExists = GRAPH.nodes.has(step.id);
      if (alreadyExists) {
        console.warn(`[onTreeDelta] DUPLICATE node id=${step.id} title="${step.title}" — already in GRAPH.nodes (el=${GRAPH.nodes.get(step.id)?.el ? 'set' : 'null'}), overwriting el=null`);
      } else {
        console.debug(`[onTreeDelta] ADD node id=${step.id} title="${step.title}" parent_ids=${JSON.stringify(parentIds)}`);
      }
      GRAPH.nodes.set(step.id, {
        id: step.id,
        title: step.title,
        parentId: step.parent_id,
        parentIds: parentIds,
        status: step.status,
        nodeType: step.node_type,
        detail: step.detail || "",
        description: step.description || "",
        toolName: step.tool_name || "",
        toolArgs: step.tool_args || "",
        el: null,
      });
      if (!alreadyExists) {
        GRAPH.nodeOrder.push(step.id);
      }
      // Register parent→child edges immediately so drawAllEdges can draw
      // them on the very next renderTree() call without waiting for a
      // full tree_snapshot.
      if (!alreadyExists) {
        for (const pid of parentIds) {
          if (pid) GRAPH.edges.push({ from: pid, to: step.id });
        }
      }
      structureChanged = true;
    }
  }

  // Process updated nodes (status / detail / args / description changes)
  if (payload.updated) {
    for (const upd of payload.updated) {
      const existing = GRAPH.nodes.get(upd.id);
      if (!existing) continue;
      let domDirty = false;
      if (upd.status && upd.status !== existing.status) {
        existing.status = upd.status;
        domDirty = true;
      }
      if (upd.detail !== undefined && upd.detail !== existing.detail) {
        existing.detail = upd.detail;
        domDirty = true;
      }
      if (upd.tool_args !== undefined && upd.tool_args !== existing.toolArgs) {
        existing.toolArgs = upd.tool_args;
        domDirty = true;
      }
      if (upd.description !== undefined && upd.description !== existing.description) {
        existing.description = upd.description;
        domDirty = true;
      }
      if (upd.title !== undefined && upd.title !== existing.title) {
        existing.title = upd.title;
        domDirty = true;
      }
      if (domDirty && existing.el) {
        const newEl = createNodeDOM(existing);
        existing.el.replaceWith(newEl);
        existing.el = newEl;
      }
    }
  }

  if (structureChanged) {
    // New nodes added — full re-render for correct BFS layout
    renderTree();
  } else {
    // Only status updates — redraw edges without DOM rebuild
    requestAnimationFrame(() => {
      requestAnimationFrame(() => drawAllEdges());
    });
    _scheduleEdgeSafetyNet();
  }
}

// ═══════════════════ Tree Rendering (Top-Down) ═══════════════════

// ResizeObserver-based edge trigger: fires drawAllEdges() as soon as
// the last newly-created node has a real height (reflow complete).
// Falls back to a 300ms safety-net for background tabs / GPU stall.
let _edgeSafetyTimer = 0;
let _edgeReflowObserver = null;

function _scheduleEdgeSafetyNet() {
  clearTimeout(_edgeSafetyTimer);
  _edgeSafetyTimer = setTimeout(() => {
    if (GRAPH.nodes.size > 0 && GRAPH.edges.length > 0) {
      drawAllEdges();
    }
  }, 300);
}

// Watch a sentinel element; fire drawAllEdges() once it has height > 0.
function _watchReflowThenDraw(sentinelEl) {
  // Disconnect any previous observer first.
  if (_edgeReflowObserver) {
    _edgeReflowObserver.disconnect();
    _edgeReflowObserver = null;
  }
  if (!sentinelEl) return;

  // If already has height, wait one rAF to let the full layout settle
  // (other nodes may still be reflowing due to the new node's size impact)
  // before drawing edges.
  if (sentinelEl.getBoundingClientRect().height > 0) {
    requestAnimationFrame(() => {
      clearTimeout(_edgeSafetyTimer);
      drawAllEdges();
    });
    return;
  }

  _edgeReflowObserver = new ResizeObserver((entries, obs) => {
    for (const entry of entries) {
      const h = entry.contentRect ? entry.contentRect.height
                                  : entry.target.getBoundingClientRect().height;
      if (h > 0) {
        obs.disconnect();
        _edgeReflowObserver = null;
        clearTimeout(_edgeSafetyTimer); // cancel safety-net — not needed
        // One rAF to let sibling/ancestor layout settle after this node
        // got its height, then draw.
        requestAnimationFrame(() => drawAllEdges());
        return;
      }
    }
  });
  _edgeReflowObserver.observe(sentinelEl);
}

function renderTree() {
  graphNodes.innerHTML = "";

  // Build levels using BFS from root
  const levels = buildLevels();

  // Create the tree container with vertical layout
  const container = document.createElement("div");
  container.className = "tree-container";

  let lastEl = null; // sentinel: last node element added

  for (let li = 0; li < levels.length; li++) {
    const level = levels[li];
    const levelEl = document.createElement("div");
    levelEl.className = "tree-level";

    for (const nodeId of level) {
      const node = GRAPH.nodes.get(nodeId);
      if (!node) continue;
      const el = createNodeDOM(node);
      levelEl.appendChild(el);
      node.el = el;
      lastEl = el;
    }

    container.appendChild(levelEl);

    // Add connector line between levels (except after the last)
    if (li < levels.length - 1) {
      const connector = document.createElement("div");
      connector.className = "tree-connector";
      connector.dataset.levelIdx = li;
      container.appendChild(connector);
    }
  }

  graphNodes.appendChild(container);

  // Use ResizeObserver on the last node to detect reflow completion.
  // Safety-net (300ms) fires if ResizeObserver never reports height > 0.
  _scheduleEdgeSafetyNet();
  _watchReflowThenDraw(lastEl);

  // Only auto-scroll if user hasn't scrolled up manually
  if (!graphUserScrolled) {
    requestAnimationFrame(() => {
      graphCanvas.scrollTop = graphCanvas.scrollHeight;
    });
  }
}

function buildLevels(nodes, nodeOrder) {
  // Accept optional params for reuse with history graph; default to GRAPH globals
  const _nodes = nodes || GRAPH.nodes;
  const _nodeOrder = nodeOrder || GRAPH.nodeOrder;

  // Pre-build parent→children index for O(N) BFS
  const childrenOf = new Map();
  const roots = [];
  for (const id of _nodeOrder) {
    const node = _nodes.get(id);
    if (!node) continue;
    if (!node.parentId) {
      roots.push(id);
    }
    for (const pid of (node.parentIds || (node.parentId ? [node.parentId] : []))) {
      if (!pid) continue;
      if (!childrenOf.has(pid)) childrenOf.set(pid, []);
      childrenOf.get(pid).push(id);
    }
  }

  if (roots.length === 0 && _nodeOrder.length > 0) {
    roots.push(_nodeOrder[0]);
  }

  const levels = [];
  const visited = new Set(roots);
  let queue = [...roots];
  if (queue.length) levels.push([...roots]);

  while (queue.length > 0) {
    const next = [];
    for (const parentId of queue) {
      for (const cid of (childrenOf.get(parentId) || [])) {
        if (!visited.has(cid)) {
          visited.add(cid);
          next.push(cid);
        }
      }
    }
    if (next.length === 0) break;
    levels.push([...new Set(next)]);
    queue = next;
  }

  // Add any remaining unvisited nodes (shouldn't happen normally)
  for (const id of _nodeOrder) {
    if (!visited.has(id)) {
      if (levels.length === 0) levels.push([]);
      levels[levels.length - 1].push(id);
    }
  }

  return levels;
}

function createNodeDOM(node) {
  const el = document.createElement("div");
  const typeClass = node.nodeType === "phase" ? "phase" : "tool";
  el.className = `tnode ${typeClass} ${node.status}`;
  el.dataset.nodeId = node.id;

  if (node.nodeType === "phase") {
    const statusIcon = node.status === "running" ? "🔄" :
                       node.status === "completed" ? "✓" : "○";
    el.innerHTML = `
      <div class="tnode-icon phase-icon">${statusIcon}</div>
      <div class="tnode-content">
        <div class="tnode-title">${esc(node.title)}</div>
        ${node.description ? `<div class="tnode-desc">${esc(node.description)}</div>` : ""}
      </div>
    `;
    if (node.status === "running") {
      el.classList.add("pulsing");
    }
  } else {
    // Tool node
    const funcCall = node.toolName
      ? `${node.toolName}(${node.toolArgs || ""})`
      : "";
    const statusIcon = node.status === "running" ? "⚙" :
                       node.status === "completed" ? "✓" :
                       node.status === "error" ? "✕" : "○";
    el.innerHTML = `
      <div class="tnode-icon tool-icon">${statusIcon}</div>
      <div class="tnode-content">
        <div class="tnode-title">${esc(node.title)}</div>
        ${funcCall ? `<div class="tnode-func">${esc(funcCall)}</div>` : ""}
        ${node.description ? `<div class="tnode-desc">${esc(node.description)}</div>` : ""}
      </div>
    `;
    el.addEventListener("click", () => showToolPopup(node));
  }

  return el;
}

// ═══════════════════ Edge Drawing (SVG) ═══════════════════

function drawAllEdges() {
  const svg = graphEdges;
  const canvas = graphCanvas;
  const sx = canvas.scrollLeft;
  const sy = canvas.scrollTop;
  const canvasRect = canvas.getBoundingClientRect();
  const sw = canvas.scrollWidth;
  const sh = canvas.scrollHeight;

  svg.setAttribute("viewBox", `0 0 ${sw} ${sh}`);
  svg.setAttribute("width", sw);
  svg.setAttribute("height", sh);

  // Compute average node width for proportional sizing
  let totalW = 0, count = 0;
  for (const node of GRAPH.nodes.values()) {
    if (node.el) {
      totalW += node.el.getBoundingClientRect().width;
      count++;
    }
  }
  const avgNodeW = count > 0 ? totalW / count : 200;
  const scale = avgNodeW / 200;

  const arrowW = Math.round(5 * scale);
  const arrowH = Math.round(4 * scale);
  const arrowRefX = Math.round(4.2 * scale);

  const baseSw = Math.max(1.2, 1.6 * scale);
  const accentSw = Math.max(1.5, 2.2 * scale);

  const arrowColor = "#8e94a8";
  let svgContent = `<defs>
    <marker id="arrowHead" markerWidth="${arrowW}" markerHeight="${arrowH}" refX="${arrowRefX}" refY="${arrowH/2}" orient="auto">
      <polygon points="0,0 ${arrowW},${arrowH/2} 0,${arrowH}" fill="${arrowColor}"/>
    </marker>
    <filter id="edgeGlow">
      <feGaussianBlur stdDeviation="0.8" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;

  let skippedNoEl = [], skippedZeroH = [], drawn = 0;

  for (const edge of GRAPH.edges) {
    const fromNode = GRAPH.nodes.get(edge.from);
    const toNode = GRAPH.nodes.get(edge.to);
    if (!fromNode || !toNode) continue;
    if (!fromNode.el || !toNode.el) {
      skippedNoEl.push(`${edge.from}->${edge.to}(fromEl=${!!fromNode?.el},toEl=${!!toNode?.el})`);
      continue;
    }

    const fromRect = fromNode.el.getBoundingClientRect();
    const toRect = toNode.el.getBoundingClientRect();

    // Skip nodes whose layout hasn't been computed yet (reflow not done).
    // height === 0 means the DOM element exists but getBoundingClientRect
    // hasn't measured a real height — drawing would produce y=0 coords.
    // Re-watch the zero-height node so drawAllEdges retries immediately
    // once that node's reflow completes (instead of waiting 300ms).
    if (fromRect.height === 0 || toRect.height === 0) {
      skippedZeroH.push(`${edge.from}(h=${fromRect.height.toFixed(1)})->${edge.to}(h=${toRect.height.toFixed(1)})`);
      const zeroEl = fromRect.height === 0 ? fromNode.el : toNode.el;
      _watchReflowThenDraw(zeroEl);
      continue;
    }
    drawn++;

    // Account for scroll offset so edges follow nodes
    const x1 = fromRect.left - canvasRect.left + fromRect.width / 2 + sx;
    const y1 = fromRect.bottom - canvasRect.top + sy;
    const x2 = toRect.left - canvasRect.left + toRect.width / 2 + sx;
    const y2 = toRect.top - canvasRect.top + sy;

    const midY1 = y1 + Math.max(20, (y2 - y1) * 0.35);
    const midY2 = y2 - Math.max(20, (y2 - y1) * 0.35);

    let strokeSw, color, dash, extraStyle;
    // Tool→Phase edges: the tool (parent) is always completed when a new
    // Phase is created — use green to show the transition is done.
    const isToolToPhase = (fromNode.nodeType === "tool" && toNode.nodeType === "phase");
    if (isToolToPhase) {
      strokeSw = baseSw; color = "#4ade80"; dash = ""; extraStyle = "";
    } else if (toNode.status === "running") {
      strokeSw = accentSw; color = "#6c8cff";
      dash = 'stroke-dasharray="6 3"';
      extraStyle = 'filter="url(#edgeGlow)"';
    } else if (toNode.status === "completed") {
      strokeSw = baseSw; color = "#4ade80"; dash = ""; extraStyle = "";
    } else if (toNode.status === "error") {
      strokeSw = baseSw; color = "#f87171"; dash = ""; extraStyle = "";
    } else {
      strokeSw = baseSw; color = "#8e94a8"; dash = ""; extraStyle = "";
    }

    svgContent += `<path
      d="M${x1},${y1} C${x1},${midY1} ${x2},${midY2} ${x2},${y2}"
      stroke="${color}" stroke-width="${strokeSw.toFixed(1)}" ${dash}
      fill="none" stroke-linecap="round" marker-end="url(#arrowHead)"
      ${extraStyle} class="gedge"/>`;
  }

  svg.innerHTML = svgContent;

  // Diagnostic log — always visible so we can catch missing-edge issues
  if (skippedNoEl.length > 0 || skippedZeroH.length > 0) {
    console.warn(`[drawAllEdges] drawn=${drawn} skippedNoEl=${skippedNoEl.length} skippedZeroH=${skippedZeroH.length}`,
      skippedNoEl.length ? '\nnoEl:' + skippedNoEl.join(', ') : '',
      skippedZeroH.length ? '\nzeroH:' + skippedZeroH.join(', ') : '');
  } else {
    console.debug(`[drawAllEdges] drawn=${drawn} total edges=${GRAPH.edges.length}`);
  }
}

// Debounced edge redraw via RAF
let _edgeRafId = 0;
function _scheduleEdgeRedraw() {
  cancelAnimationFrame(_edgeRafId);
  _edgeRafId = requestAnimationFrame(() => drawAllEdges());
}
window.addEventListener("resize", () => {
  if (GRAPH.hasContent) _scheduleEdgeRedraw();
});
graphCanvas.addEventListener("scroll", () => {
  if (GRAPH.hasContent) _scheduleEdgeRedraw();
});
// Track if user scrolled graph up (suppresses auto-scroll)
graphCanvas.addEventListener("scroll", () => {
  const dist = graphCanvas.scrollHeight - graphCanvas.scrollTop - graphCanvas.clientHeight;
  graphUserScrolled = dist > 80;
});

function resetGraph() {
  GRAPH.nodes.clear();
  GRAPH.edges = [];
  GRAPH.nodeOrder = [];
  GRAPH.hasContent = false;
  graphNodes.innerHTML = "";
  graphEdges.innerHTML = "";
  graphEmpty.style.display = "";
  setGraphStatus("idle");
}

function finalizeGraph() {
  // Mark all running nodes as completed — update DOM in-place (no recreation)
  for (const [id, node] of GRAPH.nodes) {
    if (node.status === "running") {
      node.status = "completed";
      if (node.el) {
        node.el.classList.remove("running", "pulsing");
        node.el.classList.add("completed");
        const icon = node.el.querySelector(".phase-icon, .tool-icon");
        if (icon) icon.textContent = "✓";
      }
    }
  }
  drawAllEdges();
  setGraphStatus("done");
}

// ═══════════════════ Markdown Renderer ═══════════════════

function renderMarkdown(text) {
  if (!text) return "";
  // Sanitize raw text BEFORE markdown processing so that
  // user-supplied <tags> never reach innerHTML.  Markdown
  // sigils (# * ` etc.) are untouched by escNoBr.
  let out = escNoBr(text);

  // ── Phase 0: extract & protect code blocks ──
  // Fenced and inline code are extracted BEFORE other transforms so that
  // list detection, \n→<br>, etc. never penetrate code content.
  const codeStore = [];
  // Fenced code blocks ```...```
  out = out.replace(/```(\w*)\r?\n([\s\S]*?)```/g,
    (_, lang, code) => {
      const idx = codeStore.length;
      // code was already escaped by the outer escNoBr; trimEnd preserves
      // internal newlines which <pre> renders natively.
      codeStore.push(`<pre><code>${code.trimEnd()}</code></pre>`);
      return `\n\x00CB${idx}\x00\n`;
    });
  // Inline code `...`
  out = out.replace(/`([^`]+)`/g, (_, code) => {
    const idx = codeStore.length;
    codeStore.push(`<code>${code}</code>`);
    return `\x00CB${idx}\x00`;
  });

  // ── Phase 1: block-level elements ──

  // Headers
  out = out.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  out = out.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  out = out.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Bold **...** or __...__
  out = out.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/__(.+?)__/g, '<strong>$1</strong>');

  // Italic *...* or _..._
  out = out.replace(/\*(.+?)\*/g, '<em>$1</em>');
  out = out.replace(/\b_(.+?)_\b/g, '<em>$1</em>');

  // Links
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  // Blockquote > (processed before escNoBr)
  out = out.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');

  // ── Phase 2: tables — detect consecutive |...| lines ──
  {
    const lines = out.split('\n');
    let i = 0;
    const result = [];
    while (i < lines.length) {
      // Skip code-block placeholders
      if (/^\x00CB\d+\x00$/.test(lines[i].trim())) { result.push(lines[i]); i++; continue; }
      // Try to match a table: at least 2 consecutive |...| lines
      const tableStart = i;
      while (i < lines.length && /^\|.+\|$/.test(lines[i].trim())) i++;
      const tableLines = lines.slice(tableStart, i);
      if (tableLines.length === 0) {
        // No pipe line here — pass through and advance
        result.push(lines[i]);
        i++;
      } else if (tableLines.length >= 2 && tableLines[1].trim().match(/^\|[-: |]+\|$/)) {
        // Valid table: [header, separator, ...rows]
        const headerCells = tableLines[0].trim().split('|').filter(c => c.trim() !== '').map(c => `<th>${c.trim()}</th>`);
        const headerRow = `<tr>${headerCells.join('')}</tr>`;
        const dataRows = tableLines.slice(2).map(line => {
          const cells = line.trim().split('|').filter(c => c !== '').map(c => `<td>${c.trim()}</td>`);
          return `<tr>${cells.join('')}</tr>`;
        });
        result.push(`<table><thead>${headerRow}</thead><tbody>${dataRows.join('')}</tbody></table>`);
      } else {
        // Pipe line(s) but not a valid table — push back
        for (const l of tableLines) result.push(l);
      }
    }
    out = result.join('\n');
  }

  // ── Phase 3: lists — process lines into <ul>/<ol> blocks ──
  const lines = out.split('\n');
  let inUl = false, inOl = false;
  for (let i = 0; i < lines.length; i++) {
    // Skip code-block placeholder lines
    if (/^\x00CB\d+\x00$/.test(lines[i].trim())) {
      if (inUl) { lines[i - 1] += '</ul>'; inUl = false; }
      if (inOl) { lines[i - 1] += '</ol>'; inOl = false; }
      continue;
    }
    let m;
    if ((m = lines[i].match(/^[-*] (.+)$/))) {
      if (!inUl) { lines[i] = '<ul><li>' + m[1] + '</li>'; inUl = true; }
      else { lines[i] = '<li>' + m[1] + '</li>'; }
    } else if ((m = lines[i].match(/^\d+\. (.+)$/))) {
      if (!inOl) { lines[i] = '<ol><li>' + m[1] + '</li>'; inOl = true; }
      else { lines[i] = '<li>' + m[1] + '</li>'; }
    } else {
      if (inUl) { lines[i - 1] += '</ul>'; inUl = false; }
      if (inOl) { lines[i - 1] += '</ol>'; inOl = false; }
    }
  }
  if (inUl) lines[lines.length - 1] += '</ul>';
  if (inOl) lines[lines.length - 1] += '</ol>';
  out = lines.join('\n');

  // ── Phase 4: paragraphs and cleanup ──
  // (raw text already sanitized at entry point)

  // Double newline → paragraph break
  out = out.replace(/\n\n+/g, '</p><p>');
  // Remaining single \n → line break (GFM-style)
  out = out.replace(/\n/g, '<br>');
  out = '<p>' + out + '</p>';

  // Clean up empty paragraphs and artifacts
  out = out.replace(/<p>\s*<\/p>/g, '');
  out = out.replace(/<p><\/p>/g, '');
  out = out.replace(/<p>(<[uo]l>)/g, '$1');
  out = out.replace(/(<\/[uo]l>)<\/p>/g, '$1');
  out = out.replace(/<p>(<pre>)/g, '$1');
  out = out.replace(/(<\/pre>)<\/p>/g, '$1');
  out = out.replace(/<p>(<blockquote>)/g, '$1');
  out = out.replace(/(<\/blockquote>)<\/p>/g, '$1');
  out = out.replace(/<p>(<table>)/g, '$1');
  out = out.replace(/(<\/table>)<\/p>/g, '$1');
  out = out.replace(/<p>(<h[123]>)/g, '$1');
  out = out.replace(/(<\/h[123]>)<\/p>/g, '$1');

  // ── Phase 5: restore protected code blocks ──
  out = out.replace(/\x00CB(\d+)\x00/g, (_, idx) => codeStore[Number(idx)]);

  return out;
}

// ═══════════════════ Auto-scroll ═══════════════════

messagesEl.addEventListener("scroll", () => {
  const dist = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
  userScrolledUp = dist > 120;
});

function autoScroll() {
  if (!userScrolledUp) {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

// ═══════════════════ Tool Popup ═══════════════════

function showToolPopup(node) {
  toolPopupTitle.textContent = node.title;
  const statusText = node.status === "running" ? "执行中…"
    : node.status === "completed" ? "已完成"
    : node.status === "error" ? "失败" : "等待";

  toolPopupBody.innerHTML = `
    ${node.description ? `
    <div class="popup-section">
      <div class="popup-label">说明</div>
      <pre>${esc(node.description)}</pre>
    </div>
    ` : ""}
    <div class="popup-section">
      <div class="popup-label">状态</div>
      <pre>${esc(statusText)}</pre>
    </div>
    ${node.detail ? `
    <div class="popup-section">
      <div class="popup-label">调用ID</div>
      <pre>${esc(node.detail)}</pre>
    </div>
    ` : ""}
  `;
  toolPopup.classList.remove("hidden");
}

// ═══════════════════ Chat Helpers ═══════════════════

function appendMsg(role, text) {
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;
  const avatarIcon = role === "user" ? "👤" : "◈";
  msg.innerHTML = `
    <div class="msg-avatar">${avatarIcon}</div>
    <div class="msg-bubble">${esc(text)}</div>
  `;
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendEvent(text) {
  const msg = document.createElement("div");
  msg.className = "msg event";
  msg.innerHTML = `<div class="msg-bubble">${esc(text)}</div>`;
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setRunning(running) {
  inputEl.disabled = running;
  if (running) {
    _startStalenessCheck();
    actionBtn.classList.add("running");
    actionBtn.innerHTML = '<span>停止</span>';
    actionBtn.disabled = false;
    actionBtn.type = "button";
    setStatus("running");
    setGraphStatus("running");
  } else {
    _stopStalenessCheck();
    actionBtn.classList.remove("running");
    actionBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg><span>发送</span>';
    actionBtn.disabled = false;
    actionBtn.type = "submit";
    setStatus("ready");
  }
}

function setStatus(state) {
  statusLabel.textContent = { running: "分析中…", ready: "就绪", error: "出错", cancelling: "取消中…" }[state] || state;
  statusLabel.className = "status-text" + (state === "running" ? " running" : state === "error" ? " error" : "");
}

function setGraphStatus(state, msg) {
  const text = msg || { idle: "等待任务", running: "执行中", done: "已完成" }[state] || state;
  graphStatus.textContent = text;
  graphStatus.className = "graph-status " + state;
}

function esc(s) {
  if (!s) return "";
  const d = document.createElement("div");
  d.textContent = typeof s === "string" ? s : JSON.stringify(s);
  return d.innerHTML;
}

function escNoBr(s) {
  if (!s) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ═══════════════════ Hypothesis Tree ═══════════════════

let currentLedger = null;
const tabExecution = document.getElementById("tabExecution");
const tabHypothesis = document.getElementById("tabHypothesis");
const hypothesisBadge = document.getElementById("hypothesisBadge");
const hypothesisTree = document.getElementById("hypothesisTree");
const hypothesisEmpty = document.getElementById("hypothesisEmpty");

// Tab switching
tabExecution.addEventListener("click", () => switchTab("execution"));
tabHypothesis.addEventListener("click", () => switchTab("hypothesis"));

function switchTab(tab) {
  document.querySelectorAll(".graph-tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
  if (tab === "execution") {
    tabExecution.classList.add("active");
    document.querySelector('[data-panel="execution"]').classList.add("active");
  } else {
    tabHypothesis.classList.add("active");
    document.querySelector('[data-panel="hypothesis"]').classList.add("active");
    hypothesisBadge?.classList.add("hidden");
  }
}

function onLedgerSnapshot(payload) {
  const ledger = payload.ledger;
  if (!ledger) return;
  currentLedger = ledger;

  // Show badge on hypothesis tab
  const hypCount = Object.keys(ledger.hypotheses || {}).length;
  if (hypCount > 0 && hypothesisBadge) {
    hypothesisBadge.textContent = hypCount;
    hypothesisBadge.classList.remove("hidden");
  }

  // Auto-switch to hypothesis tab on first hypothesis
  if (hypCount === 1 && !currentLedger._userSwitchedAway) {
    // Don't auto-switch; let user decide. Just show badge.
  }

  renderHypothesisTree(ledger);
}

function renderHypothesisTree(ledger) {
  if (!ledger) return;
  const hypotheses = ledger.hypotheses || {};
  const rootIds = ledger.root_hypothesis_ids || [];

  if (rootIds.length === 0) {
    if (hypothesisEmpty) hypothesisEmpty.style.display = "flex";
    if (hypothesisTree) hypothesisTree.innerHTML = "";
    return;
  }
  if (hypothesisEmpty) hypothesisEmpty.style.display = "none";

  let html = "";

  // Phase banner
  const phase = ledger.current_phase || "understand";
  const round = ledger.current_round || 0;
  const activePath = ledger.active_path || [];
  const phaseLabels = {
    understand: "理解故障",
    hypothesize: "形成假设",
    verify: "验证假设",
    evaluate: "评估路径",
    skill_verify: "技能验证",
    backtrack: "回溯",
    report: "生成报告",
  };
  html += `<div class="hyp-phase-banner">`;
  html += `阶段: <strong>${esc(phaseLabels[phase] || phase)}</strong> · 步骤: ${round}`;
  if (activePath.length > 0) {
    html += ` · 活动路径: <strong>${esc(activePath.join(" → "))}</strong>`;
  }
  if (ledger.root_cause) {
    html += `<br>🎯 根因: <strong>${esc(ledger.root_cause)}</strong>`;
  }
  html += `</div>`;

  // Render tree recursively
  for (const hid of rootIds) {
    html += renderHypothesisNode(hypotheses[hid], hypotheses, 0);
  }

  if (hypothesisTree) hypothesisTree.innerHTML = html;
}

function renderHypothesisNode(node, allHypotheses, depth) {
  if (!node) return "";
  const status = node.status || "pending";
  const prob = node.probability || 0;
  const selected = node.selected;
  const isFocus = selected && status === "verifying";

  let cls = `hyp-node ${status}`;
  if (selected) cls += " selected";

  let html = `<div class="${cls}">`;

  // Header: ID + status + probability bar + statement
  html += `<div class="hyp-header">`;
  html += `<span class="hyp-id">${esc(node.id)}</span>`;
  html += `<span class="hyp-status ${status}">${esc(status)}</span>`;
  html += `<div class="hyp-prob-bar"><div class="hyp-prob-fill" style="width:${prob}%"></div></div>`;
  html += `<span class="hyp-prob-text">${prob}%</span>`;
  if (selected) html += `<span class="hyp-star">★</span>`;
  if (isFocus) html += `<span class="hyp-focus-tag">← 当前聚焦</span>`;
  html += `</div>`;

  // Statement
  html += `<div class="hyp-statement">${esc(node.statement)}</div>`;

  // Evidence
  if (node.evidence && node.evidence.length > 0) {
    html += `<div class="hyp-evidence">`;
    for (const ev of node.evidence) {
      const evCls = ev.supports ? "support" : "refute";
      const src = ev.source || "";
      html += `<div class="hyp-evidence-item ${evCls}">${esc(ev.summary || "")}`;
      if (src) html += ` <span style="opacity:0.6">(${esc(src)})</span>`;
      html += `</div>`;
    }
    html += `</div>`;
  }

  // Verification tools
  if (node.verification_tools && node.verification_tools.length > 0) {
    html += `<div class="hyp-tools">🔧 ${esc(node.verification_tools.join(", "))}</div>`;
  }

  // Rationale (for refuted/confirmed/deprioritized)
  if (node.rationale && status !== "pending" && status !== "verifying") {
    html += `<div class="hyp-evidence-item" style="margin-top:4px;font-size:11px;color:var(--text-2)">`;
    html += `理由: ${esc(node.rationale)}</div>`;
  }

  html += `</div>`; // close hyp-node

  // Children
  if (node.sub_hypothesis_ids && node.sub_hypothesis_ids.length > 0) {
    html += `<div class="hyp-children">`;
    for (const sid of node.sub_hypothesis_ids) {
      html += renderHypothesisNode(allHypotheses[sid], allHypotheses, depth + 1);
    }
    html += `</div>`;
  }

  return html;
}

// Track if user manually switched away from hypothesis tab
tabExecution?.addEventListener("click", () => {
  if (currentLedger) currentLedger._userSwitchedAway = true;
});

