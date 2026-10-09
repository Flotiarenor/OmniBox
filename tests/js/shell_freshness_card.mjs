// Shell 共享组件「同步 / 校验」的进度卡片用例（真实 freshness.js + DOM 替身 + 假 Bridge）。
//
// 锁的是两个真实踩到过的坑：
//   1) 被动同步报 `partial` 时，组件只弹卡片**没有真起校验**（注释说会起），
//      于是卡片停在静态占位符「0 / 0」上永不消失 —— 用户看到的是"为什么弹出
//      一个正在校验（同步未覆盖全部目录）— 0 / 0"；
//   2) 预算型 partial（`reason: 'budget'`，还有目录要处理）才值得替用户起校验；
//      `dir_cap`（只是没走完）不该反复起，否则每次进视图都跑一趟全量。
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

/** 挂一份组件：`state` 是 `system_freshness_state` 的返回，`syncReport` 是同步报告。 */
function setup({ state = { available: true, busy: false, task: null, unit: '张' },
                 syncReport = null, verifyResult = { started: true, running: true } } = {}) {
  const { document, makeEl } = createFreshnessDom();
  const host = Object.assign(makeEl('div'), { id: 'host' });
  document.body.appendChild(host);

  const calls = [];
  let cur = state;
  const bridge = {
    async callSystem(method, ...args) {
      calls.push({ method, args });
      if (method === 'system_freshness_state') return cur;
      if (method === 'system_freshness_sync') return syncReport;
      if (method === 'system_freshness_verify') {
        // 真实链路里 `start_verify` 会先把任务登记进引擎再返回，因此紧接着的状态
        // 查询就能看到 running（这里照样模拟，否则卡片会被"没有任务"立刻收掉）
        if (verifyResult && verifyResult.started !== false) {
          cur = { available: true, busy: true,
                  task: { state: 'running', total: 0, processed: 0, extra: {} } };
        }
        return verifyResult;
      }
      if (method === 'system_freshness_cancel') return { success: true };
      return null;
    },
  };

  globalThis.document = document;
  globalThis.window = { Bridge: bridge };
  globalThis.Bridge = bridge;
  globalThis.Toast = { info() { }, error() { }, success() { }, warning() { }, show() { } };

  const Freshness = new Function(`${readFileSync(SRC, 'utf8')}\nreturn window.Freshness;`)();
  const ctl = Freshness.mount({ plugin: 'demo', container: host, unit: '张' });
  const card = host.querySelector('[data-fr="card"]');
  const count = host.querySelector('[data-fr="count"]');
  const title = host.querySelector('[data-fr="card-title"]');
  return {
    ctl, calls, host, card, count, title,
    setState(s) { cur = s; },
    verifyCalls: () => calls.filter((c) => c.method === 'system_freshness_verify'),
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

console.log(failed ? `\n${failed} 例失败` : `\nshell freshness 卡片 ${total} 例通过`);
process.exit(failed ? 1 : 0);
