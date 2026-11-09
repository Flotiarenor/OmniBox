// 内容页 ↔ 壳的 chrome 握手客户端（/shell/chrome-client.js）的行为用例。
//
// 背景：document-reader / image-viewer / manga-library / media-player 曾各写一份同形状的
// 握手（父窗口判空、来源校验、cubby:view/action/query 分发、cubby:ready）。协议收敛到
// 一份之后，最容易悄悄坏掉的是两条语义：
//
//   1. **来源校验**：只接受父窗口 + 同源的消息。写漏了，任意同源页面都能驱动本插件；
//   2. **先监听、再 ready**：壳收到 ready 会立刻补发当前视图，反了顺序就丢首屏视图。
//
// 两者在界面上都只表现为"某个插件点了没反应"，所以这里用最小 window 替身跑真实实现，
// 并核对四份 app.js 确实走同一份实现（没有哪一份还留着自写的分发）。
//
// 用法：node tests/js/chrome_client.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { loadShellScripts, REPO_ROOT } from './shell_scripts.mjs';

const ORIGIN = 'http://127.0.0.1:18080';
// 共享同一份握手实现的五个内容页。
const PLUGIN_APPS = [
  'plugins/document-reader/frontend/js/app.js',
  'plugins/group-mesh/frontend/js/app.js',
  'plugins/image-viewer/frontend/js/app.js',
  'plugins/manga-library/frontend/js/app.js',
  'plugins/media-player/frontend/js/app.js',
];

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

/** 最小替身：window 就是上下文本身（与浏览器一致），父子窗口靠 parent 区分。 */
function makeSandbox({ embedded = true, origin = ORIGIN } = {}) {
  const sent = [];
  const listeners = [];
  const fakeParent = {
    postMessage(payload, targetOrigin) { sent.push({ payload, targetOrigin }); },
  };
  const sandbox = {
    console,
    location: { origin },
    setTimeout, clearTimeout, setInterval, clearInterval,
    addEventListener(type, fn) { if (type === 'message') listeners.push(fn); },
    removeEventListener(type, fn) {
      const at = listeners.indexOf(fn);
      if (at >= 0) listeners.splice(at, 1);
    },
  };
  sandbox.window = sandbox;
  sandbox.parent = embedded ? fakeParent : sandbox;
  const ctx = vm.createContext(sandbox);
  loadShellScripts(ctx, ['chrome-client.js']);
  return {
    sent,
    client: sandbox.ChromeClient,
    listeners,
    /** 模拟壳发来一条消息（默认来自父窗口、同源）。 */
    dispatch(data, { source = fakeParent, messageOrigin = origin } = {}) {
      listeners.slice().forEach((fn) => fn({ data, source, origin: messageOrigin }));
    },
  };
}

// ---------------------------------------------------------------- 握手顺序

check('bind() 先注册监听、再发 cubby:ready（顺序反了会丢首屏视图）', () => {
  const env = makeSandbox();
  const calls = [];
  const client = env.client.create({ onView: () => calls.push('view') });
  assert.equal(env.listeners.length, 0);
  client.bind();
  assert.equal(env.listeners.length, 1, 'bind 之后应有且只有一个 message 监听器');
  assert.equal(env.sent.length, 1);
  // 跨 realm 的对象不能用 deepEqual（沙箱里的 Object.prototype 与宿主不是同一个），逐字段断言
  assert.equal(env.sent[0].payload.type, 'cubby:ready');
  assert.equal(env.sent[0].targetOrigin, ORIGIN, 'ready 也要带明确 origin');
  assert.equal(client.bound, true);
});

check('重复 bind() 不重复注册（壳与插件重连时不会叠出两条 ready）', () => {
  const env = makeSandbox();
  const client = env.client.create({});
  client.bind();
  client.bind();
  assert.equal(env.listeners.length, 1);
  assert.equal(env.sent.length, 1);
});

check('unbind() 摘掉监听，之后的 bind() 能重新挂上', () => {
  const env = makeSandbox();
  const client = env.client.create({});
  client.bind();
  client.unbind();
  assert.equal(env.listeners.length, 0);
  assert.equal(client.bound, false);
  client.bind();
  assert.equal(env.listeners.length, 1);
});

// ---------------------------------------------------------------- 来源校验

check('只接受父窗口 + 同源的消息：伪造来源一律忽略', () => {
  const env = makeSandbox();
  const views = [];
  env.client.create({ onView: (view) => views.push(view) }).bind();
  env.dispatch({ type: 'cubby:view', view: 'albums' });
  assert.deepEqual(views, ['albums'], '同源父窗口的消息应被分发');
  env.dispatch({ type: 'cubby:view', view: 'timeline' }, { source: { fake: true } });
  env.dispatch({ type: 'cubby:view', view: 'latest' }, { messageOrigin: 'http://evil.tld' });
  assert.deepEqual(views, ['albums'], '伪造来源与跨源消息都不得分发');
});

// ---------------------------------------------------------------- 分发

check('view / action / query 分发给对应回调，其余类型走 onMessage', () => {
  const env = makeSandbox();
  const got = [];
  env.client.create({
    onView: (view) => got.push(['view', view]),
    onAction: (action) => got.push(['action', action]),
    onQuery: (query) => got.push(['query', query]),
    onMessage: (data) => got.push(['other', data.type]),
  }).bind();
  env.dispatch({ type: 'cubby:view', view: 'albums' });
  env.dispatch({ type: 'cubby:action', action: 'settings-saved' });
  env.dispatch({ type: 'cubby:query', query: '猫' });
  env.dispatch({ type: 'cubby:appearance', alpha: 60 });
  assert.deepEqual(got, [
    ['view', 'albums'],
    ['action', 'settings-saved'],
    ['query', '猫'],
    ['other', 'cubby:appearance'],
  ]);
});

check('view 只接受字符串：非字符串不调用 onView（避免把 undefined 当视图）', () => {
  const env = makeSandbox();
  const views = [];
  env.client.create({ onView: (view) => views.push(view) }).bind();
  env.dispatch({ type: 'cubby:view', view: 3 });
  env.dispatch({ type: 'cubby:view' });
  assert.deepEqual(views, []);
});

check('没有给回调时静默忽略，不抛错', () => {
  const env = makeSandbox();
  env.client.create({}).bind();
  env.dispatch({ type: 'cubby:view', view: 'albums' });
  env.dispatch({ type: 'cubby:action', action: 'x' });
  env.dispatch({ type: 'cubby:query', query: 'x' });
  assert.equal(env.sent.length, 1, '除了 ready 之外不该多出消息');
});

// ---------------------------------------------------------------- 上报

check('chrome(patch) 补上 cubby:chrome 类型并保留插件的字段', () => {
  const env = makeSandbox();
  env.client.create({}).chrome({ title: '相册', view: '' });
  const payload = env.sent.at(-1).payload;
  assert.equal(payload.type, 'cubby:chrome');
  assert.equal(payload.title, '相册');
  assert.equal(payload.view, '', '空串视图是合法的（取消导航高亮）');
});

check('post / send 发的是插件自己定义的类型（open-settings 等）', () => {
  const env = makeSandbox();
  env.client.post({ type: 'cubby:open-settings', title: '歌词设置' });
  env.client.create({}).send({ type: 'cubby:ready' });
  assert.deepEqual(env.sent.map((m) => m.payload.type),
    ['cubby:open-settings', 'cubby:ready']);
});

check('不嵌在壳里（parent === window）时不发任何消息', () => {
  const env = makeSandbox({ embedded: false });
  const client = env.client.create({});
  assert.equal(client.embedded, false);
  client.bind();
  client.post({ type: 'cubby:chrome' });
  assert.deepEqual(env.sent, []);
  assert.equal(env.listeners.length, 1, '监听仍会挂上，只是不发消息');
});

// ---------------------------------------------------------------- 四份 app.js 走同一实现

check('五个内容页的 app.js 都通过 window.ChromeClient 收发，没有自写的分发', () => {
  for (const rel of PLUGIN_APPS) {
    const source = fs.readFileSync(path.join(REPO_ROOT, rel), 'utf8');
    assert.ok(source.includes('window.ChromeClient.create('), `${rel} 没有用共享的 create()`);
    assert.ok(source.includes('window.ChromeClient.post('), `${rel} 没有用共享的 post()`);
    assert.ok(!source.includes('event.source !== window.parent'),
      `${rel} 还留着自己写的来源校验（协议又分叉了）`);
    assert.ok(!source.includes("data.type === 'cubby:view'"),
      `${rel} 还留着自己写的消息分发`);
  }
});

console.log(failed ? `\nchrome_client: ${failed} 项失败` : '\nchrome_client: 全部通过');
process.exit(failed ? 1 : 0);
