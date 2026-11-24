# 公共组件契约

新增或修改组件均位于 `app/web-react/src/components/`。所有组件只消费 OpenAI Apps SDK UI 控件与语义 Token，不引入第三方视觉库。

## 数据展示

### AppDataTable
- 列定义 `DataColumn<T>`：`id`、`header`、`cell`、`width`、`sticky`、`className`、`headerClassName`。
- 配置任意 `width` 时使用固定布局与 `colgroup`；`sticky` 仅支持左右各一列，背景不透明。
- `onRowActivate` 提供时行可点击且可聚焦：Enter/Space 进入详情；行内链接、按钮、复选框、下拉等交互元素不触发行点击。
- `loading` 覆盖层只在首次加载时使用；后台刷新不得遮盖整表（由调用方控制）。
- 分页、筛选、请求状态归调用方所有，组件不保存业务状态。

### CellText
- 用于表格与详情中的单行文本：`truncate` + `title` 查看全文。
- `copyable` 显示复制按钮，`copyLabel` 提供读屏对象名；空值显示 `empty`（默认 `—`）。
- 需要换行的说明不要使用本组件。

### CopyButton
- 优先 `navigator.clipboard`，HTTP 内网环境自动回退 `textarea + execCommand`。
- 成功/失败在按钮内反馈并通过 `aria-live` 播报；不额外弹 Toast。

### KeyValueList
- `items: { label, value, copy?, mono? }`，统一标签宽度、长值省略与复制入口。

### AppPagination
- 服务端分页；页码窗口最多 7 个。
- 页数超过 7 时默认显示首页/末页与快速跳转；跳转值自动限制到有效范围。
- 改筛选由调用方回到第一页；组件不自行重置页码。

### FilterToolbar / SearchInput / BulkActionBar
- `FilterToolbar`：统一控件布局；`activeCount` + `onClear` 显示“清除筛选”，`summary` 说明当前范围。清除筛选不得清空批量选择。
- `SearchInput`：受控搜索框，非空时提供清除按钮。
- `BulkActionBar`：`count > 0` 才渲染；展示数量、范围说明、业务操作与“清空选择”。

## 设置与表单

### SettingsLayout / SettingSection / SettingRow / FormField / SettingsSaveBar
- `SettingsLayout`：桌面左侧分类导航，窄屏顶部横向导航；只负责布局与选中态。
- `SettingSection`：`id`、`title`、`description`，用于分类内容与锚点。
- `SettingRow`：左标题/说明、右控件；`htmlFor` 建立标签关联，`wide` 用于路径等宽控件。
- `FormField`：标签、帮助文本、行内错误；错误优先展示并保留输入。
- `SettingsSaveBar`：吸底；区分未修改/已修改/保存中/失败；主按钮触发 form submit，`onReset` 放弃草稿。

### 草稿保护
- `useUnsavedChanges(active, message)` 在有真实未保存变更时登记守卫。
- 刷新/关闭标签页使用 `beforeunload`；站内链接在捕获阶段拦截，确认后才跳转。
- 设置页后台 refetch 不覆盖草稿；保存成功后才更新基线。
- 已知限制：浏览器“后退/前进”不在拦截范围，离开页面时仍有 `beforeunload` 兜底。

## 弹层与反馈

### AppDialog / AppDrawer
- 头部与底部固定，内容独立滚动；关闭后焦点回焦触发元素。
- `busy` 为真时禁用 Esc、遮罩点击与关闭按钮，避免用户误以为已提交操作被取消。
- 长标题单行省略并通过 `title` 查看完整内容。

### ConfirmActionDialog
- 必须说明对象、范围、结果与可恢复性；`busy` 会同时锁定关闭路径。
- 不为普通操作增加无意义确认。

### ImagePreview
- `items` 传完整图片序列时支持左右切换，`initialIndex` 定位当前图。
- 缩放、平移、旋转仅作用于预览；加载、失败、重试与窄屏工具栏可用。
- 调用方有多个证据时必须传 `items`，不要只传单图。

### ToastProvider
- 成功 3 秒、普通 4.5 秒、失败 8 秒；全部可手动关闭。
- 相同文案同色提示去重并重新计时；最多同时 4 条。
- Toast 不构成业务成功依据，失败需在对应区域保留可操作信息。

## 页面骨架

### AppShell
- 侧栏可折叠为图标导航，偏好保存在当前浏览器；折叠后保留可访问名称与悬停提示。
- 角色读取中或失败时不显示“本机独立模式”，避免把未知状态误报为可用模式。
- 窄屏不使用双侧栏；导航与当前项始终可达。
