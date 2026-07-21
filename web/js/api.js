async function request(path) {
  const response = await fetch(path, {headers: {Accept: "application/json"}});
  let body;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) throw new Error(body?.detail || `请求失败 (${response.status})`);
  return body;
}

export const api = {
  overview: () => request("/api/overview"),
  metrics: (metric, start, end) => request(`/api/metrics?${new URLSearchParams({metric, start, end})}`),
  processes: (sort, limit = 20) => request(`/api/processes?${new URLSearchParams({sort, limit})}`),
  processNet: () => request("/api/processes/net"),
  events: (limit = 50) => request(`/api/events?limit=${limit}`),
  search: (q, kind) => request(`/api/search/files?${new URLSearchParams({q, kind})}`),
  largeFiles: (path, minMb) => request(`/api/search/large-files?${new URLSearchParams({path, min_mb: minMb, limit: 50})}`),
};

