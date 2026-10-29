import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Browser speech-to-text for the chat composer.
 *
 * Uses the Web Speech API (Chrome, Edge, Safari). Audio is transcribed by the
 * browser's speech service; nothing is sent to our backend. Firefox has no
 * implementation, so the mic button stays disabled there.
 */

interface SpeechAlternative {
  transcript: string
}

interface SpeechResult {
  isFinal: boolean
  [index: number]: SpeechAlternative
}

interface SpeechResultList {
  length: number
  [index: number]: SpeechResult
}

interface SpeechResultEvent {
  resultIndex: number
  results: SpeechResultList
}

interface SpeechErrorEvent {
  error?: string
}

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  maxAlternatives: number
  start: () => void
  stop: () => void
  abort: () => void
  onstart: (() => void) | null
  onend: (() => void) | null
  onresult: ((event: SpeechResultEvent) => void) | null
  onerror: ((event: SpeechErrorEvent) => void) | null
}

type RecognitionCtor = new () => SpeechRecognitionLike

interface DictationOptions {
  /** BCP-47 language tag. en-IN suits Indian English accents. */
  lang?: string
  /** Called with the full composer text (existing text + dictation). */
  onText: (text: string) => void
  onError: (message: string) => void
}

const MAX_LISTEN_MS = 90_000

const ERROR_TEXT: Record<string, string> = {
  'not-allowed':
    'Microphone blocked. Allow mic access for this site in your browser settings.',
  'service-not-allowed':
    'Microphone blocked. Allow mic access for this site in your browser settings.',
  'audio-capture': 'No microphone found. Check your input device.',
  'no-speech': "Didn't catch that - try speaking again.",
  network: 'Speech service unreachable. Check your connection and try again.',
}

function recognitionCtor(): RecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const globalWindow = window as unknown as {
    SpeechRecognition?: RecognitionCtor
    webkitSpeechRecognition?: RecognitionCtor
  }
  return globalWindow.SpeechRecognition || globalWindow.webkitSpeechRecognition || null
}

export function useDictation({ lang = 'en-IN', onText, onError }: DictationOptions) {
  const [supported] = useState(() => Boolean(recognitionCtor()))
  const [listening, setListening] = useState(false)

  const recognitionRef = useRef<SpeechRecognitionLike | null>(null)
  const baseRef = useRef('')
  const finalRef = useRef('')
  const timerRef = useRef<number | null>(null)
  const callbacksRef = useRef({ onText, onError })
  callbacksRef.current = { onText, onError }

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  useEffect(() => {
    const Recognition = recognitionCtor()
    if (!Recognition) return

    const recognition = new Recognition()
    recognition.lang = lang
    recognition.continuous = true
    recognition.interimResults = true
    recognition.maxAlternatives = 1

    recognition.onstart = () => setListening(true)
    recognition.onend = () => {
      setListening(false)
      clearTimer()
    }
    recognition.onerror = (event) => {
      const code = event?.error || 'unknown'
      setListening(false)
      clearTimer()
      if (code === 'aborted') return
      callbacksRef.current.onError(
        ERROR_TEXT[code] || `Voice input error (${code}). Try again.`
      )
    }
    recognition.onresult = (event) => {
      let interim = ''
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i]
        const text = result?.[0]?.transcript ?? ''
        if (result?.isFinal) {
          finalRef.current += `${finalRef.current ? ' ' : ''}${text.trim()}`
        } else {
          interim += text
        }
      }
      const spoken = [finalRef.current, interim.trim()].filter(Boolean).join(' ')
      callbacksRef.current.onText(`${baseRef.current}${spoken}`)
    }

    recognitionRef.current = recognition
    return () => {
      recognition.onstart = null
      recognition.onresult = null
      recognition.onerror = null
      recognition.onend = null
      recognitionRef.current = null
      clearTimer()
      try {
        recognition.abort()
      } catch {
        // Already stopped.
      }
    }
  }, [lang, clearTimer])

  const start = useCallback(
    (baseText = '') => {
      const recognition = recognitionRef.current
      if (!recognition) {
        callbacksRef.current.onError(
          'Voice input is not supported in this browser. Try Chrome or Edge.'
        )
        return
      }
      baseRef.current = baseText.trim() ? `${baseText.trimEnd()} ` : ''
      finalRef.current = ''
      try {
        recognition.start()
      } catch {
        // InvalidStateError when recognition is already running - keep going.
      }
      setListening(true)
      clearTimer()
      timerRef.current = window.setTimeout(() => {
        try {
          recognition.stop()
        } catch {
          // Already stopped.
        }
      }, MAX_LISTEN_MS)
    },
    [clearTimer]
  )

  const stop = useCallback(() => {
    setListening(false)
    clearTimer()
    try {
      recognitionRef.current?.stop()
    } catch {
      // Not running.
    }
  }, [clearTimer])

  return { supported, listening, start, stop }
}
