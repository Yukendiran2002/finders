// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import type { ReactNode } from 'react'

export function Shell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div className={`mx-auto flex min-h-full flex-col gap-4 p-4 ${wide ? 'max-w-[1500px]' : 'max-w-md'}`}>
      <header className="flex items-center gap-2 pt-1">
        <a href="/" className="flex items-center gap-2">
          <Logo />
          <span className="text-lg font-semibold tracking-tight">Finders</span>
        </a>
        <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">PAYPAL SANDBOX</span>
      </header>
      {children}
    </div>
  )
}

export function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <circle cx="13" cy="13" r="9" fill="none" stroke="#f59e0b" strokeWidth="3.5" />
      <path d="M20 20 L28 28" stroke="#f59e0b" strokeWidth="4" strokeLinecap="round" />
      <path d="M9 13.5 l3 3 l5.5 -6" fill="none" stroke="#0ea5e9" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`card p-4 ${className}`}>{children}</div>
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <div className="label mb-1">{label}</div>
      {children}
      {hint && <div className="mt-1 text-xs text-mute">{hint}</div>}
    </label>
  )
}

export function Steps({ step }: { step: number }) {
  const names = ['Reported', 'Reward paid', 'Match found', 'Both checked', 'Returned']
  return (
    <ol className="flex items-center gap-1">
      {names.map((n, i) => (
        <li key={n} className="flex flex-1 flex-col items-center gap-1 text-center">
          <span className={`h-1.5 w-full rounded-full ${i <= step ? 'bg-brand' : 'bg-line'}`} />
          <span className={`text-[10px] leading-tight ${i <= step ? 'text-slate-200' : 'text-mute'}`}>{n}</span>
        </li>
      ))}
    </ol>
  )
}

export function Pill({ tone, children }: { tone: 'good' | 'warn' | 'bad' | 'info' | 'brand'; children: ReactNode }) {
  const c = {
    good: 'bg-green-500/15 text-green-300',
    warn: 'bg-amber-500/15 text-amber-300',
    bad: 'bg-red-500/15 text-red-300',
    info: 'bg-slate-500/20 text-slate-300',
    brand: 'bg-sky-500/15 text-sky-300',
  }[tone]
  return <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${c}`}>{children}</span>
}

export function Err({ text }: { text: string }) {
  return text ? <p className="rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-300">{text}</p> : null
}

export function MoneyStrip({ items }: { items: [string, string, string?][] }) {
  return (
    <div className="card grid divide-x divide-line" style={{ gridTemplateColumns: `repeat(${items.length}, minmax(0,1fr))` }}>
      {items.map(([k, v, c]) => (
        <div key={k} className="p-3 text-center">
          <div className="label">{k}</div>
          <div className={`mono mt-0.5 text-lg font-semibold ${c ?? 'text-slate-100'}`}>{v}</div>
        </div>
      ))}
    </div>
  )
}

type IconName = 'search' | 'camera' | 'cash' | 'party'
export function Icon({ name, size = 28, className = '' }: { name: IconName; size?: number; className?: string }) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, className }
  switch (name) {
    case 'search':
      return (
        <svg {...common} stroke="#f59e0b" aria-hidden>
          <circle cx="10.5" cy="10.5" r="6.5" />
          <path d="M15.5 15.5 21 21" />
        </svg>
      )
    case 'camera':
      return (
        <svg {...common} stroke="#0ea5e9" aria-hidden>
          <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
          <circle cx="12" cy="13" r="3.5" />
        </svg>
      )
    case 'cash':
      return (
        <svg {...common} stroke="#22c55e" aria-hidden>
          <rect x="3" y="6" width="18" height="12" rx="2" />
          <circle cx="12" cy="12" r="2.5" />
          <path d="M6.5 9.5v5M17.5 9.5v5" />
        </svg>
      )
    case 'party':
      return (
        <svg {...common} stroke="#22c55e" aria-hidden>
          <path d="M5 12.5 10 17.5 19.5 7" />
          <circle cx="12" cy="12" r="10" />
        </svg>
      )
  }
}
