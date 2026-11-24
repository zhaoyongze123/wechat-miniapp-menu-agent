# 前端设计体系入口

本目录是 UI 与交互的唯一规范入口，对应 Issue #31。目标不是重做视觉，而是把成熟设计模式固化为可复用组件与默认行为，让新增页面天然继承一致交互。

## 四层模型

| 层级 | 标准 | 本项目落地 |
| --- | --- | --- |
| L1 视觉 | OpenAI Apps SDK UI | 语义 Token、字体、颜色、间距、圆角、主题；不定义自有色板 |
| L2 基础控件 | OpenAI Apps SDK UI | Button、Input、Select、Switch、Checkbox、Slider 等官方组件 |
| L3 复合组件 | Ant Design 能力参考 | `src/components/*` 的表格、分页、筛选、弹窗、抽屉、设置布局等自有实现 |
| L4 产品模式 | AWS Cloudscape 为主，IBM Carbon 补充 | 资源列表/详情、设置、表单、反馈、状态、导航与恢复模式 |

四层是项目组织模型，不是上游官方分类。**只参考行为与模式，不混装视觉库、不复制上游 CSS、不迁移 React/Tailwind/Radix。**

## 冲突优先级

1. 现有业务事实、安全权限与数据契约
2. 可访问性与正确交互
3. 项目已确认的设计决定
4. Cloudscape 通用交互模式（Carbon 专项补充）
5. Ant Design 复合组件能力参考

视觉表达始终采用 OpenAI。不得为了外部规范改变业务含义或权限；无对应模式时做最小、可解释的项目适配，不机械新增按钮与文案。

## 文档索引

- [upstream-index.md](upstream-index.md)：上游来源、版本、核对日期、采用范围与例外
- [page-patterns.md](page-patterns.md)：页面类型到设计模式的映射
- [component-contracts.md](component-contracts.md)：公共组件契约与默认行为
- [acceptance-matrix.md](acceptance-matrix.md)：全站交互验收矩阵与例外记录

## 开发规则

- 新页面先查本目录与 [component-contracts.md](component-contracts.md)，优先复用现有组件；确需新增时先补契约与 Storybook 故事。
- Storybook 仅用于开发与回归，不进入生产安装包或 Hub 镜像。启动：`npm run storybook`；静态构建：`npm run build-storybook`。
- 关键交互至少有一个自动化测试；视觉基线只在有明确理由时更新。
- 上游更新需评估后显式采纳，不自动改变生产 UI。
