/**
 * 插件内容页 → 壳的 chrome 协议（`omnibox:*` 消息）的单一真源。
 *
 * 为什么单独成模块：白名单与"补丁如何并进当前状态"原先写在 App.vue 的消息处理器里，
 * 而 SFC 无法被 Node 直接驱动，测试只能对源码做正则（`data.type !== 'omnibox:x'`）：
 * 删掉一个分支、或把一个没有发送方的类型登记进来，都测不出来。抽成函数之后
 * tests/js/plugin_chrome_protocol.mjs 直接 import 真实实现断言行为。
 *
 * 白名单只收"有发送方"的协议串：每个登记进来的类型都必须同时出现在
 * docs/plugin-page-architecture.md 的消息表里（用例逐个比对文档），避免把未实现的
 * 协议当成"已支持"。消息的接入点（事件监听、来源校验）仍在 App.vue —— 那部分
 * 是接线，不是判定。
 *
 * 不 import 任何模块，Node 直接跑。
 */

/**
 * 壳接受的插件 chrome 消息类型（顺序与文档消息表一致）。
 *
 * 每一项在 App.vue 里都必须有处理分支；反过来，没有发送方的类型不许出现在这里。
 */
export const PLUGIN_CHROME_MESSAGE_TYPES = [
  'omnibox:ready',
  'omnibox:chrome',
  'omnibox:open-settings',
  'omnibox:get-plugin-appearance',
  'omnibox:set-plugin-alpha',
]

/**
 * 一条 postMessage 数据是不是壳认识的 chrome 消息。
 *
 * 未登记的类型（例如早已没有发送方的历史协议串）一律返回 false，处理器因此不会再
 * 为"看起来支持"的类型留下分支。
 *
 * @param {unknown} data 消息体
 * @returns {boolean}
 */
export function isPluginChromeMessage(data) {
  if (!data || typeof data !== 'object') return false
  return PLUGIN_CHROME_MESSAGE_TYPES.includes(data.type)
}

/**
 * 把一条 `omnibox:chrome` 补丁并进当前状态，返回新状态（不修改入参）。
 *
 * 规则来自内容页的用法：
 * - `title` / `subtitle` / `view` 只接受字符串，其它类型忽略（保留旧值）；
 * - `badges` 整份替换并丢掉空值 —— 内容页靠"发一个空值"来清除某个角标；
 * - `actions` / `nav` / `search` 按键合并，内容页只发变化的那几项；
 * - `status` 缺失字段回退成空文本与 `idle`；
 * - `statusItems` 只接受数组（不是数组就不是"清空数组"，而是发错了，保持旧值）。
 *
 * @param {Record<string, any>} state 当前 chrome 状态
 * @param {Record<string, any>} data `omnibox:chrome` 消息体
 * @returns {Record<string, any>} 合并后的新状态
 */
export function mergeChromeState(state, data) {
  const next = { ...state }
  if (typeof data.title === 'string') next.title = data.title
  if (typeof data.subtitle === 'string') next.subtitle = data.subtitle
  if (typeof data.view === 'string') next.view = data.view
  if (data.badges && typeof data.badges === 'object') {
    const badges = {}
    Object.entries(data.badges).forEach(([key, value]) => {
      if (value !== null && value !== undefined && String(value) !== '') badges[key] = String(value)
    })
    next.badges = badges
  }
  if (data.status && typeof data.status === 'object') {
    next.status = {
      text: String(data.status.text || ''),
      state: String(data.status.state || 'idle'),
    }
  }
  if (data.actions && typeof data.actions === 'object') {
    next.actions = { ...(next.actions || {}), ...data.actions }
  }
  if (data.nav && typeof data.nav === 'object') {
    next.nav = { ...(next.nav || {}), ...data.nav }
  }
  if (data.search && typeof data.search === 'object') {
    next.search = { ...(next.search || {}), ...data.search }
  }
  if (Array.isArray(data.statusItems)) {
    next.statusItems = data.statusItems
  }
  return next
}
