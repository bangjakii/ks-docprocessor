import { useEffect, useState } from 'react'
import { getFilters, classify, confirm } from '../api.js'

const DEPTS = ['Legal', 'Marketing', 'Finance', 'Sales', 'Operasional', 'Engineering', 'HR', 'IT', 'Lainnya']

export default function UploadPage() {
  const [companies, setCompanies] = useState([])
  const [rows, setRows] = useState([])        // proposal yang bisa diedit
  const [busy, setBusy] = useState('')
  const [done, setDone] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => { getFilters().then((f) => setCompanies(f.companies)).catch(() => {}) }, [])

  const onPick = async (e) => {
    const files = [...e.target.files]
    if (!files.length) return
    setBusy(`Mengklasifikasi ${files.length} file… (Claude membaca isi, mohon tunggu)`)
    setErr(''); setDone(null)
    try {
      const r = await classify(files)
      setRows((prev) => [...prev, ...r.results.map((x) => ({ ...x, _edit: { ...x } }))])
    } catch (e) { setErr(e.message) }
    setBusy('')
    e.target.value = ''
  }

  const edit = (i, k, v) => setRows((rs) => rs.map((r, j) =>
    j === i ? { ...r, _edit: { ...r._edit, [k]: v } } : r))

  const remove = (i) => setRows((rs) => rs.filter((_, j) => j !== i))

  const submit = async () => {
    const items = rows.filter((r) => !r.error).map((r) => ({
      temp_id: r.temp_id, company: r._edit.company, counterparty: r._edit.counterparty,
      department: r._edit.department, scope: r._edit.scope, project: r._edit.project,
      subfolder: r._edit.subfolder, doc_number: r._edit.doc_number, expire_date: r._edit.expire_date,
    }))
    if (!items.length) return
    setBusy(`Memfile + meng-index ${items.length} dokumen…`); setErr('')
    try {
      const r = await confirm(items)
      setDone(r.results); setRows([])
    } catch (e) { setErr(e.message) }
    setBusy('')
  }

  return (
    <div>
      <div className="uploadzone">
        <input id="fp" type="file" multiple onChange={onPick}
               accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.jpg,.jpeg,.png,.tif,.tiff" hidden />
        <label htmlFor="fp" className="btn primary big">Pilih file (bisa banyak)</label>
        <p className="muted">PDF / Office / gambar. Claude bakal klasifikasi otomatis — lo review dulu sebelum difile.</p>
      </div>

      {busy && <div className="info">⏳ {busy}</div>}
      {err && <div className="error">⚠ {err}</div>}

      {rows.length > 0 && (
        <>
          <div className="rowsbar">
            <span>{rows.length} file siap di-review</span>
            <button className="btn primary" onClick={submit} disabled={!!busy}>Konfirmasi & File semua</button>
          </div>
          {rows.map((r, i) => (
            <div className={r.error ? 'card err' : 'card'} key={r.temp_id || i}>
              <div className="card-head">
                <div className="title">{r.filename}</div>
                <button className="btn small ghost" onClick={() => remove(i)}>hapus</button>
              </div>
              {r.error ? <div className="error">gagal: {r.error}</div> : (
                <>
                  {r.reason && <div className="reason">💡 {r.reason}</div>}
                  {r.should_split && <div className="warnline">⚠ Terdeteksi bundel campuran — v1 difile utuh.</div>}
                  <div className="grid">
                    <Field label="Perusahaan">
                      <select value={r._edit.company || ''} onChange={(e) => edit(i, 'company', e.target.value)}>
                        {companies.map((c) => <option key={c}>{c}</option>)}
                      </select>
                    </Field>
                    <Field label="Departemen">
                      <select value={r._edit.department || ''} onChange={(e) => edit(i, 'department', e.target.value)}>
                        {DEPTS.map((d) => <option key={d}>{d}</option>)}
                      </select>
                    </Field>
                    <Field label="Scope">
                      <select value={r._edit.scope || 'korporat'} onChange={(e) => edit(i, 'scope', e.target.value)}>
                        <option value="korporat">korporat</option>
                        <option value="proyek">proyek</option>
                      </select>
                    </Field>
                    <Field label="Proyek"><input value={r._edit.project || ''} onChange={(e) => edit(i, 'project', e.target.value)} placeholder="(kosong = korporat)" /></Field>
                    <Field label="Subfolder"><input value={r._edit.subfolder || ''} onChange={(e) => edit(i, 'subfolder', e.target.value)} /></Field>
                    <Field label="Counterparty"><input value={r._edit.counterparty || ''} onChange={(e) => edit(i, 'counterparty', e.target.value)} /></Field>
                    <Field label="No. dokumen"><input value={r._edit.doc_number || ''} onChange={(e) => edit(i, 'doc_number', e.target.value)} /></Field>
                    <Field label="Kadaluarsa"><input value={r._edit.expire_date || ''} onChange={(e) => edit(i, 'expire_date', e.target.value)} placeholder="YYYY-MM-DD" /></Field>
                  </div>
                </>
              )}
            </div>
          ))}
        </>
      )}

      {done && (
        <div className="results">
          <h3>Hasil filing</h3>
          {done.map((d, i) => (
            <div className={d.filed ? 'card ok' : 'card err'} key={i}>
              {d.filed ? (
                <>
                  <div className="title">✅ {d.relpath}/</div>
                  <div className="meta">
                    <span className="pill">{d.company}</span>
                    <span className="pill">{d.department}</span>
                    {d.project && <span className="pill alt">{d.project}</span>}
                    <span className="pill ghost">{d.indexed ? `indexed (${d.vectors} chunk)` : 'BELUM ter-index'}</span>
                  </div>
                  {d.index_error && <div className="error">index gagal: {d.index_error}</div>}
                </>
              ) : <div className="error">❌ {d.error}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const Field = ({ label, children }) => (
  <label className="field"><span>{label}</span>{children}</label>
)
