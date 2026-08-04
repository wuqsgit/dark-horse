import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',      // 允许外部访问
    port: 3000,
    allowedHosts: [
      '91fafafa.xyz',     // 允许你的域名
      '.91fafafa.xyz',    // 允许子域名（如 www.91fafafa.xyz）
      'localhost',
      '127.0.0.1'
    ]
  }
})
