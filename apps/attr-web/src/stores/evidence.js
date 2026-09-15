/**
 * 证据 store（右栏）：假设 / 证据链 / 下钻透视表 / 推演曲线。
 *
 * 全部来自会话已落库的记录（`/hypotheses`、`/evidence`、`/drilldowns`、`/whatifs`），
 * 因此"页面看到的"与"报告里写的"是同一份数据。
 */

import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useEvidenceStore = defineStore('evidence', {
  state: () => ({
    sessionId: null,
    hypotheses: [],
    evidence: [],
    drilldowns: [],
    whatifs: [],
    factors: [],
    loading: false,
    error: '',
  }),
  getters: {
    verified(state) {
      return state.hypotheses.filter((item) => item.status === 'verified')
    },
    pivots(state) {
      return state.drilldowns
        .map((item) => item.payload)
        .filter((payload) => payload.rows && !payload.summary)
    },
    drilldownSummaries(state) {
      return state.drilldowns
        .map((item) => item.payload)
        .filter((payload) => payload.summary)
    },
    whatifSummaries(state) {
      return state.whatifs.map((item) => item.payload).filter((payload) => payload.summary)
    },
  },
  actions: {
    async load(sessionId) {
      this.sessionId = sessionId
      this.error = ''
      if (!sessionId) {
        this.hypotheses = []
        this.evidence = []
        this.drilldowns = []
        this.whatifs = []
        this.factors = []
        return
      }
      this.loading = true
      try {
        const [hypotheses, evidence, drilldowns, whatifs, factors] = await Promise.all([
          api.hypotheses(sessionId),
          api.evidence(sessionId),
          api.drilldowns(sessionId),
          api.whatifs(sessionId),
          api.factors(sessionId),
        ])
        this.hypotheses = hypotheses.items
        this.evidence = evidence.items
        this.drilldowns = drilldowns.items
        this.whatifs = whatifs.items
        this.factors = factors.items
      } catch (error) {
        this.error = error.message
      } finally {
        this.loading = false
      }
    },
    async runWhatIf(body) {
      const result = await api.whatif(this.sessionId, body)
      await this.load(this.sessionId)
      return result
    },
  },
})
