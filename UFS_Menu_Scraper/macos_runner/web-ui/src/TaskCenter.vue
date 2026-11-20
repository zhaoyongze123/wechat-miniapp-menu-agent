<template>
  <section class="vtc" aria-label="任务中心">
    <section v-if="isHub" class="vtc-devices" aria-labelledby="vtc-devices-heading">
      <div class="vtc-devices-head">
        <h2 id="vtc-devices-heading">采集电脑最近上报</h2>
        <el-button text size="small" @click="openRecords">查看分配与同步</el-button>
      </div>
      <ul v-if="devices.length" class="vtc-device-list">
        <li
          v-for="device in devices"
          :key="device.id"
          class="vtc-device"
          tabindex="0"
          role="button"
          :aria-label="`查看 ${device.name} 的分配记录`"
          @click="openDevice(device)"
          @keydown.enter.prevent="openDevice(device)"
          @keydown.space.prevent="openDevice(device)"
        >
          <i class="vtc-dot" :class="{ live: device.online }" aria-hidden="true"></i>
          <span class="vtc-device-name">{{ device.name }}</span>
          <span class="vtc-device-state">{{ device.state }}</span>
          <span class="vtc-device-meta">{{ device.assignmentText }}</span>
          <span class="vtc-device-pending">{{ device.pendingText }}</span>
        </li>
      </ul>
      <p v-else class="vtc-muted">配对后可以在此查看采集电脑上报的运行进度。</p>
    </section>

    <div class="vtc-stats" aria-live="polite">
      <div class="vtc-stat"><span>全部任务</span><strong>{{ counts.total }}</strong></div>
      <div class="vtc-stat"><span>未结束</span><strong>{{ counts.running }}</strong></div>
      <div class="vtc-stat"><span>已完成</span><strong>{{ counts.completed }}</strong></div>
      <div class="vtc-stat-note">已终止 {{ counts.terminated }}</div>
      <div class="vtc-stat-note">失败/取消 {{ counts.failed }}</div>
    </div>

    <section class="vtc-card" aria-labelledby="vtc-history-heading">
      <div class="vtc-toolbar">
        <div class="vtc-toolbar-copy">
          <h2 id="vtc-history-heading">任务历史</h2>
          <p class="vtc-desc">{{ scopeText }}</p>
        </div>
        <div class="vtc-actions">
          <el-button @click="openPanel">运行面板</el-button>
          <el-button :loading="loading" @click="load()">刷新</el-button>
          <el-button text @click="switchVersion">回切旧版</el-button>
        </div>
      </div>

      <div class="vtc-filters">
        <el-select v-model="kind" aria-label="任务类型" @change="reload">
          <el-option label="全部类型" value="all" />
          <el-option label="采集运行" value="capture" />
          <el-option label="素材分析" value="materials" />
        </el-select>
        <el-select v-model="status" aria-label="任务状态" @change="reload">
          <el-option label="全部状态" value="all" />
          <el-option label="未结束" value="running" />
          <el-option label="已终止" value="terminated" />
          <el-option label="已完成" value="completed" />
          <el-option label="失败/取消" value="failed" />
          <el-option label="已暂停" value="paused" />
        </el-select>
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索任务名、批次或任务 ID"
          aria-label="搜索历史任务"
          @input="onSearch"
          @clear="reload"
        />
      </div>

      <div ref="tableWrap" v-loading="loading" class="vtc-table-wrap" element-loading-text="正在读取历史任务…">
        <div v-if="loadError" class="vtc-empty" role="alert">
          <strong>任务历史读取失败</strong>
          <span>{{ loadError }}</span>
          <el-button size="small" @click="load()">重试</el-button>
        </div>
        <table v-else class="vtc-table">
          <thead>
            <tr>
              <th scope="col" class="vtc-col-title">任务名称</th>
              <th scope="col" class="vtc-col-batch">批次范围</th>
              <th scope="col" class="vtc-col-status">任务状态</th>
              <th scope="col" class="vtc-col-result">结果摘要</th>
              <th scope="col" class="vtc-time">开始时间</th>
              <th scope="col" class="vtc-col-action">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="record in records" :key="`${record.kind}:${record.id}`">
              <tr>
                <td class="vtc-col-title">
                  <strong class="vtc-title" :title="`${displayTitle(record)} · ${record.id}`">
                    {{ displayTitle(record) }}
                  </strong>
                  <span v-if="showKindTag" class="vtc-kind" :class="record.kind">{{ kindLabel(record.kind) }}</span>
                </td>
                <td class="vtc-col-batch">
                  <button
                    v-if="batchList(record).length > 1"
                    type="button"
                    class="vtc-batch-toggle"
                    :aria-expanded="isExpanded(record)"
                    @click="toggleExpand(record)"
                  >
                    <span>{{ batchLabel(record) }}</span>
                    <span class="vtc-caret" :class="{ open: isExpanded(record) }" aria-hidden="true">⌄</span>
                  </button>
                  <template v-else-if="batchList(record).length === 1">
                    <span class="vtc-batch" :title="batchTitle(record)">{{ batchList(record)[0].name }}</span>
                    <span class="vtc-versions">
                      <span
                        v-for="version in batchList(record)[0].versions || []"
                        :key="version"
                        class="vtc-version"
                      >{{ version }}</span>
                    </span>
                  </template>
                  <span v-else-if="record.batch_name" class="vtc-batch" :title="record.batch_name">
                    {{ record.batch_name }}
                  </span>
                  <span v-else class="vtc-empty-cell">—</span>
                </td>
                <td class="vtc-col-status">
                  <span class="vtc-badge" :class="`tone-${statusOf(record.status).tone}`">
                    {{ statusOf(record.status).label }}
                  </span>
                </td>
                <td class="vtc-col-result">
                  <div v-if="record.kind === 'capture'" class="vtc-result">
                    <span>已处理 {{ record.counts?.recorded || 0 }} / {{ record.counts?.total || 0 }}</span>
                    <span>
                      已截图待复核 {{ record.counts?.done || 0 }} · 待检查 {{ record.counts?.attention || 0 }} ·
                      不合格 {{ record.counts?.rejected || 0 }}
                    </span>
                    <span v-if="record.counts?.unknown" class="vtc-note">
                      旧记录未保存当轮结果（{{ record.counts.unknown }} 家）
                    </span>
                  </div>
                  <div v-else class="vtc-result">
                    <span>已分析 {{ record.counts?.done || 0 }} / {{ record.counts?.total || 0 }}</span>
                    <span v-if="record.counts?.failed" class="vtc-note">分析失败 {{ record.counts.failed }}</span>
                  </div>
                </td>
                <td class="vtc-time">{{ formatTime(record.created_at) }}</td>
                <td class="vtc-col-action">
                  <el-button size="small" @click="openDetail(record)">详情</el-button>
                </td>
              </tr>
              <tr v-if="isExpanded(record)" class="vtc-expand-row">
                <td colspan="6">
                  <ul class="vtc-batch-detail">
                    <li v-for="batch in batchList(record)" :key="batch.id">
                      <span class="vtc-batch-name">{{ batch.name }}</span>
                      <span v-for="version in batch.versions || []" :key="version" class="vtc-version">
                        {{ version }}
                      </span>
                      <span class="vtc-batch-stores">{{ batch.stores }} 家门店</span>
                    </li>
                  </ul>
                </td>
              </tr>
            </template>
            <tr v-if="!records.length">
              <td colspan="6">
                <div class="vtc-empty">
                  <strong>{{ hasFilters ? '没有符合条件的任务' : '还没有任务记录' }}</strong>
                  <span>{{ hasFilters ? '调整类型、状态或搜索关键词后再试。' : '开始一次采集或素材分析后，这里会按时间倒序列出。' }}</span>
                  <el-button v-if="hasFilters" size="small" text @click="clearFilters">清除筛选</el-button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="vtc-pager">
        <span class="vtc-pager-total">共 {{ counts.total }} 条</span>
        <el-pagination
          background
          layout="prev, pager, next"
          :total="counts.total"
          :page-size="PAGE_SIZE"
          :current-page="page"
          :pager-count="7"
          @current-change="onPage"
        />
      </div>
    </section>

    <TaskDetailDialog
      v-model="detailOpen"
      :bridge="bridge"
      :record="selected"
      @renamed="onRenamed"
    />
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import TaskDetailDialog from './components/TaskDetailDialog.vue'
import { KIND_LABEL, statusOf } from './lib/constants'
import { formatTime } from './lib/format'

const props = defineProps({
  bridge: { type: Object, required: true },
})

const PAGE_SIZE = 20
const ONLINE_MS = 60 * 1000

const role = ref('')
const kind = ref('all')
const status = ref('all')
const keyword = ref('')
const page = ref(1)
const records = ref([])
const summary = ref({})
const loading = ref(false)
const loadError = ref('')
const devices = ref([])
const selected = ref(null)
const detailOpen = ref(false)
const tableWrap = ref(null)
const expanded = ref(new Set())

let loadSeq = 0
let searchTimer = null
let hubTimer = null
let savedScroll = 0

const counts = computed(() => ({
  total: Number(summary.value.total) || 0,
  running: Number(summary.value.running) || 0,
  completed: Number(summary.value.completed) || 0,
  failed: Number(summary.value.failed) || 0,
  terminated: Number(summary.value.terminated) || 0,
}))
const isHub = computed(() => role.value === 'hub')
const hasFilters = computed(() => kind.value !== 'all' || status.value !== 'all' || keyword.value.trim() !== '')
// 混排时才需要类型标签；筛选单一类型后名称列不再重复说明。
const showKindTag = computed(() => kind.value === 'all')
const scopeText = computed(() => {
  const base = isHub.value
    ? '下表统计后台素材分析和后台保留的采集运行记录。'
    : '按开始时间倒序查看本机任务。'
  return hasFilters.value
    ? `${base}统计卡只计算当前筛选命中的 ${counts.value.total} 条任务。`
    : base
})

function kindLabel(value) {
  return KIND_LABEL[value] || value
}

function displayTitle(record) {
  return record.title || record.id
}

function batchList(record) {
  return Array.isArray(record.batches) ? record.batches : []
}

function batchLabel(record) {
  return `涉及 ${batchList(record).length} 个批次`
}

function batchTitle(record) {
  return batchList(record)
    .map(batch => [batch.name, ...(batch.versions || [])].filter(Boolean).join(' · '))
    .join('，')
}

function recordKey(record) {
  return `${record.kind}:${record.id}`
}

function isExpanded(record) {
  return expanded.value.has(recordKey(record))
}

function toggleExpand(record) {
  const next = new Set(expanded.value)
  const key = recordKey(record)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expanded.value = next
}

async function load(reset = false) {
  if (reset) page.value = 1
  const requestId = ++loadSeq
  loading.value = true
  loadError.value = ''
  const params = new URLSearchParams({
    kind: kind.value,
    status: status.value === 'all' ? '' : status.value,
    q: keyword.value.trim(),
    page: String(page.value),
    page_size: String(PAGE_SIZE),
  })
  try {
    const data = await props.bridge.api(`/api/tasks/history?${params.toString()}`)
    // 筛选条件变化会发起新请求，旧响应直接丢弃，避免列表和统计错位。
    if (requestId !== loadSeq) return
    records.value = Array.isArray(data.records) ? data.records : []
    summary.value = data.summary || {}
    page.value = Number(data.page) || 1
    // 列表刷新后旧的展开状态不再对应同一行，直接收起。
    expanded.value = new Set()
  } catch (error) {
    if (requestId !== loadSeq) return
    records.value = []
    loadError.value = error?.message || '请求失败'
  } finally {
    if (requestId === loadSeq) loading.value = false
  }
}

function reload() {
  void load(true)
}

function onSearch() {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => void load(true), 220)
}

function onPage(next) {
  page.value = Number(next) || 1
  void load()
}

function clearFilters() {
  kind.value = 'all'
  status.value = 'all'
  keyword.value = ''
  void load(true)
}

function openDetail(record) {
  // 关闭详情后要回到原来的滚动位置，尤其是从第 2 页下方打开时。
  savedScroll = tableWrap.value?.scrollTop || 0
  selected.value = record
  detailOpen.value = true
}

watch(detailOpen, (open) => {
  if (open) return
  void nextTick(() => {
    if (tableWrap.value) tableWrap.value.scrollTop = savedScroll
  })
})

function onRenamed(updated) {
  if (!updated) return
  records.value = records.value.map(record => (
    record.kind === 'capture' && record.id === updated.id
      ? { ...record, title: updated.title, name: updated.name }
      : record
  ))
  if (selected.value && selected.value.kind === 'capture' && selected.value.id === updated.id) {
    selected.value = { ...selected.value, title: updated.title, name: updated.name }
  }
}

function openPanel() {
  if (role.value === 'worker') props.bridge.setPage?.('overview')
  else window.__taskCenterOpen?.()
}

function switchVersion() {
  if (typeof props.bridge.switchToLegacy === 'function') props.bridge.switchToLegacy()
}

function openDevice(device) {
  props.bridge.sharedWorkbench?.showManager?.(device.id)
}

function openRecords() {
  props.bridge.sharedWorkbench?.showRecords?.()
}

// 共享后台的设备上报来自外壳已有的共享状态轮询，这里只做展示层映射。
function readDevices() {
  const workbench = props.bridge.sharedWorkbench
  if (!workbench || typeof workbench.role !== 'function' || workbench.role() !== 'hub') return []
  const workers = (workbench.workers?.() || []).filter(device => !device.archived_at && !device.revoked_at)
  const assignments = workbench.assignments?.() || []
  return workers.map(worker => {
    const mine = assignments.filter(item => item.worker_id === worker.id && ['assigned', 'ready'].includes(item.status))
    const downloaded = mine.filter(item => item.status === 'ready').length
    const stamp = Date.parse(worker.last_seen || '')
    const online = Number.isFinite(stamp) && Date.now() - stamp < ONLINE_MS
    return {
      id: worker.id,
      name: worker.name || worker.id,
      online,
      state: online ? '在线' : `最后在线 ${formatTime(worker.last_seen)}`,
      assignmentText: `当前分配 ${mine.length} 家 · 已下载 ${downloaded} 家`,
      pendingText: `${online ? '待上传' : '上次报告待上传'} ${Number(worker.pending_uploads) || 0} 家`,
    }
  })
}

function syncHub() {
  const nextRole = props.bridge.sharedWorkbench?.role?.() || ''
  if (nextRole !== role.value) role.value = nextRole
  devices.value = readDevices()
}

onMounted(() => {
  syncHub()
  void load()
  hubTimer = window.setInterval(syncHub, 2000)
})

onBeforeUnmount(() => {
  window.clearInterval(hubTimer)
  window.clearTimeout(searchTimer)
})
</script>
