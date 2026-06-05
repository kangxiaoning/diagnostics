const messagesEl = document.querySelector("#messages");
const formEl = document.querySelector("#chatForm");
const inputEl = document.querySelector("#promptInput");
const sendBtn = document.querySelector("#sendBtn");
const stopBtn = document.querySelector("#stopBtn");
const statusEl = document.querySelector("#status");

let sessionId = localStorage.getItem("diagnostics.sessionId") || null;
let controller = null;
let activeAssistantBubble = null;

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = inputEl.value.trim();
  if (!prompt || controller) return;

  appendMessage("user", prompt);
  inputEl.value = "";
  activeAssistantBubble = appendMessage("assistant", "");
  setRunning(true, "Running");

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
    activeAssistantBubble = null;
    setRunning(false, "Ready");
  }
});

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

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    formEl.requestSubmit();
  }
});

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
  const payload = dataLine ? JSON.parse(dataLine.slice(6)) : {};

  if (type === "session") {
    sessionId = payload.session_id;
    localStorage.setItem("diagnostics.sessionId", sessionId);
    return;
  }

  if (type === "token") {
    activeAssistantBubble.textContent += payload.text;
    scrollToBottom();
    return;
  }

  if (type === "tool_call") {
    appendEvent(`tool_call: ${JSON.stringify(payload.tool_calls)}`);
    return;
  }

  if (type === "update") {
    const summary = compactUpdate(payload.data);
    if (summary) appendEvent(summary);
    return;
  }

  if (type === "error") {
    appendEvent(`${payload.message}\n${payload.hint || ""}`.trim());
    setStatus("Error");
    return;
  }

  if (type === "cancelled") {
    appendEvent("server cancelled");
    setStatus("Cancelled");
    return;
  }

  if (type === "done") {
    setStatus("Ready");
  }
}

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

function compactUpdate(data) {
  if (!data || typeof data !== "object") return "";
  return Object.entries(data)
    .map(([node, value]) => {
      if (value?.tool_calls?.length) {
        const names = value.tool_calls.map((call) => call.name || call.function?.name || "tool");
        return `${node}: calling ${names.join(", ")}`;
      }
      if (value?.message_type === "ToolMessage") {
        return `${node}: tool result`;
      }
      return "";
    })
    .filter(Boolean)
    .join("\n");
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
