interface StatCardProps {
  label: string
  value: number | string
  tone?: 'warning' | 'danger' | 'success'
  active?: boolean
  onClick?: () => void
}

export function StatCard({ label, value, tone, active, onClick }: StatCardProps) {
  const surface = 'rounded-xl border border-subtle bg-surface px-4 py-3'
  const metric = (
    <>
      <span className="block text-xs text-secondary">{label}</span>
      <span className={`mt-1 block heading-lg tabular-nums ${tone === 'danger' && value ? 'text-danger'
        : tone === 'warning' && value ? 'text-warning'
        : tone === 'success' && value ? 'text-success' : 'text-default'}`}>
        {value}
      </span>
    </>
  )
  if (!onClick) return <div className={surface}>{metric}</div>
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={Boolean(active)}
      className={`${surface} text-left ${active ? 'bg-secondary-soft' : 'hover:bg-secondary-ghost-hover'}`}
    >
      {metric}
    </button>
  )
}
