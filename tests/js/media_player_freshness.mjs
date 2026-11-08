// media-player 统一刷新接入用例。
//
// 守五件事：
//   1. 切回前台（onShow）按全部媒体根同步一次；
//   2. 同步报告真有变化时就地重载，专辑详情 / 歌单详情停在原地（歌单要重读本身，
//      否则渲染的还是进详情那一刻的曲目快照）；
//   3. 报告的零变化与 `skip` 不触发重载；
//   4. 网易云顶栏「刷新」先让壳侧引擎同步网易云插件，再清本页缓存并重拉；
//   5. 封面 URL 优先用后端给的封面文件 mtime（`cover_mtime`），缺字段时退回条目 mtime。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { loadShellScripts } from './shell_scripts.mjs';

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

const escapeHtml = (value) => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

function setup(options = {}) {
  const messages = [];
  const bridgeCalls = [];
  const systemCalls = [];
  const freshnessCalls = [];
  const order = [];
  const lifecycle = { show: [], hide: [], dispose: [] };
  const windowListeners = {};
  const stored = new Map(Object.entries(options.storage || {}));
  const localStorage = {
    getItem: (key) => (stored.has(key) ? stored.get(key) : null),
    setItem: (key, value) => { stored.set(key, String(value)); },
    removeItem: (key) => { stored.delete(key); },
    get length() { return stored.size; },
    key: (index) => Array.from(stored.keys())[index] ?? null,
  };
  const sandbox = {
    console, setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => { },
    localStorage,
    location: { origin: 'http://127.0.0.1:18080' },
    document: { getElementById: () => null, querySelectorAll: () => [], querySelector: () => null, addEventListener: () => { } },
    Utils: { escapeHtml, jsString: (value) => `'${String(value).replace(/'/g, "\\'")}'` },
    Icons: { html: () => '' },
    Toast: { info() { }, error() { }, success() { }, warning() { } },
    Bridge: {
      call(method, ...args) {
        bridgeCalls.push({ method, args });
        const handler = options.handlers && options.handlers[method];
        try {
          return Promise.resolve(handler ? handler(...args) : {});
        } catch (error) {
          return Promise.reject(error);
        }
      },
      callPlugin: async () => ({}),
      callSystem(method, ...args) {
        order.push('sync');
        systemCalls.push({ method, args });
        if (options.systemThrows) return Promise.reject(new Error('引擎不可用'));
        return Promise.resolve({ action: 'sync', added: 0, changed: 0, removed: 0 });
      },
      thumbUrl: (id) => `/thumbs/${encodeURIComponent(id)}`,
      originalUrl: (value) => `/file?path=${encodeURIComponent(value)}`,
    },
    Freshness: {
      sync(plugin, scope) {
        freshnessCalls.push({ plugin, scope });
        const report = options.report ? options.report(plugin, scope) : { action: 'sync', added: 0, changed: 0, removed: 0 };
        return Promise.resolve(report);
      },
    },
    PluginLifecycle: {
      onShow(fn) { lifecycle.show.push(fn); },
      onHide(fn) { lifecycle.hide.push(fn); },
      onDispose(fn) { lifecycle.dispose.push(fn); },
    },
  };
  sandbox.window = sandbox;
  sandbox.parent = { location: { origin: 'http://127.0.0.1:18080' }, postMessage: (payload) => messages.push(payload) };
  sandbox.addEventListener = (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); };
  sandbox.removeEventListener = (type, fn) => {
    const list = windowListeners[type] || [];
    const at = list.indexOf(fn);
    if (at >= 0) list.splice(at, 1);
  };
  vm.createContext(sandbox);
  loadShellScripts(sandbox, ['chrome-client.js']);
  for (const rel of ['utils.js', 'app.js', 'app-views.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, rel), 'utf8'), sandbox, { filename: rel });
  }
  const app = vm.runInContext('new MediaPlayerApp()', sandbox);
  app.playlists = {
    playlists: [],
    currentId: '',
    loads: 0,
    async load() { this.loads += 1; },
    syncNav() { },
    async get(id) { return this.playlists.find(p => p.id === id) || null; },
  };
  const reloads = { view: 0, stats: 0 };
  app._loadCurrentView = async () => { reloads.view += 1; };
  app._updateStats = async () => { reloads.stats += 1; };
  app._pushChrome = () => { };
  app._bindHost();
  return {
    app, messages, bridgeCalls, systemCalls, freshnessCalls, order, lifecycle, reloads,
    stored,
    MPUtils: vm.runInContext('MPUtils', sandbox),
    show() { lifecycle.show.forEach((fn) => fn()); },
    send(data) {
      (windowListeners.message || []).slice().forEach((fn) => fn({ origin: 'http://127.0.0.1:18080', source: sandbox.parent, data }));
    },
    tick: (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms)),
  };
}

const changed = { action: 'sync', added: 1, changed: 0, removed: 0 };

// ============================================================
// 1. onShow 同步全部根
// ============================================================

await check('切回前台按全部媒体根同步一次', async () => {
  const env = setup();
  env.app._bindPluginLifecycle();
  env.app._bindHost();
  env.show();
  await env.tick(10);
  assert.equal(env.freshnessCalls.length, 1, JSON.stringify(env.freshnessCalls));
  assert.equal(env.freshnessCalls[0].plugin, 'media-player');
  assert.equal(env.freshnessCalls[0].scope, '', '媒体根有多个，同步覆盖全部根');
});

await check('报告有变化时重载当前视图，专辑详情保持原地', async () => {
  const env = setup({ report: () => changed });
  env.app.currentView = 'album-detail';
  env.app.currentAlbum = { key: 'alb:1', kind: 'audio' };
  await env.app._syncFreshness(true);
  assert.equal(env.reloads.view, 1, '要有一次当前视图重载');
  assert.equal(env.reloads.stats, 1);
  assert.equal(env.app.currentView, 'album-detail', '不能退回一级视图');
  assert.equal(env.app.currentAlbum.key, 'alb:1');
});

await check('歌单详情刷新时重读歌单本身，丢掉旧曲目快照', async () => {
  const env = setup({ report: () => changed });
  env.app.playlists.playlists = [{ id: 'p1', name: '歌单一', item_ids: ['a', 'b'] }];
  env.app.currentView = 'playlist';
  env.app.currentPlaylist = { id: 'p1', name: '歌单一', items: [{ id: 'a' }] };
  env.app.playlists.currentId = 'p1';
  // 后端此刻的曲目比进详情时多了一首
  env.app.playlists.get = async (id) => ({ id, name: '歌单一', items: [{ id: 'a' }, { id: 'b' }] });
  await env.app._syncFreshness(true);
  assert.equal(env.app.currentPlaylist.items.length, 2, '要渲染后端重读后的曲目');
  assert.equal(env.app.playlists.currentId, 'p1', '歌单详情仍高亮同一项');
  assert.equal(env.app.currentView, 'playlist');
});

// ============================================================
// 2. 零变化 / skip / 别的插件
// ============================================================

await check('零变化报告不重载', async () => {
  const env = setup({ report: () => ({ action: 'sync', added: 0, changed: 0, removed: 0 }) });
  await env.app._syncFreshness(true);
  assert.equal(env.reloads.view, 0);
});

await check('skip 不重载', async () => {
  const env = setup({ report: () => ({ action: 'skip', reason: 'throttled' }) });
  env.send({ type: 'omnibox:freshness', plugin: 'media-player', scope: '', report: { action: 'skip', reason: 'throttled' } });
  await env.tick(10);
  assert.equal(env.reloads.view, 0);
});

await check('omnibox:freshness 有变化时重载，别的插件报告忽略', async () => {
  const env = setup();
  env.app.currentView = 'all-audio';
  env.send({ type: 'omnibox:freshness', plugin: 'other-plugin', scope: '', report: changed });
  await env.tick(10);
  assert.equal(env.reloads.view, 0, '别的插件的报告不该让本页重载');
  env.send({ type: 'omnibox:freshness', plugin: 'media-player', scope: '', report: changed });
  await env.tick(10);
  assert.equal(env.reloads.view, 1);
  assert.equal(env.app.currentView, 'all-audio', '刷新不改变当前视图');
});

await check('富事件之后紧跟的旧 refresh 动作不重复重载', async () => {
  const env = setup();
  env.app.currentView = 'all-audio';
  env.send({ type: 'omnibox:freshness', plugin: 'media-player', scope: '', report: changed });
  env.send({ type: 'omnibox:action', action: 'refresh' });
  await env.tick(20);
  assert.equal(env.reloads.view, 1, '两条消息属于同一次同步');
});

await check('零变化富事件后的旧 refresh 动作也不重载', async () => {
  const env = setup();
  env.send({ type: 'omnibox:freshness', plugin: 'media-player', scope: '', report: { action: 'sync', added: 0, changed: 0, removed: 0 } });
  env.send({ type: 'omnibox:action', action: 'refresh' });
  await env.tick(20);
  assert.equal(env.reloads.view, 0);
});

await check('旧壳只发 refresh 动作时仍然重载', async () => {
  const env = setup();
  env.app.currentView = 'all-audio';
  env.send({ type: 'omnibox:action', action: 'refresh' });
  await env.tick(20);
  assert.equal(env.reloads.view, 1);
});

// ============================================================
// 3. 网易云顶栏「刷新」
// ============================================================

await check('网易云刷新先同步壳侧引擎，再清缓存并重拉', async () => {
  const env = setup({
    storage: { 'ncmCache_ncm-daily': '{"results":[]}' },
  });
  env.app.currentView = 'ncm-daily';
  env.app._updateActions = () => { };
  env.app._setLoading = () => { };
  env.app._loadCurrentView = async () => { env.order.push('reload'); env.reloads.view += 1; };
  await env.app._doScan();
  assert.equal(env.systemCalls.length, 1, '要调一次壳级同步');
  assert.deepEqual(env.systemCalls[0].method, 'system_freshness_sync');
  assert.deepEqual([...env.systemCalls[0].args], ['netease-music', '']);
  assert.deepEqual(env.order.slice(0, 2), ['sync', 'reload'], `顺序 ${JSON.stringify(env.order)}`);
  assert.equal(env.stored.has('ncmCache_ncm-daily'), false, '本地在线缓存要被清掉');
  assert.equal(env.reloads.view, 1);
});

await check('网易云刷新在引擎同步失败时仍清缓存并重拉', async () => {
  const env = setup({ storage: { 'ncmCache_ncm-daily': '{"results":[]}' }, systemThrows: true });
  env.app.currentView = 'ncm-daily';
  env.app._updateActions = () => { };
  env.app._setLoading = () => { };
  env.app._loadCurrentView = async () => { env.reloads.view += 1; };
  await env.app._doScan();
  assert.equal(env.stored.has('ncmCache_ncm-daily'), false, '本地在线缓存仍要被清掉');
  assert.equal(env.reloads.view, 1, '引擎失败不影响本页重拉');
});

// ============================================================
// 4. 封面版本
// ============================================================

await check('封面 URL 优先用后端 cover_mtime，缺字段退回条目 mtime', () => {
  const env = setup();
  const withCover = env.MPUtils.coverUrl({ id: 'v1', has_cover: true, cover_mtime: 111, mtime: 222 });
  assert.ok(withCover.endsWith('&v=111'), withCover);
  const withoutCover = env.MPUtils.coverUrl({ id: 'v1', has_cover: true, mtime: 222 });
  assert.ok(withoutCover.endsWith('&v=222'), withoutCover);
  const noVersion = env.MPUtils.coverUrl({ id: 'v1', has_cover: true });
  assert.ok(!noVersion.includes('&v='), noVersion);
});

console.log(failed ? `\n${failed} 例失败` : `\nmedia-player 统一刷新用例 ${total} 例通过`);
process.exit(failed ? 1 : 0);
