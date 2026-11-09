// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { PayPalButtons, PayPalScriptProvider } from '@paypal/react-paypal-js'
import { useCallback, useEffect, useState } from 'react'
import { ApiError, ago, owner, ownerToken, photoUrl, plain, pub, type OwnerMatch, type OwnerView } from './api'
import { Card, Err, Field, Icon, MoneyStrip, Pill, Shell, Steps } from './ui'

function localNow(offsetMin = 60) {
  const d = new Date(Date.now() - offsetMin * 60000 - new Date().getTimezoneOffset() * 60000)
  return d.toISOString().slice(0, 16)
}

function ReportForm({ onDone }: { onDone: () => void }) {
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
        const f = new FormData(e.currentTarget)
        f.set('lost_at', String(new Date(String(f.get('lost_at_local'))).getTime() / 1000))
        f.delete('lost_at_local')
        const photo = f.get('photos')
        if (photo instanceof File && photo.size === 0) f.delete('photos')
        try {
          const r = await pub.reportLost(f)
          ownerToken.set(r.token)
          onDone()
        } catch (x) {
          setErr((x as Error).message)
        } finally {
          setBusy(false)
        }
      }}
    >
      <Field label="What did you lose?">
        <input name="title" className="input" required minLength={2} maxLength={80} placeholder="Brown leather wallet" />
      </Field>
      <Field label="Describe it" hint="Shown to people who might have found it.">
        <textarea name="description" className="input min-h-20" maxLength={1000} placeholder="Bifold, worn corners, a few cards inside" />
      </Field>
      <Field label="Something only you know" hint="Never shown to anyone. The AI uses it to check that a finder really has your item.">
        <textarea name="private_details" className="input min-h-16" required minLength={3} maxLength={500} placeholder="A photo of my daughter is inside the coin pocket" />
      </Field>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Where">
          <input name="place" className="input" required minLength={2} maxLength={120} placeholder="Bus 500D, Silk Board" />
        </Field>
        <Field label="When">
          <input name="lost_at_local" type="datetime-local" className="input" required defaultValue={localNow()} />
        </Field>
      </div>
      <Field label="Photo (optional)" hint="An old photo of the item helps the AI match it.">
        <input
          name="photos"
          type="file"
          accept="image/jpeg,image/png,image/webp"
          className="input file:mr-3 file:rounded-md file:border-0 file:bg-line file:px-2 file:py-1 file:text-slate-200"
          onChange={(e) => {
            const file = e.target.files?.[0]
            setPreview(file ? URL.createObjectURL(file) : null)
          }}
        />
      </Field>
      {preview && <img src={preview} alt="" className="h-28 rounded-lg object-cover" />}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Your name">
          <input name="owner_name" className="input" required maxLength={60} />
        </Field>
        <Field label="Email">
          <input name="owner_email" type="email" className="input" required />
        </Field>
      </div>
      <Field label="Reward (USD)" hint="Paid to the finder only when you confirm it's back. Refunded if nothing turns up in 14 days. Finders' fee: $1.00.">
        <input name="reward" className="input mono" required defaultValue="40.00" pattern="\d{1,3}(\.\d{1,2})?" />
      </Field>
      <Err text={err} />
      <button className="btn-primary w-full py-3 text-base" disabled={busy}>
        {busy ? 'Reading your report…' : 'Continue to reward'}
      </button>
    </form>
  )
}

function Pay({ v, clientId, onPaid }: { v: OwnerView; clientId: string | null; onPaid: () => void }) {
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const total = (Number(v.case.reward) + Number(v.case.fee)).toFixed(2)
  return (
    <Card className="space-y-3">
      <div className="label">Pay the reward</div>
      <div className="mono text-3xl font-semibold">${total}</div>
      <p className="text-sm text-slate-300">
        ${v.case.reward} reward + ${v.case.fee} fee. The reward waits with Finders and goes to the finder only after you confirm the item is back. Nothing found in 14 days: the reward is refunded.
      </p>
      {clientId && (
        <PayPalScriptProvider options={{ clientId, intent: 'capture', currency: 'USD', components: 'buttons' }}>
          <div className="rounded-lg bg-white p-2">
            <PayPalButtons
              style={{ layout: 'vertical', tagline: false, height: 44 }}
              fundingSource="paypal"
              createOrder={async () => (await owner.walletOrder()).order_id}
              onApprove={async () => {
                try {
                  await owner.walletCapture()
                  onPaid()
                } catch (x) {
                  setErr((x as Error).message)
                }
              }}
              onError={(e) => setErr(String(e))}
            />
          </div>
        </PayPalScriptProvider>
      )}
      <button
        className="btn-ghost w-full"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setErr('')
          try {
            await owner.payCard()
            onPaid()
          } catch (x) {
            setErr((x as Error).message)
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? 'Paying…' : 'Use a sandbox test card instead'}
      </button>
      <Err text={err} />
    </Card>
  )
}

function MatchCard({ m, onChange }: { m: OwnerMatch; onChange: () => void }) {
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [code, setCode] = useState<{ code: string; reward: string } | null>(null)
  const [reduce, setReduce] = useState(false)
  const [reward, setReward] = useState('')
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
  return (
    <Card className="space-y-3">
      <div className="flex items-start gap-3">
        {m.photo_id ? (
          <img src={photoUrl(m.photo_id)} alt="Found item" className="h-24 w-24 shrink-0 rounded-lg object-cover" />
        ) : (
          <div className="flex h-24 w-24 shrink-0 items-center justify-center rounded-lg bg-ink p-2 text-center text-[11px] text-mute">photo shown after the finder proves it</div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex items-center justify-between gap-2">
            <div className="font-medium">Possible match</div>
            <Pill tone={m.score >= 80 ? 'good' : 'warn'}>{m.score}% match</Pill>
          </div>
          <div className="text-xs text-mute">
            Found at {m.found_place} · {ago(m.found_at)}
            {m.finder_name ? ` · by ${m.finder_name}` : ''}
          </div>
          <ul className="list-disc pl-4 text-xs text-slate-300">
            {m.reasons.slice(0, 3).map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      </div>
      {!m.finder_proved && m.status !== 'REJECTED' && <p className="text-sm text-mute">Waiting for the finder to answer a question about your item…</p>}
      {m.status === 'REJECTED' && <p className="text-sm text-red-300">This one didn't check out. Your reward stays safe.</p>}
      {m.status === 'REVIEW' && <p className="text-sm text-amber-300">A person at Finders is checking this match before anyone is paid.</p>}
      {m.finder_proved && m.owner_question && (
        <div className="space-y-2 rounded-lg border border-sky-500/30 bg-sky-500/5 p-3">
          <div className="text-sm">
            <span className="font-medium text-sky-300">The finder proved they have it.</span> Now prove it's yours:
          </div>
          <div className="font-medium">{m.owner_question}</div>
          <input className="input" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer" maxLength={300} />
          <button className="btn-primary w-full" disabled={busy || !answer.trim()} onClick={() => act(() => owner.answer(m.match_id, answer))}>
            {busy ? 'Checking…' : 'Answer'}
          </button>
        </div>
      )}
      {m.status === 'VERIFIED' && (
        <div className="space-y-2 rounded-lg border border-green-500/30 bg-green-500/5 p-3">
          <div className="text-sm font-medium text-green-300">Both sides checked out. Meet the finder to get it back.</div>
          {code ? (
            <div className="text-center">
              <div className="text-xs text-mute">Give this code to the finder only when the item is in your hands</div>
              <div className="mono my-2 text-4xl font-bold tracking-[0.3em] text-amber-300">{code.code}</div>
              <div className="text-xs text-mute">It releases ${code.reward} to them.</div>
            </div>
          ) : (
            <>
              {reduce && <input className="input mono" value={reward} onChange={(e) => setReward(e.target.value)} placeholder="Reduced reward, e.g. 25.00" />}
              <button
                className="btn-primary w-full"
                disabled={busy}
                onClick={() => act(async () => setCode(await owner.handover(m.match_id, reduce && reward ? reward : undefined)))}
              >
                I have it back — show handover code
              </button>
              <button className="w-full text-xs text-mute underline" onClick={() => setReduce((x) => !x)}>
                {reduce ? 'Pay the full reward' : 'Item damaged? Pay a reduced reward'}
              </button>
            </>
          )}
        </div>
      )}
      <Err text={err} />
    </Card>
  )
}

function Ask() {
  const [q, setQ] = useState('')
  const [a, setA] = useState<{ answer: string; tools: string[] } | null>(null)
  const [busy, setBusy] = useState(false)
  return (
    <Card className="space-y-2">
      <div className="label">Ask about your money</div>
      <form
        className="flex gap-2"
        onSubmit={async (e) => {
          e.preventDefault()
          setBusy(true)
          try {
            setA(await owner.ask(q))
          } catch (x) {
            setA({ answer: (x as Error).message, tools: [] })
          } finally {
            setBusy(false)
          }
        }}
      >
        <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Where did my $41 go?" maxLength={500} />
        <button className="btn-ghost shrink-0" disabled={busy || q.trim().length < 2}>
          {busy ? '…' : 'Ask'}
        </button>
      </form>
      {a && (
        <div className="space-y-1">
          <p className="whitespace-pre-line text-sm leading-relaxed">{plain(a.answer)}</p>
          <p className="text-[10px] text-mute">Gemini read {a.tools.includes('paypal_order') ? "PayPal's records (Agent Toolkit)" : 'the ledger'} · read-only</p>
        </div>
      )}
    </Card>
  )
}

export function Owner() {
  const [v, setV] = useState<OwnerView | null>(null)
  const [has, setHas] = useState(!!ownerToken.get())
  const [clientId, setClientId] = useState<string | null>(null)
  const load = useCallback(async () => {
    if (!ownerToken.get()) return
    try {
      setV(await owner.view())
    } catch (x) {
      if (x instanceof ApiError && x.status === 401) {
        ownerToken.clear()
        setHas(false)
      }
    }
  }, [])
  useEffect(() => {
    pub.config().then((c) => setClientId(c.paypal_client_id)).catch(() => undefined)
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
          <h1 className="text-xl font-semibold">Report a lost item</h1>
          <p className="text-sm text-mute">Takes a minute. The AI reads your description so it can spot your item when someone finds it.</p>
        </div>
        <Card>
          <ReportForm onDone={() => setHas(true)} />
        </Card>
      </Shell>
    )
  }
  if (!v) return <Shell><p className="text-mute">Loading…</p></Shell>
  const c = v.case
  const step = c.status === 'UNPAID' ? 0 : c.status === 'RETURNED' ? 4 : v.matches.some((m) => m.status === 'VERIFIED') ? 3 : v.matches.length ? 2 : 1
  const payout = v.money.settlements.find((s) => s.kind === 'payout')
  return (
    <Shell>
      <div className="flex items-start gap-3">
        {c.photo_id && <img src={photoUrl(c.photo_id)} alt="" className="h-16 w-16 rounded-lg object-cover" />}
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">{c.title}</h1>
          <div className="text-xs text-mute">
            {c.place} · lost {ago(c.lost_at)}
          </div>
          {c.attributes.summary && <div className="mt-1 text-xs text-slate-300">AI read it as: {c.attributes.summary}</div>}
        </div>
      </div>
      <Steps step={step} />
      {c.status === 'UNPAID' ? (
        <Pay v={v} clientId={clientId} onPaid={load} />
      ) : (
        <>
          <MoneyStrip
            items={[
              ['Paid', `$${v.money.paid}`],
              ['Waiting', `$${v.money.held}`, 'text-amber-300'],
              ['To finder', `$${v.money.paid_out}`, 'text-green-300'],
              ['Refunded', `$${v.money.refunded}`, 'text-sky-300'],
            ]}
          />
          {c.status === 'RETURNED' && (
            <Card className="border-green-500/40 text-center">
              <Icon name="party" size={40} className="mx-auto" />
              <div className="font-semibold">Back with you. The finder has been paid.</div>
              {payout && <div className="mono mt-1 text-[11px] text-mute">PayPal payout {payout.paypal_id} · {payout.item_status ?? payout.state}</div>}
            </Card>
          )}
          {(c.status === 'EXPIRED' || c.status === 'CANCELLED') && (
            <Card className="text-center">
              <div className="font-semibold">Case closed. Your ${v.money.refunded} reward was refunded.</div>
            </Card>
          )}
          {c.status !== 'RETURNED' && v.matches.length === 0 && c.status === 'OPEN' && (
            <Card className="text-center">
              <div className="font-medium">Reward is live</div>
              <p className="mt-1 text-sm text-mute">When someone reports a find that looks like yours, it appears here. This page updates by itself.</p>
            </Card>
          )}
          {v.matches.map((m) => (
            <MatchCard key={m.match_id} m={m} onChange={load} />
          ))}
          <Ask />
          {(c.status === 'OPEN' || c.status === 'MATCHED') && (
            <button
              className="text-xs text-mute underline"
              onClick={async () => {
                if (confirm('Found it yourself? Your reward will be refunded (the $1 fee is kept).')) {
                  await owner.cancel().catch(() => undefined)
                  load()
                }
              }}
            >
              I found it myself — cancel and refund
            </button>
          )}
        </>
      )}
      <button
        className="mt-auto text-[11px] text-mute underline"
        onClick={() => {
          ownerToken.clear()
          setHas(false)
          setV(null)
        }}
      >
        Report another item
      </button>
    </Shell>
  )
}
