// media-player 卡片 / 媒体行迁移到共享 ContentUI 之后的交互契约（无头）。
//
// 迁移提交 f8762b8 / 2671699 / 051c521 / 4923cf3 把专辑卡与媒体行从手拼 innerHTML
// 换成了壳注入的 `/shell/content-ui.js`。手拼时代由 `_bindContentDelegation` 统一
// 代理的几件事，现在一半在共享原语里（role=button、Tab/Enter/Space、行内动作不触发
// 主激活），一半仍由插件负责（封面降级、视频抽帧钩子、播放按钮）。任何一边掉链子，
// 表现都是"点封面没反应 / 点喜欢却开始播放 / 视频封面永远空白"，而浏览器 e2e 很慢，
// 所以这里用真实 content-ui.js + 真实 app-render.js 锁住：
//
//   1. 卡片/行都带 role=button、tabindex=0，Enter/Space 激活，焦点在行内动作上不激活；
//   2. 行内动作按钮的点击/键盘不触发主激活；
//   3. 「播放全部」按钮走 _playAlbum，不打开专辑；
//   4. 封面加载失败：音频/专辑走占位降级，视频先走 MediaFrameExtractor 抽帧钩子；
//   5. 列表行保留序号、时长、动作按钮与喜欢状态刷新。
//
// 用法：node tests/js/media_player_content_ui.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const PROJECT = path.resolve(here, '..', '..');
const JS_DIR = path.join(PROJECT, 'plugins', 'media-player', 'frontend', 'js');
const CONTENT_UI = path.join(PROJECT, 'shell', 'frontend', 'public', 'shell', 'content-ui.js');

let failed = 0;
let total = 0;
async function check(name, fn) {
  total++;
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

/** dom_stub 的元素只差三样真实 DOM 语义：属性、closest、以及 removeAttribute。 */
function enhance(el) {
  if (el.__enhanced) return el;
  el.__enhanced = true;
  const attrs = {};
  el.setAttribute = (key, value) => { attrs[key] = String(value); };
  el.getAttribute = (key) => (Object.prototype.hasOwnProperty.call(attrs, key) ? attrs[key] : null);
  el.removeAttribute = (key) => { delete attrs[key]; };
  el.hasAttribute = (key) => Object.prototype.hasOwnProperty.call(attrs, key);
  const matches = (node, selector) => {
    if (selector.startsWith('.')) return String(node.className).split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    return node.tagName === selector;
  };
  el.closest = (selector) => {
    let node = el;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  };
  return el;
}

function makeHarness() {
  const { document, makeEl } = createDom();
  const storage = new Map();
  const state = { calls: [], toasts: [], plugin: {}, api: {} };
  const element = (id) => {
    const found = document.getElementById(id);
    if (found) return found;
    const created = enhance(makeEl('div'));
    created.id = id;
    document.body.appendChild(created);
    return created;
  };
  const documentShim = {
    ...document,
    createElement: (tag) => enhance(makeEl(tag)),
    getElementById: (id) => element(String(id)),
  };
  const sandbox = {
    window: null,
    document: documentShim,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    Bridge: {
      call: async (method, ...args) => {
        state.calls.push({ method, args });
        const handler = state.api[method];
        return handler ? handler(...args) : {};
      },
      callPlugin: async (plugin, method, ...args) => {
        state.calls.push({ plugin, method, args });
        const handler = state.plugin[method];
        return handler ? handler(...args) : {};
      },
      originalUrl: (p) => `/file?path=${encodeURIComponent(p)}`,
      thumbUrl: (id) => `/thumbs/${id}`,
    },
    Toast: {
      error: (m) => state.toasts.push({ level: 'error', text: m }),
      info: (m) => state.toasts.push({ level: 'info', text: m }),
      success: (m) => state.toasts.push({ level: 'success', text: m }),
    },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => { },
  };
  // 浏览器里 window 就是全局对象：content-ui.js 用 `window.ContentUI =` 暴露裸全局。
  sandbox.window = sandbox;

  const ctx = vm.createContext(sandbox);
  for (const rel of ['js/utils.js', 'js/app.js', 'js/app-ui-events.js', 'js/app-render.js']) {
    vm.runInContext(fs.readFileSync(path.join(PROJECT, 'plugins/media-player/frontend', rel), 'utf8'),
      ctx, { filename: rel });
  }
  vm.runInContext(fs.readFileSync(CONTENT_UI, 'utf8'), ctx, { filename: 'shell/content-ui.js' });

  const app = vm.runInContext('new MediaPlayerApp()', ctx);
  app.core = { setQueue: (items, index) => { state.queued = { items, index }; } };
  return { ctx, state, document: documentShim, app, element, sandbox };
}

function fakeEvent(target, extra = {}) {
  const event = {
    target,
    defaultPrevented: false,
    prevented: false,
    stopped: false,
    preventDefault() { event.prevented = true; event.defaultPrevented = true; },
    stopPropagation() { event.stopped = true; },
    ...extra,
  };
  return event;
}

function click(element, target = element) {
  const handlers = element.handlers.click || [];
  const event = fakeEvent(target);
  handlers.forEach((fn) => fn(event));
  return event;
}

function keydown(element, key, target = element) {
  const handlers = element.handlers.keydown || [];
  const event = fakeEvent(target, { key });
  handlers.forEach((fn) => fn(event));
  return event;
}

// ============================================================
// 1. 专辑卡：可激活、键盘可达、播放按钮与激活互斥
// ============================================================

await check('专辑卡渲染在 ContentUI 上：role=button / tabindex=0 / aria 与封面 URL', () => {
  const h = makeHarness();
  const album = { key: 'alb:1', kind: 'audio', name: '专辑 A', artist: '歌手',
    count: 2, total_duration: 125, cover_item_id: 'c1' };
  const card = h.app._buildAlbumCard(album, 0, 'audio');

  assert.match(card.className, /cui-card/);
  assert.match(card.className, /mp-card/);
  assert.equal(card.getAttribute('role'), 'button', '共享原语要给出 role=button');
  assert.equal(card.tabIndex, 0, '共享原语要给出 tabindex=0（Tab 可达）');
  assert.equal(card.getAttribute('aria-label'), '打开音乐专辑 专辑 A');
  assert.equal(card.dataset.key, 'alb:1');
  assert.equal(card.dataset.kind, 'audio');
  const img = card.querySelector('.cui-image');
  assert.ok(img, '有封面时要渲染 img');
  assert.equal(img.src, '/thumbs/c1');
  assert.equal(img.alt, '');
  assert.match(card.querySelector('.cui-title').textContent, /专辑 A/);
  assert.match(card.querySelector('.cui-sub').textContent, /歌手/);
  assert.equal(card.querySelector('.cui-media-badge').textContent, '2 项', '数量走封面 overlay');
  assert.equal(card.querySelector('.cui-media-count').textContent, '2 分钟', '总时长走封面 overlay');
});

await check('卡片 Enter 与 Space 都打开专辑，普通按键不激活', () => {
  const h = makeHarness();
  const opened = [];
  h.app.openAlbum = (key, kind) => opened.push([key, kind]);
  const card = h.app._buildAlbumCard({ key: 'k', kind: 'video', name: 'V', artist: '', count: 1,
    total_duration: 1 }, 0, 'video');

  const enter = keydown(card, 'Enter');
  assert.deepEqual(opened, [['k', 'video']]);
  assert.equal(enter.defaultPrevented, true, '激活后要 preventDefault，避免滚页 / 重复处理');
  const space = keydown(card, ' ');
  assert.deepEqual(opened, [['k', 'video'], ['k', 'video']]);
  assert.equal(space.defaultPrevented, true);
  keydown(card, 'a');
  assert.equal(opened.length, 2, '非 Enter/Space 不激活');
  click(card, card.querySelector('.cui-title'));
  assert.equal(opened.length, 3, '鼠标点正文也要打开专辑');
});

await check('点卡片上的播放按钮不打开专辑，且不冒泡到卡片激活', () => {
  const h = makeHarness();
  const opened = [];
  h.app.openAlbum = (key, kind) => opened.push([key, kind]);
  const card = h.app._buildAlbumCard({ key: 'k', kind: 'audio', name: 'A', artist: '', count: 1,
    total_duration: 1 }, 0, 'audio');
  const play = card.querySelector('.mp-card-play');
  assert.ok(play, '专辑卡要有播放全部按钮');
  const own = click(play, play);
  const event = click(card, play);
  assert.equal(own.stopped, true, '行内动作自身要 stopPropagation');
  assert.equal(opened.length, 0, '播放按钮不能触发卡片激活');
});

await check('焦点在播放按钮上按 Enter/Space 不打开专辑', () => {
  const h = makeHarness();
  const opened = [];
  h.app.openAlbum = (key, kind) => opened.push([key, kind]);
  const card = h.app._buildAlbumCard({ key: 'k', kind: 'audio', name: 'A', artist: '', count: 1,
    total_duration: 1 }, 0, 'audio');
  const play = card.querySelector('.mp-card-play');
  keydown(card, 'Enter', play);
  keydown(card, ' ', play);
  assert.equal(opened.length, 0, '行内动作上的键盘事件不能激活整张卡');
});

// ============================================================
// 2. 媒体行：序号 / 时长 / 动作 / 封面降级 / 视频抽帧
// ============================================================

await check('媒体行保留序号、标题、副标题、时长与三个行内动作', () => {
  const h = makeHarness();
  h.app.favIds = new Set();
  const item = { id: 'a1', kind: 'audio', title: '曲目 1', artist: '歌手', album: '专辑',
    duration: 125, path: '/x/a.mp3', has_cover: false };
  const row = h.app._buildMediaRow(item, 0, { showAlbum: true, showTag: false });

  assert.match(row.className, /cui-row/);
  assert.equal(row.getAttribute('role'), 'button');
  assert.equal(row.tabIndex, 0);
  assert.equal(row.querySelector('.mp-row-index').textContent, '1');
  assert.equal(row.querySelector('.cui-title').textContent, '曲目 1');
  assert.equal(row.querySelector('.cui-sub').textContent, '歌手 · 专辑');
  assert.equal(row.querySelector('.mp-row-duration').textContent, '02:05');
  const actions = row.querySelectorAll('.mp-row-action');
  assert.deepEqual(actions.map(a => a.dataset.mpAction), ['fav', 'queue', 'playlist']);
  assert.ok(actions.every(a => a.title), '动作按钮要有可读 title/aria-label');
});

/** dom_stub 的 querySelector 只认 tag/.class/#id，属性选择器用 class 找到再过滤。 */
function actionButton(row, action) {
  return row.querySelectorAll('.mp-row-action').find((btn) => btn.dataset.mpAction === action) || null;
}

await check('行内动作的点击与键盘都不触发行激活；点正文才激活', () => {
  const h = makeHarness();
  const activated = [];
  h.app._activateRow = (index, event) => activated.push(index);
  const item = { id: 'a1', kind: 'audio', title: '曲目', artist: '', duration: 1, path: '', has_cover: false };
  const row = h.app._buildMediaRow(item, 0, {});
  const fav = actionButton(row, 'fav');
  assert.ok(fav, '行内动作按钮要能找到');

  click(row, fav);
  keydown(row, 'Enter', fav);
  keydown(row, ' ', fav);
  assert.equal(activated.length, 0, '行内动作不能触发主激活（否则点喜欢就开始播放）');

  click(row, row.querySelector('.cui-title'));
  assert.deepEqual(activated, [0], '点正文才激活行');
});

await check('音频封面缺失时插入占位并清掉空媒体层的 aria-hidden', () => {
  const h = makeHarness();
  const item = { id: 'a1', kind: 'audio', title: '无封面', artist: '', duration: 1, path: '', has_cover: false };
  const row = h.app._buildMediaRow(item, 0, {});
  const media = row.querySelector('.cui-media');
  assert.ok(media.classList.contains('img-broken') || media.querySelector('.cover-fallback'),
    '无封面要插入 .cover-fallback 占位');
  assert.ok(media.querySelector('.cover-fallback'));
  assert.equal(media.getAttribute('aria-hidden'), null, '占位代替图片后不能继续对读屏隐藏');
});

await check('封面加载失败：音频走占位降级，不抛错', () => {
  const h = makeHarness();
  const item = { id: 'a1', kind: 'audio', title: '有封面', artist: '', duration: 1,
    path: '', has_cover: true, mtime: 1700000000 };
  const row = h.app._buildMediaRow(item, 0, {});
  const img = row.querySelector('.cui-image');
  assert.ok(img, '有封面时先渲染 img');
  const media = img.parentElement;
  const errorHandler = (img.handlers.error || [])[0];
  assert.ok(errorHandler, 'ContentUI 的 onError 要接上');
  errorHandler({ type: 'error' }, img);
  assert.ok(media.classList.contains('img-broken'));
  assert.ok(media.querySelector('.cover-fallback'), '失败后降级为占位元素');
});

await check('视频封面失败先走 MediaFrameExtractor 抽帧钩子，失败再降级', () => {
  const h = makeHarness();
  const requests = [];
  h.sandbox.window.MediaFrameExtractor = {
    request(id, img, onFail) { requests.push({ id, img, onFail }); },
  };
  const item = { id: 'v1', kind: 'video', title: '视频', artist: '', duration: 10,
    path: '', has_cover: true };
  const row = h.app._buildMediaRow(item, 0, {});
  const img = row.querySelector('.cui-image');
  assert.equal(img.dataset.mpThumbId, 'v1', 'img 要带 data-mp-thumb-id 供抽帧 / 预取定位');
  const media = img.parentElement;
  (img.handlers.error || [])[0]({ type: 'error' }, img);

  assert.equal(requests.length, 1, '视频封面失败必须请求前端抽帧');
  assert.equal(requests[0].id, 'v1');
  assert.equal(requests[0].img, img);
  assert.equal(media.classList.contains('img-broken'), false, '抽帧未出结果前不能先降级');

  requests[0].onFail();
  assert.ok(media.querySelector('.cover-fallback'), '抽帧失败后才降级为占位');
});

// ============================================================
// 3. 插件的点击代理：播放按钮走 _playAlbum，事件已处理时不重复激活
// ============================================================

await check('内容代理的捕获阶段接管播放按钮：播放全部而不是打开专辑', async () => {
  const h = makeHarness();
  const content = h.element('media-content');
  h.app._bindContentDelegation();
  const opened = [];
  h.app.openAlbum = (key, kind) => opened.push([key, kind]);
  h.state.api.media_album_items = () => [{ id: 'x', title: '第一首' }];

  const play = h.document.createElement('button');
  play.className = 'mp-card-play';
  play.dataset.key = 'k1';
  play.dataset.kind = 'audio';
  content.appendChild(play);

  const capture = content.handlers.click[0];
  const event = fakeEvent(play);
  await capture(event);

  assert.equal(opened.length, 0, '播放按钮不能同时打开专辑');
  assert.equal(event.stopped, true);
  assert.ok(h.state.queued, '要调用 core.setQueue 开始播放');
  assert.deepEqual(h.state.queued.items.map(i => i.id), ['x']);
  assert.equal(h.state.queued.index, 0);
});

await check('内容代理的捕获阶段接管行内动作：按 data-idx 路由到 _handleRowAction', async () => {
  const h = makeHarness();
  const content = h.element('media-content');
  h.app._bindContentDelegation();
  const handled = [];
  h.app._handleRowAction = async (action, idx) => handled.push([action, idx]);
  h.app._currentListData = [{ id: 'a1' }, { id: 'a2' }];

  const btn = h.document.createElement('button');
  btn.className = 'mp-row-action danger';
  btn.dataset.mpAction = 'remove';
  btn.dataset.idx = '1';
  content.appendChild(btn);

  const capture = content.handlers.click[0];
  await capture(fakeEvent(btn));

  assert.deepEqual(handled, [['remove', 1]], '行内动作必须按 data-idx 找到条目');
});

await check('ContentUI 已 preventDefault 时，冒泡代理不再重复激活行', async () => {
  const h = makeHarness();
  const content = h.element('media-content');
  h.app._bindContentDelegation();
  const activated = [];
  h.app._activateRow = async (index) => activated.push(index);
  h.app.openAlbum = () => { throw new Error('不应打开专辑'); };

  const row = h.document.createElement('div');
  row.className = 'mp-row';
  row.dataset.idx = '2';
  content.appendChild(row);

  const bubble = content.handlers.click[1];
  const alreadyHandled = fakeEvent(row);
  alreadyHandled.defaultPrevented = true;
  await bubble(alreadyHandled);
  assert.equal(activated.length, 0, '共享原语已处理过的行不能再激活一次');

  await bubble(fakeEvent(row));
  assert.deepEqual(activated, [2], '没有 ContentUI 的旧 DOM 由冒泡代理兜底');
});

// ============================================================
// 4. 喜欢状态刷新（动作按钮仍是同一套 data-* 定位）
// ============================================================

await check('喜欢状态刷新：按钮 class/title/aria 跟着 favIds 走', () => {
  const h = makeHarness();
  h.app.favIds = new Set();
  const item = { id: 'a1', kind: 'audio', title: '曲目', artist: '', duration: 1, path: '', has_cover: false };
  const row = h.app._buildMediaRow(item, 0, {});
  const fav = actionButton(row, 'fav');
  assert.ok(fav);
  h.app._currentListData = [item];
  // app 用属性选择器定位按钮；dom_stub 不支持属性选择器，这里用同一行元素兜住接口。
  h.app._currentListContainer = {
    querySelector: (selector) => (selector.includes('mp-row') ? row : null),
  };
  row.querySelector = (selector) => (selector.includes('[data-mp-action="fav"]') ? fav : null);
  h.app.favIds.add('a1');

  h.app._refreshRowState();

  assert.ok(fav.classList.contains('fav-active'));
  assert.equal(fav.getAttribute('aria-label'), '取消喜欢');
  assert.equal(fav.title, '取消喜欢');
});

console.log(failed ? `\n${failed} 例失败` : `\nmedia-player ContentUI 契约 ${total} 例通过`);
process.exit(failed ? 1 : 0);
