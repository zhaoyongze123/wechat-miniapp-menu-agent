import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { Select } from '@openai/apps-sdk-ui/components/Select'
import { CopyButton } from '../../components/CopyButton'
import { SettingRow, SettingSection } from '../../components/SettingsLayout'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { formatTime } from '../hub/format'
import { formatBytes } from '../settings/format'
import { useUpdateActions, useUpdateHistory, useUpdateStatus } from './hooks'
import type { UpdateHistoryRow } from './types'

const PLATFORM_LABEL: Record<string, string> = {
  windows: 'Windows 采集端',
  linux: '共享后台 / Linux',
  macos: 'macOS 开发环境',
}

const ACTION_LABEL: Record<string, string> = {
  check: '检查更新',
  preferences: '保存偏好',
  download: '下载更新包',
}

function historyLabel(row: UpdateHistoryRow): string {
  return ACTION_LABEL[row.action] ?? row.action
}

/** 设置中心软件更新：只展示服务端真实状态，不伪造安装或回滚能力。 */
export function UpdatesPanel() {
  const toast = useToast()
  const statusQuery = useUpdateStatus()
  const historyQuery = useUpdateHistory()
  const actions = useUpdateActions()

  if (statusQuery.isPending) {
    return <StatePanel variant="loading" title="正在读取更新状态…" />
  }
  if (statusQuery.isError || !statusQuery.data) {
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

  const status = statusQuery.data
  const latest = status.latest
  const preferences = status.preferences
  const download = status.download
  const preferenceBusy = actions.savePreferences.isPending
  const checkBusy = actions.check.isPending
  const downloadBusy = actions.download.isPending || actions.cancelDownload.isPending
  const downloadPercent = download.total > 0
    ? Math.min(100, Math.round((download.downloaded / download.total) * 100))
    : 0
  // 已下载并校验通过时不再重复提供下载入口；安装仍需按发布说明人工执行。
  const canDownload = status.capabilities.download && status.update_available
    && !download.active && download.status !== 'completed'

  const savePreferences = (partial: { auto_check?: boolean; channel?: 'stable' | 'preview' }) => {
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

  return (
    <div className="space-y-6">
      <SettingSection
        id="updates-software"
        title="软件更新"
        description="从固定 GitHub 正式 Release 发现版本，并在校验通过后下载更新包；安装与回滚执行器尚未启用。"
      >
        <Alert
          color="warning"
          variant="soft"
          title="当前阶段提供版本发现、发布清单校验与受控下载"
          description="更新包只写入数据目录并校验 SHA-256，不会自动安装或切换运行版本，也不会中断正在进行的采集。"
        />

        <div className="grid gap-3 sm:grid-cols-3">
          <SummaryCard label="当前运行版本" value={status.current_version || '未知'} hint={PLATFORM_LABEL[status.platform] ?? status.platform} />
          <SummaryCard
            label="最新可用版本"
            value={latest?.version || '尚未检查'}
            hint={
              latest && !latest.eligible
                ? `不可用：${latest.reason}`
                : status.update_available
                  ? '发现可更新版本'
                  : latest?.eligible && latest.min_supported_version && latest.version !== status.current_version
                    ? `当前版本低于最低兼容版本 ${latest.min_supported_version}，需传统升级`
                    : '当前渠道没有更高版本'
            }
            tone={status.update_available ? 'success' : latest && !latest.eligible ? 'warning' : 'secondary'}
          />
          <SummaryCard
            label="最近检查时间"
            value={status.last_checked ? formatTime(status.last_checked) : '尚未检查'}
            hint={status.stale ? (status.checking ? '正在后台检查，可稍后刷新' : '信息已过期，可手动刷新') : '缓存信息在有效期内'}
            tone={status.stale ? 'warning' : 'secondary'}
          />
        </div>

        {status.error ? (
          <Alert color="danger" variant="soft" title="最近一次检查失败" description={status.error} />
        ) : null}

        {status.capabilities.download && (status.update_available || download.status !== 'idle') ? (
          <div className="space-y-2 rounded-lg border border-subtle px-3 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="text-sm text-default">更新包下载</p>
                <p className="text-2xs text-tertiary">
                  {download.status === 'completed'
                    ? `已下载 ${download.asset_name} 并通过 SHA-256 校验；安装执行器尚未启用。`
                    : download.active
                      ? `正在${download.status === 'verifying' ? '校验' : '下载'} ${download.asset_name}`
                      : '下载只写入数据目录，支持断点续传，完成后校验 SHA-256；不会自动安装。'}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {download.active ? (
                  <Button color="secondary" variant="outline" size="sm" disabled={downloadBusy} onClick={cancelDownload}>
                    取消下载
                  </Button>
                ) : canDownload ? (
                  <Button color="primary" size="sm" disabled={downloadBusy} onClick={startDownload}>
                    {download.resumable ? '继续下载' : '下载更新包'}
                  </Button>
                ) : null}
              </div>
            </div>
            {download.active || download.status === 'completed' ? (
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-secondary-soft"
                role="progressbar"
                aria-label="更新包下载进度"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={download.status === 'completed' ? 100 : downloadPercent}
              >
                <div
                  className="h-full rounded-full bg-primary-solid transition-all"
                  style={{ width: `${download.status === 'completed' ? 100 : downloadPercent}%` }}
                />
              </div>
            ) : null}
            {download.active ? (
              <p className="text-2xs text-secondary">
                {formatBytes(download.downloaded)} / {formatBytes(download.total)} · {downloadPercent}%
              </p>
            ) : null}
            {download.status === 'completed' ? (
              <p className="text-2xs text-tertiary">
                {formatBytes(download.downloaded)} · {download.asset_name}
              </p>
            ) : null}
            {download.error ? (
              <Alert color="danger" variant="soft" title="下载未完成" description={download.error} />
            ) : null}
          </div>
        ) : null}

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

        <div className="flex flex-wrap items-center gap-2 border-t border-subtle pt-3">
          <Button color="primary" size="sm" disabled={checkBusy} onClick={checkNow}>
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
        </div>

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
      </SettingSection>

      <SettingSection
        id="updates-inventory"
        title="版本管理"
        description="只读清点本机程序版本；保护运行中、当前与最近两个可回滚版本，不执行删除。"
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
                    {formatBytes(release.size_bytes)}
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

      <SettingSection id="updates-history" title="更新记录" description="检查与偏好变更会持久化记录；不包含业务采集结果。">
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
    </div>
  )
}

function SummaryCard({
  label,
  value,
  hint,
  tone = 'secondary',
}: {
  label: string
  value: string
  hint: string
  tone?: 'success' | 'warning' | 'secondary' | 'info'
}) {
  return (
    <div className="rounded-xl border border-subtle bg-surface px-4 py-3">
      <span className="block text-xs text-secondary">{label}</span>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <span className="heading-sm text-default">{value}</span>
        {tone !== 'secondary' ? <StatusBadge tone={tone}>{tone === 'success' ? '可更新' : tone === 'warning' ? '需处理' : '信息'}</StatusBadge> : null}
      </div>
      <p className="mt-1 text-2xs text-tertiary">{hint}</p>
    </div>
  )
}
