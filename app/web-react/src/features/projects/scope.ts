/** Explicit project scope for request URLs and TanStack Query keys. */
export function projectApiPath(path: string, projectId: string): string {
  if (!projectId || !path.startsWith('/api/') || path.includes('#')) {
    throw new Error('项目请求缺少有效作用域')
  }
  const separator = path.indexOf('?')
  const pathname = separator < 0 ? path : path.slice(0, separator)
  const params = new URLSearchParams(separator < 0 ? '' : path.slice(separator + 1))
  if (params.getAll('project_id').some((id) => id !== projectId)) {
    throw new Error('项目请求作用域冲突')
  }
  params.set('project_id', projectId)
  return `${pathname}?${params.toString()}`
}

/** Use the project captured when the request/mutation was created, never a mutable global. */
export function projectQueryKey<const T extends readonly unknown[]>(projectId: string, ...key: T) {
  if (!projectId) throw new Error('项目查询缺少有效作用域')
  return ['project', projectId, ...key] as const
}
