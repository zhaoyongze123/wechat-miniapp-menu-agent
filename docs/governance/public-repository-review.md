# 公开仓库资产核查

GitHub 报告仓库为公开。以下记录只列文件位置、风险类别和处置状态。

| 范围 | 发现 | 处置状态 |
| --- | --- | --- |
| 旧版产物退出跟踪前的 `输出/`、`UFS_Menu_Scraper/macos_runner/output/` | 历史版本中有真实截图和业务输出 | 当前源码树已排除。历史仍待维护者处理。 |
| 提交 `b3d7486` 至 `b7079fa` 的 `docs/react-migration/verify/real-capture/` 和 `docs/react-migration/screenshots/release/prod-rc3-workbench.jpg` | 原始生产证据包含门店来源标识，另有实机工作台截图 | 当前文档只保留汇总结论。历史仍待维护者处理。 |
| React 迁移阶段截图、Excel 和诊断 JSON | 部分资产来源及脱敏状态尚无法核实 | 保留可追溯位置，待逐项复查。暂不作为已通过资产合规审查的证据。 |

当前分支清理只处理工作树。Git 历史、远端分支、正式 Release 和生产资料未经授权不得删除或修改。发现凭据时按安全策略通知维护者和轮换。 [安全策略](../../SECURITY.md)
