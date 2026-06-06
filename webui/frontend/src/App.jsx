import { useState } from 'react'
import SearchPage from './pages/SearchPage.jsx'
import UploadPage from './pages/UploadPage.jsx'
import StatsPage from './pages/StatsPage.jsx'

const TABS = [
  { id: 'search', label: '🔍 Cari Dokumen' },
  { id: 'upload', label: '⬆️ Upload & Filing' },
  { id: 'stats', label: '📊 Statistik' },
]

export default function App() {
  const [tab, setTab] = useState('search')
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Arsip <strong>KS</strong></div>
        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'tab active' : 'tab'}
                    onClick={() => setTab(t.id)}>{t.label}</button>
          ))}
        </nav>
      </header>
      <main className="content">
        {tab === 'search' && <SearchPage />}
        {tab === 'upload' && <UploadPage />}
        {tab === 'stats' && <StatsPage />}
      </main>
    </div>
  )
}
