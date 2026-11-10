# AGENTS.md — 本仓库协作规则

## 每轮必须写工作日志

每完成一轮用户需求（包括失败、被中断的轮次），立即追加到：

- `D:\handoff\agent-log\windows-codex\YYYY-MM-DD.md`（当天文件，只追加）
- `D:\handoff\agent-log\windows-codex\index.jsonl`（一行一轮 JSON）

格式见 `D:\handoff\agent-log\README.md`。要点：

1. 需求：用户原话，原样保留，不改写
2. 改动：文件相对路径 + 行号 + 一句话说明
3. 验证：跑过的命令与实际结果（如 `pytest -q → 66 passed, 1 skipped`）
4. 提交：SHA 与是否已推送
5. 遗留：没验证的、有风险的、没做完的，必须写
6. 失败也要写：停在哪一步、报错原文、下一步建议

不要修改 `mac-trae/` 目录下的任何文件，那是另一台机器写的。
不要批量补写历史；只回填用户明确要求的那几轮。

## 目录约定

- 本项目固定在 `D:\menu-agent`
- 离线交接包放 `C:\handoff`（说明文档、SDK 安装包）
- 输出目录、日志位置以 `menu_capture/config` 中的配置为准，不要把测试产物写进正式批次目录
