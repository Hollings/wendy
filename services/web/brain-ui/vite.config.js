import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api/brain': { target: process.env.BRAIN_API_URL || 'http://127.0.0.1:8000' },
      '/ws/brain': { target: process.env.BRAIN_API_URL || 'http://127.0.0.1:8000', ws: true },
    },
  },
  build: {
    // Local dev: output goes directly to where FastAPI serves it.
    // Docker: Dockerfile overrides with --outDir ./dist then COPYs it.
    outDir: '../static/brain',
    emptyOutDir: true,
  },
})
