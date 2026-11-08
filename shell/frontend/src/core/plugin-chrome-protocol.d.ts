/**
 * 插件 chrome 协议（`omnibox:*`）的类型声明。
 *
 * 实现是 plugin-chrome-protocol.js：纯 JS 才能被 Node 直接 import 做无头断言，
 * 且不需要 `--experimental-strip-types`。与 plugin-url.js / plugin-visibility.js
 * 是同一种分工。
 */
import type { PluginChromeState } from './plugin-loader'

/** 壳接受的插件 chrome 消息类型；每一项都必须在 App.vue 里有处理分支 */
export declare const PLUGIN_CHROME_MESSAGE_TYPES: readonly string[]

/** 消息体是不是壳认识的 chrome 消息（未登记类型一律 false） */
export declare function isPluginChromeMessage(data: unknown): boolean

/** 壳 → 内容页的刷新事件类型（带作用域与后端报告） */
export declare const FRESHNESS_EVENT: string

/** `omnibox:freshness` 的消息体；`report` 是后端报告的透传 */
export interface FreshnessEvent {
  readonly [key: string]: unknown
  type: string
  plugin: string
  scope: string
  report: Record<string, unknown> | null
}

/** 构造一条 `omnibox:freshness` 消息（`report` 非对象时按 null 处理） */
export declare function buildFreshnessEvent(
  plugin: string,
  scope: string,
  report: unknown,
): FreshnessEvent

/** `freshness_scope` 的长度上限 */
export declare const FRESHNESS_SCOPE_MAX: number

/**
 * 归一化内容页上报的同步作用域：trim、空串合法、超长与含 `..` 的值返回
 * `undefined`（调用方保留旧值）。
 */
export declare function normalizeFreshnessScope(value: unknown): string | undefined

/**
 * 把一条 `omnibox:chrome` 补丁并进当前状态，返回新状态（不修改入参）：
 * 字符串字段覆盖、badges 整份替换并丢空值、actions/nav/search 按键合并、
 * status 缺省回退、statusItems 只接受数组。
 */
export declare function mergeChromeState(
  state: PluginChromeState,
  data: Record<string, unknown>,
): PluginChromeState
