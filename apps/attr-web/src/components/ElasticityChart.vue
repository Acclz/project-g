<script setup>
/**
 * What-If 参数曲线：点估计 + 区间带。
 * 区间上下界直接用后端返回的 low/high（bootstrap 百分位区间），前端不重新估计。
 */
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'

const props = defineProps({
  curve: { type: Object, required: true }, // { points: [{adjustment, expected, low, high}], ... }
})

const option = computed(() => {
  const adjustments = props.curve.points.map((point) => `${(point.adjustment * 100).toFixed(0)}%`)
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const index = params[0].dataIndex
        const point = props.curve.points[index]
        return [
          `调整 ${adjustments[index]}`,
          `目标（点估计）：${point.expected.toLocaleString()} 分`,
          `区间：${point.low.toLocaleString()} ~ ${point.high.toLocaleString()} 分`,
          point.out_of_range ? '⚠ 超出历史观测区间（外推）' : '',
        ]
          .filter(Boolean)
          .join('<br/>')
      },
    },
    grid: { left: 80, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: adjustments },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { formatter: (value) => value.toLocaleString() },
    },
    series: [
      {
        name: '区间上界',
        type: 'line',
        showSymbol: false,
        data: props.curve.points.map((point) => point.high),
        lineStyle: { opacity: 0 },
        stack: 'band',
      },
      {
        name: '区间',
        type: 'line',
        showSymbol: false,
        data: props.curve.points.map((point) => point.high - point.low),
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'rgba(31,111,235,0.15)' },
        stack: 'band',
      },
      {
        name: '点估计',
        type: 'line',
        symbolSize: 5,
        data: props.curve.points.map((point) => point.expected),
        lineStyle: { color: '#1f6feb', width: 2 },
      },
    ],
  }
})
</script>

<template>
  <BaseChart :option="option" height="220px" />
</template>
