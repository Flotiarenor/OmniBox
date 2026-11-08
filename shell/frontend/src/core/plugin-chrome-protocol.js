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
 * 壳 → 内容页的刷新事件：把后端同步 / 校验报告原样交给内容页。
 *
 * 与 `omnibox:action: refresh` 的分工：action 只说"该刷新了"，本事件还带上
 * `scope` 与 `report`（`action` / `added` / `changed` / `removed` / `invalidated`），
 * 内容页据此决定重载哪一层。只认旧 action 的内容页仍在，所以两条都要发。
 */
export const FRESHNESS_EVENT = 'omnibox:freshness';

/**
 * 构造一条 `omnibox:freshness` 消息。
 *
 * `report` 原样透传：它来自后端 JSON，可直接被结构化克隆；非对象按 `null` 处理，
 * 免得内容页拿到字符串后去读 `report.added`。`plugin` 与 `scope` 只做类型兜底。
 *
 * @param {string} plugin 插件名
 * @param {string} scope 本次同步使用的作用域
 * @param {unknown} report 后端报告
 * @returns {{type: string, plugin: string, scope: string, report: Object|null}}
 */
export function buildFreshnessEvent(plugin, scope, report) {
  return {
    type: FRESHNESS_EVENT,
    plugin: String(plugin || ''),
    scope: typeof scope === 'string' ? scope : '',
    report: report && typeof report === 'object' ? report : null,
  };
}

/**
 * 内容页上报的同步作用域（`freshness_scope`）的长度上限。
 *
 * 作用域是目录相对路径，正常远短于此；限制长度是为了让一个写坏的内容页
 * 无法把任意长字符串塞进壳的状态与后续的 `system_freshness_sync` 参数。
 */
export const FRESHNESS_SCOPE_MAX = 256;

/**
 * 归一化 `freshness_scope`；非法值返回 `undefined`（调用方保留旧值，不落盘）。
 *
 * 规则：
 * - 非字符串 → 丢弃（数字 / null / 对象都不当作用域）；
 * - 去掉首尾空白；空串是合法值，表示「全部根」；
 * - 长度超过 {@link FRESHNESS_SCOPE_MAX} → 丢弃；
 * - 含 `..` → 丢弃：作用域会被拼进后端的目录解析，路径穿越不该从消息通道进来
 *   （后端仍会自己判一次，这里是通道侧的过滤）。
 *
 * @param {unknown} value
 * @returns {string|undefined}
 */
export function normalizeFreshnessScope(value) {
  if (typeof value !== 'string') return undefined;
  const scope = value.trim();
  if (!scope) return '';
  if (scope.length > FRESHNESS_SCOPE_MAX) return undefined;
  if (scope.includes('..')) return undefined;
  return scope;
}

/**
 * 把一条 `omnibox:chrome` 补丁并进当前状态，返回新状态（不修改入参）。
 *
 * 规则来自内容页的用法：
 * - `title` / `subtitle` / `view` 只接受字符串，其它类型忽略（保留旧值）；
 * - `freshness_scope` 先按 {@link normalizeFreshnessScope} 归一化，非法值忽略；
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
  const scope = normalizeFreshnessScope(data.freshness_scope)
  if (scope !== undefined) next.freshness_scope = scope
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
