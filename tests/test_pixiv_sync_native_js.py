"""pixiv-sync 原生逻辑页（`frontend/js/native.js`）与 `/shell/native-client.js` 的契约用例。

背景：pixiv-sync 从「内嵌完整页面」改成「逻辑页只提交 schema blocks」，native.js 是
本次新增的 724 行前端逻辑（`frontend/native.html` 只装载统一客户端）。它自己实现
OAuth 两步流、任务进度、两步确认、secret 清除与轮询；这些行为没有 Python 侧可测的
钩子，只能把真实脚本装进 node。

本用例通过临时 harness（Python 写出、node 执行）装载**真实的** native-client.js 与
native.js，锁住：
  1. `NativeExtension.create` 的 ready/init/action/schema 通道与 pagehide 清理；
  2. 未配置 Token 的空态与禁用按钮；任务运行时 progress 块的字段名；
  3. OAuth：start_oauth → form(code) → finish_oauth 参数与回到主视图；
  4. 「清除 Token」两步确认后真的只提交 `{refresh_token: ''}`（清空=清除的契约）；
  5. `settings-saved` 会重读状态；
  6. 缺 `/shell/native-client.js` 时向壳报 `omnibox:native-error`，而不是静默空白。

harness 写成字符串而不是 tests/js/*.mjs：pixiv-sync 的写范围只开放 `tests/test_pixiv_sync*`。

无 node 时跳过。

运行：
    python -m unittest tests.test_pixiv_sync_native_js -v
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HARNESS = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = process.argv[2];
const CLIENT = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'native-client.js');
const NATIVE = path.join(ROOT, 'plugins', 'pixiv-sync', 'frontend', 'js', 'native.js');

let failed = 0;
let total = 0;
async function check(name, fn) {
  total++;
  try {
    await fn();
    console.log('  PASS  ' + name);
  } catch (e) {
    failed++;
    console.log('  FAIL  ' + name + ' — ' + (e && e.message ? e.message : e));
  }
}
function assert(cond, message) {
  if (!cond) throw new Error(message || 'assertion failed');
}
function sameJson(actual, expected) {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) throw new Error(a + ' !== ' + b);
}

function setup(initialStatus) {
  const messages = [];
  const listeners = {};
  const calls = [];
  const timers = new Map();
  let timerSeq = 0;
  let status = initialStatus;
  const parent = { postMessage: (payload) => messages.push(payload) };
  const fakeWindow = {
    parent,
    location: { origin: 'http://127.0.0.1:18080' },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  };
  const sandbox = {
    window: fakeWindow,
    document: {},
    console,
    Bridge: {
      async call(method, ...args) {
        calls.push({ method, args });
        if (method === 'get_status') return status;
        if (method === 'start_oauth') return { ok: true, url: 'https://app-api.pixiv.net/web/v1/login?code_challenge=x' };
        if (method === 'finish_oauth') return { ok: true, user_id: 'u1' };
        return { ok: true };
      },
    },
    setTimeout(fn, ms) { const id = ++timerSeq; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 0; },
    clearInterval() { },
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(CLIENT, 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(NATIVE, 'utf8'), ctx);
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  const send = async (data) => {
    for (const fn of listeners.message || []) {
      await fn({ source: parent, origin: fakeWindow.location.origin, data });
    }
    await flush();
  };
  const schema = () => messages.filter(m => m.type === 'omnibox:schema').at(-1);
  const blocks = () => (schema() && schema().blocks) || [];
  const toolbar = () => (schema() && schema().toolbar) || [];
  const block = (id) => blocks().find(b => b.id === id) || null;
  return {
    messages, listeners, calls, timers, status: (next) => { status = next; },
    send, schema, blocks, toolbar, block,
    init: () => send({ type: 'omnibox:native-init' }),
    action: (name, payload) => send({ type: 'omnibox:native-action', action: name, payload }),
    lastCall: (method) => calls.filter(c => c.method === method).at(-1),
    callCount: (method) => calls.filter(c => c.method === method).length,
  };
}

const IDLE_STATUS = {
  token_configured: false,
  downloaded_total: 12,
  following_total: 3,
  pending_following: 2,
  bookmarks_total: 5,
  pending_bookmarks: 1,
  selected_artists: 0,
  root_dir: 'D:\\pixiv',
  rate_limit_cooldown: 0,
  task: null,
};

check('装载即发 omnibox:native-ready；native-init 后提交 schema 与 toolbar', async () => {
  const env = setup(IDLE_STATUS);
  assert(env.messages.some(m => m.type === 'omnibox:native-ready'), '装载期要发 native-ready');
  await env.init();
  const schema = env.schema();
  assert(schema, 'native-init 后要提交 schema');
  assert(Array.isArray(schema.blocks) && schema.blocks.length > 0, 'blocks 不能为空');
  assert(Array.isArray(schema.toolbar), 'toolbar 必须是数组');
  sameJson(schema.toolbar.map(a => a.id), ['refresh-status', 'oauth-start', 'open-config', 'open-settings']);
  assert(env.block('no-token'), '未配置 Token 要有空态块');
  const actions = env.block('main-actions').actions;
  assert(actions.every(a => a.disabled === true), '未配置 Token 时同步按钮应禁用');
});

check('任务运行中：progress 块字段名与 native 契约一致（value/total/text/detail/cancel）', async () => {
  const env = setup({
    ...IDLE_STATUS,
    token_configured: true,
    task: {
      kind: 'following', state: 'running', done: 3, total: 10,
      downloaded: 2, skipped: 1, failed: 0, current: '作品 123',
    },
  });
  await env.init();
  const progress = env.block('task-progress');
  assert(progress, '运行中要有 progress 块');
  assert(progress.type === 'progress');
  assert(progress.value === 3 && progress.total === 10, '进度取 done/total');
  assert(progress.text.indexOf('同步画师') === 0, '进度文案带任务类型');
  assert(progress.detail === '正在处理：作品 123');
  assert(progress.cancel && progress.cancel.id === 'cancel-task', 'progress 自带取消动作');
  assert(!env.block('main-actions').actions.some(a => a.disabled === false),
    '任务运行中同步按钮应禁用');
});

check('OAuth：start_oauth 后提交登录地址与 form(code)，完成后回到主视图', async () => {
  const env = setup({ ...IDLE_STATUS, token_configured: true });
  await env.init();
  await env.action('oauth-start');
  assert(env.lastCall('start_oauth'), '要调 start_oauth');
  const form = env.block('oauth-form');
  assert(form && form.type === 'form', 'OAuth 视图要有授权码表单');
  sameJson(form.fields.map(f => f.key), ['code']);
  assert(env.block('oauth-url').items[0].sub.indexOf('https://app-api.pixiv.net') === 0,
    '登录地址要能被复制');
  sameJson(env.toolbar().map(a => a.id), ['oauth-close', 'oauth-restart']);

  await env.action('oauth-finish', { values: { code: '  abc123  ' } });
  assert(env.lastCall('finish_oauth'), '要调 finish_oauth');
  assert(env.lastCall('finish_oauth').args[0] === 'abc123', '授权码要去空白后提交');
  const notice = env.block('notice');
  assert(notice && /Token 获取成功/.test(notice.text));
  assert(env.block('main-actions'), '完成后回到主视图');
});

check('清除 Token：两步确认后只提交空串 refresh_token（掩码语义不被破坏）', async () => {
  const env = setup({ ...IDLE_STATUS, token_configured: true });
  await env.init();
  await env.action('logout');
  const pending = env.block('pending-actions');
  assert(pending, '第一步只展示确认动作，不直接清 Token');
  assert(env.callCount('save_settings') === 0, '确认前不能写设置');
  sameJson(pending.actions.map(a => a.id), ['logout-confirm', 'pending-cancel']);

  await env.action('logout-confirm');
  const call = env.lastCall('save_settings');
  assert(call, '确认后要写设置');
  sameJson(call.args[0], { refresh_token: '' });
  assert(env.block('notice') && /已清除/.test(env.block('notice').text));
});

check('settings-saved 由壳回发后重读 get_status', async () => {
  const env = setup({ ...IDLE_STATUS, token_configured: true });
  await env.init();
  const before = env.callCount('get_status');
  await env.action('settings-saved');
  assert(env.callCount('get_status') > before, '设置保存后要重读状态');
});

check('pagehide 清理轮询定时器（iframe 被移除后不残留 timer）', async () => {
  const env = setup(IDLE_STATUS);
  await env.init();
  assert(env.timers.size > 0, '初始化后应有一个轮询定时器');
  for (const fn of env.listeners.pagehide || []) fn({});
  assert(env.timers.size === 0, 'pagehide 后不能继续轮询');
});

check('缺少 /shell/native-client.js 时向壳报 native-error，而不是空白面板', async () => {
  const messages = [];
  const sandbox = {
    window: {
      parent: { postMessage: (payload) => messages.push(payload) },
      location: { origin: 'http://127.0.0.1:18080' },
      addEventListener() { },
    },
    document: {},
    console,
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(NATIVE, 'utf8'), ctx);
  const error = messages.find(m => m.type === 'omnibox:native-error');
  assert(error, '缺客户端时要报 native-error');
  assert(/native-client\.js/.test(error.error), '错误文案要指出缺哪个文件');
});

console.log(failed ? ('\n' + failed + ' 例失败') : ('\npixiv-sync 原生面板 ' + total + ' 例通过'));
process.exit(failed ? 1 : 0);
"""


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 pixiv-sync 原生面板用例')
class PixivSyncNativeJsTest(unittest.TestCase):
    def test_native_client_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / 'pixiv_native_harness.js'
            harness.write_text(HARNESS, encoding='utf-8')
            proc = subprocess.run(
                ['node', str(harness), str(PROJECT_ROOT)],
                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=120,
            )
        if proc.returncode != 0:
            self.fail(f'pixiv-sync 原生面板用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
