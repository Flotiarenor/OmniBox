// image-cleaner 扫描流程的纯逻辑用例（真实 app.js + 共享 DOM 替身 + 假 Bridge）。
//
// 扫描现在跑在后端的后台任务里（壳的共享基建 `shell/backend/tasks.py` 的 BackgroundTask，
// 与 image-viewer 的缩略图重建同一套骨架）：前端不再直呼同步的
// `duplicate_scan` / `similar_scan`，而是 `scan_start` → 轮询 `scan_status`（进度 + 取消）
// → 任务结束后用 `get_cached_scan` 取结果。本用例锁这条链路：
//   1) 缓存命中：直接渲染，不再起任务（退出重进不该重扫）；
//   2) 缓存未命中：起任务 → 轮询 → 取缓存 → 渲染，且轮询期间的进度文本带 processed/total；
//   3) 取消：任务收尾为 cancelled 时显示"已取消扫描"，不渲染任何分组；
//   4) 「取消扫描」按钮真的会发 `scan_cancel`。
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
 * 造一份干净环境：真实 app.js + DOM 替身 + 脚本化的 Bridge。
 *
 * `statuses` 是按序返回的 `scan_status` 结果（最后一条会重复返回）；`snapshots` 记录每次
 * 轮询时进度文本的当时值（第 2 次轮询拿到的就是第 1 次进度渲染的结果）。
 */
function setup({ cache, statuses = [], after = null, clickCancelOnStatus = 0 } = {}) {
  const { document, makeEl } = createDom();
  for (const id of ['cleaner-results', 'cleaner-scanned', 'cleaner-selected']) {
    document.body.appendChild(Object.assign(makeEl('div'), { id }));
  }

  const calls = [];
  const snapshots = [];
  const queue = statuses.slice();
  let started = false;

  function progressText() {
    const el = document.getElementById('cleaner-scan-text');
    return el ? el.textContent : null;
  }

  const bridge = {
    // render() 会拼缩略图 URL：少了这两个，模板字符串求值抛错 → 结果区变成错误态，
    // 而"已扫描 N 张"和 groups 已经赋值，断言会假绿。
    thumbUrl: (rel) => `/thumbs/${rel}`,
    originalUrl: (rel) => `/file?path=${rel}`,
    async call(method) {
      calls.push(method);
      if (method === 'get_cached_scan') {
        return started && after ? after : (cache || { cached: false });
      }
      if (method === 'scan_start') {
        started = true;
        return { started: true, running: true };
      }
      if (method === 'scan_status') {
        snapshots.push(progressText());
        const status = queue.length > 1
          ? queue.shift()
          : (queue[0] || { running: false, success: true });
        if (snapshots.length === clickCancelOnStatus) {
          const cancel = document.getElementById('cleaner-cancel');
          if (cancel) cancel.click();
        }
        return status;
      }
      if (method === 'scan_cancel') return { success: true };
      return null;
    },
  };

  globalThis.document = document;
  globalThis.window = {};
  globalThis.Bridge = bridge;
  globalThis.Toast = { warning() { }, error() { }, success() { }, info() { } };
  const ImageCleaner = new Function(`${readFileSync(APP_JS, 'utf8')}\nreturn ImageCleaner;`)();
  return { cleaner: new ImageCleaner(), document, calls, snapshots };
}

// ---------- 1. 缓存命中：不起任务 ----------

await check('缓存命中时直接渲染，不再起后台任务', async () => {
  const env = setup({
    cache: { cached: true, scanned: 7, groups: [{ files: ['a.jpg', 'b.jpg'] }] },
  });
  await env.cleaner.runScan();
  assert.ok(!env.calls.includes('scan_start'), `缓存命中不该扫全库：${env.calls.join(',')}`);
  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '已扫描 7 张');
  assert.equal(env.cleaner.groups.length, 1, '缓存里的分组要渲染出来');
  const html = env.document.getElementById('cleaner-results').innerHTML;
  assert.match(html, /cleaner-group/, `分组要真的画出来，实际: ${html.slice(0, 120)}`);
  assert.ok(!/扫描失败/.test(html), '渲染过程不能报错');
});

// ---------- 2. 缓存未命中：scan_start → 轮询 → get_cached_scan ----------

await check('缓存未命中时起后台任务、轮询进度、结束后取缓存渲染', async () => {
  const env = setup({
    cache: { cached: false },
    statuses: [
      { running: true, success: false, mode: 'similar', processed: 5, total: 10, current: 'x/a.jpg' },
      { running: false, success: true, mode: 'similar', processed: 10, total: 10, current: '' },
    ],
    after: { cached: true, scanned: 10, groups: [{ files: ['a.jpg', 'b.jpg'] }] },
  });
  env.cleaner.mode = 'similar';
  await env.cleaner.runScan();

  assert.deepEqual(env.calls[0], 'get_cached_scan', '先读缓存');
  assert.deepEqual(env.calls[1], 'scan_start', '缓存没有才起任务');
  assert.ok(env.calls.includes('scan_status'), '任务期间要轮询进度');
  assert.equal(env.calls[env.calls.length - 1], 'get_cached_scan', '结束后从缓存取结果');
  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '已扫描 10 张');
  assert.equal(env.cleaner.groups.length, 1);
  assert.match(env.document.getElementById('cleaner-results').innerHTML, /cleaner-group/,
               '任务结束后要渲染分组，而不是错误态');
});

await check('轮询期间进度文本带 processed/total、阶段与当前文件', async () => {
  const env = setup({
    cache: { cached: false },
    statuses: [
      { running: true, success: false, mode: 'similar', processed: 5, total: 10, current: '作者A/1.jpg' },
      { running: false, success: true, mode: 'similar', processed: 10, total: 10 },
    ],
    after: { cached: true, scanned: 10, groups: [] },
  });
  env.cleaner.mode = 'similar';
  await env.cleaner.runScan();
  const during = env.snapshots[1] || '';
  assert.match(during, /5\/10/, `进度文本要带 processed/total，实际: ${during}`);
  assert.match(during, /相似图片/, `进度文本要写明阶段，实际: ${during}`);
  assert.match(during, /作者A\/1\.jpg/, `进度文本要带当前文件，实际: ${during}`);
});

// ---------- 3. 取消 ----------

await check('任务被取消时显示"已取消扫描"，不渲染分组', async () => {
  const env = setup({
    cache: { cached: false },
    statuses: [
      { running: true, success: false, mode: 'dupe', processed: 2, total: 100, current: '' },
      { running: false, done: true, success: false, cancelled: true },
    ],
  });
  await env.cleaner.runScan();
  const html = env.document.getElementById('cleaner-results').innerHTML;
  assert.match(html, /已取消扫描/, `取消后要明确告知，实际: ${html}`);
  assert.ok(!/正在扫描/.test(html), '取消后不该还留着"正在扫描"');
  assert.equal(env.cleaner.groups.length, 0, '取消不产生分组');
  assert.equal(env.document.getElementById('cleaner-scanned').textContent, '');
});

await check('「取消扫描」按钮发 scan_cancel', async () => {
  const env = setup({
    cache: { cached: false },
    statuses: [
      { running: true, success: false, mode: 'dupe', processed: 1, total: 100, current: '' },
      { running: false, done: true, success: false, cancelled: true },
    ],
    clickCancelOnStatus: 1,
  });
  await env.cleaner.runScan();
  assert.ok(env.calls.includes('scan_cancel'), `按钮要真的发取消请求：${env.calls.join(',')}`);
});

// ---------- 4. 任务失败：错误态 ----------

await check('任务失败时显示错误态，不把空结果当成"没有重复"', async () => {
  const env = setup({
    cache: { cached: false },
    statuses: [
      { running: true, success: false, mode: 'dupe', processed: 1, total: 10, current: '' },
      { running: false, done: true, success: false, cancelled: false },
    ],
  });
  await env.cleaner.runScan();
  const html = env.document.getElementById('cleaner-results').innerHTML;
  assert.match(html, /扫描失败/, `失败要有错误态，实际: ${html}`);
  assert.equal(env.cleaner.groups.length, 0);
});

console.log(failed ? `\n${failed} 例失败` : `\nimage-cleaner 扫描流程 ${total} 例通过`);
process.exit(failed ? 1 : 0);
