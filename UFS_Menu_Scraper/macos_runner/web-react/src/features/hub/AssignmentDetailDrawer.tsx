import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Image } from '@openai/apps-sdk-ui/components/Image'
import { AppDrawer } from '../../components/AppDrawer'
import { CellText } from '../../components/CellText'
import { ImagePreview } from '../../components/ImagePreview'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { errorMessage } from '../../api/client'
import { fetchResultFiles, resultFileUrl } from './api'
import { assignmentStatusOf, formatStore } from './format'
import type { AssignmentRecord } from './types'

interface AssignmentDetailDrawerProps {
  record: AssignmentRecord | null
  onOpenChange: (open: boolean) => void
  onReclaim?: (record: AssignmentRecord) => void
}

const RECLAIMABLE = new Set(['assigned', 'ready'])

/** 分配详情：状态、设备、结果文件与回收影响范围。结果文件只在后端确有记录时请求。 */
export function AssignmentDetailDrawer({ record, onOpenChange, onReclaim }: AssignmentDetailDrawerProps) {
  const [preview, setPreview] = useState<{ src: string; alt: string } | null>(null)
  const hasResult = Boolean(record && record.disposition && record.disposition !== '')

  const filesQuery = useQuery({
    queryKey: ['hub-assignment-files', record?.id],
    queryFn: ({ signal }) => fetchResultFiles(record?.id ?? '', signal),
    enabled: Boolean(record && hasResult),
    retry: false,
    staleTime: 60_000,
  })

  const images = useMemo(
    () => (filesQuery.data?.files ?? []).filter((file) => file.toLowerCase().endsWith('.png')),
    [filesQuery.data],
  )
  const jsonFiles = useMemo(
    () => (filesQuery.data?.files ?? []).filter((file) => file.toLowerCase().endsWith('.json')),
    [filesQuery.data],
  )
  // 证据按结果目录顺序连续浏览，避免每次只看一张后重新打开。
  const previewItems = useMemo(
    () => (record ? images.map((file) => ({ src: resultFileUrl(record.id, file), alt: file })) : []),
    [images, record],
  )

  if (!record) return null
  const store = formatStore(record.store)
  const meta = assignmentStatusOf(record.status, record.disposition)
  const reclaimable = RECLAIMABLE.has(record.status) && record.disposition !== 'history'

  return (
    <AppDrawer
      open={Boolean(record)}
      onOpenChange={onOpenChange}
      title={store.name || record.store}
      description={[store.code, record.batch_name, record.revision].filter(Boolean).join(' · ')}
      footer={
        reclaimable && onReclaim ? (
          <div className="space-y-2">
            <Button color="danger" size="sm" onClick={() => onReclaim(record)}>
              回收这条分配
            </Button>
            <p className="text-xs text-tertiary">
              回收后门店立即解除占用，可重新分配；设备已下载但未上传的结果会转为历史留档。
            </p>
          </div>
        ) : (
          <p className="text-xs text-tertiary">
            {!onReclaim ? '此入口仅用于查看分配和结果。' : record.disposition === 'history'
              ? '该结果已转为历史留档，只读保留。'
              : meta.label === '已收回'
                ? '这条分配已回收，门店可重新分配。'
                : '已完成并确认的分配不可回收。'}
          </p>
        )
      }
    >
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
          <span className="text-xs text-secondary">分配给 {record.worker_name || record.worker_id}</span>
        </div>

        <dl className="grid grid-cols-1 gap-x-4 gap-y-3 text-sm sm:grid-cols-2">
          <Fact label="结果处置">{record.disposition === 'history' ? '历史留档' : record.disposition || '未上传'}</Fact>
          <Fact label="任务 ID">
            <CellText copyable copyLabel="任务 ID" mono>{record.task_id}</CellText>
          </Fact>
          <Fact label="分配 ID">
            <CellText copyable copyLabel="分配 ID" mono>{record.id}</CellText>
          </Fact>
        </dl>

        {record.result_message ? (
          <div>
            <h3 className="heading-xs">结果说明</h3>
            <p className="mt-1 whitespace-pre-wrap rounded-lg bg-surface-secondary px-3 py-2 text-sm text-secondary">
              {record.result_message}
            </p>
          </div>
        ) : null}

        <section className="space-y-2">
          <h3 className="heading-xs">采集结果</h3>
          {!hasResult ? (
            <p className="rounded-lg bg-surface-secondary px-3 py-2 text-xs text-secondary">
              设备尚未上传结果；分配状态为“{meta.label}”。
            </p>
          ) : filesQuery.isError ? (
            <Alert
              color="danger"
              variant="soft"
              title="结果文件读取失败"
              description={errorMessage(filesQuery.error)}
            />
          ) : filesQuery.isLoading ? (
            <StatePanel variant="loading" title="正在读取结果文件…" />
          ) : filesQuery.data && filesQuery.data.files.length === 0 ? (
            <p className="rounded-lg bg-surface-secondary px-3 py-2 text-xs text-secondary">结果目录存在，但没有图片或 JSON 文件。</p>
          ) : (
            <div className="space-y-3">
              {images.length ? (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {images.map((file) => {
                    const src = resultFileUrl(record.id, file)
                    return (
                      <button
                        key={file}
                        type="button"
                        onClick={() => setPreview({ src, alt: file })}
                        className="overflow-hidden rounded-lg border border-subtle bg-surface-secondary text-left transition-colors hover:bg-secondary-ghost-hover"
                        title={file}
                      >
                        <Image src={src} alt={file} className="aspect-square w-full object-cover" />
                        <span className="block truncate px-2 py-1 text-2xs text-tertiary">{file}</span>
                      </button>
                    )
                  })}
                </div>
              ) : null}
              {jsonFiles.length ? (
                <ul className="space-y-1 text-xs">
                  {jsonFiles.map((file) => (
                    <li key={file}>
                      <a
                        className="break-all text-default underline-offset-2 hover:underline"
                        href={resultFileUrl(record.id, file)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {file}
                      </a>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          )}
        </section>
      </div>

      <ImagePreview
        open={Boolean(preview)}
        src={preview?.src ?? ''}
        alt={preview?.alt ?? ''}
        {...(preview ? { caption: `${store.name || record.store} · ${preview.alt}` } : {})}
        items={previewItems}
        initialIndex={preview ? Math.max(0, images.indexOf(preview.alt)) : 0}
        onOpenChange={(open) => {
          if (!open) setPreview(null)
        }}
      />
    </AppDrawer>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-tertiary">{label}</dt>
      <dd className="mt-0.5 text-default">{children}</dd>
    </div>
  )
}
