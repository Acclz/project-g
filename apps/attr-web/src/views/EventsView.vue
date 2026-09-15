<script setup>
/**
 * 页面 3：业务事件日历（需求说明书 §4.3）。
 *
 * 列表（按时间与类型筛选）/ 新建 / 软删除；另给一个"按窗口匹配"的入口，
 * 用于核对"会话窗口内命中了哪些事件"（命中 = 入因果链的外生变量，待观察 = 只提示）。
 */
import { onMounted, reactive, ref } from 'vue'

import { useCatalogStore } from '@/stores/catalog'

const catalog = useCatalogStore()
const filter = reactive({ type: '', start_day: '', end_day: '' })
const draft = reactive({
  name: '',
  type: 'promo',
  start_day: '',
  end_day: '',
  channel: '',
  category: '',
  note: '',
})
const matchForm = reactive({
  scenario: 'ecom',
  window_start: '2026-06-05',
  window_end: '2026-06-11',
  channel: '',
  category: '',
})
const matched = ref(null)
const error = ref('')

async function reload() {
  await catalog.loadEvents({
    type: filter.type || undefined,
    start_day: filter.start_day || undefined,
    end_day: filter.end_day || undefined,
  })
}

async function create() {
  error.value = ''
  try {
    const dimensions = {}
    if (draft.channel) dimensions.channel = [draft.channel]
    if (draft.category) dimensions.category = [draft.category]
    await catalog.createEvent({
      name: draft.name,
      type: draft.type,
      start_day: draft.start_day,
      end_day: draft.end_day || null,
      dimensions,
      note: draft.note,
    })
    draft.name = ''
    draft.note = ''
  } catch (problem) {
    error.value = problem.message
  }
}

async function remove(id) {
  await catalog.deleteEvent(id)
}

async function match() {
  error.value = ''
  try {
    matched.value = await catalog.matchEvents({
      scenario: matchForm.scenario,
      window_start: matchForm.window_start,
      window_end: matchForm.window_end,
      channel: matchForm.channel || undefined,
      category: matchForm.category || undefined,
    })
  } catch (problem) {
    error.value = problem.message
  }
}

onMounted(reload)
</script>

<template>
  <div>
    <div class="panel">
      <h3>筛选</h3>
      <div class="row">
        <label>类型<input v-model="filter.type" placeholder="promo / competitor / policy" /></label>
        <label>起始日<input v-model="filter.start_day" placeholder="2026-06-01" /></label>
        <label>截止日<input v-model="filter.end_day" placeholder="2026-06-30" /></label>
        <button @click="reload">查询</button>
      </div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>事件列表（删除为软删除，保留审计）</h3>
        <div class="scroll">
          <table>
            <thead>
              <tr><th>事件</th><th>类型</th><th>时间</th><th>维度</th><th>备注</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="item in catalog.events" :key="item.id">
                <td>{{ item.name }}</td>
                <td>{{ item.type }}</td>
                <td>{{ item.start_day }}<span v-if="item.end_day"> ~ {{ item.end_day }}</span></td>
                <td class="checkline">{{ JSON.stringify(item.dimensions) }}</td>
                <td class="muted">{{ item.note }}</td>
                <td><button class="ghost" @click="remove(item.id)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3>新建事件</h3>
        <div class="row">
          <label>名称<input v-model="draft.name" placeholder="竞品降价 15%" /></label>
          <label>类型<input v-model="draft.type" /></label>
          <label>起始日<input v-model="draft.start_day" placeholder="2026-06-05" /></label>
          <label>截止日<input v-model="draft.end_day" placeholder="2026-06-11" /></label>
          <label>渠道（可选）<input v-model="draft.channel" placeholder="paid_ads" /></label>
          <label>品类（可选）<input v-model="draft.category" placeholder="appliance" /></label>
          <label>备注<input v-model="draft.note" /></label>
          <button @click="create">保存</button>
        </div>
        <p v-if="error" class="error">{{ error }}</p>

        <h3 style="margin-top: 14px">按会话窗口匹配</h3>
        <div class="row">
          <label>场景
            <select v-model="matchForm.scenario">
              <option value="ecom">电商零售</option>
              <option value="fmcg">快消品</option>
            </select>
          </label>
          <label>窗口起<input v-model="matchForm.window_start" /></label>
          <label>窗口止<input v-model="matchForm.window_end" /></label>
          <label>渠道<input v-model="matchForm.channel" /></label>
          <label>品类<input v-model="matchForm.category" /></label>
          <button @click="match">匹配</button>
        </div>
        <div v-if="matched">
          <h4>命中（入因果链，标为外生变量）</h4>
          <ul>
            <li v-for="item in matched.matched" :key="item.event.id">
              {{ item.event.name }}　相关度 {{ item.relevance }}　{{ item.reason }}
            </li>
            <li v-if="!matched.matched.length" class="muted">窗口内没有命中</li>
          </ul>
          <h4>待观察</h4>
          <ul>
            <li v-for="item in matched.watching" :key="item.event.id">
              {{ item.event.name }}　相关度 {{ item.relevance }}　{{ item.reason }}
            </li>
            <li v-if="!matched.watching.length" class="muted">没有待观察事件</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>
