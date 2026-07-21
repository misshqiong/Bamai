import {api} from "./api.js";
import {formatBytes} from "./charts.js";
import {formatDateTime, formatNumber, t} from "./i18n.js";


class ToolboxController {
  constructor() {
    this.cards = document.getElementById("toolbox-cards");
    this.detail = document.getElementById("toolbox-detail");
    this.specs = [];
    this.current = null;
    this.pollTimer = null;
  }

  init() {
    window.addEventListener("languagechange", () => {
      this.renderCards();
      if (this.current) this.select(this.current.id);
    });
    this.load();
  }

  async load() {
    try {
      this.specs = (await api.toolbox()).items;
      this.renderCards();
    } catch (error) {
      this.cards.replaceChildren(this.notice("error", t("toolbox.error", {message: error.message})));
    }
  }

  renderCards() {
    this.cards.replaceChildren();
    for (const spec of this.specs) {
      const button = document.createElement("button");
      button.type = "button"; button.className = `toolbox-card${this.current?.id === spec.id ? " active" : ""}`;
      const icon = document.createElement("span"); icon.className = "toolbox-icon"; icon.textContent = spec.icon;
      const text = document.createElement("span");
      const title = document.createElement("strong"); title.textContent = t(spec.name_key);
      const desc = document.createElement("small"); desc.textContent = t(spec.desc_key);
      text.append(title, desc); button.append(icon, text);
      button.addEventListener("click", () => this.select(spec.id));
      this.cards.append(button);
    }
  }

  select(probeId) {
    this.current = this.specs.find(item => item.id === probeId);
    if (!this.current) return;
    clearTimeout(this.pollTimer);
    this.renderCards();
    this.detail.classList.remove("hidden");
    this.detail.replaceChildren();
    const heading = document.createElement("div"); heading.className = "toolbox-detail-heading";
    const title = document.createElement("h2"); title.textContent = t(this.current.name_key);
    const desc = document.createElement("p"); desc.textContent = t(this.current.desc_key);
    heading.append(title, desc); this.detail.append(heading);
    if (this.current.needs_authorization && !this.current.authorized) {
      this.renderAuthorization();
      return;
    }
    this.renderForm();
  }

  renderAuthorization() {
    const card = document.createElement("div"); card.className = "capture-auth";
    const title = document.createElement("h3"); title.textContent = t("toolbox.auth.title");
    const text = document.createElement("p"); text.textContent = t("toolbox.auth.text");
    const command = document.createElement("code"); command.textContent = "sudo ./scripts/enable-capture.sh";
    const retry = document.createElement("button"); retry.className = "secondary"; retry.type = "button"; retry.textContent = t("toolbox.auth.retry");
    retry.addEventListener("click", async () => { await this.load(); this.select("capture"); });
    card.append(title, text, command, retry); this.detail.append(card);
  }

  renderForm() {
    const form = document.createElement("form"); form.className = "probe-form";
    for (const param of this.current.params) {
      const label = document.createElement("label");
      const caption = document.createElement("span"); caption.textContent = t(param.label_key);
      let input;
      if (param.type === "choice") {
        input = document.createElement("select");
        for (const choice of param.choices || []) {
          const option = document.createElement("option"); option.value = choice; option.textContent = choice;
          input.append(option);
        }
      } else {
        input = document.createElement("input"); input.type = param.type === "int" ? "number" : "text";
      }
      input.name = param.name; input.required = Boolean(param.required);
      if (param.default != null) input.value = param.default;
      if (param.min != null) input.min = param.min;
      if (param.max != null && param.type === "int") input.max = param.max;
      label.append(caption, input); form.append(label);
    }
    const run = document.createElement("button"); run.className = "primary"; run.type = "submit"; run.textContent = t("toolbox.run");
    form.append(run);
    const output = document.createElement("div"); output.className = "probe-output";
    form.addEventListener("submit", event => { event.preventDefault(); this.run(form, output); });
    this.detail.append(form, output);
  }

  paramsFrom(form) {
    const params = {};
    for (const definition of this.current.params) {
      const value = form.elements.namedItem(definition.name).value;
      params[definition.name] = definition.type === "int" ? Number(value) : value;
    }
    return params;
  }

  async run(form, output) {
    const button = form.querySelector('button[type="submit"]'); button.disabled = true;
    output.replaceChildren(this.notice("loading", t("toolbox.running")));
    try {
      const started = await api.runProbe(this.current.id, this.paramsFrom(form));
      if (started.status === "needs_auth") { this.renderAuthorization(); return; }
      this.poll(started.job_id, output, button);
    } catch (error) {
      button.disabled = false;
      output.replaceChildren(this.notice("error", t("toolbox.error", {message: error.message})));
    }
  }

  async poll(jobId, output, button) {
    try {
      const job = await api.probeJob(jobId);
      if (job.status === "running") {
        this.pollTimer = setTimeout(() => this.poll(jobId, output, button), 600);
      } else if (job.status === "done") {
        button.disabled = false; this.renderResult(output, job.result);
      } else if (job.status === "needs_auth") {
        button.disabled = false; this.renderAuthorization();
      } else {
        button.disabled = false;
        output.replaceChildren(this.notice("error", t("toolbox.error", {message: job.error || job.status})));
      }
    } catch (error) {
      button.disabled = false;
      output.replaceChildren(this.notice("error", t("toolbox.error", {message: error.message})));
    }
  }

  renderResult(output, result) {
    output.replaceChildren();
    const heading = document.createElement("h3"); heading.textContent = t("toolbox.result"); output.append(heading);
    const summary = document.createElement("dl"); summary.className = "probe-summary";
    for (const [key, value] of Object.entries(result.summary || {})) {
      const term = document.createElement("dt"); term.textContent = this.fieldLabel(key);
      const detail = document.createElement("dd"); detail.textContent = this.formatValue(key, value);
      summary.append(term, detail);
    }
    output.append(summary);
    if (result.rows?.length) output.append(this.rowsTable(result.rows));
    if (result.raw_output) {
      const disclosure = document.createElement("details");
      const label = document.createElement("summary"); label.textContent = t("toolbox.raw");
      const raw = document.createElement("pre"); raw.textContent = result.raw_output;
      disclosure.append(label, raw); output.append(disclosure);
    }
    for (const artifact of result.artifacts || []) {
      const link = document.createElement("a"); link.className = "artifact-link";
      link.href = artifact.download_url; link.textContent = `${t("toolbox.download")} · ${artifact.name}`;
      output.append(link);
    }
    const explain = document.createElement("button"); explain.className = "secondary probe-explain"; explain.type = "button"; explain.textContent = t("toolbox.explain");
    const analysis = document.createElement("div"); analysis.className = "probe-analysis hidden";
    explain.addEventListener("click", () => this.explain(explain, analysis));
    output.append(explain, analysis);
  }

  rowsTable(rows) {
    const wrap = document.createElement("div"); wrap.className = "table-wrap probe-rows";
    const table = document.createElement("table"), head = table.createTHead().insertRow(), body = table.createTBody();
    const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
    for (const key of columns) { const cell = document.createElement("th"); cell.textContent = this.fieldLabel(key); head.append(cell); }
    for (const row of rows) {
      const line = body.insertRow();
      for (const key of columns) line.insertCell().textContent = this.formatValue(key, row[key]);
    }
    wrap.append(table); return wrap;
  }

  async explain(button, container) {
    button.disabled = true; button.textContent = t("toolbox.explaining");
    try {
      const response = await api.explainProbe(this.current.id);
      container.classList.remove("hidden"); container.textContent = response.reply;
    } catch (error) {
      container.classList.remove("hidden"); container.textContent = t("toolbox.error", {message: error.message});
    } finally {
      button.disabled = false; button.textContent = t("toolbox.explain");
    }
  }

  fieldLabel(key) { const value = t(`toolbox.field.${key}`); return value.startsWith("toolbox.field.") ? key : value; }
  formatValue(key, value) {
    if (value == null) return "—";
    if (typeof value === "boolean") return t(value ? "toolbox.value.yes" : "toolbox.value.no");
    if (typeof value === "object") return JSON.stringify(value);
    if (key.endsWith("_bytes") || key === "bytes" || key.endsWith("_rss")) return formatBytes(value);
    if (key.endsWith("_ts")) return formatDateTime(value);
    if (typeof value === "number") return formatNumber(value, {maximumFractionDigits: 2});
    const translated = t(`toolbox.value.${value}`); return translated.startsWith("toolbox.value.") ? String(value) : translated;
  }
  notice(className, text) { const item = document.createElement("p"); item.className = className; item.textContent = text; return item; }
}


export function initToolbox() {
  const controller = new ToolboxController(); controller.init(); return controller;
}
