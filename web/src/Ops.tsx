// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { AllCommunityModule, ModuleRegistry, colorSchemeDarkBlue, themeQuartz, type ColDef } from 'ag-grid-community'
import { AgGridReact } from 'ag-grid-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, opKey, ops, plain, type Audit, type FeedEvent, type OpsCase, type OpsMatch, type OpsOverview } from './api'
import { Logo, MoneyStrip, Pill } from './ui'

ModuleRegistry.registerModules([AllCommunityModule])
const theme = themeQuartz.withPart(colorSchemeDarkBlue).withParams({
  backgroundColor: '#111a2e',
  headerBackgroundColor: '#0d1526',
  borderColor: '#1f2b45',
  accentColor: '#f59e0b',
  fontSize: 12,
  rowHeight: 34,
  headerHeight: 34,
})

const s = (v: unknown) => (v == null ? '' : String(v))
function line(e: FeedEvent): { tone: 'good' | 'warn' | 'bad' | 'info' | 'brand'; text: string; sub?: string } | null {
  const d = e.data
  switch (e.kind) {
    case 'lost_reported':
      return { tone: 'info', text: `Lost: ${s(d.title)} at ${s(d.place)} · $${s(d.reward)} reward${d.sample ? ' (sample)' : ''}` }
    case 'reward_paid':
      return { tone: 'brand', text: `PayPal captured $${s(d.reward)} reward + $${s(d.fee)} fee`, sub: `capture ${s(d.capture_id)}` }
    case 'found_reported':
      return { tone: 'info', text: `Found at ${s(d.place)}: ${s((d.attributes as { summary?: string })?.summary)}` }
    case 'match_ranked':
      return { tone: 'info', text: `${s(d.brain)} compared the find with ${s(d.considered)} open cases → ${s(d.proposed)} proposed` }
    case 'match_none':
      return { tone: 'info', text: `No plausible case for a find (${s(d.considered)} in the time window)` }
    case 'match_proposed':
      return { tone: 'good', text: `Match proposed (${s(d.score)}%)`, sub: (d.reasons as string[] | undefined)?.join(' · ') }
    case 'finder_asked':
      return { tone: 'info', text: `Finder asked: “${s(d.question)}”` }
    case 'finder_checked':
      return { tone: d.verdict === 'yes' ? 'good' : d.verdict === 'no' ? 'bad' : 'warn', text: `Finder's answer: ${s(d.verdict)}`, sub: s(d.reason) }
    case 'owner_asked':
      return { tone: 'info', text: `Owner asked: “${s(d.question)}”` }
    case 'owner_checked':
      return { tone: d.status === 'VERIFIED' ? 'good' : 'warn', text: `Owner's answer: ${s(d.verdict)} → ${s(d.status)}`, sub: [s(d.reason), ...((d.flags as string[]) ?? [])].filter(Boolean).join(' · ') }
    case 'match_reviewed':
      return { tone: d.approve ? 'good' : 'bad', text: `Operator ${d.approve ? 'approved' : 'rejected'} a match` }
    case 'handover_code_issued':
      return { tone: 'info', text: `Owner issued a handover code for $${s(d.reward)}` }
    case 'handover_code_wrong':
      return { tone: 'warn', text: 'Wrong handover code entered' }
    case 'handed_over':
      return { tone: 'good', text: `Handed over · $${s(d.reward)} reward released` }
    case 'payout_submitted':
      return { tone: 'brand', text: `PayPal Payout $${s(d.amount)} to ${s(d.receiver)}`, sub: `batch ${s(d.payout_batch_id)}` }
    case 'payout_checked':
      return { tone: d.state === 'DONE' ? 'good' : 'warn', text: `Payout item ${s(d.item_status)} → ${s(d.state)}` }
    case 'payout_uncertain':
    case 'refund_uncertain':
      return { tone: 'warn', text: 'PayPal did not answer — will check before resending with the same id' }
    case 'payout_already_submitted':
      return { tone: 'good', text: 'Payout confirmed by PayPal duplicate check — nobody paid twice' }
    case 'payout_refused':
    case 'refund_refused':
      return { tone: 'bad', text: `PayPal refused: ${s(d.name)}`, sub: s(d.issues) }
    case 'refunded':
      return { tone: 'brand', text: `Refunded $${s(d.amount)} to the owner`, sub: `refund ${s(d.refund_id)}` }
    case 'case_closed':
      return { tone: 'info', text: `Case closed (${s(d.reason)}) · refund $${s(d.refund)}` }
    case 'audit_completed':
      return { tone: d.ok ? 'good' : 'bad', text: d.ok ? "Audit: all checks match PayPal's records" : 'Audit: mismatch with PayPal' }
    case 'money_question':
      return { tone: 'info', text: `Asked: “${s(d.question)}”`, sub: plain(s(d.answer)) }
    default:
      return null
  }
}
const toneBorder = { good: 'border-l-green-500', warn: 'border-l-amber-400', bad: 'border-l-red-500', info: 'border-l-slate-500', brand: 'border-l-sky-400' }

function Gate({ onOk }: { onOk: () => void }) {
  const [k, setK] = useState('')
  return (
    <div className="flex h-full items-center justify-center">
      <form
        className="card w-80 space-y-3 p-6"
        onSubmit={(e) => {
          e.preventDefault()
          opKey.set(k)
          onOk()
        }}
      >
        <div className="font-semibold">Finders operator console</div>
        <input className="input" type="password" value={k} onChange={(e) => setK(e.target.value)} placeholder="Operator key" autoFocus />
        <button className="btn-primary w-full">Open</button>
      </form>
    </div>
  )
}

export function Ops() {
  const [data, setData] = useState<OpsOverview | null>(null)
  const [need, setNeed] = useState(false)
  const [feed, setFeed] = useState<FeedEvent[]>([])
  const [sel, setSel] = useState<OpsCase | null>(null)
  const [audit, setAudit] = useState<Audit | null>(null)
  const [busy, setBusy] = useState(false)
  const [q, setQ] = useState('')
  const [ans, setAns] = useState('')
  const end = useRef<HTMLDivElement>(null)

  const load = useCallback(async () => {
    try {
      const d = await ops.overview()
      setData(d)
      setNeed(false)
      setSel((cur) => (cur ? d.cases.find((c) => c.id === cur.id) ?? null : cur))
    } catch (x) {
      if (x instanceof ApiError && x.status === 401) setNeed(true)
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 4000)
    const es = ops.events()
    const seen = new Set<number>()
    es.onmessage = () => undefined
    const handler = (ev: MessageEvent) => {
      const e = JSON.parse(ev.data) as FeedEvent
      if (seen.has(e.seq)) return
      seen.add(e.seq)
      setFeed((xs) => [...xs.slice(-300), e])
      load()
    }
    const kinds = [
      'lost_reported', 'reward_paid', 'found_reported', 'match_ranked', 'match_none', 'match_proposed', 'finder_asked', 'finder_checked', 'owner_asked',
      'owner_checked', 'match_reviewed', 'handover_code_issued', 'handover_code_wrong', 'handed_over', 'payout_submitted', 'payout_checked', 'payout_uncertain',
      'refund_uncertain', 'payout_already_submitted', 'payout_refused', 'refund_refused', 'refunded', 'case_closed', 'audit_completed', 'money_question',
    ]
    kinds.forEach((k) => es.addEventListener(k, handler as EventListener))
    return () => {
      es.close()
      clearInterval(t)
    }
  }, [load])
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'nearest' })
  }, [feed.length])

  const caseCols = useMemo<ColDef<OpsCase>[]>(
    () => [
      { field: 'title', flex: 2, minWidth: 160 },
      { field: 'status', width: 110, cellClass: (p) => (p.value === 'RETURNED' ? 'text-green-300' : p.value === 'MATCHED' ? 'text-sky-300' : p.value === 'OPEN' ? 'text-amber-300' : 'text-slate-400') },
      { field: 'place', flex: 1, minWidth: 130 },
      { field: 'reward', width: 90, valueFormatter: (p) => `$${p.value}` },
      { field: 'held', headerName: 'Waiting', width: 90, valueFormatter: (p) => `$${p.value}` },
      { field: 'paid_out', headerName: 'To finder', width: 95, valueFormatter: (p) => `$${p.value}` },
      { field: 'refunded', width: 95, valueFormatter: (p) => `$${p.value}` },
      { field: 'matches', width: 85 },
      { field: 'capture_id', headerName: 'PayPal capture', flex: 1, minWidth: 150, cellClass: 'mono' },
      { field: 'sample', width: 80, cellDataType: 'text', valueFormatter: (p) => (p.value ? 'sample' : '') },
    ],
    [],
  )
  const matchCols = useMemo<ColDef<OpsMatch>[]>(
    () => [
      { headerName: 'Case', flex: 1, minWidth: 140, valueGetter: (p) => data?.cases.find((c) => c.id === p.data?.lost_id)?.title ?? '' },
      { field: 'score', width: 80, valueFormatter: (p) => `${p.value}%` },
      { field: 'status', width: 120, cellClass: (p) => (p.value === 'REVIEW' ? 'text-amber-300 font-semibold' : p.value === 'REJECTED' ? 'text-red-300' : p.value === 'HANDED_OVER' ? 'text-green-300' : '') },
      { field: 'finder_verdict', headerName: 'Finder', width: 85 },
      { field: 'owner_verdict', headerName: 'Owner', width: 85 },
      { headerName: 'Flags / reasons', flex: 2, minWidth: 220, valueGetter: (p) => [...(p.data?.flags ?? []), ...(p.data?.reasons ?? [])].join(' · ') },
      {
        headerName: 'Review',
        width: 150,
        cellRenderer: (p: { data: OpsMatch }) =>
          p.data.status === 'REVIEW' ? (
            <span className="flex gap-1 pt-1">
              <button className="rounded bg-green-600 px-2 text-xs text-white" onClick={() => ops.review(p.data.id, true).then(load)}>
                Approve
              </button>
              <button className="rounded bg-red-600 px-2 text-xs text-white" onClick={() => ops.review(p.data.id, false).then(load)}>
                Reject
              </button>
            </span>
          ) : null,
      },
    ],
    [data, load],
  )

  if (need) return <Gate onOk={load} />
  if (!data) return <div className="p-8 text-mute">Loading…</div>
  const sum = (k: 'held' | 'paid_out' | 'refunded' | 'paid') => data.cases.reduce((a, c) => a + Number(c[k]), 0).toFixed(2)
  const fees = data.cases.filter((c) => c.capture_id).reduce((a, c) => a + Number(c.fee), 0).toFixed(2)
  const review = data.matches.filter((m) => m.status === 'REVIEW').length

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-line px-4 py-2.5">
        <Logo />
        <span className="font-semibold">Finders · operator console</span>
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">PAYPAL SANDBOX</span>
        {review > 0 && <Pill tone="warn">{review} to review</Pill>}
        <button className="btn-ghost ml-auto py-1.5" onClick={() => ops.housekeeping().then(load)}>
          Run expiry &amp; payout checks
        </button>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-[1fr_380px] gap-4 p-4">
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <MoneyStrip
            items={[
              ['Owners paid', `$${sum('paid')}`],
              ['Rewards waiting', `$${sum('held')}`, 'text-amber-300'],
              ['Paid to finders', `$${sum('paid_out')}`, 'text-green-300'],
              ['Refunded', `$${sum('refunded')}`, 'text-sky-300'],
              ['Fees', `$${fees}`, 'text-slate-300'],
            ]}
          />
          <div className="card overflow-hidden">
            <div className="label px-4 pt-3">Cases · click one to audit it against PayPal</div>
            <div className="h-[300px] p-2">
              <AgGridReact<OpsCase> theme={theme} rowData={data.cases} columnDefs={caseCols} getRowId={(p) => p.data.id} rowSelection={{ mode: 'singleRow', enableClickSelection: true, checkboxes: false }} onRowClicked={(e) => { setSel(e.data ?? null); setAudit(null); setAns('') }} />
            </div>
          </div>
          <div className="card overflow-hidden">
            <div className="label px-4 pt-3">Matches · AI scores, both checks, human review</div>
            <div className="h-[260px] p-2">
              <AgGridReact<OpsMatch> theme={theme} rowData={data.matches} columnDefs={matchCols} getRowId={(p) => p.data.id} />
            </div>
          </div>
          {sel && (
            <div className="card space-y-3 p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="label">Check with PayPal · {sel.title}</div>
                  <div className="text-xs text-mute">Owner's payment and refunds read through the PayPal Agent Toolkit; the finder's payout through the Payouts API.</div>
                </div>
                <button
                  className="btn-ghost shrink-0"
                  disabled={busy || !sel.order_id}
                  onClick={async () => {
                    setBusy(true)
                    try {
                      setAudit(await ops.audit(sel.id))
                    } finally {
                      setBusy(false)
                    }
                  }}
                >
                  {busy ? 'Reading PayPal…' : 'Run audit'}
                </button>
              </div>
              {audit && (
                <table className="w-full overflow-hidden rounded-lg border border-line text-xs">
                  <tbody>
                    {audit.checks.map((c, i) => (
                      <tr key={i} className="border-t border-line">
                        <td className={`px-3 py-1.5 ${c.ok ? 'text-green-400' : 'text-red-400'}`}>{c.ok ? '✓' : '✗'}</td>
                        <td className="px-3 py-1.5">{c.check}</td>
                        <td className="mono px-3 py-1.5 text-slate-300">{c.finders}</td>
                        <td className="mono px-3 py-1.5 text-slate-300">{c.paypal}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <form
                className="flex gap-2"
                onSubmit={async (e) => {
                  e.preventDefault()
                  setBusy(true)
                  try {
                    setAns(plain((await ops.ask(sel.id, q)).answer))
                  } catch (x) {
                    setAns((x as Error).message)
                  } finally {
                    setBusy(false)
                  }
                }}
              >
                <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask Gemini about this case's money" />
                <button className="btn-primary shrink-0" disabled={busy || q.trim().length < 2}>
                  Ask
                </button>
              </form>
              {ans && <p className="whitespace-pre-line text-sm leading-relaxed">{ans}</p>}
            </div>
          )}
        </div>
        <div className="card flex min-h-0 flex-col">
          <div className="label border-b border-line px-4 py-3">Live feed</div>
          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
            {feed.map((e) => {
              const l = line(e)
              if (!l) return null
              return (
                <div key={e.seq} className={`rounded-md border border-line border-l-4 bg-ink/60 px-3 py-2 ${toneBorder[l.tone]}`}>
                  <div className="flex justify-between gap-2">
                    <div className="text-sm">{l.text}</div>
                    <div className="shrink-0 text-[10px] text-mute">{new Date(e.at * 1000).toLocaleTimeString()}</div>
                  </div>
                  {l.sub && <div className="mt-0.5 text-xs text-mute">{l.sub}</div>}
                </div>
              )
            })}
            <div ref={end} />
          </div>
        </div>
      </div>
    </div>
  )
}
