// document-reader 统一刷新接入用例。
//
// 守四件事：
//   1. 切回前台（onShow）按全部文档根同步一次；
//   2. 同步报告真有变化时重读文档与书签，书架态重绘、阅读态不重绘（阅读态重绘会用
//      分类标题覆盖壳顶栏的书名）；
//   3. 报告的零变化与 `skip` 不触发重读；
//   4. chrome 上报同步作用域（文档根可以有多个，作用域是全部根）。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';
import { loadShellScripts } from './shell_scripts.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const FRONTEND = path.join(ROOT, 'plugins', 'document-reader', 'frontend');

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

const escapeHtml = (value) => String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const documents = [
    { id: 'd1', title: '书名', kind: 'txt', dir: '', file_size: 1024 },
    { id: 'd2', title: '第二本', kind: 'txt', dir: '子目录', file_size: 2048 },
];

function loadReader(options = {}) {
    const { document, makeEl } = createDom();
    const messages = [];
    const calls = [];
    const toasts = [];
    const lifecycle = { show: [], hide: [], dispose: [] };
    const freshnessCalls = [];
    const windowListeners = {};
    const origin = 'http://127.0.0.1:18080';
    const parent = { postMessage(payload) { messages.push(payload); } };
    const sandbox = {
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        cancelAnimationFrame: (id) => clearTimeout(id),
        document,
        location: { origin, href: `${origin}/plugins/document-reader/frontend/index.html?content=1` },
        localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
        CSS: { escape: (value) => String(value) },
        Utils: { escapeHtml },
        Icons: { html: () => '<svg class="obx-icon"></svg>' },
        DocumentUtils: {
            formatSize: () => '1 KB',
            coverDataUrl: (title) => `data:image/svg+xml,cover-${encodeURIComponent(title || '')}`,
            splitParagraphs: (text) => [text],
            formatContent: (text) => text,
        },
        Motion: { stagger() { }, retrigger() { } },
        Toast: {
            success: (message) => toasts.push({ type: 'success', message }),
            error: (message) => toasts.push({ type: 'error', message }),
            warning: (message) => toasts.push({ type: 'warning', message }),
            info: (message) => toasts.push({ type: 'info', message }),
        },
        confirmDialog: async () => true,
        PluginLifecycle: {
            onShow(fn) { lifecycle.show.push(fn); },
            onHide(fn) { lifecycle.hide.push(fn); },
            onDispose(fn) { lifecycle.dispose.push(fn); },
        },
        ContentUI: {
            card(options = {}) {
                const el = document.createElement('div');
                el.className = 'cui-card nr-card';
                if (options.data && options.data.id) el.dataset.id = options.data.id;
                return el;
            },
        },
        Freshness: {
            sync(plugin, scope) {
                freshnessCalls.push({ plugin, scope });
                const report = options.report ? options.report(plugin, scope)
                    : { action: 'sync', added: 0, changed: 0, removed: 0 };
                return Promise.resolve(report);
            },
        },
        Bridge: {
            call(method, ...args) {
                calls.push({ method, args });
                const handler = options.handlers && options.handlers[method];
                if (!handler) return Promise.resolve(method === 'marks_list' ? { marks: [] } : { documents });
                try {
                    return Promise.resolve(handler(...args));
                } catch (error) {
                    return Promise.reject(error);
                }
            },
            originalUrl: (value) => `/file?path=${encodeURIComponent(value)}`,
        },
        // 主控制器构造函数只 new 它们；用例按需覆写实例方法。
        DocumentReaderEngine: class {
            constructor(app) { this.app = app; this.chapters = []; this.currentChapterIndex = 0; }
            setMode() { }
            reset() { this.chapters = []; }
            goToChapter() { return Promise.resolve(); }
            chapterFraction() { return 0; }
            getChapterContext() { return { text: '', container: null }; }
        },
        ReaderTts: class {
            constructor(app) { this.app = app; this.lastEngine = ''; }
            stop() { }
            init() { return Promise.resolve(); }
            startFromViewport() { }
            refreshStatus() { return Promise.resolve(); }
        },
        ReaderVoicePage: class {
            constructor(app) { this.app = app; }
            open() { }
            close() { }
        },
    };
    sandbox.window = sandbox;
    sandbox.parent = parent;
    sandbox.addEventListener = (type, fn) => {
        (windowListeners[type] = windowListeners[type] || []).push(fn);
    };
    sandbox.removeEventListener = (type, fn) => {
        const list = windowListeners[type] || [];
        const at = list.indexOf(fn);
        if (at >= 0) list.splice(at, 1);
    };
    vm.createContext(sandbox);
    loadShellScripts(sandbox, ['chrome-client.js']);
    for (const rel of ['js/app.js', 'js/app-shelf.js']) {
        vm.runInContext(fs.readFileSync(path.join(FRONTEND, rel), 'utf8'), sandbox,
            { filename: `plugins/document-reader/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__Reader = DocumentReader;', sandbox);
    return {
        document, makeEl, sandbox,
        Reader: sandbox.__Reader,
        parent, messages, calls, toasts, lifecycle, freshnessCalls,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'cubby:chrome') return messages[i];
            }
            return null;
        },
        show() { lifecycle.show.forEach((fn) => fn()); },
        send(data) {
            (windowListeners.message || []).slice().forEach((fn) => fn({
                origin,
                source: parent,
                data,
            }));
        },
        tick,
    };
}

function makeApp(env) {
    const app = new env.Reader();
    app.settings = { reload: async () => false };
    app._dom = {
        shelfView: env.makeEl('div'),
        readerView: env.makeEl('div'),
        grid: env.makeEl('div'),
        contentArea: env.makeEl('div'),
        progressBar: env.makeEl('div'),
        progressFill: env.makeEl('div'),
        tocList: env.makeEl('div'),
        tocSub: env.makeEl('span'),
        tocFilter: env.makeEl('input'),
    };
    app._dom.readerView.id = 'nr-reader-view';
    app._dom.shelfView.id = 'nr-shelf-view';
    app._dom.grid.id = 'nr-grid';
    app.documents = [];
    app.marks = {};
    app._bindHost();
    return app;
}

const changed = { action: 'sync', added: 1, changed: 0, removed: 0 };
const zero = { action: 'sync', added: 0, changed: 0, removed: 0 };

// ---- 1. onShow 同步一次（全部根），无变化不重读 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._bindLifecycle();
    env.show();
    await tick(10);
    check('切回前台同步一次', env.freshnessCalls.length === 1, JSON.stringify(env.freshnessCalls));
    check('同步用本插件名', env.freshnessCalls[0].plugin === 'document-reader', env.freshnessCalls[0].plugin);
    check('同步覆盖全部文档根', env.freshnessCalls[0].scope === '', JSON.stringify(env.freshnessCalls[0]));
    check('无变化不重读文档', env.calls.filter((call) => call.method === 'document_list').length === 0);
}

// ---- 2. 有变化：书架态重读文档与书签并重绘 ----
{
    const env = loadReader({ report: () => changed });
    const app = makeApp(env);
    app._bindLifecycle();
    env.show();
    await tick(20);
    check('有变化重读文档列表', env.calls.filter((call) => call.method === 'document_list').length === 1);
    check('有变化重读书签', env.calls.filter((call) => call.method === 'marks_list').length === 1);
    const cards = app._dom.grid.children.filter((node) => String(node.className).includes('nr-card'));
    check('书架态重绘出文档', cards.length === documents.length, String(cards.length));
}

// ---- 3. 有变化且处于阅读态：只重读数据，不重绘书架、不改壳标题 ----
{
    const env = loadReader({ report: () => changed });
    const app = makeApp(env);
    app.documents = documents;
    app._renderShelf('');
    const shelfCards = app._dom.grid.children.slice();
    app._isReaderMode = true;
    app.currentDocument = documents[0];
    app._chromeTitle = '书名';
    app._chromeSub = '';
    app._pushChrome();

    let renderCalls = 0;
    const realRender = app._renderShelf.bind(app);
    app._renderShelf = (...args) => { renderCalls += 1; return realRender(...args); };

    app._bindLifecycle();
    env.show();
    await tick(20);

    check('阅读态刷新仍重读文档', env.calls.filter((call) => call.method === 'document_list').length === 1);
    check('阅读态刷新仍重读书签', env.calls.filter((call) => call.method === 'marks_list').length === 1);
    check('阅读态刷新不重绘书架', renderCalls === 0, String(renderCalls));
    check('阅读态书架内容未变',
        app._dom.grid.children.length === shelfCards.length
            && app._dom.grid.children.every((node, index) => node === shelfCards[index]),
        `children=${app._dom.grid.children.length}/${shelfCards.length}`);
    check('阅读态顶栏标题仍是书名', app._chromeTitle === '书名', app._chromeTitle);
}

// ---- 4. cubby:freshness：有变化才重读，skip 与别的插件忽略 ----
{
    const env = loadReader();
    const app = makeApp(env);
    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: zero });
    await tick(10);
    check('零变化报告不重读', env.calls.filter((call) => call.method === 'document_list').length === 0);

    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: { action: 'skip', reason: 'busy' } });
    await tick(10);
    check('skip 不重读', env.calls.filter((call) => call.method === 'document_list').length === 0);

    env.send({ type: 'cubby:freshness', plugin: 'other-plugin', scope: '', report: changed });
    await tick(10);
    check('别的插件的报告不重读', env.calls.filter((call) => call.method === 'document_list').length === 0);

    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: changed });
    await tick(20);
    check('有变化报告重读文档', env.calls.filter((call) => call.method === 'document_list').length === 1);
}

// ---- 5. 旧的 cubby:action: refresh（无 payload）仍重读 ----
{
    const env = loadReader();
    const app = makeApp(env);
    env.send({ type: 'cubby:action', action: 'refresh' });
    await tick(20);
    check('旧 refresh 动作仍重读文档', env.calls.filter((call) => call.method === 'document_list').length === 1);
}

// ---- 5b. 新壳两条都发：富事件 + 紧随其后的回声 refresh（带 payload）只重读一次 ----
{
    const env = loadReader();
    const app = makeApp(env);
    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: changed });
    env.send({
        type: 'cubby:action', action: 'refresh',
        payload: { plugin: 'document-reader', scope: '', report: changed },
    });
    await tick(20);
    check('富事件之后的回声动作不重复重读',
        env.calls.filter((call) => call.method === 'document_list').length === 1,
        String(env.calls.filter((call) => call.method === 'document_list').length));
}
{
    const env = loadReader();
    const app = makeApp(env);
    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: zero });
    env.send({
        type: 'cubby:action', action: 'refresh',
        payload: { plugin: 'document-reader', scope: '', report: zero },
    });
    await tick(20);
    check('零变化 + 回声动作也不重读',
        env.calls.filter((call) => call.method === 'document_list').length === 0);
}
{
    const env = loadReader();
    const app = makeApp(env);
    // 回声按 payload 判定，与两条消息的到达间隔无关
    env.send({ type: 'cubby:freshness', plugin: 'document-reader', scope: '', report: changed });
    await tick(1500);
    env.send({
        type: 'cubby:action', action: 'refresh',
        payload: { plugin: 'document-reader', scope: '', report: changed },
    });
    await tick(20);
    check('回声判定与到达间隔无关',
        env.calls.filter((call) => call.method === 'document_list').length === 1,
        String(env.calls.filter((call) => call.method === 'document_list').length));
}

// ---- 6. chrome 上报同步作用域 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._pushChrome();
    check('chrome 上报全部根作用域', env.chrome().freshness_scope === '',
        JSON.stringify(env.chrome().freshness_scope));
}

console.log('document-reader 统一刷新用例全部通过');
