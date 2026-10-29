import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { BrowserLiveView } from 'bedrock-agentcore/browser/live-view'
import { api } from './api'
import type { Phase, SessionState, Verdict } from './types'
import { useDictation } from './useDictation'

const SESSION_KEY = 'truthphone.session.v2'
const REMOTE_WIDTH = 1440
const REMOTE_HEIGHT = 900

const VERDICT_LABEL: Record<Verdict, string> = {
  verified: 'True',
  misleading: 'Misleading',
  false: 'False',
}

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

const MISSION_STEPS = [
  { key: 'brief', label: 'Brief' },
  { key: 'search', label: 'Search' },
  { key: 'compare', label: 'Compare' },
  { key: 'verify', label: 'Verify' },
  { key: 'report', label: 'Report' },
] as const

const ACTIVITY_BY_PHASE: Record<Phase, string> = {
  clarification: 'Clarifying your requirements…',
  search: 'Scanning Amazon.in and live retailer listings…',
  comparison: 'Comparing shortlisted phones…',
  selection: 'Matches ready — pick a phone to fact-check.',
  verification: 'Opening the product page and verifying claims…',
  done: 'Mission report ready.',
  error: 'Mission needs attention.',
}

const PHASE_STEP: Record<Phase, number> = {
  clarification: 0,
  search: 1,
  comparison: 2,
  selection: 2,
  verification: 3,
  done: 4,
  error: -1,
}

function stripMarkdown(text: string): string {
  return text.replace(/[#*_`>]/g, ' ').replace(/\s+/g, ' ').trim()
}

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remaining = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
}

function FocusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <path d="M1 4V1h3M9 1h3v3M12 9v3h-3M4 12H1V9" />
    </svg>
  )
}

function MicIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v4M8 22h8" />
    </svg>
  )
}

function PhaseTracker({ phase, running }: { phase: Phase; running: boolean }) {
  const active = PHASE_STEP[phase]
  return (
    <div className={`phase-track ${running ? 'is-running' : ''} ${phase === 'error' ? 'has-error' : ''}`}>
      {MISSION_STEPS.map((step, index) => {
        const state =
          phase === 'error'
            ? 'pending'
            : index < active
              ? 'done'
              : index === active
                ? 'active'
                : 'pending'
        return (
          <div className={`phase-step ${state}`} key={step.key}>
            <span className="phase-node">{index < active ? '✓' : index + 1}</span>
            <span className="phase-label">{step.label}</span>
            {index < MISSION_STEPS.length - 1 && <span className="phase-line" />}
          </div>
        )
      })}
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState<SessionState | null>(null)
  const [input, setInput] = useState('')
  const [forceSearch, setForceSearch] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'chat' | 'live'>('chat')
  const [focusMode, setFocusMode] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [dictationHint, setDictationHint] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)

  const {
    supported: dictationSupported,
    listening: dictating,
    start: startDictation,
    stop: stopDictation,
  } = useDictation({
    onText: (text) => {
      setInput(text)
      setDictationHint('')
    },
    onError: (message) => setDictationHint(message),
  })

  function toggleDictation() {
    if (dictating) {
      stopDictation()
      return
    }
    setDictationHint('')
    startDictation(input)
  }

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

  useEffect(() => {
    // A job is running: stop the mic so it does not keep typing into a
    // disabled composer.
    if (disabled) stopDictation()
  }, [disabled, stopDictation])

  const hasResults =
    Boolean(session?.searchResults.length) &&
    ['selection', 'comparison', 'verification', 'done'].includes(
      session?.phase || ''
    )
  const live = Boolean(
    session?.liveViewUrl && session.browserStatus === 'live'
  )

  useEffect(() => {
    if (running && runStartedAt === null) setRunStartedAt(Date.now())
  }, [running, runStartedAt])

  useEffect(() => {
    if (!running || !runStartedAt) return
    const tick = () => setElapsed(Math.floor((Date.now() - runStartedAt) / 1000))
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [running, runStartedAt])

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const typing =
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        Boolean(target?.isContentEditable)
      if (event.key === 'Escape') {
        if (reportOpen) setReportOpen(false)
        else if (focusMode && chatOpen) setChatOpen(false)
        else if (focusMode) setFocusMode(false)
      } else if (
        (event.key === 'f' || event.key === 'F') &&
        !typing &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey
      ) {
        event.preventDefault()
        setFocusMode((value) => !value)
        setChatOpen(false)
        setReportOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [focusMode, chatOpen, reportOpen])

  async function send() {
    const text = input.trim()
    if (!text || !session || disabled) return

    if (dictating) stopDictation()
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
          if (response.session.jobStatus === 'running') {
            setRunStartedAt(Date.now())
            setElapsed(0)
          }
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
      if (response.session.jobStatus === 'running') {
        setRunStartedAt(Date.now())
        setElapsed(0)
      }
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
      if (response.session.jobStatus === 'running') {
        setRunStartedAt(Date.now())
        setElapsed(0)
      }
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  async function reset() {
    if (!session || disabled) return
    stopDictation()
    setDictationHint('')
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
      setFocusMode(false)
      setChatOpen(false)
      setReportOpen(false)
      setRunStartedAt(null)
      setElapsed(0)
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
  const lastUserMessage =
    [...session.messages].reverse().find((message) => message.role === 'user')
      ?.content || ''
  const missionQuery = lastUserMessage || 'Waiting for your brief'
  const lastAssistantMessage =
    [...session.messages]
      .reverse()
      .find((message) => message.role === 'assistant')?.content || ''
  const activityText = (() => {
    if (running) {
      if (lastAssistantMessage && lastAssistantMessage.length < 180) {
        return stripMarkdown(lastAssistantMessage)
      }
      return ACTIVITY_BY_PHASE[session.phase]
    }
    if (session.error) return 'Mission needs attention.'
    if (session.finalReport) return 'Mission report ready.'
    return ACTIVITY_BY_PHASE[session.phase]
  })()
  const selectedPhone =
    session.selectedIndex !== null
      ? session.searchResults[session.selectedIndex]
      : undefined
  const verdictCounts = session.verificationResults.reduce(
    (counts, result) => {
      const key: Verdict =
        result.verdict === 'false'
          ? 'false'
          : result.verdict === 'misleading'
            ? 'misleading'
            : 'verified'
      counts[key] += 1
      return counts
    },
    { verified: 0, misleading: 0, false: 0 }
  )

  return (
    <div className={`app-shell ${focusMode ? 'focus-mode' : ''}`}>
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
          <button
            className={`ghost-btn focus-btn ${focusMode ? 'active' : ''}`}
            onClick={() => {
              setFocusMode((value) => !value)
              setChatOpen(false)
              setReportOpen(false)
            }}
            title="Focus mode (press F)"
            aria-pressed={focusMode}
          >
            <FocusIcon />
            {focusMode ? 'Exit focus' : 'Focus'}
          </button>
          <button className="ghost-btn" onClick={reset} disabled={disabled}>
            New chat
          </button>
        </div>
      </header>

      <section className={`mission-bar ${focusMode ? 'in-focus' : ''} ${tone}`}>
        <div className="mission-brief">
          <span className="mission-kicker">Brief</span>
          <span className="mission-query" title={missionQuery}>
            {missionQuery}
          </span>
        </div>
        <PhaseTracker phase={session.phase} running={Boolean(running)} />
        <div className="mission-status">
          <span className={`mission-activity ${running ? 'is-live' : ''}`}>
            <span className="mission-dot" />
            {activityText}
          </span>
          {running && (
            <span className="mission-timer">{formatElapsed(elapsed)}</span>
          )}
        </div>
      </section>

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

      <main className={`workspace ${focusMode ? 'focus' : ''}`}>
        {focusMode && chatOpen && (
          <div
            className="drawer-scrim"
            onClick={() => setChatOpen(false)}
            aria-hidden="true"
          />
        )}
        <section
          className={`pane chat-pane ${tab === 'live' ? 'hidden-mobile' : ''} ${
            focusMode && chatOpen ? 'drawer-open' : ''
          }`}
        >
          <div className="pane-head">
            <div>
              <h2>Conversation</h2>
              <p>Requirements, matches, and the final fact-check report.</p>
            </div>
            <div className="pane-head-actions">
              <span className="phase-chip">{PHASE_LABEL[session.phase]}</span>
              <button
                className="drawer-close"
                type="button"
                aria-label="Close chat"
                onClick={() => setChatOpen(false)}
              >
                ×
              </button>
            </div>
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
                <div className="claim-summary">
                  <span className="section-label">Claim check</span>
                  <span className="claim-tally">
                    {verdictCounts.verified} true · {verdictCounts.misleading}{' '}
                    misleading · {verdictCounts.false} false
                  </span>
                </div>
                {session.verificationResults.map((result, index) => {
                  const verdict: Verdict =
                    result.verdict === 'false'
                      ? 'false'
                      : result.verdict === 'misleading'
                        ? 'misleading'
                        : 'verified'
                  const evidence = result.evidence || []
                  return (
                    <div
                      className={`claim-card ${verdict}`}
                      key={`${result.claim}-${index}`}
                    >
                      <div className="claim-top">
                        <span className={`claim-badge ${verdict}`}>
                          {VERDICT_LABEL[verdict]}
                        </span>
                        <strong>{result.claim}</strong>
                        {result.severity === 'high' && verdict !== 'verified' && (
                          <span className="claim-severity">HIGH</span>
                        )}
                      </div>
                      {(result.category ||
                        (result.confidence && result.confidence > 0)) && (
                        <div className="claim-meta">
                          {result.category && (
                            <span className="claim-cat">{result.category}</span>
                          )}
                          {result.confidence > 0 && (
                            <span>
                              {Math.round(result.confidence * 100)}% confidence
                            </span>
                          )}
                        </div>
                      )}
                      {result.what_they_say && (
                        <p className="claim-says">
                          <span>They say</span> {result.what_they_say}
                        </p>
                      )}
                      {(result.reality || result.explanation) && (
                        <p className="claim-reality">
                          <span>Actually</span>{' '}
                          {result.reality || result.explanation}
                        </p>
                      )}
                      {result.hidden_catch && (
                        <p className="claim-catch">
                          <span>Hidden catch</span> {result.hidden_catch}
                        </p>
                      )}
                      {evidence.length > 0 && (
                        <div className="claim-evidence">
                          {evidence.map((item, evidenceIndex) => (
                            <span key={`${item.source}-${evidenceIndex}`}>
                              {item.url ? (
                                <a
                                  href={item.url}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {item.source || 'Source'}
                                </a>
                              ) : (
                                item.source
                              )}
                              {item.quote ? ` — “${item.quote}”` : ''}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}

            {session.finalReport && (
              <div className="results-block final-report">
                <div className="final-report-head">
                  <div className="section-label">Final report</div>
                  <button
                    className="report-ready-btn"
                    type="button"
                    onClick={() => {
                      setReportOpen(true)
                      setChatOpen(false)
                    }}
                  >
                    Open full report
                  </button>
                </div>
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
            {(dictating || dictationHint) && (
              <p
                className={`dictation-hint ${dictationHint ? 'is-error' : ''}`}
                role="status"
              >
                {dictationHint ||
                  'Listening… speak now. Click Speak again to stop.'}
              </p>
            )}
            <div className="composer-actions">
              <div className="composer-left">
                <button
                  className={`mic-btn ${dictating ? 'listening' : ''}`}
                  type="button"
                  onClick={toggleDictation}
                  disabled={disabled || !dictationSupported}
                  aria-pressed={dictating}
                  title={
                    dictationSupported
                      ? dictating
                        ? 'Stop voice input'
                        : 'Speak your message instead of typing'
                      : 'Voice input needs Chrome or Edge'
                  }
                >
                  <MicIcon />
                  <span className="mic-label">
                    {dictating ? 'Listening' : 'Speak'}
                  </span>
                  {dictating && (
                    <span className="mic-bars" aria-hidden="true">
                      <i />
                      <i />
                      <i />
                    </span>
                  )}
                </button>
                <button
                  className={`search-toggle ${forceSearch ? 'active' : ''}`}
                  onClick={() => setForceSearch((value) => !value)}
                  disabled={disabled}
                  title="Send this message as a new live shopping search"
                >
                  <span className="toggle-dot" />
                  Live shopping search
                </button>
              </div>
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

      {focusMode && (
        <div className="focus-dock">
          {!chatOpen && (
            <button
              className="dock-btn"
              type="button"
              onClick={() => setChatOpen(true)}
            >
              Chat
            </button>
          )}
          {session.finalReport && !reportOpen && (
            <button
              className="dock-btn accent"
              type="button"
              onClick={() => setReportOpen(true)}
            >
              Report
            </button>
          )}
        </div>
      )}

      {reportOpen && session.finalReport && (
        <div
          className="report-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Final fact-check report"
          onClick={() => setReportOpen(false)}
        >
          <div
            className="report-panel"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="report-head">
              <div className="report-title">
                <div className="section-label">Mission report</div>
                <h2>{selectedPhone?.name || 'Fact-check report'}</h2>
                {selectedPhone?.price && (
                  <p className="report-price">{selectedPhone.price}</p>
                )}
              </div>
              <button
                className="report-close"
                type="button"
                onClick={() => setReportOpen(false)}
                aria-label="Close report"
              >
                ×
              </button>
            </header>
            <div className="report-body markdown">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {session.finalReport}
              </ReactMarkdown>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
