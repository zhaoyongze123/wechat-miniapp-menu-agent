import { describe, expect, it } from 'vitest'
import { projectApiPath, projectQueryKey } from './scope'

describe('explicit project scope', () => {
  it('preserves other query params and rejects contradictory scope', () => {
    expect(projectApiPath('/api/materials/batches?view=options', 'Q4'))
      .toBe('/api/materials/batches?view=options&project_id=Q4')
    expect(() => projectApiPath('/api/materials/batches?project_id=Q3', 'Q4')).toThrow()
    expect(() => projectApiPath('/api/materials/batches', '')).toThrow()
    expect(() => projectApiPath('https://example.org/api/data', 'Q4')).toThrow()
  })

  it('separates project keys without touching global keys', () => {
    expect(projectQueryKey('Q3', 'materials', 'batches')).not.toEqual(projectQueryKey('Q4', 'materials', 'batches'))
    expect(projectQueryKey('Q4', 'materials', 'batches')).toEqual(['project', 'Q4', 'materials', 'batches'])
  })
})
