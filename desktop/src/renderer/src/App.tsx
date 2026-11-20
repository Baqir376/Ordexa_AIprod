import { useEffect, useState } from 'react'

import type { AppState } from '../../core/types'
import { ordexa } from './bridge'
import { Activation } from './screens/Activation'
import { Login } from './screens/Login'
import { Shell } from './screens/Shell'

export function App() {
  const [state, setState] = useState<AppState | null>(null)

  useEffect(() => {
    void ordexa().state().then(setState)
    return ordexa().onState(setState)
  }, [])

  useEffect(() => {
    const color = state?.branding?.primary_color
    if (color) document.documentElement.style.setProperty('--brand', color)
    document.title = state?.branding ? `${state.branding.display_name} · Ordexa ERP` : 'Ordexa ERP'
  }, [state?.branding])

  if (!state) return <div className="splash">Loading…</div>
  if (!state.activated) return <Activation onDone={setState} />
  if (!state.user) return <Login state={state} onDone={() => void ordexa().state().then(setState)} />
  return <Shell state={state} refresh={() => void ordexa().state().then(setState)} />
}
