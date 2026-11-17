import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api, formatTime, label, rupees,
  type Attention, type CallDetail, type CallSummary, type Customer, type Numbers,
} from './api'

type Notify = (msg: string) => void

export function Chip({ kind, value }: { kind: 'status' | 'reason' | 'outcome'; value: string | null }) {
  if (!value) return <span className="muted">—</span>
  return <span className={`chip ${kind}-${value}`}>{label(value)}</span>
}

/** Fetch + optional polling with graceful error state. */
function usePolled<T>(fn: () => Promise<T>, everyMs?: number) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn
  const reload = useCallback(async () => {
    try {
      setData(await fnRef.current())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])
  useEffect(() => {
    reload()
    if (!everyMs) return
    const t = setInterval(reload, everyMs)
    return () => clearInterval(t)
  }, [reload, everyMs])
  return { data, error, reload }
}

function State({ data, error, empty }: { data: unknown[] | null; error: string | null; empty: string }) {
  if (error && !data) return <div className="state error">{error}</div>
  if (!data) return <div className="state">Loading…</div>
  if (data.length === 0) return <div className="state">{empty}</div>
  return null
}

export function Customers({ notify, onChanged }: { notify: Notify; onChanged: () => void }) {
  const customers = usePolled(api.customers, 5000)
  const numbers = usePolled<Numbers>(api.numbers)
  const [to, setTo] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const target = to || numbers.data?.default || ''

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key)
    try { await fn() } catch (e) { notify((e as Error).message) } finally { setBusy(null) }
  }

  const call = (c: Customer) => run(`call-${c.id}`, async () => {
    await api.startCall(c.id, target)
    notify(`Calling ${c.name} on ${target}…`)
    customers.reload(); onChanged()
  })
  const browser = (c: Customer) => run(`web-${c.id}`, async () => {
    const { url } = await api.browserSession(c.id)
    window.open(url, '_blank', 'noopener')
  })
  const reset = () => run('reset', async () => {
    await api.reset()
    notify('Demo data reset')
    customers.reload(); onChanged()
  })

  return (
    <section>
      <div className="toolbar">
        <label>
          Call to&nbsp;
          <select value={target} onChange={(e) => setTo(e.target.value)} disabled={!numbers.data}>
            {(numbers.data?.numbers ?? []).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        {numbers.error && <span className="muted small">Numbers unavailable</span>}
        <span className="spacer" />
        <button className="ghost" onClick={reset} disabled={busy === 'reset'}>Reset demo data</button>
      </div>
      <State data={customers.data} error={customers.error} empty="No customers." />
      {customers.data && customers.data.length > 0 && (
        <table>
          <thead>
            <tr><th>Customer</th><th>Plan</th><th>Amount</th><th>Failure reason</th><th>Status</th><th>Flags</th><th /></tr>
          </thead>
          <tbody>
            {customers.data.map((c) => (
              <tr key={c.id}>
                <td>
                  <div className="strong">{c.name}</div>
                  <div className="muted small">{c.persona} · expects {label(c.expected_outcome)}</div>
                </td>
                <td>{c.plan}</td>
                <td className="num">{rupees(c.amount_due)}<div className="muted small">due {c.due_date}</div></td>
                <td>{label(c.failure_reason)}</td>
                <td><Chip kind="status" value={c.status} /></td>
                <td>{c.open_attention_count > 0 && <span className="badge">{c.open_attention_count}</span>}</td>
                <td className="actions">
                  <button disabled={c.debits_blocked || !target || busy === `call-${c.id}`}
                    title={c.debits_blocked ? 'Debits blocked (fraud flag)' : undefined}
                    onClick={() => call(c)}>Call</button>
                  <button className="ghost" disabled={busy === `web-${c.id}`} onClick={() => browser(c)}>Talk in browser</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export function Calls({ notify, openId, setOpenId }: {
  notify: Notify; openId: CallSummary['id'] | null; setOpenId: (id: CallSummary['id'] | null) => void
}) {
  const calls = usePolled(api.calls, 3000)
  return (
    <section className={openId !== null ? 'split' : undefined}>
      <div>
        <State data={calls.data} error={calls.error} empty="No calls yet." />
        {calls.data && calls.data.length > 0 && (
          <table className="clickable">
            <thead><tr><th>When</th><th>Customer</th><th>Channel</th><th>Status</th><th>Outcome</th></tr></thead>
            <tbody>
              {calls.data.map((c) => (
                <tr key={c.id} className={c.id === openId ? 'selected' : undefined} onClick={() => setOpenId(c.id)}>
                  <td>{formatTime(c.started_at)}</td>
                  <td className="strong">{c.customer_name}</td>
                  <td>{label(c.channel)}</td>
                  <td>{label(c.status)}</td>
                  <td><Chip kind="outcome" value={c.outcome} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {openId !== null && <CallDetailPanel id={openId} notify={notify} onClose={() => setOpenId(null)} />}
    </section>
  )
}

export function CallDetailPanel({ id, onClose }: { id: CallSummary['id']; notify: Notify; onClose: () => void }) {
  const fn = useCallback(() => api.call(id), [id])
  const { data: c, error } = usePolled<CallDetail>(fn, 3000)
  return (
    <aside className="panel">
      <div className="panel-head">
        <h3>{c ? c.customer_name : `Call ${id}`}</h3>
        <button className="ghost" onClick={onClose}>Close</button>
      </div>
      {error && !c && <div className="state error">{error}</div>}
      {!c && !error && <div className="state">Loading…</div>}
      {c && (
        <>
          <dl>
            <dt>Outcome</dt><dd><Chip kind="outcome" value={c.outcome} /></dd>
            <dt>Status</dt><dd>{label(c.status)} · {label(c.channel)}</dd>
            <dt>Identity</dt><dd>{label(c.identity_result)}</dd>
            <dt>Promise</dt>
            <dd>{c.promise_amount != null ? `${rupees(c.promise_amount)} on ${c.promise_date ?? '—'}` : '—'}</dd>
            <dt>Started</dt><dd>{formatTime(c.started_at)}</dd>
          </dl>
          {c.summary && <p className="summary">{c.summary}</p>}
          <h4>Transcript</h4>
          <div className="chat">
            {c.transcript.length === 0 && <div className="muted small">No transcript yet.</div>}
            {c.transcript.map((t, i) => (
              <div key={i} className={`bubble ${t.role}`}>{t.text}</div>
            ))}
          </div>
          <h4>Payment attempts</h4>
          {c.payment_attempts.length === 0 ? <div className="muted small">None</div> : (
            <ul className="plain">
              {c.payment_attempts.map((p, i) => (
                <li key={i}><code>{JSON.stringify(p)}</code></li>
              ))}
            </ul>
          )}
          <h4>Flags</h4>
          {c.attention_items.length === 0 ? <div className="muted small">None</div> : (
            <ul className="plain">
              {c.attention_items.map((a) => (
                <li key={a.id}><Chip kind="reason" value={a.reason} /> <span className="small">{a.notes}</span></li>
              ))}
            </ul>
          )}
        </>
      )}
    </aside>
  )
}

export function AttentionList({ notify, onChanged, onOpenCall }: {
  notify: Notify; onChanged: () => void; onOpenCall: (id: Attention['call_id']) => void
}) {
  const items = usePolled(api.attention, 5000)
  const resolve = async (a: Attention) => {
    try {
      await api.resolve(a.id)
      items.reload(); onChanged()
    } catch (e) { notify((e as Error).message) }
  }
  return (
    <section>
      <State data={items.data} error={items.error} empty="Nothing needs attention." />
      {items.data && items.data.length > 0 && (
        <ul className="cards">
          {items.data.map((a) => (
            <li key={a.id} className="card">
              <div className="card-top">
                <Chip kind="reason" value={a.reason} />
                <span className="strong">{a.customer_name}</span>
                <span className="muted small">{formatTime(a.created_at)}</span>
                <span className="spacer" />
                {a.call_id != null && (
                  <button className="link" onClick={() => onOpenCall(a.call_id)}>View call</button>
                )}
                <button onClick={() => resolve(a)}>Mark resolved</button>
              </div>
              <p>{a.notes}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
