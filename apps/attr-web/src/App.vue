<script setup>
import { useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const tabs = [
  { name: 'dashboard', label: '异动大盘', path: '/dashboard' },
  { name: 'workbench', label: '归因工作台', path: '/workbench' },
  { name: 'events', label: '事件日历', path: '/events' },
  { name: 'metrics', label: '指标字典', path: '/metrics' },
  { name: 'reports', label: '报告归档', path: '/reports' },
]
const currentTitle = computed(() => route.meta?.title ?? '经营归因分析系统')
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <strong>经营归因分析系统</strong>
        <span class="muted">异动 → 归因 → 交付 · 数值只来自数仓与算法包</span>
      </div>
      <nav class="tabs">
        <RouterLink
          v-for="tab in tabs"
          :key="tab.name"
          :to="tab.path"
          class="tab"
          active-class="tab--active"
          >{{ tab.label }}</RouterLink
        >
      </nav>
      <div class="page-title muted">{{ currentTitle }}</div>
    </header>
    <main class="page">
      <RouterView />
    </main>
  </div>
</template>
