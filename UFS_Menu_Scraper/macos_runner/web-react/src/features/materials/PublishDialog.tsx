import { useEffect, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { AppDialog } from '../../components/AppDialog'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatTime } from '../hub/format'
import { useBatchActions } from './hooks'
import type { MaterialBatchDetail } from './types'

interface PublishDialogProps {
  open: boolean
  batch: MaterialBatchDetail | null
  onOpenChange: (open: boolean) => void
  onPublished: () => void
}

/** 发布批次快照并管理版本：发布不自动合并主线，激活需二次确认。 */
export function PublishDialog({ open, batch, onOpenChange, onPublished }: PublishDialogProps) {
  const toast = useToast()
  const actions = useBatchActions(batch?.batch_id ?? '')
  const [name, setName] = useState(batch?.name ?? '')
  const [version, setVersion] = useState(batch?.suggested_version ?? 'v1.0.0')
  const [description, setDescription] = useState('')
  const [error, setError] = useState('')
  const [activating, setActivating] = useState<string | null>(null)

  const busy = actions.publish.isPending

  // 弹窗保持挂载：每次打开或批次变化时，用最新批次信息重置表单。
  useEffect(() => {
    if (!open || !batch) return
    setName(batch.name)
    setVersion(batch.suggested_version || 'v1.0.0')
    setDescription('')
    setError('')
  }, [open, batch])

  const submit = () => {
    if (!batch) return
    if (!name.trim()) {
      setError('请填写批次名称')
      return
    }
    if (!/^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/.test(version.trim())) {
      setError('版本号请使用 v1.0.0 格式')
      return
    }
    setError('')
    actions.publish.mutate(
      { name: name.trim(), version: version.trim(), description: description.trim() },
      {
        onSuccess: (result) => {
          toast.push(
            result.already_published
              ? `版本 ${result.release.version} 已是最新发布内容`
              : `已发布 ${result.release.version}，新增任务 ${result.task_scan.added} 家`,
            'success',
          )
          setDescription('')
          onPublished()
        },
        onError: (cause) => setError(errorMessage(cause)),
      },
    )
  }

  return (
    <>
      <AppDialog
        open={open}
        onOpenChange={(next) => {
          if (!busy) {
            onOpenChange(next)
            if (!next) setError('')
          }
        }}
        title={`发布批次：${batch?.name ?? ''}`}
        description="发布会在当前选码结果上生成新快照；是否成为默认版本在下方单独控制。"
        widthClass="w-[min(720px,94vw)]"
        footer={
          <div className="flex justify-end gap-2">
            <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={busy}>
              关闭
            </Button>
            <Button color="primary" size="sm" onClick={submit} disabled={busy || !batch}>
              {busy ? '正在发布…' : '发布新版本'}
            </Button>
          </div>
        }
      >
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm text-default">
              <span>批次名称</span>
              <Input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} disabled={busy} />
            </label>
            <label className="space-y-1 text-sm text-default">
              <span>版本号</span>
              <Input
                value={version}
                maxLength={40}
                placeholder="v1.0.0"
                onChange={(event) => setVersion(event.target.value)}
                disabled={busy}
              />
              {batch?.suggested_version ? (
                <span className="block text-2xs text-tertiary">建议版本：{batch.suggested_version}</span>
              ) : null}
            </label>
          </div>
          <label className="block space-y-1 text-sm text-default">
            <span>版本描述（可选）</span>
            <Input
              value={description}
              maxLength={200}
              onChange={(event) => setDescription(event.target.value)}
              disabled={busy}
            />
          </label>

          {batch ? (
            <div className="rounded-lg border border-subtle bg-surface-secondary p-3 text-xs text-secondary">
              <p>
                当前选中 {batch.summary.selected_stores} / {batch.summary.total_stores} 家门店
                {batch.draft_changes
                  ? ` · 较当前版本新增 ${batch.draft_changes.added}、移除 ${batch.draft_changes.removed}、变更 ${batch.draft_changes.changed}`
                  : ''}
              </p>
              {batch.release ? (
                <p className="mt-1">
                  当前默认版本：{batch.release.version}（{formatTime(batch.release.published_at)} 发布）
                </p>
              ) : (
                <p className="mt-1">该批次尚未发布任何版本。</p>
              )}
            </div>
          ) : null}

          {error ? <Alert color="danger" variant="soft" title={error} /> : null}

          <section className="space-y-2" aria-label="历史版本">
            <h3 className="heading-xs">历史版本</h3>
            {!batch || batch.releases.length === 0 ? (
              <p className="text-xs text-secondary">暂无发布记录。</p>
            ) : (
              <ul className="divide-y divide-subtle rounded-lg border border-subtle">
                {batch.releases.map((release) => (
                  <li key={release.release_id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                    <div className="min-w-0">
                      <p className="flex items-center gap-2 text-sm text-default">
                        {release.version}
                        {release.active ? <StatusBadge tone="success">默认版本</StatusBadge> : null}
                      </p>
                      <p className="text-2xs text-secondary">
                        {formatTime(release.published_at)} · {release.store_count} 家门店
                        {release.description ? ` · ${release.description}` : ''}
                      </p>
                    </div>
                    {release.active ? null : (
                      <Button
                        color="secondary"
                        variant="outline"
                        size="sm"
                        onClick={() => setActivating(release.version)}
                      >
                        设为默认
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </AppDialog>

      <ConfirmActionDialog
        open={Boolean(activating)}
        title={`切换默认版本：${activating ?? ''}`}
        description={
          <div className="space-y-1">
            <p>切换后，新分配与重新下载的门店会使用该版本的二维码快照。</p>
            <p>已分配但尚未同步的任务可能收到版本冲突提示，需要重新分配。</p>
          </div>
        }
        confirmLabel="确认切换"
        busy={actions.activate.isPending}
        onOpenChange={(next) => {
          if (!next && !actions.activate.isPending) setActivating(null)
        }}
        onConfirm={() => {
          if (!activating) return
          actions.activate.mutate(activating, {
            onSuccess: () => {
              toast.push(`已切换默认版本 ${activating}`, 'success')
              setActivating(null)
              onPublished()
            },
            onError: (cause) => toast.push(errorMessage(cause), 'danger'),
          })
        }}
      />
    </>
  )
}
