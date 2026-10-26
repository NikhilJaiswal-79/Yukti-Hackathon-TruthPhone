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

export interface VerificationClaim {
  claim: string
  context: string
}

export interface VerificationResult {
  claim: string
  is_verified: boolean
  explanation: string
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
