import { Component, StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

/** Show a render crash instead of a blank page. */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="app">
        <div className="state error" style={{ marginTop: 40 }}>
          <p className="strong">The dashboard hit an error.</p>
          <p><code>{this.state.error.message}</code></p>
          <p className="small">If the backend was just updated, restart it, then reload this page.</p>
          <button onClick={() => window.location.reload()}>Reload</button>
        </div>
      </div>
    )
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
