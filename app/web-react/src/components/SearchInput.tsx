import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Button } from '@openai/apps-sdk-ui/components/Button'

interface SearchInputProps {
  value: string
  onChange: (value: string) => void
  /** 读屏标签，同时作为占位提示的默认来源。 */
  label: string
  placeholder?: string
  className?: string
}

/** 统一搜索框：受控输入 + 可见清除入口，宽度可被筛选条压缩。 */
export function SearchInput({ value, onChange, label, placeholder, className = '' }: SearchInputProps) {
  return (
    <div className={`flex min-w-0 items-center gap-1 ${className}`}>
      <div className="min-w-0 flex-1">
        <Input
          type="search"
          aria-label={label}
          placeholder={placeholder ?? `搜索${label}`}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
      {value ? (
        <Button color="secondary" variant="ghost" size="sm" aria-label={`清除${label}`} onClick={() => onChange('')}>
          清除
        </Button>
      ) : null}
    </div>
  )
}
