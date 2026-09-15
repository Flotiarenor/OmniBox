// image-viewer 前端生命周期迁移的无头验证脚本（docs/core-contract-fixes.md §3.4.e）。
//
// 背景：image-viewer 是更新频率最高、监听器最多的插件（实测 addEventListener 35 :
// removeEventListener 0），其幻灯片用 setInterval 每 3 秒换图且从不随后台停止 ——
// 切到别的插件后仍在消耗 CPU 并继续拉取缩略图。
//
// 这里用最小 DOM/Bridge stub 跑真实 app.js，锁住三件事：
//   1. onHide 停掉幻灯片定时器，且不要求"停止播放"语义（这里是相册，无播放）；
//   2. onShow 后从原来的位置继续（不是跳回第一张），用户主动停止则不续播；
//   3. onDispose 摘掉 resize 监听器（监听器可回收，不再是 35 : 0）。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BASE_JS = path.join(ROOT, 'shell/frontend/public/shell/base.js');
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
    const parent = { location: { origin: 'http://127.0.0.1:18080' }, document: { documentElement: { getAttribute: () => null, setAttribute() { } } } };
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

    const sandbox = {
        window: win,
        parent,
        document: doc,
        console,
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
    // 先注入 Shell 基础运行时（提供 window.PluginLifecycle），再按 index.html 的声明
    // 顺序装载插件自己的全部脚本 —— 不写死单个文件名，这样 app.js 拆成分片后本用例
    // 自动跟着走（顺序或漏挂出错会在装载期直接抛，见 tests/js/script_load_contract.mjs）。
    vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: `plugins/image-viewer/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__IV = ImageViewer; globalThis.__life = window.PluginLifecycle;', sandbox);
    return {
        win, doc, getEl, winListeners, messageListeners,
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
    check('按钮变成"停止"', env.getEl('btn-slideshow').textContent === '⏸ 停止',
        `实际 ${env.getEl('btn-slideshow').textContent}`);

    env.lifecycle.setVisible(false);
    check('onHide 后定时器被清空', iv.slideshowTimer === null);
    check('onHide 记住了播放位置', iv._slideshowResumeIndex === 4, `实际 ${iv._slideshowResumeIndex}`);
    check('按钮恢复"▶ 幻灯片"', env.getEl('btn-slideshow').textContent === '▶ 幻灯片');

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

console.log(failures === 0 ? '\nimage-viewer 生命周期用例全部通过' : `\nimage-viewer 生命周期用例失败 ${failures} 项`);
process.exit(failures === 0 ? 0 : 1);
