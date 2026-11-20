import { useEffect, useState } from 'react'

import { formatRs, milliToString } from '../../../core/money'
import type { AppState, InvoiceView } from '../../../core/types'

export function Receipt({ invoice, state, onNext, nextLabel = 'Next bill' }: { invoice: InvoiceView; state: AppState; onNext: () => void; nextLabel?: string }) {
  const b = state.branding
  const [printError, setPrintError] = useState<string | null>(null)
  const print = () => {
    setPrintError(null)
    window.ordexa.printReceipt().catch((e: Error) => setPrintError(e.message))
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.key.toLowerCase() === 'p') {
        e.preventDefault()
        print()
      }
    }
    window.addEventListener('keydown', onKey)
    // Auto-print a freshly saved bill when this PC is set up for it.
    if (nextLabel === 'Next bill') void window.ordexa.getPrinter().then((p) => p.auto && p.name && print())
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return (
    <div className="receipt-page">
      <div className="receipt card" id="receipt">
        <div className="r-head">
          {b?.logo_data_url && <img src={b.logo_data_url} alt="" className="r-logo" />}
          <div className="r-name">{b?.display_name}</div>
          {b?.address && <div>{b.address}</div>}
          {b?.phone && <div>{b.phone}</div>}
        </div>
        <div className="r-meta">
          <span>Bill {invoice.number}</span>
          <span>{new Date(invoice.issued_at).toLocaleString('en-PK')}</span>
        </div>
        <div className="r-meta">
          <span>{invoice.customer_name}</span>
          <span>By {state.user?.full_name}</span>
        </div>
        <table className="r-lines">
          <tbody>
            {invoice.lines.map((l) => (
              <tr key={l.line_no}>
                <td>
                  {l.product_name}
                  <div className="r-small">
                    {l.batch_no && `B# ${l.batch_no} Exp ${l.expiry_date} · `}
                    {milliToString(l.qty)} x {formatRs(l.unit_price)}
                    {l.bonus > 0 && ` + ${milliToString(l.bonus)} bonus`}
                  </div>
                </td>
                <td className="num">{formatRs(l.line_total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="r-total">
          {invoice.discount > 0 && (
            <div>
              <span>Discount</span>
              <span>-{formatRs(invoice.discount)}</span>
            </div>
          )}
          <div className="big">
            <span>Total</span>
            <span>{formatRs(invoice.total)}</span>
          </div>
          {invoice.paid > 0 && (
            <div>
              <span>Paid</span>
              <span>{formatRs(invoice.paid)}</span>
            </div>
          )}
        </div>
        <div className="r-foot">
          {b?.license_text}
          <br />
          {b?.powered_by}
        </div>
      </div>
      <div className="receipt-actions no-print">
        <div className="alert ok">Bill {invoice.number} {nextLabel === 'Next bill' ? 'saved' : ''}.</div>
        {printError && <div className="alert error">{printError}</div>}
        <button className="btn" onClick={print}>
          Print <kbd>Ctrl+P</kbd>
        </button>
        <button className="btn primary" autoFocus onClick={onNext}>
          {nextLabel} <kbd>Enter</kbd>
        </button>
      </div>
    </div>
  )
}
