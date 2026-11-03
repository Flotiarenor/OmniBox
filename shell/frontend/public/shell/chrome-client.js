/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * 插件内容页 ↔ 壳的页面骨架（chrome）握手客户端。
 *
 * 为什么需要：document-reader / image-viewer / manga-library / media-player 各写过一份
 * 同形状的握手 —— 父窗口判空、`origin` + `event.source` 校验、`omnibox:view` /
 * `omnibox:action` / `omnibox:query` 分发、`omnibox:ready` 握手。协议改一处就要同步四处，
 * 漏一处只表现为"该插件点了没反应"。
 *
 * 只收敛"怎么收发"，不解释业务：收到某条 action / query 该做什么由调用方给的回调决定，
 * 插件各自的视图 id、动作名、搜索处理仍留在插件侧。
 *
 * 与 HostChannel 的分工：那一份用于"内嵌页请求上层渲染自己的 chrome"（设置弹窗、工具条
 * 按钮）；这一份用于"内容页向壳上报自己的 chrome，并接收壳下发的视图 / 动作 / 搜索"。
 *
 * 注入位置：Shell 引导片段把本文件排在共享运行时这一层的末尾、folder-picker.js 之前；
 * 它不依赖 base.js（只用 window / location），但插件脚本在 <head> 里跟着一起执行，
 * 所以必须先于插件自己的脚本就绪。
 */
window.ChromeClient = (function () {
  'use strict'

  /** 只接受「直接父窗口 + 同源」的消息：伪造来源与跨源消息一律忽略。 */
  function fromShell(event) {
    return event.origin === location.origin && event.source === window.parent
  }

  /**
   * 发一条消息给壳（父窗口）。
   *
   * 判空与 origin 兜底是为无头用例保留的：`window`、`parent`、`location` 都可能不存在，
   * 这时静默不发，而不是抛错。
   */
  function post(payload) {
    if (typeof window === 'undefined' || !window.parent || window.parent === window) return
    const origin = typeof location !== 'undefined' ? location.origin : ''
    try { window.parent.postMessage(payload, origin) } catch (e) { /* 非同源父窗口不发 */ }
  }

  /**
   * @param {{
   *   onView?: (view: string) => void,
   *   onAction?: (action: string) => void,
   *   onQuery?: (query: string) => void,
   *   onMessage?: (data: Record<string, unknown>) => void,
   * }} [handlers]
   *   业务回调。`onMessage` 收到 view / action / query 之外的类型（例如壳下发的
   *   `omnibox:appearance`），由插件自己解释。
   */
  function create(handlers) {
    const hooks = handlers || {}
    let listener = null

    function onMessage(event) {
      if (!fromShell(event)) return
      const data = event.data || {}
      if (data.type === 'omnibox:view' && typeof data.view === 'string') {
        if (hooks.onView) hooks.onView(data.view)
        return
      }
      if (data.type === 'omnibox:action') {
        if (hooks.onAction) hooks.onAction(data.action)
        return
      }
      if (data.type === 'omnibox:query') {
        if (hooks.onQuery) hooks.onQuery(data.query)
        return
      }
      if (hooks.onMessage) hooks.onMessage(data)
    }

    return {
      post,
      /** 就绪握手：壳收到后补发当前视图。 */
      ready() {
        post({ type: 'omnibox:ready' })
      },
      /** 上报 chrome 补丁（title / subtitle / view / actions / nav / search / status …）。 */
      chrome(patch) {
        post(Object.assign({ type: 'omnibox:chrome' }, patch || {}))
      },
      /** 发一条插件自己定义的消息类型（例如 `omnibox:open-settings`）。 */
      send(payload) {
        post(payload)
      },
      /**
       * 先监听、再握手。顺序不能反：壳收到 ready 后会立刻补发当前视图，
       * 监听器晚一步就丢掉这一条（表现为首屏视图不对）。
       */
      bind() {
        if (listener) return
        listener = onMessage
        window.addEventListener('message', listener)
        post({ type: 'omnibox:ready' })
      },
      /** 解绑（插件目前不在卸载时调用，留给需要重绑的宿主）。 */
      unbind() {
        if (!listener) return
        window.removeEventListener('message', listener)
        listener = null
      },
      get bound() {
        return listener !== null
      },
      /** 是否真的嵌在壳里（脱离壳单独打开时为 false）。 */
      get embedded() {
        return typeof window !== 'undefined' && !!window.parent && window.parent !== window
      },
    }
  }

  return { create, post }
})()
