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

/**
 * 把一条 `omnibox:chrome` 补丁并进当前状态，返回新状态（不修改入参）：
 * 字符串字段覆盖、badges 整份替换并丢空值、actions/nav/search 按键合并、
 * status 缺省回退、statusItems 只接受数组。
 */
export declare function mergeChromeState(
  state: PluginChromeState,
  data: Record<string, unknown>,
): PluginChromeState
