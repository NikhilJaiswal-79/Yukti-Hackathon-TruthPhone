import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { BrowserLiveView } from 'bedrock-agentcore/browser/live-view'
import { api } from './api'
import type { Phase, SessionState } from './types'

const SESSION_KEY = 'truthphone.session.v2'
const REMOTE_WIDTH = 1440
const REMOTE_HEIGHT = 900

const PHASE_LABEL: Record<Phase, string> = {
  clarification: 'Getting requirements',
  search: 'Searching live retailers',
  comparison: 'Comparing phones',
  selection: 'Pick a phone',
  verification: 'Fact-checking',
  done: 'Report ready',
  error: 'Needs attention',
}

const WELCOME = `Hi, I'm **TruthPhone**. Tell me your budget, brand, and what you need — or just describe the phone you're after.

When you're ready, switch on **Live shopping search** and send your message. I'll open a real live Chrome session, read retailer listings, and fact-check the claims you pick.`

const markdownComponents = {
  a: ({ node: _node, ...props }: any) => (
    <a {...props} target="_blank" rel="noreferrer" />
  ),
}

function errorText(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

function liveCopy(session: SessionState): { title: string; body: string } {
  if (session.browserStatus === 'starting') {
    return {
      title: 'Starting the browser',
      body: 'AgentCore Browser is booting a fresh Chrome session.',
    }
  }
  if (session.browserStatus === 'live') {
    return {
      title: 'Connecting the live view',
      body: 'The browser is running; the signed view is being generated.',
    }
  }
  if (session.browserStatus === 'closed') {
    return {
      title: 'Browser session ended',
      body: 'The next search or fact-check starts a new live Chrome session.',
    }
  }
  if (session.browserStatus === 'error' || session.phase === 'error') {
    return {
      title: 'Browser unavailable',
      body: 'Check the message in chat, then try again.',
    }
  }
  return {
    title: 'The live browser appears here',
    body: 'Describe a phone, switch on Live shopping search, and watch the agent browse.',
  }
}

export default function App() {
  const [session, setSession] = useState<SessionState | null>(null)
  const [input, setInput] = useState('')
  const [forceSearch, setForceSearch] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'chat' | 'live'>('chat')
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    async function boot() {
      const saved = localStorage.getItem(SESSION_KEY)
      if (saved) {
        try {
          const existing = await api.getSession(saved)
          if (!cancelled) setSession(existing)
          return
        } catch {
          localStorage.removeItem(SESSION_KEY)
        }
      }
      const created = await api.createSession()
      if (!cancelled) {
        localStorage.setItem(SESSION_KEY, created.sessionId)
        setSession(created)
      }
    }
    boot()
      .catch((err) => {
        if (!cancelled) setError(errorText(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!session) return
    const id = session.sessionId
    let stopped = false
    let timer = 0

    const tick = async () => {
      try {
        const next = await api.getSession(id)
        if (!stopped) setSession(next)
      } catch (err) {
        if (!stopped && errorText(err).includes('Session not found')) {
          stopped = true
          window.clearInterval(timer)
          const message = 'This session expired. Click New chat to start again.'
          setError(message)
          setSession((current) =>
            current
              ? {
                  ...current,
                  jobStatus: 'error',
                  phase: 'error',
                  browserStatus: 'error',
                  error: message,
                }
              : current
          )
        }
      }
    }

    timer = window.setInterval(tick, 1500)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [session?.sessionId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [
    session?.messages.length,
    session?.jobStatus,
    session?.verificationResults.length,
  ])

  const running = session?.jobStatus === 'running'
  const disabled = Boolean(busy || running)
  const hasResults =
    Boolean(session?.searchResults.length) &&
    ['selection', 'comparison', 'verification', 'done'].includes(
      session?.phase || ''
    )
  const live = Boolean(
    session?.liveViewUrl && session.browserStatus === 'live'
  )

  async function send() {
    const text = input.trim()
    if (!text || !session || disabled) return

    setBusy(true)
    setError('')
    try {
      if (
        /^\d+$/.test(text) &&
        session.searchResults.length > 0 &&
        ['selection', 'comparison'].includes(session.phase)
      ) {
        const index = Number(text) - 1
        if (index >= 0 && index < session.searchResults.length) {
          const response = await api.select(session.sessionId, index)
          setSession(response.session)
          setInput('')
          return
        }
      }

      const response = await api.sendMessage(
        session.sessionId,
        text,
        forceSearch
      )
      setSession(response.session)
      setInput('')
      setForceSearch(false)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  async function select(index: number) {
    if (!session || disabled) return
    setBusy(true)
    setError('')
    try {
      const response = await api.select(session.sessionId, index)
      setSession(response.session)
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    if (!session || disabled) return
    setBusy(true)
    setError('')
    try {
      await api.deleteSession(session.sessionId).catch(() => undefined)
      const created = await api.createSession()
      localStorage.setItem(SESSION_KEY, created.sessionId)
      setSession(created)
      setInput('')
      setForceSearch(false)
      setTab('chat')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return (
      <div className="boot">
        <div className="spinner" />
        <p>Starting TruthPhone…</p>
      </div>
    )
  }

  if (!session) {
    return (
      <div className="boot">
        <p className="boot-error">{error || 'Could not create a session.'}</p>
        <button className="primary-btn" onClick={() => window.location.reload()}>
          Retry
        </button>
      </div>
    )
  }

  const status = running
    ? 'Agent browsing'
    : session.error
      ? 'Needs attention'
      : PHASE_LABEL[session.phase]
  const tone = running
    ? 'busy'
    : session.error || session.phase === 'error'
      ? 'error'
      : session.phase === 'done'
        ? 'done'
        : 'idle'
  const placeholder = liveCopy(session)
  const typingLabel =
    session.phase === 'search'
      ? 'Browsing live retailer sites'
      : session.phase === 'verification'
        ? 'Checking claims against independent sources'
        : 'Thinking'

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">TP</div>
          <div>
            <h1>TruthPhone</h1>
            <p>Live retail buying + claim verification</p>
          </div>
        </div>
        <div className="topbar-right">
          <div className={`status-pill ${tone}`}>
            <span className="status-dot" />
            {status}
          </div>
          <button className="ghost-btn" onClick={reset} disabled={disabled}>
            New chat
          </button>
        </div>
      </header>

      <div className="mobile-tabs">
        <button
          className={tab === 'chat' ? 'active' : ''}
          onClick={() => setTab('chat')}
        >
          Chat
        </button>
        <button
          className={tab === 'live' ? 'active' : ''}
          onClick={() => setTab('live')}
        >
          Live browser
          {live && <span className="tab-dot" />}
        </button>
      </div>

      <main className="workspace">
        <section className={`pane chat-pane ${tab === 'live' ? 'hidden-mobile' : ''}`}>
          <div className="pane-head">
            <div>
              <h2>Conversation</h2>
              <p>Requirements, matches, and the final fact-check report.</p>
            </div>
            <span className="phase-chip">{PHASE_LABEL[session.phase]}</span>
          </div>

          <div className="chat-scroll">
            {session.messages.length === 0 && (
              <div className="msg assistant">
                <div className="msg-meta">TruthPhone</div>
                <div className="markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {WELCOME}
                  </ReactMarkdown>
                </div>
              </div>
            )}

            {session.messages.map((message, index) => (
              <div
                className={`msg ${message.role === 'user' ? 'user' : 'assistant'}`}
                key={`${message.ts}-${index}`}
              >
                <div className="msg-meta">
                  {message.role === 'user' ? 'You' : 'TruthPhone'}
                </div>
                {message.role === 'user' ? (
                  <div className="user-text">{message.content}</div>
                ) : (
                  <div className="markdown">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={markdownComponents}
                    >
                      {message.content}
                    </ReactMarkdown>
                  </div>
                )}
              </div>
            ))}

            {hasResults && (
              <div className="results-block">
                <div className="section-label">Live matches</div>
                {session.searchResults.map((phone, index) => (
                  <div
                    className={`phone-card ${
                      session.selectedIndex === index ? 'selected' : ''
                    }`}
                    key={`${phone.asin || phone.url}-${index}`}
                  >
                    <div className="phone-top">
                      <span className="phone-rank">{index + 1}</span>
                      <div className="phone-title-wrap">
                        <h3>{phone.name}</h3>
                        <p className="phone-price">{phone.price}</p>
                      </div>
                    </div>
                    <p className="phone-specs">{phone.specs}</p>
                    <div className="phone-actions">
                      {phone.url && phone.url !== 'N/A' && (
                        <a href={phone.url} target="_blank" rel="noreferrer">
                          View product
                        </a>
                      )}
                      <button
                        className="primary-btn small"
                        onClick={() => select(index)}
                        disabled={disabled}
                      >
                        {session.selectedIndex === index && running
                          ? 'Fact-checking…'
                          : 'Fact-check this'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {session.verificationResults.length > 0 && (
              <div className="results-block">
                <div className="section-label">Claim check</div>
                {session.verificationResults.map((result, index) => (
                  <div
                    className={`claim-card ${
                      result.is_verified ? 'ok' : 'warn'
                    }`}
                    key={`${result.claim}-${index}`}
                  >
                    <div className="claim-top">
                      <span className="claim-badge">
                        {result.is_verified ? 'Verified' : 'Misleading'}
                      </span>
                      <strong>{result.claim}</strong>
                    </div>
                    <p>{result.explanation}</p>
                  </div>
                ))}
              </div>
            )}

            {session.finalReport && (
              <div className="results-block final-report">
                <div className="section-label">Final report</div>
                <div className="markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {session.finalReport}
                  </ReactMarkdown>
                </div>
              </div>
            )}

            {running && (
              <div className="typing">
                <span className="dot" />
                <span className="dot" />
                <span className="dot" />
                <span>{typingLabel}</span>
              </div>
            )}

            {(error || session.error) && (
              <div className="error-banner">{error || session.error}</div>
            )}

            <div ref={bottomRef} />
          </div>

          <div className="composer">
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  send()
                }
              }}
              rows={1}
              disabled={disabled}
              placeholder={
                session.phase === 'selection'
                  ? 'Ask a question, or type a number (1, 2…)'
                  : forceSearch
                    ? 'Send a search, e.g. Samsung 5G under ₹20,000'
                    : 'Tell me your budget, brand, and what you need…'
              }
            />
            <div className="composer-actions">
              <button
                className={`search-toggle ${forceSearch ? 'active' : ''}`}
                onClick={() => setForceSearch((value) => !value)}
                disabled={disabled}
                title="Send this message as a new live shopping search"
              >
                <span className="toggle-dot" />
                Live shopping search
              </button>
              <button
                className="send-btn"
                onClick={send}
                disabled={disabled || !input.trim()}
              >
                {busy ? 'Sending…' : 'Send'}
              </button>
            </div>
          </div>
        </section>

        <section className={`pane live-pane ${tab === 'chat' ? 'hidden-mobile' : ''}`}>
          <div className="pane-head">
            <div>
              <h2>Live browser</h2>
              <p>Real Chrome on AgentCore Browser. Click or type directly in the view.</p>
            </div>
            <span className={`live-pill ${live ? 'on' : ''}`}>
              <span className="status-dot" />
              {live ? 'LIVE' : session.browserStatus.toUpperCase()}
            </span>
          </div>

          <div className="viewer">
            {live ? (
              <BrowserLiveView
                key={session.liveViewUrl}
                signedUrl={session.liveViewUrl}
                remoteWidth={REMOTE_WIDTH}
                remoteHeight={REMOTE_HEIGHT}
              />
            ) : (
              <div className="live-placeholder">
                <div className={`radar ${session.browserStatus}`}>
                  <span />
                </div>
                <h3>{placeholder.title}</h3>
                <p>{placeholder.body}</p>
              </div>
            )}
          </div>

          <div className="live-foot">
            <span>Interactive DCV stream · signed URL refreshes every 4 min</span>
            <span>CAPTCHA? Solve it directly in the browser.</span>
          </div>
        </section>
      </main>
    </div>
  )
}
