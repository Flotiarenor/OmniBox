// 极简 DOM 替身：给"把壳的 base.js 或插件的前端 js 装进 node 跑一遍"的用例共用。
//
// 为什么共用：`tests/js` 下已经有 5 份手写的同类替身（host_channel_host /
// image_viewer_lifecycle / media_player_netease_local / shell_folder_picker /
// shell_settings_form），每份都只差几个方法，第 6 个用例再抄一遍就开始出现
// "这份有 dataset、那份没有"的隐性差异。这里只保留**渲染路径够用**的那部分：
//
//   * 元素创建、子节点（append/appendChild/insertBefore/remove/firstChild）
//   * className / classList / dataset / style.setProperty / 属性赋值
//   * 事件登记（`el.click()` 可直接触发 click 处理器）与按 tag / `.class` / `#id` 的查询
//   * `innerHTML = "..."` 只做一件事：把其中的 `id="x"` 登记进 document.getElementById
//     —— **不解析 HTML 结构**，需要布局、样式、事件冒泡的用例请走无头浏览器
//     （`tests/debug_*.py`），不要往这里加。
//
// 用法：
//     import { createDom } from './dom_stub.mjs';
//     const { document, makeEl } = createDom();
export function createDom() {
  const byId = new Map();

  function matches(el, selector) {
    if (selector.startsWith('.')) return String(el.className).split(/\s+/).includes(selector.slice(1));
    if (selector.startsWith('#')) return el.id === selector.slice(1);
    return el.tagName === selector;
  }

  function find(el, selector) {
    for (const child of el.children) {
      if (matches(child, selector)) return child;
      const deep = find(child, selector);
      if (deep) return deep;
    }
    return null;
  }

  function findAll(el, selector, out = []) {
    for (const child of el.children) {
      if (matches(child, selector)) out.push(child);
      findAll(child, selector, out);
    }
    return out;
  }

  /** innerHTML 里 `id="x"` 的元素做成子节点并登记（标签与 class 一并带上，够选择器用）。 */
  function adoptIds(parent, html) {
    const tagRe = /<([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)(?:\/>|>)/g;
    let match;
    while ((match = tagRe.exec(html)) !== null) {
      const attrs = match[2] || '';
      const idMatch = /\bid="([^"]+)"/.exec(attrs);
      if (!idMatch) continue;
      const el = makeEl(match[1].toLowerCase());
      const classMatch = /\bclass="([^"]*)"/.exec(attrs);
      if (classMatch) el.className = classMatch[1];
      el.id = idMatch[1];
      parent.appendChild(el);
    }
  }

  function makeEl(tag) {
    const el = {
      tagName: String(tag).toLowerCase(),
      className: '', textContent: '', value: '', type: '', title: '', htmlFor: '',
      placeholder: '', checked: false, disabled: false,
      style: { setProperty() { } },
      dataset: {}, children: [], handlers: {}, parentElement: null,
      classList: {
        add(c) { if (!String(el.className).split(/\s+/).includes(c)) el.className = `${el.className} ${c}`.trim(); },
        remove(c) { el.className = String(el.className).split(/\s+/).filter((x) => x && x !== c).join(' '); },
        contains: (c) => String(el.className).split(/\s+/).includes(c),
        toggle(c, force) {
          const on = force === undefined ? !el.classList.contains(c) : !!force;
          if (on) el.classList.add(c); else el.classList.remove(c);
          return on;
        },
      },
      append(...nodes) { nodes.forEach((n) => el.appendChild(n)); },
      appendChild(child) {
        if (child.__owner) child.__owner.children = child.__owner.children.filter((c) => c !== child);
        child.__owner = el;
        child.parentElement = el;
        el.children.push(child);
        return child;
      },
      insertBefore(child, ref) {
        if (child.__owner) child.__owner.children = child.__owner.children.filter((c) => c !== child);
        child.__owner = el;
        child.parentElement = el;
        const at = ref ? el.children.indexOf(ref) : -1;
        if (at < 0) el.children.push(child); else el.children.splice(at, 0, child);
        return child;
      },
      remove() {
        if (el.__owner) el.__owner.children = el.__owner.children.filter((c) => c !== el);
      },
      setAttribute() { }, getAttribute() { return null; }, removeAttribute() { },
      addEventListener(type, fn) { (el.handlers[type] = el.handlers[type] || []).push(fn); },
      removeEventListener() { },
      /** 真实 DOM 的 click()：直接跑已登记的处理器（不模拟冒泡与默认行为）。 */
      click() { (el.handlers.click || []).slice().forEach((fn) => fn({ target: el })); },
      querySelector: (selector) => find(el, selector),
      querySelectorAll: (selector) => findAll(el, selector),
    };
    // id 赋值即登记：document.getElementById 才找得到（真实 DOM 的语义）。
    Object.defineProperty(el, 'id', {
      get: () => el.__id || '',
      set: (value) => { el.__id = String(value); byId.set(el.__id, el); },
    });
    Object.defineProperty(el, 'firstChild', { get: () => el.children[0] || null });
    Object.defineProperty(el, 'innerHTML', {
      get: () => el.__html || '',
      set: (html) => {
        el.__html = String(html);
        el.children = [];
        adoptIds(el, el.__html);
      },
    });
    return el;
  }

  const documentElement = makeEl('html');
  const document = {
    readyState: 'complete',
    body: makeEl('body'),
    documentElement,
    createElement: makeEl,
    getElementById: (id) => byId.get(String(id)) || null,
    querySelector: (selector) => find(document.body, selector),
    querySelectorAll: (selector) => findAll(document.body, selector),
    addEventListener() { }, removeEventListener() { },
  };
  return { document, makeEl, byId };
}
