<script setup>
/**
 * 步骤流：SSE 事件（kind 与 session_steps.kind 一一对应）+ 已落库步骤的合并展示。
 * 中栏产出的每一步，右栏都会有对应证据卡（三栏联动，需求说明书 §4.2）。
 */
import { computed } from 'vue'

const props = defineProps({
  steps: { type: Array, default: () => [] },
  events: { type: Array, default: () => [] },
})

const KIND_LABEL = {
  plan: '计划 / 状态迁移',
  query: '取数与分解',
  drilldown: '维度下钻',
  hypothesis: '假设生成',
  verify: '沙箱验证',
  event: '事件匹配',
  whatif: 'What-If 推演',
  report: '报告与结论',
}

const merged = computed(() => {
  const rows = [
    ...props.steps.map((step) => ({
      key: `step-${step.seq}`,
      kind: step.kind,
      status: step.status,
      duration: step.duration_ms,
      payload: step.payload,
      source: '落库步骤',
    })),
    ...props.events.map((event, index) => ({
      key: `event-${event.seq ?? index}-${index}`,
      kind: event.kind,
      status: event.status,
      duration: null,
      payload: event.payload,
      source: '实时事件',
    })),
  ]
  return rows
})

function statusClass(status) {
  if (status === 'failed') return 'step step--bad'
  if (['completed', 'awaiting_user', 'verified'].includes(status)) return 'step step--ok'
  if (['analysing', 'created', 'accepted'].includes(status)) return 'step step--running'
  return 'step'
}

function summary(item) {
  const payload = item.payload ?? {}
  const parts = []
  if (payload.note) parts.push(payload.note)
  if (payload.count !== undefined) parts.push(`条数 ${payload.count}`)
  if (payload.factor) parts.push(`因子 ${payload.factor}`)
  if (payload.coverage !== undefined) parts.push(`覆盖率 ${(payload.coverage * 100).toFixed(1)}%`)
  if (payload.dimensions) parts.push(`维度 ${payload.dimensions.join(' × ')}`)
  if (payload.final_confidence !== undefined) parts.push(`置信度 ${payload.final_confidence}`)
  if (payload.pseudo_verdict) parts.push(`伪相关 ${payload.pseudo_verdict}`)
  if (payload.error) parts.push(payload.error)
  return parts.join('　') || JSON.stringify(payload).slice(0, 80)
}
</script>

<template>
  <div class="steps">
    <div v-if="!merged.length" class="muted">还没有步骤：点"发起归因分析"或发送追问。</div>
    <div v-for="item in merged" :key="item.key" :class="statusClass(item.status)">
      <span class="pill">{{ KIND_LABEL[item.kind] ?? item.kind }}</span>
      <span class="muted">{{ item.source }}</span>
      <span>{{ item.status }}</span>
      <span v-if="item.duration !== null" class="muted">{{ item.duration }} ms</span>
      <span class="muted">{{ summary(item) }}</span>
    </div>
  </div>
</template>
