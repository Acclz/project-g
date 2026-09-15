/**
 * 报告 store：归档列表、七段中间态、批注与导出任务。
 *
 * 页面的"预览"与三种导出都指向同一份 `structure_json`（技术规格 §4.6 / 需求 §5.11），
 * 所以预览页展示的字段与导出的字段天然一致；校验和由后端计算，前端只做展示与比对提示。
 */

import { defineStore } from 'pinia'
import { api } from '@/api/client'

const FORMATS = ['md', 'pdf', 'xlsx']

export const useReportStore = defineStore('report', {
  state: () => ({
    reports: [],
    current: null,
    exports: [],
    loading: false,
    error: '',
  }),
  getters: {
    segments(state) {
      return state.current?.segments ?? []
    },
    exportChecksums(state) {
      return [...new Set(state.exports.map((job) => job.content_checksum))]
    },
    exportsConsistent(state) {
      return state.exports.length === FORMATS.length && this.exportChecksums.length === 1
    },
  },
  actions: {
    async loadReports(params = {}) {
      const payload = await api.reports(params)
      this.reports = payload.items
      return payload.items
    },
    async createFromSession(sessionId) {
      this.error = ''
      try {
        this.current = await api.createReport(sessionId)
        await this.loadReports()
        return this.current
      } catch (error) {
        this.error = error.message
        throw error
      }
    },
    async select(reportId) {
      const sameReport = this.current?.meta?.report_id === reportId
      this.current = await api.report(reportId)
      // 同一份报告的重复打开（例如批注后刷新）不应清空已导出的任务列表，
      // 否则"三格式校验和一致"的提示会因为列表被清空而误报
      if (!sameReport) this.exports = []
      return this.current
    },
    async annotate(anchor, text) {
      if (!this.current) return null
      const annotation = await api.annotate(this.current.meta.report_id, {
        anchor,
        text,
        author: 'analyst',
      })
      await this.select(this.current.meta.report_id)
      return annotation
    },
    async exportAll() {
      if (!this.current) return []
      const reportId = this.current.meta.report_id
      this.exports = []
      for (const format of FORMATS) {
        this.exports.push(await api.exportReport(reportId, format))
      }
      return this.exports
    },
  },
})
