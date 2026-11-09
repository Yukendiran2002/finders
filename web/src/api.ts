// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron

export type Money = {
  reward: string
  fee: string
  paid: string
  paid_out: string
  refunded: string
  held: string
  capture_id: string | null
  order_id: string | null
  settlements: { id: string; kind: 'payout' | 'refund'; amount: string; state: string; paypal_id: string | null; item_status: string | null; error: string | null }[]
}

export type Case = {
  id: string
  status: 'UNPAID' | 'OPEN' | 'MATCHED' | 'RETURNED' | 'EXPIRED' | 'CANCELLED'
  title: string
  description: string
  place: string
  lost_at: number
  reward: string
  fee: string
  attributes: { category?: string; brand?: string; colours?: string[]; marks?: string[]; contents?: string[]; summary?: string }
  expires_at: number | null
  sample: boolean
  photo_id: string | null
}

export type OwnerMatch = {
  match_id: string
  status: string
  score: number
  reasons: string[]
  found_place: string
  found_at: number
  finder_name: string | null
  photo_id: string | null
  finder_proved: boolean
  owner_question: string | null
  owner_verdict: string | null
  reward: string | null
  code_issued: boolean
}

export type OwnerView = { case: Case; matches: OwnerMatch[]; money: Money }

export type FinderMatch = {
  match_id: string
  status: string
  score: number
  title: string
  lost_place: string
  reward: string
  question: string | null
  finder_verdict: string | null
  waiting_for_owner: boolean
  can_claim: boolean
  payout: { state: string; item_status: string | null; amount: string; batch: string | null } | null
}

export type FinderView = {
  found: { id: string; status: string; place: string; description: string; attributes: Case['attributes']; finder_name: string }
  matches: FinderMatch[]
}

export type BoardItem = Pick<Case, 'id' | 'title' | 'description' | 'place' | 'lost_at' | 'reward' | 'sample' | 'photo_id'>

export type OpsMatch = {
  id: string
  lost_id: string
  found_id: string
  score: number
  status: string
  reasons: string[]
  flags: string[]
  finder_question: string | null
  finder_verdict: string | null
  owner_question: string | null
  owner_verdict: string | null
  reward: string | null
}
export type OpsCase = Case & Money & { matches: number; owner_name: string }
export type OpsOverview = {
  cases: OpsCase[]
  found: { id: string; status: string; place: string; description: string; finder_name: string; found_at: number; attributes: Case['attributes'] }[]
  matches: OpsMatch[]
}
export type FeedEvent = { seq: number; at: number; case_id: string | null; kind: string; data: Record<string, unknown> }
export type Audit = { ok: boolean; order_id: string; checks: { check: string; ok: boolean; finders: string; paypal: string }[] }

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function send<T>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const isForm = body instanceof FormData
  const r = await fetch(path, {
    method,
    headers: isForm || body === undefined ? headers : { 'content-type': 'application/json', ...headers },
    body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
  })
  if (!r.ok) {
    let msg = r.statusText
    try {
      const j = await r.json()
      msg = typeof j.detail === 'string' ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: { msg: string }) => d.msg).join('; ') : JSON.stringify(j)
    } catch {
      /* keep status text */
    }
    throw new ApiError(r.status, msg)
  }
  return (await r.json()) as T
}

const store = (key: string) => ({
  get: () => localStorage.getItem(key) ?? '',
  set: (v: string) => localStorage.setItem(key, v),
  clear: () => localStorage.removeItem(key),
})
export const ownerToken = store('finders.owner')
export const finderToken = store('finders.finder')
export const opKey = store('finders.op')
const bearer = (t: string) => ({ Authorization: `Bearer ${t}` })

export const pub = {
  config: () => send<{ paypal_client_id: string | null; environment: string; ai: string }>('GET', '/api/config'),
  board: () => send<BoardItem[]>('GET', '/api/board'),
  reportLost: (f: FormData) => send<{ case_id: string; token: string }>('POST', '/api/lost', f),
  reportFound: (f: FormData) => send<{ found_id: string; token: string; matches: { match_id: string; lost_id: string; score: number; reasons: string[] }[] }>('POST', '/api/found', f),
}

export const owner = {
  view: () => send<OwnerView>('GET', '/api/owner', undefined, bearer(ownerToken.get())),
  payCard: () => send<{ amount: string; capture_id: string }>('POST', '/api/owner/pay/card', {}, bearer(ownerToken.get())),
  walletOrder: () => send<{ order_id: string }>('POST', '/api/owner/pay/wallet/order', {}, bearer(ownerToken.get())),
  walletCapture: () => send<{ amount: string; capture_id: string }>('POST', '/api/owner/pay/wallet/capture', {}, bearer(ownerToken.get())),
  answer: (match_id: string, answer: string) => send<{ verdict: string; reason: string; status: string }>('POST', '/api/owner/answer', { match_id, answer }, bearer(ownerToken.get())),
  handover: (match_id: string, reward?: string) => send<{ code: string; reward: string }>('POST', '/api/owner/handover', { match_id, reward }, bearer(ownerToken.get())),
  cancel: () => send<Money>('POST', '/api/owner/cancel', {}, bearer(ownerToken.get())),
  ask: (question: string) => send<{ answer: string; tools: string[] }>('POST', '/api/owner/ask', { question }, bearer(ownerToken.get())),
}

export const finder = {
  view: () => send<FinderView>('GET', '/api/finder', undefined, bearer(finderToken.get())),
  challenge: (match_id: string) => send<{ question: string }>('POST', '/api/finder/challenge', { match_id }, bearer(finderToken.get())),
  answer: (match_id: string, answer: string) => send<{ verdict: string; reason: string }>('POST', '/api/finder/answer', { match_id, answer }, bearer(finderToken.get())),
  claim: (match_id: string, code: string) => send<Money>('POST', '/api/finder/claim', { match_id, code }, bearer(finderToken.get())),
}

const op = () => ({ 'X-Operator-Key': opKey.get() })
export const ops = {
  overview: () => send<OpsOverview>('GET', '/api/ops/overview', undefined, op()),
  review: (match_id: string, approve: boolean) => send<{ status: string }>('POST', '/api/ops/review', { match_id, approve }, op()),
  housekeeping: () => send<{ expired: string[] }>('POST', '/api/ops/housekeeping', {}, op()),
  audit: (case_id: string) => send<Audit>('POST', `/api/ops/cases/${case_id}/audit`, {}, op()),
  ask: (case_id: string, question: string) => send<{ answer: string; tools: string[] }>('POST', `/api/ops/cases/${case_id}/ask`, { question }, op()),
  /** Server-sent events read with fetch, so the operator key travels in a header, never in the URL. */
  events: (onData: (data: string) => void) => {
    const ctl = new AbortController()
    ;(async () => {
      try {
        const r = await fetch('/api/ops/events', { headers: op(), signal: ctl.signal })
        const reader = r.body!.getReader()
        const dec = new TextDecoder()
        let buf = ''
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          buf += dec.decode(value, { stream: true })
          let i
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const block = buf.slice(0, i)
            buf = buf.slice(i + 2)
            const data = block.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('\n')
            if (data) onData(data)
          }
        }
      } catch {
        /* aborted or disconnected: the 4 s refresh keeps the page current */
      }
    })()
    return () => ctl.abort()
  },
}

export const photoUrl = (id: string) => `/api/photos/${id}`
export const ago = (ts: number) => {
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 90) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}
export const plain = (t: string) => t.replace(/\*\*(.+?)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1')
