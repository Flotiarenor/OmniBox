/* 把当前子相册「提升到全部相册」（01-hero 要拍出「已提升」角标）。
 *
 * 用法：宿主里先点开某个画师卡片的 ⋯ 菜单（`.iv-album-menu`），再执行本脚本 ——
 * 菜单项由 plugins/image-viewer/frontend/js/app-nav.js 的 showAlbumMenu 渲染：
 * 已提升时那一条会变成「收回父相册」（data-act="unpromote"）。
 *
 * **幂等**：已经是提升状态就什么都不做。不这么写的话，第二次拍摄会把上一次的提升
 * 收回去 —— 而那一次的画面里就没有「已提升」角标了，且失败是静默的（点得到、也点成功了）。
 */
(() => {
  const items = Array.from(document.querySelectorAll('.iv-context-menu button'));
  const promote = items.find(btn => btn.dataset.act === 'promote');
  if (!promote) {
    return items.some(btn => btn.dataset.act === 'unpromote') ? 'already-promoted' : 'no-menu-item';
  }
  promote.click();
  return 'promoted';
})();
