<script setup>
/**
 * 维度透视表：按绝对贡献排序的前 N 行（TOP N 与覆盖率都由后端给出）。
 * 排序只影响展示顺序，不改动任何数值。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  rows: { type: Array, required: true },
  dimensions: { type: Array, default: () => [] },
  topN: { type: Number, default: 5 },
  coverage: { type: Number, default: 0 },
  heuristic: { type: Boolean, default: false },
})

const sortKey = ref('contribution')
const sorted = computed(() => {
  const copy = [...props.rows]
  copy.sort((left, right) => {
    if (sortKey.value === 'key') return String(left.key).localeCompare(String(right.key))
    return Math.abs(right[sortKey.value] ?? 0) - Math.abs(left[sortKey.value] ?? 0)
  })
  return copy
})

function formatRate(rate) {
  return rate === null || rate === undefined ? '—' : `${(rate * 100).toFixed(1)}%`
}
</script>

<template>
  <div>
    <div class="row" style="justify-content: space-between; margin-bottom: 6px">
      <span class="muted">
        维度：{{ dimensions.join(' × ') || '—' }}　TOP {{ topN }} 覆盖率
        {{ (coverage * 100).toFixed(1) }}%
        <span :class="heuristic ? 'pill pill--warn' : 'pill pill--ok'">
          {{ heuristic ? '占比分摊（启发式）' : '精确组合计算' }}
        </span>
      </span>
      <span class="row">
        <button class="ghost" @click="sortKey = 'contribution'">按贡献排序</button>
        <button class="ghost" @click="sortKey = 'key'">按维度排序</button>
      </span>
    </div>
    <div class="scroll" style="max-height: 260px">
      <table>
        <thead>
          <tr>
            <th>组合</th>
            <th>基期</th>
            <th>现期</th>
            <th>贡献额</th>
            <th>贡献率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in sorted" :key="row.key.join('/')">
            <td>
              {{ row.key.join(' / ') }}
              <span v-if="row.in_top" class="pill">TOP</span>
            </td>
            <td>{{ Math.round(row.base ?? 0).toLocaleString() }}</td>
            <td>{{ Math.round(row.current ?? 0).toLocaleString() }}</td>
            <td>{{ Math.round(row.contribution).toLocaleString() }}</td>
            <td>{{ formatRate(row.contribution_rate) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
