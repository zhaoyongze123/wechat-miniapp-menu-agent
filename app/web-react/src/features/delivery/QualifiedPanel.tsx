import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppDrawer } from '../../components/AppDrawer'
import { AppPagination } from '../../components/AppPagination'
import { CopyButton } from '../../components/CopyButton'
import { FilterToolbar } from '../../components/FilterToolbar'
import { ImagePreview } from '../../components/ImagePreview'
import { KeyValuePair } from '../../components/KeyValueList'
import { SearchInput } from '../../components/SearchInput'
import { StatCard } from '../../components/StatCard'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { fetchTaskArtifacts } from '../worker/api'
import { formatTime } from '../hub/format'
import { formatBytes } from '../settings/format'
import { useDeliveryActions, useDeliveryStatus, useDeliveryStores } from './hooks'
import type { DeliveryState, DeliveryStore } from './types'

const STATE_LABEL: Record<DeliveryState, { label: string; tone: 'success' | 'warning' | 'danger' | 'info' | 'secondary' }> = {
  unconfigured: { label: '未配置', tone: 'warning' },
  initializing: { label: '初始化中', tone: 'info' },
  syncing: { label: '同步中', tone: 'warning' },
  partial: { label: '部分失败', tone: 'warning' },
  synced: { label: '已同步', tone: 'success' },
  error: { label: '不可用', tone: 'danger' },
}

const SOURCE_LABEL: Record<string, string> = {
  shared_results: '共享采集结果',
  output: '历史输出目录',
  legacy_sources: '历史来源素材',
  other: '其他来源',
}

function countText(value: number): string {
  return value.toLocaleString('zh-CN')
}

function hostDirectory(hostPath: string, directory: string): string {
  if (!hostPath) return directory
  return `${hostPath.replace(/[\\/]+$/, '')}\\${directory}`
}

function StoreStatus({ store }: { store: DeliveryStore }) {
  if (store.status === 'ready') return <StatusBadge tone="success">已同步</StatusBadge>
  if (store.status === 'failed') {
    return <span title={store.error || '同步失败'}><StatusBadge tone="danger">同步失败</StatusBadge></span>
  }
  return (
    <span title={store.reason || store.error || '等待同步'}>
      <StatusBadge tone="warning">待同步</StatusBadge>
    </span>
  )
}

function QualifiedStoreDrawer({
  store,
  hostPath,
  onClose,
}: {
  store: DeliveryStore | null
  hostPath: string
  onClose: () => void
}) {
  const [preview, setPreview] = useState<string | null>(null)
  const taskId = store?.task_id ?? ''
  const artifacts = useQuery({
    queryKey: ['delivery-artifacts', taskId],
    queryFn: ({ signal }) => fetchTaskArtifacts(taskId, signal),
    enabled: Boolean(store && taskId),
    retry: false,
  })
  const images = (artifacts.data?.files ?? []).filter((file) => /^page-\d+\.png$/i.test(file))
  const previewItems = images.map((file) => ({
    src: `/api/artifact/${encodeURIComponent(taskId)}/${encodeURIComponent(file)}`,
    alt: file,
  }))

  return (
    <>
      <AppDrawer
        open={Boolean(store)}
        onOpenChange={(open) => { if (!open) onClose() }}
        title={store ? `${store.store_name || store.directory_name} · 合格成品` : ''}
        description="当前有效轮次的完整菜单截图；历史轮次不会混入本目录。"
      >
        {store ? (
          <div className="space-y-5">
            <dl className="grid grid-cols-2 gap-4">
              <KeyValuePair label="门店名称">{store.store_name || store.directory_name}</KeyValuePair>
              <KeyValuePair label="批次">{store.batch_name || '历史存量'}</KeyValuePair>
              <KeyValuePair label="目录名">{store.directory_name}</KeyValuePair>
              <KeyValuePair label="截图数量">{`${store.images} 张`}</KeyValuePair>
              <KeyValuePair label="成品大小">{formatBytes(store.bytes)}</KeyValuePair>
              <KeyValuePair label="最近同步">{store.synced_at ? formatTime(store.synced_at) : '尚未同步'}</KeyValuePair>
              <KeyValuePair label="数据来源">{SOURCE_LABEL[store.source] ?? store.source}</KeyValuePair>
              <KeyValuePair label="同步状态"><StoreStatus store={store} /></KeyValuePair>
            </dl>
            <div>
              <p className="text-xs text-secondary">成品目录</p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <code className="min-w-0 break-all rounded-lg bg-surface-secondary px-2 py-1 text-xs">
                  {hostDirectory(hostPath, store.directory_name)}
                </code>
                <CopyButton value={hostDirectory(hostPath, store.directory_name)} label="门店成品目录" />
              </div>
            </div>
            {store.task_id ? (
              artifacts.isPending ? (
                <p className="text-xs text-tertiary">正在读取截图…</p>
              ) : images.length ? (
                <div className="grid grid-cols-3 gap-2">
                  {images.map((file) => (
                    <button
                      key={file}
                      type="button"
                      className="overflow-hidden rounded-lg border border-subtle"
                      onClick={() => setPreview(file)}
                      aria-label={`查看 ${file}`}
                    >
                      <img
                        src={`/api/artifact/${encodeURIComponent(store.task_id)}/${encodeURIComponent(file)}`}
                        alt={file}
                        loading="lazy"
                        className="h-28 w-full object-cover"
                      />
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-tertiary">暂无可预览截图。</p>
              )
            ) : (
              <p className="text-xs text-tertiary">
                历史存量目录未关联采集任务，无法在线预览；请通过上方路径在文件管理器中查看。
              </p>
            )}
          </div>
        ) : null}
      </AppDrawer>
      <ImagePreview
        open={Boolean(preview)}
        src={preview ? `/api/artifact/${encodeURIComponent(taskId)}/${encodeURIComponent(preview)}` : ''}
        alt={preview ?? ''}
        {...(preview ? { caption: preview } : {})}
        items={previewItems}
        initialIndex={Math.max(0, previewItems.findIndex((item) => item.alt === preview))}
        onOpenChange={(next) => { if (!next) setPreview(null) }}
      />
    </>
  )
}

/** 合格清单：跨批次展示当前有效合格门店与宿主机成品目录同步状态。 */
export function QualifiedPanel() {
  const toast = useToast()
  const statusQuery = useDeliveryStatus()
  const [batchId, setBatchId] = useState('all')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [selected, setSelected] = useState<DeliveryStore | null>(null)
  const storesQuery = useDeliveryStores({ page, size: pageSize, q: query, batchId })
  const actions = useDeliveryActions()

  const status = statusQuery.data
  const stores = storesQuery.data
  if (statusQuery.isPending && !status) {
    return <StatePanel variant="loading" title="正在读取合格成品目录…" />
  }
  if (!status) {
    return (
      <StatePanel
        variant="error"
        title="合格成品目录读取失败"
        description={statusQuery.isError ? errorMessage(statusQuery.error) : '未返回目录状态'}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={() => void statusQuery.refetch()}>
            重试
          </Button>
        }
      />
    )
  }

  const state = STATE_LABEL[status.state] ?? STATE_LABEL.error
  const counts = status.counts
  const inventory = status.inventory
  const total = stores?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const batchOptions = [
    { value: 'all', label: '全部批次' },
    ...(stores?.batches ?? []).map((batch) => ({ value: batch.batch_id, label: batch.name || batch.batch_id })),
  ]
  const columns: DataColumn<DeliveryStore>[] = [
    {
      id: 'store',
      header: '门店名称',
      cell: (row) => (
        <div className="min-w-0">
          <div className="truncate font-medium text-default">{row.store_name || '—'}</div>
          <div className="truncate text-2xs text-tertiary">
            {row.legacy ? SOURCE_LABEL[row.source] : row.batch_name || row.directory_name}
          </div>
        </div>
      ),
    },
    { id: 'batch', header: '批次', width: '12rem', cell: (row) => row.batch_name || '历史存量' },
    { id: 'images', header: '截图', width: '6rem', cell: (row) => `${row.images} 张` },
    { id: 'bytes', header: '大小', width: '7rem', cell: (row) => formatBytes(row.bytes) },
    { id: 'status', header: '同步状态', width: '9rem', cell: (row) => <StoreStatus store={row} /> },
  ]

  const triggerSync = () => {
    actions.sync.mutate(undefined, {
      onSuccess: () => toast.push('已触发重新核对，目录会在后台补齐', 'info'),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const openDirectory = () => {
    actions.reveal.mutate(undefined, {
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  return (
    <div className="space-y-6">
      {status.state === 'unconfigured' ? (
        <Alert color="warning" variant="soft" title="合格成品目录尚未启用" description={status.message} />
      ) : null}
      {status.state === 'error' ? (
        <Alert color="danger" variant="soft" title="合格成品目录当前不可用" description={status.message} />
      ) : null}
      {status.initializing ? (
        <Alert
          color="info"
          variant="soft"
          title="正在初始化历史存量"
          description={`已完成 ${countText(counts.ready)} / ${countText(counts.qualified)} 家门店。初始化期间可正常采集与审核，中断后会自动续传。`}
        />
      ) : null}
      {status.state === 'partial' ? (
        <Alert color="warning" variant="soft" title="部分门店同步失败" description={status.message} />
      ) : null}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="合格门店" value={countText(counts.qualified)} />
        <StatCard label="已同步" value={countText(counts.ready)} tone={counts.failed ? 'warning' : 'success'} />
        <StatCard label="菜单截图" value={countText(counts.images)} />
        <StatCard label="成品大小" value={formatBytes(counts.bytes)} />
      </div>

      <section className="space-y-3 rounded-xl border border-subtle bg-surface px-5 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="heading-sm">合格成品目录</h3>
              <StatusBadge tone={state.tone}>{state.label}</StatusBadge>
            </div>
            <p className="text-xs text-secondary">
              通过文件管理器直接复制至 U 盘或移动硬盘；目录始终由系统自动维护，无需生成或导出。
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button color="secondary" variant="outline" size="sm" onClick={triggerSync} disabled={actions.sync.isPending}>
              {actions.sync.isPending ? '正在触发…' : '重新同步'}
            </Button>
            <Button
              color="secondary"
              variant="outline"
              size="sm"
              onClick={openDirectory}
              disabled={!status.open_supported || actions.reveal.isPending}
              {...(!status.open_supported ? { title: status.open_reason } : {})}
            >
              打开合格目录
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <code className="min-w-0 break-all rounded-lg bg-surface-secondary px-3 py-2 text-xs">
            {status.host_path || '尚未配置宿主机路径'}
          </code>
          <CopyButton value={status.host_path} label="合格目录路径" disabled={!status.host_path} />
        </div>
        {!status.open_supported && status.open_reason ? (
          <p className="text-2xs text-tertiary">{status.open_reason}</p>
        ) : null}
        <p className="text-2xs text-tertiary">
          最近同步：{status.last_sync_at ? formatTime(status.last_sync_at) : '尚未同步'}
          {status.content_digest ? ` · 内容摘要 ${status.content_digest.slice(0, 12)}…` : ''}
        </p>
        <details className="rounded-lg border border-subtle px-3 py-2">
          <summary className="cursor-pointer text-xs text-secondary">初始化核对与排除项</summary>
          <div className="mt-2 space-y-1 text-2xs text-tertiary">
            <p>
              扫描：共享结果 {countText(inventory.discovered.shared_results)} · 历史输出 {' '}
              {countText(inventory.discovered.output)} · 历史来源 {countText(inventory.discovered.legacy_sources)}
              ，纳入 {countText(inventory.included)} 家（历史目录直接纳入 {countText(inventory.legacy_included)} 家）。
            </p>
            {inventory.pending_verification.count ? (
              <p>
                待核验 {countText(inventory.pending_verification.count)} 个历史目录（
                {countText(inventory.pending_verification.stores)} 家）：缺少当前权威业务记录，未纳入交付目录。
              </p>
            ) : null}
            {inventory.excluded.reasons.map((item) => (
              <p key={item.reason}>排除 {countText(item.count)} 家：{item.reason}</p>
            ))}
            {inventory.verified ? (
              <p>
                已核对 {countText(inventory.verified.stores)} 家 / {countText(inventory.verified.images)} 张 /{' '}
                {formatBytes(inventory.verified.bytes)}。
              </p>
            ) : null}
            {status.content_digest ? <p className="break-all">内容摘要：{status.content_digest}</p> : null}
          </div>
        </details>
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h3 className="heading-sm">合格门店</h3>
            <p className="mt-1 text-xs text-secondary">全部批次的当前有效合格门店，平铺在同一成品目录下。</p>
          </div>
          <Button
            color="secondary"
            variant="ghost"
            size="sm"
            onClick={() => {
              void statusQuery.refetch()
              void storesQuery.refetch()
            }}
            disabled={storesQuery.isFetching}
          >
            {storesQuery.isFetching ? '正在刷新…' : '刷新列表'}
          </Button>
        </div>
        <FilterToolbar
          label="合格门店筛选"
          activeCount={(batchId === 'all' ? 0 : 1) + (query ? 1 : 0)}
          onClear={() => {
            setBatchId('all')
            setSearch('')
            setQuery('')
            setPage(1)
          }}
        >
          <div className="w-56">
            <Select
              aria-label="合格批次筛选"
              value={batchId}
              options={batchOptions}
              onChange={(option) => {
                setBatchId(option.value)
                setPage(1)
              }}
            />
          </div>
          <SearchInput
            className="w-64"
            label="合格门店"
            placeholder="搜索门店、批次或目录名"
            value={search}
            onChange={(value) => {
              setSearch(value)
              setQuery(value)
              setPage(1)
            }}
          />
        </FilterToolbar>
        <AppDataTable
          caption="合格门店成品目录"
          columns={columns}
          rows={stores?.items ?? []}
          rowKey={(row) => `${row.source}:${row.directory_name}`}
          loading={storesQuery.isPending}
          onRowActivate={setSelected}
          emptyState={(
            <StatePanel
              variant="empty"
              title="暂无合格门店"
              description="业务审核为合格后会自动加入成品目录，无需重新采集或导出。"
            />
          )}
        />
        <AppPagination
          page={page}
          pageCount={pageCount}
          total={total}
          pageSize={pageSize}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size)
            setPage(1)
          }}
        />
      </section>

      <QualifiedStoreDrawer store={selected} hostPath={status.host_path} onClose={() => setSelected(null)} />
    </div>
  )
}
