import {getLanguage} from "./i18n.js";

export class ApiError extends Error {
  constructor(message, status) { super(message); this.name = "ApiError"; this.status = status; }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {Accept: "application/json", "Accept-Language": getLanguage(), ...(options.headers || {})},
  });
  let body;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) throw new ApiError(body?.detail || `请求失败 (${response.status})`, response.status);
  return body;
}

export const api = {
  overview: () => request("/api/overview"),
  metrics: (metric, start, end) => request(`/api/metrics?${new URLSearchParams({metric, start, end})}`),
  processes: (sort, limit = 20) => request(`/api/processes?${new URLSearchParams({sort, limit})}`),
  processNet: () => request("/api/processes/net"),
  apps: (sort, limit = 30) => request(`/api/apps?${new URLSearchParams({sort, limit})}`),
  appDetail: (app, window = 3600) => request(`/api/apps/${encodeURIComponent(app)}/detail?${new URLSearchParams({window})}`),
  events: (limit = 50) => request(`/api/events?limit=${limit}`),
  health: () => request("/api/health"),
  explainHealth: () => request("/api/health/explain", {method: "POST"}),
  ollamaStatus: () => request("/api/ollama/status"),
  settings: () => request("/api/settings"),
  saveSettings: settings => request("/api/settings", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(settings),
  }),
  ollamaModels: () => request("/api/ollama/models"),
  pullModel: model => request("/api/ollama/pull", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({model}),
  }),
  pullStatus: () => request("/api/ollama/pull/status"),
  deleteModel: model => request("/api/ollama/delete", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({model}),
  }),
  toolbox: () => request("/api/toolbox"),
  runProbe: (probeId, params) => request(`/api/toolbox/${encodeURIComponent(probeId)}/run`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({params}),
  }),
  probeJob: jobId => request(`/api/toolbox/jobs/${encodeURIComponent(jobId)}`),
  explainProbe: probeId => request(`/api/toolbox/${encodeURIComponent(probeId)}/explain`, {method: "POST"}),
  chat: messages => request("/api/chat", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({messages}),
  }),
  search: (q, kind) => request(`/api/search/files?${new URLSearchParams({q, kind})}`),
  largeFiles: (path, minMb) => request(`/api/search/large-files?${new URLSearchParams({path, min_mb: minMb, limit: 50})}`),
};
