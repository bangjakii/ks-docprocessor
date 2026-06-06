import { useEffect, useState } from 'react'
import { getFilters, search, fileUrl, extractUrl } from '../api.js'

const isPdf = (name) => /\.pdf$/i.test(name || '')

export default function SearchPage() {
  const [filters, setFilters] = useState({ companies: [], departments: [], projects: [] })
  const [q, setQ] = useState('')
  const [f, setF] = useState({ company: '', department: '', project: '' })
  const [hits, setHits] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => { getFilters().then(setFilters).catch(() => {}) }, [])

  const go = async (e) => {
    e?.preventDefault()
    if (!q.trim()) return
    setLoading(true); setErr(''); setHits(null)
    try {
      const r = await search(q, f)
      setHits(r.hits)
    } catch (e) { setErr(e.message) }
    setLoading(false)
  }

  return (
    <div>
      <form className="searchbar" onSubmit={go}>
        <input className="q" placeholder="cari… mis. 'faktur pajak PT PAL' atau 'bank garansi proyek tug boat'"
               value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        <button className="btn primary" disabled={loading}>{loading ? '…' : 'Cari'}</button>
      </form>

      <div className="filters">
        <Select label="Perusahaan" value={f.company} opts={filters.companies}
                onChange={(v) => setF({ ...f, company: v })} />
        <Select label="Departemen" value={f.department} opts={filters.departments}
                onChange={(v) => setF({ ...f, department: v })} />
        <Select label="Proyek" value={f.project} opts={filters.projects}
                onChange={(v) => setF({ ...f, project: v })} />
      </div>

      {err && <div className="error">⚠ {err}</div>}
      {hits && <div className="muted">{hits.length} hasil</div>}

      <div className="results">
        {hits?.map((h, i) => (
          <div className="card" key={i}>
            <div className="card-head">
              <div className="title">{h.doc_name || h.filename}</div>
              <span className="score">{(h.score * 100).toFixed(0)}%</span>
            </div>
            <div className="meta">
              <span className="pill">{h.company}</span>
              <span className="pill">{h.department}</span>
              {h.project && <span className="pill alt">{h.project}</span>}
              {h.subfolder && <span className="pill ghost">{h.subfolder}</span>}
              {h.page !== '' && <span className="pill ghost">hal {h.page}</span>}
            </div>
            <div className="snippet">{h.snippet}…</div>
            <div className="card-foot">
              <div className="badges">
                {h.counterparty && <span className="badge">↔ {h.counterparty}</span>}
                {h.doc_number && <span className="badge">№ {h.doc_number}</span>}
                {h.expire_date && <span className="badge warn">exp {h.expire_date}</span>}
              </div>
              <div className="actions">
                {isPdf(h.filename) && Number(h.page) >= 1 && (
                  <a className="btn small primary" href={extractUrl(h.path, h.page)} target="_blank" rel="noreferrer"
                     title={`Ambil hanya halaman ${h.page} dari bundel`}>
                    ✂️ Potong hal {h.page}
                  </a>
                )}
                <a className="btn small" href={fileUrl(h.path, h.page)} target="_blank" rel="noreferrer">
                  Buka file asli ↗
                </a>
              </div>
            </div>
          </div>
        ))}
        {hits && hits.length === 0 && <div className="empty">Tidak ada hasil. Coba kata kunci lain / lepas filter.</div>}
      </div>
    </div>
  )
}

function Select({ label, value, opts, onChange }) {
  return (
    <label className="select">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">semua</option>
        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </label>
  )
}
