<script setup>
/**
 * 页面 5：报告归档与导出预览页（需求说明书 §4.5）。
 *
 * 左：报告列表（按指标域 / 状态筛选）；右：七段预览 + 段落级批注 + 三种格式导出。
 * 预览与三种导出都来自同一份 `structure_json`，页面直接显示后端给出的内容校验和，
 * 三种格式导出后可用它比对（一致时给出绿色提示）。
 */
import { onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { useReportStore } from '@/stores/report'

const report = useReportStore()
const route = useRoute()
const filter = reactive({ domain: '', status: '' })
const annotation = reactive({ anchor: '段1', text: '' })
const error = ref('')

async function reload() {
  await report.loadReports({
    domain: filter.domain || undefined,
    status: filter.status || undefined,
  })
}

async function open(reportId) {
  // 同一份报告已经在看就不再重复取一次（避免与导出流程互相打断）
  if (report.current?.meta?.report_id === reportId) return
  error.value = ''
  try {
    await report.select(reportId)
  } catch (problem) {
    error.value = problem.message
  }
}

async function annotate() {
  if (!report.current) return
  await report.annotate(annotation.anchor, annotation.text)
  annotation.text = ''
}

async function exportAll() {
  error.value = ''
  try {
    await report.exportAll()
  } catch (problem) {
    error.value = problem.message
  }
}

function download(job) {
  window.open(
    `${import.meta.env.VITE_API_BASE || '/api'}/exports/${job.id}/download`,
    '_blank',
  )
}

watch(
  () => route.query.report,
  (value) => {
    if (value) open(Number(value))
  },
)

onMounted(async () => {
  await reload()
  if (route.query.report) await open(Number(route.query.report))
  else if (report.reports.length) await open(report.reports[0].id)
})
</script>

<template>
  <div class="grid-2" style="grid-template-columns: 340px 1fr">
    <section class="panel">
      <h3>报告归档</h3>
      <div class="row">
        <label>指标域
          <select v-model="filter.domain" @change="reload">
            <option value="">全部</option>
            <option value="ecom">电商零售</option>
            <option value="fmcg">快消品</option>
          </select>
        </label>
        <label>状态
          <select v-model="filter.status" @change="reload">
            <option value="">全部</option>
            <option value="draft">draft</option>
            <option value="published">published</option>
          </select>
        </label>
      </div>
      <div class="scroll" style="max-height: 520px">
        <table>
          <thead>
            <tr><th>报告</th><th>期间</th><th>状态</th><th>批注</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in report.reports" :key="item.id">
              <td>
                <a href="#" @click.prevent="open(item.id)">#{{ item.id }} {{ item.title }}</a>
              </td>
              <td class="muted">{{ item.current_period }}</td>
              <td>{{ item.status }}</td>
              <td>{{ item.annotations }}</td>
            </tr>
            <tr v-if="!report.reports.length">
              <td colspan="4" class="muted">还没有报告：到工作台跑完链路后点"一键生成七段式报告"。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <template v-if="report.current">
        <h3>{{ report.current.meta.title }}</h3>
        <dl class="kv">
          <dt>会话</dt><dd>#{{ report.current.meta.session_id }}</dd>
          <dt>场景</dt>
          <dd>{{ report.current.meta.scenario_name }}（{{ report.current.meta.scenario }}）</dd>
          <dt>口径版本</dt><dd>v{{ report.current.meta.caliber_version }}</dd>
          <dt>对比期间</dt>
          <dd>
            {{ report.current.meta.base.start }}~{{ report.current.meta.base.end }} →
            {{ report.current.meta.current.start }}~{{ report.current.meta.current.end }}
          </dd>
          <dt>锁定切片</dt>
          <dd class="checkline">{{ JSON.stringify(report.current.meta.slice) }}</dd>
          <dt>数据版本</dt>
          <dd class="checkline">{{ report.current.meta.data_digest }}</dd>
        </dl>

        <div class="segment" v-for="segment in report.segments" :key="segment.index">
          <h4>{{ segment.index }}. {{ segment.title }}</h4>
          <div class="muted">
            数据来源：{{ segment.source }}　统计期间：{{ segment.period }}
          </div>
          <table>
            <thead><tr><th>字段</th><th>取值</th></tr></thead>
            <tbody>
              <tr v-for="field in segment.fields" :key="field.label">
                <td>{{ field.label }}</td>
                <td>{{ field.display }}</td>
              </tr>
            </tbody>
          </table>
          <ul class="muted">
            <li v-for="(line, index) in segment.body" :key="index">{{ line }}</li>
          </ul>
        </div>

        <h3>批注（段落级，只追加不覆盖）</h3>
        <div class="row">
          <label>锚点
            <select v-model="annotation.anchor">
              <option v-for="segment in report.segments" :key="segment.index" :value="`段${segment.index}`">
                段{{ segment.index }} {{ segment.title }}
              </option>
            </select>
          </label>
          <label style="flex: 1">批注<input v-model="annotation.text" /></label>
          <button @click="annotate">添加</button>
        </div>
        <ul>
          <li v-for="item in report.current.annotations" :key="item.id">
            [{{ item.anchor }}] {{ item.text }} —— {{ item.author }}
          </li>
        </ul>

        <h3>导出（同一份中间态渲染三种格式）</h3>
        <button @click="exportAll">导出 MD / PDF / Excel</button>
        <table style="margin-top: 8px" v-if="report.exports.length">
          <thead><tr><th>格式</th><th>文件</th><th>大小</th><th>内容校验和</th><th></th></tr></thead>
          <tbody>
            <tr v-for="job in report.exports" :key="job.id">
              <td>{{ job.format }}</td>
              <td class="checkline">{{ job.file_name }}</td>
              <td>{{ job.file_size }} B</td>
              <td class="checkline">{{ job.content_checksum }}</td>
              <td><button class="ghost" @click="download(job)">下载</button></td>
            </tr>
          </tbody>
        </table>
        <p v-if="report.exports.length">
          <span :class="report.exportsConsistent ? 'pill pill--ok' : 'pill pill--warn'">
            {{
              report.exportsConsistent
                ? '三格式校验和一致（与页面同源）'
                : '校验和尚未一致：请确认三种格式都已导出'
            }}
          </span>
        </p>
        <p v-if="error" class="error">{{ error }}</p>
        <p v-if="report.error" class="error">{{ report.error }}</p>
      </template>
      <p v-else class="muted">从左侧选一份报告查看七段预览。</p>
    </section>
  </div>
</template>
