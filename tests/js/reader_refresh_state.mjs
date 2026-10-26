// document-reader 阅读态 / 刷新 / 加载 / 进度轨 / 空态的无头回归。
//
// 这里守的是迁移到壳 chrome 之后容易丢的三类状态：
//   1. 壳顶栏的标题归内容页在阅读态维护；统一刷新（omnibox:action: refresh）会重扫书架，
//      但重扫不能顺手把顶栏标题改回书架标题 —— `_reloadSettings` 有阅读态守卫，
//      `_reloadShelf` 以前没有，用户在正文里点「校验」会看到书名变成「全部文档」。
//   2. 加载提示必须挂在阅读器容器上：engine 换章会清空正文 innerHTML，挂在正文里的
//      提示会被一起删掉（旧实现的 spinner 从没真正出现过）。
//   3. 底部进度轨只在阅读器有章节进度时出现；书架、PDF/外部打开都不该留一条空槽。
//
// 用 dom_stub 的极简 DOM，只断言状态与 DOM 归属，不做布局。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

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

function loadReader(handlers = {}) {
    const { document, makeEl } = createDom();
    const messages = [];
    const calls = [];
    const toasts = [];
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
        Icons: {
            html: (name, className) => `<svg class="obx-icon${className ? ' ' + className : ''}">`
                + `<use href="#${String(name).replace(/^icon:/, '')}"></use></svg>`,
        },
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
        PluginLifecycle: { onHide() { }, onShow() { }, onDispose() { } },
        ContentUI: {
            card(options = {}) {
                const el = document.createElement('div');
                el.className = 'cui-card nr-card';
                if (options.data && options.data.id) el.dataset.id = options.data.id;
                return el;
            },
        },
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
        },
        // 主控制器构造函数只 new 它们；用例按需覆写实例方法。
        DocumentReaderEngine: class {
            constructor(app) {
                this.app = app;
                this.chapters = [];
                this.currentChapterIndex = 0;
            }
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
    for (const rel of ['js/app.js', 'js/app-shelf.js']) {
        vm.runInContext(fs.readFileSync(path.join(FRONTEND, rel), 'utf8'), sandbox,
            { filename: `plugins/document-reader/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__Reader = DocumentReader;', sandbox);
    return {
        document, makeEl, sandbox,
        Reader: sandbox.__Reader,
        parent, messages, calls, toasts,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'omnibox:chrome') return messages[i];
            }
            return null;
        },
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

function makeApp(env, handlers = {}) {
    const app = new env.Reader();
    app.settings = {
        reload: async () => false,
    };
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
    app.documents = handlers.documents || [];
    app.marks = {};
    app._pushChrome = app._pushChrome.bind(app);
    return app;
}

// ---- 1. 阅读态收到统一刷新：书架重扫了，但壳顶栏的标题必须还是书名 ----
{
    const env = loadReader({ document_list: () => ({ documents: [{ id: 'd1', title: '书名', kind: 'txt' }] }) });
    const app = makeApp(env);
    app.documents = [{ id: 'd1', title: '书名', kind: 'txt' }];
    app._isReaderMode = true;
    app.currentDocument = app.documents[0];
    app._chromeTitle = '书名';
    app._chromeSub = '';
    app._loadMarks = async () => { };
    app._bindHost();
    app._pushChrome();
    env.send({ type: 'omnibox:action', action: 'refresh' });
    await tick(20);
    check('阅读态刷新后顶栏标题仍是书名', app._chromeTitle === '书名', app._chromeTitle);
    check('阅读态刷新后副标题不被书架统计覆盖', app._chromeSub === '', JSON.stringify(app._chromeSub));
    check('阅读态刷新后最后上报的标题仍是书名',
        env.chrome() && env.chrome().title === '书名',
        JSON.stringify(env.chrome() && env.chrome().title));
    check('阅读态刷新仍会重扫书架',
        env.calls.filter((call) => call.method === 'document_list').length === 1);
}

// ---- 2. 加载提示挂在阅读器容器，不挂正文容器 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._enterReaderMode({ id: 'd1', title: '书名', kind: 'txt' });
    app._showLoading(true);
    const loading = env.document.getElementById('document-loading');
    check('加载提示已创建', !!loading);
    check('加载提示挂在阅读器容器上',
        !!loading && loading.parentElement === app._dom.readerView,
        loading && loading.parentElement && loading.parentElement.id);
    check('加载提示不在正文容器里',
        !app._dom.contentArea.children.includes(loading));
    check('加载提示可见', !!loading && loading.style.display === 'flex');
    app._showLoading(false);
    check('渲染完成后隐藏加载提示', loading.style.display === 'none');
}

// ---- 3. 进度轨：阅读器有章节才出现，回书架/无章节隐藏 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._enterReaderMode({ id: 'd1', title: '书名', kind: 'txt' });
    check('进入阅读态先隐藏进度轨', app._dom.progressBar.classList.contains('hidden'));
    app.engine.chapters = [];
    app._updateProgressBar();
    check('无章节（PDF/外部）不显示进度轨', app._dom.progressBar.classList.contains('hidden'));
    app.engine.chapters = [{}, {}, {}, {}];
    app.engine.currentChapterIndex = 1;
    app.engine.chapterFraction = () => 0.5;
    app._updateProgressBar();
    check('有章节时显示进度轨', !app._dom.progressBar.classList.contains('hidden'));
    check('进度按章号+章内比例计算',
        app._dom.progressFill.style.width === '37.50%', app._dom.progressFill.style.width);
    app._isReaderMode = false;
    app.currentDocument = null;
    app.returnToShelf();
    check('返回书架隐藏进度轨', app._dom.progressBar.classList.contains('hidden'));
    check('返回书架显示书架容器', !app._dom.shelfView.classList.contains('hidden'));
    check('返回书架隐藏阅读器容器', app._dom.readerView.classList.contains('hidden'));
}

// ---- 4. 书架读取失败 vs 分类为空：错误态带原因与重试 ----
{
    let fail = true;
    const env = loadReader({
        document_list: () => {
            if (fail) throw new Error('根目录不可读');
            return { documents: [{ id: 'd1', title: '恢复后的书', kind: 'txt' }] };
        },
    });
    const app = makeApp(env);
    app._dom.grid.id = 'nr-grid';
    const realConsoleError = console.error;
    console.error = () => { };          // 失败分支会打一条错误日志，用例里不刷屏
    await app._loadDocuments();
    app._renderShelf();
    let html = app._dom.grid.innerHTML;
    console.error = realConsoleError;
    check('读取失败进入错误态', html.includes('书架加载失败'), html.slice(0, 100));
    check('错误态给出原因', html.includes('根目录不可读'));
    check('错误态带重试按钮', html.includes('data-nr-retry'));
    check('状态栏报失败而不是 0 本', app._statusText === '书架加载失败', app._statusText);

    fail = false;
    await app._retryShelf(null);
    html = app._dom.grid.innerHTML;
    check('重试后清掉错误态', !html.includes('书架加载失败'), html.slice(0, 100));
    check('重试后渲染出书', app._dom.grid.children.length === 1, String(app._dom.grid.children.length));
}

// ---- 5. 搜索无结果 vs 分类为空：两套文案 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app.view = 'all';
    app.documents = [];
    app._renderShelf('');
    check('空分类提示放文档', app._dom.grid.innerHTML.includes('这个分类还是空的')
        && app._dom.grid.innerHTML.includes('.txt / .md / .epub'));
    app._renderShelf('找不到');
    check('搜索无结果用独立文案', app._dom.grid.innerHTML.includes('没有匹配的文档'));
    check('搜索无结果不再提示空分类', !app._dom.grid.innerHTML.includes('这个分类还是空的'));
}

// ---- 6. chrome 上报契约：阅读态清高亮/显示返回；朗读设置页标题与视图 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._isReaderMode = true;
    app._chromeTitle = '书名';
    app._chromeSub = '';
    app._pushChrome();
    let chrome = env.chrome();
    check('阅读态清掉左栏高亮', chrome.view === '', JSON.stringify(chrome.view));
    check('阅读态显示壳返回', chrome.actions.back.visible === true);
    check('阅读态打开「当前文档」导航组',
        chrome.nav['reader-tools'] && chrome.nav['reader-tools'].visible === true,
        JSON.stringify(chrome.nav));
    check('阅读态隐藏搜索', chrome.search.enabled === false);

    app._isReaderMode = false;
    app._voicePaneOpen = true;
    app._pushChrome();
    chrome = env.chrome();
    check('朗读设置页标题为朗读设置', chrome.title === '朗读设置', chrome.title);
    check('朗读设置页视图为 voice', chrome.view === 'voice', chrome.view);
    check('朗读设置页隐藏搜索', chrome.search.enabled === false);
}

// ---- 7. 壳返回动作在阅读态回到书架 ----
{
    const env = loadReader();
    const app = makeApp(env);
    app._isReaderMode = true;
    app.currentDocument = { id: 'd1', title: '书名', kind: 'txt' };
    app._chromeTitle = '书名';
    app._bindHost();
    env.send({ type: 'omnibox:action', action: 'back' });
    await tick(10);
    check('壳返回退出阅读态', app._isReaderMode === false);
    check('壳返回后上报书架视图', env.chrome().view === 'all', env.chrome().view);
    check('壳返回后顶栏标题回到分类标题', app._chromeTitle === '全部文档', app._chromeTitle);
}

console.log('document-reader 阅读态/刷新用例全部通过');

