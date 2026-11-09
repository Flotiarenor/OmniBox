// image-viewer 前端生命周期迁移的无头验证脚本。
//
// 背景：image-viewer 是更新频率最高、监听器最多的插件（实测 addEventListener 35 :
// removeEventListener 0），其幻灯片用 setInterval 每 3 秒换图且从不随后台停止 ——
// 切到别的插件后仍在消耗 CPU 并继续拉取缩略图。
//
// 这里用最小 DOM/Bridge stub 跑真实 app.js，锁住三件事：
//   1. onHide 停掉幻灯片定时器，且不要求"停止播放"语义（这里是相册，无播放）；
//   2. onShow 后从原来的位置继续（不是跳回第一张），用户主动停止则不续播；
//   3. onDispose 摘掉 resize 监听器（监听器可回收，不再是 35 : 0）。
// 幻灯片按钮在壳里（内容页没有 #btn-slideshow），所以启停状态按上报的
// cubby:chrome.actions.slideshow（label / active）断言，而不是读内容页 DOM。
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';
import { loadShellRuntime } from './shell_scripts.mjs';

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

const tick = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));

// ---------- 最小 DOM ----------
function makeElement(id = '') {
    const el = {
        id,
        style: { setProperty() { }, removeProperty() { } },
        dataset: {},
        children: [],
        textContent: '',
        innerHTML: '',
        value: '',
        checked: false,
        classList: { add() { }, remove() { }, toggle() { }, contains: () => false },
        appendChild(child) { this.children.push(child); return child; },
        append(...children) { this.children.push(...children); },
        removeChild() { },
        remove() { },
        setAttribute() { },
        getAttribute: () => null,
        removeAttribute() { },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) { this._listeners = this._listeners || {}; (this._listeners[type] = this._listeners[type] || []).push(fn); },
        removeEventListener() { },
        focus() { },
        blur() { },
        closest: () => null,
        insertBefore() { },
        cloneNode() { return makeElement(id); },
        getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }),
        scrollTo() { },
        offsetHeight: 0,
        offsetWidth: 0,
    };
    return el;
}

function loadImageViewer() {
    const elements = new Map();
    const getEl = (id) => {
        if (!elements.has(id)) elements.set(id, makeElement(id));
        return elements.get(id);
    };
    const winListeners = {};
    const win = {
        location: { href: 'http://127.0.0.1:18080/plugins/image-viewer/frontend/index.html', origin: 'http://127.0.0.1:18080' },
        addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            const arr = winListeners[type] || [];
            const i = arr.indexOf(fn);
            if (i >= 0) arr.splice(i, 1);
        },
        innerWidth: 1200,
        innerHeight: 800,
    };
    const parent = {
        location: { origin: 'http://127.0.0.1:18080' },
        document: { documentElement: { getAttribute: () => null, setAttribute() { } } },
        postMessage(payload) { chromeMessages.push(payload); },
    };
    const chromeMessages = [];
    win.parent = parent;
    const messageListeners = [];
    const doc = {
        location: win.location,
        documentElement: { getAttribute: () => null, setAttribute() { }, style: { setProperty() { } } },
        body: makeElement('body'),
        head: makeElement('head'),
        getElementById: getEl,
        createElement: (tag) => makeElement(tag),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) { if (type === 'message') messageListeners.push(fn); },
        removeEventListener() { },
    };

    // 统一刷新控件（`/shell/freshness.js`）的替身：内容是 `Freshness.sync(plugin, scope)`。
    // 生命周期用例只需要"重新可见会不会同步一次"，报告由每个场景自己排队。
    const freshness = {
        syncs: [],
        reports: [],
        async sync(plugin, scope) {
            freshness.syncs.push([plugin, scope]);
            return freshness.reports.shift() || {};
        },
    };
    const sandbox = {
        window: win,
        parent,
        document: doc,
        console,
        Freshness: freshness,
        Bridge: {
            call: () => Promise.resolve({}),
            callSystem: () => Promise.resolve([]),
            callPlugin: () => Promise.resolve({}),
            originalUrl: (p) => '/file?path=' + encodeURIComponent(p),
            thumbUrl: (p) => '/thumbs/' + p,
            setPrefix() { },
        },
        Toast: { info() { }, error() { }, success() { }, warning() { } },
        Utils: { escapeHtml: (s) => String(s == null ? '' : s), debounce: (fn) => fn },
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        encodeURIComponent, decodeURIComponent, String, Number, Math, JSON, Object, Array, Error, Promise,
        Map, Set, Date, RegExp, parseInt, parseFloat, isNaN, Infinity, console,
    };
    vm.createContext(sandbox);
    // 图标集桩：壳由引导脚本注入 /shell/icons.generated.js 并把 Icons 挂到全局。
    // 沙箱里 `window` 只是普通对象，所以 `window.Icons` 与裸标识符 `Icons` 要各挂一次
    // （插件代码两种写法都有：app-utils.js 用 window.Icons，app-grid.js 用 Icons）。
    // 桩按壳产物的形态给出 `icon:名字` → `<svg class="obx-icon"><use href="#名字"></use></svg>`。
    const iconsStub = {
        html: (name, className) => '<svg class="obx-icon'
            + (className ? ' ' + className : '') + '"><use href="#' + String(name).slice(5) + '"></use></svg>',
    };
    sandbox.Icons = iconsStub;
    win.Icons = iconsStub;
    // 插件代码读 `window.Freshness`，而沙箱里的 `window` 只是普通对象：两处都挂
    win.Freshness = freshness;
    // 先注入 Shell 基础运行时（提供 window.PluginLifecycle），再按 index.html 的声明
    // 顺序装载插件自己的全部脚本 —— 不写死单个文件名，这样 app.js 拆成分片后本用例
    // 自动跟着走（顺序或漏挂出错会在装载期直接抛，见 tests/js/script_load_contract.mjs）。
    loadShellRuntime(sandbox);
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: `plugins/image-viewer/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__IV = ImageViewer; globalThis.__life = window.PluginLifecycle;', sandbox);
    return {
        win, doc, getEl, winListeners, messageListeners, chromeMessages, freshness,
        ImageViewer: sandbox.__IV,
        lifecycle: sandbox.__life,
    };
}

console.log('场景 1：onHide 停掉幻灯片定时器（修复前切走后每 3 秒仍在换图）');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    // 直接构造可见状态：不跑 init（避免异步加载），只验证生命周期钩子行为
    iv.lightbox = { show() { }, hide() { }, navigate() { }, getIndex: () => 4 };
    iv.mode = 'images';
    iv.currentAllImages = [1, 2, 3, 4, 5, 6, 7];
    iv.currentItems = [{ url: 'a.jpg' }];
    iv._bindPluginLifecycle();

    iv.toggleSlideshow();
    check('启动幻灯片后有定时器', iv.slideshowTimer !== null);
    const slideshowAction = () => {
        const chrome = env.chromeMessages.filter(m => m.type === 'cubby:chrome').at(-1);
        return chrome && chrome.actions ? chrome.actions.slideshow : null;
    };
    const stopAction = slideshowAction();
    check('壳顶栏的幻灯片动作变成"停止"',
        !!stopAction && stopAction.active === true && stopAction.label === '停止',
        `实际 ${JSON.stringify(stopAction)}`);

    env.lifecycle.setVisible(false);
    check('onHide 后定时器被清空', iv.slideshowTimer === null);
    check('onHide 记住了播放位置', iv._slideshowResumeIndex === 4, `实际 ${iv._slideshowResumeIndex}`);
    const playAction = slideshowAction();
    check('壳顶栏的幻灯片动作恢复"幻灯片"',
        !!playAction && playAction.active === false && playAction.label === '幻灯片',
        `实际 ${JSON.stringify(playAction)}`);

    env.lifecycle.setVisible(true);
    await tick();
    check('onShow 后从原位置继续（不是第一张）', iv._slideshowResumeIndex === 4);
    check('onShow 后定时器重新启动', iv.slideshowTimer !== null);
    iv._stopSlideshow();
}

console.log('场景 2：用户主动停止后，切回来自动续播必须不发生');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    iv.lightbox = { show() { }, hide() { }, navigate() { }, getIndex: () => 2 };
    iv.mode = 'images';
    iv.currentAllImages = [1, 2, 3, 4];
    iv._bindPluginLifecycle();

    iv.toggleSlideshow();
    iv.toggleSlideshow();              // 用户点"停止"
    check('主动停止后没有定时器', iv.slideshowTimer === null);
    env.lifecycle.setVisible(false);
    env.lifecycle.setVisible(true);
    await tick();
    check('隐藏→显示不自动续播', iv.slideshowTimer === null);
}

console.log('场景 3：onDispose 摘掉 resize 监听器（监听器可回收）');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    iv.lightbox = { show() { }, hide() { }, navigate() { }, getIndex: () => 0 };
    iv.mode = 'images';
    iv.currentAllImages = [1, 2];
    // 模拟 init() 注册 resize 的那一步
    iv._onResize = () => { };
    env.win.addEventListener('resize', iv._onResize);
    const before = (env.winListeners.resize || []).length;
    check('resize 监听器已注册', before === 1, `实际 ${before}`);

    iv._bindPluginLifecycle();
    env.lifecycle.dispose();
    const after = (env.winListeners.resize || []).length;
    check('onDispose 后 resize 监听器被摘掉', after === 0, `实际 ${after}`);
    check('onDispose 后引用置空（不再重复摘）', iv._onResize === null);
    check('onDispose 停掉幻灯片', iv.slideshowTimer === null);
}

console.log('场景 4：未注册生命周期运行时（旧壳）不得抛异常');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    delete env.win.PluginLifecycle;
    let ok = true;
    try {
        iv._bindPluginLifecycle();
    } catch (e) {
        ok = false;
    }
    check('缺少 PluginLifecycle 时静默跳过（旧壳兼容）', ok);
}

console.log('场景 5：重新可见时按当前目录同步一次，有变化才重载');
{
    const env = loadImageViewer();
    const iv = new env.ImageViewer();
    iv.lightbox = { show() { }, hide() { }, navigate() { }, getIndex: () => 3 };
    iv.mode = 'images';
    iv.currentPath = 'albumA';
    iv.currentView = 'albums';
    iv.currentPage = 2;
    iv.currentAllImages = [1, 2, 3];
    iv._slideshowResumeIndex = 1;
    iv._slideshowWanted = true;

    // 重载的落点用替身记录：相册树与当前视图各重取一次
    const reloads = [];
    iv.loadAlbums = async () => { reloads.push('albums'); };
    iv._reloadCurrentView = async () => { reloads.push('view'); };

    iv._bindPluginLifecycle();
    // 注册时生命周期若已可见会立刻补一次（首帧同步），之后的"切回"才是本场景要测的
    env.lifecycle.setVisible(false);
    env.freshness.reports.push({ action: 'sync', added: 2, changed: 0, removed: 0 });
    env.lifecycle.setVisible(true);
    await tick();
    await tick();

    const calls = env.freshness.syncs;
    const last = calls[calls.length - 1];
    check('切回时用当前目录作 scope 同步一次',
        calls.length >= 2 && last[0] === 'image-viewer' && last[1] === 'albumA',
        `实际 ${JSON.stringify(calls)}`);
    check('报告有变化 → 重载相册与当前视图',
        reloads.join(',') === 'albums,view', `实际 ${JSON.stringify(reloads)}`);
    check('幻灯片续播保留（报告有变化不影响续播）', iv.slideshowTimer !== null);
    iv._stopSlideshow();

    // 报告无变化（同步成功但零变更）：只同步、不重载
    reloads.length = 0;
    env.lifecycle.setVisible(false);
    env.freshness.reports.push({ action: 'sync', added: 0, changed: 0, removed: 0 });
    env.lifecycle.setVisible(true);
    await tick();
    await tick();
    check('零变更的报告不触发重载', reloads.length === 0, `实际 ${JSON.stringify(reloads)}`);

    // skip（引擎忙 / 节流）：不重载
    reloads.length = 0;
    env.lifecycle.setVisible(false);
    env.freshness.reports.push({ action: 'skip', reason: 'busy' });
    env.lifecycle.setVisible(true);
    await tick();
    await tick();
    check('skip 不触发重载', reloads.length === 0, `实际 ${JSON.stringify(reloads)}`);
}

console.log(failures === 0 ? '\nimage-viewer 生命周期用例全部通过' : `\nimage-viewer 生命周期用例失败 ${failures} 项`);
process.exit(failures === 0 ? 0 : 1);
