export type Phase =
  | 'clarification'
  | 'search'
  | 'comparison'
  | 'selection'
  | 'verification'
  | 'done'
  | 'error'

export type BrowserStatus = 'idle' | 'starting' | 'live' | 'closed' | 'error'
export type JobStatus = 'idle' | 'running' | 'error'

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  ts: number
}

export interface PhoneCandidate {
  name: string
  price: string
  specs: string
  url: string
  details: string
  asin: string
}

export type Verdict = 'verified' | 'misleading' | 'false'
export type Severity = 'high' | 'medium' | 'low'

export interface VerificationClaim {
  claim: string
  context: string
  category?: string
  risk?: string
}

export interface Evidence {
  source: string
  url: string
  quote: string
}

export interface VerificationResult {
  claim: string
  verdict: Verdict
  is_verified: boolean
  severity: Severity
  category: string
  what_they_say: string
  reality: string
  explanation: string
  hidden_catch: string
  confidence: number
  evidence: Evidence[]
}

export interface SessionState {
  sessionId: string
  messages: ChatMessage[]
  phase: Phase
  requirements: Record<string, unknown>
  searchResults: PhoneCandidate[]
  comparisonSummary: string
  selectedIndex: number | null
  claims: VerificationClaim[]
  verificationResults: VerificationResult[]
  criticSynthesis: string
  finalReport: string
  liveViewUrl: string
  liveViewExpiresAt: number
  browserStatus: BrowserStatus
  jobStatus: JobStatus
  error: string
  createdAt: number
  updatedAt: number
}

export interface QueueResponse {
  queued: boolean
  action: string
  session: SessionState
}
