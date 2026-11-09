// 壳的刷新通知面：`omnibox:freshness` 富事件、旧 action 兼容、设置页「数据与缓存」接线。
//
// 锁住这几件事：
//   1. `buildFreshnessEvent` 真的把后端报告原样透传（内容页据此决定重载哪一层），
//      并且不改写入参；
//   2. 壳发的是**两条**消息：`omnibox:freshness`（带 scope + report）与旧的
//      `omnibox:action: refresh`（只认旧协议的内容页仍然工作）；
//   3. 内容页侧 `/shell/chrome-client.js` 把 action 消息的 `payload` 原样交给
//      `onAction` 的第二参（普通动作的第二参是 undefined），富事件走 `onMessage`；
//   4. 设置页接上了 `system_freshness_overview` / `system_freshness_rebuild`：
//      隐藏插件没有工具栏，那张表是它们唯一的刷新入口，重建必须二次确认。
//
// 三个壳文件是 SFC / 单文件组件，node 无法 import；它们的接线用源码契约断言
// （与 `tests/test_shell_ui_review.py` 同一手法），可执行的行为落在
// `plugin-chrome-protocol.js` 与 `/shell/chrome-client.js` 上。**模板绑定除外**：
// `@action="onChromeAction(p.name, $event)"` 这种写法只能用 Vue 自己的编译器证明
// （`$event` 只带第一个 emit 参数），所以下面用 `@vue/compiler-dom` 编译真实表达式
// 并执行编译产物 —— 源码正则看不出"第二个参数被丢掉"。
//
// 用法：node tests/js/shell_freshness_notify.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { loadShellScripts } from './shell_scripts.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const PROTOCOL = path.join(ROOT, 'shell', 'frontend', 'src', 'core', 'plugin-chrome-protocol.js');
const PLUGIN_CHROME = path.join(ROOT, 'shell', 'frontend', 'src', 'components', 'PluginChrome.vue');
const APP = path.join(ROOT, 'shell', 'frontend', 'src', 'App.vue');
const SETTINGS = path.join(ROOT, 'shell', 'frontend', 'src', 'views', 'SettingsView.vue');
const COMPILER = path.join(ROOT, 'shell', 'frontend', 'node_modules', '@vue', 'compiler-dom');

const require = createRequire(import.meta.url);
const { compile } = (() => {
  try {
    return require(COMPILER);
  } catch (e) {
    throw new Error('编译模板绑定需要 @vue/compiler-dom（先 npm --prefix shell/frontend install）：' + e.message);
  }
})();

const protocol = await import(pathToFileURL(PROTOCOL).href);
const chromeSource = readFileSync(PLUGIN_CHROME, 'utf8');
const appSource = readFileSync(APP, 'utf8');
const settingsSource = readFileSync(SETTINGS, 'utf8');

let failed = 0;
let total = 0;
function check(name, fn) {
  total++;
  try {
    fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

// ---------------------------------------------------------------- 事件构造

check('事件类型是 omnibox:freshness', () => {
  assert.equal(protocol.FRESHNESS_EVENT, 'omnibox:freshness');
});

check('负载正好是 {type, plugin, scope, report}，报告原样透传', () => {
  const report = { action: 'sync', added: 2, changed: 1, removed: 0, scope: 'a/b' };
  const event = protocol.buildFreshnessEvent('image-viewer', 'a/b', report);
  assert.deepEqual(event, {
    type: 'omnibox:freshness',
    plugin: 'image-viewer',
    scope: 'a/b',
    report: { action: 'sync', added: 2, changed: 1, removed: 0, scope: 'a/b' },
  });
  // 报告里的字段一个不少：内容页要靠 added/changed/removed/invalidated 做判断
  assert.deepEqual(event.report, report, '报告被改写或丢字段');
});

check('不改写入参（报告与作用域都原样交出去）', () => {
  const report = { action: 'verify', invalidated: true };
  const before = JSON.stringify(report);
  const event = protocol.buildFreshnessEvent('p', 'x/y', report);
  assert.equal(JSON.stringify(report), before, '报告被就地改写了');
  assert.equal(event.scope, 'x/y');
});

check('非对象报告退化成 null，非字符串作用域退化成空串', () => {
  for (const bad of [undefined, null, 'oops', 5, true]) {
    const event = protocol.buildFreshnessEvent('p', 'x', bad);
    assert.equal(event.report, null, `report=${String(bad)} 不该原样带出去`);
  }
  assert.equal(protocol.buildFreshnessEvent('p', 42, null).scope, '');
  assert.equal(protocol.buildFreshnessEvent('p', null, null).scope, '');
});

check('缺插件名时是空串（壳按目标 iframe 名调用，不会传空）', () => {
  assert.equal(protocol.buildFreshnessEvent('', '', null).plugin, '');
  assert.equal(protocol.buildFreshnessEvent(undefined, '', null).plugin, '');
});

// ---------------------------------------------------------------- 壳侧接线

check('PluginChrome 的 onChange 同时发富事件与旧 action', () => {
  assert.match(chromeSource, /onChange:\s*onFreshnessChange/, 'onChange 必须走统一收口');
  assert.match(chromeSource, /emit\('freshness',\s*payload\)/, '缺少 omnibox:freshness 事件');
  assert.match(chromeSource, /emit\('action',\s*'refresh',\s*payload\)/,
    '缺少旧的 action: refresh（只认旧协议的内容页会收不到刷新）');
  const body = /function onFreshnessChange[\s\S]{0,600}?\n}/.exec(chromeSource);
  assert.ok(body, '没有找到 onFreshnessChange');
  assert.match(body[0], /freshnessEventScope\(report,\s*freshnessScope\.value\)/,
    '作用域必须取报告自己的 scope（换目录时否则会错层重载）');
  assert.match(body[0], /report,?\s*}/, '负载必须带报告');
});

check('PluginChrome 声明了 freshness 事件与可选的 action 负载', () => {
  assert.match(chromeSource, /\(e:\s*'freshness',\s*payload:\s*\{[^}]*plugin[^}]*scope[^}]*report/,
    'defineEmits 必须声明 freshness 的负载形状');
  assert.match(chromeSource, /\(e:\s*'action',\s*id:\s*string,\s*payload\?:\s*unknown\)/,
    'action 必须允许附带 payload');
});

check('App.vue 用协议模块构造富事件并转给内容页', () => {
  assert.match(appSource, /buildFreshnessEvent\(/, 'App.vue 没有走 buildFreshnessEvent');
  const handler = /function onChromeFreshness[\s\S]{0,400}?\n}/.exec(appSource);
  assert.ok(handler, '没有找到 onChromeFreshness');
  assert.match(handler[0], /postToFrame\(/, '富事件没有发给内容页');
  assert.match(handler[0], /typeof payload\.scope === 'string'/,
    '作用域必须做类型收窄（消息来自 iframe）');
});

check('App.vue 在两个 PluginChrome 挂载点都接了 freshness', () => {
  const listeners = appSource.match(/@freshness="onChromeFreshness\(p\.name, \$event\)"/g) || [];
  const chromes = appSource.match(/<PluginChrome\b/g) || [];
  assert.equal(chromes.length, 2, `PluginChrome 挂载点数变了: ${chromes.length}`);
  assert.equal(listeners.length, chromes.length,
    `每个 PluginChrome 都要接 @freshness（实际 ${listeners.length}/${chromes.length}）`);
});

// ---------------------------------------------------------------- App.vue 模板：参数透传

/**
 * 用真实 Vue 编译器编译一段模板片段，返回挂到 `PluginChrome` 上的 props。
 *
 * 编译产物是 ESM（import / export），这里剥掉外壳后用 `new Function` 执行：
 * `_resolveComponent` / `_openBlock` / `_createBlock` 是编译器按需引用的运行时函数。
 */
function compileTemplate(template, ctx) {
  const { code } = compile(template, { mode: 'module' });
  const body = code
    .replace(/^import[^\n]*\n?/m, '')
    .replace('export function render', 'function render');
  const factory = new Function(
    '_resolveComponent', '_openBlock', '_createBlock', '_ctx',
    `${body}\nreturn render;`,
  );
  let props = null;
  const render = factory(
    (name) => name,
    () => [],
    (_type, captured) => { props = captured; return { type: 'plugin-chrome' }; },
    ctx,
  );
  render(ctx, []);
  return props;
}

check('App.vue 的 @action 绑定把 emit 的全部参数转发给处理器', () => {
  const bindings = [...appSource.matchAll(/@action="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(bindings.length, 2, `两个 PluginChrome 挂载点都要有 @action：${bindings.length}`);
  const report = { action: 'sync', added: 2, changed: 0, removed: 0 };
  const payload = { plugin: 'image-viewer', scope: 'a/b', report };
  for (const binding of bindings) {
    const seen = [];
    const props = compileTemplate(`<PluginChrome @action='${binding.replace(/'/g, '&#39;')}' />`, {
      p: { name: 'image-viewer' },
      onChromeAction: (...args) => seen.push(args),
    });
    assert.equal(typeof props.onAction, 'function', `绑定写法没有生成处理器：${binding}`);
    props.onAction('refresh', payload);
    assert.deepEqual(seen, [['image-viewer', 'refresh', payload]],
      `绑定 "${binding}" 丢掉 emit 的第二个参数（payload）`);
    // 交给真实协议函数：内容页最终收到的就是这条消息
    assert.deepEqual(protocol.buildActionEvent(seen[0][1], seen[0][2]),
      { type: 'omnibox:action', action: 'refresh', payload });
  }
});

check('旧 action 消息：有负载带 payload，无负载不带该字段', () => {
  assert.match(appSource, /buildActionEvent\(action,\s*payload\)/,
    'App.vue 必须走 buildActionEvent（payload 字段只在有值时出现）');
  const payload = { plugin: 'p', scope: '', report: { action: 'verify' } };
  assert.deepEqual(protocol.buildActionEvent('refresh', payload),
    { type: 'omnibox:action', action: 'refresh', payload });
  assert.deepEqual(protocol.buildActionEvent('back'), { type: 'omnibox:action', action: 'back' });
  assert.ok(!('payload' in protocol.buildActionEvent('back')), '无负载动作不该多带 payload 字段');
});

// ---------------------------------------------------------------- 触发面（B4）

check('挂载后与视图/作用域变化各触发一次，且两处都在组件里', () => {
  const mount = /function mountFreshness\(\)[\s\S]*?\n}/.exec(chromeSource);
  assert.ok(mount, '没有找到 mountFreshness');
  assert.match(mount[0], /syncFreshness\(\)/, '挂载后必须同步一次（mount 自己不扫盘）');
  // 删掉这条 watch（或删掉它调 syncFreshness）都必须变红
  assert.match(chromeSource, /watch\(\[activeView,\s*freshnessScope\],\s*\(\)\s*=>\s*\{?\s*syncFreshness\(\)/,
    '视图 / 作用域变化的触发接线不见了');
  // tools/check_plugins.py 的壳挂载检查按这段文本判定"挂载后有触发"
  assert.match(chromeSource, /\.autoSync\(/, '组件里必须直接出现 autoSync(...) 调用');
});

check('触发决策：不可用控件 / native 面板打开时不发同步', () => {
  const calls = [];
  const controller = { autoSync: (scope) => calls.push(scope) };
  const target = protocol.freshnessSyncTarget(controller, 'a/b', { blocked: false });
  assert.equal(target.controller, controller);
  assert.equal(target.scope, 'a/b');
  target.controller.autoSync(target.scope);
  assert.deepEqual(calls, ['a/b']);
  assert.equal(protocol.freshnessSyncTarget(controller, 'a/b', { blocked: true }), null,
    'native 面板打开时内容 iframe 隐藏，不该扫盘');
  assert.equal(protocol.freshnessSyncTarget(null, 'a/b'), null);
  assert.equal(protocol.freshnessSyncTarget({}, 'a/b'), null, '没有 autoSync 的控件不算可用');
  assert.equal(protocol.freshnessSyncTarget(controller, 42).scope, '',
    '非字符串作用域回落到全部根');
});

check('事件作用域优先用报告自己的 scope（同步途中换目录不错层）', () => {
  assert.equal(protocol.freshnessEventScope({ scope: 'old/dir' }, 'new/dir'), 'old/dir');
  assert.equal(protocol.freshnessEventScope({ action: 'verify' }, 'new/dir'), 'new/dir');
  assert.equal(protocol.freshnessEventScope({ scope: 42 }, 'new/dir'), 'new/dir');
  assert.equal(protocol.freshnessEventScope(null, 'new/dir'), 'new/dir');
  assert.equal(protocol.freshnessEventScope({}, 42), '');
});

// ---------------------------------------------------------------- 内容页侧的收口

const CLIENT_ORIGIN = 'http://127.0.0.1:18080';

/**
 * 最小 window 替身：运行真实的 `/shell/chrome-client.js`。
 *
 * `window` 就是上下文本身（与浏览器一致），壳那一侧只保留 `postMessage`。
 */
function createClientEnv() {
  const sent = [];
  const listeners = [];
  const fakeParent = {
    postMessage(payload, targetOrigin) { sent.push({ payload, targetOrigin }); },
  };
  // 回声窗口按 `Date.now()` 判定：给沙箱一个可推进的时钟，用例不必真的睡。
  const clock = { now: Date.now() };
  const sandbox = {
    console,
    location: { origin: CLIENT_ORIGIN },
    Date: { now: () => clock.now },
    setTimeout, clearTimeout, setInterval, clearInterval,
    addEventListener(type, fn) { if (type === 'message') listeners.push(fn); },
    removeEventListener(type, fn) {
      const at = listeners.indexOf(fn);
      if (at >= 0) listeners.splice(at, 1);
    },
  };
  sandbox.window = sandbox;
  sandbox.parent = fakeParent;
  const ctx = vm.createContext(sandbox);
  loadShellScripts(ctx, ['chrome-client.js']);
  return {
    client: sandbox.ChromeClient,
    listeners,
    advance(ms) { clock.now += ms; },
    dispatch(data) {
      listeners.slice().forEach((fn) => fn({ data, source: fakeParent, origin: CLIENT_ORIGIN }));
    },
  };
}

check('chrome-client 把 action 的 payload 交给 onAction 第二参', () => {
  const env = createClientEnv();
  const seen = [];
  env.client.create({ onAction: (action, payload) => seen.push({ action, payload }) }).bind();
  const report = { action: 'sync', added: 2, changed: 1, removed: 0, invalidated: false };
  // 壳在 refresh 动作上附的负载与富事件同形：{plugin, scope, report}
  const payload = { plugin: 'image-viewer', scope: 'a/b', report };
  env.dispatch({ type: 'omnibox:action', action: 'refresh', payload });

  assert.equal(seen.length, 1, 'refresh 应触发一次 onAction');
  assert.equal(seen[0].action, 'refresh');
  assert.equal(seen[0].payload.plugin, 'image-viewer');
  assert.equal(seen[0].payload.scope, 'a/b');
  assert.equal(JSON.stringify(seen[0].payload.report), JSON.stringify(report),
    'report 必须原样到达内容页（字段一个不少）');
});

check('没有 payload 的普通动作：onAction 第二参是 undefined', () => {
  const env = createClientEnv();
  const seen = [];
  env.client.create({ onAction: (...args) => seen.push(args) }).bind();
  env.dispatch({ type: 'omnibox:action', action: 'back' });
  env.dispatch({ type: 'omnibox:action', action: 'settings-saved' });

  assert.deepEqual(seen.map((args) => args[0]), ['back', 'settings-saved']);
  assert.ok(seen.every((args) => args.length === 2 && args[1] === undefined),
    '旧内容页忽略第二参，但第二参必须存在且为 undefined');
  assert.equal(seen[0][1], undefined, '不能把 undefined 变成 null 或空对象');
});

check('omnibox:freshness 富事件走 onMessage，不冒充 action', () => {
  const env = createClientEnv();
  const actions = [];
  const messages = [];
  env.client.create({
    onAction: (action) => actions.push(action),
    onMessage: (data) => messages.push(data),
  }).bind();
  env.dispatch({
    type: 'omnibox:freshness',
    plugin: 'image-viewer',
    scope: 'a/b',
    report: { action: 'verify', added: 0, changed: 0, removed: 0 },
  });

  assert.deepEqual(actions, [], '富事件不该触发 onAction');
  assert.equal(messages.length, 1, '富事件由 onMessage 交给内容页');
  assert.equal(messages[0].type, 'omnibox:freshness');
  assert.equal(messages[0].scope, 'a/b');
  assert.equal(messages[0].report.action, 'verify');
});

// ---------------------------------------------------------------- 两条都处理时的去重语义（B10）
//
// 壳在一次刷新结论后发两条消息：富事件先、带 payload 的 `refresh` 随后。同时处理两条的
// 页面必须只重载一次；`chrome-client` 的 `dedupeFreshness` 就是把这条语义落在握手里，
// 页面不必各自再写一个时间窗。

check('两条都处理时：带 payload 的 refresh 回声只处理一次', () => {
  const env = createClientEnv();
  const actions = [];
  const messages = [];
  env.client.create({
    onAction: (action, payload) => actions.push({ action, payload }),
    onMessage: (data) => messages.push(data),
  }, { dedupeFreshness: true, echoWindowMs: 500 }).bind();
  const payload = { plugin: 'image-viewer', scope: 'a/b', report: { action: 'sync', added: 2 } };
  env.dispatch({ type: 'omnibox:freshness', plugin: 'image-viewer', scope: 'a/b', report: payload.report });
  env.dispatch({ type: 'omnibox:action', action: 'refresh', payload });

  assert.equal(messages.length, 1, '富事件照常交给 onMessage（重载由它负责）');
  assert.deepEqual(actions, [], '回声 refresh 不该再触发一次重载');
});

check('没有富事件在前的 refresh 照常处理（旧页面/独立刷新仍然工作）', () => {
  const env = createClientEnv();
  const actions = [];
  env.client.create({ onAction: (action) => actions.push(action) },
    { dedupeFreshness: true, echoWindowMs: 500 }).bind();
  env.dispatch({ type: 'omnibox:action', action: 'refresh', payload: { plugin: 'p', report: null } });
  assert.deepEqual(actions, ['refresh']);
});

check('默认不去重：只认旧 action 的内容页行为不变', () => {
  const env = createClientEnv();
  const actions = [];
  env.client.create({ onAction: (action) => actions.push(action) }).bind();
  env.dispatch({ type: 'omnibox:freshness', plugin: 'p', scope: '', report: { action: 'sync' } });
  env.dispatch({ type: 'omnibox:action', action: 'refresh', payload: { plugin: 'p' } });
  assert.deepEqual(actions, ['refresh'], '没开启去重时第二条必须照常送达');
});

check('窗口之外的 refresh 照常处理（回声窗口不能把真动作吞掉）', () => {
  const env = createClientEnv();
  const actions = [];
  env.client.create({ onAction: (action) => actions.push(action) },
    { dedupeFreshness: true, echoWindowMs: 20 }).bind();
  env.dispatch({ type: 'omnibox:freshness', plugin: 'p', scope: '', report: { action: 'sync' } });
  env.advance(60);
  env.dispatch({ type: 'omnibox:action', action: 'refresh', payload: { plugin: 'p' } });
  assert.deepEqual(actions, ['refresh']);
});

// ---------------------------------------------------------------- 设置页

check('设置页接上了统一刷新概览与重建入口', () => {
  assert.match(settingsSource, /system_freshness_overview/, '没有读取状态概览');
  assert.match(settingsSource, /system_freshness_rebuild/, '没有重建入口');
  assert.match(settingsSource, /function freshnessStatus/, '没有状态列文案');
  const rebuild = /async function rebuildFreshness[\s\S]{0,900}?\n}/.exec(settingsSource);
  assert.ok(rebuild, '没有找到 rebuildFreshness');
  assert.match(rebuild[0], /window\.confirm\(/, '重建必须先二次确认');
  assert.match(rebuild[0], /await loadFreshness\(\)/, '重建后要重读状态');
});

check('概览字段按后端 state() 的投影读取', () => {
  const block = /interface FreshnessState \{[\s\S]*?\n\}/.exec(settingsSource);
  assert.ok(block, '没有 FreshnessState 接口');
  for (const key of ['entries', 'dirs', 'version_stale', 'stale_dirs', 'unreachable_roots',
    'last_verify_at', 'last_sync_at', 'busy', 'unit', 'mode', 'ttl_seconds', 'ttl_remaining']) {
    assert.match(block[0], new RegExp(`\\b${key}\\??:`), `FreshnessState 缺字段 ${key}`);
  }
});

check('概览：busy 期间 1.5 秒轮询，卸载与离开设置页都停', () => {
  assert.match(settingsSource, /FRESHNESS_POLL_MS = 1500/, '轮询间隔应为 1.5 秒');
  const schedule = /function scheduleFreshnessPoll[\s\S]{0,700}?\n\}/.exec(settingsSource);
  assert.ok(schedule, '没有 scheduleFreshnessPoll');
  assert.match(schedule[0], /some\(item => item\.busy\)/, '只在有任务在跑时轮询');
  assert.match(schedule[0], /route\.path !== '\/settings'/, '不在设置页时不该为看不见的表轮询');
  assert.match(schedule[0], /window\.setTimeout/, '用 setTimeout 串下一轮');
  assert.match(schedule[0], /await loadFreshness\(\)/, '轮询要重读概览');
  const unmount = /onUnmounted\([\s\S]{0,500}?\n\}\)/.exec(settingsSource);
  assert.ok(unmount, '没有找到 onUnmounted');
  assert.match(unmount[0], /stopFreshnessPoll\(\)/, '卸载时必须清掉轮询定时器');
  const routeWatch = /watch\(\(\) => route\.path[\s\S]{0,700}?\n\}\)/.exec(settingsSource);
  assert.ok(routeWatch, '没有找到路由监听');
  assert.match(routeWatch[0], /stopFreshnessPoll\(\)/, '离开设置页要停轮询');
});

check('概览：403 说权限、旧内核才说"不提供"，不可达根也要显示', () => {
  assert.match(protocol.freshnessOverviewErrorText(new Error('禁止访问')), /管理员权限/);
  assert.match(protocol.freshnessOverviewErrorText(new Error('API x 请求失败 (HTTP 403)')), /管理员权限/);
  assert.match(protocol.freshnessOverviewErrorText(new Error('该操作需要管理员权限（当前主体：guest）')),
    /管理员权限/);
  assert.equal(protocol.freshnessOverviewErrorText(new Error('404 not found')),
    '当前内核不提供统一刷新状态。');
  assert.match(settingsSource, /freshnessOverviewErrorText\(e\)/, '设置页必须用这个分类');
  assert.match(settingsSource, /\{\{ freshnessUnavailable \}\}/, '文案要真的渲染出来');
  assert.match(settingsSource, /unreachableRoots\(item\)/, '概览要显示不可达根');
});

check('隐藏插件没有工具栏，概览表就是它们唯一的入口', () => {
  // 表格按插件逐行渲染并以插件名显示；不依赖 getPlugins() 的可见清单
  assert.match(settingsSource, /v-for="item in freshnessStates"/, '没有逐插件渲染');
  assert.match(settingsSource, /pluginLabel\(item\.plugin\)/, '插件名要能回落到 name');
  const label = /function pluginLabel[\s\S]{0,300}?\|\|\s*name/.exec(settingsSource);
  assert.ok(label, 'pluginLabel 必须回落到插件标识（隐藏插件不在可见清单里）');
});

check('用例读的是真实源码', () => {
  for (const file of [PROTOCOL, PLUGIN_CHROME, APP, SETTINGS]) {
    assert.ok(readFileSync(file, 'utf8').length > 0, `缺少 ${file}`);
  }
});

console.log(failed ? `\n${failed} 例失败` : `\nshell freshness 通知面 ${total} 例通过`);
process.exit(failed ? 1 : 0);
