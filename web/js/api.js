export class ApiError extends Error {
  constructor(message, status) { super(message); this.name = "ApiError"; this.status = status; }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {Accept: "application/json", ...(options.headers || {})},
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
  events: (limit = 50) => request(`/api/events?limit=${limit}`),
  ollamaStatus: () => request("/api/ollama/status"),
  chat: messages => request("/api/chat", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({messages}),
  }),
  search: (q, kind) => request(`/api/search/files?${new URLSearchParams({q, kind})}`),
  largeFiles: (path, minMb) => request(`/api/search/large-files?${new URLSearchParams({path, min_mb: minMb, limit: 50})}`),
};
