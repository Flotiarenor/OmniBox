// manga-library 前端逻辑用例的共享装载器。
//
// 为什么需要单独一份：插件的 chrome（品牌 / 导航 / 顶栏 / 搜索 / 状态栏）已迁到壳，
// 内容页只剩书架、下载中心、详情、弹窗与阅读器，行为全部由 `omnibox:*` 消息驱动。
// 这些分支没有 DOM 就测不了：卡片、搜索空态、下载行、返回链都要真实节点与事件。
//
// 这里提供一个只覆盖「渲染 + 事件 + 查询」的最小 DOM（不实现布局、样式计算与冒泡），
// 再用 vm 按 index.html 声明的顺序装载真实插件脚本 —— 不复制插件逻辑，不复刻实现。
// 需要布局/计算样式/焦点可见性的断言请走浏览器 e2e，不要往这里加。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const FRONTEND = path.join(ROOT, 'plugins', 'manga-library', 'frontend');
const CONTENT_UI = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'content-ui.js');

const VOID_TAGS = new Set(['img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'use', 'path']);

const escapeHtml = (value) => {
    if (value == null) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

/** 解析一个标签的属性串：支持双引号、单引号与反斜杠转义（插件模板里有内嵌引号）。 */
function parseAttrs(raw) {
    const attrs = [];
    let i = 0;
    while (i < raw.length) {
        while (i < raw.length && /\s/.test(raw[i])) i += 1;
        if (i >= raw.length) break;
        let name = '';
        while (i < raw.length && !/[\s=]/.test(raw[i])) name += raw[i++];
        if (!name) { i += 1; continue; }
        let value = '';
        while (i < raw.length && /\s/.test(raw[i])) i += 1;
        if (raw[i] === '=') {
            i += 1;
            while (i < raw.length && /\s/.test(raw[i])) i += 1;
            const quote = raw[i];
            if (quote === '"' || quote === "'") {
                i += 1;
                let closed = false;
                while (i < raw.length) {
                    const ch = raw[i];
                    if (ch === '\\') { value += raw[i + 1] ?? ''; i += 2; continue; }
                    if (ch === quote) { i += 1; closed = true; break; }
                    value += ch;
                    i += 1;
                }
                // 注：HTML 其实没有反斜杠转义；插件模板在属性里塞 JS 单引号串，
                // 解析时按"引号内不会再出现同种引号"处理即可，未闭合就到此为止。
                if (!closed) { /* 剩余部分不再解释 */ }
            } else {
                while (i < raw.length && !/\s/.test(raw[i])) value += raw[i++];
            }
        }
        attrs.push([name.toLowerCase(), value]);
    }
    return attrs;
}

/** 把一段 HTML 解析成节点树。只做插件模板用得到的子集：标签、属性、文本。 */
function parseHtml(doc, html) {
    const roots = [];
    const stack = [{ children: roots, tag: '#root' }];
    const current = () => stack[stack.length - 1];
    function addText(text) {
        if (!text) return;
        const node = doc.createElement('#text');
        node._text = text;
        node.tagName = '#text';
        current().children.push(node);
        node.parentElement = null;   // 文本节点的父级在下面统一补
        node.__parent = current();
    }
    function addNode(node) {
        const parent = current();
        parent.children.push(node);
        node.__parent = parent;
        node.parentElement = parent.__element || null;
        return node;
    }
    let i = 0;
    while (i < html.length) {
        const lt = html.indexOf('<', i);
        if (lt < 0) { addText(html.slice(i)); break; }
        if (lt > i) addText(html.slice(i, lt));
        if (html.startsWith('<!--', lt)) {
            const endComment = html.indexOf('-->', lt + 4);
            i = endComment < 0 ? html.length : endComment + 3;
            continue;
        }
        // 找标签结束的 `>`：跳过引号内的内容（onerror 属性里含有内嵌标签与引号）。
        let j = lt + 1;
        let quote = null;
        let end = -1;
        while (j < html.length) {
            const ch = html[j];
            if (quote) {
                if (ch === '\\') { j += 2; continue; }
                if (ch === quote) quote = null;
            } else if (ch === '"' || ch === "'") {
                quote = ch;
            } else if (ch === '>') {
                end = j;
                break;
            }
            j += 1;
        }
        if (end < 0) { addText(html.slice(lt)); break; }
        let raw = html.slice(lt + 1, end);
        i = end + 1;
        if (raw.startsWith('!')) continue;          // doctype
        const closing = raw.startsWith('/');
        if (closing) raw = raw.slice(1);
        const selfClosing = !closing && raw.trimEnd().endsWith('/');
        if (selfClosing) raw = raw.trimEnd().slice(0, -1);
        const nameMatch = /^\s*([a-zA-Z][\w:-]*)([\s\S]*)$/.exec(raw);
        if (!nameMatch) continue;
        const tag = nameMatch[1].toLowerCase();
        if (closing) {
            for (let s = stack.length - 1; s > 0; s -= 1) {
                if (stack[s].tag === tag) { stack.length = s; break; }
            }
            continue;
        }
        const el = doc.createElement(tag);
        for (const [name, value] of parseAttrs(nameMatch[2])) {
            if (name === 'class') el.className = value;
            else if (name === 'id') el.id = value;
            else if (name === 'style') applyStyleText(el, value);
            else if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase());
                el.dataset[key] = value;
            } else {
                el.attrs[name] = value;
                applyCommonAttr(el, name, value);
            }
        }
        el.__parent = current();
        current().children.push(el);
        el.parentElement = current().__element || null;
        if (!VOID_TAGS.has(tag) && !selfClosing) stack.push({ tag, children: el.children, __element: el });
    }
    // 文本节点的 parentElement 在解析结束后按父容器补齐（子节点数组里混着文本节点，
    // 但插件不会遍历它们，保留结构只为 innerHTML 可回读）。
    return roots;
}

function applyCommonAttr(el, name, value) {
    switch (name) {
        case 'type': el.type = value; break;
        case 'value': el.value = value; break;
        case 'checked': el.checked = true; break;
        case 'disabled': el.disabled = true; break;
        case 'title': el.title = value; break;
        case 'placeholder': el.placeholder = value; break;
        case 'src': el.src = value; break;
        case 'alt': el.alt = value; break;
        case 'role': el.setAttributeRaw('role', value); break;
        case 'tabindex': el.tabIndex = Number(value); break;
        case 'aria-label': el.setAttributeRaw('aria-label', value); break;
        case 'aria-hidden': el.setAttributeRaw('aria-hidden', value); break;
        default:
            // 其它属性（href / name / target / role 之外的 aria-* 等）保留原始值
            el.setAttributeRaw(name, value);
    }
}

function applyStyleText(el, text) {
    String(text || '').split(';').forEach((part) => {
        const at = part.indexOf(':');
        if (at < 0) return;
        const key = part.slice(0, at).trim();
        const value = part.slice(at + 1).trim();
        if (!key) return;
        if (key.startsWith('--')) el.style.setProperty(key, value);
        else el.style[key] = value;
    });
}

function matchesCompound(el, compound) {
    if (!el || el.tagName === '#text') return false;
    if (compound === ':scope') return true;
    const re = /([a-zA-Z][\w-]*)|\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
    let rest = compound;
    let matched = true;
    const consume = (text) => { rest = rest.replace(text, ''); };
    let m;
    while ((m = re.exec(compound)) !== null) {
        if (m[1]) {
            if (el.tagName !== m[1].toLowerCase()) matched = false;
            consume(m[0]);
        } else if (m[2]) {
            if (!el.classList.contains(m[2])) matched = false;
            consume(m[0]);
        } else if (m[3]) {
            if (el.id !== m[3]) matched = false;
            consume(m[0]);
        } else if (m[4]) {
            const name = m[4];
            const expected = m[5];
            let actual;
            if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase());
                actual = el.dataset[key];
            } else if (name === 'class') {
                actual = el.className;
            } else {
                actual = el.attrs[name];
            }
            if (expected === undefined) {
                if (actual === undefined || actual === null) matched = false;
            } else if (String(actual ?? '') !== expected) {
                matched = false;
            }
            consume(m[0]);
        }
        if (!matched) break;
    }
    return matched && rest.trim() === '';
}

function splitSelector(selector) {
    // `:scope > .x` 单独处理；其余按空白拆成后代链（插件里没有 `,` 与伪类）。
    const trimmed = selector.trim();
    const scopeChild = /^:scope\s*>\s*(.+)$/.exec(trimmed);
    if (scopeChild) return { scopeChild: scopeChild[1] };
    return { parts: trimmed.split(/\s+/).filter(Boolean) };
}

function matchesChain(el, parts, stopAt) {
    // 候选元素必须自己匹配选择器的最后一段；匹配到祖先不能算数（否则
    // `.cui-grid` 会把它里面的每个后代都选中）。
    let node = el;
    let index = parts.length - 1;
    if (!matchesCompound(node, parts[index])) return false;
    index -= 1;
    node = node.parentElement;
    while (index >= 0) {
        while (node && node !== stopAt && !matchesCompound(node, parts[index])) {
            node = node.parentElement;
        }
        if (!node || node === stopAt) return false;
        index -= 1;
        node = node.parentElement;
    }
    return true;
}

function createEnvDocument() {
    const byId = new Map();
    const docListeners = {};

    function makeElement(tag) {
        const el = {
            tagName: String(tag).toLowerCase(),
            __id: '',
            __text: '',
            __html: '',
            className: '',
            title: '',
            htmlFor: '',
            placeholder: '',
            type: '',
            value: '',
            src: '',
            alt: '',
            loading: '',
            checked: false,
            disabled: false,
            hidden: false,
            tabIndex: -1,
            children: [],
            childNodes: [],
            dataset: {},
            attrs: {},
            handlers: {},
            parentElement: null,
            __parent: null,
            scrollTop: 0,
            scrollHeight: 0,
            clientWidth: 0,
            clientHeight: 0,
            innerText: '',
            style: {
                setProperty(key, value) { this[key] = String(value); },
                removeProperty(key) { delete this[key]; },
            },
            classList: {
                add(...tokens) {
                    tokens.forEach((token) => {
                        if (!token) return;
                        if (!el.classList.contains(token)) el.className = `${el.className} ${token}`.trim();
                    });
                },
                remove(...tokens) {
                    const drop = new Set(tokens);
                    el.className = String(el.className).split(/\s+/)
                        .filter((token) => token && !drop.has(token)).join(' ');
                },
                contains(token) {
                    return String(el.className).split(/\s+/).includes(token);
                },
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
                if (child.__parent) {
                    const siblings = child.__parent.children;
                    const at = siblings.indexOf(child);
                    if (at >= 0) siblings.splice(at, 1);
                }
                if (child.__parent && child.__parent.__element) {
                    child.parentElement = child.__parent.__element;
                } else {
                    child.parentElement = el;
                }
                child.__parent = { children: el.children, __element: el };
                el.children.push(child);
                return child;
            },
            append(...nodes) { nodes.forEach((node) => el.appendChild(node)); },
            prepend(...nodes) { nodes.reverse().forEach((node) => el.insertBefore(node, el.children[0] || null)); },
            insertBefore(child, ref) {
                if (child.__parent) {
                    const siblings = child.__parent.children;
                    const at = siblings.indexOf(child);
                    if (at >= 0) siblings.splice(at, 1);
                }
                const at = ref ? el.children.indexOf(ref) : -1;
                child.__parent = { children: el.children, __element: el };
                child.parentElement = el;
                if (at < 0) el.children.push(child); else el.children.splice(at, 0, child);
                return child;
            },
            replaceChildren(...nodes) {
                [...el.children].forEach((child) => { child.__parent = null; child.parentElement = null; });
                el.children.length = 0;
                nodes.forEach((node) => el.appendChild(node));
            },
            replaceWith(node) {
                const parent = el.parentElement;
                if (!parent) return;
                const at = parent.children.indexOf(el);
                if (at < 0) return;
                if (node.__parent) {
                    const siblings = node.__parent.children;
                    const nodeAt = siblings.indexOf(node);
                    if (nodeAt >= 0) siblings.splice(nodeAt, 1);
                }
                node.__parent = { children: parent.children, __element: parent };
                node.parentElement = parent;
                parent.children[at] = node;
                el.parentElement = null;
            },
            remove() {
                const parent = el.parentElement;
                if (!parent) return;
                const at = parent.children.indexOf(el);
                if (at >= 0) parent.children.splice(at, 1);
                el.parentElement = null;
            },
            setAttributeRaw(name, value) { el.attrs[name] = String(value); },
            setAttribute(name, value) {
                if (name === 'id') el.id = value;
                else if (name === 'class') el.className = String(value);
                else if (name === 'style') applyStyleText(el, value);
                else if (name.startsWith('data-')) {
                    const key = name.slice(5).replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase());
                    el.dataset[key] = String(value);
                } else {
                    el.setAttributeRaw(name, value);
                    applyCommonAttr(el, name, String(value));
                }
            },
            getAttribute(name) {
                if (name === 'id') return el.id || null;
                if (name === 'class') return el.className || null;
                return el.attrs[name] ?? null;
            },
            hasAttribute(name) {
                if (name === 'id') return !!el.id;
                if (name === 'class') return !!el.className;
                if (name.startsWith('data-')) {
                    const key = name.slice(5).replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase());
                    return Object.prototype.hasOwnProperty.call(el.dataset, key);
                }
                return Object.prototype.hasOwnProperty.call(el.attrs, name);
            },
            removeAttribute(name) {
                if (name === 'id') { byId.delete(el.__id); el.__id = ''; return; }
                if (name.startsWith('data-')) {
                    const key = name.slice(5).replace(/-([a-z0-9])/g, (_, ch) => ch.toUpperCase());
                    delete el.dataset[key];
                    return;
                }
                delete el.attrs[name];
            },
            addEventListener(type, fn) {
                (el.handlers[type] = el.handlers[type] || []).push(fn);
            },
            removeEventListener(type, fn) {
                const list = el.handlers[type] || [];
                const at = list.indexOf(fn);
                if (at >= 0) list.splice(at, 1);
            },
            dispatchEvent(type, extra = {}) {
                const event = {
                    type,
                    target: el,
                    currentTarget: el,
                    defaultPrevented: false,
                    preventDefault() { event.defaultPrevented = true; },
                    stopPropagation() { },
                    stopImmediatePropagation() { },
                    clientX: 0,
                    clientY: 0,
                    detail: 1,
                    key: '',
                    ...extra,
                };
                (el.handlers[type] || []).slice().forEach((fn) => fn(event));
                return !event.defaultPrevented;
            },
            click() { el.dispatchEvent('click', { target: el, currentTarget: el }); },
            focus() { doc.activeElement = el; },
            blur() { if (doc.activeElement === el) doc.activeElement = null; },
            scrollIntoView() { },
            getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0, right: 0, bottom: 0 }; },
            querySelectorAll(selector) {
                const found = [];
                const { scopeChild, parts } = splitSelector(selector);
                const visit = (node, depth) => {
                    for (const child of node.children) {
                        if (!child || child.tagName === '#text') continue;
                        if (scopeChild) {
                            if (depth === 0 && matchesCompound(child, scopeChild)) found.push(child);
                        } else if (matchesChain(child, parts, el)) {
                            found.push(child);
                        }
                        visit(child, depth + 1);
                    }
                };
                visit(el, 0);
                return found;
            },
            querySelector(selector) { return el.querySelectorAll(selector)[0] || null; },
            matches(selector) { return matchesCompound(el, selector); },
            closest(selector) {
                let node = el;
                while (node) {
                    if (matchesCompound(node, selector)) return node;
                    node = node.parentElement;
                }
                return null;
            },
            contains(node) {
                let cur = node;
                while (cur) { if (cur === el) return true; cur = cur.parentElement; }
                return false;
            },
            cloneNode() { return makeElement(el.tagName); },
        };
        Object.defineProperty(el, 'id', {
            get: () => el.__id,
            set: (value) => {
                if (el.__id) byId.delete(el.__id);
                el.__id = String(value);
                if (el.__id) byId.set(el.__id, el);
            },
        });
        Object.defineProperty(el, 'firstElementChild', {
            get: () => el.children.find((child) => child && child.tagName !== '#text') || null,
        });
        Object.defineProperty(el, 'textContent', {
            get: () => el.__text + el.children.map((child) => (child.tagName === '#text' ? child._text : child.textContent)).join(''),
            set: (value) => {
                el.__text = String(value ?? '');
                [...el.children].forEach((child) => { child.parentElement = null; child.__parent = null; });
                el.children.length = 0;
            },
        });
        Object.defineProperty(el, 'innerHTML', {
            get: () => el.__html,
            set: (html) => {
                el.__html = String(html ?? '');
                [...el.children].forEach((child) => { child.parentElement = null; child.__parent = null; });
                el.children.length = 0;
                // 注意要遍历副本：appendChild 会把节点从解析结果数组里摘掉，
                // 直接对原数组 forEach 会跳过后面的兄弟节点。
                [...parseHtml(doc, el.__html)].forEach((child) => el.appendChild(child));
            },
        });
        Object.defineProperty(el, 'parentNode', { get: () => el.parentElement });
        Object.defineProperty(el, 'ownerDocument', { get: () => doc });
        return el;
    }

    const documentElement = makeElement('html');
    const body = makeElement('body');
    const doc = {
        location: null,
        readyState: 'complete',
        activeElement: null,
        body,
        documentElement,
        createElement: makeElement,
        createDocumentFragment() {
            const fragment = makeElement('#fragment');
            fragment.isFragment = true;
            return fragment;
        },
        getElementById: (id) => byId.get(String(id)) || null,
        querySelector(selector) {
            const { parts } = splitSelector(selector);
            for (const child of body.children) {
                if (child.tagName !== '#text' && matchesChain(child, parts, body)) return child;
                const nested = child.querySelector ? child.querySelector(selector) : null;
                if (nested) return nested;
            }
            return null;
        },
        querySelectorAll(selector) {
            const found = [];
            body.children.forEach((child) => {
                if (!child || child.tagName === '#text') return;
                if (matchesCompound(child, selector)) found.push(child);
                found.push(...child.querySelectorAll(selector));
            });
            return found;
        },
        addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
        removeEventListener(type, fn) {
            const list = docListeners[type] || [];
            const at = list.indexOf(fn);
            if (at >= 0) list.splice(at, 1);
        },
        dispatchEvent(type, extra = {}) {
            (docListeners[type] || []).slice().forEach((fn) => fn({ type, target: body, ...extra }));
        },
    };
    documentElement.appendChild(body);
    return { document: doc, body, makeElement, byId };
}

function loadScript(sandbox, file, label) {
    const source = fs.readFileSync(file, 'utf8');
    vm.runInContext(source, sandbox, { filename: label });
}

/**
 * 装载 manga-library 内容页。
 *
 * @param {object} options
 * @param {object} options.handlers   method -> (…args) => 值 / Promise / 抛错
 * @param {boolean} options.standalone true = 脱离壳（window.parent === window）
 * @param {boolean} options.noLightbox true = 不注入 createLightbox，走旧 .manga-reader 回退
 * @param {object} options.lightbox  自定义 createLightbox 工厂
 */
export function loadManga(options = {}) {
    const { handlers = {}, standalone = false, noLightbox = false } = options;
    const dom = createEnvDocument();
    const doc = dom.document;
    const messages = [];
    const bridgeCalls = [];
    const toasts = [];
    const lifecycle = { hide: [], show: [], dispose: [] };
    const lightboxEvents = [];

    const origin = 'http://127.0.0.1:18080';
    const locationStub = {
        origin,
        href: `${origin}/plugins/manga-library/frontend/index.html?content=1`,
        search: '?content=1',
    };
    doc.location = locationStub;

    const parent = {
        location: { origin },
        document: doc,
        postMessage(payload) { messages.push(payload); },
    };

    const pick = (method) => {
        if (Object.prototype.hasOwnProperty.call(handlers, method)) return handlers[method];
        if (Object.prototype.hasOwnProperty.call(handlers, '*')) return handlers['*'];
        return () => ({});
    };

    const sandbox = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        requestAnimationFrame: (fn) => setTimeout(fn, 0),
        cancelAnimationFrame: (id) => clearTimeout(id),
        document: doc,
        location: locationStub,
        localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
        Utils: {
            escapeHtml,
            jsString: (value) => `'${String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`,
        },
        Icons: {
            html: (name, className) => `<svg class="obx-icon${className ? ' ' + className : ''}">`
                + `<use href="#${String(name).replace(/^icon:/, '')}"></use></svg>`,
        },
        Motion: {
            retrigger() { },
            stagger() { },
        },
        Toast: {
            success: (message) => toasts.push({ type: 'success', message }),
            error: (message) => toasts.push({ type: 'error', message }),
            warning: (message) => toasts.push({ type: 'warning', message }),
            info: (message) => toasts.push({ type: 'info', message }),
        },
        confirmDialog: async () => true,
        PluginLifecycle: {
            onHide(fn) { lifecycle.hide.push(fn); },
            onShow(fn) { lifecycle.show.push(fn); },
            onDispose(fn) { lifecycle.dispose.push(fn); },
            setVisible(visible) {
                (visible ? lifecycle.show : lifecycle.hide).forEach((fn) => fn());
            },
            dispose() { lifecycle.dispose.forEach((fn) => fn()); },
        },
        Bridge: {
            call(method, ...args) {
                bridgeCalls.push({ method, args });
                const fn = pick(method);
                try {
                    return Promise.resolve(fn(...args));
                } catch (error) {
                    return Promise.reject(error);
                }
            },
            originalUrl: (value) => `/file?path=${encodeURIComponent(value)}`,
            thumbUrl: (value) => `/thumbs/${encodeURIComponent(value)}`,
        },
        innerWidth: 1280,
        innerHeight: 800,
    };
    sandbox.window = sandbox;
    sandbox.parent = standalone ? sandbox : parent;

    if (!noLightbox) {
        const factory = options.lightbox || ((lightboxOptions) => {
            let open = false;
            let items = [];
            let index = 0;
            return {
                show(nextItems, startIndex) {
                    items = nextItems || [];
                    index = startIndex || 0;
                    open = true;
                    lightboxEvents.push({ type: 'show', count: items.length, index });
                },
                hide() { open = false; lightboxEvents.push({ type: 'hide' }); },
                navigate(dir) { index += dir; lightboxEvents.push({ type: 'navigate', index }); },
                getIndex: () => index,
                getCount: () => items.length,
                setItems(nextItems, focusUrl, fallbackIndex) {
                    items = nextItems || [];
                    index = fallbackIndex || 0;
                    lightboxEvents.push({ type: 'setItems', count: items.length });
                },
                setDecodedLimit() { },
                isOpen: () => open,
            };
        });
        sandbox.createLightbox = factory;
    }

    vm.createContext(sandbox);
    // 真实 DOM 的空壳：把 index.html 的 body 内容解析进来（去掉脚本与注释），
    // 这样 #ml-content、阅读器容器与两个弹窗和线上结构一致。
    const indexHtml = fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
    const bodyMatch = /<body[^>]*>([\s\S]*)<\/body>/i.exec(indexHtml);
    const bodyHtml = (bodyMatch ? bodyMatch[1] : '')
        .replace(/<script\b[\s\S]*?<\/script>/gi, '')
        .replace(/<!--[\s\S]*?-->/g, '');
    doc.body.innerHTML = bodyHtml;
    // 壳注入的共享组件：content-ui 提供 ContentUI.card/grid。
    loadScript(sandbox, CONTENT_UI, 'shell/content-ui.js');
    loadScript(sandbox, path.join(FRONTEND, 'js', 'utils.js'), 'plugins/manga-library/frontend/js/utils.js');
    loadScript(sandbox, path.join(FRONTEND, 'js', 'reader.js'), 'plugins/manga-library/frontend/js/reader.js');
    loadScript(sandbox, path.join(FRONTEND, 'js', 'app.js'), 'plugins/manga-library/frontend/js/app.js');
    vm.runInContext('globalThis.__MangaApp = MangaLibraryApp; globalThis.__MangaReader = MangaReader;',
        sandbox, { filename: 'manga-library-classes.js' });

    const windowListeners = {};
    sandbox.addEventListener = (type, fn) => {
        (windowListeners[type] = windowListeners[type] || []).push(fn);
    };
    sandbox.removeEventListener = (type, fn) => {
        const list = windowListeners[type] || [];
        const at = list.indexOf(fn);
        if (at >= 0) list.splice(at, 1);
    };

    const env = {
        sandbox,
        document: doc,
        window: sandbox,
        parent,
        messages,
        bridgeCalls,
        toasts,
        lifecycle,
        lightboxEvents,
        MangaLibraryApp: sandbox.__MangaApp,
        MangaReader: sandbox.__MangaReader,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'omnibox:chrome') return messages[i];
            }
            return null;
        },
        chromeAll: () => messages.filter((message) => message && message.type === 'omnibox:chrome'),
        nav: () => {
            const chrome = env.chrome();
            return chrome && chrome.nav ? chrome.nav : {};
        },
        send(data, source = standalone ? sandbox : parent) {
            (windowListeners.message || []).slice().forEach((fn) => fn({
                origin,
                source,
                data,
            }));
        },
        sendView(view) { env.send({ type: 'omnibox:view', view }); },
        sendAction(action) { env.send({ type: 'omnibox:action', action }); },
        sendQuery(query) { env.send({ type: 'omnibox:query', query }); },
        calls: (method) => bridgeCalls.filter((call) => call.method === method),
        tick: (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms)),
    };
    return env;
}

/** 与插件常量一致的默认 Bridge 返回值，避免每个用例重抄一遍。 */
export function defaultHandlers(overrides = {}) {
    const base = {
        manga_get_state: () => ({ favorites: [], recent: [] }),
        manga_list: () => [],
        manga_search: () => [],
        manga_get_detail: () => ({}),
        manga_get_pages: () => [],
        manga_update_recent: () => ({ status: 'ok' }),
        manga_toggle_favorite: () => true,
        download_list: () => ({ tasks: [] }),
        download_submit: () => ({ success: true }),
        download_start_all: () => ({ success: true }),
        download_pause_all: () => ({ success: true }),
        download_clear_completed: () => ({ success: true }),
        download_pause: () => ({ success: true }),
        download_resume: () => ({ success: true }),
        download_retry: () => ({ success: true }),
        download_delete: () => ({ success: true }),
        download_get_album_info: () => ({}),
    };
    return { ...base, ...overrides };
}

/** 构造一个已 init 的 app（轮询立刻停掉，避免用例结束还挂着 interval）。 */
export async function createApp(env) {
    const app = new env.MangaLibraryApp();
    await app.init();
    app._stopPoll();
    return app;
}

export const mangaPaths = { FRONTEND, ROOT, CONTENT_UI };
