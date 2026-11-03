/* 让 pixiv-sync 的原生面板显示一份示例数据（README 截图用；不联网、不用真实账号）。
 *
 * 为什么不再直接改 DOM：页架构迁移后，面板的可见内容由**壳**的 NativePanel + SchemaRenderer
 * 按隐藏逻辑页提交的 blocks 渲染，`frontend/native.html` 里没有可改的可见节点。所以这里改成
 * "包装数据源"：替换隐藏逻辑页的 `Bridge.call('get_status')`，再由场景点壳侧顶栏的「刷新状态」
 * 触发一次真实渲染 —— 数字、单位与版式全部来自线上代码。
 *
 * 只覆盖**示例数字**，其余字段沿用真实返回值：特别是 `root_dir` 不伪造 —— 面板上的「下载根」
 * 就是实例自己的 `download_dir` 设置值。仓库里因此不出现任何本机目录，截图里的路径由
 * 仓库外的示例库基线配置提供。
 *
 * 遗漏字段会露出真实的 0，从图上分辨不出来，所以这里按 `get_status` 的完整返回逐项对照补数。
 * 幂等：已经包装过就什么都不做 —— 场景可能因重试跑到第二次。
 */
(() => {
  const frame = document.querySelector('iframe.native-panel-frame');
  if (!frame || !frame.contentWindow) return 'no-native-frame';
  const win = frame.contentWindow;
  if (!win.Bridge || typeof win.Bridge.call !== 'function') return 'no-bridge';
  if (win.__pixivStatusStubbed) return 'already-stubbed';
  const real = win.Bridge.call.bind(win.Bridge);
  // 示例数字：字段名与 `plugins/pixiv-sync/backend/pixiv_sync/status.py` 的返回键一致。
  const sample = {
    token_configured: true,
    downloaded_total: 1466,
    selected_artists: 12,
    following_total: 1240,
    following_done: 1238,
    pending_following: 2,
    bookmarks_total: 186,
    bookmarks_done: 186,
    pending_bookmarks: 0,
    other_done: 42,
    failed_skipped: 7,
  };
  win.Bridge.call = function (method) {
    const result = real.apply(null, arguments);
    if (String(method) !== 'get_status') return result;
    return Promise.resolve(result).then((status) => Object.assign({}, status, sample));
  };
  win.__pixivStatusStubbed = true;
  return 'stubbed';
})();
