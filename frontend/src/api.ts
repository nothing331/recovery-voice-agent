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
  // Shape not fixed by the contract; rendered defensively.
  payment_attempts: Record<string, unknown>[]
  attention_items: Attention[]
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
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') msg = body.detail
    } catch { /* non-JSON error body */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  customers: () => request<Customer[]>('/api/customers'),
  numbers: () => request<Numbers>('/api/numbers'),
  startCall: (customer_id: Customer['id'], to_number: string) =>
    post<{ call_id: number | string }>('/api/calls', { customer_id, to_number }),
  browserSession: (customer_id: Customer['id']) =>
    post<{ url: string }>('/api/browser-session', { customer_id }),
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
