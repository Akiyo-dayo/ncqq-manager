import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/manual': 'http://127.0.0.1:8000',
      '/ws': {
        target: 'http://127.0.0.1:8000',
        ws: true,
      },
    }
  }
})
