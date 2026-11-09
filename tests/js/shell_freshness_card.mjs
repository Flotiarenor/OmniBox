// Shell 共享组件「同步 / 校验」的进度卡片用例（真实 freshness.js + DOM 替身 + 假 Bridge）。
//
// 锁的是几个真实踩到过的坑：
//   1) 被动同步报 `partial` 时，组件只弹卡片**没有真起校验**（注释说会起），
//      于是卡片停在静态占位符「0 / 0」上永不消失 —— 用户看到的是"为什么弹出
//      一个正在校验（同步未覆盖全部目录）— 0 / 0"；
//   2) 预算型 partial（`reason: 'budget'`，还有目录要处理）才值得替用户起校验；
//      `dir_cap`（只是没走完）不该反复起，否则每次进视图都跑一趟全量；
//   3) 预算型 partial 里**已经发现的变化**必须先通知内容页（第 13 节），不能只在
//      续跑校验结束时才说 —— 那次校验可能还没结束，而目录集合变化（新建空相册）
//      只有 `new_dirs` / `vanished_dirs` 一个信号（第 11 节）；
//   4) 校验完成只看"上一份 state 是 busy"会丢掉快速任务的收尾：小库/空库在第一次
//      状态查询之前就跑完，没有完成文案也没有 onChange（第 12 节）；
//   5) `available:false` 有两种来源（未接入 vs 指纹库瞬时打不开），只有前者该永久
//      禁用控件（第 14 节）。
//
// 用法：node tests/js/shell_freshness_card.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createDom } from './dom_stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', '..', 'shell', 'frontend', 'public', 'shell', 'freshness.js');
const DEBOUNCE = 400;

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * 建一份"够本组件用"的 DOM：共享替身按约定不解析 innerHTML 结构，而本组件用
 * `group.querySelectorAll('[data-fr]')` 找自己的部件。这里用 Proxy 就地补上
 * （只解析 `data-fr` 节点、只支持这一种属性选择器），不改共享替身。
 */
function createFreshnessDom() {
  const env = createDom();
  const base = env.makeEl;

  const descendants = (el, out = []) => {
    for (const child of el.children || []) { out.push(child); descendants(child, out); }
    return out;
  };

  const attrQuery = (el, sel) => {
    const wanted = /^\[data-fr(?:="([^"]*)")?\]$/.exec(sel);
    if (!wanted) return null;
    return descendants(el).filter(
      (n) => n.dataset && n.dataset.fr && (!wanted[1] || n.dataset.fr === wanted[1]));
  };

  const parseInto = (el, html) => {
    el.__html = html;
    el.children = [];
    const re = /<([a-zA-Z][a-zA-Z0-9-]*)([^>]*?)data-fr="([^"]+)"([^>]*?)>/g;
    let m;
    while ((m = re.exec(html)) !== null) {
      const child = base(m[1].toLowerCase());
      const attrs = `${m[2] || ''} ${m[4] || ''}`;
      const cls = /\bclass="([^"]*)"/.exec(attrs);
      if (cls) child.className = cls[1];
      child.dataset.fr = m[3];
      const text = /^([^<]*)/.exec(html.slice(m.index + m[0].length));
      if (text && text[1].trim()) child.textContent = text[1].trim();
      el.appendChild(child);
    }
  };

  const wrap = (tag) => new Proxy(base(tag), {
    get(target, prop) {
      if (prop === 'querySelectorAll') {
        return (sel) => attrQuery(target, sel) || target.querySelectorAll(sel);
      }
      if (prop === 'querySelector') {
        return (sel) => {
          const hit = attrQuery(target, sel);
          return hit ? (hit[0] || null) : target.querySelector(sel);
        };
      }
      const value = target[prop];
      return typeof value === 'function' ? value.bind(target) : value;
    },
    set(target, prop, value) {
      if (prop === 'innerHTML') { parseInto(target, String(value)); return true; }
      target[prop] = value;
      return true;
    },
  });

  env.document.createElement = wrap;
  env.makeEl = wrap;
  return env;
}

/**
 * 挂一份组件：`state` 是 `system_freshness_state` 的返回，`syncReport` 是同步报告。
 *
 * `verifyState` 覆盖"校验启动后立刻读到的状态"：真实后端对小库（1 个目录 + 几十次
 * stat）可能在第一次状态查询之前就跑完，此时读到的是 `busy:false` + `task.state:'done'`，
 * 而不是 running。缺省仍模拟 running（旧的"先登记任务再返回"路径）。
 *
 * 每份新用例先销毁上一份：`call()` 读的是 `globalThis.Bridge`，而每个用例都会换一份
 * 假 Bridge；旧控件不销毁的话，它的低频状态重读会打到新用例的桥与调用记录上。
 */
let previousCtl = null;
function setup({ state = { available: true, busy: false, task: null, unit: '张' },
                 syncReport = null, verifyResult = { started: true, running: true },
                 verifyThrows = false, verifyState = undefined,
                 scope = '', onChange = null, idlePollMs = undefined } = {}) {
  if (previousCtl && typeof previousCtl.destroy === 'function') previousCtl.destroy();
  previousCtl = null;
  const { document, makeEl } = createFreshnessDom();
  const host = Object.assign(makeEl('div'), { id: 'host' });
  document.body.appendChild(host);

  const calls = [];
  const toasts = [];
  let cur = state;
  const bridge = {
    async callSystem(method, ...args) {
      calls.push({ method, args });
      if (method === 'system_freshness_state') return cur;
      if (method === 'system_freshness_sync') return syncReport;
      if (method === 'system_freshness_verify') {
        if (verifyThrows) throw new Error('网络错误');
        // 真实链路里 `start_verify` 会先把任务登记进引擎再返回，因此紧接着的状态
        // 查询就能看到 running（这里照样模拟，否则卡片会被"没有任务"立刻收掉）
        if (verifyState !== undefined) {
          cur = verifyState;
        } else if (verifyResult && verifyResult.started !== false) {
          cur = { available: true, busy: true,
                  task: { state: 'running', total: 0, processed: 0, extra: {} } };
        }
        return verifyResult;
      }
      if (method === 'system_freshness_cancel') return { success: true };
      return null;
    },
  };

  const toastStub = {
    info(text) { toasts.push({ kind: 'info', text }); },
    error(text) { toasts.push({ kind: 'error', text }); },
    success(text) { toasts.push({ kind: 'success', text }); },
    warning(text) { toasts.push({ kind: 'warning', text }); },
    show(text) { toasts.push({ kind: 'show', text }); },
  };

  globalThis.document = document;
  // 组件先看 `window.Toast`（真实浏览器里 window 就是全局对象，这里必须显式挂上）
  globalThis.window = { Bridge: bridge, Toast: toastStub };
  globalThis.Bridge = bridge;
  globalThis.Toast = toastStub;

  const Freshness = new Function(`${readFileSync(SRC, 'utf8')}\nreturn window.Freshness;`)();
  const ctl = Freshness.mount({
    plugin: 'demo', container: host, unit: '张', scope, onChange,
    ...(idlePollMs === undefined ? {} : { idlePollMs }),
  });
  previousCtl = ctl;
  const card = host.querySelector('[data-fr="card"]');
  const count = host.querySelector('[data-fr="count"]');
  const title = host.querySelector('[data-fr="card-title"]');
  return {
    ctl, calls, host, card, count, title, Freshness, toasts,
    status: host.querySelector('[data-fr="status"]'),
    verifyBtn: host.querySelector('[data-fr="verify"]'),
    setState(s) { cur = s; },
    verifyCalls: () => calls.filter((c) => c.method === 'system_freshness_verify'),
    syncCalls: () => calls.filter((c) => c.method === 'system_freshness_sync'),
    stateCalls: () => calls.filter((c) => c.method === 'system_freshness_state'),
    /** 模拟 keepAlive 插件页被 v-show 隐藏（控件仍在 DOM 里但没有布局盒）。 */
    hide() { ctl.el.offsetParent = null; },
    show() { ctl.el.offsetParent = {}; },
  };
}

// ---------- 1. 预算型 partial 必须真的起校验 ----------

await check('同步报 budget 型 partial 时，组件替用户起一次校验', async () => {
  const env = setup({ syncReport: { action: 'partial', reason: 'budget', dirs: 400 } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);

  assert.equal(env.verifyCalls().length, 1,
               `要真的起校验，实际调用: ${env.calls.map((c) => c.method).join(',')}`);
  assert.ok(!env.card.classList.contains('hidden'), '卡片要显示进度');
  assert.match(env.title.textContent, /未覆盖全部目录/);
});

await check('dir_cap 型 partial 不起校验（否则每次进视图都跑全量）', async () => {
  const env = setup({ syncReport: { action: 'partial', reason: 'dir_cap', dirs: 3200 } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);

  assert.equal(env.verifyCalls().length, 0, '只是没走完，不该起任务');
  assert.ok(env.card.classList.contains('hidden'), '也不该弹卡片打扰用户');
});

await check('校验起不来时卡片要收掉，不留「0 / 0」', async () => {
  const env = setup({ syncReport: { action: 'partial', reason: 'budget' },
                      verifyResult: { started: false, running: false, error: '忙' } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 200);

  assert.ok(env.card.classList.contains('hidden'), '起不来就必须收掉卡片');
  assert.equal(env.count.textContent, '0 / 0', '占位符要复位（卡片本身已隐藏）');
});

await check('已有任务在跑时不再叠一个校验', async () => {
  const env = setup({ state: { available: true, busy: true, task: { state: 'running', total: 10, processed: 3 } },
                      syncReport: { action: 'partial', reason: 'budget' } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);

  assert.equal(env.verifyCalls().length, 0, '单飞由组件这一层也挡一道');
});

// ---------- 2. 完成态 ----------

await check('任务结束：卡片显示完成并在停留后自动隐藏', async () => {
  const running = { available: true, busy: true,
                    task: { state: 'running', total: 10, processed: 4, extra: { added: 2 } } };
  const env = setup({ state: running });
  env.ctl.verify();
  await sleep(50);
  assert.ok(!env.card.classList.contains('hidden'), '校验中要显示卡片');
  assert.match(env.count.textContent, /4 \/ 10/, `进度要可读，实际: ${env.count.textContent}`);

  env.setState({ available: true, busy: false,
                 task: { state: 'done', done: true, success: true, extra: { added: 2 } } });
  await sleep(600 + 50);                       // 等一次轮询
  assert.equal(env.title.textContent, '校验完成');

  await sleep(4200);                           // CARD_HIDE_MS 之后
  assert.ok(env.card.classList.contains('hidden'), '完成卡片到点要自己收掉');
});

// ---------- 3. 同步通道：Freshness.sync / onChange 报告 ----------

await check('Freshness.sync 直接打到 system_freshness_sync 并返回报告', async () => {
  const env = setup({ syncReport: { action: 'sync', added: 3, changed: 0, removed: 0 } });
  const report = await env.Freshness.sync('demo', 'a/b');
  assert.deepEqual(report, { action: 'sync', added: 3, changed: 0, removed: 0 });
  assert.equal(env.syncCalls().length, 1, `应恰好同步一次：${env.calls.map((c) => c.method)}`);
  assert.deepEqual(env.syncCalls()[0].args, ['demo', 'a/b'], '参数必须是 (plugin, scope)');
});

await check('mount 返回的 sync 与 autoSync 同一通道，可直接拿报告', async () => {
  const env = setup({ syncReport: { action: 'sync', added: 1 }, scope: 'old/dir' });
  // 省略 scope 时用挂载时的作用域
  const first = await env.ctl.sync();
  assert.deepEqual(env.syncCalls()[0].args, ['demo', 'old/dir']);
  assert.equal(first.added, 1);
  // 传 scope 时更新作用域
  await env.ctl.sync('new/dir');
  assert.deepEqual(env.syncCalls()[1].args, ['demo', 'new/dir']);
});

await check('同步报告有变化时 onChange 收到报告本身（不是无参）', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', added: 2, changed: 1, removed: 0 },
    onChange: (report) => reports.push(report),
  });
  env.ctl.autoSync('a/b');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, `应通知一次，实际 ${reports.length}`);
  assert.equal(reports[0].added, 2, '回调参数必须是后端报告');
  assert.equal(reports[0].changed, 1);
});

await check('报告里的 invalidated（远端丢弃缓存）也算变化', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', invalidated: true },
    onChange: (report) => reports.push(report),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, 'invalidated 必须通知内容页重载');
  assert.equal(reports[0].invalidated, true);
});

await check('skip（去抖 / 被占用）不触发 onChange', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'skip', reason: 'throttled', retry_after: 3.2 },
    onChange: (report) => reports.push(report),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 0, 'skip 不是变化，不该让内容页重载');
});

await check('同步没有变化（全 0 且无 invalidated）不触发 onChange', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', added: 0, changed: 0, removed: 0 },
    onChange: (report) => reports.push(report),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 0, '没有变化就不该打扰内容页');
});

// ---------- 4. 被跳过的同步要在状态行说出来 ----------

await check('sync 被去抖挡住：状态行给可重试文案', async () => {
  const env = setup({ syncReport: { action: 'skip', reason: 'throttled', retry_after: 3.2 } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /节流/, `状态行没解释为什么没更新：${env.status.textContent}`);
  assert.match(env.status.textContent, /稍后重试/, '要给出可重试的说法');
});

await check('sync 因后台校验被跳过：状态行说明在等校验', async () => {
  const env = setup({ syncReport: { action: 'skip', reason: 'busy' } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /后台校验/, env.status.textContent);
});

await check('非法作用域被跳过：状态行说明作用域无效', async () => {
  const env = setup({ syncReport: { action: 'skip', reason: 'bad_scope', scope: '../etc' } });
  env.ctl.autoSync('../etc');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /作用域无效/, env.status.textContent);
});

await check('点「校验」会清掉跳过文案（它只解释那一次同步）', async () => {
  const env = setup({ syncReport: { action: 'skip', reason: 'throttled' } });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /稍后重试/);
  await env.ctl.verify();
  await sleep(80);
  assert.doesNotMatch(env.status.textContent, /稍后重试/, '校验已经起跑，旧文案不该留着');
});

// ---------- 5. 停留期低频重读状态 ----------

await check('没有任务时按低频重读状态（不需用户操作）', async () => {
  const env = setup({ idlePollMs: 120 });
  await sleep(60);
  const first = env.stateCalls().length;
  assert.ok(first >= 1, '挂载时至少要读一次状态');
  await sleep(250);
  assert.ok(env.stateCalls().length > first,
    `停留期间没有重读状态（仍为 ${env.stateCalls().length} 次）`);
});

await check('重读到的「有变化 · 需要校验」会自己出现在状态行', async () => {
  const env = setup({ idlePollMs: 120 });
  await sleep(60);
  env.setState({ available: true, busy: false, task: null, entries: 4, version_stale: true });
  await sleep(250);
  assert.equal(env.status.textContent, '有变化 · 需要校验', env.status.textContent);
});

await check('idlePollMs: 0 关闭低频重读', async () => {
  const env = setup({ idlePollMs: 0 });
  await sleep(60);
  const first = env.stateCalls().length;
  await sleep(250);
  assert.equal(env.stateCalls().length, first, '显式关闭后不该再读状态');
});

// ---------- 6. 同步进行中的校验（引擎单飞的 busy） ----------
//
// 引擎在同步进行时对 `system_freshness_verify` 回 `{started:false, running:false,
// reason:'busy', sync_active:true}`：没有任务可轮询。旧实现只看 `!res.running`，
// 于是分支走到"启动失败"，而卡片/轮询这两件事在这一支上都是错的。

const SYNC_BUSY = { started: false, running: false, reason: 'busy', sync_active: true };

await check('同步进行中的校验：不弹卡片、不轮询、不通知内容页', async () => {
  const reports = [];
  const env = setup({ verifyResult: SYNC_BUSY, onChange: (r) => reports.push(r), idlePollMs: 0 });
  await sleep(40);
  const before = env.stateCalls().length;
  await env.ctl.verify();
  await sleep(700);

  assert.ok(env.card.classList.contains('hidden'), '没有任务可轮询，卡片必须收着');
  assert.equal(env.stateCalls().length, before, '不能进入校验轮询');
  assert.equal(reports.length, 0, '没有确认任何变化，不该通知内容页重载');
  assert.match(env.toasts.map((t) => t.text).join('|'), /同步进行中，完成后再试/,
               `没有给出可读的提示：${JSON.stringify(env.toasts)}`);
});

await check('同步进行中的校验保留低频状态重读', async () => {
  const env = setup({ verifyResult: SYNC_BUSY, idlePollMs: 100 });
  await sleep(60);
  await env.ctl.verify();
  const after = env.stateCalls().length;
  await sleep(280);
  assert.ok(env.stateCalls().length > after, 'busy 早退不能把 idle 轮询关掉');
});

// ---------- 7. 不可达根（盘没挂上） ----------

await check('部分目录不可达：说原因，不显示"有变化 · 需要校验"', async () => {
  const env = setup({ state: {
    available: true, busy: false, task: null, unit: '张',
    entries: 4, version_stale: true, unreachable_roots: 2,
  } });
  await sleep(40);
  assert.match(env.status.textContent, /部分目录不可达（2）/);
  assert.match(env.status.textContent, /挂载后自动同步/);
  assert.doesNotMatch(env.status.textContent, /有变化/);
});

await check('不可达根也接受数组形状（条数与列表的一并兜住）', async () => {
  const env = setup({ state: {
    available: true, busy: false, task: null, unit: '张',
    entries: 4, version_stale: false, unreachable_roots: ['/mnt/a', '/mnt/b'],
  } });
  await sleep(40);
  assert.match(env.status.textContent, /部分目录不可达（2）/);
});

await check('不可达根为 0：回到常规文案', async () => {
  const env = setup({ state: {
    available: true, busy: false, task: null, unit: '张',
    entries: 4, version_stale: true, unreachable_roots: 0,
  } });
  await sleep(40);
  assert.equal(env.status.textContent, '有变化 · 需要校验');
});

// ---------- 8. 跳过提示的优先级 ----------

await check('节流提示不顶掉"有变化 · 需要校验"', async () => {
  const env = setup({
    state: { available: true, busy: false, task: null, unit: '张', entries: 2, version_stale: true },
    syncReport: { action: 'skip', reason: 'throttled', retry_after: 3.2 },
  });
  await sleep(40);
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /有变化 · 需要校验/, env.status.textContent);
  assert.match(env.status.textContent, /同步被节流/, env.status.textContent);
});

await check('节流提示也不顶掉"部分目录不可达"', async () => {
  const env = setup({
    state: { available: true, busy: false, task: null, unit: '张', unreachable_roots: 1 },
    syncReport: { action: 'skip', reason: 'throttled' },
  });
  await sleep(40);
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /部分目录不可达（1）/, env.status.textContent);
  assert.match(env.status.textContent, /同步被节流/, env.status.textContent);
});

await check('远端新鲜期内的 skip 有自己的说明（不是笼统的"已跳过"）', async () => {
  const env = setup({
    state: { available: true, busy: false, task: null, unit: '首', mode: 'remote', ttl_seconds: 600 },
    syncReport: { action: 'skip', reason: 'fresh', ttl_remaining: 420 },
  });
  await sleep(40);
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 120);
  assert.match(env.status.textContent, /新鲜期/, env.status.textContent);
});

// ---------- 9. 校验早退后 idle 轮询必须接回去 ----------

await check('校验启动抛错后，状态行说"校验启动失败"（不是状态读取失败），低频重读继续', async () => {
  const env = setup({ verifyThrows: true, idlePollMs: 100 });
  await sleep(60);
  await env.ctl.verify();
  // 失败面是 verify 不是 state：混用同一句会把人引到错误的排查方向。
  assert.match(env.status.textContent, /校验启动失败/, env.status.textContent);
  assert.doesNotMatch(env.status.textContent, /状态读取失败/, env.status.textContent);
  const after = env.stateCalls().length;
  await sleep(320);
  assert.ok(env.stateCalls().length > after, '早退路径丢了 idle 轮询：之后"需要校验"不会再出现');
});

await check('校验报 {started:false, running:false} 后，低频状态重读继续', async () => {
  const env = setup({ verifyResult: { started: false, running: false, error: '忙' }, idlePollMs: 100 });
  await sleep(60);
  await env.ctl.verify();
  const after = env.stateCalls().length;
  await sleep(320);
  assert.ok(env.stateCalls().length > after, '这条早退也必须恢复 idle 轮询');
});

// ---------- 10. 隐藏的控件不做低频重读（keepAlive 页切走后仍在 DOM 里） ----------

await check('控件被 v-show 隐藏时不做低频重读，重新可见后自己恢复', async () => {
  const env = setup({ idlePollMs: 100 });
  await sleep(60);
  env.hide();
  const hiddenAt = env.stateCalls().length;
  await sleep(350);
  assert.equal(env.stateCalls().length, hiddenAt, '隐藏期间不该再问后端');
  env.show();
  await sleep(260);
  assert.ok(env.stateCalls().length > hiddenAt, '重新可见后要自己恢复（没有事件可听）');
});

// ---------- 11. 目录集合信号：新建空目录 / 删掉空目录 ----------
//
// 条目层完全没有增删，只有 `new_dirs` / `vanished_dirs`（引擎的数组）与
// `*_truncated`（溢出计数）。壳的自动同步只有 onChange 一个出口，漏判会让内容页
// 永远看不到新建的空相册。

await check('只有目录集合变化（new_dirs）也算变化：要通知内容页', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', added: 0, changed: 0, removed: 0,
                  new_dirs: ['新建相册'], vanished_dirs: [] },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, `新建空目录没被通知（onChange ${reports.length} 次）`);
  assert.deepEqual(reports[0].new_dirs, ['新建相册']);
});

await check('只有 vanished_dirs 也算变化', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', added: 0, changed: 0, removed: 0,
                  new_dirs: [], vanished_dirs: ['被删掉的空相册'] },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, `消失的空目录没被通知（onChange ${reports.length} 次）`);
  assert.deepEqual(reports[0].vanished_dirs, ['被删掉的空相册']);
});

await check('目录列表被截断时按 *_truncated 计数也算变化', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'sync', added: 0, changed: 0, removed: 0,
                  new_dirs: [], vanished_dirs: [],
                  new_dirs_truncated: 3, vanished_dirs_truncated: 0 },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, '溢出计数也是目录集合变化');
});

await check('skip 报告即使带目录字段也不通知（skip 不是变化）', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'skip', reason: 'throttled', new_dirs: ['x'] },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 0, 'skip 只解释这一次为什么没扫，不该让内容页重载');
});

// ---------- 12. 校验完成竞态：任务在第一次状态读取之前就结束了 ----------

const VERIFY_DONE = {
  available: true, busy: false, last_verify_at: 1,
  task: { state: 'done', done: true, success: true,
          extra: { added: 2, changed: 0, removed: 0, pruned: 0 } },
};

await check('校验启动后第一次读到的状态已是终态：仍要收尾（onChange 与完成文案）', async () => {
  const reports = [];
  const env = setup({ verifyState: VERIFY_DONE, onChange: (r) => reports.push(r) });
  await sleep(40);
  await env.ctl.verify();
  await sleep(200);
  assert.equal(reports.length, 1, `收尾丢了：onChange 应为 1 次，实际 ${reports.length}`);
  assert.equal(reports[0].action, 'verify');
  assert.equal(reports[0].added, 2, '报告要带本次校验的计数');
  assert.match(env.title.textContent, /校验完成/, `卡片标题：${env.title.textContent}`);
  assert.equal(env.status.textContent, '校验完成 · 有变化');
  assert.ok(env.toasts.some((t) => /校验完成/.test(t.text)),
    `没有完成提示：${JSON.stringify(env.toasts)}`);
});

await check('校验从 running 走到 done 只收尾一次（pending 与 wasBusy 不重复通知）', async () => {
  const reports = [];
  const env = setup({
    state: { available: true, busy: true,
             task: { state: 'running', total: 10, processed: 2, extra: {} } },
    onChange: (r) => reports.push(r),
  });
  await sleep(40);
  await env.ctl.verify();
  await sleep(120);
  assert.equal(reports.length, 0, '任务还在跑，不该收尾');
  env.setState({ available: true, busy: false,
                 task: { state: 'done', done: true, success: true, extra: { added: 1 } } });
  await sleep(1000);
  assert.equal(reports.length, 1, `收尾应恰好一次，实际 ${reports.length}`);
});

// ---------- 13. 预算型 partial 里已发现的变化 ----------

await check('预算型 partial 里已有的变化先通知，再起续跑校验', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'partial', reason: 'budget', added: 1, new_dirs: ['新建相册'] },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 1, '这一趟已经发现的变化被吞掉了');
  assert.equal(reports[0].action, 'partial');
  assert.equal(env.verifyCalls().length, 1, '续跑校验仍要起');
});

await check('预算型 partial 没有变化时只续跑，不通知内容页', async () => {
  const reports = [];
  const env = setup({
    syncReport: { action: 'partial', reason: 'budget',
                  added: 0, changed: 0, removed: 0 },
    onChange: (r) => reports.push(r),
  });
  env.ctl.autoSync('');
  await sleep(DEBOUNCE + 150);
  assert.equal(reports.length, 0, '没有变化不该让内容页重载');
  assert.equal(env.verifyCalls().length, 1, '预算用尽仍要替用户续跑');
});

// ---------- 14. available:false 的两种来源 ----------
//
// 后端 `plugin_manager.freshness_state`（未接入）与 `FreshnessEngine.state`
// （sqlite 打不开）都返回 available:false。后者重试就能好，不能永久禁用控件。

await check('available:false 带 error（指纹库瞬时打不开）按可重试处理，不永久禁用', async () => {
  const env = setup({ state: { available: false, busy: false, error: 'database is locked' } });
  await sleep(40);
  assert.equal(env.ctl.supported, true, '瞬时失败不得判定为"未接入统一刷新"');
  assert.match(env.status.textContent, /状态暂不可用/, env.status.textContent);
  assert.equal(env.verifyBtn.disabled, false, '按钮要保持可点（重试入口）');
});

await check('available:false 且错误是"未参与统一刷新"：才算明确未接入，永久禁用', async () => {
  const env = setup({
    state: { available: false, busy: false,
             error: '插件未参与统一刷新（未加载或未声明 freshness_spec）' },
  });
  await sleep(40);
  assert.equal(env.ctl.supported, false);
  assert.equal(env.verifyBtn.disabled, true);
  assert.equal(env.status.textContent, '', '未接入时不显示状态文案');
});

await check('available:false 不带 error：仍按明确未接入处理（旧行为不变）', async () => {
  const env = setup({ state: { available: false } });
  await sleep(40);
  assert.equal(env.ctl.supported, false);
  assert.equal(env.verifyBtn.disabled, true);
});

console.log(failed ? `\n${failed} 例失败` : `\nshell freshness 卡片 ${total} 例通过`);
process.exit(failed ? 1 : 0);
