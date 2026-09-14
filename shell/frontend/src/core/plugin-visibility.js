/**
 * 插件 iframe 可见性状态机（宿主侧）。
 *
 * 为什么单独成模块：App.vue 是 SFC，Python/Node 侧难以驱动；而"是否要通知插件显示/隐藏"
 * 的判定本身是纯逻辑（挂载 × 活动 × 窗口可见 三个布尔量），抽出来就能被无头脚本覆盖
 * （tests/js/plugin_lifecycle.mjs），而不是只靠"看代码对不对"。
 *
 * 约定：只发"状态真的发生变化"的通知。否则每次路由切换都会重复给插件发 hidden，
 * 插件的清理逻辑会被反复触发（也掩盖真正的状态错误）。
 *
 * 消息类型在这里集中定义：宿主发给插件的协议串只有一个真源，base.js 按同样的
 * 字面量分发（tests/js/plugin_lifecycle.mjs 与构建产物核对都依赖它）。
 *
 * @typedef {'omnibox:plugin-shown' | 'omnibox:plugin-hidden' | 'omnibox:plugin-dispose'} LifecycleMessage
 */

/** 插件生命周期消息类型（宿主 → 插件 iframe） */
export const LIFECYCLE_MESSAGES = {
  shown: 'omnibox:plugin-shown',
  hidden: 'omnibox:plugin-hidden',
  dispose: 'omnibox:plugin-dispose',
}

/** 每个 frame 的上一次已通知状态：true=已通知可见，false=已通知隐藏（或尚未挂载） */
const lastVisible = new Map()
/**
 * 已通知状态对应的"文档代次"。
 *
 * destroyOnLeave 插件被重载（错误重试、设置变更改 src）时 iframe 换了新文档，插件前端的
 * 可见性认知回到初始值，而宿主的 lastVisible 还停在旧文档上 —— 不同步的话插件会永久停在
 * 错误的假设里（例如后台重载的新文档以为自己可见，白跑轮询）。代次变化即视为"未通知过"。
 */
const lastEpoch = new Map()

/** 记录 frame 当前文档代次（每次 iframe load 递增），返回是否发生了重载 */
export function noteFrameEpoch(name, epoch) {
  if (!name) return false
  const changed = lastEpoch.get(name) !== epoch
  lastEpoch.set(name, epoch)
  if (changed) {
    // 新文档：丢弃旧文档的可见性认知，下一次判定必定产生一条通知
    lastVisible.delete(name)
  }
  return changed
}

/**
 * 登记/更新一个 frame 的"已作为活动插件挂载"状态。
 *
 * 必须显式登记：destroyOnLeave 插件在 iframe 加载完成前就可能被切走，此时它还没有过
 * 任何状态；若不登记，插件会在后台加载后被误判为"可见"，第一次真正显示时收不到通知。
 *
 * @param {string} name 插件名
 * @param {boolean} mounted 当前是否作为活动插件挂载
 * @returns {LifecycleMessage[]} 需要发给该 frame 的通知（已去重）
 */
export function ensureFrame(name, mounted) {
  if (!name) return []
  if (mounted) return applyState(name, true)
  // 未挂载的 frame 一律视为隐藏；只有此前已通知过"可见"时才需要补一条 hidden
  if (lastVisible.get(name) === true) {
    lastVisible.set(name, false)
    return [LIFECYCLE_MESSAGES.hidden]
  }
  lastVisible.set(name, false)
  return []
}

/**
 * 按"挂载 / 活动 / 窗口可见"三者重新判定并返回需要发送的通知。
 *
 * @param {string} name 插件名
 * @param {{ mounted: boolean, active: boolean, windowVisible: boolean }} state
 * @returns {LifecycleMessage[]}
 */
export function refreshFrame(name, state) {
  if (!name) return []
  const { mounted, active, windowVisible } = state || {}
  if (!mounted || !active || !windowVisible) {
    // 不可见：只有此前已通知过"可见"才需要发 hidden
    if (lastVisible.get(name) === true) {
      lastVisible.set(name, false)
      return [LIFECYCLE_MESSAGES.hidden]
    }
    if (!lastVisible.has(name)) lastVisible.set(name, false)
    return []
  }
  return applyState(name, true)
}

/**
 * 页面卸载前的收尾：所有已登记的 frame 一律 dispose，并清空状态
 * （避免下次挂载沿用过期的可见性）。
 *
 * @param {Iterable<string>} [names] 需要 dispose 的插件名；缺省为全部已登记 frame
 * @returns {LifecycleMessage[]}
 */
export function disposeFrames(names) {
  const targets = names ? Array.from(names) : Array.from(lastVisible.keys())
  const messages = []
  for (const name of targets) {
    if (!name) continue
    lastVisible.delete(name)
    lastEpoch.delete(name)
    messages.push(LIFECYCLE_MESSAGES.dispose)
  }
  return messages
}

/** 单测/调试用：读取内部状态快照 */
export function frameVisibilitySnapshot() {
  return new Map(lastVisible)
}

/**
 * @param {string} name
 * @param {boolean} visible
 * @returns {LifecycleMessage[]}
 */
function applyState(name, visible) {
  if (lastVisible.get(name) === visible) return []
  lastVisible.set(name, visible)
  return [visible ? LIFECYCLE_MESSAGES.shown : LIFECYCLE_MESSAGES.hidden]
}
