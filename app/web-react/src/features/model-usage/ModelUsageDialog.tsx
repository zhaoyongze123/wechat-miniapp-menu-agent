import { Fragment, useEffect, useMemo, useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { SegmentedControl } from '@openai/apps-sdk-ui/components/SegmentedControl'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { errorMessage } from '../../api/client'
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
  groupRowHint,
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
              <div className="overflow-x-auto rounded-lg border border-subtle">
                <table className="w-full min-w-[900px] text-left text-xs">
                  <thead className="bg-surface-secondary text-2xs text-tertiary">
                    <tr>
                      <th className="px-2 py-2 font-medium">{groupByLabelOf(groupBy)}</th>
                      <th className="px-2 py-2 text-right font-medium">调用</th>
                      <th className="px-2 py-2 text-right font-medium">成功</th>
                      <th className="px-2 py-2 text-right font-medium">失败</th>
                      <th className="px-2 py-2 text-right font-medium">进行中</th>
                      <th className="px-2 py-2 text-right font-medium">中断</th>
                      <th className="px-2 py-2 text-right font-medium">输入</th>
                      <th className="px-2 py-2 text-right font-medium">输出</th>
                      <th className="px-2 py-2 text-right font-medium">总 Token</th>
                      <th className="px-2 py-2 text-right font-medium">耗时</th>
                      <th className="px-2 py-2 text-right font-medium">下钻</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-subtle">
                    {groups.map((group) => {
                      const clickable = groupBy !== 'model'
                      return (
                        <tr
                          key={group.key}
                          className={clickable ? 'cursor-pointer hover:bg-secondary-ghost-hover' : undefined}
                          onClick={() => clickable && drillGroup(group)}
                          title={clickable ? groupRowHint(groupBy, group) : undefined}
                        >
                          <td className="max-w-72 px-2 py-2">
                            <span className="block truncate text-default">{groupRowLabel(groupBy, group)}</span>
                            {groupBy === 'task' && group.source_name ? (
                              <span className="block truncate text-2xs text-tertiary">{group.source_name}</span>
                            ) : null}
                            {groupBy === 'run' && group.run_id ? (
                              <span className="block truncate text-2xs text-tertiary">{compactId(group.run_id)}</span>
                            ) : null}
                          </td>
                          <td className="px-2 py-2 text-right tabular-nums">{formatInteger(group.calls)}</td>
                          <td className="px-2 py-2 text-right tabular-nums text-success">{formatInteger(group.success_calls)}</td>
                          <td className="px-2 py-2 text-right tabular-nums text-danger">{formatInteger(group.error_calls)}</td>
                          <td className="px-2 py-2 text-right tabular-nums">{formatInteger(group.running_calls)}</td>
                          <td className="px-2 py-2 text-right tabular-nums">{formatInteger(group.interrupted_calls)}</td>
                          <td className="px-2 py-2 text-right">{numberCell(group.input_tokens, group.input_tokens_known_calls)}</td>
                          <td className="px-2 py-2 text-right">{numberCell(group.output_tokens, group.output_tokens_known_calls)}</td>
                          <td className="px-2 py-2 text-right">{numberCell(group.total_tokens, group.total_tokens_known_calls)}</td>
                          <td className="px-2 py-2 text-right tabular-nums">{elapsedText(group.elapsed_ms)}</td>
                          <td className="px-2 py-2 text-right text-2xs text-tertiary">
                            {clickable ? '查看' : '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
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
              <div className="overflow-x-auto rounded-lg border border-subtle">
                <table className="w-full min-w-[980px] text-left text-xs">
                  <thead className="bg-surface-secondary text-2xs text-tertiary">
                    <tr>
                      <th className="px-2 py-2 font-medium">开始时间</th>
                      <th className="px-2 py-2 font-medium">状态</th>
                      <th className="px-2 py-2 font-medium">用途 / 归属</th>
                      <th className="px-2 py-2 font-medium">模型</th>
                      <th className="px-2 py-2 text-right font-medium">输入</th>
                      <th className="px-2 py-2 text-right font-medium">输出</th>
                      <th className="px-2 py-2 text-right font-medium">总 Token</th>
                      <th className="px-2 py-2 text-right font-medium">耗时</th>
                      <th className="px-2 py-2 text-right font-medium">详情</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-subtle">
                    {calls.map((call) => {
                      const openDetail = expanded.has(call.call_id)
                      return (
                        <Fragment key={call.call_id}>
                          <tr className="align-top">
                            <td className="whitespace-nowrap px-2 py-2 text-secondary">{call.started_at.replace('T', ' ').slice(0, 19)}</td>
                            <td className="px-2 py-2">
                              <StatusBadge tone={callStatusTone(call.status)}>{callStatusLabel(call.status)}</StatusBadge>
                            </td>
                            <td className="max-w-52 px-2 py-2">
                              <span className="block truncate">{sourceLabel(call.source)}</span>
                              <span className="block truncate text-2xs text-tertiary">
                                {call.source_name || compactId(call.task_id) || '—'}
                                {call.stage ? ` · ${call.stage}` : ''}
                              </span>
                            </td>
                            <td className="max-w-60 px-2 py-2">
                              <span className="block truncate">{modelText(call)}</span>
                            </td>
                            <td className="px-2 py-2 text-right tabular-nums">{callToken(call.input_tokens)}</td>
                            <td className="px-2 py-2 text-right tabular-nums">{callToken(call.output_tokens)}</td>
                            <td className="px-2 py-2 text-right tabular-nums">{callToken(call.total_tokens)}</td>
                            <td className="px-2 py-2 text-right tabular-nums">{elapsedText(call.elapsed_ms)}</td>
                            <td className="px-2 py-2 text-right">
                              <button
                                type="button"
                                className="text-2xs text-primary underline-offset-2 hover:underline"
                                onClick={() => toggleExpanded(call.call_id)}
                              >
                                {openDetail ? '收起' : '展开'}
                              </button>
                            </td>
                          </tr>
                          {openDetail ? (
                            <tr className="bg-surface-secondary">
                              <td colSpan={9} className="px-3 py-3">
                                <CallDetail
                                  call={call}
                                  onFilterTask={() =>
                                    filterTask(call.task_id || '', call.source_name || call.task_id || '')
                                  }
                                />
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      )
                    })}
                  </tbody>
                </table>
              </div>
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
