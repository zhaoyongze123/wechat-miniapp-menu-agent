import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { PageHeader } from '../../components/PageHeader'
import { StatePanel } from '../../components/StatePanel'
import { StatCard } from '../../components/StatCard'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { archiveWorker, revokeWorker, saveUpdateRollout } from './api'
import { deviceStateOf, formatTime } from './format'
import { activeAssignmentsOf, isWorkerActive, useHubSharedStatus } from './hooks'
import { PairingDialog } from './PairingDialog'
import { WorkerDetailDrawer } from './WorkerDetailDrawer'
import type { HubWorker } from './types'

type ConfirmTarget = { kind: 'revoke' | 'archive'; worker: HubWorker } | null

export function HubDevicesPage() {
  const role = useRole()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [pairOpen, setPairOpen] = useState(false)
  const [detailId, setDetailId] = useState<string | null>(null)
  const [showArchived, setShowArchived] = useState(false)
  const [confirmTarget, setConfirmTarget] = useState<ConfirmTarget>(null)
  const [confirmError, setConfirmError] = useState('')

  const sharedQuery = useHubSharedStatus()
  const workers = useMemo(() => sharedQuery.data?.workers ?? [], [sharedQuery.data])
  const assignments = useMemo(() => sharedQuery.data?.assignments ?? [], [sharedQuery.data])

  const visibleWorkers = useMemo(() => {
    const list = workers.filter((worker) => showArchived || !worker.archived_at)
    return [...list].sort((left, right) => {
      if (Boolean(left.archived_at) !== Boolean(right.archived_at)) return left.archived_at ? 1 : -1
      const onlineDiff = Number(deviceStateOf(right).key === 'online') - Number(deviceStateOf(left).key === 'online')
      if (onlineDiff) return onlineDiff
      return left.name.localeCompare(right.name, 'zh-CN')
    })
  }, [workers, showArchived])

  const activeWorkers = workers.filter((worker) => isWorkerActive(worker) && !worker.archived_at)
  const onlineCount = activeWorkers.filter((worker) => deviceStateOf(worker).key === 'online').length
  const occupiedCount = assignments.filter((record) => record.status === 'assigned' || record.status === 'ready').length
  const updateSummary = sharedQuery.data?.update_summary
  const outdatedCount = updateSummary?.update_available ?? 0

  const detailWorker = workers.find((worker) => worker.id === detailId) ?? null
  const detailAssignments = detailWorker ? activeAssignmentsOf(sharedQuery.data, detailWorker.id) : []

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['hub-shared-status'] })
    void queryClient.invalidateQueries({ queryKey: ['hub-status'] })
    void queryClient.invalidateQueries({ queryKey: ['hub-assignments'] })
  }

  const revokeMutation = useMutation({
    mutationFn: (workerId: string) => revokeWorker(workerId),
    onSuccess: () => {
      setConfirmTarget(null)
      setConfirmError('')
      toast.push('已解除设备配对', 'success')
      invalidate()
    },
    onError: (cause) => setConfirmError(errorMessage(cause)),
  })

  const archiveMutation = useMutation({
    mutationFn: (workerId: string) => archiveWorker(workerId),
    onSuccess: () => {
      setConfirmTarget(null)
      setConfirmError('')
      setDetailId(null)
      toast.push('设备已从列表移除，历史记录保留', 'success')
      invalidate()
    },
    onError: (cause) => setConfirmError(errorMessage(cause)),
  })

  const rollout = sharedQuery.data?.update_rollout
  const [rolloutMode, setRolloutMode] = useState<'all' | 'batch'>('all')
  const [rolloutIds, setRolloutIds] = useState<string[]>([])
  // 以服务端值为准初始化编辑态；轮询刷新时只有范围真正变化才覆盖，避免打断正在勾选的用户。
  const rolloutKey = rollout ? `${rollout.mode}:${rollout.worker_ids.join(',')}` : ''
  useEffect(() => {
    setRolloutMode(rollout?.mode ?? 'all')
    setRolloutIds(rollout?.worker_ids ?? [])
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅按稳定 key 同步，避免数组引用变化重置编辑
  }, [rolloutKey])

  const rolloutMutation = useMutation({
    mutationFn: () => saveUpdateRollout(rolloutMode, rolloutMode === 'batch' ? rolloutIds : []),
    onSuccess: (saved) => {
      toast.push(saved.mode === 'batch' ? `已限定 ${saved.worker_ids.length} 台设备接收更新` : '已恢复全部设备接收更新', 'success')
      invalidate()
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const openConfirm = (target: NonNullable<ConfirmTarget>) => {
    setConfirmError('')
    setConfirmTarget(target)
  }

  if (role.loading) return <StatePanel variant="loading" title="正在确认当前角色…" />
  if (role.error) {
    return (
      <StatePanel
        variant="error"
        title="无法确认当前服务角色"
        description={role.error}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={role.reload}>
            重试
          </Button>
        }
      />
    )
  }
  if (role.role !== 'hub') {
    return <StatePanel variant="info" title="仅共享后台可管理采集电脑" description="Windows 采集端请在本地完成配对与任务查看。" />
  }

  const columns: DataColumn<HubWorker>[] = [
    {
      id: 'name',
      header: '采集电脑',
      className: 'min-w-48',
      width: '16rem',
      cell: (worker) => (
        <div className="min-w-0">
          <span className="block truncate text-default" title={worker.name}>{worker.name}</span>
          <span className="mt-0.5 block truncate font-mono text-2xs text-tertiary" title={worker.id}>
            {worker.id}
          </span>
        </div>
      ),
    },
    {
      id: 'state',
      header: '状态',
      className: 'whitespace-nowrap',
      width: '7rem',
      cell: (worker) => {
        const state = deviceStateOf(worker)
        return <StatusBadge tone={state.tone}>{state.label}</StatusBadge>
      },
    },
    {
      id: 'version',
      header: '版本',
      className: 'whitespace-nowrap',
      width: '9.5rem',
      cell: (worker) => {
        const software = worker.software
        // 未上报 ≠ 已是最新；只有采集端明确检查过才判断更新状态。
        if (!software) {
          return (
            <div className="space-y-0.5">
              <span className="text-tertiary">未上报</span>
              {worker.in_rollout === false ? (
                <span className="block text-2xs text-tertiary">本批不推送</span>
              ) : null}
            </div>
          )
        }
        return (
          <div className="space-y-0.5">
            <span className="block text-default tabular-nums">{software.current_version || '未知'}</span>
            {software.update_available ? (
              <StatusBadge tone="warning">可更新至 {software.latest_version || '新版本'}</StatusBadge>
            ) : (
              <span className="block text-2xs text-tertiary">
                {software.latest_version ? '已是最新' : '更新状态未知'}
              </span>
            )}
            {worker.in_rollout === false ? (
              <span className="block text-2xs text-tertiary">本批不推送</span>
            ) : null}
          </div>
        )
      },
    },
    {
      id: 'last_seen',
      header: '最近连接',
      className: 'whitespace-nowrap',
      width: '10rem',
      cell: (worker) => <span className="text-secondary">{formatTime(worker.last_seen)}</span>,
    },
    {
      id: 'pending',
      header: '待补传',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (worker) =>
        worker.pending_uploads ? (
          <StatusBadge tone="warning">{worker.pending_uploads} 份</StatusBadge>
        ) : (
          <span className="text-tertiary">—</span>
        ),
    },
    {
      id: 'occupied',
      header: '占用门店',
      className: 'whitespace-nowrap',
      width: '6rem',
      cell: (worker) => {
        const count = activeAssignmentsOf(sharedQuery.data, worker.id).length
        return <span className={count ? 'text-default tabular-nums' : 'text-tertiary'}>{count} 家</span>
      },
    },
    {
      id: 'paired',
      header: '配对时间',
      className: 'whitespace-nowrap',
      width: '10rem',
      cell: (worker) => <span className="text-secondary">{formatTime(worker.paired_at)}</span>,
    },
    {
      id: 'actions',
      header: '',
      className: 'w-20 whitespace-nowrap text-right',
      width: '5.5rem',
      sticky: 'right',
      cell: (worker) => (
        <Button color="secondary" variant="outline" size="sm" onClick={() => setDetailId(worker.id)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <section className="space-y-6" aria-labelledby="hub-devices-heading">
      <PageHeader
        title="采集电脑"
        headingId="hub-devices-heading"
        description={
          updateSummary?.target_version
            ? `集中更新目标 ${updateSummary.target_version}；版本与更新状态由采集端同步时上报。`
            : '版本与更新状态由采集端同步时上报。'
        }
        actions={
          <Button color="primary" size="sm" onClick={() => setPairOpen(true)}>
            配对采集电脑
          </Button>
        }
      />

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-5" aria-label="采集电脑统计">
        <StatCard label="已配对" value={activeWorkers.length} />
        <StatCard label="在线" value={onlineCount} />
        <StatCard label="占用门店" value={occupiedCount} />
        <StatCard label="待更新" value={outdatedCount} {...(outdatedCount ? { tone: 'warning' as const } : {})} />
        <StatCard
          label="版本未上报"
          value={updateSummary?.unreported ?? 0}
          {...(updateSummary?.unreported ? { tone: 'warning' as const } : {})}
        />
      </section>

      <section
        className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5"
        aria-labelledby="hub-rollout-heading"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="hub-rollout-heading" className="heading-xs">
              版本推送范围
            </h2>
            <p className="mt-1 max-w-3xl text-sm text-secondary">
              {rollout?.target_version
                ? `把目标版本 ${rollout.target_version} 推送给指定采集电脑；各设备仍需在本地确认安装。`
                : '本机尚未检查到可分发版本，请先在设置中心完成检查。'}
            </p>
          </div>
          <div className="flex items-center gap-2" role="group" aria-label="推送范围模式">
            <Button
              color={rolloutMode === 'all' ? 'primary' : 'secondary'}
              variant={rolloutMode === 'all' ? 'solid' : 'outline'}
              size="sm"
              onClick={() => setRolloutMode('all')}
            >
              全部设备
            </Button>
            <Button
              color={rolloutMode === 'batch' ? 'primary' : 'secondary'}
              variant={rolloutMode === 'batch' ? 'solid' : 'outline'}
              size="sm"
              onClick={() => setRolloutMode('batch')}
            >
              分批设备
            </Button>
          </div>
        </div>

        {rolloutMode === 'batch' ? (
          activeWorkers.length === 0 ? (
            <p className="text-sm text-secondary">暂无可选的已配对设备。</p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {activeWorkers.map((worker) => (
                <Checkbox
                  key={worker.id}
                  label={`${worker.name}（${worker.software?.current_version || '版本未上报'}）`}
                  checked={rolloutIds.includes(worker.id)}
                  onCheckedChange={(next) =>
                    setRolloutIds((previous) =>
                      next ? [...previous, worker.id] : previous.filter((id) => id !== worker.id),
                    )
                  }
                />
              ))}
            </div>
          )
        ) : null}

        <div className="flex flex-wrap items-center gap-3">
          <Button
            color="primary"
            size="sm"
            disabled={rolloutMutation.isPending || (rolloutMode === 'batch' && rolloutIds.length === 0)}
            onClick={() => rolloutMutation.mutate()}
          >
            {rolloutMutation.isPending ? '保存中…' : '保存推送范围'}
          </Button>
          <span className="text-xs text-tertiary">
            {rolloutMode === 'batch'
              ? `当前生效：${rollout?.worker_ids.length ?? 0} 台设备接收更新`
              : `当前生效：全部在用设备（${activeWorkers.length} 台）`}
          </span>
        </div>
      </section>

      <section className="rounded-xl border border-subtle bg-surface space-y-4 p-4 md:p-5" aria-labelledby="hub-device-list-heading">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="hub-device-list-heading" className="heading-xs">
            设备列表
          </h2>
          <div className="flex items-center gap-3">
            <Checkbox
              label="显示已移除设备"
              checked={showArchived}
              onCheckedChange={(next) => setShowArchived(next)}
            />
            <Button color="secondary" variant="outline" size="sm" onClick={() => void sharedQuery.refetch()}>
              刷新
            </Button>
          </div>
        </div>

        {sharedQuery.isError ? (
          <StatePanel
            variant="error"
            title="设备列表读取失败"
            description={errorMessage(sharedQuery.error)}
            action={
              <Button color="secondary" variant="outline" size="sm" onClick={() => void sharedQuery.refetch()}>
                重试
              </Button>
            }
          />
        ) : (
          <AppDataTable
            caption="采集电脑列表"
            columns={columns}
            rows={visibleWorkers}
            minWidthClass="min-w-[1120px]"
            rowKey={(worker) => worker.id}
            loading={sharedQuery.isFetching && !sharedQuery.data}
            onRowActivate={(worker) => setDetailId(worker.id)}
            emptyState={
              <StatePanel
                variant="empty"
                title={workers.length ? '没有符合显示条件的设备' : '还没有配对采集电脑'}
                description={
                  workers.length
                    ? '已移除的设备可通过上方开关显示；如需恢复连接，请重新配对。'
                    : '生成一次性配对码，在 Windows 采集端输入后即可开始分配任务。'
                }
                action={
                  workers.length ? null : (
                    <Button color="primary" size="sm" onClick={() => setPairOpen(true)}>
                      配对采集电脑
                    </Button>
                  )
                }
              />
            }
          />
        )}

        {onlineCount === 0 && activeWorkers.length > 0 ? (
          <Alert
            color="warning"
            variant="soft"
            title="当前没有在线采集电脑"
            description="仍可分配任务，任务会在设备重新连接并同步后下载；如需立即采集，请先确认采集端已启动并联网。"
          />
        ) : null}
      </section>

      <PairingDialog open={pairOpen} onOpenChange={setPairOpen} />

      <WorkerDetailDrawer
        open={Boolean(detailId)}
        worker={detailWorker}
        assignments={detailAssignments}
        onOpenChange={(next) => {
          if (!next) setDetailId(null)
        }}
        onUpdated={invalidate}
        onRequestRevoke={(worker) => openConfirm({ kind: 'revoke', worker })}
        onRequestArchive={(worker) => openConfirm({ kind: 'archive', worker })}
      />

      <ConfirmActionDialog
        open={Boolean(confirmTarget)}
        title={confirmTarget?.kind === 'revoke' ? `解除配对：${confirmTarget.worker.name}` : `从列表移除：${confirmTarget?.worker.name ?? ''}`}
        description={
          confirmTarget?.kind === 'revoke' ? (
            <div className="space-y-1">
              <p>解除后该电脑立即失去访问共享后台的权限，无法继续下载或上传任务。</p>
              <p>历史分配记录与稳定设备 ID 会保留；该设备已下载但未上传的本机结果会转为历史留档。</p>
            </div>
          ) : (
            <div className="space-y-1">
              <p>仅从设备列表移除，不删除任何采集结果。</p>
              <p>历史分配记录、稳定设备 ID 与结果文件关联都会保留；之后若需使用，请重新配对生成新设备。</p>
            </div>
          )
        }
        confirmLabel={confirmTarget?.kind === 'revoke' ? '确认解除配对' : '确认从列表移除'}
        tone="danger"
        busy={revokeMutation.isPending || archiveMutation.isPending}
        error={confirmError}
        onOpenChange={(next) => {
          if (!next && !revokeMutation.isPending && !archiveMutation.isPending) {
            setConfirmTarget(null)
            setConfirmError('')
          }
        }}
        onConfirm={() => {
          if (!confirmTarget) return
          if (confirmTarget.kind === 'revoke') revokeMutation.mutate(confirmTarget.worker.id)
          else archiveMutation.mutate(confirmTarget.worker.id)
        }}
      />
    </section>
  )
}
