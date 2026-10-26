// 媒体播放器「网易云 → 本地」的前端逻辑无头验证（由 test_media_player_netease_local_js.py 调用）。
//
// 两件事在这里锁住：
//
// 1. **「我的歌单」一直显示"暂无推荐歌单"的根因**：`ncm-my-playlists` 用
//    `if (cached && cached.results)` 判缓存命中，而 `[]` 在 JS 里是**真值** —— 只要
//    第一次进入该视图时接口失败（典型场景：还没登录，而推荐歌单是公开接口所以照常
//    有数据），空结果就被写进 localStorage，之后即使登录成功也永远走缓存分支，列表
//    永远是空的，且空状态文案还是推荐歌单的。这里断言：失败不缓存、失败要说原因、
//    重新进入能拿到数据、空列表用「我的歌单」自己的文案。
//
// 2. **匹配与两个入口**：匹配函数（utils.js）的判定顺序，以及「登录页导入喜欢」
//    「歌单详情重建为本地歌单」的调用参数（幂等导入、同名镜像歌单覆盖而不是新建）。
//
// 断言一律用 JSON 比较：被测代码在 vm 上下文里跑，跨 realm 的数组原型不同，
// deepStrictEqual 会以"原型不同"失败。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../plugins/media-player/frontend');
// 真实 iframe 由 file_server 的 _PLUGIN_BOOTSTRAP_SCRIPT 在插件脚本之前注入
// /shell/content-ui.js；夹具必须装载同一份文件，否则渲染路径与线上不一致。
const CONTENT_UI = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../shell/frontend/public/shell/content-ui.js');

const scripts = ['js/utils.js', 'js/app.js', 'js/app-views.js', 'js/app-render.js'];

const results = [];
let failures = 0;

function sameJson(actual, expected) {
    assert.equal(JSON.stringify(actual), JSON.stringify(expected));
}

async function check(name, fn) {
    try {
        await fn();
        results.push(`  PASS  ${name}`);
    } catch (err) {
        failures += 1;
        results.push(`  FAIL  ${name}  — ${err && err.message ? err.message : err}`);
    }
}

// ---- 环境替身：结构走 dom_stub，ContentUI 用壳里的真实文件 ----
// 这里不手写第 6 份 DOM 替身：ContentUI 会建真实子节点（grid/list/row/card），
// 用 tests/js/dom_stub.mjs 才能让 `querySelector('.cui-media')`、appendChild 与
// 事件登记按线上语义工作；innerHTML 只做 id 登记这一条已知限制也由它说明。
function serialize(node) {
    if (!node || typeof node !== 'object') return '';
    const tag = node.tagName || 'div';
    const attrs = [];
    if (node.id) attrs.push(` id="${node.id}"`);
    if (node.className) attrs.push(` class="${node.className}"`);
    for (const key of Object.keys(node.dataset || {})) {
        attrs.push(` data-${key.replace(/[A-Z]/g, ch => '-' + ch.toLowerCase())}="${node.dataset[key]}"`);
    }
    const children = (node.children || []).map(serialize).join('');
    const inner = (node.__html || '') + children;
    const text = inner ? inner : String(node.textContent == null ? '' : node.textContent);
    return `<${tag}${attrs.join('')}>${text}</${tag}>`;
}

function makeHarness() {
    const { document, makeEl } = createDom();
    const storage = new Map();
    const state = { calls: [], toasts: [], plugin: {}, api: {} };
    const element = (id) => {
        const found = document.getElementById(id);
        if (found) return found;
        const created = makeEl('div');
        created.id = id;
        document.body.appendChild(created);
        return created;
    };
    // 页面里写死的容器（内容根与几个勾选框）按需登记；其余 id 由 innerHTML 赋值时登记。
    const documentShim = {
        ...document,
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
    // 浏览器的 window 就是全局对象：content-ui.js 用 `window.ContentUI = ...` 暴露裸
    // 全局 ContentUI，夹具只有让 window 指向上下文本身才能复现这一点。
    sandbox.window = sandbox;

    const ctx = vm.createContext(sandbox);
    for (const rel of scripts) {
        vm.runInContext(fs.readFileSync(path.join(FRONTEND, rel), 'utf8'), ctx, { filename: rel });
    }
    vm.runInContext(fs.readFileSync(CONTENT_UI, 'utf8'), ctx, { filename: 'shell/content-ui.js' });
    return {
        ctx,
        state,
        storage,
        element,
        document: documentShim,
        content: () => serialize(documentShim.getElementById('media-content')),
        app: () => vm.runInContext('new MediaPlayerApp()', ctx),
        setGlobals: (values) => Object.assign(sandbox, values),
    };
}

// ============================================================
// 1. 匹配函数（纯函数）
// ============================================================
const LOCAL = [
    { id: 'l1', title: '夜に駆ける', artist: 'YOASOBI', duration: 261 },
    { id: 'l2', title: '群青', artist: 'YOASOBI', duration: 244 },
    { id: 'l3', title: 'Lemon', artist: '米津玄師', duration: 256 },
    { id: 'l4', title: '同名曲', artist: '甲', duration: 100 },
    { id: 'l5', title: '同名曲', artist: '乙', duration: 200 },
];

{
    const h = makeHarness();
    const match = (songs) => {
        h.setGlobals({ NCM_SONGS: songs, LOCAL_ITEMS: LOCAL });
        return vm.runInContext('MPUtils.matchNeteaseToLocal(NCM_SONGS, LOCAL_ITEMS)', h.ctx);
    };

    await check('歌名 + 歌手一致即命中（忽略大小写与全半角）', () => {
        const r = match([{ name: '夜に駆ける', artists: ['ＹＯＡＳＯＢＩ'], duration: 260000 }]);
        sameJson(r.ids, ['l1']);
        assert.equal(r.matched, 1);
        assert.equal(r.missed, 0);
    });

    await check('歌手写法不同（本地 "A/B"）时按时长 ±3s 命中', () => {
        const r = match([{ name: '群青', artists: ['A', 'B'], duration: 246000 }]);
        sameJson(r.ids, ['l2']);
    });

    await check('歌名唯一时即使歌手与时长都对不上也接受', () => {
        const r = match([{ name: 'Lemon', artists: ['某某'], duration: 999000 }]);
        sameJson(r.ids, ['l3']);
    });

    await check('同名多候选：歌手对得上的那条胜出，不串台', () => {
        const r = match([{ name: '同名曲', artists: ['乙'], duration: 0 }]);
        sameJson(r.ids, ['l5']);
    });

    await check('同名多候选且无法区分时宁缺勿错配', () => {
        const r = match([{ name: '同名曲', artists: ['丙'], duration: 0 }]);
        sameJson(r.ids, []);
        assert.equal(r.missed, 1);
    });

    await check('本地没有的歌名算缺失，且保持在线顺序', () => {
        const r = match([
            { name: '不存在的歌', artists: ['x'], duration: 1 },
            { name: 'Lemon', artists: ['米津玄師'], duration: 256000 },
            { name: '夜に駆ける', artists: ['YOASOBI'], duration: 261000 },
        ]);
        sameJson(r.ids, ['l3', 'l1']);
        assert.equal(r.matched, 2);
        assert.equal(r.missed, 1);
        sameJson(r.missedSongs.map(s => s.name), ['不存在的歌']);
    });

    await check('同一条本地曲目不会被两首在线曲目重复认领', () => {
        const r = match([
            { name: 'Lemon', artists: ['米津玄師'], duration: 256000 },
            { name: 'Lemon', artists: ['米津玄師'], duration: 256000 },
        ]);
        sameJson(r.ids, ['l3']);
        assert.equal(r.missed, 1);
    });
}

// ============================================================
// 2. 「我的歌单」空状态与缓存
// ============================================================
{
    const h = makeHarness();
    const app = h.app();
    app.currentView = 'ncm-my-playlists';
    h.state.plugin.get_status = () => ({ ncm_cli_available: true });

    await check('接口失败（未登录）时给出失败原因且不写缓存', async () => {
        h.state.plugin.get_created_playlists = () => ({ success: false, results: [], error: '未登录' });
        h.state.plugin.get_collected_playlists = () => ({ success: false, results: [], error: '未登录' });
        await app._loadCurrentView();
        // 统一状态机（ec71794）：未登录是前置条件，走 login 态而不是笼统的「加载失败」；
        // 仍然必须说清原因、给出重试入口，而不是把空列表当成功缓存下来。
        assert.match(h.content(), /data-ncm-state="login"/);
        assert.match(h.content(), /尚未登录网易云音乐/);
        assert.match(h.content(), /未登录/);
        assert.match(h.content(), /id="ncm-retry-btn"/);
        assert.doesNotMatch(h.content(), /data-ncm-state="empty"/);
        assert.equal(h.storage.get('ncmCache_ncm-my-playlists'), undefined);
    });

    await check('登录后重新进入能拿到歌单（空缓存不再卡住视图）', async () => {
        h.state.plugin.get_created_playlists = () => ({
            success: true,
            results: [
                { id: 'p1', name: '创建的歌单', track_count: 3, play_count: 0 },
                { id: 'p2', name: '另一个', track_count: 1, play_count: 0 },
            ],
        });
        h.state.plugin.get_collected_playlists = () => ({
            success: true,
            results: [
                { id: 'p3', name: '收藏的歌单', track_count: 9, play_count: 5 },
                { id: 'p1', name: '创建的歌单', track_count: 3, play_count: 0 },
            ],
        });
        await app._loadCurrentView();
        assert.match(h.content(), /创建的歌单/);
        assert.match(h.content(), /收藏的歌单/);
        assert.equal(app._currentListData.length, 3, '创建 + 收藏按 id 去重后应有三条');
        assert.ok(h.storage.get('ncmCache_ncm-my-playlists'), '非空结果应写入缓存');
    });

    await check('我的歌单分段显示，创建在前、收藏在后，各带数量', async () => {
        sameJson(app._currentListData.map(p => p.origin), ['created', 'created', 'collected']);
        sameJson(app._currentListData.map(p => p.id), ['p1', 'p2', 'p3']);
        assert.match(h.content(), /mp-list-group/);
        assert.match(h.content(), /创建的歌单 · 2/);
        assert.match(h.content(), /收藏的歌单 · 1/);
        // 段头不能带 data-idx，否则行索引会错位
        assert.doesNotMatch(h.content(), /mp-list-group[^>]*data-idx/);
    });

    await check('旧缓存（没有 origin 字段）不认，重新拉取后才有分段', async () => {
        // 上一版本写进 localStorage 的缓存没有 origin；认了会渲染成不分段的平表
        h.storage.set('ncmCache_ncm-my-playlists', JSON.stringify({
            results: [{ id: 'old', name: '旧缓存歌单', track_count: 1, play_count: 0 }],
        }));
        h.state.calls.length = 0;
        await app._loadCurrentView();
        assert.equal(h.state.calls.some(c => c.method === 'get_created_playlists'), true,
            '旧缓存应被忽略并重新拉取');
        assert.match(h.content(), /创建的歌单 · 2/);
        assert.doesNotMatch(h.content(), /旧缓存歌单/);
    });

    await check('单边失败时显示已有结果，但不把不完整列表写进缓存', async () => {
        // 「创建成功 + 收藏失败」渲染已有的一半是合理的（总比整页错误强），但把这份
        // 不完整列表写进缓存，会让缺的那半边在后续进入（甚至重启后）一直消失 ——
        // 与「失败不写缓存」是同一条不变量，只是失败藏在单边里。
        const h6 = makeHarness();
        const app6 = h6.app();
        app6.currentView = 'ncm-my-playlists';
        h6.state.plugin.get_status = () => ({ ncm_cli_available: true });
        h6.state.plugin.get_created_playlists = () => ({
            success: true, results: [{ id: 'p1', name: '创建的歌单', track_count: 1 }],
        });
        h6.state.plugin.get_collected_playlists = () => ({ success: false, results: [], error: '网络超时' });
        await app6._loadCurrentView();
        assert.match(h6.content(), /创建的歌单/, '已有的一半仍要显示');
        assert.equal(h6.storage.get('ncmCache_ncm-my-playlists'), undefined,
            '单边失败时不能缓存不完整列表');

        h6.state.plugin.get_collected_playlists = () => ({ success: true, results: [{ id: 'p2', name: '收藏的新歌单' }] });
        await app6._loadCurrentView();
        assert.ok(app6._currentListData.some(p => p.id === 'p2'),
            '恢复后要重新拉取，而不是继续读不完整缓存');
        assert.match(h6.content(), /收藏的新歌单/);
    });

    await check('「我的歌单」缓存超过 2h 必须重新请求，而不是直接渲染旧列表', async () => {
        // 缓存只会由本机写入；别的设备增删歌单后本机看不到，必须靠 TTL 兜底。
        const h7 = makeHarness();
        const app7 = h7.app();
        app7.currentView = 'ncm-my-playlists';
        h7.state.plugin.get_status = () => ({ ncm_cli_available: true });
        h7.storage.set('ncmCache_ncm-my-playlists', JSON.stringify({
            v: 2,
            ts: Date.now() - 2 * 60 * 60 * 1000 - 1000,
            results: [{ id: 'old', name: '旧缓存歌单', track_count: 1, origin: 'created' }],
        }));
        h7.state.calls.length = 0;
        h7.state.plugin.get_created_playlists = () => ({
            success: true, results: [{ id: 'p1', name: '创建的歌单', track_count: 2 }],
        });
        h7.state.plugin.get_collected_playlists = () => ({ success: true, results: [] });

        await app7._loadCurrentView();

        assert.equal(h7.state.calls.some(c => c.method === 'get_created_playlists'), true,
            '过期缓存必须重新请求接口');
        assert.doesNotMatch(h7.content(), /旧缓存歌单/);
        assert.match(h7.content(), /创建的歌单/);
        const saved = JSON.parse(h7.storage.get('ncmCache_ncm-my-playlists'));
        assert.ok(Number.isFinite(saved.ts) && Date.now() - saved.ts < 60 * 1000,
            '重新拉取后要写入新的 ts，供下一次判断过期');
    });

    await check('「我的歌单」v2 缓存缺 ts（旧版本写入）按过期处理', async () => {
        const h8 = makeHarness();
        const app8 = h8.app();
        app8.currentView = 'ncm-my-playlists';
        h8.state.plugin.get_status = () => ({ ncm_cli_available: true });
        h8.storage.set('ncmCache_ncm-my-playlists', JSON.stringify({
            v: 2,
            results: [{ id: 'old', name: '无 ts 缓存歌单', track_count: 1, origin: 'created' }],
        }));
        h8.state.calls.length = 0;
        h8.state.plugin.get_created_playlists = () => ({ success: true, results: [] });
        h8.state.plugin.get_collected_playlists = () => ({ success: true, results: [] });

        await app8._loadCurrentView();

        assert.equal(h8.state.calls.some(c => c.method === 'get_created_playlists'), true,
            '没有时间戳的缓存无法判断新鲜度，必须重拉');
        assert.doesNotMatch(h8.content(), /无 ts 缓存歌单/);
    });

    await check('「我的歌单」缓存 2h 内仍直接用缓存，不重复请求', async () => {
        const h9 = makeHarness();
        const app9 = h9.app();
        app9.currentView = 'ncm-my-playlists';
        h9.state.plugin.get_status = () => ({ ncm_cli_available: true });
        h9.storage.set('ncmCache_ncm-my-playlists', JSON.stringify({
            v: 2,
            ts: Date.now() - 60 * 1000,
            results: [{ id: 'c1', name: '缓存歌单', track_count: 1, origin: 'created' }],
        }));
        h9.state.calls.length = 0;

        await app9._loadCurrentView();

        assert.equal(h9.state.calls.some(c => c.method === 'get_created_playlists'), false,
            '未过期时不该重新请求（TTL 不是禁用缓存）');
        assert.match(h9.content(), /缓存歌单/);
    });

    await check('空列表不再回落到「暂无推荐歌单」文案（方法不做死兜底）', () => {
        // 5 个调用点都在空列表时提前渲染 empty/search；_renderNeteasePlaylists 自己
        // 再兜底只会把「推荐歌单」文案带回「我的歌单」。删除兜底后空数组只建空列表容器。
        const h10 = makeHarness();
        const app10 = h10.app();
        app10._renderNeteasePlaylists([]);
        assert.doesNotMatch(h10.content(), /暂无推荐歌单/);
        app10._renderNeteasePlaylists(undefined);
        assert.doesNotMatch(h10.content(), /暂无推荐歌单/);
    });

    await check('真的没有歌单时用「我的歌单」的文案，而不是推荐歌单的', async () => {
        h.storage.delete('ncmCache_ncm-my-playlists');
        h.state.plugin.get_created_playlists = () => ({ success: true, results: [] });
        h.state.plugin.get_collected_playlists = () => ({ success: true, results: [] });
        await app._loadCurrentView();
        // 空态（接口成功但无数据）与错误态互斥：不给重试按钮。
        assert.match(h.content(), /data-ncm-state="empty"/);
        assert.match(h.content(), /还没有创建或收藏的歌单/);
        assert.doesNotMatch(h.content(), /暂无推荐歌单/);
        assert.doesNotMatch(h.content(), /ncm-retry-btn/);
    });
}

// ============================================================
// 2b. 网易云统一状态机：六个分支互斥，错误态可重试、空态/搜索无结果不加重试
// ============================================================
{
    const stateCount = (html) => (html.match(/data-ncm-state="/g) || []).length;

    const h = makeHarness();
    const app = h.app();
    app.currentView = 'ncm-my-playlists';
    h.state.plugin.get_status = () => ({ ncm_cli_available: true });

    await check('通用接口错误：error 态带 role=alert、原始原因与重试', async () => {
        h.state.plugin.get_created_playlists = () => ({ success: false, results: [], error: '网络超时' });
        h.state.plugin.get_collected_playlists = () => ({ success: false, results: [], error: '网络超时' });
        await app._loadCurrentView();
        const html = h.content();
        assert.match(html, /data-ncm-state="error"/);
        assert.match(html, /empty-state--error/);
        assert.match(html, /role="alert"/);
        assert.match(html, /网易云数据加载失败/);
        assert.match(html, /网络超时/);
        assert.match(html, /id="ncm-retry-btn"/);
        assert.equal(stateCount(html), 1, '一次渲染只能有一个状态标记');
    });

    await check('未登录：login 态与 error 态互斥（不是空白也不是加载失败）', async () => {
        h.state.plugin.get_created_playlists = () => ({ success: false, results: [], error: '未登录' });
        h.state.plugin.get_collected_playlists = () => ({ success: false, results: [], error: '未登录' });
        await app._loadCurrentView();
        const html = h.content();
        assert.match(html, /data-ncm-state="login"/);
        assert.doesNotMatch(html, /data-ncm-state="error"/);
        assert.match(html, /role="status"/);
        assert.equal(stateCount(html), 1);
    });

    await check('API key 未配置：api-key 态给出配置原因且不贴 CLI 命令', async () => {
        h.state.plugin.get_created_playlists = () => ({ success: false, results: [], error: 'API key 未设置，执行 npm install -g @music163/ncm-cli 后 config set' });
        h.state.plugin.get_collected_playlists = () => ({ success: false, results: [], error: 'API key 未设置' });
        await app._loadCurrentView();
        const html = h.content();
        assert.match(html, /data-ncm-state="api-key"/);
        assert.match(html, /网易云 API 未配置/);
        assert.doesNotMatch(html, /npm install|ncm-cli|config set/, '状态页不得回显安装/配置原始指令');
        assert.match(html, /id="ncm-retry-btn"/);
    });

    await check('ncm-cli 缺失：cli-missing 态用自己的重试文案，不回显安装命令', () => {
        app._renderNeteaseCliMissing({ error: 'ncm-cli 未安装\nnpm install -g @music163/ncm-cli' });
        const html = h.content();
        assert.match(html, /data-ncm-state="cli-missing"/);
        assert.match(html, /网易云组件不可用/);
        assert.match(html, /重新检测/);
        assert.doesNotMatch(html, /npm install|ncm-cli/);
    });

    await check('搜索无结果：search 态不加重试', async () => {
        app.currentView = 'ncm-playlists';
        app.searchQuery = '找不到';
        h.state.plugin.search_playlist = () => ({ success: true, results: [] });
        await app._loadCurrentView();
        const html = h.content();
        assert.match(html, /data-ncm-state="search"/);
        assert.match(html, /没有匹配的歌单/);
        assert.doesNotMatch(html, /ncm-retry-btn/);
        assert.equal(stateCount(html), 1);
    });

    await check('搜索接口失败：error 态带重试，而不是当成 0 个结果', async () => {
        app.currentView = 'ncm-playlists';
        app.searchQuery = '报错';
        h.state.plugin.search_playlist = () => ({ success: false, results: [], error: '接口 500' });
        await app._loadCurrentView();
        const html = h.content();
        assert.match(html, /data-ncm-state="error"/);
        assert.match(html, /接口 500/);
        assert.match(html, /id="ncm-retry-btn"/);
    });

    await check('二级页错误态的重试回调由调用方接管', () => {
        let retried = 0;
        app._renderNcmError('网络超时', () => { retried += 1; });
        h.element('ncm-retry-btn').click();
        assert.equal(retried, 1, '重试按钮应调用传入的回调，而不是回到一级视图加载');
    });
}

// ============================================================
// 3. 登录页导入喜欢 / 歌单重建
// ============================================================
{
    const h = makeHarness();
    const app = h.app();
    h.state.plugin.get_status = () => ({ ncm_cli_available: true });
    h.state.plugin.check_login = () => ({ success: true });
    const local = [
        { id: 'l1', title: '夜に駆ける', artist: 'YOASOBI', duration: 261 },
        { id: 'l2', title: '群青', artist: 'YOASOBI', duration: 244 },
    ];

    await check('登录页在已登录时提供「导入喜欢」入口与全量同步', async () => {
        app.currentView = 'ncm-login';
        await app._loadCurrentView();
        assert.match(h.content(), /导入「喜欢」到我的喜欢/);
        assert.match(h.content(), /全量同步所有歌单/);
        assert.match(h.content(), /ncm-export-missing/);
    });

    await check('导入喜欢只把本地命中的曲目写进我的喜欢（幂等接口）', async () => {
        h.state.plugin.get_liked_songs = () => ({
            success: true,
            results: [
                { name: '夜に駆ける', artists: ['YOASOBI'], duration: 261000 },
                { name: '本地没有的歌', artists: ['x'], duration: 1 },
            ],
        });
        h.state.api.media_all_audio = () => local;
        h.state.api.media_add_favorites = (ids) => ({ success: true, added: ids.length });
        await app._importNeteaseLiked();

        const call = h.state.calls.find(c => c.method === 'media_add_favorites');
        assert.ok(call, '未调用 media_add_favorites');
        sameJson(call.args[0], ['l1']);
        assert.ok(app.favIds.has('l1'));
        const toast = h.state.toasts.find(t => t.level === 'success');
        assert.match(toast.text, /已加入 1 首/);
        assert.match(toast.text, /缺失 1 首/);
    });

    await check('重建本地歌单：新建镜像歌单并带来源前缀', async () => {
        app.currentView = 'ncm-playlist-detail';
        app.playlists = { playlists: [], load: async () => { } };
        h.state.plugin.get_playlist_tracks = () => ({
            success: true,
            results: [
                { name: '群青', artists: ['YOASOBI'], duration: 244000 },
                { name: '本地没有的歌', artists: ['x'], duration: 1 },
            ],
        });
        h.state.api.media_all_audio = () => local;
        h.state.api.media_playlist_save = () => ({ success: true });

        await app._rebuildLocalPlaylistFromNetease({ id: 'p1', name: '测试歌单' });

        const call = h.state.calls.filter(c => c.method === 'media_playlist_save').pop();
        sameJson(call.args, ['网易云 · 测试歌单', '', ['l2']]);
        assert.match(h.state.toasts.at(-1).text, /已创建本地歌单「网易云 · 测试歌单」：命中 1 首，本地缺失 1 首/);
    });

    await check('重建本地歌单：同名镜像歌单走覆盖而不是新建', async () => {
        app.playlists = { playlists: [{ id: 'pl-mirror', name: '网易云 · 测试歌单' }], load: async () => { } };
        await app._rebuildLocalPlaylistFromNetease({ id: 'p1', name: '测试歌单' });
        const call = h.state.calls.filter(c => c.method === 'media_playlist_save').pop();
        sameJson(call.args, ['网易云 · 测试歌单', 'pl-mirror', ['l2']]);
        assert.match(h.state.toasts.at(-1).text, /已重建本地歌单/);
    });

    await check('歌单详情按来源显示标签（创建 / 收藏 / 未知）', async () => {
        const h5 = makeHarness();
        const app5 = h5.app();
        let header = null;
        app5._renderDetail = (items, head) => { header = head; };
        // 空歌单现在是「歌单为空」空态（ec71794），不再进详情页；给一条曲目走详情路径。
        h5.state.plugin.get_playlist_tracks = () => ({
            success: true,
            results: [{ id: 's1', name: '曲目', artists: ['歌手'], duration: 1000 }],
        });

        await app5.openNeteasePlaylist({ id: 'p1', name: '收藏单', origin: 'collected' });
        assert.equal(header.label, '收藏的歌单');
        await app5.openNeteasePlaylist({ id: 'p2', name: '自建单', origin: 'created' });
        assert.equal(header.label, '创建的歌单');
        await app5.openNeteasePlaylist({ id: 'p3', name: '搜到的' });
        assert.equal(header.label, '歌单');
    });
}

// ============================================================
// 4. 全量同步所有歌单 + 缺失清单导出
// ============================================================
{
    const h = makeHarness();
    const app = h.app();
    const local = [
        { id: 'l1', title: '夜に駆ける', artist: 'YOASOBI', duration: 261 },
        { id: 'l2', title: '群青', artist: 'YOASOBI', duration: 244 },
    ];
    app.playlists = { playlists: [], load: async () => { } };
    h.state.api.media_all_audio = () => local;
    h.state.api.media_playlist_save = () => ({ success: true });
    h.state.api.media_export_missing = (lines) => ({ success: true, path: 'D:\\音频\\音乐\\网易云缺失曲目.txt', count: lines.length });
    h.state.plugin.get_created_playlists = () => ({
        success: true,
        results: [{ id: 'p1', name: '歌单一' }, { id: 'p2', name: '歌单二' }],
    });
    h.state.plugin.get_collected_playlists = () => ({
        success: true,
        results: [{ id: 'p1', name: '歌单一' }, { id: 'p3', name: '歌单三' }],
    });
    h.state.plugin.get_playlist_tracks = (id) => ({
        success: true,
        results: {
            p1: [{ name: '群青', artists: ['YOASOBI'], duration: 244000 },
                { name: '缺A', artists: ['甲'], duration: 1 }],
            p2: [{ name: '夜に駆ける', artists: ['YOASOBI'], duration: 261000 }],
            p3: [{ name: '缺B', artists: ['乙'], duration: 2 }],
        }[id] || [],
    });

    await check('全量同步默认只同步自建歌单，收藏的歌单连拉都不拉', async () => {
        h.element('ncm-export-missing').checked = false;
        h.element('ncm-sync-collected').checked = false;
        h.state.calls.length = 0;
        await app._syncAllNeteasePlaylists();

        const saved = h.state.calls.filter(c => c.method === 'media_playlist_save');
        sameJson(saved.map(c => c.args[0]), ['网易云 · 歌单一', '网易云 · 歌单二']);
        sameJson(saved[0].args[2], ['l2']);
        assert.equal(h.state.calls.some(c => c.method === 'get_collected_playlists'), false,
            '未勾选时不应请求收藏歌单');
        assert.match(h.state.toasts.at(-1).text, /全量同步完成（仅自建歌单）：2 个歌单 → 写入 2 个本地歌单/);
        assert.match(h.state.toasts.at(-1).text, /命中 2 首，本地缺失 1 首/);
        assert.equal(h.state.calls.some(c => c.method === 'media_export_missing'), false,
            '未勾选时不应写缺失清单');
        assert.equal(h.storage.get('ncmSyncCollected'), '0', '开关状态应存成偏好');
    });

    await check('勾选后才同步收藏歌单，并按勾选输出缺失清单', async () => {
        h.element('ncm-sync-collected').checked = true;
        h.element('ncm-export-missing').checked = true;
        h.state.calls.length = 0;
        await app._syncAllNeteasePlaylists();

        const saved = h.state.calls.filter(c => c.method === 'media_playlist_save');
        sameJson(saved.map(c => c.args[0]), ['网易云 · 歌单一', '网易云 · 歌单二']);
        assert.match(h.state.toasts.at(-1).text, /全量同步完成（创建 \+ 收藏歌单）：3 个歌单 → 写入 2 个本地歌单/);
        assert.match(h.state.toasts.at(-1).text, /命中 2 首，本地缺失 2 首/);
        const call = h.state.calls.find(c => c.method === 'media_export_missing');
        assert.ok(call, '未调用 media_export_missing');
        sameJson(call.args[1], '网易云缺失曲目');
        sameJson(call.args[0], [
            '【歌单一】1 首本地缺失', '  甲 - 缺A',
            '【歌单三】1 首本地缺失', '  乙 - 缺B',
        ]);
        assert.match(h.state.toasts.at(-1).text, /缺失清单：D:\\音频\\音乐\\网易云缺失曲目\.txt/);
        assert.equal(h.storage.get('ncmExportMissing'), '1', '勾选状态应存成偏好');
        assert.equal(h.storage.get('ncmSyncCollected'), '1');
    });

    await check('歌单详情重建写自己名字的清单，不覆盖全量清单', async () => {
        const h2 = makeHarness();
        const app2 = h2.app();
        app2.playlists = { playlists: [], load: async () => { } };
        h2.storage.set('ncmExportMissing', '1');
        h2.document.getElementById = (id) => (id === 'ncm-export-missing' ? null : h2.element(id));
        h2.state.api.media_all_audio = () => local;
        h2.state.api.media_playlist_save = () => ({ success: true });
        h2.state.api.media_export_missing = (lines, title) => ({ success: true, path: `X:\\${title}.txt`, count: lines.length });
        h2.state.plugin.get_playlist_tracks = () => ({
            success: true,
            results: [{ name: '缺C', artists: ['丙'], duration: 3 }],
        });

        await app2._rebuildLocalPlaylistFromNetease({ id: 'p9', name: '只有缺失' });

        const call = h2.state.calls.find(c => c.method === 'media_export_missing');
        assert.ok(call, '未调用 media_export_missing');
        assert.equal(call.args[1], '网易云缺失曲目 · 只有缺失');
        sameJson(call.args[0], ['【只有缺失】1 首本地缺失', '  丙 - 缺C']);
        assert.match(h2.state.toasts.at(-1).text, /本地媒体库中没有可匹配的曲目；缺失清单：X:\\网易云缺失曲目 · 只有缺失\.txt/);
    });

    await check('重复全量同步：没有缺失时也重写清单（不留旧内容）', async () => {
        // 全部命中 → 缺失 0 首，仍要写清单，否则补齐后旧的缺失清单还挂着
        h.state.plugin.get_created_playlists = () => ({ success: true, results: [{ id: 'p1', name: '歌单一' }] });
        h.state.plugin.get_playlist_tracks = () => ({
            success: true,
            results: [{ name: '群青', artists: ['YOASOBI'], duration: 244000 }],
        });
        h.element('ncm-sync-collected').checked = false;
        h.element('ncm-export-missing').checked = true;
        h.state.calls.length = 0;

        await app._syncAllNeteasePlaylists();

        const call = h.state.calls.find(c => c.method === 'media_export_missing');
        assert.ok(call, '没有缺失时也应写清单');
        sameJson(call.args[0], []);
        assert.equal(call.args[1], '网易云缺失曲目');
        assert.match(h.state.toasts.at(-1).text, /命中 1 首，本地缺失 0 首/);
        assert.match(h.state.toasts.at(-1).text, /缺失清单：D:\\音频\\音乐\\网易云缺失曲目\.txt/);
    });

    await check('缺失清单开关：勾选框缺席时读存档偏好，默认开', () => {
        const h3 = makeHarness();
        const app3 = h3.app();
        h3.document.getElementById = (id) => (id === 'ncm-export-missing' ? null : h3.element(id));
        assert.equal(app3._exportMissingEnabled(), true, '默认应为开');
        h3.storage.set('ncmExportMissing', '0');
        assert.equal(app3._exportMissingEnabled(), false);
        h3.storage.set('ncmExportMissing', '1');
        assert.equal(app3._exportMissingEnabled(), true);
    });

    await check('同步收藏歌单开关：勾选框缺席时读存档偏好，默认关', () => {
        const h4 = makeHarness();
        const app4 = h4.app();
        h4.document.getElementById = (id) => (id === 'ncm-sync-collected' ? null : h4.element(id));
        assert.equal(app4._syncCollectedEnabled(), false, '默认应为关');
        h4.storage.set('ncmSyncCollected', '1');
        assert.equal(app4._syncCollectedEnabled(), true);
        h4.storage.set('ncmSyncCollected', '0');
        assert.equal(app4._syncCollectedEnabled(), false);
    });
}

for (const result of results) console.log(result);
console.log(`  共 ${results.length} 项，失败 ${failures} 项`);
process.exitCode = failures ? 1 : 0;
