# 上游规范索引

核对日期：2026-11-24。只记录已核对的来源、版本与采用范围；上游文档更新不自动改变生产 UI。

## 代码依赖（本仓库锁定版本）

| 依赖 | 版本 | 用途 | 来源 |
| --- | --- | --- | --- |
| `@openai/apps-sdk-ui` | `0.2.2` | L1 视觉 + L2 基础控件 | npm / `package-lock.json` |
| `@radix-ui/react-dialog` | `^1.1.15` | 弹窗/抽屉可访问基础 | npm |
| `react` / `react-dom` | `^19.1.1` | 运行时 | npm |
| `vite` | `^7.1.3` | 构建 | npm |
| `tailwindcss` | `^4.1.12` | 布局与工具类 | npm |
| `@tanstack/react-query` | `^5.87.1` | 服务端状态 | npm |
| `storybook` / `@storybook/react-vite` | `10.6.8` | 开发期组件展示与交互验证 | npm，devDependency |

## 设计模式来源

| 来源 | 角色 | 采用范围 | 不采用 |
| --- | --- | --- | --- |
| [OpenAI Apps SDK UI](https://github.com/openai/apps-sdk-ui) · [文档](https://openai.github.io/apps-sdk-ui/) | 唯一视觉与基础组件标准 | 语义 Token、字阶、颜色、间距、圆角、官方控件 | 自定义色板、第三方主题、替换官方组件 |
| [AWS Cloudscape patterns](https://cloudscape.design/patterns/) · [demos](https://github.com/cloudscape-design/demos) | L4 主参考 | 资源列表/详情、筛选与选择、页面布局、操作位置、加载/空/错状态、设置与运维 | Cloudscape 组件、CSS 与主题 |
| [Ant Design Table](https://ant.design/components/table-cn/) · [设计规范](https://ant.design/docs/spec/introduce-cn/) | L3 能力参考 | 列宽、固定列、省略与完整查看、分页、筛选、表单、弹窗、抽屉边界行为 | Ant Design 组件、Token 或样式 |
| [IBM Carbon patterns](https://www.carbondesignsystem.com/building-blocks/core/patterns/overview) · [Carbon](https://github.com/carbon-design-system/carbon) · [IBM Products](https://github.com/carbon-design-system/ibm-products) | L4 专项补充 | 复杂表单、危险操作、弹窗选择、反馈、可访问性、多步任务 | Carbon 组件与样式；已归档的 carbon-website 不作实时规范 |
| [PatternFly](https://www.patternfly.org/) | 仅设备运维补充 | 当前三者未覆盖的设备/运维形态 | 作为平级规则或引入组件 |

外部资料不可达时，以本目录已核验的精简规则与固定出处继续开发，不阻塞交付。

## 已采纳的模式（摘要）

- 资源列表：标题与范围说明、主操作唯一、筛选可清除、跨页选择可见、长文本省略且可查看、固定关键列。
- 资源详情：抽屉承载，头部固定、内容独立滚动、关闭回焦；只读详情不出现误导性写操作。
- 设置：分类导航 + 左标题右控件；本地偏好即时保存，服务端配置显式保存；草稿不被后台刷新覆盖。
- 反馈：成功短暂、失败可停留且可关闭；错误就近展示并保留输入；不以 Toast 代替业务成功。
- 危险操作：说明对象、范围、后果与可恢复性；处理中禁止误关。

## 必要差异与例外

- 不引入 Ant Design / Carbon / Cloudscape 运行时依赖或额外主题；相关能力均在 `src/components` 内用 OpenAI 控件与 Token 实现。
- Storybook 使用 devDependency，不进入 `vite build` 产物与安装包；组件故事使用合成数据。
- 视觉回归以稳定 DOM/布局断言和小范围截图基线为主，不做全站逐像素比较。
- 远程控制页为受限路由，只消费既有授权接口；设计模式适配不得扩大接口权限。
