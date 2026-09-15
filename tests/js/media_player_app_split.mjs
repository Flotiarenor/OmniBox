// media-player 前端脚本「装载契约」的无头验证脚本（由 tests/test_media_player_scripts_js.py 调用）。
//
// 为什么需要它：插件前端没有构建步骤，index.html 里的 <script src> 顺序就是唯一的依赖
// 声明；而 tools/check_plugins.py 的 _check_frontend_assets 只校验"文件存在且不越界"，
// 既不校验顺序，也不校验某个磁盘上的脚本是否真的被引用。于是下面三类改动在门禁里完全
// 看不见，只会在运行时表现成 `X is not defined` 或某个方法凭空消失：
//
//   1. 新增的分片没加进 index.html（→ 分片里的方法全部不存在）；
//   2. 分片顺序错（→ 装载期 ReferenceError）；
//   3. 方法搬走后原文件里的副本没删（→ 静默覆盖，两个实现同时存在）。
//
// 本脚本按 index.html 的**声明顺序**在 vm 里装载全部本地脚本，然后逐条断言上述风险。
// 它同时是"把 1993 行的 app.js 按分节注释拆成多个分片"这件事的护栏：拆分前后都应通过，
// 而且**新增分片只要出现在 index.html 里就会被自动纳入检查**（第 2、6 条会强制如此），
// 不需要改这个文件。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../plugins/media-player/frontend');
const JS_DIR = path.join(FRONTEND, 'js');
const INDEX = path.join(FRONTEND, 'index.html');

let failures = 0;
function check(name, ok, extra = '') {
    if (ok) {
        console.log(`  PASS  ${name}`);
        return;
    }
    failures += 1;
    console.log(`  FAIL  ${name}${extra ? `  — ${extra}` : ''}`);
}

// ---------------------------------------------------------------------------
// 1. index.html 声明了哪些本地脚本（顺序即依赖顺序）
// ---------------------------------------------------------------------------
const html = fs.readFileSync(INDEX, 'utf8');
const declared = [];
for (const match of html.matchAll(/<script[^>]*\ssrc="([^"]+)"/g)) {
    const src = match[1];
    // 服务端注入的 /shell/*.js、远程脚本、data: 都不在插件自己的文件里
    if (/^(?:\/\/|https?:|\/|data:|#)/.test(src)) continue;
    declared.push(src.split('?')[0].split('#')[0]);
}
check('index.html 声明了本地脚本', declared.length > 0, `declared=${JSON.stringify(declared)}`);

const missingFiles = declared.filter(rel => !fs.existsSync(path.join(FRONTEND, rel)));
check('声明的脚本文件都存在', missingFiles.length === 0, `缺失: ${JSON.stringify(missingFiles)}`);

// js/ 目录下的每个脚本都必须被 index.html 引用 —— 拆分新加的分片忘了挂上就会在这里失败
const onDisk = fs.readdirSync(JS_DIR).filter(f => f.endsWith('.js')).map(f => `js/${f}`);
const orphans = onDisk.filter(rel => !declared.includes(rel));
check('js/ 下没有未被 index.html 引用的孤立脚本', orphans.length === 0,
    `孤立: ${JSON.stringify(orphans)}（新分片必须加进 index.html）`);

// ---------------------------------------------------------------------------
// 2. 按声明顺序在 vm 里装载
// ---------------------------------------------------------------------------
const noop = () => { };
const elementStub = () => ({
    style: { setProperty: noop, removeProperty: noop },
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    dataset: {}, children: [], childNodes: [],
    setAttribute: noop, removeAttribute: noop, getAttribute: () => null,
    appendChild: noop, removeChild: noop, remove: noop, insertBefore: noop,
    addEventListener: noop, removeEventListener: noop,
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }),
    focus: noop, blur: noop, click: noop, scrollIntoView: noop,
    classList_add: noop,
});
const documentStub = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: elementStub,
    createDocumentFragment: elementStub,
    addEventListener: noop,
    removeEventListener: noop,
    documentElement: elementStub(),
    body: elementStub(),
    head: elementStub(),
    hidden: false,
    visibilityState: 'visible',
};

const sandbox = {
    // 经典脚本里 `window.X = ...` 与裸标识符 `X` 在真实浏览器里是同一个东西；
    // 这里 window 只做写入落点，读取裸标识符走 context 里的词法绑定（见下）。
    window: {},
    document: documentStub,
    localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
    Bridge: {
        call: () => Promise.resolve({}),
        callPlugin: () => Promise.resolve({}),
        originalUrl: p => `/file?path=${encodeURIComponent(p)}`,
        thumbUrl: id => `/thumbs/${id}`,
    },
    Toast: { error: noop, info: noop, success: noop },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 0, cancelAnimationFrame: noop,
    MutationObserver: class { observe() { } disconnect() { } },
    IntersectionObserver: class { observe() { } unobserve() { } disconnect() { } },
    ResizeObserver: class { observe() { } unobserve() { } disconnect() { } },
    Audio: function () {
        return { addEventListener: noop, removeEventListener: noop, load: noop, play: () => Promise.resolve(), pause: noop };
    },
    URL, URLSearchParams, Number, Math, JSON, String, Boolean, Object, Array, Promise,
    Map, Set, WeakMap, WeakSet, Date, RegExp, Error, TypeError, Symbol, Proxy, Reflect,
    isFinite, isNaN, NaN, Infinity, undefined,
    parseFloat, parseInt, encodeURIComponent, decodeURIComponent, parseInt,
};
const ctx = vm.createContext(sandbox);

// 取当前 MediaPlayerApp.prototype 上「成员名 -> 成员值」的快照。
// `class MediaPlayerApp {}` 在 vm 里是词法绑定（不是 globalThis 属性），必须回 context 求值；
// app.js 装载之前求值会抛 ReferenceError，此时返回空快照。
function snapshotProto() {
    try {
        return vm.runInContext(
            'Object.fromEntries(Object.getOwnPropertyNames(MediaPlayerApp.prototype)'
            + '.map(n => [n, MediaPlayerApp.prototype[n]]))', ctx);
    } catch {
        return {};
    }
}

const loaded = [];
let loadError = null;
const owner = new Map();        // 成员名 -> 首次定义它的脚本
const overrides = [];           // 成员值被后加载脚本改写的记录

for (const rel of declared) {
    const source = fs.readFileSync(path.join(FRONTEND, rel), 'utf8');
    const before = snapshotProto();
    try {
        vm.runInContext(source, ctx, { filename: rel });
    } catch (err) {
        loadError = `${rel}: ${err && err.message ? err.message : err}`;
        break;
    }
    loaded.push(rel);
    const after = snapshotProto();
    for (const [name, value] of Object.entries(after)) {
        if (name === 'constructor') continue;   // 每个类都有自己的 constructor，不算重复定义
        if (!(name in before)) {
            owner.set(name, rel);
        } else if (before[name] !== value) {
            // 同名成员被重新定义：搬移时"原文件里的副本没删"就是这个形态，
            // 后加载的静默覆盖先加载的，两个实现同时存在。
            overrides.push(`${name}（先由 ${owner.get(name) || '未知脚本'} 定义，被 ${rel} 覆盖）`);
        }
    }
}
check('全部本地脚本按声明顺序装载成功', loadError === null, loadError || '');
if (loadError) {
    console.log(`\n${failures} 项失败`);
    process.exit(1);
}

// ---------------------------------------------------------------------------
// 3. 方法契约：拆分/搬移不得丢方法
// ---------------------------------------------------------------------------
const protoNames = Object.keys(snapshotProto());
check('MediaPlayerApp 已定义', protoNames.length > 0);

// 这份清单是"拆 app.js（1993 行，83 个成员）"时的搬移契约：逐个搬走都必须仍然存在。
// 它是子集断言 —— 新增方法不必更新本文件，只有**丢方法或改名**才会失败。
const REQUIRED_METHODS = [
    '_applyEQPreset', '_bindContentDelegation', '_bindFsMove', '_bindKeyboard',
    '_bindThumbPrefetch', '_bindUI', '_buildEmpty', '_buildEQBands',
    '_clearNeteaseCache', '_clearQueue', '_closePlaylistMenu', '_confirmEQName',
    '_confirmPlaylistModal', '_doScan', '_ensureIndex', '_enterFullscreen',
    '_exitFullscreen', '_findAlbumCover', '_findAlbumName', '_handleRowAction',
    '_hideControls', '_hideEQ', '_highlightRows', '_isSameDay',
    '_isVideoShowing', '_loadCurrentView', '_loadEQPresets', '_ncmCacheGet',
    '_ncmCacheSet', '_neteaseToMediaItem', '_observeThumbImgs', '_onDocumentClick',
    '_onStageClick', '_openAddToPlaylist', '_openPlaylistModal', '_openSettings',
    '_prefetchThumbs', '_prepareNeteaseItems', '_refreshRowState', '_renderAlbums',
    '_renderDetail', '_renderEmpty', '_renderList', '_renderNeteaseCliMissing',
    '_renderNeteaseLogin', '_renderNeteasePlaylists', '_renderQueue', '_resetEQ',
    '_restorePlayback', '_rowHtml', '_scheduleAutoHide', '_setLoading',
    '_showControls', '_stopAutoHide', '_toggleCurrentFavorite', '_toggleEQ',
    '_toggleLyrics', '_toggleQueue', '_toggleVideoMode', '_toggleWideMode',
    '_updateFavButton', '_updateMiniEq', '_updatePlayerCover', '_updateStageBackdrop',
    '_updateStageCover', '_updateStats', '_waitScanDone',
    'init', 'loadExtensions', 'onPlayStateChange', 'onTimeUpdate',
    'onTrackChange', 'openAlbum', 'openNeteasePlaylist', 'openNeteaseView',
    'openPlaylist', 'showPlaylistMenu', 'switchView', 'toggleFullscreen',
    'updatePlayModeUI', 'updateStageLyrics', 'updateVolumeUI',
];
const absent = REQUIRED_METHODS.filter(name => !protoNames.includes(name));
check(`MediaPlayerApp.prototype 上 ${REQUIRED_METHODS.length} 个成员都存在`, absent.length === 0,
    `丢失: ${JSON.stringify(absent)}`);

// ---------------------------------------------------------------------------
// 4. 同名成员不得被两个脚本重复定义
// ---------------------------------------------------------------------------
check('没有成员被两个脚本重复定义', overrides.length === 0, overrides.join('; '));

console.log(`\n装载 ${loaded.length} 个脚本，MediaPlayerApp.prototype 共 ${protoNames.length} 个成员`);
if (failures) {
    console.log(`${failures} 项失败`);
    process.exit(1);
}
console.log('全部通过');
