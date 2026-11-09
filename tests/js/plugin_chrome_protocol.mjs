// 壳的插件 chrome 协议（shell/frontend/src/core/plugin-chrome-protocol.js）行为用例。
//
// 为什么不再用源码字符串断言：原先 tests/test_shell_ui_review.py 用
// `function_body` + `re.findall(r"data.type !== '(cubby:[a-z-]+)'")` 从 App.vue 的
// 处理器里反推白名单 —— 加一条分支、删一条分支、或把类型字面量换个写法都可能让
// 断言与实现一起漂移而仍然是绿的。这里 import 真实实现：白名单是数据，判定是函数。
//
// 同时锁住"补丁怎么并进当前状态"：badges 整份替换并丢空值（内容页靠它清除角标）、
// actions / nav / search 按键合并、status 缺字段回退。
//
// 用法：node tests/js/plugin_chrome_protocol.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const PROTOCOL = path.join(ROOT, 'shell', 'frontend', 'src', 'core', 'plugin-chrome-protocol.js');
const DOC = path.join(ROOT, 'docs', 'plugin-page-architecture.md');

const protocol = await import(pathToFileURL(PROTOCOL).href);

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

const EXPECTED_TYPES = [
  'cubby:ready',
  'cubby:chrome',
  'cubby:open-settings',
  'cubby:get-plugin-appearance',
  'cubby:set-plugin-alpha',
];

// ---------------------------------------------------------------- 白名单

check('白名单正好是这 5 个协议串且无重复', () => {
  const types = protocol.PLUGIN_CHROME_MESSAGE_TYPES;
  assert.equal(new Set(types).size, types.length, '白名单里有重复项');
  assert.deepEqual([...types].sort(), [...EXPECTED_TYPES].sort());
});

check('每个登记类型都能在插件页面架构文档的消息表里找到出处', () => {
  const doc = fs.readFileSync(DOC, 'utf8');
  for (const type of protocol.PLUGIN_CHROME_MESSAGE_TYPES) {
    assert.ok(doc.includes('`' + type + '`'), `${type} 未在 ${path.basename(DOC)} 的消息表里出现`);
  }
});

check('登记的类型全部被接受', () => {
  for (const type of protocol.PLUGIN_CHROME_MESSAGE_TYPES) {
    assert.equal(protocol.isPluginChromeMessage({ type }), true, `${type} 被拒了`);
  }
});

check('未登记类型与畸形消息一律拒绝', () => {
  const rejected = [
    null,
    undefined,
    0,
    '',
    'cubby:ready',                                  // 字符串本身不是消息体
    {},
    { type: null },
    { type: 'cubby:ready2' },
    { type: 'cubby:close-native' },                 // 全仓库已无发送方的历史协议串
  ];
  for (const data of rejected) {
    assert.equal(protocol.isPluginChromeMessage(data), false,
      `${JSON.stringify(data) ?? String(data)} 不该被当成 chrome 消息`);
  }
});

// ---------------------------------------------------------------- 状态合并

/** 一份"内容页刚上报过"的完整状态，逐例复制，避免用例之间互相污染。 */
function baseState() {
  return {
    title: '媒体播放器',
    subtitle: '',
    badges: { count: '3' },
    status: { text: '就绪', state: 'ready' },
    actions: { play: { visible: true } },
    nav: { main: { visible: true } },
    search: { enabled: true },
  };
}

check('字符串字段覆盖，非字符串保留旧值（空串是合法的"清空高亮"）', () => {
  const next = protocol.mergeChromeState(baseState(), { title: '歌词', subtitle: 5, view: '' });
  assert.equal(next.title, '歌词');
  assert.equal(next.subtitle, '', '非字符串不该覆盖已有副标题');
  assert.equal(next.view, '', 'view 的空串表示清掉内容页之前设的高亮');
});

check('badges 整份替换，空值即清除角标', () => {
  const next = protocol.mergeChromeState(baseState(), {
    badges: { next: '4', gone: '', nul: null, count: 0 },
  });
  assert.deepEqual(next.badges, { next: '4', count: '0' });
});

check('actions / nav / search 按键合并，未提到的键保留', () => {
  const next = protocol.mergeChromeState(baseState(), {
    actions: { next: { disabled: true } },
    nav: { reading: { badge: '2' } },
    search: { value: '关键词' },
  });
  assert.deepEqual(next.actions, { play: { visible: true }, next: { disabled: true } });
  assert.deepEqual(next.nav, { main: { visible: true }, reading: { badge: '2' } });
  assert.deepEqual(next.search, { enabled: true, value: '关键词' });
});

check('status 缺字段回退成空文本与 idle', () => {
  assert.deepEqual(protocol.mergeChromeState(baseState(), { status: { text: '播放中' } }).status,
    { text: '播放中', state: 'idle' });
  assert.deepEqual(protocol.mergeChromeState(baseState(), { status: {} }).status,
    { text: '', state: 'idle' });
});

check('statusItems 只接受数组：发错类型保留旧值，空数组才是清空', () => {
  const state = { ...baseState(), statusItems: [{ id: 'a', text: '1' }] };
  assert.deepEqual(protocol.mergeChromeState(state, { statusItems: 'oops' }).statusItems,
    [{ id: 'a', text: '1' }]);
  assert.deepEqual(protocol.mergeChromeState(state, { statusItems: [] }).statusItems, []);
});

check('不就地修改传入状态（App.vue 用赋值替换整条记录）', () => {
  const state = baseState();
  const snapshot = JSON.parse(JSON.stringify(state));
  const next = protocol.mergeChromeState(state, { title: '新标题', badges: { a: '1' } });
  assert.notEqual(next, state);
  assert.deepEqual(state, snapshot, '传入的状态被就地改写了');
});

check('未提到的字段不影响既有状态', () => {
  assert.deepEqual(protocol.mergeChromeState(baseState(), { unknown: true, kind: 'whatever' }), baseState());
});

// ---------------------------------------------------------------- 用例自身防漂移

check('用例读的是真实源码：实现与文档都在', () => {
  assert.ok(fs.existsSync(PROTOCOL), `缺少 ${PROTOCOL}`);
  assert.ok(fs.existsSync(DOC), `缺少 ${DOC}`);
});

console.log(failed ? `\nplugin_chrome_protocol: ${failed} 项失败` : '\nplugin_chrome_protocol: 全部通过');
process.exit(failed ? 1 : 0);
