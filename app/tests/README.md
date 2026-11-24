# 后端测试分类

从 `UFS_Menu_Scraper/macos_runner/` 执行 `uv run pytest`。测试用 pytest marker 表达验证类型，支持按类型选取：

```sh
uv run pytest -m unit
uv run pytest -m integration
uv run pytest -m contract
uv run pytest -m platform
uv run pytest -m real_data
```

- `unit` 使用合成输入验证纯规则，不访问本机运行数据或外部服务。
- `integration` 在临时目录中组合应用、文件系统或 SQLite。
- `contract` 验证 HTTP、设备协议或兼容行为。
- `platform` 只适用于标注的 Windows/macOS 桌面环境；测试需在用例上说明平台原因。
- `real_data` 是可选的本机采集证据核验。运行 `test_chat_image_locator.py` 的真实截图用例前，设置 `MENU_CAPTURE_TEST_TASK_DB` 和 `MENU_CAPTURE_TEST_EVIDENCE_DIR` 指向已准备的本机数据；缺少这些输入时会逐例报告跳过原因。默认逻辑测试仅用临时合成图，不读取 `data/`。

新增测试应标出适用 marker。平台或真实数据测试不能替代可在干净工作区运行的合成测试。
