// manga-library 书架/搜索迁移回归：ContentUI 卡片、封面降级、收藏、空态区分。
//
// 迁移前这些节点由插件自己拼（.ml-card + 手写 click）；迁移后结构走共享
// `ContentUI.card`（role/tabindex/Enter 激活/操作槽位），封面降级仍是插件自己的
// `.ml-cover-fallback`。这里锁住两件事都不能丢：
//   1. 卡片仍是可激活的书架项（点击与 Enter 都能打开详情），收藏按钮不触发卡片激活；
//   2. "搜索无结果"与"空库"必须是两套文案 —— 前者不该提示去改漫画根目录。
import assert from 'node:assert/strict';
import { loadManga, defaultHandlers, createApp } from './manga_library_harness.mjs';

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

const mangaA = {
    folder_name: 'A', title: '漫画A', author: '作者A', page_count: 12,
    cover_url: '', is_fav: false,
};
const mangaB = {
    folder_name: 'B', title: '漫画B', author: '', page_count: 3,
    cover_url: 'covers/b.jpg', is_fav: true,
};
const detail = {
    folder_name: 'A', title: '漫画A', author: '作者A', is_fav: false,
    is_multi_chapter: true, info: {},
    chapters: [{ name: '第 1 话', path: 'ch1', cover_url: '' }],
};

let library = [mangaA, mangaB];
const env = loadManga({
    handlers: defaultHandlers({
        manga_list: () => library,
        manga_get_state: () => ({ favorites: [mangaB], recent: [mangaA] }),
        manga_get_detail: () => detail,
        manga_toggle_favorite: () => true,
    }),
});
const app = await createApp(env);
const content = env.document.getElementById('ml-content');
const grid = content.querySelector('.cui-grid');

check('书架渲染成共享网格', !!grid && grid.classList.contains('ml-shelf-grid'));
const cards = grid.querySelectorAll('.ml-shelf-card');
check('每部漫画一张书架卡', cards.length === 2, `实际 ${cards.length}`);
check('卡片 class 同时保留 ContentUI 基类', cards[0].classList.contains('cui-card'));

const cardA = cards.find((card) => card.dataset.folder === 'A');
const cardB = cards.find((card) => card.dataset.folder === 'B');
check('卡片带 folder 标识', !!cardA && !!cardB);
check('标题走 .cui-title', cardA.querySelector('.cui-title').textContent === '漫画A');
check('副标题走 .cui-sub', cardA.querySelector('.cui-sub').textContent === '作者A');
check('页数走封面只读叠加层',
    cardA.querySelector('.cui-media-badge').textContent === '12页',
    cardA.querySelector('.cui-media-badge').textContent);
check('卡片是键盘可激活项',
    cardA.getAttribute('role') === 'button' && cardA.tabIndex === 0,
    `role=${cardA.getAttribute('role')} tabindex=${cardA.tabIndex}`);
check('无封面时用插件自己的图标降级',
    !!cardA.querySelector('.ml-cover-fallback') && !cardA.querySelector('.cui-image'));
check('有封面时走缩略图 URL',
    cardB.querySelector('.cui-image').src === '/file?path=covers%2Fb.jpg',
    cardB.querySelector('.cui-image').src);
check('收藏态来自后端', cardB.querySelector('.ml-fav-star').classList.contains('active'));
check('收藏按钮上有 folder 标识',
    cardB.querySelector('.ml-fav-star').dataset.folder === 'B');
check('导航徽标上报收藏/最近数量',
    env.nav().favorites && env.nav().favorites.badge === '1'
    && env.nav().recent && env.nav().recent.badge === '1',
    JSON.stringify({ favorites: env.nav().favorites, recent: env.nav().recent }));

// ---- 封面加载失败：换成插件自己的占位图，且不能把卡片正文一起换掉 ----
const img = cardB.querySelector('.cui-image');
img.dispatchEvent('error');
check('封面失败后换成占位图', !!cardB.querySelector('.ml-cover-fallback'));
check('占位图不删除卡片正文', cardB.querySelector('.cui-title').textContent === '漫画B');
check('占位图不删除页数叠加层', !!cardB.querySelector('.cui-media-badge'));

// ---- 点击卡片 = 打开详情 ----
const beforeDetail = env.calls('manga_get_detail').length;
cardA.click();
await env.tick();
check('点击卡片调用详情接口',
    env.calls('manga_get_detail').length === beforeDetail + 1);
check('进入详情后 viewLevel 上报为 chapters（多章）', app.viewLevel === 'chapters', app.viewLevel);

// 回到书架，测键盘激活
app.switchView('all');
await env.tick();
const shelfCard = env.document.getElementById('ml-content').querySelector('.ml-shelf-card');
const beforeKey = env.calls('manga_get_detail').length;
shelfCard.dispatchEvent('keydown', { key: 'Enter', target: shelfCard });
await env.tick();
check('Enter 也能激活卡片', env.calls('manga_get_detail').length === beforeKey + 1);
app.switchView('all');
await env.tick();

// ---- 收藏：按钮点击不触发卡片激活 ----
const freshCard = env.document.getElementById('ml-content').querySelector('.ml-shelf-card');
const detailCallsBeforeFav = env.calls('manga_get_detail').length;
const star = freshCard.querySelector('.ml-fav-star');
star.click();
await env.tick();
check('收藏按钮调用收藏接口', env.calls('manga_toggle_favorite').length === 1);
check('收藏按钮不会打开详情',
    env.calls('manga_get_detail').length === detailCallsBeforeFav);
check('收藏成功后星标点亮', star.classList.contains('active'));
check('收藏后徽标重新上报',
    env.nav().favorites.badge === '2', JSON.stringify(env.nav().favorites));

// ---- 空库：仍是"暂无漫画" + 去设置里改根目录 ----
library = [];
app.view = 'all';
app.switchView('all');
await env.tick();
let html = env.document.getElementById('ml-content').innerHTML;
check('空库显示"暂无漫画"', html.includes('暂无漫画'));
check('空库提示去设置里改漫画根目录', html.includes('可在设置中调整漫画根目录'));

// ---- 搜索空态：换文案，不能误报"空库" ----
env.sendQuery('不存在的关键词');
await env.tick();
const searchChrome = env.chrome();
check('搜索后标题带关键词', searchChrome.title.includes('不存在的关键词'), searchChrome.title);
check('搜索结果显示条数', searchChrome.subtitle === '0 个结果', searchChrome.subtitle);
html = env.document.getElementById('ml-content').innerHTML;
check('搜索无结果是独立文案', html.includes('没有匹配的漫画'), html.slice(0, 120));
check('搜索无结果不再提示改根目录', !html.includes('可在设置中调整漫画根目录'));

// ---- 标题里的 HTML 不能变成节点（ContentUI 用 textContent 写入） ----
const dirty = { folder_name: 'X', title: '<b>危险</b>', author: '', page_count: 1, cover_url: '', is_fav: false };
library = [dirty];
env.sendQuery('');
app.switchView('all');
await env.tick();
const dirtyTitle = env.document.getElementById('ml-content')
    .querySelector('.ml-shelf-card').querySelector('.cui-title');
check('书名按文本写入，标签不变成节点',
    dirtyTitle.textContent === '<b>危险</b>' && dirtyTitle.children.length === 0,
    JSON.stringify({ text: dirtyTitle.textContent, children: dirtyTitle.children.length }));

console.log('manga-library 书架/搜索用例全部通过');
