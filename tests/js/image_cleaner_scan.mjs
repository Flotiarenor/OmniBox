// image-cleaner 结果加载与统一刷新挂载的纯逻辑用例（真实 app.js + 共享 DOM 替身 + 假 Bridge）。
//
// 本插件的扫描链路已并入壳的统一刷新基建（`system_freshness_*` + 共享组件
// `shell/frontend/public/shell/freshness.js`）：前端不再自己起后台任务、轮询进度、
// 画取消按钮，那些都由组件负责；页内只做「读结果缓存 + 按 stale 提示」。
// 本用例锁这条新契约：
//   1) 缓存命中：直接渲染分组，且**不**碰 scan_start / scan_status；
//   2) 缓存未命中：显示"尚未校验"，空态文案指向「校验」，不自己起任务；
//   3) `stale`：提示"磁盘有变化，建议重新校验"（旧实现没有这个信号）；
//   4) 挂载：`Freshness.mount` 收到正确的 plugin / unit，onChange 能触发重读；
//   5) 切换模式：按 mode 重读缓存（相似模式读 similar）；
//   6) 删除后主动触发被动同步（让后端立刻看到这次删除）。
//
// 用法：node tests/js/image_cleaner_scan.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createDom } from './dom_stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const APP_JS = join(here, '..', '..', 'plugins', 'image-cleaner', 'frontend', 'js', 'app.js');

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

/**
 * 造一份干净环境：真实 app.js + DOM 替身 + 脚本化的 Bridge 与共享组件。
 *
 * `cache` 按 mode 给 `get_cached_scan` 的返回值（也可以是函数）。
 * `mounts` 记录 `Freshness.mount` 收到的参数，`syncs` 记录 `autoSync` 的调用。
 */
function setup({ cache = {}, mountFreshness = true } = {}) {
  const { document, makeEl } = createDom();
  for (const id of ['cleaner-results', 'cleaner-scanned', 'cleaner-selected',
                    'cleaner-freshness', 'tab-dupe', 'tab-similar']) {
    document.body.appendChild(Object.assign(makeEl('div'), { id }));
  }

  const calls = [];
  const mounts = [];
  const syncs = [];
  const control = {
    autoSync: (scope) => syncs.push(scope),
    verify: () => calls.push('verify'),
  };

  const bridge = {
    // render() 会拼缩略图 URL：少了这两个，模板字符串求值抛错 → 结果区变成错误态，
    // 而断言会假绿。
    thumbUrl: (rel) => `/thumbs/${rel}`,
    originalUrl: (rel) => `/file?path=${rel}`,
    async call(method, arg) {
      calls.push(method);
      if (method === 'get_cached_scan') {
        const entry = typeof cache === 'function' ? cache(arg) : cache[arg];
        return entry || { cached: false, stale: false, groups: [], scanned: 0 };
      }
      if (method === 'get_status') return { root_dir: 'D:/album', roots: ['D:/album'] };
      if (method === 'delete_files') return { deleted: ['a.jpg'], errors: [] };
      return null;
    },
  };

  globalThis.document = document;
  globalThis.window = {};
  globalThis.Bridge = bridge;
  globalThis.Toast = { warning() { }, error() { }, success() { }, info() { } };
  if (mountFreshness) {
    globalThis.Freshness = {
      mount(opts) {
        mounts.push(opts);
        return control;
      },
    };
  } else {
    delete globalThis.Freshness;
  }
  const ImageCleaner = new Function(`${readFileSync(APP_JS, 'utf8')}\nreturn ImageCleaner;`)();
  return { cleaner: new ImageCleaner(), document, calls, mounts, syncs, control };
}

// ---------- 1. 读缓存并渲染 ----------

await check('缓存命中时直接渲染分组，不碰任何扫描任务接口', async () => {
  const env = setup({
    cache: { dupe: { cached: true, scanned: 7, stale: false, groups: [{ files: ['a.jpg', 'b.jpg'] }] } },
  });
  await env.cleaner._loadResults();

  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '已扫描 7 张');
  assert.equal(env.cleaner.groups.length, 1, '缓存里的分组要渲染出来');
  const html = env.document.getElementById('cleaner-results').innerHTML;
  assert.match(html, /cleaner-group/, `分组要真的画出来，实际: ${html.slice(0, 120)}`);
  assert.ok(!/读取结果失败/.test(html), '渲染过程不能报错');
  assert.ok(!env.calls.includes('scan_start'), `不该再自己起任务：${env.calls.join(',')}`);
  assert.ok(!env.calls.includes('scan_status'), `不该再轮询任务：${env.calls.join(',')}`);
});

await check('缓存未命中时提示"尚未校验"，空态指向「校验」', async () => {
  const env = setup({ cache: {} });
  await env.cleaner._loadResults();

  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '尚未校验');
  const html = env.document.getElementById('cleaner-results').innerHTML;
  assert.match(html, /校验/, `空态要指向「校验」，实际: ${html.slice(0, 200)}`);
  assert.ok(!env.calls.includes('scan_start'), '未命中也不该自动起任务');
});

await check('stale 时提示磁盘有变化、建议重新校验', async () => {
  const env = setup({
    cache: { dupe: { cached: true, scanned: 5, stale: true, groups: [] } },
  });
  await env.cleaner._loadResults();

  const text = env.document.getElementById('cleaner-scanned').textContent;
  assert.match(text, /已扫描 5 张/);
  assert.match(text, /建议重新校验/, `要提示结果可能过期，实际: ${text}`);
});

// ---------- 2. 统一刷新挂载 ----------

await check('挂载共享组件：plugin/unit 正确，onChange 触发重读', async () => {
  const env = setup({ cache: { dupe: { cached: true, scanned: 3, groups: [] } } });
  env.cleaner._mountFreshness();

  assert.equal(env.mounts.length, 1, '要挂一次组件');
  assert.equal(env.mounts[0].plugin, 'image-cleaner');
  assert.equal(env.mounts[0].unit, '张');
  assert.equal(typeof env.mounts[0].onChange, 'function');

  env.document.getElementById('cleaner-scanned').textContent = '';
  await env.mounts[0].onChange();
  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '已扫描 3 张',
               'onChange 要重新读缓存并渲染');
});

await check('组件缺失时不抛错（页面脱离壳单独打开）', async () => {
  const env = setup({ mountFreshness: false });
  env.cleaner._mountFreshness();
  assert.equal(env.cleaner.freshness, undefined);
});

// ---------- 3. 模式切换 ----------

await check('切换到相似模式时读 similar 的结果', async () => {
  const env = setup({
    cache: (mode) => ({
      cached: true,
      scanned: mode === 'similar' ? 42 : 7,
      groups: [],
    }),
  });
  await env.cleaner._loadResults();
  await env.cleaner.switchMode('similar');

  assert.equal(env.cleaner.mode, 'similar');
  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '已扫描 42 张');
});

// ---------- 4. 删除后触发被动同步 ----------

await check('删除选中后触发被动同步，让后端立刻看到删除', async () => {
  const env = setup({ cache: {} });
  env.cleaner._mountFreshness();
  env.cleaner.groups = [{ files: ['a.jpg', 'b.jpg'] }];
  env.cleaner.selected = new Set(['a.jpg']);
  globalThis.confirmDialog = async () => true;

  await env.cleaner.deleteSelected();

  assert.ok(env.calls.includes('delete_files'), `要真的发删除：${env.calls.join(',')}`);
  assert.deepEqual(env.syncs, [''], `删除后要触发一次被动同步：${JSON.stringify(env.syncs)}`);
});

console.log(failed ? `\n${failed} 例失败` : `\nimage-cleaner 结果加载 ${total} 例通过`);
process.exit(failed ? 1 : 0);
