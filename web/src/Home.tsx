// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { useEffect, useState } from 'react'
import { ago, finderToken, ownerToken, photoUrl, pub, type BoardItem } from './api'
import { Icon, Shell } from './ui'

export function Home() {
  const [board, setBoard] = useState<BoardItem[]>([])
  useEffect(() => {
    pub.board().then(setBoard).catch(() => undefined)
  }, [])
  return (
    <Shell>
      <section className="pt-2">
        <h1 className="text-2xl font-semibold leading-tight">Lost something? Post a reward.<br />Found something? Get paid for returning it.</h1>
        <p className="mt-2 text-sm text-mute">
          The reward waits with PayPal. AI matches what was found to what was lost, and asks each side a question only the real finder and the real owner can answer. The finder is paid the moment the owner says it's back.
        </p>
      </section>
      <div className="grid grid-cols-2 gap-3">
        <a href="/lost" className="card block p-4 hover:border-brand">
          <Icon name="search" />
          <div className="mt-2 font-semibold">I lost something</div>
          <div className="text-xs text-mute">{ownerToken.get() ? 'Open my case' : 'Post a reward'}</div>
        </a>
        <a href="/found" className="card block p-4 hover:border-brand">
          <Icon name="camera" />
          <div className="mt-2 font-semibold">I found something</div>
          <div className="text-xs text-mute">{finderToken.get() ? 'Open my find' : 'Snap it, get matched'}</div>
        </a>
      </div>
      <section>
        <div className="label mb-2">Open rewards</div>
        <div className="space-y-2">
          {board.length === 0 && <p className="text-sm text-mute">No open rewards right now.</p>}
          {board.map((b) => (
            <div key={b.id} className="card flex gap-3 p-3">
              {b.photo_id ? (
                <img src={photoUrl(b.photo_id)} alt="" className="h-14 w-14 shrink-0 rounded-lg object-cover" />
              ) : (
                <div className="flex h-14 w-14 shrink-0 items-center justify-center rounded-lg bg-ink text-xl">?</div>
              )}
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <div className="truncate font-medium">{b.title}</div>
                  <div className="mono shrink-0 font-semibold text-amber-300">${b.reward}</div>
                </div>
                <div className="truncate text-xs text-mute">
                  {b.place} · lost {ago(b.lost_at)}
                </div>
                {b.sample && <div className="mt-1 text-[10px] uppercase tracking-wide text-mute">sample report</div>}
              </div>
            </div>
          ))}
        </div>
      </section>
      <footer className="mt-auto pt-4 text-center text-[11px] text-mute">
        PayPal sandbox demo — no real money moves. <a className="underline" href="/ops">Operator console</a>
      </footer>
    </Shell>
  )
}
