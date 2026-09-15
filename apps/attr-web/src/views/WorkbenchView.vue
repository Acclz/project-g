<script setup>
/**
 * 页面 2：归因分析工作台（三栏，需求说明书 §4.2）。
 *
 * 左：会话与指标导航（锁定上下文一目了然）
 * 中：对话与探索（SSE 步骤流 + 下钻 + 暂停/取消/恢复）
 * 右：动态归因看板（瀑布图 / 透视表 / 假设证据卡 / What-If / 一键出报告）
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ElasticityChart from '@/components/ElasticityChart.vue'
import PivotTable from '@/components/PivotTable.vue'
import StepStream from '@/components/StepStream.vue'
import WaterfallChart from '@/components/WaterfallChart.vue'
import { useCatalogStore } from '@/stores/catalog'
import { useEvidenceStore } from '@/stores/evidence'
import { useReportStore } from '@/stores/report'
import { useSessionStore } from '@/stores/session'

const sessions = useSessionStore()
const evidence = useEvidenceStore()
const report = useReportStore()
const catalog = useCatalogStore()
const route = useRoute()
const router = useRouter()

const message = ref('')
const notice = ref('')
const drilldownForm = reactive({ dimension: 'category', value: '', pivot: 'category' })
const whatifForm = reactive({
  factor: '',
  adjustments: '-20,-10,0,10,20',
  dimensions: '',
})
const whatifResult = ref(null)

const decompositionStep = computed(() =>
  [...sessions.steps].reverse().find((item) => item.kind === 'query'),
)
const waterfall = computed(() => {
  const root = decompositionRoot.value
  if (!root) return null
  return {
    baseValue: root.base_total,
    currentValue: root.current_total,
    contributions: root.contributions,
  }
})
const decompositionRoot = computed(() => {
  // 根节点数据来自 L1 的 query 步骤载荷（后端分解结果原样落库）；
  // 没有会话数据时退回报告结构（同一份后端数据，字段级一致）
  return decompositionStep.value?.payload?.root ?? rootFromReport.value
})
const rootFromReport = computed(() => report.current?.segments?.[1]?.data?.decomposition?.nodes?.[0] ?? null)
const factors = computed(() => evidence.factors ?? [])
const lockedSlice = computed(() => sessions.current?.slice ?? {})

const form = reactive({
  scenario: 'ecom',
  base_start: '2026-06-01',
  base_end: '2026-06-04',
  current_start: '2026-06-05',
  current_end: '2026-06-08',
  channel: '',
  category: '',
  region: '',
  segment: '',
})

watch(
  () => sessions.current?.id,
  (id) => {
    evidence.load(id)
    if (factors.value.length && !whatifForm.factor) {
      whatifForm.factor = factors.value.find((item) => item.estimable)?.code ?? ''
    }
  },
)

// 链路跑完（状态离开 analysing）时必须重拉证据：假设、证据链、下钻记录都是后端落库后才有的
watch(
  () => sessions.current?.status,
  async (status) => {
    if (['awaiting_user', 'completed', 'failed'].includes(status) && sessions.current) {
      await evidence.load(sessions.current.id)
      if (!whatifForm.factor) {
        whatifForm.factor = factors.value.find((item) => item.estimable)?.code ?? ''
      }
    }
  },
)

async function loadSampleWindow() {
  await catalog.loadAnomalies()
  const root = catalog.anomalies?.items?.find((item) => item.level === 'composite')
  if (root) {
    form.scenario = catalog.domain
    form.base_start = root.base_period.start
    form.base_end = root.base_period.end
    form.current_start = root.period.start
    form.current_end = root.period.end
  }
}

async function createAndRun() {
  const body = {
    scenario: form.scenario,
    base_start: form.base_start,
    base_end: form.base_end,
    current_start: form.current_start,
    current_end: form.current_end,
    title: `${form.scenario} ${form.base_start}~${form.current_end}`,
    actor: 'analyst',
  }
  for (const key of ['channel', 'category', 'region', 'segment']) {
    if (form[key]) body[key] = form[key]
  }
  await sessions.createSession(body)
  await sessions.start('为什么变了？')
  notice.value = `会话 #${sessions.current.id} 已创建并开跑（L1：异动 → 拆解 → 假设 → 验证 → 事件）`
}

async function send() {
  await sessions.start(message.value)
  message.value = ''
}

async function pause() {
  await sessions.control('pause')
  notice.value = '已请求暂停：当前任务结束后停在等待用户'
}
async function cancel() {
  await sessions.control('cancel')
  notice.value = '已取消：会话标记 failed，旧结论不进入报告'
}
async function resume() {
  await sessions.control('resume')
  notice.value = '已恢复：重新校验权限与数据版本后继续'
}

async function runDrilldown() {
  const body = {}
  if (drilldownForm.value) body[drilldownForm.dimension] = drilldownForm.value
  if (drilldownForm.pivot) body.dimensions = [drilldownForm.pivot.split(',')]
  await sessions.drilldown(body)
  notice.value = `下钻已受理：切片只能收紧（${drilldownForm.dimension}=${drilldownForm.value}）`
}

async function runWhatif() {
  const adjustments = whatifForm.adjustments
    .split(',')
    .map((item) => Number(item.trim()) / 100)
    .filter((item) => !Number.isNaN(item))
  const payload = {
    factor: whatifForm.factor,
    adjustments,
  }
  if (whatifForm.dimensions) payload.dimensions = [whatifForm.dimensions.split(',')]
  whatifResult.value = await evidence.runWhatIf(payload)
  notice.value = `推演完成：把握度 ${(whatifResult.value.whatif.confidence * 100).toFixed(0)}%`
}

async function makeReport() {
  await report.createFromSession(sessions.current.id)
  notice.value = `报告 #${report.current.meta.report_id} 已生成（七段中间态）`
  router.push({ name: 'reports', query: { report: report.current.meta.report_id } })
}

onMounted(async () => {
  const queryId = Number(route.query.session)
  if (queryId) await sessions.selectSession(queryId)
  else await sessions.loadSessions()
  await evidence.load(sessions.current?.id ?? null)
})

onBeforeUnmount(() => sessions.closeStream())
</script>

<template>
  <div class="grid-3">
    <!-- 左：会话与指标导航 -->
    <section class="panel">
      <h3>会话</h3>
      <button class="secondary" style="width: 100%" @click="loadSampleWindow">取大盘期间</button>
      <div class="row" style="margin: 8px 0">
        <label>场景
          <select v-model="form.scenario">
            <option value="ecom">电商零售</option>
            <option value="fmcg">快消品</option>
          </select>
        </label>
        <label>基期起<input v-model="form.base_start" /></label>
        <label>基期止<input v-model="form.base_end" /></label>
        <label>现期起<input v-model="form.current_start" /></label>
        <label>现期止<input v-model="form.current_end" /></label>
        <label>渠道<input v-model="form.channel" placeholder="paid_ads" /></label>
        <label>品类<input v-model="form.category" placeholder="appliance" /></label>
      </div>
      <button :disabled="sessions.isRunning" @click="createAndRun">新建会话并开跑</button>

      <h3 style="margin-top: 12px">当前锁定上下文</h3>
      <dl v-if="sessions.current" class="kv">
        <dt>会话</dt><dd>#{{ sessions.current.id }}　{{ sessions.current.status }}</dd>
        <dt>指标</dt><dd>{{ sessions.current.title }}</dd>
        <dt>口径版本</dt><dd>v{{ sessions.current.caliber_version }}</dd>
        <dt>基期</dt><dd>{{ sessions.current.base.start }} ~ {{ sessions.current.base.end }}</dd>
        <dt>现期</dt><dd>{{ sessions.current.current.start }} ~ {{ sessions.current.current.end }}</dd>
        <dt>锁定切片</dt><dd class="checkline">{{ JSON.stringify(lockedSlice) }}</dd>
        <dt>数据版本</dt><dd class="checkline">{{ sessions.current.data_digest }}</dd>
      </dl>
      <p v-else class="muted">还没有会话：左侧建一个，或从异动大盘一键发起。</p>

      <h3 style="margin-top: 12px">历史会话</h3>
      <div class="scroll" style="max-height: 160px">
        <table>
          <tbody>
            <tr v-for="item in sessions.sessions" :key="item.id">
              <td>
                <a href="#" @click.prevent="sessions.selectSession(item.id)">#{{ item.id }}</a>
                {{ item.title }}
              </td>
              <td class="muted">{{ item.status }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 中：对话与探索 -->
    <section class="panel">
      <h3>分析路径（SSE 步骤流）</h3>
      <div class="row">
        <button class="ghost" :disabled="!sessions.current" @click="pause">暂停</button>
        <button class="ghost" :disabled="!sessions.current" @click="cancel">取消</button>
        <button class="ghost" :disabled="!sessions.current" @click="resume">恢复</button>
      </div>
      <div class="scroll" style="margin: 8px 0">
        <StepStream :steps="sessions.steps" :events="sessions.events" />
      </div>

      <h3>追问</h3>
      <div class="row">
        <input v-model="message" style="flex: 1" placeholder="例如：付费渠道里是哪个品类拖的？" />
        <button :disabled="!sessions.current || sessions.isRunning" @click="send">发送</button>
      </div>

      <h3 style="margin-top: 12px">维度下钻（L2，切片只能收紧）</h3>
      <div class="row">
        <label>收紧维度
          <select v-model="drilldownForm.dimension">
            <option value="channel">渠道</option>
            <option value="category">品类</option>
            <option value="region">区域</option>
            <option value="segment">客群</option>
            <option value="sku">物料</option>
          </select>
        </label>
        <label>取值<input v-model="drilldownForm.value" placeholder="paid_ads" /></label>
        <label>透视维度（逗号分隔，最多 3 个）
          <input v-model="drilldownForm.pivot" placeholder="category,region" />
        </label>
        <button :disabled="!sessions.current" @click="runDrilldown">下钻</button>
      </div>

      <h3 style="margin-top: 12px">What-If 推演（L3）</h3>
      <div class="row">
        <label>可干预因子
          <select v-model="whatifForm.factor">
            <option v-for="item in factors" :key="item.code" :value="item.code" :disabled="!item.estimable">
              {{ item.name }}（{{ item.code }}）{{ item.estimable ? '' : '· 不可估' }}
            </option>
          </select>
        </label>
        <label>调整档位（%，逗号分隔）<input v-model="whatifForm.adjustments" /></label>
        <label>分维度（可选，逗号分隔）<input v-model="whatifForm.dimensions" placeholder="channel" /></label>
        <button :disabled="!sessions.current" @click="runWhatif">推演</button>
      </div>
      <p v-if="notice" class="muted">{{ notice }}</p>
      <p v-if="sessions.error" class="error">{{ sessions.error }}</p>
      <p v-if="evidence.error" class="error">{{ evidence.error }}</p>
    </section>

    <!-- 右：动态归因看板 -->
    <section class="panel">
      <h3>因子贡献瀑布</h3>
      <WaterfallChart
        v-if="waterfall"
        :base-value="waterfall.baseValue"
        :current-value="waterfall.currentValue"
        :contributions="waterfall.contributions"
      />
      <p v-else class="muted">跑完 L1 后这里会出现瀑布图（数据来自分解步骤载荷）。</p>

      <h3>维度透视</h3>
      <PivotTable
        v-for="pivot in evidence.pivots"
        :key="pivot.dimensions.join('-') + pivot.rows.length"
        :rows="pivot.rows"
        :dimensions="pivot.dimensions"
        :top-n="pivot.top_n"
        :coverage="pivot.coverage"
        :heuristic="pivot.heuristic"
      />
      <p v-if="!evidence.pivots.length" class="muted">还没有下钻结果。</p>

      <h3>核心结论与证据卡</h3>
      <div v-for="summary in evidence.drilldownSummaries" :key="summary.slice" class="panel" style="margin-bottom: 8px">
        <div class="row" style="justify-content: space-between">
          <span class="pill" :class="summary.conserved ? 'pill--ok' : 'pill--danger'">
            逐层守恒 {{ summary.conserved ? '通过' : '未通过' }}
          </span>
          <span class="muted">收紧 {{ summary.narrowed ? '是' : '否' }}</span>
        </div>
        <ul>
          <li v-for="(item, index) in summary.highlights" :key="index">{{ item }}</li>
        </ul>
        <p class="muted">{{ summary.conclusion?.summary }}</p>
      </div>

      <h3>假设与证据链</h3>
      <table>
        <thead>
          <tr><th>命题</th><th>状态</th><th>置信度</th><th>依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in evidence.hypotheses" :key="item.id">
            <td>{{ item.statement }}</td>
            <td>
              <span :class="item.status === 'verified' ? 'pill pill--ok' : 'pill pill--warn'">
                {{ item.status }}
              </span>
            </td>
            <td>{{ item.confidence === null ? '—' : item.confidence.toFixed(2) }}</td>
            <td class="muted">{{ item.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!evidence.hypotheses.length" class="muted">还没有假设。</p>

      <h3>What-If 曲线</h3>
      <div v-for="item in evidence.whatifSummaries" :key="item.factor.code + item.window.start" style="margin-bottom: 10px">
        <div class="muted">
          {{ item.factor.name }}（{{ item.factor.proxy_label }}）弹性
          {{ item.curve.elasticity.value.toFixed(3) }}
          （{{ (item.curve.elasticity.level * 100).toFixed(0) }}% 区间
          {{ item.curve.elasticity.low.toFixed(3) }} ~ {{ item.curve.elasticity.high.toFixed(3) }}）
          把握度 {{ (item.confidence * 100).toFixed(0) }}
        </div>
        <ElasticityChart :curve="item.curve" />
        <ul class="muted">
          <li v-for="(warning, index) in item.warnings" :key="index">{{ warning }}</li>
        </ul>
      </div>
      <p v-if="!evidence.whatifSummaries.length" class="muted">还没有推演结果。</p>

      <h3>交付</h3>
      <button :disabled="!sessions.current" @click="makeReport">一键生成七段式报告</button>
      <p v-if="report.error" class="error">{{ report.error }}</p>
    </section>
  </div>
</template>
