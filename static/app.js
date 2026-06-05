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

// ── 当前消息的 UI 引用 ──
let activeThinkSection = null;
let activeThinkContent = null;
let activeThinkStatus = null;
let activeThinkToggle = null;
let activeAnswerEl = null;
let activeBubble = null;

// ── 树状图状态 ──
let treeNodes = new Map();  // step_id -> { element, status, title, children }
let treeRootEl = null;

// ── 面板切换 ──
let treeVisible = true;

function setTreeVisible(visible) {
  treeVisible = visible;
  if (visible) {
    treePanel.classList.remove("hidden");
    toggleTreeBtn.textContent = "步骤";
  } else {
    treePanel.classList.add("hidden");
    toggleTreeBtn.textContent = "步骤";
  }
}

toggleTreeBtn.addEventListener("click", () => {
  setTreeVisible(!treeVisible);
});

closeTreeBtn.addEventListener("click", () => {
  setTreeVisible(false);
});

// ── 重置当前消息的 UI 状态 ──
function resetMessageUI() {
  activeThinkSection = null;
  activeThinkContent = null;
  activeThinkStatus = null;
  activeThinkToggle = null;
  activeAnswerEl = null;
  activeBubble = null;
}

// ── 重置树状图 ──
function resetTree() {
  treeNodes.clear();
  treeRootEl = null;
  treeContainer.innerHTML =
    '<div class="tree-empty">正在分析...</div>';
}

// ── 创建思考过程区域 ──
function ensureThinkSection() {
  if (activeThinkSection) return;
  if (!activeBubble) return;

  // 在气泡中创建思考区域
  const section = document.createElement("div");
  section.className = "think-section";

  const toggle = document.createElement("button");
  toggle.className = "think-toggle open";
  toggle.type = "button";
  toggle.innerHTML =
    '<span class="arrow">▶</span><span class="think-label">思考过程</span><span class="think-status">思考中...</span>';

  const content = document.createElement("div");
  content.className = "think-content open";

  toggle.addEventListener("click", () => {
    const isOpen = content.classList.toggle("open");
    toggle.classList.toggle("open", isOpen);
    if (isOpen) {
      content.scrollTop = content.scrollHeight;
    }
  });

  section.appendChild(toggle);
  section.appendChild(content);

  // 插入到气泡中，在 final answer 之前
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

// ── 确保有 final answer 容器 ──
function ensureAnswerContainer() {
  if (activeAnswerEl) return;
  if (!activeBubble) return;

  const answer = document.createElement("div");
  answer.className = "final-answer";
  activeBubble.appendChild(answer);
  activeAnswerEl = answer;
}

// ── 更新树状图节点 ──
function upsertTreeNode(stepId, title, parentId, status) {
  const existing = treeNodes.get(stepId);

  if (existing) {
    // 更新现有节点状态
    existing.element.classList.remove("pending", "running", "completed", "error");
    existing.element.classList.add(status);
    existing.status = status;

    const badge = existing.element.querySelector(".node-status-badge");
    if (badge) {
      badge.textContent = statusLabel(status);
    }

    // 更新图标
    const icon = existing.element.querySelector(".node-icon");
    if (icon) {
      icon.textContent = statusIcon(status);
    }
    return;
  }

  // 创建新节点
  const nodeEl = document.createElement("div");
  nodeEl.className = `tree-node ${status}`;

  const icon = document.createElement("span");
  icon.className = "node-icon";
  icon.textContent = statusIcon(status);

  const content = document.createElement("div");
  content.className = "node-content";

  const titleEl = document.createElement("div");
  titleEl.className = "node-title";
  titleEl.textContent = title;

  const badge = document.createElement("span");
  badge.className = "node-status-badge";
  badge.textContent = statusLabel(status);

  content.appendChild(titleEl);
  content.appendChild(badge);
  nodeEl.appendChild(icon);
  nodeEl.appendChild(content);

  treeNodes.set(stepId, {
    element: nodeEl,
    status: status,
    title: title,
    parentId: parentId,
    children: [],
  });

  // 插入到树中
  if (!parentId) {
    // 根节点
    if (!treeRootEl) {
      treeRootEl = document.createElement("ul");
      treeRootEl.className = "tree";
      treeContainer.innerHTML = "";
      treeContainer.appendChild(treeRootEl);
    }
    const li = document.createElement("li");
    li.appendChild(nodeEl);
    treeRootEl.appendChild(li);
    nodeEl._li = li;
  } else {
    // 子节点
    const parent = treeNodes.get(parentId);
    if (parent) {
      let childList = parent.element._li.querySelector("ul");
      if (!childList) {
        childList = document.createElement("ul");
        parent.element._li.appendChild(childList);
      }
      const li = document.createElement("li");
      li.appendChild(nodeEl);
      childList.appendChild(li);
      nodeEl._li = li;
      parent.children.push(stepId);
    }
  }
}

function statusLabel(status) {
  const labels = {
    pending: "等待中",
    running: "执行中",
    completed: "已完成",
    error: "失败",
  };
  return labels[status] || status;
}

function statusIcon(status) {
  const icons = {
    pending: "○",
    running: "◉",
    completed: "✓",
    error: "✕",
  };
  return icons[status] || "○";
}

// ── 构建完整树 ──
function buildTreeFromSnapshot(steps) {
  treeContainer.innerHTML = "";
  treeNodes.clear();
  treeRootEl = null;

  if (!steps || steps.length === 0) {
    treeContainer.innerHTML =
      '<div class="tree-empty">发送诊断请求后，步骤将在此显示</div>';
    return;
  }

  for (const step of steps) {
    upsertTreeNode(step.id, step.title, step.parent_id, step.status);
  }
}

// ── 表单提交处理 ──
formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt || controller) return;

  // 追加用户消息
  appendMessage("user", prompt);
  inputEl.value = "";

  // 重置 UI
  resetMessageUI();
  resetTree();

  // 创建 assistant 消息容器
  const article = document.createElement("article");
  article.className = "message assistant";
  activeBubble = document.createElement("div");
  activeBubble.className = "bubble";
  article.appendChild(activeBubble);
  messagesEl.appendChild(article);

  setRunning(true, "分析中...");

  controller = new AbortController();
  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: prompt, session_id: sessionId }),
      signal: controller.signal,
    });

    if (!response.ok || !response.body) {
      throw new Error(await response.text());
    }

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

// ── 中止按钮 ──
stopBtn.addEventListener("click", async () => {
  if (!controller) return;
  const currentSession = sessionId;
  controller.abort();
  setStatus("Cancelling");

  // 将运行中的步骤标记为完成（因为被中断了）
  for (const [id, node] of treeNodes) {
    if (node.status === "running") {
      upsertTreeNode(id, node.title, node.parentId, "completed");
    }
  }

  if (currentSession) {
    await fetch(`/api/sessions/${currentSession}/cancel`, {
      method: "POST",
    }).catch(() => {});
  }
  appendEvent("cancelled by user");
});

// ── 快捷键 ──
inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    formEl.requestSubmit();
  }
});

// ── SSE 事件流解析 ──
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
      const rawEvent = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      handleSseEvent(rawEvent);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleSseEvent(rawEvent) {
  const lines = rawEvent.split("\n");
  const eventLine = lines.find((line) => line.startsWith("event: "));
  const dataLine = lines.find((line) => line.startsWith("data: "));
  const type = eventLine ? eventLine.slice(7) : "message";
  let payload = {};
  try {
    payload = dataLine ? JSON.parse(dataLine.slice(6)) : {};
  } catch {
    payload = {};
  }

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
      if (activeThinkStatus) {
        activeThinkStatus.textContent = "思考完成";
        activeThinkStatus.classList.add("done");
      }
      break;

    case "step_start":
      upsertTreeNode(
        payload.step_id,
        payload.title,
        payload.parent_id,
        "running"
      );
      // 显示树面板
      if (!treeVisible) setTreeVisible(true);
      break;

    case "step_end":
      upsertTreeNode(
        payload.step_id,
        treeNodes.get(payload.step_id)?.title || "",
        treeNodes.get(payload.step_id)?.parentId || null,
        payload.status
      );
      break;

    case "tree_snapshot":
      buildTreeFromSnapshot(payload.steps);
      break;

    case "token":
      ensureAnswerContainer();
      if (activeAnswerEl) {
        activeAnswerEl.textContent += payload.text;
      }
      // 当最终答案开始输出时，折叠思考区域
      if (activeThinkToggle && activeThinkContent) {
        activeThinkToggle.classList.remove("open");
        activeThinkContent.classList.remove("open");
      }
      scrollToBottom();
      break;

    case "tool_call":
      // Step tracker handles this via step_start events
      break;

    case "update":
      // Step tracker handles this via step_end events
      break;

    case "error":
      appendEvent(`${payload.message}\n${payload.hint || ""}`.trim());
      setStatus("Error");
      // 标记运行中的步骤为错误
      for (const [id, node] of treeNodes) {
        if (node.status === "running") {
          upsertTreeNode(id, node.title, node.parentId, "error");
        }
      }
      break;

    case "cancelled":
      appendEvent("server cancelled");
      setStatus("Cancelled");
      break;

    case "done":
      setStatus("Ready");
      // 确保所有运行中的步骤完成
      for (const [id, node] of treeNodes) {
        if (node.status === "running") {
          upsertTreeNode(id, node.title, node.parentId, "completed");
        }
      }
      break;

    case "debug":
      // 静默忽略调试事件
      break;

    default:
      // 未知事件类型，静默忽略
      break;
  }
}

// ── 辅助函数 ──
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
