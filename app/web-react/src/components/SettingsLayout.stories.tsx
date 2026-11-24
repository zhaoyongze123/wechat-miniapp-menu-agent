import type { Meta, StoryObj } from '@storybook/react-vite'
import { expect, userEvent, within } from 'storybook/test'
import { Input } from '@openai/apps-sdk-ui/components/Input'
import { Checkbox } from '@openai/apps-sdk-ui/components/Checkbox'
import { useState } from 'react'
import {
  FormField,
  SettingRow,
  SettingSection,
  SettingsLayout,
  SettingsSaveBar,
  type SettingsNavItem,
} from './SettingsLayout'

const items: SettingsNavItem[] = [
  { id: 'general', label: '常规', description: '目录与基础偏好' },
  { id: 'notifications', label: '通知与提醒', description: '声音与桌面提醒' },
  { id: 'model', label: '模型服务', description: '接口、密钥与测试' },
  { id: 'storage', label: '存储与日志', description: '容量与清理' },
]

function SettingsDemo() {
  const [active, setActive] = useState('general')
  return (
    <form onSubmit={(event) => event.preventDefault()}>
      <SettingsLayout items={items} activeId={active} onSelect={setActive}>
        <SettingSection id="general" title="常规" description="示例数据，仅用于展示设置布局与对齐。">
          <SettingRow
            label="输出目录"
            htmlFor="demo-output"
            description="采集截图与结果文件写入此目录；长路径会省略并可复制。"
            wide
          >
            <Input id="demo-output" readOnly value="D:\\menu-agent\\data\\output\\2026-11-24-example" />
          </SettingRow>
          <SettingRow label="提示音" description="浏览器本地偏好，立即保存。">
            <Checkbox label="启用提示音" defaultChecked />
          </SettingRow>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="最大页数" htmlFor="demo-pages" help="2–500。">
              <Input id="demo-pages" type="number" defaultValue={30} />
            </FormField>
            <FormField label="请求超时（秒）" htmlFor="demo-timeout" error="请求超时需在 10–300 秒之间">
              <Input id="demo-timeout" type="number" defaultValue={5} />
            </FormField>
          </div>
        </SettingSection>
        <SettingsSaveBar dirty saving={false} note="保存后对后续任务生效。" onReset={() => undefined} />
      </SettingsLayout>
    </form>
  )
}

const meta: Meta = {
  title: '组件/设置布局',
  parameters: { layout: 'padded' },
}

export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  render: () => <SettingsDemo />,
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement)
    const model = canvas.getByRole('button', { name: /模型服务/ })
    await userEvent.click(model)
    await expect(model).toHaveAttribute('aria-current', 'true')
  },
}

export const SavingError: Story = {
  render: () => (
    <SettingsLayout items={items} activeId="model" onSelect={() => undefined}>
      <SettingSection id="model" title="模型服务" description="展示保存中的禁用态与失败提示。">
        <FormField label="模型名称" htmlFor="demo-model">
          <Input id="demo-model" defaultValue="example-model" />
        </FormField>
        <SettingsSaveBar
          dirty
          saving
          error="采集运行中，请先暂停再修改配置"
          note="保存后对后续任务生效。"
          onReset={() => undefined}
        />
      </SettingSection>
    </SettingsLayout>
  ),
}
