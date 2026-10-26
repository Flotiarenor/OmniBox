// image-cleaner 原生面板（frontend/js/native.js）的 schema blocks 用例。
//
// 本插件的新形态是「逻辑页只提交 blocks，壳的 SchemaRenderer 负责画」：原来的内嵌页
// （app.js）测试锁定的是 DOM 拼接，native.js 的文件级分页与选中集合没有任何覆盖。
// 这里装载**真实的 /shell/native-client.js 与 native.js**，用假 window.parent 接收
// omnibox:schema / omnibox:native-error，用假 Bridge 提供 get_cached_scan / delete_files，
// 锁住最危险的一条不变量：
//
//   * 组内「全选组」与全局「全选」作用于**全部文件**，而不是当前显示的 12 张；
//     否则用户会以为删掉了整组，实际只删了前 12 张（数据风险）。
//
// 同时锁住提交 253f783 的文件级分页契约：每组首屏 12 张、每次展开 24 张、
// 组头「显示 x/y」、模式切换 / 重读 / 删除后重置、组级「显示更多」保留。
//
// 用法：node tests/js/image_cleaner_native.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const PROJECT = join(here, '..', '..');
const NATIVE_CLIENT = join(PROJECT, 'shell', 'frontend', 'public', 'shell', 'native-client.js');
const NATIVE_JS = join(PROJECT, 'plugins', 'image-cleaner', 'frontend', 'js', 'native.js');

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

/** 每组 N 个文件的假缓存；`byMode` 可以给两个模式不同分组。 */
function cacheGroups(byMode) {
  return (mode) => (typeof byMode === 'function' ? byMode(mode) : byMode);
}

function makeGroup(prefix, count) {
  const files = [];
  for (let i = 0; i < count; i++) files.push(`${prefix}/${i}.jpg`);
  return { files };
}

/**
 * 造一份干净环境：真实 native-client.js + native.js、假壳消息通道、假 Bridge。
 *
 * `groups` 是 get_cached_scan 的返回值（对象或 (mode) => 对象），
 * `deleted` 决定 delete_files 回什么（默认全部删除）。
 */
function setup({ groups = [], scanned = 0, stale = false, deleted = null } = {}) {
  const messages = [];
  const listeners = {};
  const bridgeCalls = [];
  const parent = { postMessage: (payload) => messages.push(payload) };
  const fakeWindow = {
    parent,
    location: { origin: 'http://127.0.0.1:18080' },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const sandbox = {
    window: fakeWindow,
    document: { createElement: (tag) => ({ tagName: tag }) },
    Bridge: {
      async call(method, ...args) {
        bridgeCalls.push({ method, args });
        if (method === 'get_cached_scan') {
          const value = typeof groups === 'function' ? groups(args[0]) : groups;
          return { cached: true, scanned, stale, groups: value || [] };
        }
        if (method === 'delete_files') {
          const files = args[0] || [];
          return deleted ? deleted(files) : { deleted: files, errors: [] };
        }
        return {};
      },
      thumbUrl: (rel) => '/thumbs/' + encodeURIComponent(rel),
    },
    Freshness: {
      mount: () => ({
        state: () => ({ busy: false, task: null }),
        verify: async () => { },
        cancel: async () => { },
        autoSync: () => { },
      }),
    },
    setInterval: () => 0,
    clearInterval: () => { },
    setTimeout,
    clearTimeout,
    console,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(readFileSync(NATIVE_CLIENT, 'utf8'), ctx, { filename: 'shell/native-client.js' });
  vm.runInContext(readFileSync(NATIVE_JS, 'utf8'), ctx, { filename: 'image-cleaner/native.js' });

  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
  const send = async (data) => {
    for (const fn of listeners.message || []) {
      await fn({ source: parent, origin: fakeWindow.location.origin, data });
    }
    await flush();
  };
  const schema = () => messages.filter(m => m.type === 'omnibox:schema').at(-1);
  const blocks = () => (schema() && schema().blocks) || [];
  const toolbar = () => (schema() && schema().toolbar) || [];
  const findBlock = (id) => blocks().find(b => b.id === id) || null;
  const groupsBlock = () => findBlock('groups');
  const textOf = (id) => {
    const block = findBlock(id);
    return block ? block.text : '';
  };
  const selectedCount = () => {
    const match = /已选 (\d+) 张/.exec(textOf('selected-count'));
    return match ? Number(match[1]) : -1;
  };
  const init = () => send({ type: 'omnibox:native-init' });
  const action = (name, payload) => send({ type: 'omnibox:native-action', action: name, payload });
  const confirmDelete = async () => {
    await action('delete-selected');
    await action('delete-confirm');
  };
  const deleteCall = () => bridgeCalls.filter(c => c.method === 'delete_files').at(-1);

  return {
    ctx, parent, messages, listeners, bridgeCalls,
    init, action, flush, schema, blocks, toolbar, findBlock, groupsBlock,
    textOf, selectedCount, confirmDelete, deleteCall,
  };
}

// ============================================================
// 1. 文件级分页：首屏 12、每次展开 24、组头计数、组级「显示更多」保留
// ============================================================

await check('每组首屏只提交 12 张，组头显示已显示/总数，并给出组内「显示更多」', async () => {
  const env = setup({ groups: [makeGroup('a', 30)], scanned: 60 });
  await env.init();

  const group = env.groupsBlock().groups[0];
  assert.equal(group.items.length, 12, '首屏只能提交 12 张缩略图');
  assert.match(group.title, /重复组 · 30 张/);
  assert.match(group.title, /显示 12\/30/, '组头要说明当前显示窗口，避免误以为只有 12 张');
  const more = group.actions.find(a => a.id === 'show-group-more');
  assert.ok(more, '还有 18 张时组内要有「显示更多」');
  assert.equal(more.payload.index, 0);
  assert.match(more.label, /还有 18 张/);
  // 组内全选与只留一张始终在，不受分页影响
  assert.ok(group.actions.some(a => a.id === 'select-group'));
  assert.ok(group.actions.some(a => a.id === 'keep-one-group'));
});

await check('每次展开追加 24 张，展开完不再给「显示更多」', async () => {
  const env = setup({ groups: [makeGroup('a', 100)] });
  await env.init();

  await env.action('show-group-more', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 36, '12 + 24 = 36');
  assert.match(env.groupsBlock().groups[0].title, /显示 36\/100/);

  await env.action('show-group-more', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 60, '再展开 24 张');

  await env.action('show-group-more', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 84);

  await env.action('show-group-more', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 100, '封顶是组内文件总数');
  assert.doesNotMatch(env.groupsBlock().groups[0].title, /显示 /);
  assert.ok(!env.groupsBlock().groups[0].actions.some(a => a.id === 'show-group-more'),
    '全部展开后不该再有「显示更多」');
});

await check('组级「显示更多」保留：20 组首屏，其余按 20 组追加', async () => {
  const groups = [];
  for (let i = 0; i < 21; i++) groups.push(makeGroup('g' + i, 2));
  const env = setup({ groups });

  await env.init();
  assert.equal(env.groupsBlock().groups.length, 20, '组级分页每页 20 组');
  const more = env.findBlock('more-actions');
  assert.ok(more, '还有 1 组时要给组级「显示更多」');
  assert.match(more.actions[0].label, /还有 1 组/);

  await env.action('show-more');
  assert.equal(env.groupsBlock().groups.length, 21, '组级展开后 21 组都在');
  assert.equal(env.findBlock('more-actions'), null);
});

// ============================================================
// 2. 选中集合必须覆盖全部文件（不是当前显示的 12 张）
// ============================================================

await check('组内全选作用于组内全部文件：删除请求包含全部 30 张而不是 12 张', async () => {
  const group = makeGroup('a', 30);
  const env = setup({ groups: [group] });
  await env.init();
  assert.equal(env.groupsBlock().groups[0].items.length, 12);

  await env.action('select-group', { index: 0 });
  assert.equal(env.selectedCount(), 30, '「全选组」要选中 30 张，而不是可见的 12 张');

  await env.confirmDelete();
  const call = env.deleteCall();
  assert.ok(call, '删除要真的发到 delete_files');
  const paths = call.args[0];
  assert.equal(paths.length, 30, `删除请求必须是 30 张，实际 ${paths.length} 张`);
  assert.equal(new Set(paths).size, 30);
  assert.equal(paths[29], group.files[29], '第 30 张（未显示）也必须在删除请求里');
});

await check('组内「只留一张」也作用于全部文件：保留第一张、其余 29 张参删', async () => {
  const group = makeGroup('a', 30);
  const env = setup({ groups: [group] });
  await env.init();

  await env.action('keep-one-group', { index: 0 });
  assert.equal(env.selectedCount(), 29);
  await env.confirmDelete();
  const paths = env.deleteCall().args[0];
  assert.equal(paths.length, 29);
  assert.ok(!paths.includes(group.files[0]), '第一张是保留项，不能被删');
  assert.ok(paths.includes(group.files[29]), '未显示的第 30 张也要被选中');
});

await check('全局「全选」覆盖分页外的组：组级只显示 20 组也选中全部 25 组', async () => {
  const groups = [];
  for (let i = 0; i < 25; i++) groups.push(makeGroup('g' + i, 3));
  const env = setup({ groups });
  await env.init();
  assert.equal(env.groupsBlock().groups.length, 20);

  await env.action('select-all');
  assert.equal(env.selectedCount(), 75, '25 组 × 3 张，不能只选可见的 20 组');

  await env.confirmDelete();
  const paths = env.deleteCall().args[0];
  assert.equal(paths.length, 75);
  assert.ok(paths.includes('g24/2.jpg'), '第 25 组（未显示）也要被选中');
});

await check('单个复选框 toggle 与勾选计数一致', async () => {
  const env = setup({ groups: [makeGroup('a', 5)] });
  await env.init();
  await env.action('toggle', { value: 'a/3.jpg', checked: true });
  assert.equal(env.selectedCount(), 1);
  const item = env.groupsBlock().groups[0].items.find(i => i.value === 'a/3.jpg');
  assert.equal(item.checked, true, '重渲染后该行要保持勾选');
  await env.action('toggle', { value: 'a/3.jpg', checked: false });
  assert.equal(env.selectedCount(), 0);
});

// ============================================================
// 3. 模式切换 / 重读 / 删除后的重置
// ============================================================

await check('切换模式后文件窗口与选中集合都重置', async () => {
  const env = setup({
    groups: cacheGroups(() => [makeGroup('a', 100)]),
  });
  await env.init();
  await env.action('show-group-more', { index: 0 });
  await env.action('select-group', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 36);
  assert.equal(env.selectedCount(), 100);

  await env.action('switch-mode', { mode: 'similar' });
  assert.equal(env.bridgeCalls.filter(c => c.method === 'get_cached_scan').at(-1).args[0], 'similar');
  assert.equal(env.groupsBlock().groups[0].items.length, 12, '换模式后重新从 12 张开始');
  assert.equal(env.selectedCount(), 0, '换模式要清空选中');
});

await check('重读（reload）后文件窗口与选中集合都重置', async () => {
  const env = setup({ groups: [makeGroup('a', 100)] });
  await env.init();
  await env.action('show-group-more', { index: 0 });
  await env.action('select-group', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 36);

  await env.action('reload');
  assert.equal(env.groupsBlock().groups[0].items.length, 12);
  assert.equal(env.selectedCount(), 0);
});

await check('删除成功后文件窗口重置，并清空选中', async () => {
  const big = makeGroup('a', 100);
  const small = makeGroup('b', 4);
  const env = setup({
    groups: [big, small],
    deleted: (files) => ({ deleted: files, errors: [] }),
  });
  await env.init();
  await env.action('show-group-more', { index: 0 });
  assert.equal(env.groupsBlock().groups[0].items.length, 36);

  await env.action('select-group', { index: 1 });
  await env.confirmDelete();
  assert.equal(env.deleteCall().args[0].length, 4);
  assert.equal(env.groupsBlock().groups[0].items.length, 12, '删除后大组的文件窗口也要回到 12');
  assert.equal(env.selectedCount(), 0, '删除成功后选中集合清空');
  assert.equal(env.groupsBlock().groups.length, 1, '不足两张的小组被丢弃');
});

// ============================================================
// 4. 空态 / 错误态（blocks 契约的最基本形态）
// ============================================================

await check('读取失败时提交 empty 错误块与重试动作，而不是空列表', async () => {
  const messages = [];
  const listeners = {};
  const parent = { postMessage: (payload) => messages.push(payload) };
  const fakeWindow = {
    parent,
    location: { origin: 'http://127.0.0.1:18080' },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const sandbox = {
    window: fakeWindow,
    document: { createElement: (tag) => ({ tagName: tag }) },
    Bridge: {
      async call() { throw new Error('宿主不可达'); },
      thumbUrl: (rel) => '/thumbs/' + rel,
    },
    Freshness: {
      mount: () => ({ state: () => ({ busy: false }), verify: async () => { }, cancel: async () => { }, autoSync: () => { } }),
    },
    setInterval: () => 0,
    clearInterval: () => { },
    setTimeout, clearTimeout, console,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(readFileSync(NATIVE_CLIENT, 'utf8'), ctx);
  vm.runInContext(readFileSync(NATIVE_JS, 'utf8'), ctx);
  for (const fn of listeners.message || []) {
    await fn({ source: parent, origin: fakeWindow.location.origin, data: { type: 'omnibox:native-init' } });
  }
  await new Promise((resolve) => setTimeout(resolve, 0));
  const schema = messages.filter(m => m.type === 'omnibox:schema').at(-1);
  const empty = schema.blocks.find(b => b.id === 'empty-error');
  assert.ok(empty, `错误态要提交 empty 块，实际: ${JSON.stringify(schema.blocks.map(b => b.id))}`);
  assert.equal(empty.type, 'empty');
  assert.match(empty.text, /读取结果失败/);
  assert.equal(empty.submit.id, 'reload');
});

console.log(failed ? `\n${failed} 例失败` : `\nimage-cleaner 原生面板 ${total} 例通过`);
process.exit(failed ? 1 : 0);
