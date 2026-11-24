/** 环境诊断 DTO；与服务端 `/api/connections`、`/api/environment` 的返回结构保持一致。 */

export type CheckStatus = 'checking' | 'ready' | 'warning' | 'error' | 'unknown'
export type ConnectionStatus = 'ready' | 'checking' | 'warning' | 'error' | 'unknown'

export type ActionKind = 'reconnect' | 'open_settings' | 'settings' | 'test'

export interface CheckAction {
  kind: ActionKind
  label: string
  /** 重连通常没有目标；系统设置指向 pane，测试指向检查项 ID。 */
  target?: string
  id?: string
}

export interface ConnectionModule {
  id: string
  name: string
  status: CheckStatus
  summary: string
  detail?: string
  checked_at: string
  actions: CheckAction[]
}

export interface EnvironmentCheck {
  id: string
  name: string
  status: CheckStatus
  checked_at: string
  summary: string
  detail: string
  actions: CheckAction[]
}

export interface DiagnosticsEvent {
  time: string
  stage: string
  status: string
  message: string
}

export interface ConnectionsSnapshot {
  status: ConnectionStatus
  busy: boolean
  checked_at: string
  summary: string
  modules: ConnectionModule[]
  preflight: { ready: boolean; blockers: string[] }
  diagnostics: {
    stage: string
    events: DiagnosticsEvent[]
    checks: EnvironmentCheck[]
  }
}

export interface EnvironmentSnapshot {
  checked_at: string
  preflight: { ready: boolean; blockers: string[] }
  checks: EnvironmentCheck[]
}

export interface SettingsPaneResult {
  message?: string
}
