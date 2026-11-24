import { useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { errorMessage } from '../../api/client'
import { AppDataTable } from '../../components/AppDataTable'
import { AppDialog } from '../../components/AppDialog'
import { AppPagination } from '../../components/AppPagination'
import { StatusBadge } from '../../components/StatusBadge'
import { fetchModelUsage } from './api'
import {
  GROUP_OPTIONS,
  SOURCE_OPTIONS,
  callStatusLabel,
  callStatusTone,
  compactId,
  coverageText,
  elapsedText,
  formatInteger,
  groupRowLabel,
  hasUnknownUsage,
  localDate,
  modelText,
  sourceLabel,
  stepText,
  summaryLine,
  tokenText,
  usageStateLabel,
} from './format'
import type { UsageCall, UsageGroup, UsageGroupBy, UsageSource } from './types'

interface ModelUsageDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

type UsageMode = 'stats' | 'calls'

const PAGE_SIZE_OPTIONS = [20, 50, 100]

function numberCell(value: number | undefined, knownCalls: number | undefined) {
  return <span className="tabular-nums">{tokenText(value, knownCalls)}</span>
}

/** 单次调用的 Token：缺失就是未返回，不能显示成 0。 */
function callToken(value: number | null | undefined): string {
  return typeof value === 'number' ? formatInteger(value) : '—'
}

/** 设置内的模型用量弹窗：只读统计与调用明细，按日期、用途、队列与维度筛选。 */
export function ModelUsageDialog({ open, onOpenChange }: ModelUsageDialogProps) {
  const [dateFrom, setDateFrom] = useState(() => localDate(6))
  const [dateTo, setDateTo] = useState(() => localDate(0))
  const [source, setSource] = useState<UsageSource>('all')
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('day')
  const [runId, setRunId] = useState('')
  const [taskId, setTaskId] = useState('')
  const [taskLabel, setTaskLabel] = useState('')
  const [mode, setMode] = useState<UsageMode>('stats')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  // 任一筛选变化都回到第一页，避免落在旧页码的空页上。
  useEffect(() => {
    setPage(1)
  }, [dateFrom, dateTo, source, groupBy, runId, taskId, pageSize])

  const query = useQuery({
    queryKey: ['model-usage', dateFrom, dateTo, groupBy, source, runId, taskId, page, pageSize],
    queryFn: ({ signal }) =>
      fetchModelUsage({ dateFrom, dateTo, groupBy, source, runId, taskId, page, pageSize }, signal),
    enabled: open,
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  })

  const data = query.data
  const summary = data?.summary
  const runs = data?.filters.runs ?? []
  const runOptions = useMemo(() => {
    const options = [
      { value: '', label: '全部队列' },
      ...runs.map((run) => ({ value: run.run_id, label: run.label || compactId(run.run_id) })),
    ]
    if (runId && !runs.some((run) => run.run_id === runId)) options.push({ value: runId, label: runId })
    return options
  }, [runs, runId])

  const resetFilters = () => {
    setDateFrom(localDate(6))
    setDateTo(localDate(0))
    setSource('all')
    setGroupBy('day')
    setRunId('')
    setTaskId('')
    setTaskLabel('')
    setMode('stats')
    setExpanded(new Set())
  }

  const drillGroup = (group: UsageGroup) => {
    if (groupBy === 'day') {
      setDateFrom(group.key)
      setDateTo(group.key)
      setGroupBy('run')
      setRunId('')
      setTaskId('')
      setTaskLabel('')
      setMode('stats')
      return
    }
    if (groupBy === 'run') {
      setRunId(group.run_id || group.key)
      setGroupBy('task')
      setTaskId('')
      setTaskLabel('')
      setMode('stats')
      return
    }
    if (groupBy === 'task') {
      setTaskId(group.task_id || group.key)
      setTaskLabel(group.label || group.key)
      setMode('calls')
      return
    }
  }

  const toggleExpanded = (callId: string) => {
    setExpanded((previous) => {
      const next = new Set(previous)
      if (next.has(callId)) next.delete(callId)
      else next.add(callId)
      return next
    })
  }

  const filterTask = (id: string, label: string) => {
    setTaskId(id)
    setTaskLabel(label || id)
    setMode('calls')
  }

  const groups = data?.groups ?? []
  const calls = data?.calls ?? []
  const pagination = data?.pagination
  const busy = query.isFetching && Boolean(data)

  return (
    <AppDialog
      open={open}
      onOpenChange={onOpenChange}
      title="模型用量"
      widthClass="w-[min(1180px,96vw)]"
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-2xs text-tertiary">
            数据来自本机调用账本；未返回用量的调用不计入 Token 合计。
          </span>
          <Button color="secondary" variant="outline" size="sm" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
        </div>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          <label className="space-y-1">
            <span className="block text-2xs text-secondary">起始日期</span>
            <Input type="date" value={dateFrom} max={dateTo} onChange={(event) => setDateFrom(event.target.value)} />
          </label>
          <label className="space-y-1">
            <span className="block text-2xs text-secondary">结束日期</span>
            <Input type="date" value={dateTo} min={dateFrom} onChange={(event) => setDateTo(event.target.value)} />
          </label>
          <div className="space-y-1">
            <span className="block text-2xs text-secondary">用途</span>
            <Select
              aria-label="用途筛选"
              value={source}
              options={SOURCE_OPTIONS}
              onChange={(option) => setSource(option.value as UsageSource)}
            />
          </div>
          <div className="space-y-1">
            <span className="block text-2xs text-secondary">采集队列</span>
            <Select
              aria-label="采集队列筛选"
              value={runId}
              options={runOptions}
              onChange={(option) => setRunId(option.value)}
            />
          </div>
          <div className="space-y-1">
            <span className="block text-2xs text-secondary">统计维度</span>
            <Select
              aria-label="统计维度"
              value={groupBy}
              options={GROUP_OPTIONS}
              onChange={(option) => setGroupBy(option.value as UsageGroupBy)}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <SegmentedControl
            aria-label="用量视图"
            value={mode}
            onChange={(next) => setMode(next as UsageMode)}
            size="sm"
            pill
          >
            <SegmentedControl.Option value="stats">统计</SegmentedControl.Option>
            <SegmentedControl.Option value="calls">每次调用</SegmentedControl.Option>
          </SegmentedControl>
          {taskId ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-subtle px-2 py-1 text-2xs text-secondary">
              门店：{taskLabel || taskId}
              <button
                type="button"
                className="text-tertiary hover:text-default"
                aria-label="清除门店筛选"
                onClick={() => {
                  setTaskId('')
                  setTaskLabel('')
                }}
              >
                ×
              </button>
            </span>
          ) : null}
          <div className="ml-auto flex items-center gap-2">
            {busy ? <span className="text-2xs text-tertiary">正在刷新…</span> : null}
            <Button color="secondary" variant="outline" size="sm" onClick={resetFilters}>
              重置
            </Button>
            <Button color="secondary" variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
              刷新
            </Button>
          </div>
        </div>

        {query.isPending && !data ? <p className="py-8 text-center text-sm text-secondary">正在读取模型用量…</p> : null}
        {query.isError ? (
          <Alert
            color="danger"
            variant="soft"
            title={errorMessage(query.error)}
            actions={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void query.refetch()}>
                重试
              </Button>
            }
          />
        ) : null}

        {summary ? (
          <section className="space-y-2 rounded-lg border border-subtle p-3" aria-label="用量汇总">
            <p className="text-sm text-default">{summaryLine(summary)}</p>
            <p className="text-2xs text-tertiary">
              {coverageText(summary)}
              {hasUnknownUsage(summary) ? ' · 部分调用未返回用量，Token 合计仅覆盖已知部分' : ''}
            </p>
            <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 lg:grid-cols-7">
              <div>
                <p className="text-2xs text-tertiary">输入 Token</p>
                <p className="tabular-nums text-default">{tokenText(summary.input_tokens, summary.input_tokens_known_calls)}</p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">输出 Token</p>
                <p className="tabular-nums text-default">{tokenText(summary.output_tokens, summary.output_tokens_known_calls)}</p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">总 Token</p>
                <p className="tabular-nums text-default">{tokenText(summary.total_tokens, summary.total_tokens_known_calls)}</p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">缓存读取</p>
                <p className="tabular-nums text-default">{tokenText(summary.cached_tokens, summary.cached_tokens_known_calls)}</p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">推理 Token</p>
                <p className="tabular-nums text-default">
                  {tokenText(summary.reasoning_tokens, summary.reasoning_tokens_known_calls)}
                </p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">图片 Token</p>
                <p className="tabular-nums text-default">{tokenText(summary.image_tokens, summary.image_tokens_known_calls)}</p>
              </div>
              <div>
                <p className="text-2xs text-tertiary">累计耗时</p>
                <p className="tabular-nums text-default">{elapsedText(summary.elapsed_ms)}</p>
              </div>
            </div>
          </section>
        ) : null}

        {mode === 'stats' && data ? (
          <section className="space-y-2" aria-label="用量统计">
            {groups.length === 0 ? (
              <p className="rounded-lg border border-subtle py-8 text-center text-sm text-secondary">该条件下没有调用记录。</p>
            ) : (
              <AppDataTable
                caption="模型用量分组统计"
                minWidthClass="min-w-[900px]"
                rows={groups}
                rowKey={(group) => group.key}
                {...(groupBy !== 'model' ? { onRowActivate: drillGroup } : {})}
                columns={[
                  {
                    id: 'group',
                    header: groupByLabelOf(groupBy),
                    width: '18rem',
                    cell: (group) => (
                      <>
                        <span className="block truncate text-default">{groupRowLabel(groupBy, group)}</span>
                        {groupBy === 'task' && group.source_name ? (
                          <span className="block truncate text-2xs text-tertiary">{group.source_name}</span>
                        ) : null}
                        {groupBy === 'run' && group.run_id ? (
                          <span className="block truncate text-2xs text-tertiary">{compactId(group.run_id)}</span>
                        ) : null}
                      </>
                    ),
                  },
                  {
                    id: 'calls', header: '调用', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (group) => formatInteger(group.calls),
                  },
                  {
                    id: 'success', header: '成功', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums text-success',
                    cell: (group) => formatInteger(group.success_calls),
                  },
                  {
                    id: 'error', header: '失败', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums text-danger',
                    cell: (group) => formatInteger(group.error_calls),
                  },
                  {
                    id: 'running', header: '进行中', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (group) => formatInteger(group.running_calls),
                  },
                  {
                    id: 'interrupted', header: '中断', width: '5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (group) => formatInteger(group.interrupted_calls),
                  },
                  {
                    id: 'input', header: '输入', width: '6rem',
                    headerClassName: 'text-right', className: 'text-right',
                    cell: (group) => numberCell(group.input_tokens, group.input_tokens_known_calls),
                  },
                  {
                    id: 'output', header: '输出', width: '6rem',
                    headerClassName: 'text-right', className: 'text-right',
                    cell: (group) => numberCell(group.output_tokens, group.output_tokens_known_calls),
                  },
                  {
                    id: 'total', header: '总 Token', width: '7rem',
                    headerClassName: 'text-right', className: 'text-right',
                    cell: (group) => numberCell(group.total_tokens, group.total_tokens_known_calls),
                  },
                  {
                    id: 'elapsed', header: '耗时', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (group) => elapsedText(group.elapsed_ms),
                  },
                  {
                    id: 'drill', header: '下钻', width: '4.5rem',
                    headerClassName: 'text-right', className: 'text-right text-2xs text-tertiary',
                    cell: () => (groupBy !== 'model' ? '查看' : '—'),
                  },
                ]}
              />
            )}
            {data.groups_total > groups.length ? (
              <p className="text-2xs text-tertiary">共 {data.groups_total} 组，仅显示最近 {groups.length} 组；可缩小日期或添加筛选。</p>
            ) : null}
          </section>
        ) : null}

        {mode === 'calls' && data ? (
          <section className="space-y-2" aria-label="调用明细">
            {calls.length === 0 ? (
              <p className="rounded-lg border border-subtle py-8 text-center text-sm text-secondary">该条件下没有调用记录。</p>
            ) : (
              <AppDataTable
                caption="模型调用明细"
                minWidthClass="min-w-[980px]"
                rows={calls}
                rowKey={(call) => call.call_id}
                subRow={(call) => (expanded.has(call.call_id) ? (
                  <CallDetail
                    call={call}
                    onFilterTask={() => filterTask(call.task_id || '', call.source_name || call.task_id || '')}
                  />
                ) : null)}
                columns={[
                  {
                    id: 'started', header: '开始时间', width: '10rem',
                    className: 'whitespace-nowrap text-xs text-secondary',
                    cell: (call) => call.started_at.replace('T', ' ').slice(0, 19),
                  },
                  {
                    id: 'status', header: '状态', width: '6.5rem',
                    cell: (call) => <StatusBadge tone={callStatusTone(call.status)}>{callStatusLabel(call.status)}</StatusBadge>,
                  },
                  {
                    id: 'source', header: '用途 / 归属', width: '13rem',
                    cell: (call) => (
                      <>
                        <span className="block truncate">{sourceLabel(call.source)}</span>
                        <span className="block truncate text-2xs text-tertiary">
                          {call.source_name || compactId(call.task_id) || '—'}
                          {call.stage ? ` · ${call.stage}` : ''}
                        </span>
                      </>
                    ),
                  },
                  {
                    id: 'model', header: '模型', width: '12rem',
                    cell: (call) => <span className="block truncate">{modelText(call)}</span>,
                  },
                  {
                    id: 'input', header: '输入', width: '6rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (call) => callToken(call.input_tokens),
                  },
                  {
                    id: 'output', header: '输出', width: '6rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (call) => callToken(call.output_tokens),
                  },
                  {
                    id: 'total', header: '总 Token', width: '7rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (call) => callToken(call.total_tokens),
                  },
                  {
                    id: 'elapsed', header: '耗时', width: '5.5rem',
                    headerClassName: 'text-right', className: 'text-right tabular-nums',
                    cell: (call) => elapsedText(call.elapsed_ms),
                  },
                  {
                    id: 'detail', header: '详情', width: '5rem',
                    headerClassName: 'text-right', className: 'text-right',
                    cell: (call) => (
                      <Button
                        color="secondary"
                        variant="ghost"
                        size="sm"
                        aria-expanded={expanded.has(call.call_id)}
                        onClick={() => toggleExpanded(call.call_id)}
                      >
                        {expanded.has(call.call_id) ? '收起' : '展开'}
                      </Button>
                    ),
                  },
                ]}
              />
            )}
            {pagination && pagination.pages > 1 ? (
              <AppPagination
                page={pagination.page}
                pageCount={pagination.pages}
                total={pagination.total}
                pageSize={pageSize}
                pageSizeOptions={PAGE_SIZE_OPTIONS}
                onPageChange={setPage}
                onPageSizeChange={setPageSize}
              />
            ) : null}
          </section>
        ) : null}
      </div>
    </AppDialog>
  )
}

function groupByLabelOf(groupBy: UsageGroupBy): string {
  return GROUP_OPTIONS.find((item) => item.value === groupBy)?.label ?? '统计维度'
}

interface CallDetailProps {
  call: UsageCall
  onFilterTask: () => void
}

function CallDetail({ call, onFilterTask }: CallDetailProps) {
  return (
    <div className="grid gap-3 text-xs sm:grid-cols-2 lg:grid-cols-3">
      <div className="space-y-1">
        <p className="text-2xs text-tertiary">调用标识</p>
        <p className="font-mono break-all">{call.call_id}</p>
        <p className="text-2xs text-tertiary">请求 {call.request_id || '—'} · HTTP {call.http_status ?? '—'}</p>
        <p className="text-2xs text-tertiary">{usageStateLabel(call.usage_state)}</p>
        {call.error_type ? <p className="text-danger">错误类型：{call.error_type}</p> : null}
      </div>
      <div className="space-y-1">
        <p className="text-2xs text-tertiary">归属</p>
        <p>
          {sourceLabel(call.source)}
          {call.source_name ? ` · ${call.source_name}` : ''}
        </p>
        <p className="text-2xs text-tertiary">
          队列 {compactId(call.run_id)} · 任务 {compactId(call.task_id)}
        </p>
        <p className="text-2xs text-tertiary">
          阶段 {call.stage || '—'} · 步 {stepText(call.step)} · 尝试 {compactId(call.attempt_id)}
        </p>
        {call.task_id ? (
          <Button color="secondary" variant="ghost" size="sm" onClick={onFilterTask}>
            只看该门店
          </Button>
        ) : null}
      </div>
      <div className="space-y-1">
        <p className="text-2xs text-tertiary">Token 明细</p>
        <p>
          输入 {callToken(call.input_tokens)} · 输出 {callToken(call.output_tokens)}
        </p>
        <p>
          总计 {callToken(call.total_tokens)} · 缓存读 {callToken(call.cached_tokens)}
        </p>
        <p>
          缓存写 {callToken(call.cache_write_tokens)} · 推理 {callToken(call.reasoning_tokens)} · 图片{' '}
          {callToken(call.image_tokens)}
        </p>
        <p className="text-2xs text-tertiary">接口 {call.endpoint || '—'}</p>
        <p className="text-2xs text-tertiary">
          结束 {call.finished_at ? call.finished_at.replace('T', ' ').slice(0, 19) : '—'}
        </p>
      </div>
    </div>
  )
}
