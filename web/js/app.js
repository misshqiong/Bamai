import {api} from "./api.js";
import {initChat} from "./chat.js";
import {HistoryChart, RealtimeCharts, formatBytes, formatRate} from "./charts.js";

const realtimeCharts = new RealtimeCharts();
const historyChart = new HistoryChart();
let historyHours = 1;
let processSort = "cpu";

const byId = id => document.getElementById(id);
const setText = (id, text) => { byId(id).textContent = text; };

function updateCards(metric, disks = null) {
  if (!metric) return;
  setText("cpu-value", `${metric.cpu_percent.toFixed(1)}%`);
  setText("load-value", `1 分钟负载 ${metric.load_1.toFixed(2)}`);
  setText("memory-value", `${metric.mem_percent.toFixed(1)}%`);
  setText("memory-detail", `${formatBytes(metric.mem_used)} / ${formatBytes(metric.mem_total)}`);
  setText("network-value", formatRate(metric.net_up_bps + metric.net_down_bps));
  setText("network-detail", `↑ ${formatRate(metric.net_up_bps)} · ↓ ${formatRate(metric.net_down_bps)}`);
  if (disks?.length) {
    const disk = disks.find(item => item.mount === "/System/Volumes/Data") || disks[0];
    setText("disk-value", `${disk.percent.toFixed(1)}%`);
    setText("disk-detail", `${disk.mount} · ${formatBytes(disk.used)} / ${formatBytes(disk.total)}`);
  }
}

function connectRealtime() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/realtime`);
  socket.addEventListener("open", () => { byId("ws-dot").className = "status-dot online"; setText("ws-status", "实时监控中"); });
  socket.addEventListener("message", event => { const metric = JSON.parse(event.data); updateCards(metric); realtimeCharts.push(metric); });
  socket.addEventListener("close", () => { byId("ws-dot").className = "status-dot offline"; setText("ws-status", "连接中断，正在重连"); setTimeout(connectRealtime, 2000); });
}

async function loadOverview() {
  try {
    const overview = await api.overview();
    updateCards(overview.metric, overview.disks);
    if (overview.metric) realtimeCharts.push(overview.metric);
    setText("event-count", overview.unresolved_events);
  } catch (error) { console.error(error); }
}

async function loadHistory() {
  const metric = byId("history-metric").value;
  const end = Math.floor(Date.now() / 1000), start = end - historyHours * 3600;
  try { historyChart.set(metric, (await api.metrics(metric, start, end)).points); }
  catch (error) { console.error(error); }
}

function fillProcessTable(items, sort) {
  const body = byId("process-body"); body.replaceChildren();
  setText("process-value-head", sort === "cpu" ? "CPU" : sort === "memory" ? "内存" : "吞吐");
  if (!items.length) { const row = body.insertRow(); const cell = row.insertCell(); cell.colSpan = 3; cell.className = "empty"; cell.textContent = "暂无进程数据"; return; }
  for (const item of items) {
    const row = body.insertRow();
    const name = row.insertCell(); name.className = "process-name"; name.textContent = item.name; name.title = item.cmdline || item.name;
    row.insertCell().textContent = item.pid;
    const value = row.insertCell(); value.textContent = sort === "cpu" ? `${item.cpu_percent.toFixed(1)}%` : sort === "memory" ? formatBytes(item.memory_rss) : formatRate(item.up_bps + item.down_bps);
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
    const {items} = await api.events(); const list = byId("events-list"); list.replaceChildren();
    if (!items.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "暂无异常事件"; list.append(empty); return; }
    for (const item of items) {
      const event = document.createElement("article"); event.className = `event ${item.severity}${item.resolved_ts ? " resolved" : ""}`;
      const title = document.createElement("strong"); title.textContent = item.title;
      const detail = document.createElement("p"); detail.textContent = item.detail;
      const time = document.createElement("small"); time.textContent = `${new Date(item.ts * 1000).toLocaleString()} · ${item.resolved_ts ? "已恢复" : item.severity}`;
      event.append(title, detail, time);
      if (item.ai_analysis) { const disclosure = document.createElement("details"); disclosure.innerHTML = "<summary>AI 诊断</summary>"; const analysis = document.createElement("p"); analysis.textContent = item.ai_analysis; disclosure.append(analysis); event.append(disclosure); }
      list.append(event);
    }
  } catch (error) { console.error(error); }
}

function renderFiles(container, items, truncated = false) {
  container.replaceChildren();
  if (!items.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "没有找到结果"; container.append(empty); return; }
  if (truncated) { const note = document.createElement("p"); note.className = "empty"; note.textContent = "扫描达到 30 秒上限，以下为已扫描结果"; container.append(note); }
  for (const item of items) {
    const row = document.createElement("div"); row.className = "file-item";
    const meta = document.createElement("div"), name = document.createElement("strong"), path = document.createElement("small"), size = document.createElement("span");
    name.textContent = item.name; path.textContent = item.path; path.title = item.path; size.className = "file-size"; size.textContent = item.size == null ? "" : formatBytes(item.size);
    meta.append(name, path); row.append(meta, size); container.append(row);
  }
}

function bindControls() {
  document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === button));
    document.querySelectorAll(".view").forEach(view => view.classList.toggle("active", view.id === `${button.dataset.view}-view`));
    setTimeout(() => realtimeCharts.resize(), 0);
  }));
  byId("history-metric").addEventListener("change", loadHistory);
  byId("range-buttons").addEventListener("click", event => { if (!event.target.dataset.hours) return; historyHours = Number(event.target.dataset.hours); [...event.currentTarget.children].forEach(button => button.classList.toggle("active", button === event.target)); loadHistory(); });
  byId("process-sort").addEventListener("click", event => { if (!event.target.dataset.sort) return; processSort = event.target.dataset.sort; [...event.currentTarget.children].forEach(button => button.classList.toggle("active", button === event.target)); loadProcesses(); });
  byId("chat-toggle").addEventListener("click", () => { if (matchMedia("(max-width: 1180px)").matches) byId("chat-panel").classList.toggle("open"); else document.body.classList.toggle("chat-collapsed"); setTimeout(() => realtimeCharts.resize(), 260); });
  byId("search-form").addEventListener("submit", async event => { event.preventDefault(); const target = byId("search-results"); target.innerHTML = '<p class="loading">正在搜索…</p>'; try { renderFiles(target, (await api.search(byId("search-query").value, byId("search-kind").value)).items); } catch (error) { target.innerHTML = `<p class="error"></p>`; target.firstChild.textContent = error.message; } });
  byId("large-form").addEventListener("submit", async event => { event.preventDefault(); const target = byId("large-results"); target.innerHTML = '<p class="loading">正在扫描，最多等待 30 秒…</p>'; try { const result = await api.largeFiles(byId("large-path").value, byId("large-min").value); renderFiles(target, result.items, result.truncated); } catch (error) { target.innerHTML = `<p class="error"></p>`; target.firstChild.textContent = error.message; } });
}

bindControls(); initChat(); connectRealtime(); loadOverview(); loadHistory(); loadProcesses(); loadEvents();
setInterval(loadOverview, 15000); setInterval(loadProcesses, 5000); setInterval(loadEvents, 30000);
