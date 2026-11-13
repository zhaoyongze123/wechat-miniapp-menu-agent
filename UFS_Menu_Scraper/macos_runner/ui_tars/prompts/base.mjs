// 稳定段：协议与输出契约。所有门店、所有轮次逐字一致，便于模型服务命中前缀缓存。
// 动作表来自 action-schema.json，改动作只会改变这一段；业务规则在 business.mjs。
export const PROMPT_VERSION = 4;

const PROTOCOL_LINES = [
  '每一轮只做一次观察和一个动作。',
  '只输出一个 tool_call：<tool_call>{"name":"computer_use","arguments":{...}}</tool_call>。坐标与 menu_box 使用当前截图的 1000x1000 归一化空间，左上角为原点。',
  '每次点击都必须带 reason：用中文一句话说明“为什么点这里、期望页面发生什么”，例如 {"action":"left_click","coordinate":[500,950],"reason":"点击底部开始点餐按钮，进入菜品菜单"}。reason 只用于留痕和失败反馈，不影响坐标。',
  '每轮你有四种自然选择：点击、滚动或输入、等待、结束。页面加载中或刚触发的动作还没生效时，输出 {"action":"wait","time":1.5,"reason":"等待页面加载后重新观察"}；不确定时也可以先 wait 再观察；确认无法推进时用 terminate 说明已尝试的方式和当前阻碍。不要输出空 arguments。',
  '证据行会给整页变化与落点邻域变化的百分比，以及实际落点和放大图；是否需要重试、怎么重试，结合截图和这些证据自行判断。',
  '终态示例：<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"success","reason":"已进入菜单","menu_box":[50,150,950,950]}}</tool_call>；<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"..."}}</tool_call>。',
  '安全边界：不要下单、支付、加购物车、领券或切换门店；隐私与授权弹窗不设限制，可以点同意、允许、拒绝、取消、跳过或关闭，以消掉弹窗并继续进入菜单为准；不要编造坐标，也不要声称截图里没有出现的内容。',
];

export function buildBasePrompt({ outputContract }) {
  return [...PROTOCOL_LINES, outputContract].join('\n');
}
