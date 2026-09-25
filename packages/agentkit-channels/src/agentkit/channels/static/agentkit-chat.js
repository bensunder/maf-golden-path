/* agentkit web chat: a dependency-free <agentkit-chat> element that speaks AG-UI.
 *
 *   <script type="module" src="/chat/agentkit-chat.js"></script>
 *   <agentkit-chat endpoint="/v1/agui" title="Order Status Agent"></agentkit-chat>
 *
 * Streams replies, shows tool activity, and turns approval interrupts into Approve / Reject buttons
 * (or "waiting for an approver" under separation of duties). Everything the agent or a tool produced is
 * rendered with textContent, never as HTML. Auth is the page's own (Easy Auth cookie, same origin).
 */

const STYLE = `
:host { display: flex; flex-direction: column; height: 100%; min-height: 360px; font: 15px/1.45 system-ui, sans-serif;
  color: var(--akc-fg, #1b1f24); background: var(--akc-bg, #fff); border: 1px solid var(--akc-border, #d0d7de);
  border-radius: 10px; overflow: hidden; }
header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 14px;
  border-bottom: 1px solid var(--akc-border, #d0d7de); font-weight: 600; }
header button { font: inherit; font-weight: 400; font-size: 13px; }
.log { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: 85%; padding: 8px 12px; border-radius: 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.user { align-self: flex-end; background: var(--akc-user, #0b5cad); color: #fff; }
.assistant { align-self: flex-start; background: var(--akc-agent, #f1f3f5); }
.error { align-self: stretch; background: #fff1f0; color: #a4161a; }
.tool { align-self: flex-start; font-size: 13px; color: #57606a; }
.sources { align-self: flex-start; margin: -4px 0 0; padding-left: 28px; font-size: 13px; color: #57606a; }
.sources a { color: inherit; }
.tool::before { content: "⚙ "; }
.approval { align-self: stretch; border: 1px solid #d4a72c; background: #fff8e5; border-radius: 10px; padding: 10px 12px; }
.approval h4 { margin: 0 0 6px; font-size: 14px; }
.approval table { border-collapse: collapse; margin: 4px 0 8px; font-size: 13px; }
.approval td { padding: 2px 10px 2px 0; vertical-align: top; overflow-wrap: anywhere; }
.approval td:first-child { color: #57606a; }
.approval .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.approval input { flex: 1; min-width: 140px; font: inherit; padding: 4px 8px; }
form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid var(--akc-border, #d0d7de); }
textarea { flex: 1; resize: none; font: inherit; padding: 8px; border-radius: 8px; border: 1px solid var(--akc-border, #d0d7de); }
button { font: inherit; padding: 6px 14px; border-radius: 8px; border: 1px solid var(--akc-border, #d0d7de);
  background: #f6f8fa; cursor: pointer; }
button.primary { background: var(--akc-user, #0b5cad); color: #fff; border-color: transparent; }
button.danger { color: #a4161a; }
button:disabled { opacity: .5; cursor: default; }
@media (prefers-color-scheme: dark) {
  :host { --akc-fg: #e6edf3; --akc-bg: #0d1117; --akc-border: #30363d; --akc-agent: #161b22; }
  .approval { background: #2b2111; border-color: #9e6a03; } .error { background: #3c1618; color: #ffa198; }
  button { background: #21262d; color: inherit; } textarea { background: #0d1117; color: inherit; }
}`;

const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

function store(key, value) {
  try {
    if (value === undefined) return sessionStorage.getItem(key);
    if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, value);
  } catch (_) { /* storage blocked: the thread lives only in memory */ }
  return null;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v; else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

class AgentkitChat extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    this.endpoint = this.getAttribute("endpoint") || "/v1/agui";
    this.storageKey = `agentkit-chat:${this.endpoint}`;
    this.threadId = store(this.storageKey) || uuid();
    store(this.storageKey, this.threadId);

    const root = this.attachShadow({ mode: "open" });
    root.append(el("style", { text: STYLE }));
    this.log = el("div", { class: "log", role: "log", "aria-live": "polite" });
    this.input = el("textarea", { rows: "2", placeholder: this.getAttribute("placeholder") || "Ask something…",
      "aria-label": "Message" });
    this.sendButton = el("button", { class: "primary", type: "submit", text: "Send" });
    this.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this.form.requestSubmit(); }
    });
    this.form = el("form", { onsubmit: (e) => { e.preventDefault(); this.send(); } }, this.input, this.sendButton);
    const header = el("header", {}, el("span", { text: this.getAttribute("title") || "Agent" }),
      el("button", { type: "button", text: "New chat", onclick: () => this.reset() }));
    root.append(header, this.log, this.form);
  }

  disconnectedCallback() { clearInterval(this.poller); }

  reset() {
    clearInterval(this.poller);
    this.threadId = uuid();
    store(this.storageKey, this.threadId);
    this.log.replaceChildren();
    this.input.focus();
  }

  add(cls, text) {
    const node = el("div", { class: `msg ${cls}`, text });
    this.log.append(node);
    this.log.scrollTop = this.log.scrollHeight;
    return node;
  }

  busy(on) {
    this.sendButton.disabled = on;
    this.input.disabled = on;
    // Panels already on screen are spent once a run starts; a panel the run adds stays clickable.
    if (on) for (const b of this.log.querySelectorAll(".approval button, .approval input")) b.disabled = true;
    if (!on) this.input.focus();
  }

  async send() {
    const text = this.input.value.trim();
    if (!text || this.sendButton.disabled) return;
    this.input.value = "";
    this.add("user", text);
    await this.run({ messages: [{ id: uuid(), role: "user", content: text }] });
  }

  async decide(interrupts, approved, comment) {
    const resume = interrupts.map((i) => ({ interruptId: i.id, status: "resolved",
      payload: comment ? { approved, comment } : { approved } }));
    await this.run({ messages: [], resume });
  }

  async run(extra) {
    this.busy(true);
    const body = { threadId: this.threadId, runId: uuid(), state: {}, tools: [], context: [], forwardedProps: {}, ...extra };
    try {
      const response = await fetch(this.endpoint, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const data = await response.json();
          detail = typeof data.detail === "string" ? data.detail : (data.detail && data.detail.detail) || detail;
        } catch (_) { /* not JSON */ }
        if (response.status === 409 && /approval is pending/.test(detail)) detail = "Still waiting for a decision on the last request.";
        this.add("error", detail);
        return;
      }
      await this.consume(response.body.getReader());
    } catch (err) {
      this.add("error", `Connection problem: ${err.message || err}`);
    } finally {
      this.busy(false);
    }
  }

  async consume(reader) {
    const decoder = new TextDecoder();
    let buffer = "";
    const bubbles = new Map();
    const tools = new Map();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
        if (!data) continue;
        let event;
        try { event = JSON.parse(data); } catch (_) { continue; }
        this.handle(event, bubbles, tools);
      }
    }
  }

  handle(event, bubbles, tools) {
    switch (event.type) {
      case "TEXT_MESSAGE_START": bubbles.set(event.messageId, this.add("assistant", "")); break;
      case "TEXT_MESSAGE_CONTENT": {
        const node = bubbles.get(event.messageId) || this.add("assistant", "");
        bubbles.set(event.messageId, node);
        node.textContent += event.delta;
        this.log.scrollTop = this.log.scrollHeight;
        break;
      }
      case "TOOL_CALL_START": tools.set(event.toolCallId, this.add("tool", `Using ${event.toolCallName}…`)); break;
      case "TOOL_CALL_END": {
        const node = tools.get(event.toolCallId);
        if (node) node.textContent = node.textContent.replace(/…$/, "");
        break;
      }
      case "RUN_ERROR": this.add("error", event.message || "Something went wrong."); break;
      case "CUSTOM":
        if (event.name === "agentkit.citations" && event.value) {
          this.sources(bubbles.get(event.value.messageId), event.value.citations || []);
        }
        break;
      case "RUN_FINISHED":
        if (event.outcome && event.outcome.type === "interrupt") this.approval(event.outcome.interrupts || []);
        break;
      default: break;
    }
  }

  watch(url, status) {
    clearInterval(this.poller);
    if (!url) return;
    const thread = this.threadId;
    this.poller = setInterval(async () => {
      if (thread !== this.threadId) { clearInterval(this.poller); return; }
      try {
        const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) return;
        const pending = await response.json();
        if (Array.isArray(pending) && pending.length === 0) {
          clearInterval(this.poller);
          status.textContent = "An approver has decided. Send a message to continue.";
        }
      } catch (_) { /* transient: try again next tick */ }
    }, Number(this.getAttribute("poll-ms")) || 5000);
  }

  sources(bubble, citations) {
    if (!citations.length) return;
    const list = el("ol", { class: "sources", "aria-label": "Sources" });
    for (const c of citations) {
      let link = null;
      try {
        const url = c.url ? new URL(c.url, location.href) : null;
        if (url && (url.protocol === "https:" || url.protocol === "http:")) {
          link = el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer", text: c.title || c.id });
        }
      } catch (_) { /* not a URL: show the title as text */ }
      list.append(el("li", { value: String(c.n) }, link || el("span", { text: c.title || c.id })));
    }
    if (bubble) bubble.after(list); else this.log.append(list);
    this.log.scrollTop = this.log.scrollHeight;
  }

  approval(interrupts) {
    if (!interrupts.length) return;
    const waitingForApprover = interrupts.some((i) => (i.metadata || {}).awaiting === "approver");
    const panel = el("div", { class: "approval", role: "group", "aria-label": "Approval request" });
    for (const item of interrupts) {
      const meta = item.metadata || {};
      const table = el("table");
      for (const [k, v] of Object.entries(meta.arguments || {})) {
        table.append(el("tr", {}, el("td", { text: k }), el("td", { text: typeof v === "string" ? v : JSON.stringify(v) })));
      }
      panel.append(el("h4", { text: `Approve ${meta.tool || "action"}?` }), table);
    }
    if (waitingForApprover) {
      const role = interrupts[0].metadata && interrupts[0].metadata.approver_role;
      const status = el("div", { text: `Waiting for an approver${role ? ` (${role})` : ""}. You can continue once it's decided.` });
      panel.append(status);
      this.watch(interrupts[0].metadata && interrupts[0].metadata.status_url, status);
    } else {
      const comment = el("input", { type: "text", placeholder: "Comment (optional)", maxlength: "1000", "aria-label": "Comment" });
      const approve = el("button", { class: "primary", type: "button", text: "Approve",
        onclick: () => this.decide(interrupts, true, comment.value.trim()) });
      const reject = el("button", { class: "danger", type: "button", text: "Reject",
        onclick: () => this.decide(interrupts, false, comment.value.trim()) });
      panel.append(el("div", { class: "row" }, comment, approve, reject));
    }
    this.log.append(panel);
    this.log.scrollTop = this.log.scrollHeight;
  }
}

if (!customElements.get("agentkit-chat")) customElements.define("agentkit-chat", AgentkitChat);
