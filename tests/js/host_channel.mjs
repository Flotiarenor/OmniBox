// HostChannel（子插件 → 上层）协议自检。
//
// 背景：内嵌插件页要"请上层用它的文档渲染我的设置弹窗"时，走的是 base.js 的
// HostChannel：子页 postMessage 一个 request，上层收到后用 `event.source` 回信，
// 需要保存时由上层把值 callback 回子页、子页自己落盘。
//
// 这个脚本只跑**一个窗口**（子插件页），验证它在四种上层姿态下的行为：
//   · 没人应答      → probe=false / request={ok:false}，调用方得以回落到本地实现；
//   · 上层应答      → request 拿到 data，且 payload 形状符合契约；
//   · 来源不对      → 非父窗口发来的 reply / callback 一律忽略（防伪造）；
//   · 回调          → 上层 callback 的值能交给注册的处理器，处理器抛错回传 ok:false。
//
// 为什么不用两个 vm 上下文模拟父子窗口：Node 的 vm 在"函数放进 contextified sandbox
// 再由另一个上下文调用"时，闭包里的 `window` 会解析到调用者所在上下文的全局（实测：
// 子窗口调 probe，postMessage 却落在宿主窗口上）。那是宿主侧测试框架的坑，不是协议
// 行为；两侧之间的真实投递由 tests/js/host_channel_wiring.mjs 的宿主用例覆盖。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const BASE_JS = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'base.js');

const ORIGIN = 'http://127.0.0.1:18080';
const SENT = [];

function loadPluginRuntime() {
  const listeners = { message: [] };
  const location = { origin: ORIGIN, href: `${ORIGIN}/plugins/probe/frontend/index.html`, reload() { } };
  const body = { children: [], appendChild(el) { this.children.push(el); } };
  const parentWin = {
    location: { origin: ORIGIN },
    postMessage(payload) { SENT.push(payload); },
  };
  const win = {
    label: 'probe',
    location,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    removeEventListener() { },
    postMessage(payload) { SENT.push(payload); },
  };
  win.parent = parentWin;

  const sandbox = {
    window: win,
    parent: parentWin,
    document: {
      location,
      body,
      addEventListener() { },
      createElement: () => ({
        style: {}, classList: { add() { }, remove() { } }, appendChild() { },
        remove() { }, setAttribute() { }, addEventListener() { },
        // 只有 querySelectorAll 的元素是宿主侧的活，这里给空表即可
        querySelectorAll: () => [],
      }),
      querySelector: () => null,
      querySelectorAll: () => [],
      documentElement: { getAttribute: () => null, setAttribute() { }, style: { setProperty() { } } },
    },
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });
  return {
    hc: win.HostChannel,
    /** 模拟宿主/壳给本窗口派发一条消息 */
    emit(data, source = parentWin) {
      (listeners.message || []).slice().forEach((fn) => fn({ data, source, origin: ORIGIN }));
    },
  };
}

let failed = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

const rt = loadPluginRuntime();
const T = 80;

await check('HostChannel 暴露了子插件入口（request / requestSettings / onCallback）', () => {
  assert.equal(typeof rt.hc.request, 'function');
  assert.equal(typeof rt.hc.requestSettings, 'function');
  assert.equal(typeof rt.hc.onCallback, 'function');
  assert.equal(typeof rt.hc.probe, 'function');
});

await check('没人应答时 probe 为 false（短超时，不拖住调用方）', async () => {
  assert.equal(await rt.hc.probe(T), false);
});

await check('没人应答时 request 解析为 {ok:false}（调用方据此回落）', async () => {
  const res = await rt.hc.request('ui', { kind: 'settings' }, T);
  // 注意：值来自 vm 上下文，跨 realm 的 deepEqual 会因原型不同而失败，逐字段断言
  assert.equal(res.ok, false);
  assert.equal(res.data, null);
});

await check('发出的 request 形状符合契约（type/exchange/action/data）', () => {
  const req = SENT.find((m) => m.type === 'omnibox:host-request');
  assert.ok(req, '没有发出 host-request');
  assert.equal(typeof req.exchange, 'number');
  assert.equal(req.action, 'ui');
  assert.deepEqual(req.data, { kind: 'settings' });
});

await check('上层回信后 request 拿到 data', async () => {
  const pending = rt.hc.request('ui', { kind: 'settings' }, 200);
  rt.emit({ type: 'omnibox:host-reply', exchange: SENT[SENT.length - 1].exchange, ok: true, data: { saved: true } });
  const res = await pending;
  assert.equal(res.ok, true);
  assert.deepEqual(res.data, { saved: true });
});

await check('非父窗口发来的回信一律忽略（防伪造）', async () => {
  const pending = rt.hc.request('ui', { kind: 'settings' }, T);
  rt.emit({ type: 'omnibox:host-reply', exchange: SENT[SENT.length - 1].exchange, ok: true, data: { forged: true } },
    { location: { origin: ORIGIN }, postMessage() { } });   // 不是 window.parent
  const res = await pending;
  assert.equal(res.ok, false, '伪造来源不得被接受');
});

await check('上层 callback 把值交给注册的处理器，并回传结果', async () => {
  const got = [];
  rt.hc.onCallback('ui', (values) => { got.push(values); return 'done'; });
  const before = SENT.length;
  rt.emit({ type: 'omnibox:host-callback', step: 7, action: 'ui', data: { threshold: 8 } });
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(got, [{ threshold: 8 }]);
  const reply = SENT.slice(before).find((m) => m.type === 'omnibox:host-callback-reply');
  assert.ok(reply, '没有回传 callback-reply');
  assert.deepEqual({ step: reply.step, ok: reply.ok, data: reply.data }, { step: 7, ok: true, data: 'done' });
});

await check('处理器抛错时回传 ok:false 与原因（上层得以保住弹窗）', async () => {
  rt.hc.onCallback('ui', () => { throw new Error('保存失败：磁盘只读'); });
  const before = SENT.length;
  rt.emit({ type: 'omnibox:host-callback', step: 8, action: 'ui', data: {} });
  await new Promise((r) => setTimeout(r, 5));
  const reply = SENT.slice(before).find((m) => m.type === 'omnibox:host-callback-reply');
  assert.ok(reply, '没有回传 callback-reply');
  assert.equal(reply.ok, false);
  assert.match(String(reply.data && reply.data.error), /保存失败/);
});

console.log(failed ? `\n${failed} 例失败` : '\nHostChannel 子插件侧 8 例通过');
process.exit(failed ? 1 : 0);
