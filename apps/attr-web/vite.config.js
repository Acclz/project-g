import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期把 /api 代理到本机 FastAPI 服务（services/attr-api），避免 CORS 与环境变量两套配置
const API_TARGET = process.env.ATTR_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    // ECharts 体积大，单独切一个 vendor 包，避免主包过大（不影响功能，只是构建更干净）
    rollupOptions: {
      output: {
        manualChunks: { echarts: ['echarts'], vue: ['vue', 'vue-router', 'pinia'] },
      },
    },
  },
})
