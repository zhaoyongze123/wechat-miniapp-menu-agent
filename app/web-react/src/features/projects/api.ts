import { apiFetch } from '../../api/client'

export interface Project {
  id: string
  code: string
  name: string
  description: string
  created_at: string
  updated_at: string
  archived_at: string
}

export interface CreateProjectPayload {
  code: string
  name: string
  description?: string
}

export type UpdateProjectPayload = Partial<Pick<Project, 'name' | 'description'>>

const BASE = '/api/projects'
export const projectsKeys = {
  all: ['projects'] as const,
  list: (includeArchived: boolean) => ['projects', 'list', includeArchived] as const,
  detail: (id: string) => ['projects', 'detail', id] as const,
}

function projectPath(id: string): string {
  return `${BASE}/${encodeURIComponent(id)}`
}

export function fetchProjects(includeArchived = false, signal?: AbortSignal): Promise<Project[]> {
  return apiFetch<Project[]>(`${BASE}?include_archived=${includeArchived}`, { signal })
}

export function fetchProject(id: string, signal?: AbortSignal): Promise<Project> {
  return apiFetch<Project>(projectPath(id), { signal })
}

export function createProject(payload: CreateProjectPayload): Promise<Project> {
  return apiFetch<Project>(BASE, { method: 'POST', json: payload })
}

export function updateProject(id: string, payload: UpdateProjectPayload): Promise<Project> {
  return apiFetch<Project>(projectPath(id), { method: 'PATCH', json: payload })
}

export function archiveProject(id: string): Promise<Project> {
  return apiFetch<Project>(`${projectPath(id)}/archive`, { method: 'POST', json: {} })
}

export function restoreProject(id: string): Promise<Project> {
  return apiFetch<Project>(`${projectPath(id)}/restore`, { method: 'POST', json: {} })
}
