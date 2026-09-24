// HostChannel 的**宿主侧**行为自检：把子插件声明的按钮渲染进宿主自己的工具栏，
// 点击后回发 host-run；以及 `ui` 设置弹窗的多回合路径（值交回子插件）。
//
// 与 host_channel.mjs 的分工：那边跑真实插件页（子插件侧），这边用 stub 跑宿主侧
// （`serve()` 的 MOUNT / REQUEST 分支）。之所以要单独一个文件，是因为 vm 里"一个
// 上下文只能有一个 window"，宿主与子插件放同一个上下文才能可靠地互相派发消息
// （详见 host_channel.mjs 文件头的坑位说明）。
//
// 覆盖：
//   · host-mount：按 iframe.src 认领来源 → 渲染 .btn.btn-sm → 回 host-reply(ok:true)
//   · 重挂载：先清掉上一批（页面重载后子插件会再发一次），且不顶走宿主自己的按钮
//   · 找不到容器 / 认不出 iframe：回 ok:false（子插件据此把按钮放回自己页里）
//   · host-run：点击回发 {type, id}，源窗口是那个 iframe（不是父窗口）
//   · 非自己嵌的窗口发来的 host-mount 一律忽略
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const BASE_JS = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'base.js');
const ORIGIN = 'http://127.0.0.1:18080';

let failed = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

/** 极简 DOM：够 base.js 的 mountButtons / openModal 走完渲染路径。 */
function createHost({ withContainer = true, withFrame = true, frameSrc = '/plugins/image-cleaner/frontend/index.html' } = {}) {
  const posted = [];
  const listeners = { message: [] };
  const hostWin = {
    label: 'host',
    location: { origin: ORIGIN, href: `${ORIGIN}/plugins/image-viewer/frontend/index.html` },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() { },
    postMessage(payload) { posted.push(payload); },
  };
  hostWin.parent = { label: 'shell', location: { origin: ORIGIN }, postMessage() { } };
  const made = [];
  function makeEl(tag) {
    const el = {
      tagName: tag, id: '', className: '', type: '', title: '', innerHTML: '',
      style: {}, dataset: {}, children: [],
      classList: { add(c) { el.className += ' ' + c; }, remove() { }, toggle() { }, contains: () => false },
      addEventListener(type, fn) { (el.handlers = el.handlers || {})[type] = fn; },
      appendChild(child) {
        // 真实 DOM：appendChild 会先把节点从原位置摘下来（重排依赖这一点）
        if (child.__owner) child.__owner.children = child.__owner.children.filter((c) => c !== child);
        child.__owner = el;
        el.children.push(child);
        return child;
      },
      insertBefore(child, ref) {
        if (child.__owner) child.__owner.children = child.__owner.children.filter((c) => c !== child);
        child.__owner = el;
        const at = ref ? el.children.indexOf(ref) : -1;
        if (at < 0) el.children.push(child); else el.children.splice(at, 0, child);
        return child;
      },
      remove() {
        if (el.__owner) el.__owner.children = el.__owner.children.filter((c) => c !== el);
      },
      setAttribute() { }, removeAttribute() { }, querySelector: () => null,
    };
    // firstChild 是 insertBefore 的锚点，stub 必须也提供（缺了会静默变成 append）
    Object.defineProperty(el, 'firstChild', { get: () => el.children[0] || null });
    el.querySelectorAll = (sel) => (sel === '.obx-host-action'
      ? el.children.filter((c) => String(c.className).includes('obx-host-action'))
      : []);
    made.push(el);
    return el;
  }

  const container = makeEl('div');
  container.id = 'extension-view-actions';
  const closeBtn = makeEl('button');
  closeBtn.id = 'extension-view-close';
  closeBtn.className = 'btn btn-sm';
  container.appendChild(closeBtn);

  const iframe = {
    getAttribute: (name) => (name === 'src' ? frameSrc : null),
    contentWindow: null,
  };
  // 子窗口对象也要有 postMessage：宿主回信是 post(event.source, ...)，事件里的 source
  // 就是这个对象。没有它 = 回信那段静默失败（post 里 try/catch 吞掉了 TypeError），
  // 现象是"宿主明明回了，测试却什么也看不到"。
  const childWin = {
    label: 'child',
    location: { origin: ORIGIN },
    postMessage(payload) { posted.push(payload); },
  };
  iframe.contentWindow = childWin;

  const document = {
    location: { origin: ORIGIN },
    body: makeEl('body'),
    createElement: makeEl,
    addEventListener() { },
    getElementById: (id) => (withContainer && id === container.id ? container : null),
    querySelector: () => null,
    querySelectorAll: (sel) => (sel === 'iframe' && withFrame ? [iframe] : []),
    documentElement: { getAttribute: () => null, setAttribute() { }, style: { setProperty() { } } },
  };

  const sandbox = {
    window: hostWin, document, console, setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });

  const hc = hostWin.HostChannel;
  hc.serve({
    isOwnFrame: hc.ownFrame(() => (withFrame ? [iframe] : [])),
    containers: withContainer ? { 'host-toolbar': container } : {},
  });

  return {
    hc, posted, container, closeBtn, childWin, made,
    emit(data, source = childWin) {
      (listeners.message || []).slice().forEach((fn) => fn({ data, source, origin: ORIGIN }));
    },
    mountedButtons: () => container.children.filter((c) => String(c.className).includes('obx-host-action')),
    lastReply: () => posted.filter((m) => m.type === 'omnibox:host-reply').pop(),
  };
}

const MOUNT_DATA = {
  buttons: [
    { id: 'btn-settings', label: '设置', icon: 'icon:settings-2', title: '相似判定阈值等设置' },
    { id: 'btn-rescan', label: '重新扫描' },
  ],
  container: 'host-toolbar',
  url: `${ORIGIN}/plugins/image-cleaner/frontend/index.html`,
};

// ---------- 挂载 ----------
check('host-mount 把子插件的按钮渲染进宿主容器，并回 host-reply(ok:true)', () => {
  const h = createHost();
  h.emit({ type: 'omnibox:host-mount', exchange: 11, data: MOUNT_DATA });
  const buttons = h.mountedButtons();
  assert.equal(buttons.length, 2, '应当挂上两个按钮');
  // 有图标的按钮：innerHTML = iconHtml(...) + ' ' + 文本；本 stub 没有 Icons，图标位为空
  assert.deepEqual(buttons.map((b) => ({ id: b.innerHTML.includes('设置') ? 'btn-settings' : 'btn-rescan', text: b.innerHTML.trim() })),
    [{ id: 'btn-settings', text: '设置' }, { id: 'btn-rescan', text: '重新扫描' }],
    '两个按钮按声明顺序挂上，文本不转义错位');
  assert.equal(buttons[0].title, '相似判定阈值等设置');
  assert.match(String(buttons[0].className), /btn btn-sm obx-host-action/);
  const reply = h.lastReply();
  assert.deepEqual({ exchange: reply.exchange, ok: reply.ok }, { exchange: 11, ok: true });
});

check('挂载的按钮排在宿主自己的按钮之前（不顶走「返回相册」）', () => {
  const h = createHost();
  h.emit({ type: 'omnibox:host-mount', exchange: 12, data: MOUNT_DATA });
  const ids = h.container.children.map((c) => c.id || c.innerHTML.trim());
  assert.equal(ids[ids.length - 1], 'extension-view-close', `宿主按钮必须留在最后（实际: ${ids.join(',')}）`);
});

check('重挂载先清掉上一批（页面重载后子插件会再发一次）', () => {
  const h = createHost();
  h.emit({ type: 'omnibox:host-mount', exchange: 13, data: MOUNT_DATA });
  h.emit({ type: 'omnibox:host-mount', exchange: 14, data: MOUNT_DATA });
  assert.equal(h.mountedButtons().length, 2, '不能累积成 4 个');
});

check('点击挂载出来的按钮回发 host-run，且 event.source 认的是那个 iframe', () => {
  const h = createHost();
  h.emit({ type: 'omnibox:host-mount', exchange: 15, data: MOUNT_DATA });
  const btn = h.mountedButtons()[0];
  btn.handlers.click();
  const run = h.posted.filter((m) => m.type === 'omnibox:host-run').pop();
  assert.ok(run, '没有回发 host-run');
  assert.equal(run.id, 'btn-settings');
});

// ---------- 认不出来 / 没容器：必须显式失败 ----------
check('宿主认领了这个 iframe、但 src 对不上时回 ok:false/no-frame（子插件把按钮放回自己页里）', () => {
  const h = createHost({ frameSrc: '/plugins/other-plugin/frontend/index.html' });
  h.emit({ type: 'omnibox:host-mount', exchange: 16, data: MOUNT_DATA });
  const reply = h.lastReply();
  assert.equal(reply.ok, false);
  assert.equal(reply.data.error, 'no-frame');
});

check('容器不存在时回 ok:false 且带 no-container（不是静默成功）', () => {
  const h = createHost({ withContainer: false });
  h.emit({ type: 'omnibox:host-mount', exchange: 17, data: MOUNT_DATA });
  const reply = h.lastReply();
  assert.equal(reply.ok, false);
  assert.match(String(reply.data.error), /no-container/);
});

check('非自己嵌的窗口发来的 host-mount 一律忽略（不渲染、不回信）', () => {
  const h = createHost();
  const before = h.posted.length;
  h.emit({ type: 'omnibox:host-mount', exchange: 18, data: MOUNT_DATA },
    { label: 'stranger', location: { origin: ORIGIN } });
  assert.equal(h.mountedButtons().length, 0, '不该渲染');
  assert.equal(h.posted.length, before, '不该回信');
});

// ---------- 设置弹窗的多回合：值交回子插件 ----------
check('ui 设置请求：先回信「我接下了」，再把值交回子插件（不等保存才算完）', async () => {
  const h = createHost();
  const asked = [];
  h.hc.serve({
    isOwnFrame: h.hc.ownFrame(() => [{ contentWindow: h.childWin }]),
    containers: { 'host-toolbar': h.container },
    handlers: {
      ui: (data) => {
        asked.push(data);
        return (commit) => commit({ threshold: 8 });
      },
    },
  });
  h.emit({
    type: 'omnibox:host-request', exchange: 21, action: 'ui',
    data: { kind: 'settings', title: '相册清理设置', schema: [{ key: 'threshold', type: 'range', label: '阈值' }], values: { threshold: 4 } },
  });
  await new Promise((r) => setTimeout(r, 5));
  const reply = h.posted.filter((m) => m.type === 'omnibox:host-reply').pop();
  assert.deepEqual({ exchange: reply.exchange, ok: reply.ok }, { exchange: 21, ok: true },
    '回信只表示"接下了"');
  const callbacks = h.posted.filter((m) => m.type === 'omnibox:host-callback');
  assert.equal(callbacks.length, 1, '应当把值作为 callback 交回子插件');
  assert.deepEqual(callbacks[0].data, { threshold: 8 });
  assert.equal(callbacks[0].action, 'ui', 'callback 的 action 与请求同名，子插件按它注册处理器');
  assert.equal(asked.length, 1);
});

console.log(failed ? `\n${failed} 例失败` : '\nHostChannel 宿主侧 8 例通过');
process.exit(failed ? 1 : 0);

