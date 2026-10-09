// Shell 共享「扩展入口」渲染的互斥用例（真实 base.js 装进 vm + DOM 替身 + 假 Bridge）。
//
// 锁的是这条不变量：**同一时刻只有一个"当前项"** —— 扩展入口与宿主侧栏项互斥。
// 为什么在壳这一层测：高亮原先由每个宿主自己写（两次 className 查询），media-player
// 只写了单向、image-viewer 写了双向才没暴露（先点「网易云登录」再点「全部音乐」两个
// 同时亮）。现在 renderExtensions 统一维护，宿主只剩一次 clearActive()。
//
// 用法：node tests/js/shell_extensions_active.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const BASE_JS = path.join(here, '..', '..', 'shell', 'frontend', 'public', 'shell', 'base.js');

let failed = 0;
let total = 0;
async function check(name, fn) {
  total++;
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

const EXTENSIONS = [
  { id: 'ncm-daily', label: '每日推荐', view: 'ncm-daily' },          // 宿主渲染的视图
  { id: 'cleaner', label: '相册清理', embedUrl: '/plugins/image-cleaner/frontend/index.html' },
  { id: 'wipe', label: '清理缓存', plugin: 'demo', method: 'wipe' },   // 纯动作型
];

/** 装载真实 base.js，返回 { renderExtensions, container, navs, opened, calls }。 */
function loadShell(extensions = EXTENSIONS) {
  const { document, makeEl } = createDom();
  const container = Object.assign(makeEl('div'), { id: 'ext-host' });
  document.body.appendChild(container);
  // 宿主侧栏项：两个作用域项，其中一个默认选中（模拟"全部音乐"亮着）
  const navs = ['recent', 'all-audio'].map(view => {
    const el = Object.assign(makeEl('button'), { id: 'nav-' + view });
    el.className = 'mp-nav-item' + (view === 'all-audio' ? ' active' : '');
    el.dataset.view = view;
    document.body.appendChild(el);
    return el;
  });

  const opened = [];
  const calls = [];
  // `window` 必须是沙箱全局本身：base.js 用 `window.Bridge = …` 定义桥，若 window 只是
  // 一个普通属性对象，`Bridge` 这个裸标识符在上下文里就找不到（浏览器里两者等价）。
  const sandbox = {
    document, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    location: { origin: 'http://127.0.0.1:1', href: 'http://127.0.0.1:1/x' },
    addEventListener() { }, removeEventListener() { }, postMessage() { },
    __extensions: extensions,
    __opened: opened,
    __calls: calls,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.parent = sandbox;      // 没有上层：HostChannel.self() 因此为 null
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });
  vm.runInContext(`
    Bridge.callSystem = (method) => Promise.resolve(method === 'system_get_plugin_extensions'
      ? window.__extensions : null);
    Bridge.callPlugin = (plugin, method) => {
      window.__calls.push(plugin + '.' + method);
      return Promise.resolve({ success: true });
    };
  `, sandbox);
  return {
    renderExtensions: vm.runInContext('renderExtensions', sandbox),
    container, navs, opened, calls, document,
    buttons: () => container.querySelectorAll('.obx-extension'),
    // 用下标而不是文案判断：替身不解析 innerHTML 里的文本节点
    activeExt: () => container.querySelectorAll('.obx-extension')
      .map((b, index) => (b.classList.contains('active') ? index : -1))
      .filter(index => index >= 0),
    activeNav: () => navs.filter(n => n.classList.contains('active')).map(n => n.dataset.view),
  };
}

const mount = (env, options) => env.renderExtensions(env.container, 'demo-host', 'sidebar', {
  navSelector: '.mp-nav-item',
  onOpen: (ext, btn) => env.opened.push(['open', ext.id, btn === env.buttons()[0]]),
  onEmbed: (ext, btn) => env.opened.push(['embed', ext.id, !!btn]),
  ...options,
});

// ---------- 扩展 → 侧栏 ----------

await check('点宿主渲染的扩展入口：它高亮、侧栏项全灭', async () => {
  const env = loadShell();
  const ctl = await mount(env);
  assert.equal(env.activeNav().length, 1, '前置：侧栏项默认亮着');

  env.buttons()[0].click();

  assert.deepEqual(env.opened, [['open', 'ncm-daily', true]], 'onOpen 要拿到 ext 与按钮');
  assert.equal(env.activeExt().length, 1, '扩展入口要亮且只亮一个');
  assert.deepEqual(env.activeNav(), [], '侧栏项必须被清掉（这就是原先漏掉的方向）');
  assert.equal(ctl.buttons.length, 3, '控制器要暴露全部按钮');
});

await check('点内嵌型扩展入口：同样互斥', async () => {
  const env = loadShell();
  await mount(env);

  env.buttons()[1].click();

  assert.deepEqual(env.opened, [['embed', 'cleaner', true]]);
  assert.equal(env.activeExt().length, 1);
  assert.deepEqual(env.activeNav(), []);
});

await check('纯动作型扩展不参与高亮（不谎报当前视图）', async () => {
  const env = loadShell();
  await mount(env);

  env.buttons()[2].click();
  await Promise.resolve();

  assert.deepEqual(env.calls, ['demo.wipe'], '动作型扩展照旧调用后端');
  assert.deepEqual(env.activeExt(), [], '不该高亮');
});

// ---------- 侧栏 → 扩展 ----------

await check('宿主切视图后 clearActive()：扩展高亮灭', async () => {
  const env = loadShell();
  const ctl = await mount(env);
  env.buttons()[0].click();
  assert.equal(env.activeExt().length, 1);

  ctl.clearActive();

  assert.deepEqual(env.activeExt(), [], '宿主切换视图时必须能清掉扩展高亮');
});

await check('activate(btn) 可由宿主直接调用（程序化打开扩展视图）', async () => {
  const env = loadShell();
  const ctl = await mount(env);

  ctl.activate(env.buttons()[1]);

  assert.deepEqual(env.activeExt(), [1], '第二个入口（相册清理）要亮');
  assert.deepEqual(env.activeNav(), ['all-audio'], 'activate 不动侧栏项（那是宿主的事）');
});

// ---------- 空场景 ----------

await check('没有扩展 / 容器为空时仍返回控制器（宿主无需判空）', async () => {
  const empty = loadShell([]);
  const ctlEmpty = await mount(empty);
  assert.equal(ctlEmpty.buttons.length, 0);
  ctlEmpty.clearActive();

  const noContainer = loadShell();
  const ctlNone = await noContainer.renderExtensions(null, 'demo-host', 'sidebar', {});
  assert.equal(typeof ctlNone.clearActive, 'function', '容器为空也要给控制器');
  ctlNone.clearActive();
});

console.log(failed ? `\n${failed} 例失败` : `\nshell 扩展入口互斥 ${total} 例通过`);
process.exit(failed ? 1 : 0);
