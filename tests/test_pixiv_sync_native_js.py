"""pixiv-sync 原生逻辑页（`frontend/js/native.js`）与 `/shell/native-client.js` 的契约用例。

背景：pixiv-sync 是「逻辑页只提交 schema blocks」的形态，native.js 是它的全部前端逻辑
（`frontend/native.html` 只装载统一客户端）。它自己实现 OAuth 两步流、任务进度、
两步确认、secret 清除与轮询；这些行为没有 Python 侧可测的钩子，只能把真实脚本装进 node。

本用例通过临时 harness（Python 写出、node 执行）装载**真实的** native-client.js 与
native.js，锁住：
  1. `NativeExtension.create` 的 ready/init/action/schema 通道与 pagehide 清理；
  2. 未配置 Token 的空态与禁用按钮；任务运行时 progress 块的字段名；
  3. OAuth：start_oauth → form(code) → finish_oauth 参数与回到主视图；
  4. 「清除 Token」两步确认后真的只提交 `{refresh_token: ''}`（清空=清除的契约）；
  5. `settings-saved` 会重读状态；
  6. 缺 `/shell/native-client.js` 时向壳报 `omnibox:native-error`，而不是静默空白；
  7. 本地内容的空闲概要（上次校验 / 索引待更新 / 上次变更计数）与校验进度块。

harness 写成字符串而不是 tests/js/*.mjs：pixiv-sync 的 node 用例统一放在
`tests/test_pixiv_sync*.py` 里，临时脚本随用例一起生成、用完即删。

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
  const overrides = {};
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
        // 按方法覆盖返回：维护类接口的返回形状由用例决定。
        if (Object.prototype.hasOwnProperty.call(overrides, method)) {
          const value = overrides[method];
          return typeof value === 'function' ? value(...args) : value;
        }
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
  // blocks 可被 section 嵌套（文档式版把指标/任务/操作分区），查找必须递归。
  const flatten = (list) => (list || []).reduce(
    (acc, item) => acc.concat(item, item.blocks ? flatten(item.blocks) : []), []);
  const block = (id) => flatten(blocks()).find(b => b.id === id) || null;
  return {
    messages, listeners, calls, timers, status: (next) => { status = next; },
    /** 指定某个 Bridge 方法的返回值（用于维护类接口）。 */
    bridgeResult: (method, value) => { overrides[method] = value; },
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

// 每个 check 都必须 await：早先版本直接串行调用、随后立即 process.exit，
// 于是所有 async 断言都在退出前没跑完，用例永远"通过"。现在由 run() 顺序 await。
async function run() {
  await check('装载即发 omnibox:native-ready；native-init 后提交 schema 与 toolbar', async () => {
    const env = setup(IDLE_STATUS);
    assert(env.messages.some(m => m.type === 'omnibox:native-ready'), '装载期要发 native-ready');
    await env.init();
    const schema = env.schema();
    assert(schema, 'native-init 后要提交 schema');
    assert(Array.isArray(schema.blocks) && schema.blocks.length > 0, 'blocks 不能为空');
    assert(Array.isArray(schema.toolbar), 'toolbar 必须是数组');
    assert(schema.layout === 'document', '版式要声明 document（壳据此收窄正文）');
    sameJson(schema.toolbar.map(a => a.id), ['refresh-status', 'oauth-start', 'open-config', 'open-settings']);
    assert(env.block('no-token'), '未配置 Token 要有空态块');
    assert(env.block('no-token').icon, '空态要带图标');
    assert(env.block('no-token').submit.id === 'oauth-start', '空态的主操作是获取 Token');
    // 没有 Token 时同步一定失败：空态取代禁用按钮，而不是留一排灰按钮。
    assert(!env.block('main-actions'), '未配置 Token 时不渲染同步按钮');
  });

  await check('指标区：四格同级数字，第 4 格在无失败时显示 other_done', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true, other_done: 7, failed_skipped: 0 });
    await env.init();
    const stats = env.block('status-stats');
    assert(stats && stats.type === 'stats', '要有 stats 块');
    sameJson(stats.metrics.map(m => m.label), ['已下载', '关注待下', '收藏待下', '其他作品']);
    assert(stats.metrics[0].value === '12', '指标值由插件格式化（千分位/单位）');
    sameJson(stats.metrics[2].hint, '共 5');
    const main = env.block('main-actions').actions;
    assert(main.length === 2, '主操作只留两个同步按钮（刷新名单入口已移除）');
    sameJson(main.map(a => a.variant), ['accent', 'accent']);
    sameJson(main.map(a => a.label), ['同步关注作品', '同步收藏作品']);
    assert(!env.block('secondary-actions'), '没有失败跳过时不出现重试入口');
    const meta = env.block('status-meta');
    assert(meta.dot === 'success', '已连接要有绿色状态点');
    const acct = env.block('account-actions').actions;
    const verify = acct.find(a => a.id === 'verify-library');
    const logout = acct.find(a => a.id === 'logout');
    assert(verify && verify.size === 'sm' && !verify.variant,
      '维护区要有「校验本地内容」小按钮（同步不再全盘核对）');
    assert(logout && logout.size === 'sm' && logout.variant === 'danger', '清除 Token 是小号危险按钮');
    assert(!env.block('task-section'), '上次任务正常结束时任务区整块隐藏');
  });

  await check('有失败跳过时：第 4 格换成失败计数，并出现重试入口', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true, failed_skipped: 3 });
    await env.init();
    const stats = env.block('status-stats');
    const last = stats.metrics[3];
    assert(last.label === '失败跳过' && last.value === '3', '失败计数占用第 4 格');
    assert(last.tone === 'danger', '失败计数用语义色');
    const retry = env.block('secondary-actions').actions.find(a => a.id === 'retry-failed');
    assert(retry && /3/.test(retry.label), '失败>0 才出现重试入口，且带数量');
  });

  await check('任务运行中：progress 块字段名与 native 契约一致（value/total/text/detail/cancel）', async () => {
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
    assert(progress.text.indexOf('同步关注作品') === 0, '进度文案带任务类型');
    assert(progress.detail.indexOf('作品 3/10') > 0, '明细行给计数');
    assert(/已用 \d+:\d\d/.test(progress.detail), '明细行要带已用时间，避免只有一个“准备中”');
    assert(progress.indeterminate === false, '有总数时不是不确定进度');
    assert(progress.cancel && progress.cancel.id === 'cancel-task', 'progress 自带取消动作');
    assert(progress.detail.indexOf('作品 123') === 0, '阶段文案（当前处理项）放在明细行最前面');
    assert(!env.block('main-actions').actions.some(a => a.disabled === false),
      '任务运行中同步按钮应禁用');
  });

  await check('总数未定时 progress 标为 indeterminate，不画假百分比', async () => {
    const env = setup({
      ...IDLE_STATUS,
      token_configured: true,
      task: { kind: 'following', state: 'running', done: 0, total: 0, downloaded: 0, skipped: 0, failed: 0 },
    });
    await env.init();
    assert(env.block('task-progress').indeterminate === true, '扫名单阶段要标 indeterminate');
    const idle = env.block('task-progress');
    assert(idle.detail.indexOf('已用') > 0, '没有总数时也要给阶段与已用时间');
  });

  await check('OAuth：start_oauth 后提交登录地址与 form(code)，完成后回到主视图', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true });
    await env.init();
    await env.action('oauth-start');
    assert(env.lastCall('start_oauth'), '要调 start_oauth');
    const form = env.block('oauth-form');
    assert(form && form.type === 'form', 'OAuth 视图要有授权码表单');
    sameJson(form.fields.map(f => f.key), ['code']);
    const code = env.block('oauth-url');
    assert(code && code.type === 'code', '登录地址要用可复制的 code 块');
    assert(code.text.indexOf('https://app-api.pixiv.net') === 0, '登录地址要能被复制');
    assert(code.copyable === true, '要支持复制');
    sameJson(env.toolbar().map(a => a.id), ['oauth-close', 'oauth-restart']);

    await env.action('oauth-finish', { values: { code: '  abc123  ' } });
    assert(env.lastCall('finish_oauth'), '要调 finish_oauth');
    assert(env.lastCall('finish_oauth').args[0] === 'abc123', '授权码要去空白后提交');
    // 操作结果走消息级 toast，不进 blocks（旧版是正文里的 banner）。
    const toast = env.schema().toast;
    assert(toast && /Token 获取成功/.test(toast.text), '结果提示用消息级 toast');
    assert(!env.block('notice'), '操作结果不再占用 blocks');
    assert(env.block('main-actions'), '完成后回到主视图');
  });

  await check('清除 Token：模态确认块由 blocks 派生，确认后才写设置', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true });
    await env.init();
    await env.action('logout');
    const dialog = env.block('confirm-dialog');
    assert(dialog && dialog.type === 'confirm', '清除 Token 要弹阻断确认');
    assert(dialog.tone === 'danger' && dialog.title, '确认块带标题与危险语义');
    sameJson([dialog.confirm.id, dialog.cancel.id], ['logout-confirm', 'confirm-cancel']);
    assert(env.callCount('save_settings') === 0, '确认前不能写设置');

    await env.action('confirm-cancel');
    assert(!env.block('confirm-dialog'), '取消后确认块消失（壳据此关闭模态）');
    assert(env.callCount('save_settings') === 0, '取消不能写设置');

    await env.action('logout');
    await env.action('logout-confirm');
    const call = env.lastCall('save_settings');
    assert(call, '确认后要写设置');
    sameJson(call.args[0], { refresh_token: '' });
    assert(!env.block('confirm-dialog'), '执行后确认块消失');
    assert(/已清除/.test(env.schema().toast.text), '结果用 toast 反馈');
  });

  await check('失败用常驻横幅（可关闭），成功才用 Toast', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true });
    await env.init();
    await env.action('oauth-start');
    // 空授权码是本地校验失败，不碰网络：OAuth 视图里必须能看到持续可见的错误。
    await env.action('oauth-finish', { values: { code: '   ' } });
    const alert = env.block('notice-alert');
    assert(alert && alert.type === 'banner', '失败要常驻横幅，不是一闪而过的 Toast');
    assert(alert.tone === 'danger', '失败用危险色');
    assert(alert.action && alert.action.id === 'notice-dismiss', '横幅要能手动关闭');
    assert(!env.schema().toast, '失败不弹 Toast');
    await env.action('notice-dismiss');
    assert(!env.block('notice-alert'), '点「知道了」后横幅消失');

    // 成功路径：清除 Token 成功只弹 Toast，不占版面。
    await env.action('oauth-close');
    await env.action('logout');
    await env.action('logout-confirm');
    assert(env.schema().toast && /已清除/.test(env.schema().toast.text), '成功用 Toast');
    assert(!env.block('notice-alert'), '成功不产生常驻横幅');
  });

  await check('重试失败：可重来的操作走就地确认，不弹模态', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true, failed_skipped: 2 });
    await env.init();
    await env.action('retry-failed');
    assert(env.block('pending-actions'), '就地二次确认');
    assert(!env.block('confirm-dialog'), '派生数据的清除不弹模态');
    const ids = env.block('pending-actions').actions.map(a => a.id);
    sameJson(ids, ['retry-failed-confirm', 'pending-cancel']);
  });

  await check('限流冷却用横幅提示，不混在正文里', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true, rate_limit_cooldown: 84 });
    await env.init();
    const banner = env.block('cooldown');
    assert(banner && banner.type === 'banner' && banner.tone === 'warning', '冷却要有 warning 横幅');
    assert(/01:24/.test(banner.text), '横幅带冷却时间');
  });

  await check('校验本地内容：启动后台任务，失败留在横幅', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true });
    env.bridgeResult('verify_downloaded', { ok: true, started: true, running: true });
    await env.init();
    await env.action('verify-library');
    assert(env.lastCall('verify_downloaded'), '按钮要调 verify_downloaded');
    const toast = env.schema().toast;
    assert(toast && /正在校验本地内容/.test(toast.text), '启动后要给"正在校验"的 Toast');
    assert(!env.block('notice-alert'), '启动成功不产生横幅');

    const failing = setup({ ...IDLE_STATUS, token_configured: true });
    failing.bridgeResult('verify_downloaded', { ok: false, error: '同步任务进行中' });
    await failing.init();
    await failing.action('verify-library');
    const alert = failing.block('notice-alert');
    assert(alert && /校验启动失败/.test(alert.text), '启动失败留在常驻横幅');
    assert(!failing.schema().toast, '失败不弹 Toast');
  });

  await check('校验本地内容：后台任务期间有进度块，结束后补结果 Toast', async () => {
    const running = {
      ...IDLE_STATUS, token_configured: true,
      library: {
        available: true, busy: true, entries: 0, dirs: 0, last_verify_at: 0,
        task: { state: 'running', total: 9054, processed: 1516,
                current: 'Leafpie/106706225', extra: {} },
      },
    };
    const env = setup(running);
    await env.init();
    const progress = env.block('library-progress');
    assert(progress && progress.type === 'progress', '校验期间要有进度块');
    assert(progress.value === 1516 && progress.total === 9054, '进度取 processed/total');
    assert(/个目录/.test(progress.detail), '明细要说明已处理目录数');
    assert(/Leafpie/.test(progress.detail), '明细要给出当前目录');
    assert(progress.cancel && progress.cancel.id === 'cancel-verify', '要有停止校验动作');
    assert(env.block('main-actions').actions.every(a => a.disabled === true),
      '校验在跑时同步按钮要禁用');
    assert(env.block('account-actions').actions.every(a => a.disabled === true),
      '校验在跑时维护按钮要禁用');

    // 结束：busy=false，结果计数来自引擎任务 extra 的 pass_end 摘要
    env.status({
      ...IDLE_STATUS, token_configured: true,
      library: {
        available: true, busy: false, entries: 29534, dirs: 9083, last_verify_at: 1,
        task: { state: 'done', extra: {
          total: 29534, imported: 3, stale_removed: 1, incomplete: 2 } },
      },
    });
    await env.action('refresh-status');
    const toast = env.schema().toast;
    assert(toast && /本地内容已校验/.test(toast.text), '结束后要有结果 Toast');
    assert(/新增 3/.test(toast.text) && /移除失效 1/.test(toast.text)
      && /缺页重新入队 2/.test(toast.text), 'Toast 要给出三个业务计数');
    assert(!env.block('library-progress'), '结束后进度块消失');
    assert(/本地索引 29,534 项/.test(env.block('account-status').text), '账号区显示索引条目数');
  });

  await check('本地内容空闲时保留概要：上次校验、索引待更新与上次变更计数', async () => {
    const env = setup({
      ...IDLE_STATUS, token_configured: true,
      library: {
        available: true, busy: false, entries: 120, dirs: 30, last_verify_at: 1700000000,
        version_stale: true, stale_dirs: 4,
        task: { state: 'done', extra: {
          total: 120, imported: 2, stale_removed: 1, incomplete: 3, zero_removed: 1 } },
      },
    });
    await env.init();
    const meta = env.block('library-meta');
    assert(meta && meta.type === 'text', '空闲时也要给一行索引概要');
    assert(/已索引 120 项/.test(meta.text), '概要带条目数');
    assert(/上次校验/.test(meta.text), '概要带上次校验时间');
    assert(/索引待更新（4 个目录）/.test(meta.text), '版本落后要说明，并给待盖章目录数');
    assert(/新增 2/.test(meta.text) && /移除失效 1/.test(meta.text)
      && /缺页重新入队 3/.test(meta.text) && /清 0 字节 1/.test(meta.text),
      '上次校验的变更计数留在面板上');
    assert(!env.block('library-progress'), '空闲时没有进度块');

    // 从未索引也从未校验：整块不出现，不给一行空文案。
    const fresh = setup({
      ...IDLE_STATUS, token_configured: true,
      library: {
        available: true, busy: false, entries: 0, dirs: 0, last_verify_at: 0,
        version_stale: false, stale_dirs: 0, task: null,
      },
    });
    await fresh.init();
    assert(!fresh.block('library-meta'), '没有索引也没有校验记录时整块不出现');
  });

  await check('取消是乐观的：点击后界面立刻变，不等后端返回', async () => {
    const env = setup({
      ...IDLE_STATUS,
      token_configured: true,
      running: true,
      task: {
        kind: 'following', state: 'running', done: 3, total: 10,
        downloaded: 2, skipped: 1, failed: 0, current: '作品 123',
      },
    });
    // 后端永不返回：如果实现是"等 cancel_task 回来再改界面"，进度块就会留在原地。
    env.bridgeResult('cancel_task', new Promise(() => {}));
    await env.init();
    assert(env.block('task-progress'), '取消前有进度块');
    void env.action('cancel-task');
    assert(!env.block('task-progress'), '点击后立刻不再显示进度块（乐观取消）');
    const toast = env.schema().toast;
    assert(toast && /已取消/.test(toast.text), '点击后立刻给“已取消”的提示');
  });

  await check('settings-saved 由壳回发后重读 get_status', async () => {
    const env = setup({ ...IDLE_STATUS, token_configured: true });
    await env.init();
    const before = env.callCount('get_status');
    await env.action('settings-saved');
    assert(env.callCount('get_status') > before, '设置保存后要重读状态');
  });

  await check('pagehide 清理轮询定时器（iframe 被移除后不残留 timer）', async () => {
    const env = setup(IDLE_STATUS);
    await env.init();
    assert(env.timers.size > 0, '初始化后应有一个轮询定时器');
    for (const fn of env.listeners.pagehide || []) fn({});
    assert(env.timers.size === 0, 'pagehide 后不能继续轮询');
  });

  await check('缺少 /shell/native-client.js 时向壳报 native-error，而不是空白面板', async () => {
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
}

run().then(() => {
  console.log(failed ? ('\n' + failed + ' 例失败') : ('\npixiv-sync 原生面板 ' + total + ' 例通过'));
  process.exit(failed ? 1 : 0);
}, (e) => {
  console.log('用例执行崩溃：' + (e && e.message ? e.message : e));
  process.exit(1);
});
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
