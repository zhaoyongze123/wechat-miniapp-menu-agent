# GUI Agent 菜单自动采集系统

GUI Agent 浏览商家点餐界面，确认菜单可采集后，将窗口截图和菜单区域交给 UiPath 滚动截图。工作台管理素材清洗、批次、队列和结果。macOS 和 Windows 共用业务流程，Windows 桌面链路处于实机验收阶段。

**后续开发和优化请先阅读长期开发与优化规范。** 规范定义了 Agent 的决策权、只读操作边界、成功交接条件和实机回归口径。涉及导航、工具、结果分类或采集交接的改动，以这份规范为准。 [长期开发与优化规范](docs/gui-agent-development.md)

## 工作流程

1. 清洗二维码素材并发布采集批次，在工作台选择门店任务。
2. UiPath 打开微信中的二维码入口，或接管已经打开的小程序窗口。
3. GUI Agent 观察画面、调用通用工具并决定导航终态。当前实现使用字节 GUI Agent SDK 2.0 和百炼 `gui-plus-2026-02-26`。
4. Agent 返回 `success` 后，程序校验窗口和坐标，UiPath 接手滚动截图。正式结果由任务系统按清洗批次和门店发布。
5. 系统分别记录导航结论、运行故障和采集结果，保留本轮动作、截图证据和模型用量供复核。

| Agent 终态 | 含义 |
| --- | --- |
| `success` | 当前画面是可直接滚动采集的菜品菜单，Agent 提供菜单区域。 |
| `unqualified` | 画面有充分证据表明本轮无法采集。 |
| `needs_review` | 合理尝试后仍无法可靠完成或判断，需要人工检查。 |

## 本地运行

### macOS

准备 macOS、Python 3.12、uv、Node.js 22.17.1 或更新版本，以及已登录的微信和 UiPath CLI。按 UiPath 工程要求授予辅助功能与屏幕录制权限。

从仓库根目录执行以下命令安装依赖并启动本机控制台。

```sh
cd UFS_Menu_Scraper/macos_runner
uv sync --frozen --python 3.12
npm ci --prefix ui_tars
.venv/bin/python -m menu_capture.server
```

如果本机服务已启动，可直接打开控制台。在设置页配置百炼北京地域的 OpenAI 兼容接口、模型名和密钥，再通过环境诊断检查连接与桌面权限。使用自动扫码模式前，需完成 UiPath 窗口和坐标校准，并将微信停在文件传输助手。 [本机控制台](http://127.0.0.1:8765/) · [GUI Agent 联调说明](UFS_Menu_Scraper/macos_runner/ui_tars/README.md)

### Windows

在已登录、未锁屏的 Windows 桌面中准备微信、UiPath Studio/Assistant、UiPath CLI、uv 和 Node.js 22.17.1 或更新版本。微信 OCR 和自动扫码需要 Tesseract，并安装 `chi_sim`、`eng` 语言数据。将 `tesseract.exe` 加入 PATH，或将 `TESSERACT_CMD` 设为其绝对路径。可用 `tesseract --list-langs` 检查语言数据。 [Tesseract 命令行文档](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)

从仓库根目录在 PowerShell 启动。

```powershell
Set-Location UFS_Menu_Scraper/macos_runner
.\启动菜单采集.ps1
```

配置模型和本机输入、输出目录。Windows 的选择器、快捷键和校准数据保存在 `uipath.windows.profile.json`。先手动打开一个微信小程序，用工作台的单店接管方式验证截图、点击、菜单交接和滚动采集。随后在 Windows 上录制主窗口与图片预览的选择器，校准聊天输入区，再测试自动扫码和批量续跑。

Windows 验收先用单屏 100% 缩放完成单店，再测 125% 或 150% 缩放的截图和点击落点。最后用同一批素材分别在两端验证扫码、菜单输出和失败后的安全收尾。

## 代码与文档

| 入口 | 用途 |
| --- | --- |
| [控制台与任务服务](UFS_Menu_Scraper/macos_runner/) | 素材管理、任务状态、UiPath 通信、截图归档及工作台。 |
| [GUI Agent 联调说明](UFS_Menu_Scraper/macos_runner/ui_tars/README.md) | 模型配置、坐标协议、实机探测和隔离试验。 |
| [运行时导航 Skill](UFS_Menu_Scraper/macos_runner/ui_tars/skills/menu-navigation.md) | Agent 实际读取的通用菜单导航经验。 |
| [UiPath Main.xaml](UFS_Menu_Capture_CrossPlatform/Main.xaml) | 执行窗口发现、截图、点击和滚动等桌面操作。 |
| [长期开发与优化规范](docs/gui-agent-development.md) | 后续设计、代码审查和验收的依据。 |
| [首版 PRD](<文档/微信小程序菜单导航 Agent PRD.md>) | 早期试验的范围、背景与设计记录。 |

## 验证与优化

在 `UFS_Menu_Scraper/macos_runner` 目录运行 `.venv/bin/python -m pytest -q` 做本地回归。涉及导航、坐标或终态的改动，再用隔离试验复跑问题门店和已有成功门店。独立核对 Agent 最后看到的截图、执行动作和最终采集页，按实际可用菜单及正确判定不合格的结果计算有效处理率。 [实机试验入口](UFS_Menu_Scraper/macos_runner/ui_tars/README.md#全自动批量试验)
