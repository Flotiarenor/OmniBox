/* 把**指定画师**提升到「全部相册」（01-hero 要拍出唯一一枚「已提升」角标）。
 *
 * 为什么要点名画师：基线里 pixiv 下的画师按修改时间排序，第一张卡会随素材更新而变；
 * 按"第一张卡"提升会让画面出现两枚「已提升」徽标（缓存里本来已经提升过某一位）。
 *
 * 用法：宿主里先进入 pixiv 相册（画师卡片可见），再执行本脚本 —— 它自己打开目标画师的
 * ⋯ 菜单、按需点「提升」、并且**无论走哪条分支都把菜单收掉**（浮层留在画面上会盖住卡片，
 * 正式出图时就这样翻过车）。幂等：已经是提升状态就只收菜单，不再动数据。
 */
(() => {
  const ARTIST = '柠檬静静静静';
  const byName = (a) => (a.dataset.path || '').split('/').pop() === ARTIST;
  const card = Array.from(document.querySelectorAll('.iv-album')).find(byName);
  if (!card) return 'no-artist-card';
  const button = card.querySelector('.iv-album-menu');
  if (!button) return 'no-menu-button';
  button.click();                       // 菜单由 showAlbumMenu 同步插进 body
  const menu = document.querySelector('.iv-context-menu');
  if (!menu) return 'no-menu';
  const promote = Array.from(menu.querySelectorAll('button'))
    .find((item) => item.dataset.act === 'promote');
  const result = promote ? (promote.click(), 'promoted') : 'already-promoted';
  // 点「提升」时菜单自己的处理器会移除它；这里兜底一次，保证浮层一定不在画面里。
  if (menu.isConnected) menu.remove();
  return result;
})();
