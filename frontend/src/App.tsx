import { useCallback, useRef, useState } from 'react'
import { AttentionList, Calls, Customers } from './components'
import type { CallSummary } from './api'

type Tab = 'customers' | 'calls' | 'attention'
const TABS: { id: Tab; label: string }[] = [
  { id: 'customers', label: 'Customers' },
  { id: 'calls', label: 'Calls' },
  { id: 'attention', label: 'Needs attention' },
]

export default function App() {
  const [tab, setTab] = useState<Tab>('customers')
  const [toast, setToast] = useState<string | null>(null)
  const [openCall, setOpenCall] = useState<CallSummary['id'] | null>(null)
  const [, setVersion] = useState(0)
  const timer = useRef<number | undefined>(undefined)

  const notify = useCallback((msg: string) => {
    setToast(msg)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setToast(null), 5000)
  }, [])
  const changed = useCallback(() => setVersion((v) => v + 1), [])

  return (
    <div className="app">
      <header>
        <h1>Autopay Recovery <span className="muted">Voice Agent</span></h1>
        <nav>
          {TABS.map((t) => (
            <button key={t.id} className={`tab${tab === t.id ? ' active' : ''}`} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === 'customers' && <Customers notify={notify} onChanged={changed} />}
        {tab === 'calls' && <Calls notify={notify} openId={openCall} setOpenId={setOpenCall} />}
        {tab === 'attention' && (
          <AttentionList notify={notify} onChanged={changed}
            onOpenCall={(id) => { if (id != null) { setOpenCall(id); setTab('calls') } }} />
        )}
      </main>
      {toast && (
        <div className="toast" role="alert" onClick={() => setToast(null)}>{toast}</div>
      )}
    </div>
  )
}
