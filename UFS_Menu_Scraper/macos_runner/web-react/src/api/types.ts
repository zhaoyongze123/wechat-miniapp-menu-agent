/** 共享端与采集端状态的最小 DTO；字段缺失时按“未知”展示，不推断事实。 */
export type AppRole = 'hub' | 'worker' | 'standalone'

export interface WorkerSummary {
  id: string
  name: string
  last_seen?: string
  pending_uploads?: number
  description?: string
  paired_at?: string
  revoked_at?: string
  archived_at?: string
}

export interface AssignmentSummary {
  id: string
  worker_id: string
  status: string
}

export interface HubStatus {
  role: 'hub'
  workers: WorkerSummary[]
  assignments: AssignmentSummary[]
}

/** 本机分配记录：只包含后端可靠字段，用于同步状态汇总，不推断逐文件上传进度。 */
export interface WorkerAssignmentRecord {
  id: string
  task_id: string
  batch_id?: string
  batch_name?: string
  store?: string
  revision?: string
  status: string
  disposition?: string
}

export interface WorkerConnectionStatus {
  role: 'worker'
  paired: boolean
  online: boolean
  name?: string
  error?: string
  credential_invalid?: boolean
  pending_uploads?: number
  assignments?: WorkerAssignmentRecord[]
}

export interface StandaloneStatus {
  role: 'standalone'
}

export type SharedStatus = HubStatus | WorkerConnectionStatus | StandaloneStatus

export function roleOf(status: SharedStatus | undefined): AppRole {
  if (!status) return 'standalone'
  if (status.role === 'hub' || status.role === 'worker') return status.role
  return 'standalone'
}
