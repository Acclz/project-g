<script setup>
/**
 * 指标序列 + 基线带：基线（中位数）与上下界（±2σ）都来自后端异动判定接口。
 * 基线带用一个"上限线 + 下限线 + 填充"的组合画出来（ECharts 的 band 效果）。
 */
import { computed } from 'vue'
import BaseChart from './BaseChart.vue'

const props = defineProps({
  points: { type: Array, required: true }, // [{ day, value }]
  baseline: { type: Object, default: null }, // { value, low, high }
})

const option = computed(() => {
  const days = props.points.map((item) => item.day)
  const values = props.points.map((item) => item.value)
  const baseline = props.baseline?.value
  const series = [
    {
      name: '指标',
      type: 'line',
      smooth: false,
      showSymbol: false,
      data: values,
      lineStyle: { color: '#1f6feb', width: 2 },
    },
  ]
  if (baseline !== null && baseline !== undefined) {
    series.push({
      name: '基线（过去 4 周同星期几中位数）',
      type: 'line',
      showSymbol: false,
      data: days.map(() => baseline),
      lineStyle: { color: '#b7791f', type: 'dashed', width: 1 },
    })
    if (props.baseline.low !== null && props.baseline.high !== null) {
      series.push({
        name: '基线带下界',
        type: 'line',
        showSymbol: false,
        data: days.map(() => props.baseline.low),
        lineStyle: { opacity: 0 },
        stack: 'band',
      })
      series.push({
        name: '基线带',
        type: 'line',
        showSymbol: false,
        data: days.map(() => props.baseline.high - props.baseline.low),
        lineStyle: { opacity: 0 },
        areaStyle: { color: 'rgba(183,121,31,0.12)' },
        stack: 'band',
      })
    }
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 70, right: 20, top: 30, bottom: 30 },
    xAxis: { type: 'category', data: days, axisLabel: { hideOverlap: true } },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value) => value.toLocaleString() } },
    series,
  }
})
</script>

<template>
  <BaseChart :option="option" height="240px" />
</template>
