import { useEffect, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { AppDialog } from '../../components/AppDialog'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatTime } from '../hub/format'
import { WORKFLOW_STAGES, batchStatusMeta } from './format'
import { useBatchActions, useBatchManagement } from './hooks'
import type { MaterialBatch } from './types'

type ConfirmKind = 'delete' | 'purge' | 'clear'

interface BatchManagementDialogProps {
  open: boolean
  selectedBatchId: string
  onOpenChange: (open: boolean) => void
  onSelectBatch: (batchId: string) => void
}

/** 批次回收站与阶段维护：所有破坏性操作都先展示影响范围与阻断原因。 */
export function BatchManagementDialog({
  open,
  selectedBatchId,
  onOpenChange,
  onSelectBatch,
}: BatchManagementDialogProps) {
  const [showDeleted, setShowDeleted] = useState(false)
  const query = useBatchManagement(showDeleted, open)
  const [editing, setEditing] = useState<MaterialBatch | null>(null)
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; batch: MaterialBatch } | null>(null)

  const batches = query.data ?? []

  return (
    <>
      <AppDialog
        open={open}
        onOpenChange={onOpenChange}
        title="批次管理"
        description="编辑名称与阶段、移入回收站或彻底删除。已发布或有任务引用的批次会显示阻断原因。"
        widthClass="w-[min(880px,94vw)]"
        footer={
          <div className="flex justify-end">
            <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
              关闭
            </Button>
          </div>
        }
      >
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Checkbox
              label="显示回收站批次"
              checked={showDeleted}
              onCheckedChange={(next) => setShowDeleted(next)}
            />
            <Button color="secondary" variant="outline" size="sm" onClick={() => void query.refetch()}>
              刷新
            </Button>
          </div>

          {query.isError ? (
            <StatePanel variant="error" title="批次列表读取失败" description={errorMessage(query.error)} />
          ) : query.isLoading ? (
            <StatePanel variant="loading" title="正在读取批次…" />
          ) : batches.length === 0 ? (
            <StatePanel
              variant="empty"
              title={showDeleted ? '回收站为空' : '还没有素材批次'}
              description={showDeleted ? '移入回收站的批次会显示在这里。' : '新建批次后即可上传或导入素材。'}
            />
          ) : (
            <ul className="divide-y divide-subtle rounded-xl border border-subtle">
              {batches.map((batch) => (
                <ManagementRow
                  key={batch.batch_id}
                  batch={batch}
                  selected={batch.batch_id === selectedBatchId}
                  onSelect={() => {
                    onSelectBatch(batch.batch_id)
                    onOpenChange(false)
                  }}
                  onEdit={() => setEditing(batch)}
                  onRequestConfirm={(kind) => setConfirm({ kind, batch })}
                />
              ))}
            </ul>
          )}
        </div>
      </AppDialog>

      {editing ? <EditBatchDialog batch={editing} onClose={() => setEditing(null)} /> : null}

      {confirm ? <ManagementConfirm target={confirm} onClose={() => setConfirm(null)} /> : null}
    </>
  )
}

function ManagementRow({
  batch,
  selected,
  onSelect,
  onEdit,
  onRequestConfirm,
}: {
  batch: MaterialBatch
  selected: boolean
  onSelect: () => void
  onEdit: () => void
  onRequestConfirm: (kind: ConfirmKind) => void
}) {
  const toast = useToast()
  const actions = useBatchActions(batch.batch_id)
  const status = batchStatusMeta(batch.status)
  const deleted = Boolean(batch.deleted_at)

  return (
    <li className="space-y-2 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-sm text-default">
            <span className="truncate">{batch.name || batch.batch_id}</span>
            {selected ? <StatusBadge tone="info">当前批次</StatusBadge> : null}
            {batch.current_active ? <StatusBadge tone="success">默认版本在用</StatusBadge> : null}
            {deleted ? <StatusBadge tone="secondary">回收站</StatusBadge> : null}
          </p>
          <p className="mt-0.5 text-2xs text-secondary">
            {batch.workflow.label} · {batch.summary.total_stores} 家门店 · 已选码 {batch.summary.selected_stores}
            {batch.release ? ` · 默认 ${batch.release.version}` : ' · 未发布'}
            {deleted ? ` · 移入于 ${formatTime(batch.deleted_at)}` : ` · 更新于 ${formatTime(batch.updated_at)}`}
          </p>
        </div>
        <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {!deleted ? (
          <div className="w-52">
            <Select
              aria-label="批次阶段"
              value={batch.workflow.manual_stage}
              options={WORKFLOW_STAGES}
              onChange={(option) => {
                actions.setStage.mutate(option.value, {
                  onSuccess: () => toast.push('已更新批次阶段', 'success'),
                  onError: (cause) => toast.push(errorMessage(cause), 'danger'),
                })
              }}
              disabled={actions.setStage.isPending}
            />
          </div>
        ) : null}
        {!deleted ? (
          <Button color="secondary" variant="outline" size="sm" onClick={onSelect} disabled={selected}>
            {selected ? '已选中' : '选择此批次'}
          </Button>
        ) : null}
        {!deleted ? (
          <Button color="secondary" variant="ghost" size="sm" onClick={onEdit}>
            编辑
          </Button>
        ) : null}
        {!deleted ? (
          <span title={batch.can_clear_selection ? undefined : batch.clear_selection_block_reason}>
            <Button
              color="secondary"
              variant="ghost"
              size="sm"
              disabled={!batch.can_clear_selection}
              onClick={() => onRequestConfirm('clear')}
            >
              清除选用状态
            </Button>
          </span>
        ) : null}
        {!deleted ? (
          <span title={batch.can_delete ? undefined : batch.delete_block_reason}>
            <Button
              color="danger"
              variant="outline"
              size="sm"
              disabled={!batch.can_delete}
              onClick={() => onRequestConfirm('delete')}
            >
              移入回收站
            </Button>
          </span>
        ) : (
          <>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={() => {
                actions.restore.mutate(undefined, {
                  onSuccess: () => toast.push('批次已恢复', 'success'),
                  onError: (cause) => toast.push(errorMessage(cause), 'danger'),
                })
              }}
              disabled={actions.restore.isPending}
            >
              恢复
            </Button>
            <span title={batch.can_purge ? undefined : batch.purge_block_reason}>
              <Button
                color="danger"
                variant="solid"
                size="sm"
                disabled={!batch.can_purge}
                onClick={() => onRequestConfirm('purge')}
              >
                彻底删除
              </Button>
            </span>
          </>
        )}
      </div>
      {!deleted && !batch.can_delete && batch.delete_block_reason ? (
        <p className="text-2xs text-tertiary">不可删除：{batch.delete_block_reason}</p>
      ) : null}
      {deleted && !batch.can_purge && batch.purge_block_reason ? (
        <p className="text-2xs text-tertiary">不可彻底删除：{batch.purge_block_reason}</p>
      ) : null}
    </li>
  )
}

function EditBatchDialog({ batch, onClose }: { batch: MaterialBatch; onClose: () => void }) {
  const toast = useToast()
  const actions = useBatchActions(batch.batch_id)
  const [name, setName] = useState(batch.name)
  const [description, setDescription] = useState(batch.description)
  const [error, setError] = useState('')

  useEffect(() => {
    setName(batch.name)
    setDescription(batch.description)
    setError('')
  }, [batch])

  const submit = () => {
    if (!name.trim()) {
      setError('请填写批次名称')
      return
    }
    actions.update.mutate(
      { name: name.trim(), description: description.trim() },
      {
        onSuccess: () => {
          toast.push('已保存批次信息', 'success')
          onClose()
        },
        onError: (cause) => setError(errorMessage(cause)),
      },
    )
  }

  return (
    <AppDialog
      open
      onOpenChange={(next) => {
        if (!next && !actions.update.isPending) onClose()
      }}
      title="编辑批次"
      widthClass="w-[min(520px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={onClose} disabled={actions.update.isPending}>
            取消
          </Button>
          <Button color="primary" size="sm" onClick={submit} disabled={actions.update.isPending}>
            {actions.update.isPending ? '保存中…' : '保存'}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        <label className="block space-y-1 text-sm text-default">
          <span>批次名称</span>
          <Input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} />
        </label>
        <label className="block space-y-1 text-sm text-default">
          <span>说明</span>
          <Input value={description} maxLength={200} onChange={(event) => setDescription(event.target.value)} />
        </label>
        {error ? <Alert color="danger" variant="soft" title={error} /> : null}
      </div>
    </AppDialog>
  )
}

function ManagementConfirm({
  target,
  onClose,
}: {
  target: { kind: ConfirmKind; batch: MaterialBatch }
  onClose: () => void
}) {
  const toast = useToast()
  const actions = useBatchActions(target.batch.batch_id)
  const [error, setError] = useState('')
  const mutation =
    target.kind === 'delete' ? actions.remove : target.kind === 'purge' ? actions.purge : actions.clearSelection
  const busy = mutation.isPending

  const copy = {
    delete: {
      title: `移入回收站：${target.batch.name}`,
      body: '批次会从默认列表移除，但素材文件、已发布快照与任务记录都会保留，可在回收站中恢复。',
      confirm: '确认移入回收站',
    },
    purge: {
      title: `彻底删除：${target.batch.name}`,
      body: '将永久删除该批次的素材索引与门店决策，且无法恢复。仅在批次没有发布版本、快照或任务引用时允许执行。',
      confirm: '确认彻底删除',
    },
    clear: {
      title: `清除选用状态：${target.batch.name}`,
      body: '清除后共享后台不再将该批次视为当前选用版本，新任务会按其他已发布版本分配。',
      confirm: '确认清除',
    },
  }[target.kind]

  const run = () => {
    mutation.mutate(undefined, {
      onSuccess: () => {
        toast.push(target.kind === 'purge' ? '批次已彻底删除' : target.kind === 'delete' ? '批次已移入回收站' : '已清除选用状态', 'success')
        onClose()
      },
      onError: (cause) => setError(errorMessage(cause)),
    })
  }

  return (
    <ConfirmActionDialog
      open
      title={copy.title}
      description={copy.body}
      confirmLabel={copy.confirm}
      tone="danger"
      busy={busy}
      error={error}
      onOpenChange={(next) => {
        if (!next && !busy) onClose()
      }}
      onConfirm={run}
    />
  )
}
