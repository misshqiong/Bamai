import {api} from "./api.js";
import {formatBytes} from "./charts.js";
import {getLanguage, setLanguage, t} from "./i18n.js";


class SettingsController {
  constructor() {
    this.form = document.getElementById("settings-form");
    this.models = document.getElementById("settings-models");
    this.language = document.getElementById("settings-language");
    this.temperature = document.getElementById("settings-temperature");
    this.temperatureValue = document.getElementById("temperature-value");
    this.context = document.getElementById("settings-context");
    this.message = document.getElementById("settings-message");
    this.pullPanel = document.getElementById("pull-panel");
    this.pullProgress = document.getElementById("pull-progress");
    this.pullPercent = document.getElementById("pull-percent");
    this.pullStatus = document.getElementById("pull-status");
    this.settings = null;
    this.modelData = null;
    this.pollTimer = null;
  }

  init() {
    this.form.addEventListener("submit", event => { event.preventDefault(); this.save(); });
    this.temperature.addEventListener("input", () => {
      this.temperatureValue.value = Number(this.temperature.value).toFixed(1);
    });
    window.addEventListener("languagechange", () => {
      if (this.modelData) this.renderModels(this.modelData);
      if (this.pullPanel.classList.contains("hidden")) this.message.textContent = "";
    });
    this.load();
  }

  async load() {
    try {
      this.settings = await api.settings();
      if (this.settings.language !== getLanguage()) setLanguage(this.settings.language);
      this.language.value = this.settings.language;
      this.temperature.value = this.settings.temperature;
      this.temperatureValue.value = Number(this.settings.temperature).toFixed(1);
      this.context.value = this.settings.num_ctx;
      await this.loadModels();
      const pull = await api.pullStatus();
      if (pull.status === "pulling") this.showPull(pull);
    } catch (error) {
      this.models.replaceChildren(this.notice(t("settings.modelsUnavailable")));
    }
  }

  async loadModels() {
    try {
      this.modelData = await api.ollamaModels();
      this.renderModels(this.modelData);
    } catch {
      this.modelData = {installed: [], recommended: []};
      this.models.replaceChildren(this.notice(t("settings.modelsUnavailable")));
    }
  }

  renderModels(data) {
    const installed = new Map(data.installed.map(item => [item.name, item]));
    const recommendations = new Map(data.recommended.map(item => [item.name, item]));
    for (const item of data.installed) {
      if (!recommendations.has(item.name)) recommendations.set(item.name, {name: item.name});
    }
    this.models.replaceChildren();
    if (!recommendations.size) {
      this.models.append(this.notice(t("settings.noModels")));
      return;
    }
    for (const recommendation of recommendations.values()) {
      const local = installed.get(recommendation.name);
      const row = document.createElement("label");
      row.className = "model-option";
      const radio = document.createElement("input");
      radio.type = "radio"; radio.name = "settings-model"; radio.value = recommendation.name;
      radio.disabled = !local; radio.checked = recommendation.name === this.settings?.model;
      const details = document.createElement("span");
      const name = document.createElement("strong"); name.textContent = recommendation.name;
      const meta = document.createElement("small");
      meta.textContent = local
        ? `${t("settings.installed")} · ${local.size ? formatBytes(local.size) : t("settings.sizeUnknown")}`
        : t("settings.recommendedFor", {memory: recommendation.memory_gb || "—", size: recommendation.size_gb || "—"});
      details.append(name, meta); row.append(radio, details);
      if (!local) {
        const download = document.createElement("button");
        download.type = "button"; download.className = "secondary"; download.textContent = t("settings.download");
        download.addEventListener("click", () => this.pull(recommendation.name));
        row.append(download);
      }
      this.models.append(row);
    }
  }

  notice(text) {
    const paragraph = document.createElement("p"); paragraph.className = "empty"; paragraph.textContent = text;
    return paragraph;
  }

  async save() {
    const selected = this.form.querySelector('input[name="settings-model"]:checked');
    const payload = {
      temperature: Number(this.temperature.value),
      num_ctx: Number(this.context.value),
      language: this.language.value,
    };
    if (selected && selected.value !== this.settings?.model) payload.model = selected.value;
    const button = document.getElementById("settings-save");
    button.disabled = true; this.message.textContent = t("settings.saving");
    try {
      this.settings = await api.saveSettings(payload);
      setLanguage(this.settings.language);
      this.message.textContent = t("settings.saved");
      window.dispatchEvent(new CustomEvent("settingschanged", {detail: this.settings}));
    } catch (error) {
      this.message.textContent = t("settings.saveFailed", {message: error.message});
    } finally {
      button.disabled = false;
    }
  }

  async pull(model) {
    try {
      await api.pullModel(model);
      this.showPull({model, status: "pulling", percent: 0});
    } catch (error) {
      this.message.textContent = t("settings.pullFailed", {message: error.message});
    }
  }

  showPull(state) {
    this.pullPanel.classList.remove("hidden");
    this.pullProgress.value = state.percent;
    this.pullPercent.textContent = `${state.percent}%`;
    this.pullStatus.textContent = t(`settings.pull.${state.status}`, {model: state.model});
    clearTimeout(this.pollTimer);
    if (state.status === "pulling") {
      this.pollTimer = setTimeout(async () => {
        try { this.showPull(await api.pullStatus()); }
        catch { this.showPull({...state, status: "error"}); }
      }, 1000);
    } else if (state.status === "done") {
      this.loadModels();
      window.dispatchEvent(new CustomEvent("settingschanged"));
    }
  }
}


export function initSettings() {
  const controller = new SettingsController();
  controller.init();
  return controller;
}
