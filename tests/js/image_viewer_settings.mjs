// image-viewer 设置保存失败的无头回归。
//
// 后端 `PluginBase.save_settings` 对 number/range 做 min/max 校验，失败时整次调用
// 不落盘并返回 `{success:false, error, errors:{字段: 文案}}`。内容页设置弹窗以前
// 不看这个返回值：清空/越界数值被 parseInt 变成 null 后照样提示「设置已保存」、
// 更新内存并关闭弹窗；同一 payload 里的 root_dir 也跟着没写进去。用户看到的和磁盘上
// 的不一致，而且下次打开设置才发现值没变。
//
// 这里锁住失败契约：不更新内存、不报成功、不关弹窗、不触发刷新，并把后端字段级
// errors 显示在弹窗里；成功路径保持原行为。
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
    function makeEl(tag = 'div') {
        const el = {
            tagName: String(tag).toUpperCase(),
            className: '',
            id: '',
            textContent: '',
            value: '',
            checked: false,
            disabled: false,
            src: '',
            dataset: {},
            children: [],
            style: { setProperty() { }, removeProperty() { } },
            handlers: {},
            parentElement: null,
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
            setAttribute() { },
            getAttribute() { return null; },
            removeAttribute() { },
            addEventListener() { },
            removeEventListener() { },
            focus() { },
            querySelector: () => null,
            querySelectorAll: () => [],
        };
        return el;
    }
    const body = makeEl('body');
    const document = {
        body,
        documentElement: makeEl('html'),
        activeElement: null,
        createElement: (tag) => makeEl(tag),
        getElementById(id) {
            if (!byId.has(id)) {
                const el = makeEl('div');
                el.id = id;
                byId.set(id, el);
            }
            return byId.get(id);
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() { },
        removeEventListener() { },
    };
    return { document, body, byId };
}

function loadImageViewer(handlers = {}) {
    const dom = makeDom();
    const messages = [];
    const calls = [];
    const toasts = [];
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
        Toast: {
            success: (message) => toasts.push({ type: 'success', message }),
            error: (message) => toasts.push({ type: 'error', message }),
            warning: (message) => toasts.push({ type: 'warning', message }),
            info: (message) => toasts.push({ type: 'info', message }),
        },
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
        document: dom.document, byId: dom.byId,
        calls, toasts, messages,
        tick,
    };
}

/** 按设置弹窗的表单 id 填好一组合法值；返回可直接读写的元素。 */
function prepareSettingsForm(env) {
    const doc = env.document;
    const set = (id, value) => { doc.getElementById(id).value = String(value); return doc.getElementById(id); };
    set('setting-row-height', 300);
    set('setting-per-page', 40);
    set('setting-sort-by', 'mtime');
    set('setting-sort-order', 'desc');
    set('setting-lightbox-cache', 8);
    set('setting-album-sort-by', 'mtime');
    set('setting-album-sort-order', 'desc');
    doc.getElementById('setting-follow-shell').checked = true;
    set('setting-plugin-alpha', 84);
    doc.getElementById('setting-apply-to-folder').checked = false;
    return doc;
}

function makeApp(env, { currentPath = 'A', currentSettings = { row_height: 200 } } = {}) {
    const app = new env.ImageViewer();
    app.mode = 'albums';
    app.currentView = 'albums';
    app.currentPath = currentPath;
    app.currentSettings = { ...currentSettings };
    app.rootsPicker = null;
    app._albumSortVisible = false;
    app._pixivFuzzyVisible = false;
    app.counters = { closed: 0, cache: 0, albumSort: 0, showAlbums: 0, loadAlbums: 0, refresh: 0 };
    app.closeSettingsModal = () => { app.counters.closed += 1; };
    app._applyLightboxCache = () => { app.counters.cache += 1; };
    app._applyAlbumSortSettings = () => { app.counters.albumSort += 1; };
    app.showAlbums = () => { app.counters.showAlbums += 1; };
    app.loadAlbums = async () => { app.counters.loadAlbums += 1; };
    app.loadImages = async () => { };
    return app;
}

// ---- 1. 后端拒绝保存：不更新内存、不报成功、不关弹窗，显示字段级错误 ----
{
    const env = loadImageViewer({
        save_settings: () => ({
            success: false,
            error: '图片行高必须在 100 到 400 之间',
            errors: { row_height: '图片行高必须在 100 到 400 之间' },
        }),
        clear_folder_settings: () => ({ success: true }),
        refresh: () => ({ success: true }),
    });
    const app = makeApp(env);
    prepareSettingsForm(env);
    // 清空输入框 → parseInt('') = NaN → 结构化克隆/JSON 后是 null（越界值的同一路径）
    env.document.getElementById('setting-row-height').value = '';

    await app.saveSettings();

    check('保存请求已发出', env.calls.some((call) => call.method === 'save_settings'));
    check('失败时没有成功提示',
        !env.toasts.some((toast) => toast.type === 'success' && toast.message.includes('设置已保存')),
        JSON.stringify(env.toasts));
    const errorToast = env.toasts.find((toast) => toast.type === 'error');
    check('失败时给出错误提示', !!errorToast && errorToast.message.includes('图片行高'),
        JSON.stringify(env.toasts));
    const errorBox = env.document.getElementById('settings-errors');
    const errorText = () => errorBox.children.map((child) => child.textContent).join(' ');
    check('字段级错误显示在弹窗里', !errorBox.classList.contains('hidden'));
    check('错误文案带字段名与后端原因',
        errorText().includes('图片行高') && errorText().includes('100 到 400'),
        errorText());
    check('失败时不更新内存设置', app.currentSettings.row_height === 200,
        JSON.stringify(app.currentSettings));
    check('失败时不关闭弹窗', app.counters.closed === 0);
    check('失败时不重建缩略图缓存设置', app.counters.cache === 0);
    check('失败时不刷新相册树', app.counters.loadAlbums === 0 && app.counters.showAlbums === 0);
    check('失败时不触发后端 refresh', env.calls.filter((call) => call.method === 'refresh').length === 0);
    check('失败时不清除文件夹级设置',
        env.calls.filter((call) => call.method === 'clear_folder_settings').length === 0);
}

// ---- 2. 保存成功：维持原有行为，并清掉上一次的错误提示 ----
{
    let mode = 'ok';
    const env = loadImageViewer({
        save_settings: () => (mode === 'ok'
            ? { success: true }
            : { success: false, error: '每页图片数必须在 10 到 200 之间', errors: { per_page: '每页图片数必须在 10 到 200 之间' } }),
        clear_folder_settings: () => ({ success: true }),
        refresh: () => ({ success: true }),
    });
    const app = makeApp(env);
    prepareSettingsForm(env);

    mode = 'fail';
    env.document.getElementById('setting-per-page').value = '1';
    await app.saveSettings();
    check('第一次失败后错误框可见',
        !env.document.getElementById('settings-errors').classList.contains('hidden'));

    mode = 'ok';
    env.document.getElementById('setting-per-page').value = '40';
    env.document.getElementById('setting-row-height').value = '300';
    await app.saveSettings();
    check('成功时给出成功提示',
        env.toasts.some((toast) => toast.type === 'success' && toast.message.includes('设置已保存')));
    check('成功时关闭弹窗', app.counters.closed === 1);
    check('成功时更新内存设置',
        app.currentSettings.row_height === 300 && app.currentSettings.per_page === 40,
        JSON.stringify(app.currentSettings));
    check('成功时应用灯箱缓存设置', app.counters.cache === 1);
    check('成功时触发后端 refresh 与相册重载',
        env.calls.filter((call) => call.method === 'refresh').length === 1
        && app.counters.loadAlbums === 1);
    check('成功时清掉上次的错误提示',
        env.document.getElementById('settings-errors').classList.contains('hidden')
        && env.document.getElementById('settings-errors').children.length === 0);
}

// ---- 3. 后端只给汇总 error（没有 errors 映射）时也要显示原因 ----
{
    const env = loadImageViewer({
        save_settings: () => ({ success: false, error: '设置存储不可写' }),
    });
    const app = makeApp(env);
    prepareSettingsForm(env);
    await app.saveSettings();
    const errorBox = env.document.getElementById('settings-errors');
    const errorText = errorBox.children.map((child) => child.textContent).join(' ');
    check('没有字段错误时显示汇总原因',
        !errorBox.classList.contains('hidden') && errorText.includes('设置存储不可写'),
        errorText);
}

// ---- 4. 「仅应用于当前文件夹」路径：保存成功才更新内存 ----
{
    const env = loadImageViewer({
        save_settings: () => ({ success: true }),
        clear_folder_settings: () => ({ success: true }),
    });
    const app = makeApp(env, { currentPath: 'A' });
    prepareSettingsForm(env);
    env.document.getElementById('setting-apply-to-folder').checked = true;
    await app.saveSettings();
    const saveCalls = env.calls.filter((call) => call.method === 'save_settings');
    check('文件夹级保存使用当前目录作用域',
        saveCalls.length >= 1 && saveCalls[0].args[0] === 'A',
        JSON.stringify(saveCalls.map((call) => call.args[0])));
    check('文件夹级保存不写 root_dir',
        saveCalls[0].args[1].root_dir === undefined,
        JSON.stringify(saveCalls[0].args[1]));
    check('文件夹级保存成功后更新内存', app.counters.closed === 1);
}

console.log('image-viewer 设置保存失败用例全部通过');
