import { useEffect, useState } from 'react'
import { getStats } from '../api.js'

export default function StatsPage() {
  const [s, setS] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { getStats().then(setS).catch((e) => setErr(e.message)) }, [])

  if (err) return <div className="error">⚠ {err}</div>
  if (!s) return <div className="muted">memuat…</div>

  return (
    <div className="stats">
      <div className="kpis">
        <Kpi n={s.total.toLocaleString()} label="total dokumen" />
        <Kpi n={s.with_counterparty.toLocaleString()} label="punya counterparty" />
        <Kpi n={s.with_doc_number.toLocaleString()} label="punya no. dokumen" />
        <Kpi n={s.with_expire.toLocaleString()} label="punya tgl kadaluarsa" />
      </div>
      <div className="cols">
        <Bars title="Per Departemen" data={s.by_department} />
        <Bars title="Per Perusahaan" data={s.by_company} />
      </div>
      <Bars title="Per Proyek (top 20)" data={s.by_project} />
    </div>
  )
}

const Kpi = ({ n, label }) => (
  <div className="kpi"><div className="kpi-n">{n}</div><div className="kpi-l">{label}</div></div>
)

function Bars({ title, data }) {
  const max = Math.max(1, ...data.map((d) => d.count))
  return (
    <div className="bars">
      <h3>{title}</h3>
      {data.map((d) => (
        <div className="bar-row" key={d.name}>
          <div className="bar-label" title={d.name}>{d.name}</div>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${(d.count / max) * 100}%` }} /></div>
          <div className="bar-num">{d.count.toLocaleString()}</div>
        </div>
      ))}
    </div>
  )
}
