// Shell 共享目录组件（shell/frontend/public/shell/folder-picker.js）的回归用例。
//
// 为什么单独守它：image-viewer / media-player / manga-library / novel-reader 的
// 「多位置文件夹」界面都走这一份实现（`window.FolderPicker.createList`），而它渲染的
// 是**磁盘目录名与完整路径** —— 名字里带 `<`/`"`/`&` 的目录在 Linux 上完全合法
// （Windows 文件名禁用这几个字符），未转义就是一个注入点。这里把真实内核
// `Utils.escapeHtml` 装进沙箱，喂恶意数据后断言产物里没有原始载荷。
//
// 本文件曾被 tests/js/image_viewer_roots_list.mjs 引用却不存在（引用了一个不存在的
// 文件，等于这条守护一直是空的），所以它同时是"引用即存在"的锁。
//
// 用法：node tests/js/shell_folder_picker.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const SHELL_PUBLIC = join(here, '..', '..', 'shell', 'frontend', 'public', 'shell');
const PICKER = join(SHELL_PUBLIC, 'folder-picker.js');
const BASE = join(SHELL_PUBLIC, 'base.js');

const PAYLOAD = '\'"><img src=x onerror=alert(1)>&';

let passed = 0;
// 用例可以是 async 的：`check` 会 await 它。
//
// 这里必须往后兼容地 await，而不是"异步用例自己 catch"：`vm` 沙箱里造出来的对象
// 与宿主的 `Object.prototype` **不是同一个 realm**，`assert/strict` 的 deepEqual
// 会因此判不相等（实测踩到）。所以下面跨 realm 的返回值一律**逐字段**断言；
// 而没被 await 的异步断言会变成 unhandled rejection 把整个脚本带崩。
const check = async (name, fn) => {
    try {
        await fn();
        passed += 1;
        console.log(`  PASS  ${name}`);
    } catch (err) {
        console.error(`  FAIL  ${name}  — ${err.message}`);
        process.exitCode = 1;
    }
};

/** 从 base.js 里截出 `window.Utils = { ... }` 并求值，拿到真实内核转义实现。 */
function loadKernelUtils(sandbox) {
    const source = readFileSync(BASE, 'utf8');
    const start = source.indexOf('window.Utils =');
    assert.ok(start >= 0, 'base.js 里找不到 window.Utils 定义');
    const open = source.indexOf('{', start);
    let depth = 0;
    let end = -1;
    for (let i = open; i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) { end = i + 1; break; }
        }
    }
    assert.ok(end > 0, 'base.js 的 window.Utils 大括号不配对');
    vm.runInContext(`window.Utils = ${source.slice(open, end)};`, sandbox);
    return sandbox.window.Utils;
}

/** 够用的 DOM 替身：记录 innerHTML、把 append 的子节点收进 children。 */
function makeElement(tag = 'div') {
    const el = {
        tagName: String(tag).toUpperCase(),
        className: '',
        innerHTML: '',
        textContent: '',
        value: '',
        disabled: false,
        style: { setProperty() { }, removeProperty() { } },
        dataset: {},
        children: [],
        classList: { add() { }, remove() { }, toggle() { }, contains: () => false },
        append(...nodes) { el.children.push(...nodes); },
        appendChild(node) { el.children.push(node); return node; },
        // 组件用 insertAdjacentHTML('beforeend', …) 追加「网络位置」按钮（默认才有）
        insertAdjacentHTML(position, html) { el.innerHTML += html; },
        remove() { },
        addEventListener() { },
        removeEventListener() { },
        setAttribute() { },
        getAttribute: () => null,
        querySelector: (selector) => makeElement(selector === 'input' ? 'input' : 'div'),
        querySelectorAll: () => [],
    };
    return el;
}

function makeSandbox() {
    const sandbox = {
        console,
        document: {
            createElement: makeElement,
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            body: makeElement('body'),
            documentElement: makeElement('html'),
            addEventListener() { },
        },
        Bridge: { callSystem: async () => ({ path: '', parent: null, entries: [] }) },
        Toast: { warning() { }, info() { }, error() { }, success() { } },
        // 组件会给 window 挂 message 监听（网络位置回填），沙箱必须能记录/移除
        addEventListener() { },
        removeEventListener() { },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.setTimeout = setTimeout;
    sandbox.clearTimeout = clearTimeout;
    return sandbox;
}

const sandbox = makeSandbox();
vm.createContext(sandbox);
loadKernelUtils(sandbox);
vm.runInContext(readFileSync(PICKER, 'utf8'), sandbox, { filename: 'folder-picker.js' });
const FolderPicker = sandbox.window.FolderPicker;

console.log('[shell] 共享目录组件');

await check('组件挂在 window.FolderPicker 上并导出约定成员', () => {
    assert.ok(FolderPicker, 'window.FolderPicker 不存在');
    for (const name of ['DRIVES_SENTINEL', 'KIND_LABELS', 'normalize', 'openDirBrowser', 'createList']) {
        assert.ok(name in FolderPicker, `缺少导出 ${name}`);
    }
    assert.equal(typeof FolderPicker.createList, 'function');
});

await check('normalize 去掉首尾空白与末尾分隔符', () => {
    assert.equal(FolderPicker.normalize('  D:\\图库\\  '), 'D:\\图库');
    assert.equal(FolderPicker.normalize('/home/me/pics///'), '/home/me/pics');
    assert.equal(FolderPicker.normalize(null), '');
});

await check('渲染目录名/路径/占位符时转义载荷（文本与属性都不逃逸）', () => {
    const list = FolderPicker.createList({
        paths: [],
        placeholder: PAYLOAD,
        emptyText: PAYLOAD,
        labels: () => PAYLOAD,
    });
    const listBox = list.element.children[0];
    assert.ok(listBox, 'createList 没有产出列表容器');
    // 空列表 → 渲染 emptyText
    assert.ok(!listBox.innerHTML.includes('<img src=x'), `空态未转义：${listBox.innerHTML}`);
    assert.ok(listBox.innerHTML.includes('&lt;img'), '空态应产出实体化后的载荷');

    list.addPath(PAYLOAD);
    assert.deepEqual(list.getPaths(), [PAYLOAD]);
    const html = listBox.innerHTML;
    assert.ok(!html.includes('<img src=x'), `列表未转义：${html}`);
    assert.ok(!/['"]><img/.test(html), `载荷逃出了属性：${html}`);
    assert.ok(html.includes('&quot;') && html.includes('&lt;img'), '值没有被实体化');
    // 行内 title 属性也必须转义（悬停显示完整路径）
    assert.ok(!html.includes(`title="${PAYLOAD}"`), 'title 属性里的路径未转义');

    // 输入框占位符来自插件 schema，同样是外部数据
    const addRow = list.element.children[1];
    assert.ok(!addRow.innerHTML.includes('<img src=x'), `占位符未转义：${addRow.innerHTML}`);
    assert.ok(addRow.innerHTML.includes('&lt;img'), '占位符应产出实体化后的载荷');
});

await check('addPath 去重并拒绝空值', () => {
    const list = FolderPicker.createList({ paths: [] });
    assert.equal(list.addPath('   '), false, '空路径应被拒绝');
    assert.equal(list.addPath('/data/a/'), true);
    assert.equal(list.addPath('/data/a'), false, '同一个目录（尾斜杠不同）应被去重');
    assert.deepEqual(list.getPaths(), ['/data/a']);
});

await check('setPaths 覆盖式替换并重新渲染', () => {
    const list = FolderPicker.createList({ paths: ['/old'] });
    list.setPaths(['/a', '/b']);
    assert.deepEqual(list.getPaths(), ['/a', '/b']);
    const listBox = list.element.children[0];
    assert.ok(listBox.innerHTML.includes('/a') && listBox.innerHTML.includes('/b'));
    assert.ok(!listBox.innerHTML.includes('/old'), '旧路径应被移除');
});

await check('single 模式只保留一个目录，且每次变化都通知调用方', () => {
    // 单值 directory 设置（如 group-mesh 下载目录 / pixiv 下载目录）用这个模式：
    // 多行在保存时只会取第一行，界面就不能允许加出第二行。
    const changes = [];
    const list = FolderPicker.createList({
        paths: ['/a', '/b'],
        single: true,
        onChange: (paths) => changes.push(paths.slice()),
    });
    assert.deepEqual(list.getPaths(), ['/a'], '初始值里超出的行应被裁掉');
    assert.equal(list.addPath('/c'), true);
    assert.deepEqual(list.getPaths(), ['/c'], 'single 模式用新路径替换旧路径');
    assert.equal(changes.length, 1, 'addPath 应触发 onChange');
    assert.deepEqual(changes[0], ['/c']);
    list.setPaths(['/d', '/e']);
    assert.deepEqual(list.getPaths(), ['/d']);
    assert.equal(changes.length, 2, 'setPaths 也应触发 onChange');
});

await check('导出含网络位置相关的成员（提供方发现 + 回填协议）', () => {
    for (const name of ['NETWORK_MESSAGE_TYPE', 'loadNetworkProviders', 'readProviderMessage']) {
        assert.ok(name in FolderPicker, `缺少导出 ${name}`);
    }
    assert.equal(FolderPicker.NETWORK_MESSAGE_TYPE, 'omnibox:network-location');
});

await check('loadNetworkProviders 只认带 embedUrl 的提供方，宿主接口不可用时返回空表', async () => {
    const original = sandbox.Bridge.callSystem;
    const rows = [
        { placement: 'network-location', label: '团体组网', embedUrl: '/plugins/group-mesh/frontend/nl.html' },
        { placement: 'network-location', label: '缺 embedUrl' },
        null,
    ];
    const calls = [];
    sandbox.Bridge.callSystem = async (...args) => { calls.push(args); return rows; };
    const providers = await FolderPicker.loadNetworkProviders();
    // 跨 realm 的数组不能 deepEqual：逐项断言
    assert.equal(providers.length, 1, `应当只保留有 embedUrl 的提供方：${JSON.stringify(providers)}`);
    assert.equal(providers[0].label, '团体组网');
    // 必须按 placement 过滤、且 host 传 null（"任意宿主"）—— 组件出现在各插件的设置里
    assert.deepEqual(calls, [['system_get_plugin_extensions', null, 'network-location']]);

    sandbox.Bridge.callSystem = async () => { throw new Error('宿主接口不可用'); };
    const failed = await FolderPicker.loadNetworkProviders();
    assert.equal(failed.length, 0, '接口异常时应当返回空表而不是抛出');
    sandbox.Bridge.callSystem = original;
});

await check('回填协议校验来源与形状（防任意同源页面往列表里塞路径）', () => {
    const TYPE = FolderPicker.NETWORK_MESSAGE_TYPE;
    const frameWindow = { name: 'provider-frame' };
    const other = { name: 'evil' };
    const good = (data, source) => FolderPicker.readProviderMessage({ data, source }, frameWindow);

    const picked = good({ type: TYPE, action: 'picked', path: '  D:\\镜像\\设备A\\  ' }, frameWindow);
    assert.equal(picked.action, 'picked');
    assert.equal(picked.path, 'D:\\镜像\\设备A', '首尾空白与末尾分隔符应当被规范化');
    assert.equal(picked.label, '', '没给 label 时应当是空串而不是 undefined');

    const labelled = good({ type: TYPE, action: 'picked', path: '/mnt/a', label: '团体组网 · 设备A' }, frameWindow);
    assert.equal(labelled.label, '团体组网 · 设备A');
    assert.equal(good({ type: TYPE, action: 'cancelled' }, frameWindow).action, 'cancelled');

    assert.equal(good({ type: TYPE, action: 'picked', path: '/mnt/a' }, other), null,
        '来自别的窗口的消息必须忽略');
    assert.equal(good({ type: 'other:event', action: 'picked', path: '/mnt/a' }, frameWindow), null,
        '别的消息类型必须忽略');
    assert.equal(good({ type: TYPE, action: 'picked', path: '   ' }, frameWindow), null,
        '空路径必须忽略');
    assert.equal(good({ type: TYPE, action: '删除所有目录' }, frameWindow), null,
        '未知 action 必须忽略');
    assert.equal(FolderPicker.readProviderMessage({ data: { type: TYPE, action: 'picked', path: '/a' } }, null),
        null, '拿不到来源窗口时一律拒绝');
});

await check('添加行含「网络位置」入口，且不改变原有三件套', () => {
    const list = FolderPicker.createList({ paths: [] });
    const addRow = list.element.children[1];
    assert.ok(addRow.innerHTML.includes('data-act="network"'), '缺少网络位置按钮');
    assert.ok(addRow.innerHTML.includes('data-act="browse"'), '浏览按钮不应消失');
    assert.ok(addRow.innerHTML.includes('data-act="add"'), '添加按钮不应消失');
});

await check('localOnly 的字段不给「网络位置」入口（该目录本身就是产物）', () => {
    // 背景：group-mesh 的「远端下载目录」是取回文件的落点，在那里选"网络位置"
    // 等于用取回的中间目录当下载目录，语义不成立 —— schema 声明 local_only 后
    // 组件不能把入口渲染出来。三件套仍要在。
    const list = FolderPicker.createList({ paths: [], localOnly: true });
    const addRow = list.element.children[1];
    assert.ok(!addRow.innerHTML.includes('data-act="network"'),
        `localOnly 时不应有网络位置按钮：${addRow.innerHTML}`);
    assert.ok(addRow.innerHTML.includes('data-act="browse"'), '浏览按钮不应消失');
    assert.ok(addRow.innerHTML.includes('data-act="add"'), '添加按钮不应消失');
    // 默认（未声明）仍然是有的，避免"改错了方向"也通过
    const normal = FolderPicker.createList({ paths: [] });
    assert.ok(normal.element.children[1].innerHTML.includes('data-act="network"'),
        '未声明 localOnly 时应保留网络位置入口');
});

await check('提供方菜单的一行转义 icon/label（两者都来自插件声明）', () => {
    // 多提供方时组件会弹这个菜单：里面的文字是插件 `get_extensions()` 给的，
    // 属于外部数据，必须走 escapeHtml（转义门禁的登记表也按这一处登记）。
    const html = FolderPicker.providerRow({ icon: PAYLOAD, label: PAYLOAD }, 3);
    assert.ok(!html.includes('<img src=x'), `提供方行未转义：${html}`);
    assert.ok(!/['"]><img/.test(html), `载荷逃出了属性：${html}`);
    assert.ok(html.includes('&quot;') && html.includes('&lt;img'), '值没有被实体化');
    assert.ok(html.includes('data-index="3"'), '索引要落进 data-index（选中时按它取回提供方）');
    // 缺 icon / label 时回落默认值，不能渲染出 "undefined"。
    // 默认图标现在是图标集里的 `icon:globe`（由壳内联的 sprite 渲染），不再是 emoji：
    // 沙箱里补一个最小的 Icons 桩，验证落到的是图标标记而不是空串。
    sandbox.window.Icons = {
        html: (name) => `<svg class="obx-icon"><use href="#${String(name).slice(5)}"></use></svg>`,
    };
    const fallback = FolderPicker.providerRow({}, 0);
    assert.ok(fallback.includes('#globe') && fallback.includes('网络位置'), `默认值缺失：${fallback}`);
    assert.ok(!fallback.includes('undefined'), `默认值缺失：${fallback}`);
    // 反过来：没有图标集时（脱离壳单独打开）也不能渲染出 undefined 或残留占位
    delete sandbox.window.Icons;
    const noIcons = FolderPicker.providerRow({}, 0);
    assert.ok(!noIcons.includes('undefined') && noIcons.includes('网络位置'),
        `图标集缺失时回退异常：${noIcons}`);
});

// ---------- 目录浏览弹窗（openDirBrowser）：真实渲染路径 ----------
//
// 前几组用例只跑 createList；目录浏览弹窗的失败分支（后端抛错 / data.error）是
// "点『选择当前目录』拿到旧路径"这类缺陷的现场，必须把弹窗真正跑起来。下面的 DOM
// 替身按选择器缓存节点，并把 list 的 innerHTML 解析成可点击条目（真实浏览器行为的
// 最小复刻：属性实体在解析时解码）。

class HtmlElementStub {}

function decodeEntities(value) {
    return String(value)
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
}

function makeRichElement(tag = 'div') {
    const el = new HtmlElementStub();
    const handlers = {};
    const attrs = {};
    const cached = new Map();
    let html = '';
    let entries = [];
    Object.assign(el, {
        tagName: String(tag).toUpperCase(),
        className: '',
        textContent: '',
        value: '',
        disabled: false,
        tabIndex: -1,
        dataset: {},
        children: [],
        parentNode: null,
        isConnected: true,
        style: { setProperty() { }, removeProperty() { } },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        removeEventListener(type, fn) {
            handlers[type] = (handlers[type] || []).filter((item) => item !== fn);
        },
        click() {
            if (el.disabled) return;
            (handlers.click || []).slice().forEach((fn) => fn({ target: el }));
        },
        setAttribute(key, value) { attrs[key] = String(value); },
        getAttribute(key) { return key in attrs ? attrs[key] : null; },
        removeAttribute(key) { delete attrs[key]; },
        focus() { el.__focused = true; },
        appendChild(child) {
            child.parentNode = el;
            el.children.push(child);
            return child;
        },
        remove() {
            if (el.parentNode) el.parentNode.children = el.parentNode.children.filter((c) => c !== el);
            el.parentNode = null;
            el.isConnected = false;
        },
        querySelector(selector) {
            if (!cached.has(selector)) {
                const child = makeRichElement('div');
                child.parentNode = el;
                el.children.push(child);
                cached.set(selector, child);
            }
            return cached.get(selector);
        },
        querySelectorAll(selector) {
            if (selector === '.iv-dirbrowser-item[data-path]') return entries.slice();
            return [];
        },
    });
    Object.defineProperty(el, 'innerHTML', {
        get: () => html,
        set: (value) => {
            html = String(value);
            entries = [];
            const re = /data-path="([^"]*)"/g;
            let match;
            while ((match = re.exec(html)) !== null) {
                const item = makeRichElement('div');
                item.className = 'iv-dirbrowser-item';
                item.dataset.path = decodeEntities(match[1]);
                item.parentNode = el;
                el.children.push(item);
                entries.push(item);
            }
        },
    });
    return el;
}

function makeRichDocument() {
    const body = makeRichElement('body');
    return {
        body,
        activeElement: null,
        readyState: 'complete',
        createElement: (tag) => makeRichElement(tag),
        addEventListener() { },
        removeEventListener() { },
        contains: (node) => node === body || (body.children || []).includes(node),
        documentElement: makeRichElement('html'),
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

/** 用真实 folder-picker.js 起一个能跑 openDirBrowser 的沙箱；routes 按路径给后端返回。 */
function makeBrowserEnv(routes) {
    const document = makeRichDocument();
    const calls = [];
    const sandbox = {
        console,
        document,
        HTMLElement: HtmlElementStub,
        Bridge: {
            callSystem: async (method, path) => {
                calls.push([method, path]);
                const route = routes[path];
                if (typeof route === 'function') return route();
                if (route instanceof Error) throw route;
                if (route === undefined) throw new Error('no fixture: ' + path);
                return route;
            },
        },
        Toast: { warning() { }, info() { }, error() { }, success() { } },
        addEventListener() { },
        removeEventListener() { },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.setTimeout = setTimeout;
    sandbox.clearTimeout = clearTimeout;
    vm.createContext(sandbox);
    loadKernelUtils(sandbox);
    vm.runInContext(readFileSync(PICKER, 'utf8'), sandbox, { filename: 'folder-picker.js' });
    return { picker: sandbox.window.FolderPicker, document, calls };
}

const tick = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
const overlayOf = (env) => env.document.body.children.find(
    (child) => String(child.className).includes('iv-picker-modal'));

const ROUTES = {
    // 盘符层：后端把「我的电脑」的 entry 也按目录返回
    '': { path: '', parent: null, entries: [{ path: 'D:\\图库', name: '图库', kinds: [] }] },
    'D:\\图库': {
        path: 'D:\\图库', parent: 'D:\\',
        entries: [{ path: 'D:\\图库\\a', name: 'a', kinds: [] }],
    },
    'D:\\图库\\a': new Error('目录已删除'),
};

await check('目录浏览器：禁用态按钮置灰、可用态不置灰（我的电脑按钮）', async () => {
    const env = makeBrowserEnv(ROUTES);
    const opening = env.picker.openDirBrowser('');
    opening.then(() => { });
    await tick();

    const overlay = overlayOf(env);
    assert.ok(overlay, 'openDirBrowser 没有把弹窗挂到 body');
    const drivesBtn = overlay.querySelector('[data-act="drives"]');
    assert.equal(drivesBtn.disabled, true, '盘符层里「我的电脑」就是当前层，应禁用');
    assert.equal(drivesBtn.style.opacity, '0.45', '禁用态必须置灰');

    // 点进一个真实目录：按钮恢复可用，且不能再留在"置灰"外观
    const listEl = overlay.querySelector('[data-act="list"]');
    listEl.querySelectorAll('.iv-dirbrowser-item[data-path]')[0].click();
    await tick();
    assert.equal(drivesBtn.disabled, false, '离开盘符层后按钮应可用');
    assert.equal(drivesBtn.style.opacity, '1', '可用态不该显灰');

    overlay.querySelector('[data-act="cancel"]').click();
    await opening;
});

await check('目录导航失败后清空当前目录，不再允许「选择当前目录」返回旧路径', async () => {
    const env = makeBrowserEnv(ROUTES);
    const resolved = [];
    const opening = env.picker.openDirBrowser('D:\\图库');
    opening.then((value) => resolved.push(value));
    await tick();

    const overlay = overlayOf(env);
    const selectBtn = overlay.querySelector('[data-act="select"]');
    const listEl = overlay.querySelector('[data-act="list"]');
    assert.equal(selectBtn.disabled, false, '前置：成功进入目录后可以选它');

    // 点一个加载会抛错的子目录
    listEl.querySelectorAll('.iv-dirbrowser-item[data-path]')[0].click();
    await tick();
    assert.ok(listEl.innerHTML.includes('目录读取失败'), `失败文案缺失：${listEl.innerHTML}`);
    assert.equal(selectBtn.disabled, true, '导航失败后不能继续选目录');

    // 点「选择当前目录」不得拿着上一次成功的路径结算
    selectBtn.click();
    await tick();
    assert.deepEqual(resolved, [], '导航失败后点选择不应返回任何路径（更不能是旧路径）');

    overlay.querySelector('[data-act="cancel"]').click();
    await opening;
    assert.deepEqual(resolved, [null], '取消应结算为 null');
});

await check('后端以 data.error 报错时同样清空当前目录并禁用选择', async () => {
    const routes = {
        'D:\\图库': {
            path: 'D:\\图库', parent: 'D:\\',
            entries: [{ path: 'D:\\图库\\locked', name: 'locked', kinds: [] }],
        },
        // 错误响应里也可能回带 path（把失败的目录原样报回来）；无论带不带，
        // 这次导航都失败了 —— 不能把 path 当成"当前目录"选中。
        'D:\\图库\\locked': { error: PAYLOAD, path: 'D:\\图库\\locked', parent: 'D:\\图库' },
    };
    const env = makeBrowserEnv(routes);
    const opening = env.picker.openDirBrowser('D:\\图库');
    opening.then(() => { });
    await tick();

    const overlay = overlayOf(env);
    const selectBtn = overlay.querySelector('[data-act="select"]');
    const listEl = overlay.querySelector('[data-act="list"]');
    assert.equal(selectBtn.disabled, false, '前置：可以选当前目录');

    listEl.querySelectorAll('.iv-dirbrowser-item[data-path]')[0].click();
    await tick();
    assert.ok(!listEl.innerHTML.includes('<img src=x'), `错误文本未转义：${listEl.innerHTML}`);
    assert.ok(listEl.innerHTML.includes('&lt;img'), '错误文本应实体化');
    assert.equal(selectBtn.disabled, true, 'data.error 之后不能继续选目录');

    overlay.querySelector('[data-act="cancel"]').click();
    await opening;
});

console.log(`\nshell_folder_picker: ${passed} 项检查${process.exitCode ? '（有失败）' : '全部通过'}`);
