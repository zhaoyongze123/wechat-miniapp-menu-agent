#!/usr/bin/env node
/**
 * 设计体系静态门禁（#58）。
 *
 * 只拦截可静态识别的平行实现：业务页面手写表格、原生选择/滑块、页面私有
 * Metric/Fact/Field，以及通用 UI 硬编码颜色。语义容器、Canvas 与业务标注色
 * 通过白名单按文件、规则、原因登记，禁止整目录放行。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const featuresDir = join(root, 'src/features')

const WHITELIST = [
  {
    file: 'src/features/annotation/AnnotationCanvas.tsx',
    rule: 'hardcoded-color',
    reason: '标注框需覆盖任意门店截图，颜色为业务语义（建议/编辑/只读）且集中定义，不能套用界面文本 Token。',
  },
]

const RULES = [
  {
    id: 'raw-table',
    pattern: /<table\b/,
    message: '业务页面禁止手写 <table>，请复用 components/AppDataTable（需要行内展开时使用 subRow）。',
  },
  {
    id: 'raw-select',
    pattern: /<select\b/,
    message: '业务页面禁止原生 <select>，请复用 @openai/apps-sdk-ui 的 Select。',
  },
  {
    id: 'raw-range',
    pattern: /type=["']range["']/,
    message: '业务页面禁止手写 range，请复用 @openai/apps-sdk-ui 的 Slider。',
  },
  {
    id: 'private-metric',
    pattern: /(?:function|const)\s+(?:Metric|Fact|Field)\b/,
    message: '禁止页面私有 Metric/Fact/Field，请复用 StatCard 或 KeyValueList/KeyValuePair。',
  },
  {
    id: 'hardcoded-color',
    pattern: /(?:#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\(\s*\d)/,
    message: '业务页面禁止硬编码通用 UI 颜色，请使用语义 Token；业务画布色需登记白名单。',
  },
]

function walk(dir) {
  const files = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) files.push(...walk(full))
    else if (/\.(tsx?|jsx?)$/.test(name) && !/\.test\./.test(name) && !/\.stories\./.test(name)) files.push(full)
  }
  return files
}

const violations = []
for (const file of walk(featuresDir)) {
  const rel = relative(root, file)
  const source = readFileSync(file, 'utf8')
  const lines = source.split('\n')
  for (const rule of RULES) {
    const allowed = WHITELIST.some((item) => item.file === rel && item.rule === rule.id)
    if (allowed) continue
    lines.forEach((line, index) => {
      if (rule.pattern.test(line)) {
        violations.push(`${rel}:${index + 1} [${rule.id}] ${rule.message}`)
      }
    })
  }
}

if (violations.length) {
  console.error('设计体系门禁未通过：')
  for (const item of violations) console.error(`- ${item}`)
  process.exit(1)
}
console.log('设计体系门禁通过：未发现手写表格、原生选择/滑块、页面私有展示组件或未登记的通用硬编码颜色。')
