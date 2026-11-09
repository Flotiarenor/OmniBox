/**
 * 插件内容页 → 壳的 chrome 协议（`cubby:*` 消息）的单一真源。
 *
 * 为什么单独成模块：白名单与"补丁如何并进当前状态"原先写在 App.vue 的消息处理器里，
 * 而 SFC 无法被 Node 直接驱动，测试只能对源码做正则（`data.type !== 'cubby:x'`）：
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
  'cubby:ready',
  'cubby:chrome',
  'cubby:open-settings',
  'cubby:get-plugin-appearance',
  'cubby:set-plugin-alpha',
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
 * 与 `cubby:action: refresh` 的分工：action 只说"该刷新了"，本事件还带上
 * `scope` 与 `report`（`action` / `added` / `changed` / `removed` / `invalidated`），
 * 内容页据此决定重载哪一层。只认旧 action 的内容页仍在，所以两条都要发。
 */
export const FRESHNESS_EVENT = 'cubby:freshness';

/**
 * 构造一条 `cubby:freshness` 消息。
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
 * 归一化 `freshness_scope` → 后端 `FsSource.scope_start` 认得的相对路径；非法返回 `null`。
 *
 * 规则必须与引擎的 `scope_start` 对齐，否则壳会拿一个后端解释成**别的目录**的值去同步
 * （`/a/b` 被后端当作根内 `a/b`；`a//b`、`a/./b` 解析到同一物理目录、但拼出的目录键
 * 与遍历时的键不同形，会被当成新目录）。因此这一层做规范化，而不是只做"像不像路径"：
 * - 非字符串 → `null`（数字 / null / 对象都不当作用域）；
 * - 反斜杠、盘符（`C:`）、UNC（`//host/share`）→ `null`：作用域是 `/` 分隔的相对路径，
 *   这几种写法只会被后端解释成另一个位置；
 * - 长度超过 {@link FRESHNESS_SCOPE_MAX} → `null`；
 * - 去首尾 `/`、折叠重复 `/`、丢掉 `.` 段；任何一段是 `..` → `null`（路径穿越不该
 *   从消息通道进来；后端仍会自己判一次，这里是通道侧的过滤）。
 *
 * 合法结果里不含首尾 `/`、重复 `/`、`.` 段与空串以外的空路径：`''` 表示「全部根」。
 *
 * @param {unknown} value
 * @returns {string|null}
 */
export function normalizeFreshnessScope(value) {
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (!raw) return '';
  if (raw.length > FRESHNESS_SCOPE_MAX) return null;
  if (raw.includes('\\') || /^[A-Za-z]:/.test(raw) || raw.startsWith('//')) return null;
  const segments = raw.split('/').filter((part) => part !== '' && part !== '.');
  if (segments.includes('..')) return null;
  return segments.join('/');
}

/**
 * 构造一条 `cubby:action` 消息。
 *
 * `payload` 只在实际给了值时出现：没有负载的动作（顶栏按钮、返回、设置保存完成）
 * 与旧协议完全一致，内容页的 `if (data.payload)` 一类判断不会被凭空造出的字段干扰。
 *
 * @param {string} action 动作 id
 * @param {unknown} [payload] 负载；`undefined` 表示没有
 * @returns {{type: string, action: string, payload?: unknown}}
 */
export function buildActionEvent(action, payload) {
  const message = { type: 'cubby:action', action: String(action || '') }
  if (payload !== undefined) message.payload = payload
  return message
}

/**
 * 一次被动同步该发给谁、用什么作用域（返回 `null` 表示这次不发）。
 *
 * 抽成函数是为了让"挂载后、视图变化、作用域变化各同步一次"这件事可以被 Node 用例
 * 直接驱动：SFC 里的 `syncFreshness` 只做"取决策 → 调 `autoSync`"，不再自己判断。
 *
 * @param {unknown} controller `Freshness.mount` 的返回值
 * @param {unknown} scope 内容页上报的 `freshness_scope`
 * @param {{blocked?: boolean}} [options] `blocked`：native 面板打开时内容 iframe 隐藏
 * @returns {{controller: {autoSync: (scope?: string) => void}, scope: string}|null}
 */
export function freshnessSyncTarget(controller, scope, options) {
  const opts = options || {}
  if (!controller || typeof controller.autoSync !== 'function') return null
  if (opts.blocked) return null
  return { controller, scope: typeof scope === 'string' ? scope : '' }
}

/**
 * 刷新事件该带哪个作用域：优先报告自己的 `scope`，没有才回落到当前值。
 *
 * 同步是异步的：用户可能在同步途中又换了一层目录。报告里的 `scope` 是**这次同步**
 * 实际用的作用域，内容页据此决定重载哪一层；用交付时的当前值会让它重载错层
 * （例如把新目录当成刚同步完的目录）。
 *
 * @param {unknown} report 后端报告
 * @param {unknown} fallback 当前作用域
 * @returns {string}
 */
export function freshnessEventScope(report, fallback) {
  const own = report && typeof report === 'object' ? report.scope : undefined
  if (typeof own === 'string') return own
  return typeof fallback === 'string' ? fallback : ''
}

/**
 * 「拿不到统一刷新概览」时该说哪句话。
 *
 * 概览端点属于壳的管理员专属接口（`_ADMIN_ONLY_API`）：浏览器模式下的 403 会回
 * `error: '禁止访问'`，桌面模式是 `AdminRequired` 的中文消息。把这两种都报成
 * "内核不提供"会让使用者以为是旧版本，实际是权限不够。
 *
 * @param {unknown} error 调用抛出的错误
 * @returns {string}
 */
export function freshnessOverviewErrorText(error) {
  const message = String((error && error.message) || error || '')
  return /403|禁止访问|管理员|权限/.test(message)
    ? '需要管理员权限才能查看统一刷新状态。'
    : '当前内核不提供统一刷新状态。'
}

/**
 * 把一条 `cubby:chrome` 补丁并进当前状态，返回新状态（不修改入参）。
 *
 * 规则来自内容页的用法：
 * - `title` / `subtitle` / `view` 只接受字符串，其它类型忽略（保留旧值）；
 * - `freshness_scope` 先按 {@link normalizeFreshnessScope} 归一化；非法值落成空串
 *   （=「全部根」）而**不保留旧值**：旧值只在错误的目录上同步，比全根同步更糟；
 * - `badges` 整份替换并丢掉空值 —— 内容页靠"发一个空值"来清除某个角标；
 * - `actions` / `nav` / `search` 按键合并，内容页只发变化的那几项；
 * - `status` 缺失字段回退成空文本与 `idle`；
 * - `statusItems` 只接受数组（不是数组就不是"清空数组"，而是发错了，保持旧值）。
 *
 * @param {Record<string, any>} state 当前 chrome 状态
 * @param {Record<string, any>} data `cubby:chrome` 消息体
 * @returns {Record<string, any>} 合并后的新状态
 */
export function mergeChromeState(state, data) {
  const next = { ...state }
  if (typeof data.title === 'string') next.title = data.title
  if (typeof data.subtitle === 'string') next.subtitle = data.subtitle
  if (typeof data.view === 'string') next.view = data.view
  if (Object.prototype.hasOwnProperty.call(data, 'freshness_scope')) {
    const scope = normalizeFreshnessScope(data.freshness_scope)
    next.freshness_scope = scope === null ? '' : scope
  }
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
