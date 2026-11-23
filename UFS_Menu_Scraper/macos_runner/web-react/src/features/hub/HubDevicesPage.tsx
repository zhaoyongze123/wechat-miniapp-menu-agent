import { useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { archiveWorker, revokeWorker } from './api'
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
  const pendingUploads = activeWorkers.reduce((sum, worker) => sum + (worker.pending_uploads ?? 0), 0)

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
      cell: (worker) => (
        <div className="min-w-0">
          <span className="block truncate text-default">{worker.name}</span>
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
      cell: (worker) => {
        const state = deviceStateOf(worker)
        return <StatusBadge tone={state.tone}>{state.label}</StatusBadge>
      },
    },
    {
      id: 'last_seen',
      header: '最近连接',
      className: 'whitespace-nowrap',
      cell: (worker) => <span className="text-secondary">{formatTime(worker.last_seen)}</span>,
    },
    {
      id: 'pending',
      header: '待补传',
      className: 'whitespace-nowrap',
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
      cell: (worker) => {
        const count = activeAssignmentsOf(sharedQuery.data, worker.id).length
        return <span className={count ? 'text-default tabular-nums' : 'text-tertiary'}>{count} 家</span>
      },
    },
    {
      id: 'paired',
      header: '配对时间',
      className: 'whitespace-nowrap',
      cell: (worker) => <span className="text-secondary">{formatTime(worker.paired_at)}</span>,
    },
    {
      id: 'actions',
      header: '',
      className: 'w-20 whitespace-nowrap text-right',
      cell: (worker) => (
        <Button color="secondary" variant="outline" size="sm" onClick={() => setDetailId(worker.id)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <section className="space-y-6" aria-labelledby="hub-devices-heading">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 id="hub-devices-heading" className="heading-md">
            采集电脑
          </h1>
          <p className="mt-1 text-sm text-secondary">
            查看设备连接状态、管理配对信息，并确认每台电脑正在占用的门店。
          </p>
        </div>
        <Button color="primary" size="sm" onClick={() => setPairOpen(true)}>
          配对采集电脑
        </Button>
      </header>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="采集电脑统计">
        <Metric label="已配对" value={activeWorkers.length} />
        <Metric label="在线" value={onlineCount} />
        <Metric label="占用门店" value={occupiedCount} />
        <Metric label="待补传文件" value={pendingUploads} {...(pendingUploads ? { tone: 'warning' as const } : {})} />
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

function Metric({ label, value, tone }: { label: string; value: number; tone?: 'warning' }) {
  return (
    <div className="rounded-xl border border-subtle bg-surface px-4 py-3">
      <span className="block text-xs text-secondary">{label}</span>
      <span className={`mt-1 block heading-lg tabular-nums ${tone === 'warning' && value > 0 ? 'text-warning' : 'text-default'}`}>
        {value}
      </span>
    </div>
  )
}
