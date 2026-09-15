import { createRouter, createWebHashHistory } from 'vue-router'

import DashboardView from '@/views/DashboardView.vue'
import EventsView from '@/views/EventsView.vue'
import MetricsView from '@/views/MetricsView.vue'
import ReportsView from '@/views/ReportsView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

// 五页面（需求说明书 §4）：异动大盘 / 归因工作台 / 事件日历 / 指标字典 / 报告归档
const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', name: 'dashboard', component: DashboardView, meta: { title: '指标异动大盘' } },
  { path: '/workbench', name: 'workbench', component: WorkbenchView, meta: { title: '归因分析工作台' } },
  { path: '/events', name: 'events', component: EventsView, meta: { title: '业务事件日历' } },
  { path: '/metrics', name: 'metrics', component: MetricsView, meta: { title: '指标字典配置' } },
  { path: '/reports', name: 'reports', component: ReportsView, meta: { title: '报告归档与导出' } },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
