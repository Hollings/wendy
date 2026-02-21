import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    // Local dev: output goes directly to where FastAPI serves it.
    // Docker: Dockerfile overrides with --outDir ./dist then COPYs it.
    outDir: '../static/brain',
    emptyOutDir: true,
  },
})
