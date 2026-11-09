// image-viewer：子文件夹瓦片右下角的数量角标与排序模式无关。
//
// 背景：角标原先只在「Pixiv 排序支持」的瓦片下显示。普通排序（默认「修改时间倒序」）
// 里作品瓦片看不到页数，只能点进灯箱去数；而灯箱打开的是块首（= 封面 p0），
// 「一个作品有几页」于是完全不可见。现在数量已知就显示，与排序模式无关；
// 数量未知（占位瓦片、二级加载还没回来）时不显示，避免先亮一个 0。
//
// 用例驱动真实插件脚本（与 index.html 的装载顺序一致），只替身宿主提供的全局对象。
import assert from 'node:assert/strict';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';
import { readDeclaredScripts } from './script_load_contract.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = path.join(ROOT, 'plugins/image-viewer/frontend');

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

/** 装载 index.html 声明的全部插件脚本，返回构造器与一块可渲染的网格容器。 */
function load() {
    const { document, makeEl } = createDom();
    const origin = 'http://127.0.0.1:18080';
    const locationStub = {
        origin,
        href: `${origin}/plugins/image-viewer/frontend/index.html?content=1`,
    };
    document.location = locationStub;
    const sandbox = {
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        cancelAnimationFrame: (id) => clearTimeout(id),
        document,
        location: locationStub,
        localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
        Utils: { escapeHtml: (value) => String(value == null ? '' : value), debounce: (fn) => fn },
        Icons: { html: () => '<svg></svg>' },
        Toast: { success() { }, error() { }, warning() { }, info() { } },
        Motion: { retrigger() { }, stagger() { } },
        confirmDialog: async () => true,
        PluginLifecycle: { onHide() { }, onShow() { }, onDispose() { } },
        Bridge: {
            call: () => Promise.resolve({}),
            originalUrl: (value) => `/file?path=${encodeURIComponent(value)}`,
            thumbUrl: (value) => `/thumb?path=${encodeURIComponent(value)}`,
        },
        URL,
        URLSearchParams,
    };
    sandbox.window = sandbox;
    sandbox.parent = { location: { origin }, postMessage() { } };
    vm.createContext(sandbox);
    for (const { rel, source } of readDeclaredScripts(FRONTEND)) {
        vm.runInContext(source, sandbox, { filename: rel });
    }
    const grid = makeEl('div');
    grid.id = 'image-grid';
    grid.clientWidth = 900;
    return { ImageViewer: vm.runInContext('ImageViewer', sandbox), makeEl, grid };
}

const { ImageViewer, makeEl, grid } = load();
const iv = new ImageViewer();
iv.currentRowHeight = 200;
iv.selectedImages = new Set();

// 两张作品瓦片：一张数量已知（普通排序，use_time_name=false），一张占位未补齐
const items = [
    {
        type: 'album', path: '其他分类/73798/141236569', name: '141236569',
        cover: '其他分类/73798/141236569/141236569_p0.jpg', mtime: 2,
        width: 800, height: 600, image_count: 21, has_children: false,
        pending: false, total_count: 21, use_time_name: false,
    },
    {
        type: 'album', path: '作者A', name: '作者A', cover: '作者A/1/p0.jpg', mtime: 3,
        width: 800, height: 600, image_count: 0, has_children: true,
        pending: false, total_count: null, use_time_name: false,
    },
];
iv.currentItems = items;
iv.renderJustifiedLayout(items);

const [workCard, placeholderCard] = grid.children;
const workBadge = workCard && workCard.querySelector('.iv-count-badge');
check('普通排序下作品瓦片显示页数角标（use_time_name=false 也显示）',
    !!workBadge && String(workBadge.textContent) === '21',
    workBadge ? `text=${workBadge.textContent}` : '没有角标');
check('数量未知的瓦片不显示角标（不先亮一个 0）',
    !!placeholderCard && !placeholderCard.querySelector('.iv-count-badge'));

// 二级加载补齐：use_time_name 仍是 false，数量回来后应当补上角标
iv._applyAlbumTile(items[1], placeholderCard, {
    cover: '', total_count: 7, mtime: 3, use_time_name: false,
});
const filledBadge = placeholderCard.querySelector('.iv-count-badge');
check('二级加载补齐后按数量出现角标（与排序模式无关）',
    !!filledBadge && String(filledBadge.textContent) === '7',
    filledBadge ? `text=${filledBadge.textContent}` : '没有角标');

// 数量仍是未知（tile 回来了但计数为 null）：保持不显示
const unknownCard = makeEl('div');
iv._applyAlbumTile({ type: 'album', path: '未知', name: '未知' }, unknownCard,
    { cover: '', total_count: null, mtime: 0, use_time_name: false });
check('补齐时计数仍未知则不显示角标',
    !unknownCard.querySelector('.iv-count-badge'));

console.log('ok - image_viewer_grid_badge');
