import {api} from "./api.js";

const INSTALL_COMMAND = "brew install ollama && ollama pull qwen3:4b";

class ChatController {
  constructor() {
    this.messages = [];
    this.ready = false;
    this.busy = false;
    this.list = document.getElementById("chat-messages");
    this.guide = document.getElementById("ollama-guide");
    this.state = document.getElementById("ollama-state");
    this.form = document.getElementById("chat-form");
    this.input = document.getElementById("chat-text");
    this.send = document.getElementById("chat-send");
    this.retry = document.getElementById("ollama-retry");
  }

  init() {
    this.form.addEventListener("submit", event => { event.preventDefault(); this.submit(); });
    this.retry.addEventListener("click", () => this.checkStatus());
    this.input.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); this.submit(); }
    });
    this.checkStatus();
  }

  async checkStatus() {
    this.retry.disabled = true;
    this.state.textContent = "检测中";
    this.state.className = "ollama-state";
    try {
      const status = await api.ollamaStatus();
      this.setReady(Boolean(status.available && status.model_pulled));
    } catch {
      this.setReady(false);
    } finally {
      this.retry.disabled = false;
    }
  }

  setReady(ready) {
    this.ready = ready;
    this.guide.classList.toggle("hidden", ready);
    this.list.classList.toggle("hidden", !ready);
    this.state.textContent = ready ? "qwen3:4b 就绪" : "未就绪";
    this.state.className = `ollama-state ${ready ? "ready" : "offline"}`;
    this.input.disabled = !ready || this.busy;
    this.send.disabled = !ready || this.busy;
    if (ready && this.list.childElementCount === 0) {
      this.addBubble("assistant", "你好，我是 MacPilot。你可以问我当前资源占用、历史趋势、异常事件或本机文件。", []);
    }
  }

  async submit() {
    const content = this.input.value.trim();
    if (!content || !this.ready || this.busy) return;
    this.messages.push({role: "user", content});
    this.addBubble("user", content, []);
    this.input.value = "";
    this.setBusy(true);
    const pending = this.addPending();
    try {
      const response = await api.chat(this.messages.slice(-30));
      pending.remove();
      this.messages.push({role: "assistant", content: response.reply});
      this.addBubble("assistant", response.reply, response.tool_trace || []);
    } catch (error) {
      pending.remove();
      this.addBubble("assistant", `暂时无法回答：${error.message}`, []);
      if (error.status === 503) this.setReady(false);
    } finally {
      this.setBusy(false);
      if (this.ready) this.input.focus();
    }
  }

  setBusy(busy) {
    this.busy = busy;
    this.input.disabled = busy || !this.ready;
    this.send.disabled = busy || !this.ready;
    this.send.textContent = busy ? "思考中" : "发送";
  }

  addBubble(role, content, trace) {
    const item = document.createElement("article");
    item.className = `chat-message ${role}`;
    const body = document.createElement("div");
    body.className = "chat-bubble";
    body.textContent = content;
    item.append(body);
    if (trace.length) {
      const details = document.createElement("details");
      details.className = "tool-trace";
      const summary = document.createElement("summary");
      summary.textContent = `🔧 查看 ${trace.length} 项查询过程`;
      details.append(summary);
      for (const entry of trace) {
        const line = document.createElement("p");
        line.textContent = entry.summary || entry.tool;
        details.append(line);
      }
      item.append(details);
    }
    this.list.append(item);
    this.scrollBottom();
    return item;
  }

  addPending() {
    const item = document.createElement("article");
    item.className = "chat-message assistant pending";
    item.textContent = "正在分析本机数据…";
    this.list.append(item);
    this.scrollBottom();
    return item;
  }

  scrollBottom() { this.list.scrollTop = this.list.scrollHeight; }
}

export function initChat() {
  const controller = new ChatController();
  controller.init();
  return controller;
}

export {INSTALL_COMMAND};
