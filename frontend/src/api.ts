export type CustomerStatus =
  | 'due' | 'paid' | 'scheduled' | 'partial_scheduled' | 'escalated' | 'callback' | 'blocked'

export type CallOutcome =
  | 'paid' | 'scheduled' | 'partial_paid_and_scheduled' | 'escalated' | 'callback'
  | 'voicemail' | 'no_answer' | 'incomplete'

export interface Customer {
  id: number | string
  name: string
  plan: string
  amount_due: number
  due_date: string
  failure_reason: string
  scenario: string
  scenario_tests: string
  persona: string
  expected_outcome: CallOutcome
  expected_flag: string | null
  balance: number
  instrument_status: InstrumentStatus
  default_balance: number | null
  default_instrument_status: InstrumentStatus | null
  account_edited: boolean
  last_call_flags: string[]
  result: 'pass' | 'fail' | null
  status: CustomerStatus
  debits_blocked: boolean
  open_attention_count: number
  open_attention_reasons: string[]
  last_outcome: CallOutcome | null
}

export type InstrumentStatus = 'active' | 'expired' | 'lost'
export const INSTRUMENT_STATUSES: InstrumentStatus[] = ['active', 'expired', 'lost']

export interface AccountUpdate { balance?: number; top_up?: number; instrument_status?: InstrumentStatus }

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

/** Tolerate an older backend that doesn't send the scenario / bank fields yet. */
function withDefaults(c: Customer): Customer {
  return {
    ...c,
    scenario: c.scenario ?? c.name,
    scenario_tests: c.scenario_tests ?? '',
    persona: c.persona ?? '',
    expected_flag: c.expected_flag ?? null,
    balance: c.balance ?? 0,
    instrument_status: c.instrument_status ?? 'active',
    default_balance: c.default_balance ?? null,
    default_instrument_status: c.default_instrument_status ?? null,
    account_edited: c.account_edited ?? false,
    last_call_flags: c.last_call_flags ?? [],
    open_attention_reasons: c.open_attention_reasons ?? [],
    result: c.result ?? null,
  }
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  customers: () => request<Customer[]>('/api/customers').then((cs) => cs.map(withDefaults)),
  numbers: () => request<Numbers>('/api/numbers'),
  startCall: (customer_id: Customer['id'], to_number: string, override_do_not_call = false) =>
    post<{ call_id: number | string }>('/api/calls', { customer_id, to_number, override_do_not_call }),
  browserSession: (customer_id: Customer['id'], override_do_not_call = false) =>
    post<{ url: string }>('/api/browser-session', { customer_id, override_do_not_call }),
  account: (customer_id: Customer['id'], body: AccountUpdate) =>
    post<{ balance: number; instrument_status: InstrumentStatus }>(`/api/customers/${customer_id}/account`, body),
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
