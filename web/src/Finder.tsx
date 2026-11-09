// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useCallback, useEffect, useState } from 'react'
import { ApiError, finder, finderToken, pub, type FinderMatch, type FinderView } from './api'
import { Card, Err, Field, Icon, Pill, Shell } from './ui'

function FoundForm({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [preview, setPreview] = useState<string | null>(null)
  return (
    <form
      className="space-y-3"
      onSubmit={async (e) => {
        e.preventDefault()
        setBusy(true)
        setErr('')
        try {
          const r = await pub.reportFound(new FormData(e.currentTarget))
          finderToken.set(r.token)
          onDone()
        } catch (x) {
          setErr((x as Error).message)
        } finally {
          setBusy(false)
        }
      }}
    >
      <label className="block cursor-pointer">
        {preview ? (
          <img src={preview} alt="" className="h-56 w-full rounded-xl object-cover" />
        ) : (
          <div className="flex h-56 flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-line text-mute">
            <Icon name="camera" size={44} />
            <div className="text-sm">Take a photo of what you found</div>
          </div>
        )}
        <input
          name="photos"
          type="file"
          accept="image/jpeg,image/png,image/webp"
          capture="environment"
          className="hidden"
          required
          onChange={(e) => {
            const f = e.target.files?.[0]
            setPreview(f ? URL.createObjectURL(f) : null)
          }}
        />
      </label>
      <Field label="What is it?">
        <input name="description" className="input" required minLength={2} maxLength={1000} placeholder="Keys left on a metro seat" />
      </Field>
      <Field label="Where did you find it?">
        <input name="place" className="input" required minLength={2} maxLength={120} placeholder="Indiranagar metro" />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Your name">
          <input name="finder_name" className="input" required maxLength={60} />
        </Field>
        <Field label="PayPal email" hint="Where the reward is sent.">
          <input name="finder_email" type="email" className="input" required />
        </Field>
      </div>
      <Err text={err} />
      <button className="btn-primary w-full py-3 text-base" disabled={busy}>
        {busy ? 'Looking for the owner…' : 'Find the owner'}
      </button>
      <p className="text-center text-[11px] text-mute">Keep the item safe until the owner confirms it's theirs.</p>
    </form>
  )
}

function Match({ m, onChange }: { m: FinderMatch; onChange: () => void }) {
  const [question, setQuestion] = useState<string | null>(m.question)
  const [answer, setAnswer] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [verdict, setVerdict] = useState<{ verdict: string; reason: string } | null>(null)
  useEffect(() => setQuestion(m.question), [m.question])
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr('')
    try {
      await fn()
    } catch (x) {
      setErr((x as Error).message)
    } finally {
      setBusy(false)
      onChange()
    }
  }
  const paid = m.payout
  return (
    <Card className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="font-medium">{m.title}</div>
          <div className="text-xs text-mute">lost at {m.lost_place}</div>
        </div>
        <div className="text-right">
          <div className="mono text-xl font-semibold text-amber-300">${m.reward}</div>
          <div className="text-[10px] text-mute">reward</div>
        </div>
      </div>
      <Pill tone={m.score >= 80 ? 'good' : 'warn'}>{m.score}% match</Pill>
      {m.status === 'PROPOSED' && !question && (
        <button className="btn-primary w-full" disabled={busy} onClick={() => act(async () => setQuestion((await finder.challenge(m.match_id)).question))}>
          {busy ? 'Getting a question…' : "That's probably it — prove I have it"}
        </button>
      )}
      {question && m.finder_verdict == null && m.status !== 'REJECTED' && (
        <div className="space-y-2 rounded-lg border border-sky-500/30 bg-sky-500/5 p-3">
          <div className="text-xs text-sky-300">Look at the item and answer. The owner told us the answer privately.</div>
          <div className="font-medium">{question}</div>
          <input className="input" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer" maxLength={300} />
          <button className="btn-primary w-full" disabled={busy || !answer.trim()} onClick={() => act(async () => setVerdict(await finder.answer(m.match_id, answer)))}>
            {busy ? 'Checking…' : 'Answer'}
          </button>
          <div className="text-[11px] text-mute">One try per match.</div>
        </div>
      )}
      {verdict && <p className={`text-sm ${verdict.verdict === 'yes' ? 'text-green-300' : 'text-red-300'}`}>{verdict.reason}</p>}
      {m.status === 'REJECTED' && <p className="text-sm text-red-300">That doesn't match the owner's details, so this isn't their item.</p>}
      {m.status === 'REVIEW' && <p className="text-sm text-amber-300">A person at Finders is checking this before anything is paid.</p>}
      {m.waiting_for_owner && <p className="text-sm text-green-300">You proved it. Now the owner is checking it's theirs…</p>}
      {m.status === 'VERIFIED' && !m.can_claim && <p className="text-sm text-green-300">Confirmed on both sides. Arrange to meet; the owner will give you a code when they have it back.</p>}
      {m.can_claim && !paid && (
        <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
          <div className="text-sm">Hand the item over, then type the owner's 6-digit code.</div>
          <input className="input mono text-center text-2xl tracking-[0.4em]" inputMode="numeric" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} placeholder="000000" />
          <button className="btn-primary w-full" disabled={busy || code.length !== 6} onClick={() => act(() => finder.claim(m.match_id, code))}>
            {busy ? 'Paying you…' : 'Get my reward'}
          </button>
        </div>
      )}
      {paid && (
        <div className="rounded-lg border border-green-500/40 bg-green-500/10 p-3 text-center">
          <Icon name="cash" size={40} className="mx-auto" />
          <div className="font-semibold">${paid.amount} sent to your PayPal</div>
          <div className="mono mt-1 text-[11px] text-mute">
            payout {paid.batch} · {paid.item_status ?? paid.state}
          </div>
          {paid.item_status === 'UNCLAIMED' && <div className="mt-1 text-xs text-amber-300">PayPal is holding it until you sign up with that email.</div>}
        </div>
      )}
      <Err text={err} />
    </Card>
  )
}

export function Finder() {
  const [v, setV] = useState<FinderView | null>(null)
  const [has, setHas] = useState(!!finderToken.get())
  const load = useCallback(async () => {
    if (!finderToken.get()) return
    try {
      setV(await finder.view())
    } catch (x) {
      if (x instanceof ApiError && x.status === 401) {
        finderToken.clear()
        setHas(false)
      }
    }
  }, [])
  useEffect(() => {
    if (!has) return
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [has, load])

  if (!has) {
    return (
      <Shell>
        <div>
          <h1 className="text-xl font-semibold">You found something</h1>
          <p className="text-sm text-mute">Snap a photo. The AI looks for the owner's report, and if they offered a reward it can be yours.</p>
        </div>
        <Card>
          <FoundForm onDone={() => setHas(true)} />
        </Card>
      </Shell>
    )
  }
  if (!v) return <Shell><p className="text-mute">Loading…</p></Shell>
  return (
    <Shell>
      <div>
        <h1 className="text-xl font-semibold">Your find</h1>
        <div className="text-xs text-mute">{v.found.place}</div>
        {v.found.attributes.summary && <div className="mt-1 text-sm text-slate-300">AI sees: {v.found.attributes.summary}</div>}
      </div>
      {v.matches.length === 0 ? (
        <Card className="text-center">
          <div className="font-medium">No matching report yet</div>
          <p className="mt-1 text-sm text-mute">If the owner reports it later, it will show up here.</p>
        </Card>
      ) : (
        v.matches.map((m) => <Match key={m.match_id} m={m} onChange={load} />)
      )}
      <button
        className="mt-auto text-[11px] text-mute underline"
        onClick={() => {
          finderToken.clear()
          setHas(false)
          setV(null)
        }}
      >
        Report another find
      </button>
    </Shell>
  )
}
