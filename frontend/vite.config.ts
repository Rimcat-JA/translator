import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

const participant = process.env.TRANSLATOR_SURFACE === 'participant';
const target = process.env.TRANSLATOR_API_ORIGIN || `http://127.0.0.1:${participant ? 8000 : 8765}`;
export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: participant ? 5174 : 5173, strictPort: true,
    proxy: participant ? {
      '/api/v1': { target, changeOrigin: false }, '/ws/v1': { target, ws: true, changeOrigin: false },
    } : {
      '/api/local': { target, changeOrigin: false }, '/ws/local': { target, ws: true, changeOrigin: false },
    },
  },
  test: { environment: 'jsdom', include: ['src/**/*.test.{ts,tsx}'], setupFiles: ['src/test-setup.ts'] },
});
