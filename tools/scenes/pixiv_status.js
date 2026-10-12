/* 把 pixiv-sync 的状态栏补成一份示例数据（README 截图用；不联网、不用真实账号）。
 *
 * 字段与格式逐条抄自 plugins/pixiv-sync/frontend/index.html 的 refreshStatus()——
 * 漏补一个字段，画面上就会露出真实的 0，而 picture 里看不出来是漏了哪个。
 * `#st-root` 刻意不动：它由实例的 download_dir 设置渲染，正是我们要展示的路径。
 *
 * 为什么可以直接改 DOM：目标是**一张图**，不是让应用真的处于这个状态。补之前场景已把
 * 页面自己的定时器（refreshStatus 每 10s、冷却倒计时每 1s）停掉，否则下一次轮询会把
 * 这里的取值覆盖回去，而且失败是静默的。
 */
(() => {
  const set = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  const token = document.getElementById('st-token');
  if (token) {
    token.className = 'dot ok';
    token.title = '已配置';
  }
  set('st-total', '已下载 1466 张');
  set('st-last', '上次结束: 09:24:31');
  set('st-ft', '1240');
  set('st-pf', '2');
  set('st-fd', '1238');
  set('st-bt', '186');
  set('st-pb', '0');
  set('st-bd', '186');
  set('st-od', '42');
  set('st-failed', '7');
  set('st-selected', '画师名单: 12 位');
})();
