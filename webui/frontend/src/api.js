// Klien API ke backend FastAPI. Dev: di-proxy ke :8000 (lihat vite.config.js).
const J = async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`)
  return r.json()
}

export const getFilters = () => fetch('/api/filters').then(J)
export const getStats = () => fetch('/api/stats').then(J)

export const search = (q, { company, department, project, top_k = 15 } = {}) => {
  const p = new URLSearchParams({ q, top_k })
  if (company) p.set('company', company)
  if (department) p.set('department', department)
  if (project) p.set('project', project)
  return fetch(`/api/search?${p}`).then(J)
}

export const fileUrl = (path, page) =>
  `/api/file?path=${encodeURIComponent(path)}` + (page ? `#page=${page}` : '')

// Potong halaman dari bundel → PDF 1-lembar (±context)
export const extractUrl = (path, page, context = 0) =>
  `/api/extract?path=${encodeURIComponent(path)}&page=${page}&context=${context}`

export const classify = (files) => {
  const fd = new FormData()
  for (const f of files) fd.append('files', f)
  return fetch('/api/classify', { method: 'POST', body: fd }).then(J)
}

export const confirm = (items) =>
  fetch('/api/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(items),
  }).then(J)
