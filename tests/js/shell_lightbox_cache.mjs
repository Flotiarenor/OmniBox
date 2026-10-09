// 灯箱「解码位图内存缓存」的无头验证脚本（由 test_shell_lightbox_cache_js.py 调用）。
//
// 背景：客户端持有文件字节 ≠ 显示时不卡。实测同一张 10 MB / 数千像素的原图，重复
// 打开有两种表现 —— 有时整张瞬出，有时**从上到下一行行刷**。后者是渲染进程把
// **解码后的位图**丢掉了，于是重新解码 + 渐进绘制；HTTP 缓存与系统页缓存都是无辜的
// （请求可能只回一个 304，位图仍要重建）。
//
// 保住位图的唯一现实做法是让持有它的 <img> 元素活着，所以灯箱多养一小池离屏
// <img>（`createLightbox().setDecodedLimit(n)`）。本脚本锁住三件事：
//   ① 上限生效：池子张数不超过设定值，超出时淘汰最久未用的；
//   ② 0 = 关闭：不再保留任何位图；
//   ③ 关灯箱即清空：不把内存留到下次打开。
// 用最小 DOM 桩（vm 沙箱）跑，不需要真浏览器。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const SHELL_BASE_JS = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../shell/frontend/public/shell/base.js');

let failures = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

// ---------- 极简 DOM ----------
function makeEl(tag = 'div') {
    const el = {
        tagName: tag, className: '', style: {}, dataset: {}, innerHTML: '',
        textContent: '', src: '', currentSrc: '', children: [],
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
            contains(c) { return this._s.has(c); },
        },
        addEventListener() { }, removeEventListener() { },
        querySelector() { return null },
        appendChild(c) { this.children.push(c); return c },
        cloneNode() { const copy = makeEl(tag); copy.src = this.src; copy.currentSrc = this.currentSrc; return copy },
        remove() { },
    };
    return el;
}

// 组件先 createElement('div') 再 el.innerHTML = 模板，然后 el.querySelector(...) 取
// 内部节点。桩必须让同一个元素对同一选择器稳定返回同一个对象：每次返回新对象时
// 事件绑定会拿到 null 而抛错。
const imgEl = makeEl('img');
const infoEl = makeEl('div');
function attachQuery(root) {
    const cache = new Map();
    root.querySelector = (sel) => {
        if (sel === '#lightbox-img') return imgEl;
        if (sel === '.lightbox-info') return infoEl;
        if (!cache.has(sel)) cache.set(sel, makeEl('div'));
        return cache.get(sel);
    };
    return root;
}

const sandbox = {
    window: { addEventListener() { } },
    parent: null,
    document: {
        createElement: (tag) => attachQuery(makeEl(tag)),
        addEventListener() { }, removeEventListener() { },
        querySelector: () => null,
        body: { appendChild() { } },
    },
    console,
    encodeURIComponent, decodeURIComponent, String, Math, Date, JSON, Object, Array, Error, Set, Map,
    setTimeout, clearTimeout,
    Utils: { iconHtml: () => '', formatFileSize: (n) => `${n} B` },
};
const ctx = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SHELL_BASE_JS, 'utf8'), ctx, { filename: 'shell/base.js' });

// 页面里 `Bridge` 是全局名字（base.js 内部直接写 `Bridge.originalUrl(...)`），
// 而它只挂在 window 上；vm 沙箱里两者是分开的：把 window 上的再挂回沙箱全局。
// `createLightbox` 是顶层函数声明，直接落在沙箱全局上。
sandbox.Bridge = sandbox.window.Bridge || sandbox.Bridge;
const Bridge = sandbox.Bridge;
const createLightbox = sandbox.createLightbox;
Bridge.setPrefix('image-viewer');

const items = [0, 1, 2, 3, 4, 5].map((i) => ({ url: `a/p${i}.jpg` }));
const lightbox = createLightbox({ getImageUrl: (item) => item.url });
const pool = () => lightbox.getDecodedCount();

console.log('场景 1：上限 = 2，超出时淘汰最久未用的');
lightbox.setDecodedLimit(2);
check('setDecodedLimit 可调用', typeof lightbox.setDecodedLimit === 'function');
lightbox.show(items, 0);
check('打开第 1 张后池内 1 张', pool() === 1, `pool=${pool()}`);
lightbox.navigate(1);
lightbox.navigate(2);
check('看过 3 张后池内不超过上限 2 张', pool() === 2, `pool=${pool()}`);
lightbox.navigate(-1);
lightbox.navigate(-1);
check('切回看过的图不新增池条目', pool() === 2, `pool=${pool()}`);

console.log('场景 2：0 = 关闭');
lightbox.setDecodedLimit(0);
check('设为 0 立即清空', pool() === 0, `pool=${pool()}`);
lightbox.navigate(4);
lightbox.navigate(5);
check('关闭后不再保留任何位图', pool() === 0, `pool=${pool()}`);

console.log('场景 3：关灯箱即清空');
lightbox.setDecodedLimit(8);
lightbox.show(items, 0);
check('打开时保留位图', pool() >= 1, `pool=${pool()}`);
lightbox.navigate(1);
lightbox.hide();
check('关灯箱后池清空', pool() === 0, `pool=${pool()}`);

console.log('场景 4：非法值归一为 0');
lightbox.setDecodedLimit(-3);
lightbox.show(items, 2);
check('负数按 0 处理', pool() === 0, `pool=${pool()}`);
lightbox.setDecodedLimit('abc');
lightbox.navigate(3);
check('非数字按 0 处理', pool() === 0, `pool=${pool()}`);

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
