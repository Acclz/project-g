<script setup>
/**
 * 页面 4：指标字典配置页（需求说明书 §4.4）。
 *
 * 展示场景 → 指标节点（层级 / 结构 / 方法 / 单位 / 口径），并支持逐个节点做口径校验
 * （公式可解析、引用存在、结构可分解、SQL 能出数）。前端不解析公式，全部由后端判定。
 */
import { computed, onMounted, ref } from 'vue'

import { useCatalogStore } from '@/stores/catalog'

const catalog = useCatalogStore()
const selected = ref('')
const error = ref('')

const scenario = computed(() => catalog.dictionary?.[catalog.domain] ?? null)
const metrics = computed(() => scenario.value?.metrics ?? [])

async function load() {
  await catalog.loadDictionary()
  const first = catalog.dictionary?.[catalog.domain]?.root
  if (first) await pick(first)
}

async function pick(code) {
  selected.value = code
  error.value = ''
  try {
    await catalog.loadTree(code)
    await catalog.validate(code)
  } catch (problem) {
    error.value = problem.message
  }
}

function flatten(node, result = [], depth = 0) {
  result.push({ ...node, depth })
  ;(node.children ?? []).forEach((child) => flatten(child, result, depth + 1))
  return result
}

const treeRows = computed(() => (catalog.tree?.tree ? flatten(catalog.tree.tree) : []))

onMounted(load)
</script>

<template>
  <div>
    <div class="panel">
      <div class="row">
        <label>场景
          <select v-model="catalog.domain" @change="load">
            <option value="ecom">电商零售</option>
            <option value="fmcg">快消品</option>
          </select>
        </label>
        <span class="muted">
          事实表：{{ scenario?.fact_table ?? '—' }}　维度：{{ (scenario?.dimensions ?? []).join('、') }}
        </span>
      </div>
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>指标清单（{{ metrics.length }} 个节点）</h3>
        <div class="scroll" style="max-height: 520px">
          <table>
            <thead>
              <tr><th>指标</th><th>层级</th><th>结构</th><th>方法</th><th>口径</th></tr>
            </thead>
            <tbody>
              <tr v-for="item in metrics" :key="item.code">
                <td>
                  <a href="#" @click.prevent="pick(item.code)">
                    {{ item.name }}<span class="muted">（{{ item.code }}）</span>
                  </a>
                  <span v-if="item.code === selected" class="pill pill--ok">当前</span>
                </td>
                <td>{{ item.level }}</td>
                <td>
                  <span :class="item.decomposable ? 'pill pill--ok' : 'pill'">{{ item.structure }}</span>
                </td>
                <td>{{ item.method ?? '—' }}</td>
                <td class="muted">{{ item.caliber ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3>拆解树 · {{ catalog.tree?.tree?.name ?? '—' }}</h3>
        <table>
          <thead>
            <tr><th>节点</th><th>结构 / 方法</th><th>公式</th><th>SQL 摘要</th></tr>
          </thead>
          <tbody>
            <tr v-for="node in treeRows" :key="node.code">
              <td>
                <span :style="{ paddingLeft: `${node.depth * 12}px` }">{{ node.name }}</span>
                <span class="muted">（{{ node.code }}）</span>
              </td>
              <td>{{ node.structure }} / {{ node.method ?? '—' }}</td>
              <td class="muted">{{ node.formula ?? '—' }}</td>
              <td class="checkline">{{ node.has_sql ? '有 SQL' : '缺 SQL' }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="error" class="error">{{ error }}</p>
      </div>
    </div>

    <div class="panel" v-if="catalog.validation">
      <h3>口径校验 · {{ catalog.validation.metric }}</h3>
      <div>
        结构 <span class="pill">{{ catalog.validation.structure }}</span>　
        可分解
        <span :class="catalog.validation.decomposable ? 'pill pill--ok' : 'pill pill--warn'">
          {{ catalog.validation.decomposable ? '是' : '否（比率型只展示，不参与分解）' }}
        </span>　
        校验节点 {{ catalog.validation.nodes_checked }} 个　
        <span :class="catalog.validation.ok ? 'pill pill--ok' : 'pill pill--danger'">
          {{ catalog.validation.ok ? '全部出数' : '存在问题' }}
        </span>
      </div>
      <table style="margin-top: 8px">
        <thead>
          <tr><th>节点</th><th>取值</th><th>问题</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in catalog.validation.checks" :key="item.code">
            <td>{{ item.code }}</td>
            <td>{{ item.value === null ? '—' : Math.round(item.value).toLocaleString() }}</td>
            <td class="muted">{{ item.problem ?? '' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
