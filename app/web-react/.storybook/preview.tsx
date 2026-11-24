import type { Preview } from '@storybook/react-vite'
import { AppsSDKUIProvider } from '@openai/apps-sdk-ui/components/AppsSDKUIProvider'
import '../src/styles/index.css'

/**
 * Storybook 复用产品同一套 OpenAI 样式与组件源码，不另写 mock 样式。
 * 示例数据全部为合成内容，不接入生产接口。
 */
const preview: Preview = {
  parameters: {
    layout: 'padded',
    controls: { matchers: { color: /(background|color)$/i, date: /Date$/i } },
    backgrounds: {
      options: {
        surface: { name: 'surface', value: 'var(--color-surface)' },
        secondary: { name: 'surface-secondary', value: 'var(--color-surface-secondary)' },
      },
    },
  },
  decorators: [
    (Story) => (
      <AppsSDKUIProvider linkComponent="a">
        <div className="min-h-40 bg-surface p-4 text-default">
          <Story />
        </div>
      </AppsSDKUIProvider>
    ),
  ],
}

export default preview
