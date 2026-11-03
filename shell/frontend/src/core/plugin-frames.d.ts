/**
 * 插件 iframe 挂载计划的类型声明。
 *
 * 实现是 plugin-frames.js：纯 JS 才能被 Node 直接 import 做无头断言，而且不需要
 * `--experimental-strip-types`（CI 的 Node 版本低于该开关所需的 22.6，用了会静默跳过）。
 * 与 plugin-visibility.js + plugin-visibility.d.ts 是同一种分工。
 */
import type { LifecycleMessage } from './plugin-visibility'

/** 挂载计划只关心的插件字段（PluginManifest 的结构子集） */
export interface FramePlugin {
  name: string
  entryUrl: string
  keepAlive?: boolean
  chrome?: unknown
}

export interface KeepAliveSplit<T> {
  /** 声明了 keepAlive 的插件：iframe 常驻，切走只隐藏 */
  keepAlive: T[]
  /** 未声明 keepAlive 的插件：离开即卸载，回来重新加载 */
  transient: T[]
}

/** 按 keepAlive 把插件清单分成常驻与过路两组（顺序保持，两组互补不交叠） */
export declare function splitByKeepAlive<T extends { keepAlive?: boolean }>(
  plugins: readonly T[] | null | undefined,
): KeepAliveSplit<T>

/** 拼 iframe 的 src：chrome 插件加 content=1，重试计数以 `_r` 追加在末尾 */
export declare function pluginFrameSrc(
  plugin: Pick<FramePlugin, 'entryUrl' | 'chrome'>,
  reloadCount?: number,
): string

export interface LeavingFramesInput {
  /** 已注册（渲染过）的 frame 名，顺序即返回顺序 */
  registered: Iterable<string>
  /** 当前仍作为活动插件挂载的 frame 名 */
  mounted: Iterable<string>
  /** 声明了 keepAlive 的插件名 */
  keepAlive: Iterable<string>
}

/** 路由切换后必须 dispose 的 frame 名（不保活且仍挂载的那些） */
export declare function leavingFrames(input: LeavingFramesInput): string[]

/**
 * iframe 重载后要补发的通知：只有"仍是活动插件且窗口可见"才补一条 shown，
 * 其余情形返回空数组（新文档默认认为自己隐藏）。
 */
export declare function reloadedFrameMessages(
  mounted: boolean,
  windowVisible: boolean,
): LifecycleMessage[]
