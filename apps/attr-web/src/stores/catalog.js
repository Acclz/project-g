/**
 * 目录 store：异动大盘、事件日历、指标字典三个页面共用的只读数据。
 *
 * 三个页面的交互彼此独立，因此放在一个 store 里按前缀区分，避免为每个页面写一套样板。
 */

import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useCatalogStore = defineStore('catalog', {
  state: () => ({
    domain: 'ecom',
    // 大盘
    anomalies: null,
    series: null,
    selectedMetric: 'gmv',
    // 事件
    events: [],
    matched: null,
    // 指标字典
    dictionary: null,
    scenarios: [],
    tree: null,
    validation: null,
    loading: false,
    error: '',
  }),
  actions: {
    async loadAnomalies(period) {
      this.loading = true
      this.error = ''
      try {
        this.anomalies = await api.anomalies(this.domain, period)
        const root = this.anomalies.items?.[0]?.code
        if (root) this.selectedMetric = this.anomalies.items.find((item) => item.level === 'composite')?.code ?? root
        await this.loadSeries()
      } catch (error) {
        this.error = error.message
      } finally {
        this.loading = false
      }
    },
    async loadSeries(days = 60) {
      if (!this.selectedMetric) return null
      this.series = await api.series(this.domain, this.selectedMetric, days)
      return this.series
    },
    async loadEvents(params = {}) {
      this.events = (await api.events(params)).items
      return this.events
    },
    async createEvent(body) {
      const created = await api.createEvent(body)
      await this.loadEvents()
      return created
    },
    async deleteEvent(id) {
      await api.deleteEvent(id)
      await this.loadEvents()
    },
    async matchEvents(params) {
      this.matched = await api.matchEvents(params)
      return this.matched
    },
    async loadDictionary() {
      // /api/metrics 返回 {items: {场景: 结构}, scenarios: [...]}；页面只关心结构那一层
      const payload = await api.metrics()
      this.dictionary = payload.items
      this.scenarios = payload.scenarios ?? Object.keys(payload.items ?? {})
      return this.dictionary
    },
    async loadTree(code) {
      this.tree = await api.metricTree(code, this.domain)
      return this.tree
    },
    async validate(code) {
      this.validation = await api.validateMetric(code, this.domain)
      return this.validation
    },
  },
})
