// Shell 分隔条内核（shell/frontend/public/shell/splitter.js）的注册表生命周期用例。
//
// 为什么守它：插件前端大量动态渲染，每次重渲染都会产生新的 [data-obx-splitter]
// 元素；`scan()` 只增不删，`items` 数组又强引用元素与其把手（handle 是插在元素
// 后面的兄弟节点，元素被移除时它不会跟着走）。长会话里这是线性的内存与重排增长。
//
// 锁两件事：
//   1) 元素从文档移除后，relayout 把它从注册表里摘掉（getItemCount 可观测）；
//   2) 它留下的把手节点也被移除，不在父容器里变成孤儿。
//
// 用法：node tests/js/shell_splitter.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SRC = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'splitter.js');

let failed = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failed++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

function makeEl(tag = 'div') {
    const attrs = {};
    const listeners = {};
    const el = {
        tagName: String(tag).toUpperCase(),
        className: '',
        id: '',
        isConnected: true,
        parentElement: null,
        children: [],
        style: {
            _vars: {},
            setProperty(name, value) { this._vars[name] = String(value); },
            removeProperty(name) { delete this._vars[name]; },
            getPropertyValue(name) { return this._vars[name] || ''; },
        },
        classList: {
            _set: new Set(),
            add(...names) { names.forEach((name) => this._set.add(name)); },
            remove(...names) { names.forEach((name) => this._set.delete(name)); },
            contains(name) { return this._set.has(name); },
            toggle(name, force) {
                if (force === undefined) force = !this._set.has(name);
                if (force) this._set.add(name); else this._set.delete(name);
                return force;
            },
        },
        setAttribute(name, value) { attrs[name] = String(value); },
        getAttribute(name) { return name in attrs ? attrs[name] : null; },
        removeAttribute(name) { delete attrs[name]; },
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            listeners[type] = (listeners[type] || []).filter((item) => item !== fn);
        },
        getBoundingClientRect() {
            return { width: 240, height: 100, top: 0, left: 0, bottom: 100, right: 240 };
        },
        appendChild(child) { child.parentElement = el; el.children.push(child); return child; },
        insertAdjacentElement(position, node) {
            const parent = el.parentElement;
            assert.equal(position, 'afterend', 'splitter 只应把把手插到元素后面');
            assert.ok(parent, '被拖元素必须先挂在父容器里');
            const index = parent.children.indexOf(el);
            parent.children.splice(index + 1, 0, node);
            node.parentElement = parent;
            node.isConnected = true;
            return node;
        },
        remove() {
            el.isConnected = false;
            if (el.parentElement) {
                el.parentElement.children = el.parentElement.children.filter((c) => c !== el);
            }
            el.parentElement = null;
        },
        querySelectorAll() { return []; },
        matches() { return false; },
    };
    return el;
}

const doc = {
    readyState: 'complete',
    documentElement: makeEl('html'),
    body: makeEl('body'),
    createElement: (tag) => makeEl(tag),
    querySelectorAll: () => registered,
    addEventListener() { },
};

let registered = [];
// ResizeObserver 会持有被观察元素的强引用，直到 unobserve/disconnect：
// 元素移除后若只摘注册表、不停观察，泄漏依然存在。这里记录观察/停止观察调用。
const observed = { observe: [], unobserve: [] };
const sandbox = {
    console,
    document: doc,
    location: { origin: 'http://127.0.0.1:1', href: 'http://127.0.0.1:1/plugins/x/frontend/index.html' },
    addEventListener() { },
    removeEventListener() { },
    setTimeout,
    clearTimeout,
    ResizeObserver: class {
        observe(el) { observed.observe.push(el); }
        unobserve(el) { observed.unobserve.push(el); }
        disconnect() { }
    },
    // 同步执行：scheduleRelayout 里的 requestAnimationFrame 立即跑一次 relayout。
    requestAnimationFrame: (fn) => { fn(); return 1; },
    MutationObserver: class { observe() { } disconnect() { } takeRecords() { return []; } },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SRC, 'utf8'), sandbox, { filename: 'shell/splitter.js' });
const ShellSplitter = sandbox.window.ShellSplitter;

const count = () => (typeof ShellSplitter.getItemCount === 'function'
    ? ShellSplitter.getItemCount() : -1);

console.log('[shell] 分隔条注册表');
assert.ok(ShellSplitter, 'window.ShellSplitter 不存在');

const parent = makeEl('div');
const target = makeEl('aside');
target.setAttribute('data-obx-splitter', '--side-width');
target.setAttribute('data-obx-splitter-min', '120');
parent.appendChild(target);
registered = [target];

ShellSplitter.scan(doc);
check('scan 注册带 data-obx-splitter 的元素', count() === 1, `count=${count()}`);
const handle = parent.children.find((child) => String(child.className).includes('obx-splitter'));
check('注册时在元素后插入把手', !!handle && handle.parentElement === parent,
    `children=${parent.children.length}`);

// 模拟插件重渲染：元素从文档移除（把手是兄弟节点，留在原容器里）
target.remove();
check('前置：元素移除后把手还在容器里', parent.children.includes(handle));
check('前置：注册表尚未回收（等 relayout）', count() === 1, `count=${count()}`);

ShellSplitter.relayout();
check('relayout 后注册表摘掉已移除的元素', count() === 0, `count=${count()}`);
check('已移除元素的把手不再留在容器里', !parent.children.includes(handle),
    `children=[${parent.children.map((c) => c.className).join(',')}]`);
check('已移除元素停止被 ResizeObserver 观察', observed.unobserve.includes(target),
    `observe=${observed.observe.length} unobserve=${observed.unobserve.length}`);

console.log(failed ? `\n${failed} 项失败` : '\nshell splitter 注册表 6 项检查全部通过');
process.exit(failed ? 1 : 0);
