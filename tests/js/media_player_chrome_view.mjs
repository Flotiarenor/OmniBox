// media-player 与壳的左栏高亮契约（`omnibox:chrome.view`）用例。
//
// 迁移后侧栏由壳的 PluginChrome 渲染，内容页不再操作 `.mp-nav-item`：高亮的唯一输入
// 是内容页上报的 `view`。旧的 `tests/js/media_player_nav_active.mjs` 断言的是
// `_setNavActive` / `.mp-nav-item` 这类已删除的 DOM，本文件补回**行为等价**的断言：
//
//   1. 六个一级视图上报自身 id；
//   2. 网易云扩展视图上报 `ncm-*`（壳据此高亮扩展入口）；
//   3. 专辑详情 / 网易云歌单详情上报空串（壳取消高亮）；
//   4. 本地歌单详情上报 `playlist:<id>`（壳高亮动态歌单项）；
//   5. 扩展视图切回一级视图后，上报的是新视图 —— 侧栏不会同时亮两个。
//
// 用法：node tests/js/media_player_chrome_view.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const JS_DIR = path.resolve(here, '..', '..', 'plugins', 'media-player', 'frontend', 'js');

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

function setup() {
  const messages = [];
  const localStorage = {
    getItem: () => null,
    setItem: () => { },
    removeItem: () => { },
  };
  const sandbox = {
    window: {
      parent: {
        postMessage: (payload) => messages.push(payload),
      },
      addEventListener: () => { },
      Utils: undefined,
    },
    location: { origin: 'http://127.0.0.1:18080' },
    document: { getElementById: () => null, querySelectorAll: () => [], addEventListener: () => { } },
    localStorage,
    Bridge: { call: async () => ({}), callPlugin: async () => ({}), thumbUrl: (id) => `/thumbs/${id}` },
    Toast: { info() { }, error() { }, success() { } },
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => { },
  };
  const ctx = vm.createContext(sandbox);
  for (const rel of ['utils.js', 'app.js', 'app-views.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, rel), 'utf8'), ctx, { filename: rel });
  }
  const app = vm.runInContext('new MediaPlayerApp()', ctx);
  app.playlists = {
    playlists: [{ id: 'p1', name: '歌单一', item_ids: ['a'] }, { id: 'p2', name: '歌单二', item_ids: [] }],
    currentId: '',
    syncNav() { },
    async get(id) { return this.playlists.find(p => p.id === id) || null; },
  };
  app._loadCurrentView = async () => { };
  app._updateStats = async () => { };
  const chrome = () => messages.filter(m => m.type === 'omnibox:chrome').at(-1);
  return { app, messages, chrome };
}

// ============================================================
// 1. 一级视图
// ============================================================

await check('六个一级视图各自上报自己的 id', async () => {
  const { app, chrome } = setup();
  for (const view of ['recent', 'audio-albums', 'all-audio', 'video-albums', 'all-video', 'favorites']) {
    await app.switchView(view);
    app._pushChrome();
    assert.equal(chrome().view, view, `切到 ${view} 要上报 ${view}`);
  }
});

// ============================================================
// 2. 网易云扩展视图 vs 一级视图：后一次上报覆盖前一次（不会两个同时亮）
// ============================================================

await check('网易云视图上报 ncm-*；切回一级视图后只上报新视图', async () => {
  const { app, chrome } = setup();
  app.openNeteaseView('ncm-daily');
  await Promise.resolve();
  app._pushChrome();
  assert.equal(chrome().view, 'ncm-daily', '扩展入口高亮靠这个 id');

  await app.switchView('all-audio');
  app._pushChrome();
  assert.equal(chrome().view, 'all-audio', '本地视图要覆盖扩展视图，侧栏不能同时亮两个');
});

await check('壳下发 omnibox:view 的扩展视图走同一条上报路径', async () => {
  const { app, chrome } = setup();
  app._applyHostView('ncm-my-playlists');
  await Promise.resolve();
  app._pushChrome();
  assert.equal(app.currentView, 'ncm-my-playlists');
  assert.equal(chrome().view, 'ncm-my-playlists');
});

// ============================================================
// 3. 二级页：详情不亮主导航，歌单详情亮动态歌单项
// ============================================================

await check('专辑详情与网易云歌单详情上报空串（取消导航高亮）', async () => {
  const { app, chrome } = setup();
  await app.openAlbum('alb:1', 'audio');
  app._pushChrome();
  assert.equal(chrome().view, '', '专辑详情是二级页，不应高亮任何导航项');

  app.currentView = 'ncm-playlist-detail';
  app._pushChrome();
  assert.equal(chrome().view, '');
});

await check('本地歌单详情上报 playlist:<id>（壳据此高亮动态歌单项）', async () => {
  const { app, chrome } = setup();
  await app.openPlaylist('p2');
  app._pushChrome();
  assert.equal(chrome().view, 'playlist:p2');

  app.currentPlaylist = null;
  app._pushChrome();
  assert.equal(chrome().view, '', '歌单已不存在时不要上报一个幽灵高亮');
});

// ============================================================
// 4. 动态导航组（chrome.nav）
// ============================================================

await check('chrome.nav 上报「我的歌单」整组，末尾固定「新建歌单」', () => {
  const { app, chrome } = setup();
  app._chromeViewId = () => 'recent';
  app._pushChrome();
  const nav = chrome().nav;
  assert.ok(nav && nav.playlists && Array.isArray(nav.playlists.items), '要上报 playlists 组');
  assert.deepEqual(nav.playlists.items.map(i => i.id), ['playlist:p1', 'playlist:p2', 'playlist-new']);
  assert.equal(nav.playlists.items[0].badge, '1', '有曲目时带数量徽标');
  assert.equal(nav.playlists.items[2].label, '新建歌单');
});

console.log(failed ? `\n${failed} 例失败` : `\nmedia-player chrome.view 契约 ${total} 例通过`);
process.exit(failed ? 1 : 0);
