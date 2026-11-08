// image-viewer 接入统一刷新「触发面」与缩略图版本号的契约用例。
//
// 背景：迁移到壳渲染骨架后，内容页不再持有 `Freshness.mount()` 的控件句柄，也没有任何
// 地方在"切回插件"时调同步 —— 被动同步整条通道无人调用。这里把新接的三件事钉成契约：
//
//   1. `_freshnessScope()`：图片层给当前目录；相册层、子相册层与时间线「按图片」层给空串
//      （全根）—— 时间线那一层的列表来自 `list_recent_images`，不属于任何目录。
//   2. `omnibox:chrome` 带 `freshness_scope`：壳据此记住作用域并作为 `autoSync(scope)`
//      的入参。
//   3. `_syncFreshness(reload)`：调 `Freshness.sync('image-viewer', scope)`；只有报告里
//      确实有 added / changed / removed / invalidated 且 `reload` 为真时才重载；`skip`、
//      零变更、同步异常都不重载。
//
// 另外锁住缩略图 URL 的版本号优先取封面文件自身的 mtime（相册/瓦片），普通图片仍用
// 自己的 mtime：`/thumbs` 带一天强缓存，替换同名封面不改目录 mtime，只有版本号能击穿它。
//
// 端到端那一段（重新可见 → 同步 → 重载相册与当前视图）由 tests/js/image_viewer_lifecycle.mjs
// 的「场景 5」用完整 DOM/Bridge 替身驱动；本文件只测本页自己的判定与上报。
//
// 运行：
//     node tests/js/image_viewer_refresh.mjs
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const FRONTEND = path.join(ROOT, 'plugins/image-viewer/frontend');

let failures = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

// ---------- 最小 DOM ----------
// 本用例只调用不渲染的成员（作用域、chrome 上报、同步判定），所以元素替身给到"能建能挂"
// 就够；需要布局与事件冒泡的路径请走 tests/js/image_viewer_lifecycle.mjs。
function makeElement(tag = '') {
    return {
        tagName: String(tag).toLowerCase(),
        id: '', className: '', textContent: '', innerHTML: '', value: '', checked: false,
        style: { setProperty() { }, removeProperty() { } },
        dataset: {}, children: [], classList: { add() { }, remove() { }, toggle() { } },
        appendChild(child) { this.children.push(child); return child; },
        append(...children) { this.children.push(...children); },
        removeChild() { }, remove() { },
        setAttribute() { }, getAttribute: () => null, removeAttribute() { },
        querySelector: () => null, querySelectorAll: () => [],
        addEventListener() { }, removeEventListener() { },
        getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    };
}

function loadImageViewer() {
    const chromeMessages = [];
    const win = {
        location: { href: 'http://127.0.0.1:18080/plugins/image-viewer/frontend/index.html', origin: 'http://127.0.0.1:18080' },
        addEventListener() { }, removeEventListener() { },
        innerWidth: 1200, innerHeight: 800,
    };
    const parent = {
        location: { origin: 'http://127.0.0.1:18080' },
        postMessage(payload) { chromeMessages.push(payload); },
    };
    win.parent = parent;

    // 统一刷新控件（`/shell/freshness.js`）的替身：T2 落地前内容页只依赖 `sync` 这一条
    // API，所以替身只提供它；报告由各断言自己排队。
    const freshness = {
        syncs: [],
        reports: [],
        async sync(plugin, scope) {
            freshness.syncs.push([plugin, scope]);
            return freshness.reports.shift() || {};
        },
    };
    win.Freshness = freshness;

    const doc = {
        location: win.location,
        body: makeElement('body'),
        documentElement: { getAttribute: () => null, setAttribute() { }, style: { setProperty() { } } },
        getElementById: () => makeElement('div'),
        createElement: (tag) => makeElement(tag),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() { }, removeEventListener() { },
    };
    // 上报出口：真实页面由 /shell/chrome-client.js 提供（先 post 到父窗口）
    const chromeClient = { post(payload) { chromeMessages.push(payload); } };
    win.ChromeClient = chromeClient;
    const sandbox = {
        window: win,
        parent,
        document: doc,
        console,
        Freshness: freshness,
        ChromeClient: chromeClient,
        Bridge: {
            call: () => Promise.resolve({}),
            callSystem: () => Promise.resolve([]),
            thumbUrl: (target) => '/thumbs/' + target,
        },
        Toast: { info() { }, error() { }, success() { }, warning() { } },
        Utils: { escapeHtml: (s) => String(s == null ? '' : s), debounce: (fn) => fn },
        Icons: { html: () => '' },
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        encodeURIComponent, decodeURIComponent, String, Number, Math, JSON, Object, Array,
        Error, Promise, Map, Set, Date, RegExp, parseInt, parseFloat, isNaN, Infinity, console,
    };
    win.Icons = sandbox.Icons;
    vm.createContext(sandbox);
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: `plugins/image-viewer/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__IV = ImageViewer;', sandbox);
    return { win, doc, chromeMessages, freshness, ImageViewer: sandbox.__IV };
}

const lastChrome = (env) => env.chromeMessages.filter((m) => m.type === 'omnibox:chrome').at(-1);

console.log('场景 1：作用域按层取值（图片层当前目录，其余全根）');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();

    iv.mode = 'albums';
    iv.currentPath = '作者A/作品1';
    iv._recentActive = false;
    check('相册层 scope 为空串', iv._freshnessScope() === '', `实际 ${JSON.stringify(iv._freshnessScope())}`);

    iv.mode = 'children';
    check('子相册层 scope 为空串', iv._freshnessScope() === '');

    iv.mode = 'images';
    check('图片层 scope = 当前目录', iv._freshnessScope() === '作者A/作品1',
        `实际 ${JSON.stringify(iv._freshnessScope())}`);

    iv.currentPath = '';
    check('根目录图片层 scope 为空串（全根）', iv._freshnessScope() === '');

    iv.currentPath = '作者A/作品1';
    iv._recentActive = true;                 // 时间线「按图片」层
    check('时间线「按图片」层 scope 为空串', iv._freshnessScope() === '',
        `实际 ${JSON.stringify(iv._freshnessScope())}`);
}

console.log('场景 2：omnibox:chrome 上报 freshness_scope');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();

    iv.mode = 'images';
    iv.currentPath = '作者A/作品1';
    iv._pushChrome();
    check('图片层上报当前目录', lastChrome(env).freshness_scope === '作者A/作品1',
        `实际 ${JSON.stringify(lastChrome(env).freshness_scope)}`);

    iv.mode = 'albums';
    iv._pushChrome();
    check('相册层上报空串', lastChrome(env).freshness_scope === '');
}

console.log('场景 3：_syncFreshness 的调用参数与重载判定');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    let reloads = 0;
    iv._onFreshnessChanged = async () => { reloads += 1; };
    iv.mode = 'images';
    iv.currentPath = '作者A/作品1';

    env.freshness.reports.push({ action: 'sync', added: 3, changed: 1, removed: 0 });
    const report = await iv._syncFreshness(true);
    const call = env.freshness.syncs.at(-1) || [];
    check('调用 Freshness.sync(plugin, scope)',
        call[0] === 'image-viewer' && call[1] === '作者A/作品1',
        `实际 ${JSON.stringify(call)}`);
    check('返回同步报告', !!report && report.added === 3);
    check('有变化且 reload=true → 重载当前视图', reloads === 1, `实际 ${reloads}`);

    env.freshness.reports.push({ action: 'sync', added: 0, changed: 0, removed: 0 });
    reloads = 0;
    await iv._syncFreshness(true);
    check('零变更不重载', reloads === 0, `实际 ${reloads}`);

    env.freshness.reports.push({ action: 'sync', changed: 2 });
    reloads = 0;
    await iv._syncFreshness(false);
    check('reload=false 时只同步不重载', reloads === 0, `实际 ${reloads}`);

    env.freshness.reports.push({ action: 'sync', invalidated: true });
    reloads = 0;
    await iv._syncFreshness(true);
    check('invalidated 也算变化', reloads === 1, `实际 ${reloads}`);

    env.freshness.reports.push({ action: 'skip', reason: 'busy' });
    reloads = 0;
    await iv._syncFreshness(true);
    check('skip 不重载', reloads === 0, `实际 ${reloads}`);

    env.freshness.sync = async () => { throw new Error('boom'); };
    reloads = 0;
    const failed = await iv._syncFreshness(true);
    check('同步异常不外抛、不重载', failed === null && reloads === 0, `实际 ${failed}`);

    delete env.win.Freshness;               // 旧壳：共享运行时里没有 sync
    reloads = 0;
    const legacy = await iv._syncFreshness(true);
    check('没有 Freshness.sync 时静默跳过', legacy === null && reloads === 0);
}

console.log('场景 4：缩略图 URL 的版本号优先取封面文件自身的 mtime');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();

    const cover = iv._thumbUrl({ url: 'albumA/001_p0.png', cover_mtime: 1700000100.4, mtime: 1700000000 });
    check('相册/瓦片封面用 cover_mtime', cover.endsWith('&v=1700000100'), `实际 ${cover}`);

    const image = iv._thumbUrl({ url: 'albumA/001_p1.png', mtime: 1700000000.7 });
    check('普通图片仍用自身 mtime', image.endsWith('&v=1700000001'), `实际 ${image}`);

    const fallback = iv._thumbUrl({ url: 'albumA/001_p0.png', cover_mtime: 0, mtime: 1700000000 });
    check('cover_mtime 为空时回退到 mtime', fallback.endsWith('&v=1700000000'), `实际 ${fallback}`);

    const byCoverField = iv._thumbUrl({ cover: 'albumA/001_p0.png', cover_mtime: 1700000100 });
    check('相册对象只给 cover 字段时同样带版本', byCoverField.endsWith('&v=1700000100'),
        `实际 ${byCoverField}`);

    const noStamp = iv._thumbUrl({ url: 'albumA/001_p0.png' });
    check('没有任何时间戳时不带版本号', !noStamp.includes('&v='), `实际 ${noStamp}`);

    check('没有封面路径时返回空串', iv._thumbUrl({ cover: '' }) === '');
}

console.log(failures === 0
    ? '\nimage-viewer 刷新触发面用例全部通过'
    : `\nimage-viewer 刷新触发面用例失败 ${failures} 项`);
process.exit(failures === 0 ? 0 : 1);
