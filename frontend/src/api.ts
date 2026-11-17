export type CustomerStatus =
  | 'due' | 'paid' | 'scheduled' | 'partial_scheduled' | 'escalated' | 'blocked'

export type CallOutcome =
  | 'paid' | 'scheduled' | 'partial_paid_and_scheduled' | 'escalated'
  | 'voicemail' | 'no_answer' | 'incomplete'

export interface Customer {
  id: number | string
  name: string
  plan: string
  amount_due: number
  due_date: string
  failure_reason: string
  persona: string
  expected_outcome: string
  status: CustomerStatus
  debits_blocked: boolean
  open_attention_count: number
  open_attention_reasons: string[]
  last_outcome: CallOutcome | null
}

export interface Numbers { default: string; numbers: string[] }

export interface CallSummary {
  id: number | string
  customer_id: number | string
  customer_name: string
  to_number: string
  channel: string
  status: string
  outcome: CallOutcome | null
  summary: string | null
  started_at: string | null
  ended_at: string | null
  avg_latency_ms: number | null
}

export interface ServiceTtfb { avg_ttfb_ms: number; count: number }
export interface Latency {
  turns_ms: number[]
  response_ms: { avg: number; p50: number; p95: number; max: number; count: number }
  services: { stt: ServiceTtfb; llm: ServiceTtfb; tts: ServiceTtfb }
}

export interface TranscriptLine { role: 'agent' | 'customer'; text: string }

export interface Attention {
  id: number | string
  customer_id: number | string
  customer_name: string
  call_id: number | string | null
  reason: string
  notes: string
  created_at: string
  resolved: boolean
}

export interface CallDetail extends CallSummary {
  identity_result: string | null
  promise_amount: number | null
  promise_date: string | null
  transcript: TranscriptLine[]
  latency: Latency | null
  // Shape not fixed by the contract; rendered defensively.
  payment_attempts: Record<string, unknown>[]
  attention_items: Attention[]
}

export const DNC_CODE = 'do_not_call_request_open'

export class ApiError extends Error {
  code?: string
  constructor(message: string, code?: string) { super(message); this.code = code }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    })
  } catch {
    throw new Error('Cannot reach the server. Is the backend running on :8000?')
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`
    let code: string | undefined
    try {
      const body = await res.json()
      const d = body?.detail
      if (typeof d === 'string') msg = d
      else if (typeof d?.message === 'string') msg = d.message
      code = typeof d?.code === 'string' ? d.code : undefined
    } catch { /* non-JSON error body */ }
    throw new ApiError(msg, code)
  }
  return res.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  customers: () => request<Customer[]>('/api/customers'),
  numbers: () => request<Numbers>('/api/numbers'),
  startCall: (customer_id: Customer['id'], to_number: string, override_do_not_call = false) =>
    post<{ call_id: number | string }>('/api/calls', { customer_id, to_number, override_do_not_call }),
  browserSession: (customer_id: Customer['id'], override_do_not_call = false) =>
    post<{ url: string }>('/api/browser-session', { customer_id, override_do_not_call }),
  calls: () => request<CallSummary[]>('/api/calls'),
  call: (id: CallSummary['id']) => request<CallDetail>(`/api/calls/${id}`),
  attention: () => request<Attention[]>('/api/attention?resolved=false'),
  resolve: (id: Attention['id']) => post<{ ok: boolean }>(`/api/attention/${id}/resolve`),
  reset: () => post<{ ok: boolean }>('/api/demo/reset'),
}

export const rupees = (n: number) => '₹' + n.toLocaleString('en-IN')

export const label = (s: string | null | undefined) =>
  s ? s.replace(/_/g, ' ') : '—'

export function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime())
    ? iso
    : d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
}
