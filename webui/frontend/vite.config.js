import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: proxy /api ke backend FastAPI (port 8000). Build: file statis disajikan FastAPI.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
  build: { outDir: 'dist' },
})
