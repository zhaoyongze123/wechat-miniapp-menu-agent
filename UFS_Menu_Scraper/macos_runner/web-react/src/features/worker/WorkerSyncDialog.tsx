import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { AppDialog } from '../../components/AppDialog'
import type { WorkerAssignmentRecord, WorkerConnectionStatus } from '../../api/types'

interface WorkerSyncDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  connection: WorkerConnectionStatus | null
}

function summaryOf(connection: WorkerConnectionStatus | null): string {
  if (!connection) return '正在读取本机连接状态…'
  if (connection.credential_invalid) return '配对已失效，待上传结果保存在本机。'
  if (!connection.paired) return '本机尚未连接共享工作台。'
  return connection.online
    ? '本机与共享工作台连接正常。'
    : '工作台暂不可达，已下载门店仍可继续采集。'
}

function metaOf(record: WorkerAssignmentRecord): string {
  return [record.batch_name || record.batch_id, record.revision].filter(Boolean).join(' · ')
}

/**
 * 本机同步详情：只展示后端已持久化的分配与结果去向，
 * 不用文件数或时间推断逐文件上传进度。
 */
export function WorkerSyncDialog({ open, onOpenChange, connection }: WorkerSyncDialogProps) {
  const records = connection?.assignments ?? []
  const pending = records.filter((record) => record.status === 'pending_upload')
  const confirmed = records.filter((record) => record.status === 'completed').slice(0, 20)

  return (
    <AppDialog open={open} onOpenChange={onOpenChange} title="同步状态">
      <div className="space-y-5">
        <div className="space-y-2">
          <p className="text-sm text-secondary">{summaryOf(connection)}</p>
          {connection?.error ? (
            <Alert color="danger" variant="soft" title="最近一次同步错误" description={connection.error} />
          ) : null}
        </div>

        <section className="space-y-2" aria-labelledby="worker-sync-pending-heading">
          <h3 id="worker-sync-pending-heading" className="heading-xs">
            待上传
          </h3>
          {pending.length ? (
            <ul className="divide-y divide-subtle rounded-lg border border-subtle">
              {pending.map((record) => (
                <li key={record.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span className="min-w-0 truncate" title={record.store || record.task_id}>
                    {record.store || record.task_id}
                  </span>
                  <span className="min-w-0 truncate text-xs text-tertiary">{metaOf(record)}</span>
                  <span className="text-xs text-tertiary">
                    {record.disposition === 'history' ? '旧结果待确认' : '等待上传'}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-tertiary">没有待上传的采集结果。</p>
          )}
        </section>

        <section className="space-y-2" aria-labelledby="worker-sync-confirmed-heading">
          <h3 id="worker-sync-confirmed-heading" className="heading-xs">
            最近确认
          </h3>
          {confirmed.length ? (
            <ul className="divide-y divide-subtle rounded-lg border border-subtle">
              {confirmed.map((record) => (
                <li key={record.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span className="min-w-0 truncate" title={record.store || record.task_id}>
                    {record.store || record.task_id}
                  </span>
                  <span className="min-w-0 truncate text-xs text-tertiary">{metaOf(record)}</span>
                  <span className="text-xs text-tertiary">
                    {record.disposition === 'history' ? '后台留档' : '后台已确认'}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-tertiary">还没有后台确认的结果。</p>
          )}
        </section>
      </div>
    </AppDialog>
  )
}
