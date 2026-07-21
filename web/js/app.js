import {api} from "./api.js";
import {initChat} from "./chat.js";
import {initSettings} from "./settings.js";
import {initToolbox} from "./toolbox.js";
import {HistoryChart, RealtimeCharts, formatBytes, formatRate} from "./charts.js";
import {formatDateTime, formatNumber, initI18n, t} from "./i18n.js";

initI18n();

const realtimeCharts = new RealtimeCharts();
const historyChart = new HistoryChart();
let historyHours = 1;
let processSort = "cpu";
let latestMetric = null;
let latestDisks = null;
let latestHealth = null;
let healthExplanation = null;
let ollamaReady = false;

const byId = id => document.getElementById(id);
const setText = (id, text) => { byId(id).textContent = text; };

function updateCards(metric, disks = null) {
  if (!metric) return;
  latestMetric = metric;
  if (disks) latestDisks = disks;
  setText("cpu-value", `${formatNumber(metric.cpu_percent, {maximumFractionDigits: 1})}%`);
  setText("load-value", t("cards.loadValue", {value: formatNumber(metric.load_1, {maximumFractionDigits: 2})}));
  setText("memory-value", `${formatNumber(metric.mem_percent, {maximumFractionDigits: 1})}%`);
  setText("memory-detail", `${formatBytes(metric.mem_used)} / ${formatBytes(metric.mem_total)}`);
  setText("network-value", formatRate(metric.net_up_bps + metric.net_down_bps));
  setText("network-detail", `↑ ${formatRate(metric.net_up_bps)} · ↓ ${formatRate(metric.net_down_bps)}`);
  if (latestDisks?.length) {
    const disk = latestDisks.find(item => item.mount === "/System/Volumes/Data") || latestDisks[0];
    setText("disk-value", `${formatNumber(disk.percent, {maximumFractionDigits: 1})}%`);
    setText("disk-detail", `${disk.mount} · ${formatBytes(disk.used)} / ${formatBytes(disk.total)}`);
  }
}

let wsConnected = false;

function renderConnectionState() {
  byId("ws-dot").className = `status-dot ${wsConnected ? "online" : "offline"}`;
  setText("ws-status", t(wsConnected ? "connection.online" : "connection.offline"));
}

function connectRealtime() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/realtime`);
  socket.addEventListener("open", () => {
    wsConnected = true;
    renderConnectionState();
  });
  socket.addEventListener("message", event => {
    const metric = JSON.parse(event.data);
    updateCards(metric);
    realtimeCharts.push(metric);
  });
  socket.addEventListener("close", () => {
    wsConnected = false;
    renderConnectionState();
    setTimeout(connectRealtime, 2000);
  });
}

async function loadOverview() {
  try {
    const overview = await api.overview();
    updateCards(overview.metric, overview.disks);
    if (overview.metric) realtimeCharts.push(overview.metric);
    setText("event-count", formatNumber(overview.unresolved_events));
    ollamaReady = Boolean(overview.ollama?.available && overview.ollama?.model_pulled);
    byId("health-explain").disabled = !ollamaReady;
  } catch (error) { console.error(error); }
}

async function loadHealth() {
  try {
    latestHealth = await api.health();
    renderHealth();
  } catch (error) { console.error(error); }
}

function renderHealth() {
  if (!latestHealth) return;
  const level = latestHealth.level;
  const banner = byId("health-banner");
  banner.className = `health-banner ${level}`;
  setText("health-icon", level === "critical" ? "!" : level === "warn" ? "•" : "✓");
  setText("health-headline", t(`health.${level}.headline`));
  const analysis = healthExplanation || latestHealth.ai_analysis;
  setText("health-advice", t(`health.${level}.advice`));
  byId("health-advice").classList.toggle("hidden", Boolean(analysis));
  const list = byId("health-checks");
  list.replaceChildren();
  const abnormal = latestHealth.checks.filter(check => check.level !== "ok");
  for (const check of abnormal) {
    const item = document.createElement("li");
    const headline = document.createElement("strong");
    const advice = document.createElement("span");
    headline.textContent = t(`health.${check.kind}.headline`, check.params);
    advice.textContent = t(`health.${check.kind}.advice`, check.params);
    item.append(headline);
    if (!(healthExplanation || latestHealth.ai_analysis)) item.append(advice);
    list.append(item);
  }
  byId("health-ai").classList.toggle("hidden", !analysis);
  setText("health-ai-text", analysis || "");
}

async function explainHealth() {
  if (!ollamaReady) return;
  const button = byId("health-explain");
  button.disabled = true;
  button.textContent = t("health.explaining");
  try {
    healthExplanation = (await api.explainHealth()).reply;
    renderHealth();
  } catch (error) {
    console.error(error);
  } finally {
    button.disabled = !ollamaReady;
    button.textContent = t("health.explain");
  }
}

async function loadHistory() {
  const metric = byId("history-metric").value;
  const end = Math.floor(Date.now() / 1000), start = end - historyHours * 3600;
  try { historyChart.set(metric, (await api.metrics(metric, start, end)).points); }
  catch (error) { console.error(error); }
}

function fillProcessTable(items, sort) {
  const body = byId("process-body");
  body.replaceChildren();
  setText("process-value-head", t(sort === "cpu" ? "process.cpu" : sort === "memory" ? "process.memory" : "process.throughput"));
  if (!items.length) {
    const row = body.insertRow(), cell = row.insertCell();
    cell.colSpan = 3; cell.className = "empty"; cell.textContent = t("process.empty");
    return;
  }
  for (const item of items) {
    const row = body.insertRow();
    const name = row.insertCell();
    name.className = "process-name"; name.textContent = item.name; name.title = item.cmdline || item.name;
    row.insertCell().textContent = formatNumber(item.pid);
    const value = row.insertCell();
    value.textContent = sort === "cpu"
      ? `${formatNumber(item.cpu_percent, {maximumFractionDigits: 1})}%`
      : sort === "memory" ? formatBytes(item.memory_rss) : formatRate(item.up_bps + item.down_bps);
  }
}

async function loadProcesses() {
  try {
    const response = processSort === "network" ? await api.processNet() : await api.processes(processSort);
    fillProcessTable(response.items, processSort);
  } catch (error) { console.error(error); }
}

async function loadEvents() {
  try {
    const {items} = await api.events();
    const list = byId("events-list");
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty"; empty.textContent = t("events.empty"); list.append(empty); return;
    }
    for (const item of items) {
      const event = document.createElement("article");
      event.className = `event ${item.severity}${item.resolved_ts ? " resolved" : ""}`;
      const title = document.createElement("strong"), detail = document.createElement("p"), time = document.createElement("small");
      // params=NULL 表示迁移前旧行，必须原样显示存量文本。
      title.textContent = item.params ? t(`events.${item.kind}.title`, item.params) : item.title;
      detail.textContent = item.params ? t(`events.${item.kind}.detail`, item.params) : item.detail;
      time.textContent = `${formatDateTime(item.ts)} · ${item.resolved_ts ? t("events.resolved") : t(`events.severity.${item.severity}`)}`;
      event.append(title, detail, time);
      if (item.ai_analysis) {
        const disclosure = document.createElement("details"), summary = document.createElement("summary"), analysis = document.createElement("p");
        summary.textContent = t("events.aiDiagnosis"); analysis.textContent = item.ai_analysis;
        disclosure.append(summary, analysis); event.append(disclosure);
      }
      list.append(event);
    }
  } catch (error) { console.error(error); }
}

function renderFiles(container, items, truncated = false) {
  container.replaceChildren();
  if (!items.length) { setNotice(container, "empty", t("search.noResults")); return; }
  if (truncated) setNotice(container, "empty", t("search.truncated"));
  for (const item of items) {
    const row = document.createElement("div"); row.className = "file-item";
    const meta = document.createElement("div"), name = document.createElement("strong"), path = document.createElement("small"), size = document.createElement("span");
    name.textContent = item.name; path.textContent = item.path; path.title = item.path;
    size.className = "file-size"; size.textContent = item.size == null ? "" : formatBytes(item.size);
    meta.append(name, path); row.append(meta, size); container.append(row);
  }
}

function setNotice(container, className, text) {
  const notice = document.createElement("p"); notice.className = className; notice.textContent = text;
  container.append(notice);
}

function bindControls() {
  document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === button));
    document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `${button.dataset.view}-view`));
    setTimeout(() => realtimeCharts.resize(), 0);
  }));
  byId("history-metric").addEventListener("change", loadHistory);
  byId("range-buttons").addEventListener("click", event => {
    if (!event.target.dataset.hours) return;
    historyHours = Number(event.target.dataset.hours);
    [...event.currentTarget.children].forEach(button => button.classList.toggle("active", button === event.target));
    loadHistory();
  });
  byId("process-sort").addEventListener("click", event => {
    if (!event.target.dataset.sort) return;
    processSort = event.target.dataset.sort;
    [...event.currentTarget.children].forEach(button => button.classList.toggle("active", button === event.target));
    loadProcesses();
  });
  byId("chat-toggle").addEventListener("click", () => {
    if (matchMedia("(max-width: 1180px)").matches) byId("chat-panel").classList.toggle("open");
    else document.body.classList.toggle("chat-collapsed");
    setTimeout(() => realtimeCharts.resize(), 260);
  });
  byId("health-explain").addEventListener("click", explainHealth);
  byId("search-form").addEventListener("submit", async event => {
    event.preventDefault(); const target = byId("search-results"); target.replaceChildren(); setNotice(target, "loading", t("search.searching"));
    try { renderFiles(target, (await api.search(byId("search-query").value, byId("search-kind").value)).items); }
    catch (error) { target.replaceChildren(); setNotice(target, "error", error.message); }
  });
  byId("large-form").addEventListener("submit", async event => {
    event.preventDefault(); const target = byId("large-results"); target.replaceChildren(); setNotice(target, "loading", t("search.scanning"));
    try { const result = await api.largeFiles(byId("large-path").value, byId("large-min").value); renderFiles(target, result.items, result.truncated); }
    catch (error) { target.replaceChildren(); setNotice(target, "error", error.message); }
  });
  window.addEventListener("languagechange", () => {
    healthExplanation = null;
    renderConnectionState();
    realtimeCharts.translate();
    if (latestMetric) updateCards(latestMetric, latestDisks);
    loadHistory(); loadProcesses(); loadEvents(); loadHealth();
  });
}

bindControls();
initChat();
initSettings();
initToolbox();
connectRealtime();
loadOverview();
loadHealth();
loadHistory();
loadProcesses();
loadEvents();
setInterval(loadOverview, 15000);
setInterval(loadHealth, 15000);
setInterval(loadProcesses, 5000);
setInterval(loadEvents, 30000);
