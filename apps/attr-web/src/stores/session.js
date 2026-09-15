/**
 * 会话上下文 store（技术规格 §8：会话 / 证据 / 报告三个 store，三栏联动靠派生，不互相调用）。
 *
 * 这里只保存"后端说了什么"：锁定上下文、状态、步骤流、各类记录。
 * 任何数值（贡献额、覆盖率、弹性区间、校验和）都直接来自接口响应，前端不做二次计算。
 */

import { defineStore } from 'pinia'
import { api, openStepStream } from '@/api/client'

export const useSessionStore = defineStore('session', {
  state: () => ({
    sessions: [],
    current: null,
    steps: [],
    events: [],
    streamClose: null,
    loading: false,
    error: '',
  }),
  getters: {
    lockedContext(state) {
      if (!state.current) return null
      return {
        metric: state.current.title,
        caliberVersion: state.current.caliber_version,
        base: state.current.base,
        current: state.current.current,
        slice: state.current.slice,
        digest: state.current.data_digest,
      }
    },
    isRunning(state) {
      return state.current?.status === 'analysing'
    },
  },
  actions: {
    async loadSessions(limit = 50) {
      const payload = await api.listSessions(limit)
      this.sessions = payload.items
      return payload.items
    },
    async createSession(body) {
      this.error = ''
      const session = await api.createSession(body)
      this.current = session
      this.steps = session.steps ?? []
      this.events = []
      await this.loadSessions()
      return session
    },
    async selectSession(id) {
      this.current = await api.getSession(id)
      this.steps = this.current.steps ?? []
      this.events = []
      return this.current
    },
    refresh() {
      return this.current ? this.selectSession(this.current.id) : Promise.resolve(null)
    },
    stream({ since = -1 } = {}) {
      if (!this.current) return () => {}
      this.streamClose?.()
      this.streamClose = openStepStream(this.current.id, {
        since,
        onEvent: (event) => {
          this.events.push(event)
          if (['awaiting_user', 'completed', 'failed'].includes(event.status)) this.refresh()
        },
        onError: (error) => {
          this.error = `步骤流中断：${error?.message ?? error}`
        },
      })
      return this.streamClose
    },
    closeStream() {
      this.streamClose?.()
      this.streamClose = null
    },
    async start(message = '') {
      if (!this.current) return null
      const session = await api.sendMessage(this.current.id, message)
      this.current = session
      this.stream()
      return session
    },
    async control(action) {
      if (!this.current) return null
      this.current = await api.control(this.current.id, action)
      return this.current
    },
    async drilldown(body) {
      if (!this.current) return null
      const session = await api.drilldown(this.current.id, body)
      this.current = session
      this.stream()
      return session
    },
  },
})
