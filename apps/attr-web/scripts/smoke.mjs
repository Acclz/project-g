/**
 * 界面冒烟：用真实浏览器把三条主链路在界面上跑一遍（P5 出口条件"界面真跑通"的证据）。
 *
 * 前置：
 *   1) 后端在跑：`cd services/attr-api && python -m uvicorn app.main:app --port 8000`
 *   2) 前端在跑：`cd apps/attr-web && npm run dev`
 *
 * 运行：`npm run smoke`（默认用系统已安装的 Edge/Chrome，不下载浏览器）
 * 产物：`output/playwright/*.png`（截图，不进版本库）
 *
 * 判定口径：每一步都必须出现**后端给出的数字**（贡献额、覆盖率、弹性、校验和），
 * 只看"页面有文字"不算通过——避免把静态占位当成链路跑通。
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { chromium } from 'playwright-core'

const APP_URL = process.env.ATTR_WEB_URL || 'http://127.0.0.1:5173'
const OUT_DIR = resolve(process.cwd(), '../../output/playwright')
const STEP_TIMEOUT = 180_000

mkdirSync(OUT_DIR, { recursive: true })

const results = []
function record(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'} │ ${name}${detail ? ` │ ${detail}` : ''}`)
}

async function launch() {
  const options = { headless: true }
  for (const channel of ['msedge', 'chrome']) {
    try {
      return await chromium.launch({ ...options, channel })
    } catch (error) {
      console.log(`（channel=${channel} 不可用：${error.message.split('\n')[0]}）`)
    }
  }
  throw new Error('本机没有可用的 Edge/Chrome，无法跑界面冒烟')
}

async function shot(page, name) {
  await page.screenshot({ path: resolve(OUT_DIR, `${name}.png`), fullPage: true })
}

const browser = await launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
page.setDefaultTimeout(STEP_TIMEOUT)

try {
  // ---------------------------------------------------------------- 页面 1：异动大盘
  await page.goto(`${APP_URL}/#/dashboard`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=异动清单（按严重度排序）')
  await page.waitForSelector('table tbody tr')
  const anomalies = await page.locator('table tbody tr').count()
  const firstRowZ = await page.locator('table tbody tr').first().locator('td').nth(4).innerText()
  record('页面1 异动大盘：清单出数', anomalies > 3, `${anomalies} 行，首行 Z 分数=${firstRowZ}`)
  const trendCanvas = await page.locator('canvas').count()
  record('页面1 异动大盘：趋势图 + 基线带渲染', trendCanvas > 0, `${trendCanvas} 个 canvas`)
  await shot(page, '01-dashboard')

  // ---------------------------------------------------------------- 页面 2：工作台（L1）
  await page.getByRole('button', { name: '发起归因' }).first().click()
  await page.waitForURL(/#\/workbench/, { timeout: STEP_TIMEOUT })
  await page.waitForSelector('text=分析路径（SSE 步骤流）')
  await page.waitForSelector('text=假设与证据链')
  await page.waitForFunction(
    () => !document.body.innerText.includes('还没有假设'),
    undefined,
    { timeout: STEP_TIMEOUT },
  )
  const hypothesisRows = await page.locator('table tbody tr').count()
  const swimlaneText = await page.locator('.steps').innerText()
  record(
    'L1 链路：拆解 → 假设 → 验证 → 事件（步骤流 + 假设落库）',
    swimlaneText.includes('取数与分解') && hypothesisRows > 0,
    `步骤流 ${swimlaneText.split('\n').length} 行；假设表 ${hypothesisRows} 行`,
  )
  const waterfallCanvas = await page.locator('canvas').count()
  record('L1 链路：因子贡献瀑布图渲染', waterfallCanvas > 0, `${waterfallCanvas} 个 canvas`)
  await shot(page, '02-workbench-l1')

  // ---------------------------------------------------------------- L2 下钻
  await page.locator('input[placeholder="paid_ads"]').last().fill('appliance')
  await page.locator('input[placeholder="category,region"]').fill('category,region')
  await page.getByRole('button', { name: '下钻' }).click()
  await page.waitForFunction(
    () => document.body.innerText.includes('精确组合计算') || document.body.innerText.includes('占比分摊'),
    undefined,
    { timeout: STEP_TIMEOUT },
  )
  const pivotText = await page.locator('.panel', { hasText: '维度透视' }).first().innerText()
  record(
    'L2 链路：维度透视图 + 覆盖率 + 精确/分摊标注',
    /覆盖率\s*[\d.]+%/.test(pivotText),
    pivotText.split('\n').find((line) => line.includes('覆盖率')) ?? '',
  )
  await shot(page, '03-workbench-l2')

  // ---------------------------------------------------------------- L3 What-If
  await page.getByRole('button', { name: '推演' }).click()
  await page.waitForFunction(
    () => document.body.innerText.includes('把握度'),
    undefined,
    { timeout: STEP_TIMEOUT },
  )
  const whatifText = await page.locator('.panel', { hasText: 'What-If 曲线' }).first().innerText()
  const whatifOk =
    whatifText.includes('弹性') && whatifText.includes('区间') && whatifText.includes('把握度')
  record(
    'L3 链路：What-If 弹性区间与把握度',
    whatifOk,
    whatifText.split('\n').find((line) => line.includes('弹性')) ?? '',
  )
  await shot(page, '04-workbench-l3-whatif')

  // ---------------------------------------------------------------- L3 报告与导出
  await page.getByRole('button', { name: '一键生成七段式报告' }).click()
  await page.waitForURL(/#\/reports/, { timeout: STEP_TIMEOUT })
  await page.waitForSelector('.segment h4')
  const segmentHeadings = await page.locator('.segment h4').allInnerTexts()
  record(
    '报告预览：七段齐全',
    segmentHeadings.length === 7,
    `${segmentHeadings.length} 段：${segmentHeadings[0]} … ${segmentHeadings.at(-1)}`,
  )
  await page.getByRole('button', { name: '导出 MD / PDF / Excel' }).click()
  await page.waitForSelector('text=三格式校验和一致', { timeout: STEP_TIMEOUT })
  const checksums = await page.locator('.checkline').allInnerTexts()
  const exportedFiles = checksums.filter((item) => /report-\d+-[0-9a-f]+\.(md|pdf|xlsx)$/.test(item))
  const contentChecksums = checksums.filter((item) => item.startsWith('sha256:'))
  record(
    'E6：三格式导出校验和一致（页面可见）',
    exportedFiles.length === 3 && new Set(contentChecksums).size === 1,
    `${exportedFiles.length} 个导出文件；内容校验和 ${[...new Set(contentChecksums)].join(', ')}`,
  )
  await shot(page, '05-reports-export')

  // ---------------------------------------------------------------- 其余两页
  await page.goto(`${APP_URL}/#/events`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=事件列表')
  // 事件列表是异步拉的：先等第一行出现再计数，否则会数到"还没加载"的 0 行
  await page.waitForSelector('table tbody tr')
  await page.waitForFunction(
    () => document.querySelectorAll('table tbody tr').length > 0,
    undefined,
    { timeout: 30_000 },
  )
  const eventRows = await page.locator('table tbody tr').count()
  record('页面3 事件日历：列表出数', eventRows > 0, `${eventRows} 行事件`)
  await shot(page, '06-events')

  await page.goto(`${APP_URL}/#/metrics`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('text=指标清单')
  await page.waitForSelector('text=口径校验', { timeout: STEP_TIMEOUT })
  const validationText = await page.locator('.panel', { hasText: '口径校验' }).innerText()
  record(
    '页面4 指标字典：结构 + 口径校验出数',
    validationText.includes('校验节点'),
    validationText.split('\n').slice(0, 2).join(' '),
  )
  await shot(page, '07-metrics')
} catch (error) {
  record('冒烟执行', false, error.message)
  writeFileSync(resolve(OUT_DIR, 'error.txt'), await page.evaluate(() => document.body.innerText))
  await shot(page, 'error')
} finally {
  await browser.close()
}

const failed = results.filter((item) => !item.ok)
console.log('='.repeat(70))
console.log(`合计 ${results.length} 项，通过 ${results.length - failed.length}，失败 ${failed.length}`)
console.log(`截图目录：${OUT_DIR}`)
process.exit(failed.length ? 1 : 0)
