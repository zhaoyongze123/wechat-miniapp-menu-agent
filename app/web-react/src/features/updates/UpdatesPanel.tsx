import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { useEffect, useRef, useState } from 'react'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { CopyButton } from '../../components/CopyButton'
import { SettingRow, SettingSection } from '../../components/SettingsLayout'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatTime } from '../hub/format'
import { formatBytes } from '../settings/format'
import { useUpdateActions, useUpdateCleanup, useUpdateHistory, useUpdateInstall, useUpdateStatus } from './hooks'
import type { UpdateFlowStage, UpdateHistoryRow, UpdateMode } from './types'

const PLATFORM_LABEL: Record<string, string> = {
  windows: 'Windows 采集端',
  linux: '共享后台 / Linux',
  macos: 'macOS 开发环境',
}

const ACTION_LABEL: Record<string, string> = {
  check: '检查更新',
  preferences: '保存偏好',
  download: '下载更新包',
  install: '安装更新',
  schedule: '空闲后更新',
  update: '一次授权更新',
  cleanup: '清理存储',
}

const INSTALL_STATUS_LABEL: Record<string, string> = {
  idle: '未开始',
  starting: '正在启动独立安装进程',
  installing: '正在安装',
  verifying: '正在核对新版本文件',
  restarting: '正在切换运行版本',
  rolling_back: '安装失败，正在回滚',
  completed: '安装完成',
  failed: '安装失败',
  rolled_back: '已回滚到上一版本',
}

const FLOW_STAGE_LABEL: Record<UpdateFlowStage, string> = {
  idle: '',
  downloading: '正在下载并校验更新包',
  waiting: '等待采集任务结束后安装',
  installing: '正在安装更新',
  verifying: '正在核对新版本文件',
  restarting: '正在重启服务',
  rolling_back: '安装失败，正在回滚',
  completed: '更新完成',
  failed: '更新失败',
  rolled_back: '已回滚到上一版本',
}

const DOWNLOAD_METHOD_LABEL: Record<string, string> = {
  windows_patch: 'Windows 文件级增量补丁',
  windows_installer: 'Windows 完整安装包',
  hub_image: 'Hub 完整镜像归档',
}

function historyLabel(row: UpdateHistoryRow): string {
  return ACTION_LABEL[row.action] ?? row.action
}

/** 只保留少量更新说明用于首屏，完整内容仍在下方“更新说明”中。 */
function shortNotes(notes: string): string {
  const text = notes.trim()
  if (!text) return ''
  const lines = text.split(/\r?\n/).filter((line) => line.trim())
  const joined = lines.slice(0, 4).join('\n')
  return joined.length > 320 ? `${joined.slice(0, 320)}…` : joined
}

/** 设置中心软件更新：一次授权完成下载、等待空闲、安装、重启与重连。 */
export function UpdatesPanel() {
  const toast = useToast()
  const statusQuery = useUpdateStatus()
  const installQuery = useUpdateInstall()
  const historyQuery = useUpdateHistory()
  const cleanupQuery = useUpdateCleanup(statusQuery.data?.capabilities.cleanup ?? false)
  const actions = useUpdateActions()
  const [confirmCleanup, setConfirmCleanup] = useState(false)
  const initialVersion = useRef('')

  const status = statusQuery.data
  const currentVersion = status?.current_version ?? ''
  useEffect(() => {
    if (!initialVersion.current && currentVersion) initialVersion.current = currentVersion
  }, [currentVersion])

  const flow = status?.flow
  const flowStage = flow?.stage ?? 'idle'
  const flowVersion = flow?.version ?? ''
  // 服务重启后先让页面重连，确认运行版本已是目标版本再加载新版前端；只自动刷新一次。
  useEffect(() => {
    if (flowStage !== 'completed' || !flowVersion) return
    if (currentVersion !== flowVersion) return
    if (!initialVersion.current || initialVersion.current === currentVersion) return
    try {
      const key = 'menu-capture-update-reloaded'
      if (window.sessionStorage.getItem(key) === flowVersion) return
      window.sessionStorage.setItem(key, flowVersion)
      window.setTimeout(() => window.location.reload(), 800)
    } catch {
      // 存储不可用时只展示完成状态，不强制刷新。
    }
  }, [flowStage, flowVersion, currentVersion])

  if (statusQuery.isPending && !status) {
    return <StatePanel variant="loading" title="正在读取更新状态…" />
  }
  if (!status) {
    return (
      <StatePanel
        variant="error"
        title="更新状态读取失败"
        description={statusQuery.isError ? errorMessage(statusQuery.error) : '未返回更新状态'}
        action={
          <Button color="secondary" variant="outline" size="sm" onClick={() => void statusQuery.refetch()}>
            重试
          </Button>
        }
      />
    )
  }

  const latest = status.latest
  const preferences = status.preferences
  const download = status.download
  const schedule = status.schedule
  const install = installQuery.data
  const usesHubOci = status.platform === 'linux' && schedule.delivery === 'oci'
  const downloadMethod = usesHubOci
    ? 'Hub OCI 分层镜像'
    : DOWNLOAD_METHOD_LABEL[download.asset_kind] ?? download.asset_kind
  const diagnosticVersion = usesHubOci
    ? flowVersion || schedule.version || install?.target_version || ''
    : download.version
  const diagnosticBaseVersion = download.from_version || (usesHubOci ? install?.previous_version ?? '' : '')
  const preferenceBusy = actions.savePreferences.isPending
  const checkBusy = actions.check.isPending
  const downloadBusy = actions.download.isPending || actions.cancelDownload.isPending
  const downloadPercent = download.total > 0
    ? Math.min(100, Math.round((download.downloaded / download.total) * 100))
    : 0
  // 下载完成必须与当前最新版本及资产绑定，否则旧版本的 completed 会挡住新版本下载。
  const latestAsset = latest?.assets.find((asset) => asset.kind === download.asset_kind)
  const downloadedCurrent = download.status === 'completed'
    && latest != null
    && download.version === latest.version
    && (!latestAsset || latestAsset.name === download.asset_name)
  const canDownload = status.capabilities.download && status.update_available
    && !download.active && !downloadedCurrent
  const showProgress = download.active || downloadedCurrent
  const installReadError = installQuery.isError ? errorMessage(installQuery.error) : ''
  const storage = cleanupQuery.data
  const cleanupBusy = actions.cleanup.isPending
  const storageScopes = storage
    ? [
        { key: 'releases', label: '历史程序版本', scope: storage.scopes.releases },
        { key: 'downloads', label: '下载缓存', scope: storage.scopes.downloads },
        { key: 'logs', label: 'CLI 日志（超过 7 天）', scope: storage.scopes.logs },
      ]
    : []
  const cleanupItemCount = storageScopes.reduce((total, row) => total + row.scope.items.length, 0)
  // 历史版本体积由服务端后台统计；未完成时不能把 0 当成真实可释放空间。
  const storagePending = storageScopes.some((row) => row.scope.sizes_pending)
  const canCleanup = storage != null && !storage.blocked && cleanupItemCount > 0 && !cleanupBusy

  const flowActive = flow?.active ?? false
  const flowFailed = flowStage === 'failed' || flowStage === 'rolled_back'
  const justUpdated = flowStage === 'completed'
    && flowVersion !== ''
    && flowVersion === status.current_version
    && initialVersion.current !== ''
    && initialVersion.current !== status.current_version
  const canAuthorize = status.capabilities.install && status.update_available && !flowActive
  const headlineVersion = (flowActive || flowFailed || justUpdated) && flowVersion
    ? flowVersion
    : status.update_available
      ? latest?.version || flowVersion || status.current_version
      : status.current_version

  const authorize = (mode: UpdateMode) => {
    actions.update.mutate(mode, {
      onSuccess: () => toast.push(
        mode === 'immediate' ? '已开始更新，后台会自动完成安装与重启' : '已安排空闲后更新，采集结束会自动开始',
        'info',
      ),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const cancelUpdate = () => {
    actions.cancelUpdate.mutate(undefined, {
      onSuccess: () => toast.push('已取消本次更新，已下载部分会保留', 'info'),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const savePreferences = (partial: { auto_check?: boolean; auto_download?: boolean; keep_versions?: number; channel?: 'stable' | 'preview' }) => {
    actions.savePreferences.mutate(partial, {
      onSuccess: () => toast.push('更新偏好已保存', 'success'),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const checkNow = () => {
    actions.check.mutate(undefined, {
      onSuccess: (next) => {
        toast.push(next.update_available ? `发现新版本 ${next.latest?.version ?? ''}` : '已是最新状态', next.update_available ? 'info' : 'success')
      },
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const startDownload = () => {
    actions.download.mutate(undefined, {
      onSuccess: (state) => toast.push(
        state.status === 'completed' ? '更新包已下载并通过校验' : '已开始下载更新包',
        'info',
      ),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const cancelDownload = () => {
    actions.cancelDownload.mutate(undefined, {
      onSuccess: () => toast.push('已请求取消下载，已下载部分会保留', 'info'),
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  const startCleanup = () => {
    if (!storage) return
    const scopes = storageScopes.filter((row) => row.scope.items.length > 0).map((row) => row.key)
    actions.cleanup.mutate(scopes, {
      onSuccess: (result) => {
        setConfirmCleanup(false)
        toast.push(
          result.failures.length
            ? `已清理 ${result.deleted.length} 项，${result.failures.length} 项失败`
            : `已清理 ${result.deleted.length} 项，释放 ${formatBytes(result.freed_bytes)}`,
          result.failures.length ? 'danger' : 'success',
        )
      },
      onError: (error) => toast.push(errorMessage(error), 'danger'),
    })
  }

  return (
    <div className="space-y-6">
      <SettingSection
        id="updates-software"
        title="软件更新"
        description="一次授权完成下载、校验、等待空闲、安装与重启；技术细节收在高级设置中。"
      >
        {statusQuery.isError ? (
          <Alert
            color="warning"
            variant="soft"
            title="暂时无法连接更新服务"
            description="服务可能正在重启，页面会自动重连；无需重复操作。"
          />
        ) : null}
        {status.error ? (
          <Alert color="danger" variant="soft" title="最近一次检查失败" description={status.error} />
        ) : null}

        <div className="rounded-xl border border-subtle bg-surface px-5 py-4">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0 flex-1 space-y-2">
              <p className="text-xs text-secondary">软件更新</p>
              <div className="flex flex-wrap items-center gap-2">
                <span className="heading-lg text-default">{headlineVersion || '未知版本'}</span>
                {flowActive ? (
                  <StatusBadge tone="warning">正在更新</StatusBadge>
                ) : justUpdated ? (
                  <StatusBadge tone="success">已更新</StatusBadge>
                ) : flowStage === 'rolled_back' ? (
                  <StatusBadge tone="warning">已回滚</StatusBadge>
                ) : flowFailed ? (
                  <StatusBadge tone="danger">更新失败</StatusBadge>
                ) : status.update_available ? (
                  <StatusBadge tone="info">有新版本可用</StatusBadge>
                ) : (
                  <StatusBadge tone="secondary">已是最新</StatusBadge>
                )}
              </div>
              <p className="text-sm text-secondary">
                {flowActive
                  ? [FLOW_STAGE_LABEL[flowStage], flow?.message].filter(Boolean).join(' · ')
                  : justUpdated
                    ? `已更新至 ${flowVersion}，当前运行版本 ${status.current_version}`
                    : flowFailed
                      ? flow?.error || flow?.message || '更新未完成，可重试'
                      : status.update_available
                        ? `当前版本 ${status.current_version}，可更新至 ${latest?.version || flowVersion}`
                        : `当前版本 ${status.current_version}，已是最新版本`}
              </p>
              {latest && status.update_available && shortNotes(latest.notes) ? (
                <div className="max-h-24 overflow-y-auto whitespace-pre-wrap rounded-lg border border-subtle px-3 py-2 text-xs leading-relaxed text-secondary">
                  {shortNotes(latest.notes)}
                </div>
              ) : null}
              <p className="text-2xs text-tertiary">
                更新不会中断正在进行的采集任务；安装期间服务会短暂重启，完成后页面会自动重连。
              </p>
            </div>
            <div className="flex flex-col items-stretch gap-2 sm:min-w-44">
              {flowActive ? (
                flowStage === 'downloading' || flowStage === 'waiting' ? (
                  <Button color="secondary" variant="outline" size="sm" disabled={actions.cancelUpdate.isPending} onClick={cancelUpdate}>
                    {actions.cancelUpdate.isPending ? '正在取消…' : '取消更新'}
                  </Button>
                ) : (
                  <p className="text-2xs text-tertiary">安装已开始，无法取消；请保持服务运行。</p>
                )
              ) : justUpdated ? (
                <Button color="secondary" variant="outline" size="sm" disabled={checkBusy} onClick={checkNow}>
                  检查更新
                </Button>
              ) : flowFailed ? (
                <>
                  <Button color="primary" size="sm" disabled={actions.update.isPending} onClick={() => authorize('immediate')}>
                    重试更新
                  </Button>
                  <Button color="secondary" variant="outline" size="sm" disabled={checkBusy} onClick={checkNow}>
                    检查更新
                  </Button>
                </>
              ) : canAuthorize ? (
                <>
                  <Button color="primary" size="sm" disabled={actions.update.isPending} onClick={() => authorize('immediate')}>
                    {actions.update.isPending ? '正在启动…' : '立即更新'}
                  </Button>
                  <Button color="secondary" variant="outline" size="sm" disabled={actions.update.isPending} onClick={() => authorize('idle')}>
                    空闲后更新
                  </Button>
                </>
              ) : status.update_available ? (
                <p className="text-2xs text-tertiary">当前环境不支持应用内安装，请使用安装包升级。</p>
              ) : (
                <Button color="secondary" variant="outline" size="sm" disabled={checkBusy} onClick={checkNow}>
                  {checkBusy ? '检查中…' : '检查更新'}
                </Button>
              )}
            </div>
          </div>
          {flowActive && flowStage === 'downloading' && !usesHubOci ? (
            <div className="mt-3 space-y-1">
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-secondary-soft"
                role="progressbar"
                aria-label="更新包下载进度"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={flow?.progress ?? downloadPercent}
              >
                <div className="h-full rounded-full bg-primary-solid transition-all" style={{ width: `${flow?.progress ?? downloadPercent}%` }} />
              </div>
              <p className="text-2xs text-secondary">
                {formatBytes(download.downloaded)} / {formatBytes(download.total)} · {flow?.progress ?? downloadPercent}%
              </p>
            </div>
          ) : null}
          {flowActive ? (
            <p className="mt-2 text-2xs text-tertiary">可以关闭页面，后台会继续完成更新。</p>
          ) : null}
        </div>

        <details className="rounded-lg border border-subtle px-3 py-2">
          <summary className="cursor-pointer text-sm text-default">高级设置与诊断</summary>
          <div className="mt-3 space-y-3">
            <p className="text-2xs text-tertiary">
              以下为发布渠道、自动下载偏好、版本保留与校验信息；日常更新不需要修改。
            </p>
            <SettingRow label="发布渠道" description="默认只接收正式稳定版；预览渠道仅用于明确授权的验证。">
              <Select
                aria-label="发布渠道"
                value={preferences.channel}
                disabled={preferenceBusy}
                options={[
                  { value: 'stable', label: '正式版（stable）' },
                  { value: 'preview', label: '预览版（preview）' },
                ]}
                onChange={(option) => savePreferences({ channel: option.value as 'stable' | 'preview' })}
              />
            </SettingRow>

            <SettingRow label="自动检查" description="服务端按小时缓存检查结果，不会让每台设备高频轮询 GitHub。">
              <Checkbox
                label="自动检查正式发布"
                checked={preferences.auto_check}
                disabled={preferenceBusy}
                onCheckedChange={(checked) => savePreferences({ auto_check: checked })}
              />
            </SettingRow>

            <SettingRow label="自动下载" description="发现新版本后只在后台下载并校验，不会自动安装或打断采集。">
              <Checkbox
                label="自动下载更新包"
                checked={preferences.auto_download}
                disabled={preferenceBusy}
                onCheckedChange={(checked) => savePreferences({ auto_download: checked })}
              />
            </SettingRow>

            <SettingRow
              label="已安装程序保留数量"
              description="包含当前版本；运行中、待安装与回滚目标始终受保护，清理前仍需确认。"
            >
              <Select
                aria-label="已安装程序保留数量"
                value={String(preferences.keep_versions)}
                disabled={preferenceBusy}
                options={[2, 3, 4, 5].map((value) => ({ value: String(value), label: `${value} 个版本` }))}
                onChange={(option) => savePreferences({ keep_versions: Number(option.value) })}
              />
            </SettingRow>

            <div className="flex flex-wrap items-center gap-2 border-t border-subtle pt-3">
              <Button color="secondary" variant="outline" size="sm" disabled={checkBusy} onClick={checkNow}>
                {checkBusy ? '检查中…' : '手动检查更新'}
              </Button>
              {latest?.html_url ? (
                <a
                  className="rounded-md border border-subtle px-3 py-1.5 text-sm text-default hover:bg-secondary-ghost-hover"
                  href={latest.html_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看 Release
                </a>
              ) : null}
              <Button color="secondary" variant="ghost" size="sm" onClick={() => void historyQuery.refetch()}>
                刷新记录
              </Button>
              <span className="text-2xs text-tertiary">
                {PLATFORM_LABEL[status.platform] ?? status.platform} · 当前 {status.current_version || '未知'} · 检查于{' '}
                {status.last_checked ? formatTime(status.last_checked) : '尚未检查'}
                {status.stale ? '（已过期）' : ''}
              </span>
            </div>

            {status.capabilities.download && (status.update_available || download.status !== 'idle') ? (
              <div className="space-y-2 rounded-lg border border-subtle px-3 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm text-default">更新包下载</p>
                    <p className="text-2xs text-tertiary">
                      {downloadedCurrent
                        ? `已下载 ${download.asset_name} 并通过 SHA-256 校验。`
                        : download.active
                          ? `正在${download.status === 'verifying' ? '校验' : '下载'} ${download.asset_name}`
                          : download.status === 'completed' && download.version
                            ? `已下载 ${download.version}；当前最新为 ${latest?.version ?? '未知'}。`
                            : '下载只写入数据目录，支持断点续传，完成后校验 SHA-256。'}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {download.active ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={downloadBusy} onClick={cancelDownload}>
                        取消下载
                      </Button>
                    ) : canDownload ? (
                      <Button color="secondary" variant="outline" size="sm" disabled={downloadBusy} onClick={startDownload}>
                        {download.resumable ? '继续下载' : '仅下载更新包'}
                      </Button>
                    ) : null}
                  </div>
                </div>
                {showProgress ? (
                  <div
                    className="h-2 w-full overflow-hidden rounded-full bg-secondary-soft"
                    role="progressbar"
                    aria-label="更新包下载进度"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={downloadedCurrent ? 100 : downloadPercent}
                  >
                    <div className="h-full rounded-full bg-primary-solid transition-all" style={{ width: `${downloadedCurrent ? 100 : downloadPercent}%` }} />
                  </div>
                ) : null}
                {download.error ? (
                  <Alert color="danger" variant="soft" title="下载未完成" description={download.error} />
                ) : null}
              </div>
            ) : null}

            {usesHubOci || download.status !== 'idle' || download.asset_kind || download.fallback_reason ? (
              <div className="space-y-2 rounded-lg border border-subtle px-3 py-3">
                <p className="text-sm text-default">{flowActive || schedule.armed ? '本次交付诊断' : '最近一次交付诊断'}</p>
                <div className="grid gap-1 text-2xs text-secondary sm:grid-cols-2">
                  {downloadMethod ? (
                    <p><span className="text-tertiary">交付方式：</span>{downloadMethod}</p>
                  ) : null}
                  {diagnosticVersion ? (
                    <p><span className="text-tertiary">目标版本：</span>{diagnosticVersion}</p>
                  ) : null}
                  {diagnosticBaseVersion ? (
                    <p><span className="text-tertiary">增量基线：</span>{diagnosticBaseVersion} → {diagnosticVersion || '目标版本'}</p>
                  ) : null}
                  {usesHubOci ? (
                    <p><span className="text-tertiary">网络传输量：</span>由 Hub 宿主执行器处理，本页不显示估算字节或百分比</p>
                  ) : download.total > 0 ? (
                    <p><span className="text-tertiary">更新包大小：</span>{formatBytes(download.total)} · 已下载 {formatBytes(download.downloaded)}</p>
                  ) : null}
                </div>
                {!usesHubOci && download.fallback_reason ? (
                  <Alert
                    color="warning"
                    variant="soft"
                    title="已回退到完整安装包"
                    description={`增量补丁未能使用：${download.fallback_reason}`}
                  />
                ) : null}
              </div>
            ) : null}

            {install != null && (install.supported || install.status !== 'idle' || install.reason) ? (
              <div className="space-y-2 rounded-lg border border-subtle px-3 py-3">
                <p className="text-sm text-default">安装与切换</p>
                <p className="text-2xs text-tertiary">
                  {install.supported ? INSTALL_STATUS_LABEL[install.status] ?? install.status : install.reason || '安装执行器未启用'}
                  {install.target_version ? ` · 目标 ${install.target_version}` : ''}
                  {install.previous_version ? ` · 上一版本 ${install.previous_version}` : ''}
                </p>
                {install.message ? <p className="text-2xs text-secondary">{install.message}</p> : null}
                {install.error ? (
                  <Alert color="danger" variant="soft" title="安装未完成" description={install.error} />
                ) : null}
                {installReadError ? <p className="text-2xs text-danger">安装状态读取失败：{installReadError}</p> : null}
                {schedule.armed ? <p className="text-2xs text-secondary">当前安排：{schedule.note || '等待空闲'}</p> : null}
                {schedule.last_result && schedule.last_result !== 'started' ? (
                  <p className="text-2xs text-danger">安排未执行：{schedule.last_result}</p>
                ) : null}
              </div>
            ) : null}

            {latest ? (
              <details className="rounded-lg border border-subtle px-3 py-2" open={status.update_available}>
                <summary className="cursor-pointer text-sm text-default">
                  更新说明 · {latest.name || latest.version}
                  {status.update_available ? <span className="ml-2 text-xs text-warning">有新版本</span> : null}
                </summary>
                <div className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-secondary">
                  {latest.notes || '该 Release 没有提供更新说明。'}
                </div>
                {latest.git_commit ? (
                  <p className="mt-2 text-2xs text-tertiary">源码提交 {latest.git_commit.slice(0, 12)}</p>
                ) : null}
              </details>
            ) : null}

            {latest?.assets?.length ? (
              <div className="space-y-2">
                <h3 className="text-sm text-default">发布资产与完整性校验</h3>
                <ul className="divide-y divide-subtle rounded-lg border border-subtle">
                  {latest.assets.map((asset) => (
                    <li key={asset.kind} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs">
                      <span className="min-w-0">
                        <span className="block truncate text-default" title={asset.name}>{asset.name}</span>
                        <span className="text-tertiary">{formatBytes(asset.size)} · SHA-256 {asset.sha256.slice(0, 12)}…</span>
                      </span>
                      <CopyButton value={asset.sha256} label={`${asset.kind} SHA-256`} />
                    </li>
                  ))}
                </ul>
                <p className="text-2xs text-tertiary">
                  SHA-256 只证明资产完整性；发布者真实性依赖固定仓库的正式 Release 语义。
                </p>
              </div>
            ) : null}
          </div>
        </details>
      </SettingSection>

      <SettingSection
        id="updates-inventory"
        title="版本管理"
        description="只读清点本机程序版本，保护运行中、当前与最近两个可回滚版本；清理在“存储治理”中确认后执行。"
      >
        {status.inventory.supported ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-xs text-secondary">
              <span>安装目录：<code className="font-mono">{status.inventory.install_root}</code></span>
              <span>当前版本：{status.inventory.current_version || '未知'}</span>
              <span>运行版本：{status.inventory.running_version || '未知'}</span>
            </div>
            <ul className="divide-y divide-subtle rounded-lg border border-subtle">
              {status.inventory.releases.map((release) => (
                <li key={release.version} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="text-default">{release.version}</span>
                    {release.running ? <StatusBadge tone="success">运行中</StatusBadge> : null}
                    {release.current ? <StatusBadge tone="info">当前版本</StatusBadge> : null}
                    {release.protected ? <StatusBadge tone="secondary">受保护</StatusBadge> : null}
                  </span>
                  <span className="text-xs text-tertiary">
                    {release.size_bytes === null ? '正在统计…' : formatBytes(release.size_bytes)}
                    {release.built_at ? ` · ${formatTime(release.built_at)}` : ''}
                  </span>
                </li>
              ))}
              {status.inventory.releases.length === 0 ? (
                <li className="px-3 py-3 text-sm text-secondary">没有找到带发布清单的已安装版本。</li>
              ) : null}
            </ul>
            <p className="text-xs text-secondary">
              {status.inventory.cleanup_candidates.length
                ? `可清理候选 ${status.inventory.cleanup_candidates.length} 个：${status.inventory.cleanup_candidates.map((row) => row.version).join('、')}。`
                : '没有超出保留策略的版本。'}
              {status.inventory.cleanup_reason ? ` ${status.inventory.cleanup_reason}。` : ''}
            </p>
          </>
        ) : (
          <StatePanel variant="info" title="当前环境不支持程序版本清点" description={status.inventory.reason || '仅 Windows 安装版可清点 releases 目录。'} />
        )}
      </SettingSection>

      <SettingSection
        id="updates-storage"
        title="存储治理"
        description="先只读预览可清理项，确认后才删除；保护运行中/当前/最近两个版本、待安装包与业务数据。"
      >
        {cleanupQuery.isError ? (
          <p className="text-xs text-danger">存储预览读取失败：{errorMessage(cleanupQuery.error)}</p>
        ) : !storage ? (
          <p className="text-xs text-secondary">正在统计可清理项…</p>
        ) : (
          <>
            <Alert
              color="warning"
              variant="soft"
              title={storagePending ? '正在统计可释放空间…' : `可释放约 ${formatBytes(storage.reclaimable_bytes)}`}
              description={
                storage.blocked
                  ? storage.blocked_reason
                  : '只删除超出保留策略的版本、下载缓存与旧 CLI 转录，不会删除数据目录、数据库或采集结果。'
              }
            />
            <ul className="divide-y divide-subtle rounded-lg border border-subtle">
              {storageScopes.map((row) => (
                <li key={row.key} className="flex flex-wrap items-start justify-between gap-2 px-3 py-2 text-sm">
                  <span className="min-w-0">
                    <span className="block text-default">{row.label}</span>
                    <span className="text-2xs text-tertiary">
                      {row.scope.supported
                        ? row.scope.items.length
                          ? `${row.scope.items.length} 项 · ${row.scope.sizes_pending ? '正在统计…' : formatBytes(row.scope.reclaimable_bytes)}`
                          : '没有可清理项'
                        : row.scope.reason}
                    </span>
                    {row.scope.kept?.length ? (
                      <span className="block text-2xs text-tertiary">保留待安装：{row.scope.kept.join('、')}</span>
                    ) : null}
                    {row.scope.supported && row.scope.reason ? (
                      <span className="block text-2xs text-tertiary">{row.scope.reason}</span>
                    ) : null}
                  </span>
                  {row.scope.items.length ? (
                    <span className="text-xs text-secondary">
                      {row.scope.items
                        .slice(0, 3)
                        .map((item) => item.version ?? item.name ?? '')
                        .filter(Boolean)
                        .join('、')}
                      {row.scope.items.length > 3 ? ' 等' : ''}
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
            <div className="flex flex-wrap items-center gap-2">
              <Button color="secondary" variant="outline" size="sm" onClick={() => void cleanupQuery.refetch()}>
                重新统计
              </Button>
              <Button color="primary" size="sm" disabled={!canCleanup} onClick={() => setConfirmCleanup(true)}>
                {cleanupBusy ? '清理中…' : '清理可清理项'}
              </Button>
            </div>
          </>
        )}
      </SettingSection>

      <SettingSection id="updates-history" title="更新记录" description="检查、授权与偏好变更会持久化记录；不包含业务采集结果。">
        {historyQuery.isError ? (
          <p className="text-xs text-danger">{errorMessage(historyQuery.error)}</p>
        ) : historyQuery.data?.length ? (
          <ul className="divide-y divide-subtle rounded-lg border border-subtle text-xs">
            {historyQuery.data.map((row, index) => (
              <li key={`${row.time}-${index}`} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className="w-40 shrink-0 text-tertiary">{row.time ? formatTime(row.time) : '—'}</span>
                <span className="text-default">{historyLabel(row)}</span>
                <StatusBadge tone={row.result === 'ok' ? 'success' : 'danger'}>{row.result === 'ok' ? '成功' : '失败'}</StatusBadge>
                {row.version ? <span className="text-secondary">{row.version}</span> : null}
                <span className="min-w-0 flex-1 truncate text-secondary" title={row.message}>{row.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-secondary">暂无更新记录。</p>
        )}
      </SettingSection>

      <ConfirmActionDialog
        open={confirmCleanup}
        title="清理历史版本与缓存"
        description={
          <div className="space-y-2">
            <p>
              将删除超出保留策略的历史版本、旧下载缓存与超过 7 天的 CLI 转录，预计释放{' '}
              {formatBytes(storage?.reclaimable_bytes ?? 0)}。
            </p>
            <p>不会删除运行中/当前/最近两个版本、待安装的下载包、数据目录、数据库或采集结果。</p>
            <p>安装或回滚进行中时服务端会拒绝清理。</p>
          </div>
        }
        confirmLabel="确认清理"
        tone="danger"
        busy={cleanupBusy}
        onConfirm={startCleanup}
        onOpenChange={setConfirmCleanup}
      />
    </div>
  )
}
