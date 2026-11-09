# GUI Agent 试验

试验使用字节 GUI Agent SDK 2.0（`@gui-agent/agent-sdk@0.3.1`）和百炼 `gui-plus-2026-02-26`。SDK 管理导航循环和上下文。适配器解析 GUI-Plus 的 `computer_use`，UiPath 负责观察已打开的微信小程序窗口、点击和滚动。导航结束后，Python 按结果交给现有采集器。

## 本地联调

在 `macos_runner` 目录执行：

```sh
npm ci --prefix ui_tars
.venv/bin/python -m pytest -q tests/test_ui_tars_trial.py
```

联调用真实 SDK、本机模拟百炼 SSE 响应和模拟 UiPath。需要 Node.js 22.17.1 或更新版本。

## 实机前先检查模型坐标

在网页设置中保存北京地域接口、`gui-plus-2026-02-26` 模型名和对应密钥。也可以通过环境变量提供 `GUI_PLUS_API_KEY` 或 `DASHSCOPE_API_KEY`。环境变量的默认地址是 `https://dashscope.aliyuncs.com/compatible-mode/v1`，使用业务空间专属域名时请同时设置 `GUI_PLUS_BASE_URL`。

通过环境变量运行只读探测时，测试会将合成图交给 SDK，检查换算后的坐标是否落在标记内。整个探测在模拟执行器中完成。

```sh
GUI_PLUS_LIVE_PROBE=1 .venv/bin/python -m pytest -q tests/test_ui_tars_trial.py -k live_gui_plus_pixel_contract
```

GUI-Plus 返回相对于整张送模图的 1000×1000 归一化坐标。适配器先换算到送模图像素，再按当前窗口尺寸换算成 UiPath 的 points。大图送模前会等比缩小，原截图保留供取证。合成图探测通过后再启用真实桌面动作。 [百炼 GUI-Plus 指南](https://help.aliyun.com/zh/model-studio/gui-automation)

## 全自动批量试验

将评测清单的任务按 `suggested_live_order` 排序，以 `{"task_ids": ["..."]}` 传给 `POST /api/ui-tars/trial`。沿用已校准的 UiPath CLI 和微信文件传输助手。程序根据每条任务的发布快照发送并识别二维码，在小程序窗口打开后交给 GUI Agent。每家结束后确认回到文件传输助手，再执行下一家。

`result_file` 指向批量汇总 `batch-result.json`。其中 `results` 关联每家的导航、采集、截图证据和安全收尾，`unstarted_task_ids` 记录尚未启动的任务。安全起点无法确认时批量中止在当前边界。产物保存在试验目录中，正式队列和发布目录保持独立。

## 手动单店接管

1. 打开本机控制台，确认 GUI-Plus 配置就绪、UiPath 使用 CLI 启动方式。评测时从用例清单取出 `task_id`，无需改动正式队列或切换已激活的素材版本。
2. 手动打开该门店的小程序，只保留这一扇小程序窗口。
3. 在控制台网页的开发者工具中预约请求，随后在 8 秒内切到小程序。运行期间让小程序保持在前台。

```js
const taskId = "填入用例清单里的 task_id";
const html = await (await fetch("/")).text();
const token = html.match(/^\s*let TOKEN = '([A-Za-z0-9_-]+)';/m)?.[1];
setTimeout(async () => {
  const response = await fetch("/api/ui-tars/trial", {
    method: "POST",
    headers: { "X-Capture-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  console.log(await response.json());
}, 8000);
```

`result_file` 指向试验报告。报告记录素材版本、首帧和分段耗时。采集或人工标注正在运行时需等待其结束。需要停止时，以同样方式请求 `POST /api/terminate`。报告中的 `navigation.outcome` 记录菜单导航结论，`source: runtime` 标记技术中断或协议错误导致的需复核。只有 `success` 进入试验采集，`capture.status` 另行记录截图结果。

在输出根目录的隐藏 `.uipath-runs/<本轮>/ui-tars-trial/` 中核对终态截图、页面和报告。实机验收应独立核对门店身份、菜单内容和滚动结果。
