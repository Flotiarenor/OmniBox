/**
 * `plugin-url.js` 的类型声明。
 *
 * 实现是纯 JS（Node 直接 import 做无头测试，无需转译），这里给 App.vue 侧
 * strict 模式的类型检查用。
 */

/** 插件名形状：小写字母/数字开头，后续只允许小写字母、数字与连字符。 */
export declare function isPluginName(value: unknown): boolean

/** URL 是否只指向 `/plugins/<plugin>/` 目录内（点段与编码分隔符会被拒绝）。 */
export declare function isPluginPageUrl(plugin: string, url: unknown): boolean
