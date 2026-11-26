import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Button } from '@openai/apps-sdk-ui/components/Button'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { AppDataTable, type DataColumn } from '../../components/AppDataTable'
import { AppDialog } from '../../components/AppDialog'
import { ConfirmActionDialog } from '../../components/ConfirmActionDialog'
import { FilterToolbar } from '../../components/FilterToolbar'
import { ListPageLayout } from '../../components/ListPageLayout'
import { SearchInput } from '../../components/SearchInput'
import { StatePanel } from '../../components/StatePanel'
import { StatusBadge } from '../../components/StatusBadge'
import { useToast } from '../../components/ToastProvider'
import { errorMessage } from '../../api/client'
import { useRole } from '../../app/RoleProvider'
import { archiveProject, createProject, fetchProjects, projectsKeys, restoreProject, updateProject, type Project } from './api'

type ProjectFilter = 'active' | 'archived'
type ProjectDraft = { code: string; name: string; description: string }

const EMPTY_DRAFT: ProjectDraft = { code: '', name: '', description: '' }
const CODE_PATTERN = /^(?!^(con|prn|aux|nul|com[1-9]|lpt[1-9])$)[a-zA-Z0-9-]{1,48}$/i

function suggestedCode(): string {
  const date = new Date()
  const stamp = [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0'), String(date.getHours()).padStart(2, '0'), String(date.getMinutes()).padStart(2, '0')].join('')
  return `project-${stamp}`
}

export function ProjectsPage() {
  const role = useRole()
  const toast = useToast()
  const navigate = useNavigate()
  const client = useQueryClient()
  const query = useQuery({ queryKey: projectsKeys.list(true, 'hub'), queryFn: ({ signal }) => fetchProjects(true, signal) })
  const [filter, setFilter] = useState<ProjectFilter>('active')
  const [search, setSearch] = useState('')
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<Project | null>(null)
  const [draft, setDraft] = useState<ProjectDraft>(EMPTY_DRAFT)
  const [formError, setFormError] = useState('')
  const [pendingArchive, setPendingArchive] = useState<Project | null>(null)
  const [archiveError, setArchiveError] = useState('')

  const activeProjects = query.data ?? []
  const rows = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase()
    return activeProjects.filter((project) => {
      const archived = Boolean(project.archived_at)
      if ((filter === 'archived') !== archived) return false
      if (!needle) return true
      return `${project.name} ${project.code} ${project.description}`.toLocaleLowerCase().includes(needle)
    })
  }, [activeProjects, filter, search])

  const closeForm = () => {
    setDialogOpen(false)
    setEditing(null)
    setDraft(EMPTY_DRAFT)
    setFormError('')
  }

  const invalidateProjects = () => client.invalidateQueries({ queryKey: projectsKeys.all })
  const saveMutation = useMutation({
    mutationFn: () => editing ? updateProject(editing.id, { name: draft.name.trim(), description: draft.description.trim() }) : createProject({ code: draft.code.trim(), name: draft.name.trim(), description: draft.description.trim() }),
    onSuccess: (project) => {
      void invalidateProjects()
      closeForm()
      toast.push(editing ? '项目资料已更新' : '项目已创建', 'success')
      if (!editing) navigate(`/workbench?project=${encodeURIComponent(project.id)}`)
    },
    onError: (cause) => setFormError(errorMessage(cause)),
  })
  const archiveMutation = useMutation({
    mutationFn: (project: Project) => archiveProject(project.id),
    onSuccess: () => {
      void invalidateProjects()
      setPendingArchive(null)
      setArchiveError('')
      toast.push('项目已归档，可在“已归档”中恢复', 'success')
    },
    onError: (cause) => setArchiveError(errorMessage(cause)),
  })
  const restoreMutation = useMutation({
    mutationFn: (project: Project) => restoreProject(project.id),
    onSuccess: () => {
      void invalidateProjects()
      toast.push('项目已恢复', 'success')
    },
    onError: (cause) => toast.push(errorMessage(cause), 'danger'),
  })

  const openCreate = () => {
    setEditing(null)
    setDraft({ ...EMPTY_DRAFT, code: suggestedCode() })
    setFormError('')
    setDialogOpen(true)
  }
  const openEdit = (project: Project) => {
    setEditing(project)
    setDraft({ code: project.code, name: project.name, description: project.description })
    setFormError('')
    setDialogOpen(true)
  }
  const validate = (): string => {
    if (!draft.name.trim()) return '请填写项目名称'
    if (draft.name.trim().length > 80) return '项目名称最多 80 个字符'
    if (!editing && !CODE_PATTERN.test(draft.code.trim())) return '标识仅支持 1–48 位 ASCII 字母、数字和连字符，且不能使用 Windows 保留名'
    const duplicate = !editing && activeProjects.some((project) => project.code.toLocaleLowerCase() === draft.code.trim().toLocaleLowerCase())
    if (duplicate) return '该项目标识已存在，请调整后重试'
    return ''
  }

  const columns: DataColumn<Project>[] = [
    { id: 'name', header: '项目', width: '30%', cell: (project) => <div className="min-w-0"><Link to={`/workbench?project=${encodeURIComponent(project.id)}`} className="font-medium text-default underline decoration-subtle underline-offset-2 hover:decoration-default">{project.name}</Link><p className="truncate text-2xs text-tertiary">{project.description || '未填写描述'}</p></div> },
    { id: 'code', header: '标识', width: '18%', cell: (project) => <code className="break-all text-xs text-secondary">{project.code}</code> },
    { id: 'status', header: '状态', width: '14%', cell: (project) => project.archived_at ? <StatusBadge tone="secondary">已归档</StatusBadge> : <StatusBadge tone="success">进行中</StatusBadge> },
    { id: 'updated', header: '更新时间', width: '20%', cell: (project) => project.updated_at ? <time className="text-xs text-secondary" dateTime={project.updated_at}>{project.updated_at}</time> : <span className="text-xs text-tertiary">未记录</span> },
    { id: 'actions', header: '操作', width: '18%', cell: (project) => <div className="flex flex-wrap gap-1">{!project.archived_at ? <Button color="secondary" variant="ghost" size="sm" onClick={() => openEdit(project)}>编辑</Button> : null}{project.archived_at ? <Button color="secondary" variant="outline" size="sm" disabled={restoreMutation.isPending} onClick={() => restoreMutation.mutate(project)}>恢复</Button> : <Button color="secondary" variant="ghost" size="sm" onClick={() => { setArchiveError(''); setPendingArchive(project) }}>归档</Button>}</div> },
  ]

  if (role.loading) return <StatePanel variant="loading" title="正在确认运行角色…" />
  if (role.error) return <StatePanel variant="error" title="无法确认运行角色" description={role.error} />
  if (role.role !== 'hub') return <StatePanel variant="info" title="项目管理仅在共享后台提供" description="Worker 只能选择本机已同步的项目，不能创建、编辑或归档项目。" />

  return (
    <>
      <ListPageLayout
        title="项目管理"
        headingId="projects-heading"
        description="项目组织素材批次、采集记录、审核结果和交付视图。项目标识创建后不可修改。"
        actions={<Button color="primary" size="sm" onClick={openCreate}>创建项目</Button>}
        filters={
          <FilterToolbar label="项目筛选" activeCount={search ? 1 : 0} onClear={() => setSearch('')}>
            <div className="w-64 max-w-full"><SearchInput value={search} onChange={setSearch} label="项目" placeholder="搜索名称、标识或描述" /></div>
            <div className="flex gap-1" role="group" aria-label="项目状态">
              <Button color={filter === 'active' ? 'primary' : 'secondary'} variant={filter === 'active' ? 'solid' : 'ghost'} size="sm" onClick={() => setFilter('active')}>进行中</Button>
              <Button color={filter === 'archived' ? 'primary' : 'secondary'} variant={filter === 'archived' ? 'solid' : 'ghost'} size="sm" onClick={() => setFilter('archived')}>已归档</Button>
            </div>
          </FilterToolbar>
        }
        error={query.isError ? <Alert color="danger" variant="soft" title={errorMessage(query.error)} /> : undefined}
        listTitle={filter === 'active' ? '进行中的项目' : '已归档项目'}
      >
        <AppDataTable
          columns={columns}
          rows={rows}
          rowKey={(project) => project.id}
          caption="项目列表"
          loading={query.isPending}
          minWidthClass="min-w-3xl"
          emptyState={filter === 'active'
            ? <StatePanel variant="empty" title="还没有进行中的项目" description="创建项目后即可导入素材并开展业务。" action={<Button color="primary" size="sm" onClick={openCreate}>创建项目</Button>} />
            : <StatePanel variant="empty" title="没有已归档项目" />}
        />
      </ListPageLayout>

      <AppDialog
        open={dialogOpen}
        onOpenChange={(open) => !open && !saveMutation.isPending && closeForm()}
        title={editing ? '编辑项目' : '创建项目'}
        description={editing ? '项目标识是目录标识，创建后保持不变。' : '项目标识用于隔离输出目录，创建后不可修改。'}
        busy={saveMutation.isPending}
        widthClass="w-[min(560px,94vw)]"
        footer={<div className="flex justify-end gap-2"><Button color="secondary" variant="outline" size="sm" onClick={closeForm} disabled={saveMutation.isPending}>取消</Button><Button color="primary" size="sm" disabled={saveMutation.isPending} onClick={() => { const message = validate(); if (message) { setFormError(message); return } setFormError(''); saveMutation.mutate() }}>{saveMutation.isPending ? '正在保存…' : '保存项目'}</Button></div>}
      >
        <div className="space-y-4">
          {formError ? <Alert color="danger" variant="soft" title={formError} /> : null}
          <label className="block space-y-1 text-sm text-default"><span>项目名称</span><Input autoFocus maxLength={80} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：2026 年 Q4 菜单采集" /></label>
          <label className="block space-y-1 text-sm text-default"><span>项目标识</span><Input maxLength={48} value={draft.code} readOnly={Boolean(editing)} onChange={(event) => setDraft((current) => ({ ...current, code: event.target.value }))} placeholder="例如：2026-Q4" /><span className="block text-2xs text-tertiary">仅用于目录隔离；创建后不可修改。</span></label>
          <label className="block space-y-1 text-sm text-default"><span>描述（可选）</span><Input maxLength={500} value={draft.description} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} placeholder="说明项目范围或交付批次" /></label>
        </div>
      </AppDialog>

      <ConfirmActionDialog
        open={pendingArchive !== null}
        title="归档项目？"
        description={<div className="space-y-1"><p>将归档「{pendingArchive?.name ?? ''}」（{pendingArchive?.code ?? ''}）。归档项目保留历史、证据和导出读取能力，并禁止新增业务写入。</p><p>若仍有上传、分析、发布、活动分配、运行、标注或待补传，服务端会阻止归档并返回具体原因。</p></div>}
        confirmLabel="归档项目"
        tone="danger"
        busy={archiveMutation.isPending}
        error={archiveError}
        onOpenChange={(open) => { if (!open && !archiveMutation.isPending) { setPendingArchive(null); setArchiveError('') } }}
        onConfirm={() => { if (pendingArchive) archiveMutation.mutate(pendingArchive) }}
      />
    </>
  )
}
