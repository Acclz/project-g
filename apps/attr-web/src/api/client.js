/**
 * 接口客户端：只做 HTTP/SSE 的搬运与错误归一，不含任何业务计算。
 *
 * 约定（与 docs/01-技术规格.md §4 一致）：
 * * 所有数值都来自后端（数仓 + packages/attribution），前端只负责展示，不做二次计算；
 * * SSE 的事件 kind 与 session_steps.kind 一一对应，直接映射到步骤流节点。
 */

const BASE = import.meta.env.VITE_API_BASE || '/api'

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail))
    this.status = status
    this.detail = detail
  }
}

function withQuery(path, params) {
  if (!params) return path
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.append(key, value)
  })
  const text = search.toString()
  return text ? `${path}?${text}` : path
}

async function request(path, { method = 'GET', body, params } = {}) {
  const response = await fetch(withQuery(`${BASE}${path}`, params), {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await response.text()
  const payload = text ? JSON.parse(text) : null
  if (!response.ok) throw new ApiError(response.status, payload?.detail ?? text)
  return payload
}

export const api = {
  // 指标字典
  metrics: (domain) => request('/metrics', { params: { domain } }),
  metricTree: (code, scenario) => request(`/metrics/${code}/tree`, { params: { scenario } }),
  validateMetric: (code, scenario) =>
    request(`/metrics/${code}/validate`, { method: 'POST', body: { scenario } }),

  // 大盘与异动
  anomalies: (domain, period) => request('/dashboard/anomalies', { params: { domain, period } }),
  series: (domain, metric, days = 60) =>
    request('/dashboard/series', { params: { domain, metric, days } }),

  // 事件日历
  events: (params) => request('/events', { params }),
  createEvent: (body) => request('/events', { method: 'POST', body }),
  updateEvent: (id, body) => request(`/events/${id}`, { method: 'PUT', body }),
  deleteEvent: (id) => request(`/events/${id}`, { method: 'DELETE' }),
  matchEvents: (params) => request('/events/match', { params }),

  // 会话（L1 / L2）
  createSession: (body) => request('/sessions', { method: 'POST', body }),
  listSessions: (limit = 50) => request('/sessions', { params: { limit } }),
  getSession: (id) => request(`/sessions/${id}`),
  sendMessage: (id, message, actor = 'analyst') =>
    request(`/sessions/${id}/messages`, { method: 'POST', body: { message, actor } }),
  control: (id, action, actor = 'analyst') =>
    request(`/sessions/${id}/${action}`, { method: 'POST', body: { actor } }),
  drilldown: (id, body) => request(`/sessions/${id}/drilldown`, { method: 'POST', body }),
  hypotheses: (id) => request(`/sessions/${id}/hypotheses`),
  evidence: (id) => request(`/sessions/${id}/evidence`),
  drilldowns: (id) => request(`/sessions/${id}/drilldowns`),

  // What-If（L3）
  factors: (id) => request(`/sessions/${id}/factors`),
  whatif: (id, body) => request(`/sessions/${id}/whatif`, { method: 'POST', body }),
  whatifs: (id) => request(`/sessions/${id}/whatifs`),

  // 报告与导出（L3）
  createReport: (sessionId, actor = 'analyst') =>
    request(`/sessions/${sessionId}/reports`, { method: 'POST', body: { actor } }),
  reports: (params) => request('/reports', { params }),
  report: (id) => request(`/reports/${id}`),
  annotate: (id, body) => request(`/reports/${id}/annotations`, { method: 'POST', body }),
  exportReport: (id, format) =>
    request(`/reports/${id}/export`, { method: 'POST', body: { format } }),
  exportStatus: (jobId) => request(`/exports/${jobId}`),
  downloadUrl: (jobId) => `${BASE}/exports/${jobId}/download`,
}

/**
 * 打开 SSE 步骤流：事件 kind = plan / query / drilldown / hypothesis / verify / event / whatif / report。
 * 返回一个关闭函数（组件卸载时调用，避免泄漏连接）。
 */
export function openStepStream(sessionId, { since = -1, onEvent, onError } = {}) {
  const source = new EventSource(`${BASE}/sessions/${sessionId}/stream?since=${since}`)
  source.onmessage = (message) => {
    try {
      const payload = JSON.parse(message.data)
      onEvent?.(payload)
      if (payload.status === 'stream_closed') source.close()
    } catch (error) {
      onError?.(error)
    }
  }
  source.onerror = (error) => onError?.(error)
  return () => source.close()
}
