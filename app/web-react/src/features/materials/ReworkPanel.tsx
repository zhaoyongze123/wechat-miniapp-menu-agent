import { useEffect, useMemo, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppPagination } from '../../components/AppPagination'
import { BulkActionBar } from '../../components/BulkActionBar'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { FilterToolbar } from '../../components/FilterToolbar'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatCard } from '../../components/StatCard'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatStore } from '../hub/format'
import { decisionMeta, storeKey } from './format'
import { useRework, useReworkActions } from './hooks'
import type { ReworkCategory, ReworkRecord, ReworkPublishResponse } from './types'

type CategoryFilter = 'all' | ReworkCategory

const CATEGORY_META: Record<ReworkCategory, { label: string; tone: 'warning' | 'danger' | 'info' }> = {
  pending: { label: '待处理', tone: 'warning' },
  rejected: { label: '不合格', tone: 'danger' },
  unpublished: { label: '待发布', tone: 'info' },
}

const PAGE_SIZE = 50

/** 跨批次返工：统一筛选、跨页选择、批量打回与发布。 */
export function ReworkPanel() {
  const toast = useToast()
  const [category, setCategory] = useState<CategoryFilter>('all')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string[]>([])
  const [confirm, setConfirm] = useState<'return' | 'publish' | null>(null)
  const [error, setError] = useState('')
  const [lastResult, setLastResult] = useState<ReworkPublishResponse | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  const reworkQuery = useRework(query)
  const records = useMemo(() => reworkQuery.data?.records ?? [], [reworkQuery.data])
  const summary = reworkQuery.data?.summary
  const actions = useReworkActions()

  // 列表刷新后剔除已不存在的选择，避免对已处理门店重复操作。
  useEffect(() => {
    if (!reworkQuery.data) return
    const keys = new Set(reworkQuery.data.records.map((record) => storeKey(record.batch_id, record.store_id)))
    setSelected((current) => {
      const next = current.filter((key) => keys.has(key))
      return next.length === current.length ? current : next
    })
  }, [reworkQuery.data])

  useEffect(() => {
    setPage(1)
  }, [category, query])

  const filtered = useMemo(
    () => (category === 'all' ? records : records.filter((record) => record.category === category)),
    [records, category],
  )
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount)
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE)

  const selectedSet = useMemo(() => new Set(selected), [selected])
  const selectedRecords = records.filter((record) => selectedSet.has(storeKey(record.batch_id, record.store_id)))
  const publishable = selectedRecords.filter((record) => record.publishable)
  const pageKeys = pageRows.map((record) => storeKey(record.batch_id, record.store_id))
  const pageSelectedCount = pageKeys.filter((key) => selectedSet.has(key)).length
  const allPageSelected = pageKeys.length > 0 && pageSelectedCount === pageKeys.length

  const toggle = (key: string) => {
    setSelected((current) => (current.includes(key) ? current.filter((item) => item !== key) : [...current, key]))
  }

  const togglePage = (next: boolean) => {
    setSelected((current) => {
      if (next) return Array.from(new Set([...current, ...pageKeys]))
      return current.filter((key) => !pageKeys.includes(key))
    })
  }

  const runReturn = async () => {
    setError('')
    const targets = selectedRecords
    let done = 0
    const failures: string[] = []
    for (const record of targets) {
      try {
        await actions.returnStore.mutateAsync({ batchId: record.batch_id, storeId: record.store_id })
        done += 1
      } catch (cause) {
        failures.push(`${record.store_name}：${errorMessage(cause)}`)
      }
    }
    if (done) toast.push(`已打回 ${done} 家门店，可在对应批次重新选码`, 'success')
    if (failures.length) {
      setError(`部分门店打回失败：${failures.slice(0, 3).join('；')}${failures.length > 3 ? '…' : ''}`)
      return
    }
    setSelected([])
    setConfirm(null)
  }

  const runPublish = () => {
    setError('')
    const keys = publishable.map((record) => storeKey(record.batch_id, record.store_id))
    actions.publish.mutate(keys, {
      onSuccess: (result) => {
        setLastResult(result)
        setConfirm(null)
        const successfulBatches = new Set(result.results.map((item) => item.batch_id))
        const failedBatches = new Set(result.errors.map((item) => item.batch_id))
        // 仅清除已成功发布的门店；失败批次及当前不可发布的选中项保留，便于核对和重试。
        const completedKeys = new Set(
          publishable
            .filter((record) => successfulBatches.has(record.batch_id) && !failedBatches.has(record.batch_id))
            .map((record) => storeKey(record.batch_id, record.store_id)),
        )
        setSelected((current) => current.filter((key) => !completedKeys.has(key)))
        const published = result.results.reduce((sum, item) => sum + (item.task_scan.added || 0), 0)
        toast.push(
          result.errors.length
            ? `已发布 ${result.results.length} 个批次，${result.errors.length} 个批次失败`
            : `已发布 ${result.results.length} 个批次，新增任务 ${published} 家`,
          result.errors.length ? 'danger' : 'success',
        )
      },
      onError: (cause) => setError(errorMessage(cause)),
    })
  }

  const columns: DataColumn<ReworkRecord>[] = [
    {
      id: 'select',
      header: (
        <Checkbox
          label="本页全选"
          checked={allPageSelected ? true : pageSelectedCount > 0 ? 'indeterminate' : false}
          onCheckedChange={(next) => togglePage(next)}
          aria-label="全选当前页"
        />
      ),
      className: 'w-10',
      width: '3.25rem',
      sticky: 'left',
      cell: (record) => {
        const key = storeKey(record.batch_id, record.store_id)
        return (
          <Checkbox
            checked={selectedSet.has(key)}
            onCheckedChange={() => toggle(key)}
            aria-label={`选择 ${record.store_name}`}
          />
        )
      },
    },
    {
      id: 'store',
      header: '门店 / 批次',
      className: 'min-w-56',
      width: '18rem',
      cell: (record) => {
        const store = formatStore(record.store_name)
        const meta = [store.code, record.batch_name].filter(Boolean).join(' · ')
        return (
          <div className="min-w-0">
            <span className="block truncate text-default" title={record.store_name}>{store.name || record.store_name}</span>
            <span className="mt-0.5 block truncate text-2xs text-tertiary" title={[meta, record.source_folder].filter(Boolean).join(' · ')}>
              {meta || record.source_folder || '批次信息未记录'}
            </span>
          </div>
        )
      },
    },
    {
      id: 'category',
      header: '分类',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (record) => (
        <StatusBadge tone={CATEGORY_META[record.category]?.tone ?? 'secondary'}>
          {CATEGORY_META[record.category]?.label ?? record.category}
        </StatusBadge>
      ),
    },
    {
      id: 'decision',
      header: '当前决策',
      className: 'whitespace-nowrap',
      width: '8rem',
      cell: (record) => {
        const meta = decisionMeta(record.decision)
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
      },
    },
    {
      id: 'reason',
      header: '原因',
      className: 'min-w-40',
      width: '14rem',
      cell: (record) =>
        record.reason_label ? (
          <span className="text-secondary">
            <span className="block truncate" title={record.reason_label}>{record.reason_label}</span>
            {record.reason_detail ? (
              <span className="line-clamp-2 text-2xs text-tertiary" title={record.reason_detail}>{record.reason_detail}</span>
            ) : null}
          </span>
        ) : (
          <span className="text-tertiary">—</span>
        ),
    },
    {
      id: 'version',
      header: '版本',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (record) => <span className="text-secondary">{record.version || '未发布'}</span>,
    },
    {
      id: 'publishable',
      header: '可发布',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (record) =>
        record.publishable ? <StatusBadge tone="success">可发布</StatusBadge> : <span className="text-tertiary">否</span>,
    },
    {
      id: 'actions',
      header: '',
      className: 'w-24 whitespace-nowrap text-right',
      width: '5rem',
      sticky: 'right',
      cell: (record) => (
        <Button
          color="secondary"
          variant="outline"
          size="sm"
          onClick={() => {
            setSelected([storeKey(record.batch_id, record.store_id)])
            setConfirm('return')
          }}
        >
          打回
        </Button>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="返工统计">
        <StatCard label="待处理" value={summary?.pending ?? 0} />
        <StatCard label="不合格" value={summary?.rejected ?? 0} />
        <StatCard label="已选码待发布" value={summary?.unpublished ?? 0} />
        <StatCard label="涉及批次" value={summary?.batches ?? 0} />
      </section>

      <section className="space-y-3 rounded-xl border border-subtle p-4" aria-labelledby="rework-list-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="rework-list-heading" className="heading-xs">
            返工门店
          </h2>
          <Button color="secondary" variant="outline" size="sm" onClick={() => void reworkQuery.refetch()}>
            刷新
          </Button>
        </div>

        <FilterToolbar
          label="返工门店筛选"
          activeCount={[search, category !== 'all' ? category : ''].filter(Boolean).length}
          onClear={() => {
            setSearch('')
            setCategory('all')
          }}
        >
          <SearchInput
            className="w-64"
            label="返工门店"
            placeholder="搜索门店或批次名称"
            value={search}
            onChange={setSearch}
          />
          <SegmentedControl<CategoryFilter>
            value={category}
            onChange={(next) => setCategory(next)}
            aria-label="返工分类"
          >
            <SegmentedControl.Option value="all">全部</SegmentedControl.Option>
            <SegmentedControl.Option value="pending">待处理</SegmentedControl.Option>
            <SegmentedControl.Option value="rejected">不合格</SegmentedControl.Option>
            <SegmentedControl.Option value="unpublished">待发布</SegmentedControl.Option>
          </SegmentedControl>
        </FilterToolbar>

        <BulkActionBar
          count={selected.length}
          hint={`其中可发布 ${publishable.length} 家`}
          onClear={() => setSelected([])}
        >
          <Button
            color="primary"
            size="sm"
            disabled={!publishable.length || actions.publish.isPending}
            onClick={() => {
              setError('')
              setConfirm('publish')
            }}
          >
            发布已选门店
          </Button>
          <Button
            color="secondary"
            variant="outline"
            size="sm"
            disabled={!selected.length || actions.returnStore.isPending}
            onClick={() => {
              setError('')
              setConfirm('return')
            }}
          >
            打回已选门店
          </Button>
        </BulkActionBar>

        {reworkQuery.isError ? (
          <StatePanel
            variant="error"
            title="返工清单读取失败"
            description={errorMessage(reworkQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void reworkQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <AppDataTable
            caption="跨批次返工清单"
            columns={columns}
            rows={pageRows}
            minWidthClass="min-w-[1080px]"
            rowKey={(record) => storeKey(record.batch_id, record.store_id)}
            loading={reworkQuery.isFetching && !reworkQuery.data}
            emptyState={
              <StatePanel
                variant="empty"
                title={query ? '没有匹配的返工门店' : '当前没有待返工门店'}
                description={query ? '请尝试其他关键词或切换分类。' : '所有门店都已确认并发布，或尚未开始清洗。'}
              />
            }
          />
        )}

        <AppPagination
          page={currentPage}
          pageCount={pageCount}
          total={filtered.length}
          pageSize={PAGE_SIZE}
          pageSizeOptions={[PAGE_SIZE]}
          onPageChange={setPage}
        />

        {lastResult?.errors.length ? (
          <Alert
            color="danger"
            variant="soft"
            title="部分批次发布失败"
            description={lastResult.errors
              .map((item) => `${item.batch_name || item.batch_id}：${item.error}`)
              .join('；')}
          />
        ) : null}
      </section>

      <ConfirmActionDialog
        open={confirm === 'return'}
        title={selectedRecords.length > 1 ? `打回 ${selectedRecords.length} 家门店` : '打回门店'}
        description={
          <div className="space-y-1">
            <p>打回后门店回到「待处理」，需要重新选码；不会新建批次，也不会修改已发布版本。</p>
            {selectedRecords.length > 1 ? <p>本次影响：{selectedRecords.length} 家门店。</p> : null}
          </div>
        }
        confirmLabel="确认打回"
        busy={actions.returnStore.isPending}
        error={error}
        onOpenChange={(next) => {
          if (!next && !actions.returnStore.isPending) {
            setConfirm(null)
            setError('')
          }
        }}
        onConfirm={() => void runReturn()}
      />

      <ConfirmActionDialog
        open={confirm === 'publish'}
        title={`发布 ${publishable.length} 家门店`}
        description={
          <div className="space-y-1">
            <p>按原批次分组，在各自最新快照上增量更新；不会切换默认版本。</p>
            {selectedRecords.length !== publishable.length ? (
              <p>{selectedRecords.length - publishable.length} 家不合格门店不会发布。</p>
            ) : null}
            <p>发布后这些门店会重新进入任务分配，请确认选码已复核。</p>
          </div>
        }
        confirmLabel="确认发布"
        busy={actions.publish.isPending}
        error={error}
        onOpenChange={(next) => {
          if (!next && !actions.publish.isPending) {
            setConfirm(null)
            setError('')
          }
        }}
        onConfirm={runPublish}
      />
    </div>
  )
}
