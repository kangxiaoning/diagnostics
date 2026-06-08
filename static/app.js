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
const toolPopup = document.getElementById("toolPopup");
const toolPopupTitle = document.getElementById("toolPopupTitle");
const toolPopupBody = document.getElementById("toolPopupBody");
const toolPopupClose = document.getElementById("toolPopupClose");

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

document.addEventListener("mousemove", (e) => {
  if (!isResizing) return;
  const appRect = document.querySelector(".app").getBoundingClientRect();
  const pct = ((e.clientX - appRect.left) / appRect.width) * 100;
  const clamped = Math.max(25, Math.min(75, pct));
  chatPanel.style.width = clamped + "%";
});

document.addEventListener("mouseup", () => {
  if (!isResizing) return;
  isResizing = false;
  resizer.classList.remove("active");
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  if (GRAPH.hasContent) {
    requestAnimationFrame(() => drawAllEdges());
  }
});

// ═══════════════════ Form submit ═══════════════════
formEl.addEventListener("submit", async (e) => {
  e.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt || controller) return;

  appendMsg("user", prompt);
  inputEl.value = "";
  inputEl.style.height = "auto";
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
      body: JSON.stringify({ message: prompt, session_id: sessionId }),
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

function parseSSE(raw) {
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
    case "agent_start":
      break;
    case "agent_end":
      break;
    case "done":
      setStatus("ready");
      finalizeGraph();
      finishStreamUI("done");
      break;
    case "error":
      appendEvent(payload.message);
      setStatus("error");
      setGraphStatus("idle");
      finishStreamUI("error");
      break;
    case "cancelled":
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

  // Update graph state from snapshot
  GRAPH.nodes.clear();
  GRAPH.edges = [];
  GRAPH.nodeOrder = [];

  for (const step of payload.steps) {
    GRAPH.nodes.set(step.id, {
      id: step.id,
      title: step.title,
      parentId: step.parent_id,
      parentIds: step.parent_ids || (step.parent_id ? [step.parent_id] : []),
      status: step.status,
      nodeType: step.node_type,
      detail: step.detail || "",
      description: step.description || "",
      toolName: step.tool_name || "",
      toolArgs: step.tool_args || "",
      el: null,
    });
    GRAPH.nodeOrder.push(step.id);

    // Build edges from parent_ids
    for (const pid of (step.parent_ids || (step.parent_id ? [step.parent_id] : []))) {
      if (pid) {
        GRAPH.edges.push({ from: pid, to: step.id });
      }
    }
  }

  renderTree();
}

// ═══════════════════ Tree Rendering (Top-Down) ═══════════════════

function renderTree() {
  graphNodes.innerHTML = "";

  // Build levels using BFS from root
  const levels = buildLevels();

  // Create the tree container with vertical layout
  const container = document.createElement("div");
  container.className = "tree-container";

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

  // Ensure DOM layout is stable before drawing edges.
  // offsetTop/offsetLeft depend on computed layout.
  requestAnimationFrame(() => {
    setTimeout(() => drawAllEdges(), 50);
  });
  requestAnimationFrame(() => {
    graphCanvas.scrollTop = graphCanvas.scrollHeight;
  });
}

function buildLevels() {
  // BFS from root to determine levels
  const levels = [];
  const visited = new Set();
  const queue = [];

  // Find root nodes (no parent or parent_id is null)
  const roots = [];
  for (const id of GRAPH.nodeOrder) {
    const node = GRAPH.nodes.get(id);
    if (!node) continue;
    if (node.nodeType === "root" || !node.parentId) {
      roots.push(id);
    }
  }

  if (roots.length === 0 && GRAPH.nodeOrder.length > 0) {
    roots.push(GRAPH.nodeOrder[0]);
  }

  queue.push(...roots);
  for (const r of roots) visited.add(r);
  levels.push([...roots]);

  while (queue.length > 0) {
    const currentLevel = [...queue];
    queue.length = 0;
    const nextLevel = [];

    for (const parentId of currentLevel) {
      // Find children whose parent_ids include this node
      for (const id of GRAPH.nodeOrder) {
        if (visited.has(id)) continue;
        const node = GRAPH.nodes.get(id);
        if (!node) continue;
        if (node.parentIds.includes(parentId) || node.parentId === parentId) {
          nextLevel.push(id);
          visited.add(id);
        }
      }
    }

    if (nextLevel.length === 0) break;

    // Deduplicate and preserve order
    const uniqueNext = [...new Set(nextLevel)];
    levels.push(uniqueNext);
    queue.push(...uniqueNext);
  }

  // Add any remaining unvisited nodes (shouldn't happen normally)
  for (const id of GRAPH.nodeOrder) {
    if (!visited.has(id)) {
      if (levels.length === 0) levels.push([]);
      levels[levels.length - 1].push(id);
    }
  }

  return levels;
}

function createNodeDOM(node) {
  const el = document.createElement("div");
  const typeClass = node.nodeType === "root" ? "root" :
                    node.nodeType === "phase" ? "phase" : "tool";
  el.className = `tnode ${typeClass} ${node.status}`;
  el.dataset.nodeId = node.id;

  if (node.nodeType === "root") {
    el.innerHTML = `
      <div class="tnode-icon">◈</div>
      <div class="tnode-content">
        <div class="tnode-title">${esc(node.title)}</div>
      </div>
    `;
  } else if (node.nodeType === "phase") {
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
  const nodesRect = graphNodes.getBoundingClientRect();

  // Compute positions relative to graphNodes (shared origin with SVG).
  // Both are children of graphCanvas at top:0 left:0.
  function nodeLeft(el) { return el.getBoundingClientRect().left - nodesRect.left; }
  function nodeTop(el) { return el.getBoundingClientRect().top - nodesRect.top; }

  // Compute viewBox from node positions + sizes
  let maxX = 0, maxY = 0, totalW = 0, count = 0;
  for (const node of GRAPH.nodes.values()) {
    if (!node.el) continue;
    const w = node.el.offsetWidth, h = node.el.offsetHeight;
    const nx = nodeLeft(node.el) + w, ny = nodeTop(node.el) + h;
    if (nx > maxX) maxX = nx; if (ny > maxY) maxY = ny;
    totalW += w; count++;
  }
  const pad = 60;
  const sw = maxX + pad;
  const sh = maxY + pad;

  svg.setAttribute("viewBox", `0 0 ${sw} ${sh}`);
  svg.setAttribute("width", sw);
  svg.setAttribute("height", sh);

  const avgNodeW = count > 0 ? totalW / count : 200;
  const scale = avgNodeW / 200;
  const arrowW = Math.round(5 * scale), arrowH = Math.round(4 * scale);
  const arrowRefX = Math.round(4.2 * scale);
  const baseSw = Math.max(1.2, 1.6 * scale);
  const accentSw = Math.max(1.5, 2.2 * scale);

  let svgContent = `<defs>
    <marker id="arrowHead" markerWidth="${arrowW}" markerHeight="${arrowH}" refX="${arrowRefX}" refY="${arrowH/2}" orient="auto">
      <polygon points="0,0 ${arrowW},${arrowH/2} 0,${arrowH}" fill="#8e94a8"/>
    </marker>
    <filter id="edgeGlow">
      <feGaussianBlur stdDeviation="0.8" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;

  for (const edge of GRAPH.edges) {
    const fromNode = GRAPH.nodes.get(edge.from);
    const toNode = GRAPH.nodes.get(edge.to);
    if (!fromNode || !toNode) continue;
    if (!fromNode.el || !toNode.el) continue;

    const x1 = nodeLeft(fromNode.el) + fromNode.el.offsetWidth / 2;
    const y1 = nodeTop(fromNode.el) + fromNode.el.offsetHeight;
    const x2 = nodeLeft(toNode.el) + toNode.el.offsetWidth / 2;
    const y2 = nodeTop(toNode.el);

    const midY1 = y1 + Math.max(20, (y2 - y1) * 0.35);
    const midY2 = y2 - Math.max(20, (y2 - y1) * 0.35);

    let strokeSw, color, dash, extraStyle;
    if (toNode.status === "running") {
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
  // Ensure SVG container matches content height
  svg.style.minHeight = (sh + 120) + "px";
}

// Redraw on resize or scroll
window.addEventListener("resize", () => {
  if (GRAPH.hasContent) drawAllEdges();
});
graphCanvas.addEventListener("scroll", () => {
  if (GRAPH.hasContent) drawAllEdges();
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
  // Mark all running nodes as completed
  for (const [id, node] of GRAPH.nodes) {
    if (node.status === "running") {
      node.status = "completed";
      if (node.el) {
        node.el.className = `tnode ${node.nodeType === "root" ? "root" : node.nodeType === "phase" ? "phase" : "tool"} completed`;
        // Re-render the node to update icon
        const newEl = createNodeDOM(node);
        node.el.replaceWith(newEl);
        node.el = newEl;
      }
    }
  }
  drawAllEdges();
  setGraphStatus("done");
}

// ═══════════════════ Markdown Renderer ═══════════════════

function renderMarkdown(text) {
  if (!text) return "";
  let out = escNoBr(text);

  // Fenced code blocks ```...```
  out = out.replace(/```(\w*)\n([\s\S]*?)```/g,
    (_, lang, code) => `<pre><code>${code.trimEnd()}</code></pre>`);

  // Inline code `...`
  out = out.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Headers
  out = out.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  out = out.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  out = out.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Bold **...** or __...__
  out = out.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/__(.+?)__/g, '<strong>$1</strong>');

  // Italic *...* or _..._ (not inside words)
  out = out.replace(/\*(.+?)\*/g, '<em>$1</em>');
  out = out.replace(/\b_(.+?)_\b/g, '<em>$1</em>');

  // Blockquote >
  out = out.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // Unordered lists
  out = out.replace(/^(\s*)[-*] (.+)$/gm, (_, indent, item) => {
    const depth = indent.length ? 1 : 0;
    return depth ? `  <li>${item}</li>` : `</ul><ul><li>${item}</li>`;
  });
  out = out.replace(/<\/ul><ul>/, '</ul>\n<ul>');
  out = out.replace(/^<li>/, '<ul><li>');
  out = out.replace(/<\/li>$/, '</li></ul>');

  // Ordered lists
  out = out.replace(/^(\s*)\d+\. (.+)$/gm, (_, indent, item) => {
    const depth = indent.length ? 1 : 0;
    return depth ? `  <li>${item}</li>` : `</ol><ol><li>${item}</li>`;
  });
  out = out.replace(/<\/ol><ol>/, '</ol>\n<ol>');
  out = out.replace(/^<li>/, '<ol><li>');
  out = out.replace(/<\/li>$/, '</li></ol>');

  // Links
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  // Double newline → paragraph break
  out = out.replace(/\n\n+/g, '</p><p>');
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
  out = out.replace(/<p>(<h[123]>)/g, '$1');
  out = out.replace(/(<\/h[123]>)<\/p>/g, '$1');

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
    actionBtn.classList.add("running");
    actionBtn.innerHTML = '<span>停止</span>';
    actionBtn.disabled = false;
    actionBtn.type = "button";
    setStatus("running");
    setGraphStatus("running");
  } else {
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

function setGraphStatus(state) {
  graphStatus.textContent = { idle: "等待任务", running: "执行中", done: "已完成" }[state] || state;
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
