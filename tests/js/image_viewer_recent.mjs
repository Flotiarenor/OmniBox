// image-viewer 时间线「按相册 / 按图片」切换的无头回归。
//
// 需求：时间线视图顶部有一个本页自绘的分段控件；「按图片」进入图片层，用后端
// `list_recent_images` 的「更新单元」（作品卡 + 单图）铺 justified 网格 —— 作品卡是
// p0 封面 + 张数角标，点开灯箱时用 `all_images` 展开序列、起始下标 = 该单元前面
// 各单元的张数累加。
//
// 这里锁住这些事（都在真实脚本上跑，后端全部用 Bridge 打桩）：
//   1. 切到「按图片」确实调 `list_recent_images(page, perPage)`，混合单元渲染成
//      作品卡（封面 / 角标 / 作品名）与单图卡；
//   2. 点作品卡、单图卡打开的灯箱序列都是 `all_images`，起始下标按前面单元张数累加；
//   3. 状态栏区分单元数与张数（`共 N 项 · M 张图片` / 纯单图 `共 N 张图片`），
//      partial 用 warn 并注明只列出最近扫描到的部分，分页按单元数；
//   4. 切回「按相册」与壳返回都能退出图片层，`_recentActive` 复位，容器清干净；
//   5. 在途的 `list_recent_images` 响应在切走之后不得重绘（与 loadImages 同一套代次守卫）；
//   6. 图片层重载走 `_reloadCurrentView()`：最近图片层请求 `list_recent_images`，
//      普通目录层仍请求 `list_folder_items`；搜索过滤对作品卡与单图都生效。
//
// 装载的是 index.html 声明的真实脚本（不复制插件逻辑）；宿主 DOM 用手写极简替身，
// 只提供渲染与事件登记够用的部分。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { readDeclaredScripts } from './script_load_contract.mjs';
import { loadShellScripts } from './shell_scripts.mjs';

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
            textContent: '',
            value: '',
            type: '',
            src: '',
            alt: '',
            dataset: {},
            children: [],
            handlers: {},
            attrs: {},
            style: { setProperty() { }, removeProperty() { } },
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
                if (!child) return child;
                if (child.isFragment) {
                    [...child.children].forEach((node) => el.appendChild(node));
                    return child;
                }
                el.children.push(child);
                child.parentElement = el;
                return child;
            },
            append(...nodes) { nodes.forEach((node) => el.appendChild(node)); },
            insertBefore(child, ref) {
                if (!ref) return el.appendChild(child);
                const at = el.children.indexOf(ref);
                if (at < 0) return el.appendChild(child);
                el.children.splice(at, 0, child);
                child.parentElement = el;
                return child;
            },
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
            setAttribute(name, value) { el.attrs[name] = String(value); },
            getAttribute(name) {
                return Object.prototype.hasOwnProperty.call(el.attrs, name) ? el.attrs[name] : null;
            },
            removeAttribute(name) { delete el.attrs[name]; },
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
        Object.defineProperty(el, 'firstChild', { get: () => el.children[0] || null });
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
        Utils: {
            escapeHtml: (value) => String(value == null ? '' : value),
            debounce: (fn) => fn,
        },
        Icons: {
            html: (name, className) => `<svg class="obx-icon${className ? ' ' + className : ''}">`
                + `<use href="#${String(name).replace(/^icon:/, '')}"></use></svg>`,
        },
        // 相册卡片的共享基座：本用例只走时间线/图片层，卡片本身不是断言对象，
        // 给一个能 appendChild 的最小实现即可（真实实现由 /shell/content-ui.js 注入）。
        ContentUI: {
            card() {
                const card = dom.document.createElement('div');
                card.className = 'cui-card';
                return card;
            },
            grid() {
                const grid = dom.document.createElement('div');
                grid.className = 'cui-grid';
                return grid;
            },
            setSelected() { },
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
        URL,
        URLSearchParams,
    };
    sandbox.window = sandbox;
    sandbox.parent = parent;
    vm.createContext(sandbox);
    // 内容页的握手在壳注入的 /shell/chrome-client.js 里：按引导片段装载同一份实现。
    loadShellScripts(sandbox, ['chrome-client.js']);
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: `plugins/image-viewer/frontend/${rel}` });
    }
    vm.runInContext('globalThis.__IV = ImageViewer;', sandbox);
    return {
        ImageViewer: sandbox.__IV,
        document: dom.document,
        parent, messages, calls, toasts,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'cubby:chrome') return messages[i];
            }
            return null;
        },
        chromeCount: () => messages.filter((message) => message && message.type === 'cubby:chrome').length,
        tick,
    };
}

const ALBUMS = [
    {
        path: 'A', name: 'A 相册', parent: '', depth: 1, mtime: 1750000000,
        image_count: 3, direct_count: 3, readable: 3, has_children: false, cover: '',
    },
    {
        path: 'B', name: 'B 相册', parent: '', depth: 1, mtime: 1700000000,
        image_count: 2, direct_count: 2, readable: 2, has_children: false, cover: '',
    },
];

// 更新单元：作品卡（type:'album'，p0 封面 + 张数角标）与散图（type:'image'）混排。
const MIXED_ITEMS = [
    {
        type: 'album', path: 'A/作品1', name: '作品1', cover: 'A/作品1/p0.jpg',
        image_count: 3, total_count: 3, width: 800, height: 600,
        mtime: 1750000400, has_children: false,
    },
    {
        type: 'image', url: 'B/单图1.jpg', name: '单图1.jpg', width: 600, height: 900,
        mtime: 1750000300,
    },
    {
        type: 'album', path: 'A/作品2', name: '作品2', cover: 'A/作品2/p0.jpg',
        image_count: 2, total_count: 2, width: 700, height: 700,
        mtime: 1750000200, has_children: false,
    },
];

// 单图的连续浏览序列（作品按 p0→pN 展开）：与上面的单元顺序一一对应。
const MIXED_ALL_IMAGES = [
    { url: 'A/作品1/p0.jpg', width: 800, height: 600 },
    { url: 'A/作品1/p1.jpg', width: 800, height: 600 },
    { url: 'A/作品1/p2.jpg', width: 800, height: 600 },
    { url: 'B/单图1.jpg', width: 600, height: 900 },
    { url: 'A/作品2/p0.jpg', width: 700, height: 700 },
    { url: 'A/作品2/p1.jpg', width: 700, height: 700 },
];

/** list_recent_images 的新契约：items 是更新单元，all_images 是本页展开序列。 */
function mixedPayload(page = 1, perPage = 40, overrides = {}) {
    return {
        items: MIXED_ITEMS.map((item) => ({ ...item })),
        all_images: MIXED_ALL_IMAGES.map((image) => ({ ...image })),
        page,
        per_page: perPage,
        // total 是单元数（作品 + 单图），image_total 才是图片张数。
        total: 3,
        image_total: 6,
        has_next: false,
        has_prev: false,
        scanned_dirs: 2,
        partial: false,
        ...overrides,
    };
}

/** 全为散图的一页：total === image_total，状态栏用「共 N 张图片」。 */
function singleImagesPayload(page = 1, perPage = 40, overrides = {}) {
    const images = [
        { type: 'image', url: 'A/1.jpg', name: '1.jpg', width: 800, height: 600, mtime: 1750000100 },
        { type: 'image', url: 'A/2.jpg', name: '2.jpg', width: 600, height: 900, mtime: 1750000200 },
    ];
    return {
        items: images,
        all_images: images.map(({ url, width, height }) => ({ url, width, height })),
        page,
        per_page: perPage,
        total: images.length,
        image_total: images.length,
        has_next: false,
        has_prev: false,
        scanned_dirs: [],
        partial: false,
        ...overrides,
    };
}

/** 记录灯箱调用：本用例断言的是"用哪个序列、从第几张开始"。 */
function makeLightbox() {
    const shown = [];
    return {
        shown,
        show(items, index) { shown.push({ items, index }); },
        hide() { },
        navigate() { },
        setItems() { },
        getIndex: () => 0,
        isOpen: () => false,
    };
}

/** 停在时间线相册层、不跑 init 的实例（init 会读设置/相册并绑整套 UI）。 */
function makeTimelineViewer(env, { timelineMode = 'albums', albums = ALBUMS } = {}) {
    const iv = new env.ImageViewer();
    iv.currentView = 'timeline';
    iv.mode = 'albums';
    iv.albums = albums;
    iv.albumConfig = { collapsed: [], promoted: [], expanded: [], visible_empty_dirs: [] };
    iv.currentSettings = { per_page: 40, row_height: 200 };
    iv.currentRowHeight = 200;
    iv.timelineMode = timelineMode;
    iv.lightbox = makeLightbox();
    iv.pagination = { pages: [], render(page, total) { this.pages.push({ page, total }); } };
    return iv;
}

function findModeSwitch(container) {
    return container.children.find(
        (child) => String(child.className).split(/\s+/).includes('iv-mode-switch'),
    ) || null;
}

const modeButtons = (container) => {
    const switcher = findModeSwitch(container);
    return switcher ? switcher.children : [];
};

const recentCalls = (env) => env.calls.filter((call) => call.method === 'list_recent_images');
const childWithClass = (card, token) => card.children.find(
    (child) => String(child.className).split(/\s+/).includes(token),
) || null;

/** 进入时间线「按图片」层的公共步骤。 */
async function enterRecentLayer(env, iv) {
    iv.showAlbums();
    const albumsEl = env.document.getElementById('iv-albums');
    modeButtons(albumsEl)[1].click();
    await tick();
    return env.document.getElementById('image-grid');
}

// ---- 0. 控件容器在网格容器之前：图片层里控件必须留在网格上方 ------------------
{
    const html = fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
    check('切换控件容器 #iv-albums 在 #image-grid 之前（控件留在网格上方）',
        html.indexOf('id="iv-albums"') >= 0
        && html.indexOf('id="iv-albums"') < html.indexOf('id="image-grid"'));
}

// ---- 1. 切到「按图片」：请求 list_recent_images，混合单元渲染成作品卡 + 单图 ----
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
    });
    const iv = makeTimelineViewer(env);
    iv.showAlbums();

    const albumsEl = env.document.getElementById('iv-albums');
    const grid = env.document.getElementById('image-grid');
    const switcher = findModeSwitch(albumsEl);
    check('时间线顶部渲染了切换控件', !!switcher);
    check('控件带 role=group', switcher && switcher.getAttribute('role') === 'group',
        switcher && switcher.getAttribute('role'));
    const [byAlbums, byImages] = switcher.children;
    check('两个按钮文案是「按相册 / 按图片」',
        byAlbums.textContent === '按相册' && byImages.textContent === '按图片',
        JSON.stringify([byAlbums.textContent, byImages.textContent]));
    check('默认「按相册」按下（active + aria-pressed=true）',
        byAlbums.getAttribute('aria-pressed') === 'true' && byAlbums.classList.contains('active'));
    check('「按图片」未按下', byImages.getAttribute('aria-pressed') === 'false');
    check('相册层不请求最近图片', recentCalls(env).length === 0);
    check('相册层状态栏仍是相册统计', env.chrome().status.text.includes('个相册'),
        env.chrome().status.text);

    env.calls.length = 0;
    iv.searchQuery = '上个视图留下的关键词';
    byImages.click();
    await tick();

    const calls = recentCalls(env);
    check('切到「按图片」调用 list_recent_images(page, perPage)',
        calls.length === 1 && calls[0].args[0] === 1 && calls[0].args[1] === 40,
        JSON.stringify(calls.map((call) => call.args)));
    check('进入图片层：mode/_recentActive/currentPath/currentPage 就位',
        iv.mode === 'images' && iv._recentActive === true
        && iv.currentPath === '' && iv.currentPage === 1 && iv.navStack.length === 0,
        JSON.stringify({ mode: iv.mode, recent: iv._recentActive, path: iv.currentPath }));
    check('进入图片层清空搜索词并通知壳清空',
        iv.searchQuery === '' && iv._searchClear === 1,
        JSON.stringify({ query: iv.searchQuery, clear: iv._searchClear }));
    check('混合单元渲染为 3 张卡片',
        grid.children.length === 3
        && grid.children.every((card) => card.classList.contains('iv-image-card')),
        String(grid.children.length));

    // 作品卡：p0 封面 + 页数角标 + 作品名（不另写卡片，复用 renderJustifiedLayout 的 album 分支）
    const artworkCard = grid.children[0];
    const artworkCover = childWithClass(artworkCard, 'iv-cover-img');
    const artworkBadge = childWithClass(artworkCard, 'iv-count-badge');
    const artworkLabel = artworkCard.children.find((child) => child.tagName === 'P');
    check('作品卡带 p0 封面',
        !!artworkCover && String(artworkCover.src).includes('p0.jpg'),
        artworkCover && artworkCover.src);
    check('作品卡角标是作品张数', !!artworkBadge && String(artworkBadge.textContent) === '3',
        artworkBadge && String(artworkBadge.textContent));
    check('作品卡带作品名', !!artworkLabel && artworkLabel.textContent === '作品1',
        artworkLabel && artworkLabel.textContent);
    check('作品卡按相册路径可识别', artworkCard.dataset.path === 'A/作品1',
        artworkCard.dataset.path);

    // 单图卡：正常单图（url 进 dataset，与目录层一致）
    const singleCard = grid.children[1];
    check('单图卡带原图地址与缩略图',
        singleCard.dataset.url === 'B/单图1.jpg'
        && singleCard.children.some((child) => child.tagName === 'IMG'),
        JSON.stringify(singleCard.dataset));

    check('灯箱序列是本页 all_images（偏移 0）',
        iv.currentAllOffset === 0 && iv.currentAllImages.length === 6,
        JSON.stringify({ offset: iv.currentAllOffset, length: iv.currentAllImages.length }));
    check('currentImages 只算散图', iv.currentImages.length === 1,
        String(iv.currentImages.length));

    // ---- 2. 点作品卡 / 单图卡：灯箱起始下标 = 前面各单元张数累加 ----
    iv.lightbox.shown.length = 0;
    grid.children[0].click();
    const firstOpen = iv.lightbox.shown.at(-1);
    check('点作品卡：灯箱序列是本页 all_images',
        !!firstOpen && firstOpen.items === iv.currentAllImages && firstOpen.items.length === 6);
    check('点第一个作品卡：起始下标 0（前面没有单元）', firstOpen.index === 0, String(firstOpen.index));

    grid.children[1].click();
    const secondOpen = iv.lightbox.shown.at(-1);
    check('点单图卡：起始下标 = 前面作品张数累加（3）', secondOpen.index === 3, String(secondOpen.index));

    grid.children[2].click();
    const thirdOpen = iv.lightbox.shown.at(-1);
    check('点第二个作品卡：起始下标 = 3 张 + 1 张单图（4）', thirdOpen.index === 4, String(thirdOpen.index));

    const chrome = env.chrome();
    check('状态栏区分单元数与张数：共 3 项 · 6 张图片',
        chrome.status.text === '共 3 项 · 6 张图片', chrome.status.text);
    check('状态栏 state=ok', chrome.status.state === 'ok', chrome.status.state);
    check('搜索占位改成「搜索当前页图片…」',
        chrome.search.placeholder === '搜索当前页图片…', chrome.search.placeholder);
    check('分页按单元总数渲染',
        iv.pagination.pages.length === 1 && iv.pagination.pages[0].page === 1
        && iv.pagination.pages[0].total === 1, JSON.stringify(iv.pagination.pages));

    const switcherNow = findModeSwitch(albumsEl);
    check('图片层里 #iv-albums 只剩切换控件（没有相册卡片残留）',
        albumsEl.children.length === 1 && albumsEl.children[0] === switcherNow,
        String(albumsEl.children.length));
    check('图片层里控件切到「按图片」',
        switcherNow.children[1].getAttribute('aria-pressed') === 'true'
        && switcherNow.children[1].classList.contains('active')
        && switcherNow.children[0].getAttribute('aria-pressed') === 'false');

    // ---- 3. 点「按相册」退出图片层 ----
    env.calls.length = 0;
    switcherNow.children[0].click();
    await tick();
    check('点「按相册」回到相册层',
        iv.mode === 'albums' && iv._recentActive === false
        && iv.timelineMode === 'albums' && iv.currentPath === '',
        JSON.stringify({ mode: iv.mode, recent: iv._recentActive, timeline: iv.timelineMode }));
    check('退出后 #image-grid 清空且高度归零',
        grid.children.length === 0 && grid.style.height === '0',
        JSON.stringify({ children: grid.children.length, height: grid.style.height }));
    check('退出后 #pagination 清空', env.document.getElementById('pagination').children.length === 0);
    check('退出时不重复请求最近图片', recentCalls(env).length === 0);
    const switcherBack = findModeSwitch(albumsEl);
    check('退出后切换控件仍在且「按相册」按下',
        !!switcherBack && switcherBack.children[0].getAttribute('aria-pressed') === 'true'
        && switcherBack.children[0].classList.contains('active'));
    check('退出后状态栏回到相册统计',
        env.chrome().status.text.includes('个相册') && !env.chrome().status.text.includes('共 3 项'),
        env.chrome().status.text);
    check('退出后仍停在时间线视图', env.chrome().view === 'timeline', env.chrome().view);
}

// ---- 4. 壳「返回」也退出图片层，且不消费混合瀑布流的 navStack ------------------
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
    });
    const iv = makeTimelineViewer(env);
    await enterRecentLayer(env, iv);
    check('壳返回用例：已进入图片层', iv.mode === 'images' && iv._recentActive === true);
    // 图片层的位面本不该有 navStack 条目；塞一个哨兵证明返回不走那个分支。
    iv.navStack.push({ path: '哨兵目录', fromChildren: false, childParentPath: '' });

    iv._handleBack();
    await tick();
    check('壳返回退出「按图片」层',
        iv.mode === 'albums' && iv._recentActive === false && iv.timelineMode === 'albums',
        JSON.stringify({ mode: iv.mode, recent: iv._recentActive, timeline: iv.timelineMode }));
    check('壳返回不消费 navStack（不走混合瀑布流分支）', iv.navStack.length === 1,
        String(iv.navStack.length));
    check('壳返回的落点仍是时间线', env.chrome().view === 'timeline', env.chrome().view);
    check('壳返回后网格清空',
        env.document.getElementById('image-grid').children.length === 0);
    const switcher = findModeSwitch(env.document.getElementById('iv-albums'));
    check('壳返回后「按相册」按下',
        !!switcher && switcher.children[0].getAttribute('aria-pressed') === 'true');
}

// ---- 5. 在途的 list_recent_images 响应在切走之后不得重绘 ----------------------
{
    let resolveRecent;
    const pending = new Promise((resolve) => { resolveRecent = resolve; });
    const env = loadImageViewer({ list_recent_images: () => pending });
    const iv = makeTimelineViewer(env);
    const grid = await enterRecentLayer(env, iv);
    check('慢响应场景：最近图片请求已发出', recentCalls(env).length === 1);
    check('等待期间停在图片层', iv.mode === 'images' && iv._recentActive === true);

    modeButtons(env.document.getElementById('iv-albums'))[0].click();
    await tick();
    check('切走后回到相册层', iv.mode === 'albums' && iv._recentActive === false);
    const chromeBefore = env.chrome();
    const chromeCountBefore = env.chromeCount();

    resolveRecent(mixedPayload(1, 40, { total: 99, image_total: 200 }));
    await tick(20);
    check('迟到的最近图片响应没有重绘网格',
        grid.children.length === 0 && iv.currentItems.length === 0,
        JSON.stringify({ children: grid.children.length, items: iv.currentItems.length }));
    check('迟到的最近图片响应没有覆盖状态栏',
        env.chrome() === chromeBefore && env.chromeCount() === chromeCountBefore,
        JSON.stringify({ text: env.chrome().status.text, count: env.chromeCount() }));
    check('迟到响应没有把层级改回图片层',
        iv.mode === 'albums' && iv._recentActive === false);
}

// ---- 6. 图片层重载走 _reloadCurrentView / _reloadPage --------------------------
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
        list_folder_items: (itemPath, page) => ({ items: [], total: 0, image_total: 0, page }),
        get_settings: () => ({ row_height: 200, per_page: 40 }),
        delete_files: () => ({ deleted: ['B/单图1.jpg'], errors: [] }),
    });
    const iv = makeTimelineViewer(env);
    await enterRecentLayer(env, iv);

    env.calls.length = 0;
    await iv._reloadCurrentView();
    let calls = recentCalls(env);
    check('_reloadCurrentView 在图片层请求最近图片',
        calls.length === 1 && calls[0].args[0] === 1, JSON.stringify(calls.map((call) => call.args)));
    check('_reloadCurrentView 在图片层不请求目录列表',
        !env.calls.some((call) => call.method === 'list_folder_items'));

    env.calls.length = 0;
    await iv._reloadPage(2);
    calls = recentCalls(env);
    check('_reloadPage 在图片层请求最近图片的指定页',
        calls.length === 1 && calls[0].args[0] === 2, JSON.stringify(calls.map((call) => call.args)));

    // 普通目录图片层不受影响：仍走 list_folder_items 的当前页。
    const plain = new env.ImageViewer();
    plain.mode = 'images';
    plain.currentView = 'albums';
    plain.currentPath = 'A';
    plain.currentPage = 2;
    plain.currentSettings = { per_page: 40, row_height: 200 };
    plain.currentRowHeight = 200;
    plain.pagination = { render() { } };
    env.calls.length = 0;
    await plain._reloadCurrentView();
    const folderCalls = env.calls.filter((call) => call.method === 'list_folder_items');
    check('普通图片层重载仍走 list_folder_items 的当前页',
        folderCalls.length === 1 && folderCalls[0].args[0] === 'A' && folderCalls[0].args[1] === 2,
        JSON.stringify(folderCalls.map((call) => call.args)));
    check('普通图片层重载不请求最近图片', recentCalls(env).length === 0);

    // 删除图片后的重载同样走当前层的来源。
    env.calls.length = 0;
    iv.selectedImages.add('B/单图1.jpg');
    await iv.deleteSelectedImages();
    check('删除后重载走最近图片而不是目录列表',
        recentCalls(env).length === 1
        && !env.calls.some((call) => call.method === 'list_folder_items'),
        JSON.stringify(env.calls.map((call) => call.method)));
}

// ---- 7. 状态栏与分页：纯单图「共 N 张图片」、partial 的可翻阅范围 ----------
{
    const singleEnv = loadImageViewer({
        list_recent_images: (page, perPage) => singleImagesPayload(page, perPage),
    });
    const singleIv = makeTimelineViewer(singleEnv);
    await enterRecentLayer(singleEnv, singleIv);
    check('全为单图时状态栏是「共 2 张图片」',
        singleEnv.chrome().status.text === '共 2 张图片', singleEnv.chrome().status.text);

    const partialEnv = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage, {
            partial: true, reach: 3,
        }),
    });
    const partialIv = makeTimelineViewer(partialEnv);
    await enterRecentLayer(partialEnv, partialIv);
    const partialChrome = partialEnv.chrome();
    check('partial 时状态 state=warn', partialChrome.status.state === 'warn', partialChrome.status.state);
    check('partial 文案给出预算内的实际条数',
        partialChrome.status.text.includes('共 3 项 · 6 张图片')
        && partialChrome.status.text.includes('仅列出最近 3 项'),
        partialChrome.status.text);
    check('has_next 为假时不给「下一页」',
        partialIv.pagination.pages.length === 1 && partialIv.pagination.pages[0].total === 1,
        JSON.stringify(partialIv.pagination.pages));

    // partial（排序依据不完整）但这一页填满了：仍给下一页，不能把预算当成列表边界
    const partialNextEnv = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage, {
            partial: true, reach: 40, has_next: true,
        }),
    });
    const partialNextIv = makeTimelineViewer(partialNextEnv);
    await enterRecentLayer(partialNextEnv, partialNextIv);
    check('partial 但页填满时仍给后续页码',
        partialNextIv.pagination.pages.length === 1
        && partialNextIv.pagination.pages[0].total === 6,
        JSON.stringify(partialNextIv.pagination.pages));

    // 非 partial 且还没到边界：一次给出后 5 页，第 1 页不再是「1 2」两格
    const pagedEnv = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage, {
            total: 900, image_total: 1500, page, has_next: true,
        }),
    });
    const pagedIv = makeTimelineViewer(pagedEnv);
    await enterRecentLayer(pagedEnv, pagedIv);
    check('第 1 页一次给出后 5 页（1 → 6）',
        pagedIv.pagination.pages.length === 1 && pagedIv.pagination.pages[0].total === 6,
        JSON.stringify(pagedIv.pagination.pages));

    // 翻到第 4 页：列表里 90 个单元只有 3 页，第 4 页为空，但空态要说明"没有更多图片"
    const beyondEnv = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage, {
            total: 90, image_total: 150, items: [], all_images: [], page,
        }),
    });
    const beyondIv = makeTimelineViewer(beyondEnv);
    beyondIv._recentActive = true;
    beyondIv.mode = 'images';
    beyondIv.currentSettings = { per_page: 40, row_height: 200 };
    const beyondGrid = beyondEnv.document.getElementById('image-grid');
    await beyondIv.loadRecentImages(4);
    check('页码超出整库时是「没有更多图片」而不是「最近没有图片」',
        beyondGrid.innerHTML.includes('没有更多图片')
        && !beyondGrid.innerHTML.includes('最近没有图片'),
        beyondGrid.innerHTML.slice(0, 80));

    // 翻过预算边界：先回落到真实最后一页（有内容），不再停在空页
    const overEnv = loadImageViewer({
        list_recent_images: (page, perPage) => (page <= 2
            ? mixedPayload(page, perPage, { partial: true, reach: 80, page })
            : mixedPayload(page, perPage, {
                partial: true, reach: 80, items: [], all_images: [], page,
            })),
    });
    const overIv = makeTimelineViewer(overEnv);
    overIv._recentActive = true;
    overIv.mode = 'images';
    overIv.currentSettings = { per_page: 40, row_height: 200 };
    const overGrid = overEnv.document.getElementById('image-grid');
    await overIv.loadRecentImages(3);
    const overCalls = recentCalls(overEnv).map((call) => call.args[0]);
    check('超出预算边界时回落到真实最后一页（recovery 请求第 2 页）',
        overCalls.includes(2) && overCalls[overCalls.length - 1] === 2,
        JSON.stringify(overCalls));
    check('回落后的页面有内容', overGrid.children.length === 3, String(overGrid.children.length));
    check('回落后的分页停在真实最后一页',
        overIv.pagination.pages.length === 1 && overIv.pagination.pages[0].total === 2,
        JSON.stringify(overIv.pagination.pages));

    // 防御分支：回落目标也排不出内容时，给出扫描上限而不是空白
    const overEmptyEnv = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage, {
            partial: true, reach: 80, items: [], all_images: [], page,
        }),
    });
    const overEmptyIv = makeTimelineViewer(overEmptyEnv);
    overEmptyIv._recentActive = true;
    overEmptyIv.mode = 'images';
    overEmptyIv.currentSettings = { per_page: 40, row_height: 200 };
    const overEmptyGrid = overEmptyEnv.document.getElementById('image-grid');
    await overEmptyIv.loadRecentImages(3);
    check('预算外翻页给「已到扫描上限」并说明实际条数',
        overEmptyGrid.innerHTML.includes('已到扫描上限')
        && overEmptyGrid.innerHTML.includes('80'),
        overEmptyGrid.innerHTML.slice(0, 120));
    check('预算外空态仍保留分页（可点回第 1 页）',
        overEmptyIv.pagination.pages.length === 1
        && overEmptyIv.pagination.pages[0].total === 2,
        JSON.stringify(overEmptyIv.pagination.pages));

    const emptyEnv = loadImageViewer({
        list_recent_images: () => mixedPayload(1, 40, {
            items: [], all_images: [], total: 0, image_total: 0, reach: 0,
        }),
    });
    const emptyIv = makeTimelineViewer(emptyEnv);
    const emptyGrid = await enterRecentLayer(emptyEnv, emptyIv);
    check('空态文案保持「最近没有图片」',
        emptyGrid.innerHTML.includes('最近没有图片') && emptyGrid.children.length === 0,
        emptyGrid.innerHTML.slice(0, 80));
    check('total 为 0 时不渲染分页', emptyIv.pagination.pages.length === 0,
        JSON.stringify(emptyIv.pagination.pages));
}

// ---- 8. 幂等：同一模式重复点击不重复发请求 ------------------------------------
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
    });
    const iv = makeTimelineViewer(env);
    const albumsEl = env.document.getElementById('iv-albums');
    await enterRecentLayer(env, iv);
    const scrollDepth = iv.scrollStack.length;
    env.calls.length = 0;
    modeButtons(albumsEl)[1].click();
    await tick();
    check('重复点「按图片」不重复请求', recentCalls(env).length === 0,
        JSON.stringify(env.calls.map((call) => call.method)));
    check('重复点「按图片」仍停在图片层', iv.mode === 'images' && iv._recentActive === true);
    check('重复点「按图片」不重复记滚动位置', iv.scrollStack.length === scrollDepth,
        String(iv.scrollStack.length));

    modeButtons(albumsEl)[0].click();
    await tick();
    env.calls.length = 0;
    modeButtons(albumsEl)[0].click();
    await tick();
    check('重复点「按相册」不重复请求且仍在相册层',
        env.calls.length === 0 && iv.mode === 'albums' && iv._recentActive === false,
        JSON.stringify(env.calls.map((call) => call.method)));
}

// ---- 9. 壳重发同一视图：仍回到记住的「按图片」层 ------------------------------
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
        list_albums: () => ({ albums: ALBUMS, config: {} }),
    });
    const iv = makeTimelineViewer(env);
    await enterRecentLayer(env, iv);
    check('视图重发用例：已进入图片层', iv.mode === 'images' && iv._recentActive === true);

    env.calls.length = 0;
    iv._applyHostView('timeline');
    await tick();
    check('壳重发时间线视图后仍在「按图片」层',
        iv.mode === 'images' && iv._recentActive === true && iv.timelineMode === 'images',
        JSON.stringify({ mode: iv.mode, recent: iv._recentActive }));
    check('重发视图重新请求最近图片',
        recentCalls(env).length === 1, JSON.stringify(env.calls.map((call) => call.method)));

    iv._applyHostView('albums');
    await tick();
    check('切到「全部相册」复位 _recentActive',
        iv._recentActive === false && iv.mode === 'albums' && iv.currentView === 'albums',
        JSON.stringify({ mode: iv.mode, recent: iv._recentActive, view: iv.currentView }));
}

// ---- 10. 搜索过滤：作品卡与单图都参与，过滤后灯箱起点仍按张数累加 --------------
{
    const env = loadImageViewer({
        list_recent_images: (page, perPage) => mixedPayload(page, perPage),
    });
    const iv = makeTimelineViewer(env);
    const grid = await enterRecentLayer(env, iv);

    iv._onQuery('作品1');
    check('搜索能过滤出作品卡', grid.children.length === 1 && iv.searchQuery === '作品1',
        String(grid.children.length));
    iv._onQuery('单图1');
    check('搜索能过滤出单图卡', grid.children.length === 1, String(grid.children.length));

    // '1' 命中作品1（name/path）与单图1（name），不命中作品2：两卡的灯箱起点
    // 由过滤时按张数累加出的映射给出（作品1 = 0，单图1 = 3）。
    iv._onQuery('1');
    check('搜索同时命中作品卡与单图卡', grid.children.length === 2, String(grid.children.length));
    iv.lightbox.shown.length = 0;
    grid.children[1].click();
    const filteredOpen = iv.lightbox.shown.at(-1);
    check('过滤后点单图卡：灯箱起点仍是前面作品张数（3）',
        filteredOpen.index === 3, String(filteredOpen.index));

    iv._onQuery('');
    check('清空搜索恢复本页全部单元', grid.children.length === 3, String(grid.children.length));
}

console.log('image-viewer 时间线「按图片」用例全部通过');
