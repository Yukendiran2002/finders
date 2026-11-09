// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Liceron
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Finder } from './Finder'
import { Home } from './Home'
import { Ops } from './Ops'
import { Owner } from './Owner'
import './index.css'

const path = location.pathname.replace(/\/+$/, '') || '/'
const Page = { '/lost': Owner, '/found': Finder, '/ops': Ops }[path] ?? Home

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Page />
  </StrictMode>,
)
