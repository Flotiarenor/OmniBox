// Shell 共享组件「同步 / 校验」状态文案用例（真实 freshness.js + DOM 替身 + 假 Bridge）。
//
// 锁住的状态机（不新增后端字段，只用现有 state）：
//   未校验 / 校验中 x/y / version_stale / 上次校验（不再断言“已是最新”）/
//   刚完成且无变化 / 刚完成且有变化 / 超过 7 天建议重新校验。
//
// 用法：node tests/js/shell_freshness.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createDom } from './dom_stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, '..', '..', 'shell', 'frontend', 'public', 'shell', 'freshness.js');
const DAY = 86400;

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

/** 与 shell_freshness_card.mjs 相同的 `data-fr` 解析小替身，不改共享 DOM stub。 */
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

function setup(initialState) {
  const env = createFreshnessDom();
  const host = Object.assign(env.makeEl('div'), { id: 'host' });
  env.document.body.appendChild(host);

  let cur = initialState;
  let stateFailures = 0;
  const calls = [];
  const bridge = {
    async callSystem(method) {
      calls.push(method);
      if (method === 'system_freshness_state') {
        if (stateFailures > 0) {
          stateFailures -= 1;
          throw new Error('state read failed');
        }
        return cur;
      }
      if (method === 'system_freshness_verify') {
        cur = { available: true, busy: true,
                task: { state: 'running', total: 10, processed: 4, extra: {} } };
        return { started: true, running: true };
      }
      if (method === 'system_freshness_cancel') return { success: true };
      return null;
    },
  };

  globalThis.document = env.document;
  globalThis.window = { Bridge: bridge };
  globalThis.Bridge = bridge;
  globalThis.Toast = { info() { }, error() { }, success() { }, warning() { }, show() { } };

  const Freshness = new Function(`${readFileSync(SRC, 'utf8')}\nreturn window.Freshness;`)();
  const ctl = Freshness.mount({ plugin: 'demo', container: host, unit: '张' });
  return {
    ctl,
    host,
    status: host.querySelector('[data-fr="status"]'),
    card: host.querySelector('[data-fr="card"]'),
    verifyBtn: host.querySelector('[data-fr="verify"]'),
    setState(next) { cur = next; },
    failStateOnce() { stateFailures += 1; },
    calls,
  };
}

const nowSec = () => Math.floor(Date.now() / 1000);
const baseState = (patch = {}) => ({
  available: true, busy: false, task: null,
  entries: 0, version_stale: false,
  last_verify_at: 0,
  ...patch,
});

/** 挂载时 mount() 会主动 poll 一次。 */
async function mount(state) {
  const env = setup(state);
  await sleep(30);
  return env;
}

await check('未校验且没有条目：只显示“尚未校验”', async () => {
  const env = await mount(baseState());
  assert.equal(env.status.textContent, '尚未校验');
});

await check('未校验但有索引：保留 entries/unit 文案', async () => {
  const env = await mount(baseState({ entries: 12 }));
  assert.equal(env.status.textContent, '已索引 12 张 · 尚未校验');
});

await check('version_stale：有变化 · 需要校验', async () => {
  const env = await mount(baseState({ version_stale: true, last_verify_at: nowSec() }));
  assert.equal(env.status.textContent, '有变化 · 需要校验');
});

await check('校验中：显示 x/y', async () => {
  const env = await mount(baseState({
    busy: true, task: { state: 'running', total: 10, processed: 4 },
    last_verify_at: nowSec(),
  }));
  assert.equal(env.status.textContent, '校验中 4/10');
});

await check('有 last_verify_at：只说上次校验，不断言最新', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() }));
  assert.equal(env.status.textContent, '上次校验 刚刚');
  assert.doesNotMatch(env.status.textContent, /最新/);
});

await check('上次校验超过 7 天：建议重新校验，而不是最新', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() - 8 * DAY }));
  assert.match(env.status.textContent, /^上次校验 .+ · 建议重新校验$/);
  assert.doesNotMatch(env.status.textContent, /最新/);
});

await check('校验完成且无变化：仅本次完成后短暂显示“未发现变化”', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  await env.ctl.verify();
  await sleep(40);
  assert.equal(env.status.textContent, '校验中 4/10');

  env.setState(baseState({
    last_verify_at: nowSec(),
    task: { state: 'done', done: true, success: true,
            extra: { added: 0, changed: 0, removed: 0, pruned: 0 } },
  }));
  await sleep(650);
  assert.equal(env.status.textContent, '校验完成 · 未发现变化');

  await sleep(4300);
  assert.equal(env.status.textContent, '上次校验 刚刚');
});

await check('校验完成且有变化：不冒充“未发现变化”', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  await env.ctl.verify();
  await sleep(40);
  env.setState(baseState({
    last_verify_at: nowSec(),
    task: { state: 'done', done: true, success: true,
            extra: { added: 2, changed: 0, removed: 0, pruned: 0 } },
  }));
  await sleep(650);
  assert.equal(env.status.textContent, '校验完成 · 有变化');
});

await check('校验完成但有失败项：显示失败数', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  await env.ctl.verify();
  await sleep(40);
  env.setState(baseState({
    last_verify_at: nowSec(),
    task: { state: 'done', done: true, success: false, error_count: 2,
            extra: { added: 0, changed: 0, removed: 0, pruned: 0 } },
  }));
  await sleep(650);
  assert.equal(env.status.textContent, '校验完成 · 2 项失败');
});

await check('校验完成但后端没给变化计数：不冒充“未发现变化”', async () => {
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  await env.ctl.verify();
  await sleep(40);
  env.setState(baseState({
    last_verify_at: nowSec(),
    task: { state: 'done', done: true, success: true },
  }));
  await sleep(650);
  assert.equal(env.status.textContent, '校验完成');
});

await check('同一实例连续第二次校验仍会轮询（首次结束后轮询定时器必须复位）', async () => {
  // 回归：poll() 结束一次轮询后没有把 timer 置回 null，verify() 的
  // `if (!timer) poll()` 于是被一个已经 fire 过的旧 id 挡住 —— 第二次点「校验」
  // 只弹卡片、不轮询，状态永远停在「上次校验 …」，卡片停在 0 / 0。
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  await env.ctl.verify();
  await sleep(40);
  env.setState(baseState({
    last_verify_at: nowSec(),
    task: { state: 'done', done: true, success: true,
            extra: { added: 0, changed: 0, removed: 0, pruned: 0 } },
  }));
  await sleep(650);
  assert.equal(env.status.textContent, '校验完成 · 未发现变化', '第一次校验应先正常结算');

  // 第二次：桥把 state 重新置为 running；不轮询的话状态会停在「上次校验 刚刚」
  await env.ctl.verify();
  await sleep(80);
  assert.equal(env.status.textContent, '校验中 4/10');
});

await check('状态/卡片文本不接受 HTML（task.current 与 errors 只走 textContent）', async () => {
  // 这两个字段来自后端任务状态（可能含文件名 / 错误消息），不是本组件生成的文本。
  const payload = '\'"><img src=x onerror=alert(1)>&';
  const env = await mount(baseState({
    busy: true,
    task: { state: 'running', total: 2, processed: 1, current: payload, errors: [payload] },
  }));
  assert.equal(env.status.textContent, '校验中 1/2');
  const current = env.host.querySelector('[data-fr="current"]');
  const errors = env.host.querySelector('[data-fr="errors"]');
  assert.ok(current, '没有渲染出 current 节点');
  assert.ok(errors, '没有渲染出 errors 节点');
  assert.equal(current.textContent, `正在处理：${payload}`, '载荷应按原样作为文本显示');
  assert.ok(errors.textContent.includes(payload), '失败示例应按原样作为文本显示');
  assert.ok(!String(current.innerHTML).includes('<img'), `current 被当成 HTML 写入：${current.innerHTML}`);
  assert.ok(!String(errors.innerHTML).includes('<img'), `errors 被当成 HTML 写入：${errors.innerHTML}`);
});

await check('一次状态读取失败不永久禁用校验（错误态可重试）', async () => {
  // 回归：poll 的 catch 曾把 supported 置 false —— 后端一次 5xx / 超时之后，
  // 「校验」按钮永远禁用、状态被清空，只有刷新页面才能恢复。
  const env = await mount(baseState({ last_verify_at: nowSec() - 30 }));
  env.failStateOnce();
  await env.ctl.verify();   // verify 把状态置 busy 后立刻 poll，这一次读取会失败
  await sleep(40);
  assert.equal(env.ctl.supported, true, '瞬时错误不得把插件判定为"未接入统一刷新"');
  assert.equal(env.verifyBtn.disabled, false, '校验按钮应保持可点（可重试）');
  assert.match(env.status.textContent, /重试/, `错误态应提示可重试：${env.status.textContent}`);

  // 后端恢复后重试：正常轮询并显示进度
  await env.ctl.verify();
  await sleep(80);
  assert.equal(env.status.textContent, '校验中 4/10');
});

console.log(failed ? `\n${failed} 例失败` : `\nshell freshness 状态 ${total} 例通过`);
process.exit(failed ? 1 : 0);
