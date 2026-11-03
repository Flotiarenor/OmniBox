// 壳插件 iframe 挂载计划（shell/frontend/src/core/plugin-frames.js）的行为用例。
//
// 为什么不再用源码字符串断言：原先 tests/test_shell_lifecycle_wiring.py 用
// assertIn('filter(p => p.keepAlive)') / assertIn('filter(p => !p.keepAlive)') 守
// "常驻组极性"。两个方向都会失效 —— 改一句注释、拆行或改用 `if` 就红（假红），
// 而把两组极性写反、或让同一个插件同时进两组时，源码里那两段字符串仍然在（假绿）。
// 这里直接 import 真实实现，断言的是"哪个插件进哪一组、src 长什么样、谁被 dispose"。
//
// 用法：node tests/js/plugin_frames.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
// Windows 上必须转成 file:// URL：默认 ESM 加载器不认 `d:\...` 这种盘符路径
const PLAN = path.join(ROOT, 'shell', 'frontend', 'src', 'core', 'plugin-frames.js');
const APP = path.join(ROOT, 'shell', 'frontend', 'src', 'App.vue');

const plan = await import(pathToFileURL(PLAN).href);

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

const PLUGINS = [
  { name: 'media-player', keepAlive: true },
  { name: 'image-viewer' },
  { name: 'document-reader', keepAlive: false },
  { name: 'manga-library', keepAlive: true },
];

// ---------------------------------------------------------------- 常驻 / 过路分组

check('常驻组只收声明了 keepAlive 的插件，其余进过路组', () => {
  const { keepAlive, transient } = plan.splitByKeepAlive(PLUGINS);
  assert.deepEqual(keepAlive.map((p) => p.name), ['media-player', 'manga-library']);
  assert.deepEqual(transient.map((p) => p.name), ['image-viewer', 'document-reader']);
});

check('两组互补且不交叠：每个插件恰好落在一组', () => {
  const { keepAlive, transient } = plan.splitByKeepAlive(PLUGINS);
  assert.equal(keepAlive.length + transient.length, PLUGINS.length);
  const names = new Set([...keepAlive, ...transient].map((p) => p.name));
  assert.equal(names.size, PLUGINS.length, '有插件同时进了两组（iframe 会被渲染两次）');
});

check('空清单与缺省入参返回两组空数组（首屏插件未到达时不抛）', () => {
  assert.deepEqual(plan.splitByKeepAlive([]), { keepAlive: [], transient: [] });
  assert.deepEqual(plan.splitByKeepAlive(null), { keepAlive: [], transient: [] });
});

// ---------------------------------------------------------------- iframe src

check('chrome 插件只加载内容页：追加 content=1，否则不加', () => {
  assert.equal(
    plan.pluginFrameSrc({ entryUrl: '/plugins/x/frontend/index.html', chrome: { brand: {} } }, 0),
    '/plugins/x/frontend/index.html?content=1');
  assert.equal(
    plan.pluginFrameSrc({ entryUrl: '/plugins/x/frontend/index.html' }, 0),
    '/plugins/x/frontend/index.html',
    '没有 chrome 声明的插件不该收到 content=1');
});

check('entryUrl 已带查询串时用 & 续接', () => {
  assert.equal(
    plan.pluginFrameSrc({ entryUrl: '/plugins/x/frontend/index.html?tab=1', chrome: {} }, 0),
    '/plugins/x/frontend/index.html?tab=1&content=1');
});

check('重试计数以 _r 追加在末尾，且不截断 content=1', () => {
  const src = plan.pluginFrameSrc({ entryUrl: '/plugins/x/frontend/index.html', chrome: {} }, 3);
  assert.equal(src, '/plugins/x/frontend/index.html?content=1&_r=3');
  assert.ok(src.includes('content=1'), '重试参数覆盖了 content=1：壳与内容页会各画一套导航');
});

check('未重试（0 / 缺省 / 非法计数）不加 _r', () => {
  const plugin = { entryUrl: '/plugins/x/frontend/index.html', chrome: {} };
  for (const count of [0, undefined, null, -2, Number.NaN, 'abc']) {
    assert.ok(!plan.pluginFrameSrc(plugin, count).includes('_r='), `count=${String(count)} 仍追加了 _r`);
  }
});

// ---------------------------------------------------------------- 离开时 dispose

check('离开时只 dispose 不保活且仍挂载的 frame', () => {
  assert.deepEqual(plan.leavingFrames({
    registered: ['media-player', 'image-viewer', 'manga-library'],
    mounted: ['media-player', 'image-viewer'],
    keepAlive: ['media-player', 'manga-library'],
  }), ['image-viewer']);
});

check('保活插件离开不卸载 iframe（媒体播放不该被打断）', () => {
  assert.deepEqual(plan.leavingFrames({
    registered: ['media-player'],
    mounted: ['media-player'],
    keepAlive: ['media-player'],
  }), []);
});

check('已卸载过的 frame 不再重复 dispose', () => {
  assert.deepEqual(plan.leavingFrames({
    registered: ['image-viewer'],
    mounted: [],
    keepAlive: [],
  }), [], '未挂载的 frame 再 dispose 一次会重复触发插件的清理逻辑');
});

check('多个过路插件按注册顺序 dispose', () => {
  assert.deepEqual(plan.leavingFrames({
    registered: ['a', 'b', 'c'],
    mounted: ['c', 'a', 'b'],
    keepAlive: ['c'],
  }), ['a', 'b']);
});

// ---------------------------------------------------------------- 重载后的补发通知

check('重载后仍是活动插件且窗口可见：补一条 shown', () => {
  assert.deepEqual(plan.reloadedFrameMessages(true, true), ['omnibox:plugin-shown']);
});

check('后台重载或窗口不可见时不发 shown（新文档默认认为自己隐藏）', () => {
  assert.deepEqual(plan.reloadedFrameMessages(false, true), [], '告诉后台文档"可见"会让它在不可见时跑轮询');
  assert.deepEqual(plan.reloadedFrameMessages(true, false), []);
  assert.deepEqual(plan.reloadedFrameMessages(false, false), []);
});

// ---------------------------------------------------------------- 用例自身防漂移

check('用例读的是真实源码（模块与接线处都在），避免"测试全过但没测到东西"', () => {
  assert.ok(fs.existsSync(PLAN), `缺少 ${PLAN}`);
  const app = fs.readFileSync(APP, 'utf8');
  assert.ok(app.includes("from './core/plugin-frames'"), 'App.vue 没有 import core/plugin-frames');
});

console.log(failed ? `\nplugin_frames: ${failed} 项失败` : '\nplugin_frames: 全部通过');
process.exit(failed ? 1 : 0);
