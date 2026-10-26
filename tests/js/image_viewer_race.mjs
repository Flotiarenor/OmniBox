// image-viewer 请求代次 / 迟到视图 / 刷新重绘的无头回归。
//
// 三个真实场景（都来自迁移后壳驱动的路径）：
//   1. 慢响应 + 切视图：旧 list_folder_items 返回时不能再把已切走的瀑布流画回来；
//   2. 慢响应 + 壳首帧补发 omnibox:view：用户已点进相册后，迟到的同视图消息只吞一次；
//   3. 相册里点「校验」/保存设置：刷新相册树不能把图片层强制退回相册列表
//      （`loadAlbums()` 走 `showAlbums()` 会把 mode 改成 albums，随后 `loadImages`
//       永远不会执行——用户在图片层点刷新会被弹回全部相册）。
//
// 用手写的极简 DOM（只需要容器/事件/引用），不复制插件逻辑：装载的是 index.html
// 声明的真实脚本。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = path.join(ROOT, 'plugins', 'image-viewer', 'frontend');

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

function makeDom() {
    const byId = new Map();
    const docListeners = {};
    function makeEl(tag = 'div') {
        const el = {
            tagName: String(tag).toUpperCase(),
            className: '',
            textContent: '',
            value: '',
            src: '',
            alt: '',
            dataset: {},
            children: [],
            style: {
                setProperty() { },
                removeProperty() { },
            },
            handlers: {},
            parentElement: null,
            clientWidth: 800,
            clientHeight: 600,
            scrollTop: 0,
            isConnected: true,
            classList: {
                add(...tokens) {
                    tokens.forEach((token) => {
                        if (token && !el.classList.contains(token)) el.className = `${el.className} ${token}`.trim();
                    });
                },
                remove(...tokens) {
                    const drop = new Set(tokens);
                    el.className = String(el.className).split(/\s+/).filter((token) => token && !drop.has(token)).join(' ');
                },
                contains: (token) => String(el.className).split(/\s+/).includes(token),
                toggle(token, force) {
                    const on = force === undefined ? !el.classList.contains(token) : !!force;
                    if (on) el.classList.add(token); else el.classList.remove(token);
                    return on;
                },
            },
            appendChild(child) {
                if (child && child.isFragment) {
                    [...child.children].forEach((node) => el.appendChild(node));
                    return child;
                }
                el.children.push(child);
                if (child) child.parentElement = el;
                return child;
            },
            append(...nodes) { nodes.forEach((node) => el.appendChild(node)); },
            replaceChildren(...nodes) {
                el.children = [];
                nodes.forEach((node) => el.appendChild(node));
            },
            remove() {
                if (!el.parentElement) return;
                const at = el.parentElement.children.indexOf(el);
                if (at >= 0) el.parentElement.children.splice(at, 1);
                el.parentElement = null;
            },
            setAttribute(name, value) { el[name] = value; },
            getAttribute(name) { return el[name] === undefined ? null : el[name]; },
            removeAttribute(name) { delete el[name]; },
            addEventListener(type, fn) { (el.handlers[type] = el.handlers[type] || []).push(fn); },
            removeEventListener(type, fn) {
                const list = el.handlers[type] || [];
                const at = list.indexOf(fn);
                if (at >= 0) list.splice(at, 1);
            },
            dispatchEvent(type, event = {}) {
                (el.handlers[type] || []).slice().forEach((fn) => fn({ target: el, currentTarget: el, ...event }));
            },
            click() { el.dispatchEvent('click', { target: el }); },
            querySelector() { return null; },
            querySelectorAll() { return []; },
            closest() { return null; },
            contains(node) { return node === el; },
            focus() { },
            blur() { },
            getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; },
            scrollTo() { },
        };
        let html = '';
        Object.defineProperty(el, 'innerHTML', {
            get: () => html,
            set: (value) => {
                html = String(value ?? '');
                el.children = [];
            },
        });
        return el;
    }
    const body = makeEl('body');
    const document = {
        location: null,
        body,
        documentElement: makeEl('html'),
        activeElement: null,
        createElement: (tag) => makeEl(tag),
        createDocumentFragment() {
            const fragment = makeEl('#fragment');
            fragment.isFragment = true;
            return fragment;
        },
        getElementById(id) {
            if (!byId.has(id)) byId.set(id, makeEl('div'));
            return byId.get(id);
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
        removeEventListener() { },
    };
    return { document, body, byId };
}

function loadImageViewer(handlers = {}) {
    const dom = makeDom();
    const messages = [];
    const calls = [];
    const origin = 'http://127.0.0.1:18080';
    const locationStub = { origin, href: `${origin}/plugins/image-viewer/frontend/index.html?content=1` };
    dom.document.location = locationStub;
    const parent = { location: { origin }, postMessage(payload) { messages.push(payload); } };
    const sandbox = {
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        cancelAnimationFrame: (id) => clearTimeout(id),
        document: dom.document,
        location: locationStub,
        localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
        Utils: {
            escapeHtml: (value) => String(value == null ? '' : value),
            debounce: (fn) => fn,
        },
        Icons: {
            html: (name, className) => `<svg class="obx-icon${className ? ' ' + className : ''}">`
                + `<use href="#${String(name).replace(/^icon:/, '')}"></use></svg>`,
        },
        Toast: { success() { }, error() { }, warning() { }, info() { } },
        Motion: { retrigger() { }, stagger() { } },
        confirmDialog: async () => true,
        PluginLifecycle: { onHide() { }, onShow() { }, onDispose() { } },
        Bridge: {
            call(method, ...args) {
                calls.push({ method, args });
                const handler = handlers[method];
                if (!handler) return Promise.resolve({});
                try {
                    return Promise.resolve(handler(...args));
                } catch (error) {
                    return Promise.reject(error);
                }
            },
            originalUrl: (value) => `/file?path=${encodeURIComponent(value)}`,
            thumbUrl: (value) => `/thumb?path=${encodeURIComponent(value)}`,
        },
        URL,
        URLSearchParams,
    };
    sandbox.window = sandbox;
    sandbox.parent = parent;
    vm.createContext(sandbox);
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: `plugins/image-viewer/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__IV = ImageViewer;', sandbox);
    return {
        ImageViewer: sandbox.__IV,
        document: dom.document,
        parent, messages, calls,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'omnibox:chrome') return messages[i];
            }
            return null;
        },
        tick,
    };
}

/** 构造一个停在图片层、不会自己 init 的实例。 */
function imageLayerViewer(env, handlers = {}) {
    const iv = new env.ImageViewer();
    iv.mode = 'images';
    iv.currentView = 'albums';
    iv.currentPath = 'A';
    iv.currentPage = 1;
    iv.currentSettings = { per_page: 40 };
    iv.pagination = { render() { } };
    iv.lightbox = { show() { }, hide() { }, navigate() { }, getIndex: () => 0, setItems() { } };
    return iv;
}

// ---- 1. 慢响应 + 切视图：旧请求返回后不能重绘 ----
{
    let resolveList;
    const deferred = new Promise((resolve) => { resolveList = resolve; });
    const env = loadImageViewer({
        get_settings: () => ({}),
        list_albums: () => ({ albums: [], config: {} }),
        list_folder_items: () => deferred,
    });
    const iv = imageLayerViewer(env);
    const grid = env.document.getElementById('image-grid');
    const pending = iv.loadImages('A', 1);
    await tick();
    check('慢请求已发出', env.calls.some((call) => call.method === 'list_folder_items'),
        JSON.stringify(env.calls.map((call) => call.method)));

    iv._applyHostView('timeline');
    await tick();
    check('切到时间线后处于相册层',
        iv.mode === 'albums' && iv.currentView === 'timeline',
        JSON.stringify({ mode: iv.mode, view: iv.currentView }));

    resolveList({
        items: [{ type: 'image', url: 'A/1.jpg', width: 100, height: 100 }],
        image_total: 1, total: 1, page: 1, all_offset: 0,
    });
    await pending;
    await tick();
    check('迟到的旧响应没有重绘图片网格',
        grid.children.length === 0 && iv.currentItems.length === 0,
        JSON.stringify({ children: grid.children.length, items: iv.currentItems.length }));
    check('迟到响应没有覆盖当前视图', iv.currentView === 'timeline');
    check('当前高亮仍是时间线', env.chrome().view === 'timeline', env.chrome().view);
}

// ---- 2. 首帧迟到补发：进入相册后只吞一次同视图消息 ----
{
    const env = loadImageViewer({ list_albums: () => ({ albums: [], config: {} }) });
    const iv = imageLayerViewer(env);
    iv._startupView = 'albums';
    iv._startupViewCount = 0;
    iv._startupReplayUntil = Date.now() + 3000;
    iv._armViewReplayGuard();
    const chromeBefore = env.messages.length;
    iv._applyHostView('albums');
    check('进入相册后迟到的首帧视图被吞掉',
        iv.mode === 'images' && iv.currentPath === 'A',
        JSON.stringify({ mode: iv.mode, path: iv.currentPath }));
    iv._applyHostView('albums');
    await tick();
    check('同视图的第二条消息按正常导航处理',
        iv.mode === 'albums' && iv.currentPath === '',
        JSON.stringify({ mode: iv.mode, path: iv.currentPath }));
    check('第二次导航确实上报了 chrome', env.messages.length > chromeBefore);
}

// ---- 3. 图片层点「校验」/保存设置：刷新相册树不能退回相册列表 ----
{
    const env = loadImageViewer({
        get_settings: () => ({ per_page: 40 }),
        list_albums: () => ({ albums: [], config: {} }),
        list_folder_items: () => ({ items: [], image_total: 0, total: 0, page: 1 }),
    });
    const iv = imageLayerViewer(env);
    await iv._onFreshnessChanged();
    await tick();
    check('刷新后仍在图片层', iv.mode === 'images', iv.mode);
    check('刷新后仍停在原目录', iv.currentPath === 'A', iv.currentPath);
    const folderCalls = env.calls.filter((call) => call.method === 'list_folder_items');
    check('刷新重新拉了当前目录的图片',
        folderCalls.length === 1 && folderCalls[0].args[0] === 'A',
        JSON.stringify(folderCalls.map((call) => call.args)));
    check('重新拉取的是当前页', folderCalls.length === 1 && folderCalls[0].args[1] === 1);
}

// ---- 4. 相册层刷新：仍然要重绘相册树（别把修复做成"永远不刷新相册"） ----
{
    const env = loadImageViewer({
        list_albums: () => ({ albums: [], config: {} }),
    });
    const iv = new env.ImageViewer();
    iv.mode = 'albums';
    iv.currentView = 'albums';
    await iv._onFreshnessChanged();
    check('相册层刷新后仍在相册层', iv.mode === 'albums');
    check('相册层刷新确实读取了相册树',
        env.calls.filter((call) => call.method === 'list_albums').length === 1);
    check('相册层刷新上报了 chrome', env.chrome() !== null);
}

console.log('image-viewer 请求代次/刷新用例全部通过');
