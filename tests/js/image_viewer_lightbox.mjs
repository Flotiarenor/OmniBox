// image-viewer：灯箱打开时壳返回/左栏导航先关灯箱。
//
// 背景：灯箱是内容页最内层，壳的返回与左栏导航在灯箱之上仍可点击。`createLightbox`
// 现在提供 `isOpen()` / `onClose`，插件应当在收到 `omnibox:action: back` 或
// `omnibox:view` 时先 hide() 并吞掉这次动作；否则内容在灯箱背后切层，用户看到的是
// "点了返回/导航没反应"，且灯箱停在旧图上。
//
// 用例用带 isOpen()/hide() 的灯箱替身驱动真实插件脚本；不复制插件逻辑。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = path.join(ROOT, 'plugins/image-viewer/frontend');

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

function makeDom() {
    const byId = new Map();
    function makeEl(tag = 'div') {
        const el = {
            tagName: String(tag).toUpperCase(),
            className: '',
            id: '',
            textContent: '',
            value: '',
            src: '',
            dataset: {},
            children: [],
            parentElement: null,
            style: { setProperty() { }, removeProperty() { } },
            handlers: {},
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
            appendChild(child) { el.children.push(child); if (child) child.parentElement = el; return child; },
            append(...nodes) { nodes.forEach((node) => el.appendChild(node)); },
            replaceChildren(...nodes) { el.children = []; nodes.forEach((node) => el.appendChild(node)); },
            remove() {
                if (!el.parentElement) return;
                const at = el.parentElement.children.indexOf(el);
                if (at >= 0) el.parentElement.children.splice(at, 1);
                el.parentElement = null;
            },
            setAttribute() { },
            getAttribute() { return null; },
            removeAttribute() { },
            addEventListener(type, fn) { (el.handlers[type] = el.handlers[type] || []).push(fn); },
            removeEventListener() { },
            dispatchEvent(type, event = {}) {
                (el.handlers[type] || []).slice().forEach((fn) => fn({ target: el, currentTarget: el, ...event }));
            },
            click() { el.dispatchEvent('click', { target: el }); },
            focus() { },
            scrollTo() { },
            querySelector: () => null,
            querySelectorAll: () => [],
        };
        let html = '';
        Object.defineProperty(el, 'innerHTML', {
            get: () => html,
            set: (value) => { html = String(value ?? ''); el.children = []; },
        });
        return el;
    }
    const body = makeEl('body');
    const docListeners = {};
    const document = {
        body,
        documentElement: makeEl('html'),
        activeElement: null,
        createElement: (tag) => makeEl(tag),
        createDocumentFragment() { const fragment = makeEl('#fragment'); fragment.isFragment = true; return fragment; },
        getElementById(id) {
            if (!byId.has(id)) { const el = makeEl('div'); el.id = id; byId.set(id, el); }
            return byId.get(id);
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
        removeEventListener() { },
    };
    return { document, body, byId, makeEl };
}

function loadImageViewer() {
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
        Utils: { escapeHtml: (value) => String(value == null ? '' : value), debounce: (fn) => fn },
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
                return Promise.resolve({});
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
        messages,
        calls,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'omnibox:chrome') return messages[i];
            }
            return null;
        },
    };
}

/** 停在图片层、灯箱可开可关的实例（不跑 init，避免真实加载流程）。 */
function makeViewer(env, { open = true, withApi = true } = {}) {
    const iv = new env.ImageViewer();
    iv.mode = 'images';
    iv.currentView = 'albums';
    iv.currentPath = 'A';
    iv.currentPage = 1;
    iv.currentSettings = { per_page: 40 };
    iv.currentItems = [{ type: 'image', url: 'A/1.jpg' }];
    iv.currentAllImages = [{ url: 'A/1.jpg' }, { url: 'A/2.jpg' }];
    iv.pagination = { render() { } };
    const state = { open: !!open, hides: 0 };
    const lightbox = {
        show() { state.open = true; },
        hide() { state.open = false; state.hides += 1; },
        navigate() { },
        setItems() { },
        setDecodedLimit() { },
        getIndex: () => 1,
        getCount: () => 2,
    };
    if (withApi) lightbox.isOpen = () => state.open;
    iv.lightbox = lightbox;
    iv.lightboxState = state;
    return iv;
}

// ---- 1. 灯箱打开时壳返回：先关灯箱，层级不变（旧代码会切回相册层） ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: true });
    iv._handleBack();
    check('灯箱打开时返回先 hide 灯箱', iv.lightboxState.hides === 1, `hides=${iv.lightboxState.hides}`);
    check('壳返回被吞掉，图片层保持不变',
        iv.mode === 'images' && iv.currentPath === 'A',
        JSON.stringify({ mode: iv.mode, path: iv.currentPath }));
    const chrome = env.chrome();
    check('关灯箱后补报当前视图，壳高亮不残留',
        !!chrome && chrome.view === 'albums', chrome && chrome.view);
    check('图片层的返回动作仍可见', !!chrome && chrome.actions.back.visible === true);
}

// ---- 2. 灯箱打开时壳左栏导航：先关灯箱，不切视图 ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: true });
    iv._applyHostView('timeline');
    check('灯箱打开时导航先 hide 灯箱', iv.lightboxState.hides === 1, `hides=${iv.lightboxState.hides}`);
    check('本次导航被吞掉，视图不变',
        iv.mode === 'images' && iv.currentView === 'albums',
        JSON.stringify({ mode: iv.mode, view: iv.currentView }));
    const chrome = env.chrome();
    check('被吞掉的导航不改变壳上报视图', !!chrome && chrome.view === 'albums', chrome && chrome.view);
}

// ---- 3. omnibox:action: back 路径（_applyHostAction → _handleBack） ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: true });
    await iv._applyHostAction('back');
    check('壳 action=back 同样先关灯箱', iv.lightboxState.hides === 1);
    check('action=back 不再切层级', iv.mode === 'images' && iv.currentPath === 'A');
}

// ---- 4. 灯箱已关时不能吞掉返回/导航（回归） ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: false });
    iv._handleBack();
    check('灯箱已关时返回照常工作',
        iv.mode === 'albums' && iv.lightboxState.hides === 0,
        JSON.stringify({ mode: iv.mode, hides: iv.lightboxState.hides }));

    const env2 = loadImageViewer();
    const iv2 = makeViewer(env2, { open: false });
    iv2._applyHostView('timeline');
    check('灯箱已关时导航照常工作',
        iv2.mode === 'albums' && iv2.currentView === 'timeline' && iv2.lightboxState.hides === 0,
        JSON.stringify({ mode: iv2.mode, view: iv2.currentView }));
}

// ---- 5. 旧壳灯箱没有 isOpen()：行为保持不变 ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: true, withApi: false });
    iv._handleBack();
    check('旧壳（无 isOpen）返回照常工作', iv.mode === 'albums');
}

// ---- 6. 首帧迟到补发不能被误当成导航而关掉灯箱 ----
{
    const env = loadImageViewer();
    const iv = makeViewer(env, { open: true });
    iv._startupView = 'albums';
    iv._startupViewCount = 0;
    iv._startupReplayUntil = Date.now() + 3000;
    iv._armViewReplayGuard();
    iv._applyHostView('albums');       // 壳 iframe load 后的同视图重复消息
    check('首帧补发消息不关灯箱',
        iv.lightboxState.hides === 0 && iv.mode === 'images',
        JSON.stringify({ hides: iv.lightboxState.hides, mode: iv.mode }));
    // 真正的导航点击（非首帧重复）仍然先关灯箱
    iv._applyHostView('timeline');
    check('首帧窗口过后导航仍先关灯箱',
        iv.lightboxState.hides === 1 && iv.mode === 'images',
        JSON.stringify({ hides: iv.lightboxState.hides, mode: iv.mode }));
}

console.log('image-viewer 灯箱关闭用例全部通过');
