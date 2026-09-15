<script setup>
/**
 * 页面 1：指标异动大盘（需求说明书 §4.1）。
 *
 * 卡片：当前值 / 环比 / 与基线带的位置；清单：按严重度排序，带 Z 分数与"是否已发起归因"；
 * 一键发起归因：带上指标、两期与切片直接建会话并跳到工作台。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import TrendChart from '@/components/TrendChart.vue'
import { useCatalogStore } from '@/stores/catalog'
import { useSessionStore } from '@/stores/session'

const catalog = useCatalogStore()
const sessions = useSessionStore()
const router = useRouter()
const period = ref('')
const days = ref(60)
const busy = ref(false)

const composite = computed(
  () => catalog.anomalies?.items?.find((item) => item.level === 'composite') ?? null,
)
const domains = [
  { code: 'ecom', label: '电商零售' },
  { code: 'fmcg', label: '快消品' },
]

async function reload() {
  await catalog.loadAnomalies(period.value || undefined)
  await catalog.loadSeries(days.value)
}

function switchDomain(code) {
  catalog.domain = code
  catalog.selectedMetric = code === 'ecom' ? 'gmv' : 'gross_profit'
  reload()
}

function pickMetric(code) {
  catalog.selectedMetric = code
  catalog.loadSeries(days.value)
}

async function startAttribution(item) {
  busy.value = true
  try {
    const session = await sessions.createSession({
      scenario: catalog.domain,
      base_start: item.base_period.start,
      base_end: item.base_period.end,
      current_start: item.period.start,
      current_end: item.period.end,
      title: `${catalog.anomalies.scenario_name}·${item.name}（大盘发起）`,
      actor: 'analyst',
    })
    await sessions.start(`从大盘发起：${item.name} ${item.period.start}~${item.period.end}`)
    router.push({ name: 'workbench', query: { session: session.id } })
  } finally {
    busy.value = false
  }
}

function formatAmount(value) {
  return value === null || value === undefined ? '—' : Math.round(value).toLocaleString()
}
function formatRate(value) {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

onMounted(reload)
</script>

<template>
  <div>
    <div class="panel">
      <div class="row">
        <label>
          指标域
          <select :value="catalog.domain" @change="switchDomain($event.target.value)">
            <option v-for="item in domains" :key="item.code" :value="item.code">
              {{ item.label }}
            </option>
          </select>
        </label>
        <label>
          现期（留空取数仓末尾 7 天）
          <input v-model="period" placeholder="2026-06-05~2026-06-11" />
        </label>
        <label>
          趋势天数
          <input v-model.number="days" type="number" min="7" max="400" style="width: 90px" />
        </label>
        <button :disabled="catalog.loading" @click="reload">刷新</button>
        <span v-if="catalog.error" class="error">{{ catalog.error }}</span>
      </div>
    </div>

    <div v-if="composite" class="grid-3" style="grid-template-columns: repeat(3, 1fr)">
      <div class="panel">
        <h3>{{ composite.name }}（现期）</h3>
        <div style="font-size: 22px">{{ formatAmount(composite.current_value) }}</div>
        <div class="muted">
          基期 {{ formatAmount(composite.base_value) }}　环比
          <span :class="composite.change_rate < 0 ? 'pill pill--danger' : 'pill pill--ok'">
            {{ formatRate(composite.change_rate) }}
          </span>
        </div>
      </div>
      <div class="panel">
        <h3>与基线带的位置</h3>
        <div>{{ composite.in_normal_band ? '落在正常波动区间内' : '超出正常波动区间' }}</div>
        <div class="muted">
          Z 分数 {{ composite.z_score === null ? '—' : composite.z_score.toFixed(2) }}　
          基线 {{ formatAmount(composite.baseline_value) }}
        </div>
        <div class="muted">
          带下界 {{ formatAmount(composite.baseline_low) }} ~ 上界
          {{ formatAmount(composite.baseline_high) }}
        </div>
      </div>
      <div class="panel">
        <h3>异动判定口径</h3>
        <div>
          幅度阈值 {{ (catalog.anomalies.thresholds.change_rate * 100).toFixed(0) }}%　Z 阈值
          {{ catalog.anomalies.thresholds.z_score }}
        </div>
        <div class="muted">
          基线 = 过去 4 周同星期几中位数 ± 2σ；每条判定都回答"是否落在正常波动区间"
        </div>
        <div class="muted">期间：{{ catalog.anomalies.period.start }} ~ {{ catalog.anomalies.period.end }}</div>
      </div>
    </div>

    <div class="panel">
      <h3>趋势与基线带 · {{ catalog.series?.name ?? '—' }}</h3>
      <TrendChart
        v-if="catalog.series"
        :points="catalog.series.points"
        :baseline="catalog.series.baseline"
      />
      <div v-else class="muted">先选一个指标</div>
      <div class="row" style="margin-top: 6px">
        <button
          v-for="item in catalog.anomalies?.items?.slice(0, 8) ?? []"
          :key="item.code"
          class="ghost"
          @click="pickMetric(item.code)"
        >
          {{ item.name }}
        </button>
      </div>
    </div>

    <div class="panel">
      <h3>异动清单（按严重度排序）</h3>
      <table>
        <thead>
          <tr>
            <th>指标</th>
            <th>期间</th>
            <th>基期 → 现期</th>
            <th>变动幅度</th>
            <th>Z 分数</th>
            <th>正常波动区间</th>
            <th>是否异动</th>
            <th>归因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in catalog.anomalies?.items ?? []" :key="item.code">
            <td>{{ item.name }}<span class="muted">（{{ item.code }}）</span></td>
            <td>{{ item.period.start }} ~ {{ item.period.end }}</td>
            <td>{{ formatAmount(item.base_value) }} → {{ formatAmount(item.current_value) }}</td>
            <td>{{ formatRate(item.change_rate) }}</td>
            <td>{{ item.z_score === null ? '—' : item.z_score.toFixed(2) }}</td>
            <td>
              <span :class="item.in_normal_band ? 'pill pill--ok' : 'pill pill--danger'">
                {{ item.in_normal_band ? '在区间内' : '超出区间' }}
              </span>
            </td>
            <td>
              <span :class="item.is_anomaly ? 'pill pill--danger' : 'pill'">
                {{ item.is_anomaly ? '异动' : '正常' }}
              </span>
            </td>
            <td>
              <button
                v-if="!item.attribution?.started"
                class="secondary"
                :disabled="busy"
                @click="startAttribution(item)"
              >
                发起归因
              </button>
              <span v-else class="pill pill--ok">
                已发起（会话 #{{ item.attribution.session_ids[0] }}）
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="catalog.anomalies?.items?.some((item) => item.note)" class="muted">
        说明：{{ catalog.anomalies.items.find((item) => item.note)?.note }}
      </p>
    </div>
  </div>
</template>
