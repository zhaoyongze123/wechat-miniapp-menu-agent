import { useMemo, useState, type ChangeEvent } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { AppDialog } from '../../components/AppDialog'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { validateBatchFiles, formatBytes } from './format'
import { useBatchActions, useBatchUpload } from './hooks'
import type { MaterialBatchDetail } from './types'

type ImportMode = 'upload' | 'local'

interface ImportBatchDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (batch: MaterialBatchDetail) => void
}

function defaultName(): string {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  return `素材批次 ${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`
}

/** 新建素材批次：浏览器上传图片/文件夹，或导入服务器上已有的素材目录。 */
export function ImportBatchDialog({ open, onOpenChange, onCreated }: ImportBatchDialogProps) {
  const toast = useToast()
  const upload = useBatchUpload()
  const { createLocal } = useBatchActions('')
  const [mode, setMode] = useState<ImportMode>('upload')
  const [name, setName] = useState(defaultName)
  const [description, setDescription] = useState('')
  const [inputDir, setInputDir] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [error, setError] = useState('')

  const totalBytes = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files])
  const busy = upload.running || createLocal.isPending
  const percent = upload.progress?.totalBytes
    ? Math.min(100, Math.round((upload.progress.bytes / upload.progress.totalBytes) * 100))
    : 0

  const reset = () => {
    setMode('upload')
    setName(defaultName())
    setDescription('')
    setInputDir('')
    setFiles([])
    setError('')
  }

  const close = () => {
    if (busy) return
    onOpenChange(false)
    reset()
  }

  const onFilesPicked = (event: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(event.target.files ?? [])
    setFiles(picked)
    setError('')
    if (!name.trim() && picked.length) {
      const first = (picked[0] as File & { webkitRelativePath?: string }).webkitRelativePath
      setName(first?.split('/')[0] || '素材批次')
    }
  }

  const submitUpload = async () => {
    const validation = validateBatchFiles(files)
    if (validation) {
      setError(validation)
      return
    }
    if (!name.trim()) {
      setError('请填写批次名称')
      return
    }
    try {
      const batch = await upload.run(files, { name: name.trim(), description: description.trim() })
      toast.push(`已上传 ${files.length} 张图片`, 'success')
      onCreated(batch)
      onOpenChange(false)
      reset()
    } catch (cause) {
      setError(errorMessage(cause))
    }
  }

  const submitLocal = () => {
    if (!inputDir.trim()) {
      setError('请填写服务器素材目录')
      return
    }
    if (!name.trim()) {
      setError('请填写批次名称')
      return
    }
    createLocal.mutate(
      { input_dir: inputDir.trim(), name: name.trim(), description: description.trim() },
      {
        onSuccess: (batch) => {
          toast.push('已导入本地素材目录', 'success')
          onCreated(batch)
          onOpenChange(false)
          reset()
        },
        onError: (cause) => setError(errorMessage(cause)),
      },
    )
  }

  return (
    <AppDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close()
      }}
      busy={busy}
      title="新建素材批次"
      widthClass="w-[min(640px,94vw)]"
      footer={
        <div className="flex justify-end gap-2">
          <Button color="secondary" variant="outline" size="sm" onClick={close} disabled={busy}>
            取消
          </Button>
          {upload.running ? (
            <Button color="danger" variant="outline" size="sm" onClick={upload.cancel}>
              取消上传
            </Button>
          ) : (
            <Button
              color="primary"
              size="sm"
              onClick={() => void (mode === 'upload' ? submitUpload() : submitLocal())}
              disabled={busy}
            >
              {mode === 'upload' ? '上传并创建' : '导入目录'}
            </Button>
          )}
        </div>
      }
    >
      <div className="space-y-4">
        <SegmentedControl<ImportMode>
          value={mode}
          onChange={(next) => {
            setMode(next)
            setError('')
          }}
          aria-label="导入方式"
          disabled={busy}
        >
          <SegmentedControl.Option value="upload">上传图片</SegmentedControl.Option>
          <SegmentedControl.Option value="local">服务器目录</SegmentedControl.Option>
        </SegmentedControl>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm text-default">
            <span>批次名称</span>
            <Input value={name} onChange={(event) => setName(event.target.value)} disabled={busy} maxLength={80} />
          </label>
          <label className="space-y-1 text-sm text-default">
            <span>说明（可选）</span>
            <Input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              disabled={busy}
              maxLength={200}
            />
          </label>
        </div>

        {mode === 'upload' ? (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <label className="inline-flex cursor-pointer items-center rounded-lg border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover">
                选择图片
                <input
                  type="file"
                  multiple
                  accept=".jpg,.jpeg,.png,.webp"
                  onChange={onFilesPicked}
                  className="sr-only"
                  disabled={busy}
                />
              </label>
              <label className="inline-flex cursor-pointer items-center rounded-lg border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover">
                选择文件夹
                <input
                  type="file"
                  multiple
                  {...({ webkitdirectory: '' } as Record<string, string>)}
                  onChange={onFilesPicked}
                  className="sr-only"
                  disabled={busy}
                />
              </label>
            </div>
            <p className="text-xs text-secondary">
              {files.length
                ? `已选择 ${files.length} 个文件 · ${formatBytes(totalBytes)}`
                : '支持 JPG / PNG / WEBP，单文件不超过 50 MB，单批次最多 10000 个文件。'}
            </p>
            {upload.progress ? (
              <div className="space-y-1" role="status" aria-live="polite">
                <div className="h-2 overflow-hidden rounded-full bg-surface-secondary">
                  <div className="h-full bg-primary-solid transition-all" style={{ width: `${percent}%` }} />
                </div>
                <p className="text-2xs text-secondary">
                  已上传 {upload.progress.done}/{upload.progress.total} · {formatBytes(upload.progress.bytes)} /{' '}
                  {formatBytes(upload.progress.totalBytes)}
                  {upload.progress.current ? ` · ${upload.progress.current}` : ''}
                </p>
              </div>
            ) : null}
          </div>
        ) : (
          <label className="block space-y-1 text-sm text-default">
            <span>素材目录（服务器绝对路径）</span>
            <Input
              value={inputDir}
              onChange={(event) => setInputDir(event.target.value)}
              placeholder="例如 D:\\menu-agent\\素材\\2026-11-23"
              disabled={busy}
            />
            <span className="block text-2xs text-tertiary">
              要求目录按门店分文件夹且服务端可访问；共享后台会直接从该路径读取，不会复制文件。
            </span>
          </label>
        )}

        {error ? <Alert color="danger" variant="soft" title={error} /> : null}
      </div>
    </AppDialog>
  )
}
