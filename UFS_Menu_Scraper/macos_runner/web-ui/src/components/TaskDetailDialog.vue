<template>
  <el-dialog
    v-model="visible"
    class="vtc-dialog"
    width="min(760px, 94vw)"
    align-center
    append-to-body
    destroy-on-close
    @closed="reset"
  >
    <template #header>
      <div class="vtc-dialog-head">
        <div class="vtc-dialog-title">
          <h2>{{ title }}</h2>
          <p class="vtc-dialog-meta">
            <span class="vtc-badge" :class="`tone-${statusTone}`">{{ statusText }}</span>
            <span>{{ kindText }}</span>
            <span v-if="record?.created_at">{{ formatTime(record.created_at) }}</span>
          </p>
        </div>
        <div v-if="record?.kind === 'capture'" class="vtc-rename">
          <template v-if="!renaming">
            <el-button size="small" @click="startRename">重命名</el-button>
          </template>
          <template v-else>
            <el-input
              v-model="renameValue"
              size="small"
              maxlength="64"
              show-word-limit
              placeholder="任务名称"
              aria-label="任务名称"
              @keyup.enter="saveName"
            />
            <el-button size="small" type="primary" :loading="savingName" @click="saveName">保存</el-button>
            <el-button size="small" text @click="cancelRename">取消</el-button>
          </template>
        </div>
      </div>
      <p v-if="renameError" class="vtc-error" role="alert">{{ renameError }}</p>
    </template>

    <div class="vtc-dialog-body">
      <el-skeleton v-if="loading" :rows="6" animated />
      <div v-else-if="error" class="vtc-empty" role="alert">
        <strong>详情读取失败</strong>
        <span>{{ error }}</span>
        <el-button size="small" @click="loadDetail">重试</el-button>
      </div>
      <template v-else-if="detail">
        <div v-if="detail.kind === 'capture'" class="vtc-detail">
          <div class="vtc-chips">
            <span>已处理 {{ detail.counts?.recorded || 0 }} / {{ detail.counts?.total || 0 }}</span>
            <span>已截图待复核 {{ detail.counts?.done || 0 }}</span>
            <span>待检查 {{ detail.counts?.attention || 0 }}</span>
            <span>不合格 {{ detail.counts?.rejected || 0 }}</span>
            <span v-if="detail.counts?.unknown" class="vtc-note">{{ detail.counts.unknown }} 家旧记录缺少本轮结果</span>
          </div>
          <p v-if="detail.message" class="vtc-message">{{ detail.message }}</p>
          <div class="vtc-detail-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">门店</th>
                  <th scope="col">批次 / 版本</th>
                  <th scope="col">结果</th>
                  <th scope="col">说明</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="task in detail.tasks || []" :key="task.task_id">
                  <td>{{ storeLabel(task) }}</td>
                  <td>{{ [task.batch_name, task.version].filter(Boolean).join(' · ') || '—' }}</td>
                  <td>{{ task.label || task.status || '未处理' }}</td>
                  <td class="vtc-detail-message">{{ task.reject_reason || task.message || '—' }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="!(detail.tasks || []).length" class="vtc-empty">
              <span>没有门店明细。</span>
            </div>
          </div>
        </div>
        <div v-else class="vtc-detail">
          <div class="vtc-chips">
            <span>完成 {{ detail.counts?.done || 0 }} / {{ detail.counts?.total || 0 }}</span>
            <span>失败 {{ detail.counts?.failed || 0 }}</span>
          </div>
          <p v-if="detail.message" class="vtc-message">{{ detail.message }}</p>
          <ul v-if="(detail.events || []).length" class="vtc-events">
            <li v-for="(event, index) in detail.events" :key="index" :class="event.level">
              <time>{{ shortTime(event.time) }}</time>
              <span>{{ event.message }}</span>
            </li>
          </ul>
          <div v-else class="vtc-empty">
            <span>没有更多明细。</span>
          </div>
        </div>
      </template>
    </div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { KIND_LABEL, statusOf } from '../lib/constants'
import { formatTime, shortTime } from '../lib/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  bridge: { type: Object, required: true },
  record: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'renamed'])

const visible = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value),
})

const detail = ref(null)
const loading = ref(false)
const error = ref('')
const renaming = ref(false)
const renameValue = ref('')
const renameError = ref('')
const savingName = ref(false)

const title = computed(() => props.record?.title || props.record?.id || '任务详情')
const kindText = computed(() => KIND_LABEL[props.record?.kind] || props.record?.kind || '')
const statusText = computed(() => statusOf(props.record?.status).label)
const statusTone = computed(() => statusOf(props.record?.status).tone)

function storeLabel(task) {
  const raw = task?.store_name || task?.task_id || ''
  const format = props.bridge?.storeLabel
  if (typeof format === 'function') {
    try {
      return format(raw).name || raw
    } catch {
      return raw
    }
  }
  return raw
}

async function loadDetail() {
  if (!props.record) return
  loading.value = true
  error.value = ''
  try {
    detail.value = await props.bridge.api(
      `/api/tasks/history/${props.record.kind}/${encodeURIComponent(props.record.id)}`,
    )
  } catch (cause) {
    detail.value = null
    error.value = cause?.message || '请求失败'
  } finally {
    loading.value = false
  }
}

function startRename() {
  renameValue.value = props.record?.name || props.record?.title || ''
  renameError.value = ''
  renaming.value = true
}

function cancelRename() {
  // 取消只收起表单，不改动任何已保存名称。
  renaming.value = false
  renameError.value = ''
}

async function saveName() {
  const name = renameValue.value.trim()
  if (!name) {
    renameError.value = '任务名称不能为空'
    return
  }
  if (name.length > 64) {
    renameError.value = '任务名称不能超过 64 个字符'
    return
  }
  savingName.value = true
  renameError.value = ''
  try {
    const updated = await props.bridge.api(
      `/api/tasks/history/capture/${encodeURIComponent(props.record.id)}`,
      { method: 'PATCH', body: JSON.stringify({ name }) },
    )
    detail.value = updated
    renaming.value = false
    emit('renamed', updated)
    ElMessage.success('任务名称已更新')
  } catch (cause) {
    // 保存失败保留原输入，并说明失败原因，方便直接修改后重试。
    renameError.value = cause?.message || '保存失败，请稍后重试'
  } finally {
    savingName.value = false
  }
}

function reset() {
  detail.value = null
  error.value = ''
  loading.value = false
  renaming.value = false
  renameValue.value = ''
  renameError.value = ''
}

watch(
  () => [props.modelValue, props.record?.id, props.record?.kind],
  ([open]) => {
    if (!open) return
    reset()
    void loadDetail()
  },
  { immediate: true },
)
</script>
