<script setup>
/**
 * 因子贡献瀑布图：用 ECharts 的自定义 series 画"从基期到现期"的逐因子拆解。
 * 数值直接来自后端分解结果（贡献额），前端不做累加。
 */
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'

const props = defineProps({
  baseValue: { type: Number, required: true },
  currentValue: { type: Number, required: true },
  contributions: { type: Array, required: true }, // [{ factor, contribution, rate }]
})

const option = computed(() => {
  const labels = ['基期', ...props.contributions.map((item) => item.factor), '现期']
  let running = props.baseValue
  const bottoms = []
  const values = []
  let index = 0
  bottoms.push(0)
  values.push([index, props.baseValue, props.baseValue])
  props.contributions.forEach((item, order) => {
    index = order + 1
    const start = running
    running += item.contribution
    bottoms.push(Math.min(start, running))
    values.push([index, Math.min(start, running), Math.max(start, running)])
  })
  index += 1
  values.push([index, 0, props.currentValue])
  return {
    tooltip: {
      formatter: (params) => {
        const [x, low, high] = params.value
        const name = labels[x]
        const detail = props.contributions[x - 1]
        const text = detail
          ? `${name}：${detail.contribution.toLocaleString()} 分（贡献率 ${
              detail.rate === null ? '—' : (detail.rate * 100).toFixed(1) + '%'
            }）`
          : `${name}：${high.toLocaleString()} 分`
        return text
      },
    },
    grid: { left: 70, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: labels },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value) => value.toLocaleString() } },
    series: [
      {
        type: 'custom',
        renderItem: (params, api) => {
          const index = api.value(0)
          const low = api.coord([index, api.value(1)])
          const high = api.coord([index, api.value(2)])
          const width = api.size([1, 0])[0] * 0.5
          return {
            type: 'rect',
            shape: {
              x: low[0] - width / 2,
              y: high[1],
              width,
              height: Math.max(low[1] - high[1], 1),
            },
            style: {
              fill: index === 0 || index === labels.length - 1 ? '#8aa2bf' : '#3d7fd1',
            },
          }
        },
        encode: { x: 0, y: [1, 2] },
        data: values,
      },
    ],
  }
})
</script>

<template>
  <BaseChart :option="option" height="220px" />
</template>
