/**
 * 插件 iframe 的挂载计划（宿主侧）。
 *
 * 为什么单独成模块：这些判定原先写在 App.vue 里，而 App.vue 是 SFC —— Node 与 Python
 * 都无法直接驱动它（要先过 Vue 编译器），于是"哪些插件常驻、iframe 的 src 怎么拼、
 * 切走时该给谁发 dispose、重载后补哪条通知"只能靠对源码做 assertIn：改一句注释就红，
 * 把两组判定的极性写反反而可能绿。它们的输入输出都是纯数据，抽出来就能被
 * tests/js/plugin_frames.mjs 直接 import 真实实现断言行为。
 *
 * 与 plugin-visibility.js 的分工：那一份回答"这次该不该发通知"，这一份回答"什么时候
 * 去问它，以及 iframe 本身的挂载形态"。除它之外不 import 任何模块，Node 直接跑。
 */
import { LIFECYCLE_MESSAGES } from './plugin-visibility.js'

/**
 * 按 `keepAlive` 把插件分成"常驻"与"过路"两组。
 *
 * 声明了 `keepAlive: true` 的插件留在 keep-alive 容器里（`v-show` 隐藏，iframe 不销毁，
 * 切走时只发 hidden）；没有声明的插件离开即卸载，回来是干净重载。两组必须互补且不交叠：
 * 同一个插件同时进两组会让它的 iframe 被渲染两次（两个 iframe 抢同一个 URL），
 * 两边都不进则是"点了导航没反应"。
 *
 * @template {{ keepAlive?: boolean }} T
 * @param {readonly T[]} plugins
 * @returns {{ keepAlive: T[], transient: T[] }}
 */
export function splitByKeepAlive(plugins) {
  const keepAlive = []
  const transient = []
  for (const plugin of plugins || []) {
    if (!plugin) continue
    if (plugin.keepAlive) keepAlive.push(plugin)
    else transient.push(plugin)
  }
  return { keepAlive, transient }
}

/**
 * 拼一个插件 iframe 的 src。
 *
 * 只做两件事：
 * 1. 声明了 `frontend.chrome` 的插件由壳渲染页面骨架、内容页只装内容 —— 因此要带
 *    `content=1`，否则内容页会把自己那套导航也画出来，界面上出现两排导航；
 * 2. 重试计数 `_r` 追加在末尾。它唯一的用途是改变 URL 逼浏览器重新加载同一个页面，
 *    因此不能覆盖已有查询串（`content=1` 被 `?` 截断会让壳重新渲染骨架）。
 *
 * @param {{ entryUrl: string, chrome?: unknown }} plugin
 * @param {number} [reloadCount] 已重试次数；0、缺省或非法值表示不加参数
 * @returns {string}
 */
export function pluginFrameSrc(plugin, reloadCount) {
  const entryUrl = String(plugin.entryUrl || '')
  const base = plugin.chrome
    ? `${entryUrl}${entryUrl.includes('?') ? '&' : '?'}content=1`
    : entryUrl
  const count = Math.max(0, Math.trunc(Number(reloadCount) || 0))
  if (!count) return base
  return `${base}${base.includes('?') ? '&' : '?'}_r=${count}`
}

/**
 * 路由切换后，哪些已注册的 frame 必须 dispose。
 *
 * 只处理"不保活且当前仍作为活动插件挂载"的那些：保活插件的 iframe 留着（切走只发
 * hidden，媒体播放不该被打断）；未挂载的 frame 在上一次离开时已经 dispose 过，
 * 再发一次会让插件的清理逻辑被重复触发。
 *
 * @param {{
 *   registered: Iterable<string>,
 *   mounted: Iterable<string>,
 *   keepAlive: Iterable<string>,
 * }} input
 * @returns {string[]} 按注册顺序排列的待 dispose 插件名
 */
export function leavingFrames(input) {
  const keepAlive = new Set(input.keepAlive || [])
  const mounted = new Set(input.mounted || [])
  const leaving = []
  for (const name of input.registered || []) {
    if (!name || keepAlive.has(name) || !mounted.has(name)) continue
    leaving.push(name)
  }
  return leaving
}

/**
 * iframe 重载（错误重试 / 设置变更改了 src）之后要补发的通知。
 *
 * 新文档对"自己是否可见"的认知回到初始值（默认认为自己隐藏），所以只有当它重载后
 * 仍然是活动插件、且宿主窗口也可见时才补一条 shown。后台重载的新文档必须保持沉默：
 * 告诉它"可见"会让它在不可见的 iframe 里跑定时器与轮询。
 *
 * @param {boolean} mounted 重载后是否仍作为活动插件挂载
 * @param {boolean} windowVisible 宿主窗口是否可见
 * @returns {import('./plugin-visibility').LifecycleMessage[]}
 */
export function reloadedFrameMessages(mounted, windowVisible) {
  return mounted && windowVisible ? [LIFECYCLE_MESSAGES.shown] : []
}
