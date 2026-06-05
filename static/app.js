const messagesEl = document.querySelector("#messages");
const formEl = document.querySelector("#chatForm");
const inputEl = document.querySelector("#promptInput");
const sendBtn = document.querySelector("#sendBtn");
const stopBtn = document.querySelector("#stopBtn");
const statusEl = document.querySelector("#status");
const treePanel = document.querySelector("#treePanel");
const treeContainer = document.querySelector("#treeContainer");
const toggleTreeBtn = document.querySelector("#toggleTreeBtn");
const closeTreeBtn = document.querySelector("#closeTreeBtn");

let sessionId = localStorage.getItem("diagnostics.sessionId") || null;
let controller = null;

// ── 当前消息 UI 引用 ──
let activeThinkSection = null;
let activeThinkContent = null;
let activeThinkStatus = null;
let activeThinkToggle = null;
let activeAnswerEl = null;
let activeBubble = null;

// ── 面板切换 ──
let treeVisible = true;

function setTreeVisible(visible) {
  treeVisible = visible;
  if (visible) {
    treePanel.classList.remove("hidden");
    toggleTreeBtn.textContent = "隐藏步骤";
  } else {
    treePanel.classList.add("hidden");
    toggleTreeBtn.textContent = "显示步骤";
  }
}

toggleTreeBtn.addEventListener("click", () => {
  setTreeVisible(!treeVisible);
});
closeTreeBtn.addEventListener("click", () => {
  setTreeVisible(false);
});

// ── 重置 UI 状态 ──
function resetMessageUI() {
  activeThinkSection = null;
  activeThinkContent = null;
  activeThinkStatus = null;
  activeThinkToggle = null;
  activeAnswerEl = null;
  activeBubble = null;
}

function resetTree() {
  treeContainer.innerHTML = '<div class="tree-empty">等待诊断开始…</div>';
}

// ── 思考区域 ──
function ensureThinkSection() {
  if (activeThinkSection) return;
  if (!activeBubble) return;

  const section = document.createElement("div");
  section.className = "think-section";

  const toggle = document.createElement("button");
  toggle.className = "think-toggle open";
  toggle.type = "button";
  toggle.innerHTML =
    '<span class="arrow">▶</span><span class="think-label">思考过程</span><span class="think-status">思考中…</span>';

  const content = document.createElement("div");
  content.className = "think-content open";

  toggle.addEventListener("click", () => {
    const isOpen = content.classList.toggle("open");
    toggle.classList.toggle("open", isOpen);
    if (isOpen) content.scrollTop = content.scrollHeight;
  });

  section.appendChild(toggle);
  section.appendChild(content);
  if (activeAnswerEl) {
    activeBubble.insertBefore(section, activeAnswerEl);
  } else {
    activeBubble.appendChild(section);
  }

  activeThinkSection = section;
  activeThinkContent = content;
  activeThinkStatus = toggle.querySelector(".think-status");
  activeThinkToggle = toggle;
}

function ensureAnswerContainer() {
  if (activeAnswerEl) return;
  if (!activeBubble) return;
  const answer = document.createElement("div");
  answer.className = "final-answer";
  activeBubble.appendChild(answer);
  activeAnswerEl = answer;
}

// ── 树渲染（从 snapshot 完整重建）──
function iconFor(nodeType, status) {
  if (status === "running") return "◉";
  if (status === "completed") return "✓";
  if (status === "error") return "✕";
  if (nodeType === "root") return "⬤";
  if (nodeType === "phase") return "▶";
  if (nodeType === "tool") return "⚙";
  return "○";
}

function statusLabel(status) {
  return { pending: "等待", running: "执行", completed: "完成", error: "失败" }[status] || status;
}

function renderTree(steps) {
  treeContainer.innerHTML = "";

  if (!steps || steps.length === 0) {
    treeContainer.innerHTML = '<div class="tree-empty">发送诊断请求后，步骤将在此显示</div>';
    return;
  }

  // Build parent→children map
  const children = new Map();
  for (const s of steps) {
    const pid = s.parent_id || "__root__";
    if (!children.has(pid)) children.set(pid, []);
    children.get(pid).push(s);
  }

  const rootItems = children.get("__root__") || [];
  if (rootItems.length === 0 && steps.length > 0) {
    // All nodes have parent_id → use "root" as virtual root's children
    rootItems.push(...(children.get("root") || []));
  }

  const rootUl = document.createElement("ul");
  rootUl.className = "tree";
  treeContainer.appendChild(rootUl);

  for (const item of rootItems) {
    buildSubtree(item, children, rootUl);
  }
}

function buildSubtree(step, children, parentUl) {
  const li = document.createElement("li");

  const nodeEl = document.createElement("div");
  nodeEl.className = `tree-node ${step.status}`;
  if (step.node_type) nodeEl.classList.add(`type-${step.node_type}`);

  const icon = document.createElement("span");
  icon.className = "node-icon";
  icon.textContent = iconFor(step.node_type, step.status);

  const content = document.createElement("div");
  content.className = "node-content";

  const title = document.createElement("div");
  title.className = "node-title";
  title.textContent = step.title;

  const badge = document.createElement("span");
  badge.className = "node-status-badge";
  badge.textContent = statusLabel(step.status);

  content.appendChild(title);
  content.appendChild(badge);
  nodeEl.appendChild(icon);
  nodeEl.appendChild(content);
  li.appendChild(nodeEl);
  parentUl.appendChild(li);

  // Recurse into children
  const kids = children.get(step.id);
  if (kids && kids.length > 0) {
    const childUl = document.createElement("ul");
    li.appendChild(childUl);
    for (const kid of kids) {
      buildSubtree(kid, children, childUl);
    }
  }
}

// ── 表单提交 ──
formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt || controller) return;

  appendMessage("user", prompt);
  inputEl.value = "";
  resetMessageUI();
  resetTree();

  const article = document.createElement("article");
  article.className = "message assistant";
  activeBubble = document.createElement("div");
  activeBubble.className = "bubble";
  article.appendChild(activeBubble);
  messagesEl.appendChild(article);

  setRunning(true, "分析中…");

  controller = new AbortController();
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: prompt, session_id: sessionId }),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error(await response.text());
    await readEventStream(response.body);
  } catch (error) {
    if (error.name !== "AbortError") {
      appendEvent(`error: ${error.message}`);
      setStatus("Error");
    }
  } finally {
    controller = null;
    resetMessageUI();
    setRunning(false, "Ready");
  }
});

// ── 中止 ──
stopBtn.addEventListener("click", async () => {
  if (!controller) return;
  const currentSession = sessionId;
  controller.abort();
  setStatus("Cancelling");
  if (currentSession) {
    await fetch(`/api/sessions/${currentSession}/cancel`, { method: "POST" }).catch(() => {});
  }
  appendEvent("cancelled by user");
});

// ── 快捷键 ──
inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    formEl.requestSubmit();
  }
});

// ── SSE 解析 ──
async function readEventStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleSseEvent(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleSseEvent(raw) {
  const lines = raw.split("\n");
  const eventLine = lines.find((l) => l.startsWith("event: "));
  const dataLine = lines.find((l) => l.startsWith("data: "));
  const type = eventLine ? eventLine.slice(7) : "message";
  let payload = {};
  try { payload = dataLine ? JSON.parse(dataLine.slice(6)) : {}; } catch { payload = {}; }

  switch (type) {
    case "session":
      sessionId = payload.session_id;
      localStorage.setItem("diagnostics.sessionId", sessionId);
      break;

    case "think_start":
      ensureThinkSection();
      break;

    case "think_token":
      ensureThinkSection();
      if (activeThinkContent) {
        activeThinkContent.textContent += payload.text;
        activeThinkContent.scrollTop = activeThinkContent.scrollHeight;
      }
      scrollToBottom();
      break;

    case "think_end":
      break;

    case "tree_snapshot":
      renderTree(payload.steps);
      if (!treeVisible) setTreeVisible(true);
      break;

    case "token":
      ensureAnswerContainer();
      if (activeAnswerEl) activeAnswerEl.textContent += payload.text;
      if (activeThinkToggle && activeThinkContent) {
        activeThinkToggle.classList.remove("open");
        activeThinkContent.classList.remove("open");
      }
      scrollToBottom();
      break;

    case "error":
      appendEvent(`${payload.message}\n${payload.hint || ""}`.trim());
      setStatus("Error");
      break;

    case "cancelled":
      appendEvent("server cancelled");
      setStatus("Cancelled");
      break;

    case "done":
      setStatus("Ready");
      break;
  }
}

// ── 辅助 ──
function appendMessage(role, text) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  article.appendChild(bubble);
  messagesEl.appendChild(article);
  scrollToBottom();
  return bubble;
}

function appendEvent(text) {
  const article = document.createElement("article");
  article.className = "message event";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  article.appendChild(bubble);
  messagesEl.appendChild(article);
  scrollToBottom();
}

function setRunning(running, label) {
  sendBtn.disabled = running;
  stopBtn.disabled = !running;
  inputEl.disabled = running;
  setStatus(label);
}

function setStatus(label) {
  statusEl.textContent = label;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}
