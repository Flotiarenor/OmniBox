/**
 * 插件 iframe 可见性状态机的类型声明。
 *
 * 实现是 plugin-visibility.js（纯 JS：Node 直接 import 做无头测试，无需转译）。
 * 这里给出类型，App.vue 侧保持 strict 模式的类型检查通过。
 */

/** 需要发给插件 iframe 的生命周期通知（完整的 postMessage type） */
export type LifecycleMessage = 'omnibox:plugin-shown' | 'omnibox:plugin-hidden' | 'omnibox:plugin-dispose'

/** 消息类型表：宿主发给插件的协议串单一真源 */
export declare const LIFECYCLE_MESSAGES: {
  readonly shown: 'omnibox:plugin-shown'
  readonly hidden: 'omnibox:plugin-hidden'
  readonly dispose: 'omnibox:plugin-dispose'
}

/** 登记/更新一个 frame 是否已作为活动插件挂载 */
export declare function ensureFrame(name: string, mounted: boolean): LifecycleMessage[]

/**
 * 记录 frame 当前文档代次（每次 iframe load 递增）。
 *
 * 返回是否发生了重载；重载会丢弃旧文档的可见性认知，使下一次判定必定产生一条通知。
 */
export declare function noteFrameEpoch(name: string, epoch: number): boolean

/** 可见性判定的三个输入 */
export interface FrameState {
  /** 是否已作为活动插件挂载 */
  mounted: boolean
  /** 是否是当前路由对应的活动插件 */
  active: boolean
  /** 宿主窗口是否可见（visibilitychange） */
  windowVisible: boolean
}

/** 按"挂载 / 活动 / 窗口可见"三者重新判定并返回需要发送的通知（已去重） */
export declare function refreshFrame(name: string, state: FrameState): LifecycleMessage[]

/** 页面卸载前的收尾：指定 frame 一律 dispose，并清空其状态 */
export declare function disposeFrames(names?: Iterable<string>): LifecycleMessage[]

/** 单测/调试用：内部状态快照 */
export declare function frameVisibilitySnapshot(): Map<string, boolean>
