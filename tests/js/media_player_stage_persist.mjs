// media-player「仅声音 / 画面」偏好的持久化失败路径（`_toggleVideoMode` / `_persistVideoMode`）。
//
// 为什么需要：切换本身立即生效，偏好由 `Bridge.call('media_set_config', ...)` 异步写进
// 设置。原来那一步是 `.catch(() => {})`：API 不可用、或后端返回 `{success: false}`
// （`media_set_config` 把 `update_setting` 的布尔结果包成 `success`，HTTP 仍是 200，
// 不算 reject）时，偏好没写进去、界面什么都没说，用户重启后回到旧值，只会觉得
// "这个设置不生效"。这类"报成功但没写"的静默失败在其它模块已经出过（A1/A2）。
//
// 这里用**线上真实实现**跑三条路径：
//   1. 接口成功但 `{success: false}`：提示可见、切换已生效；
//   2. Promise 被拒：同上；
//   3. `{success: true}`：不出现错误提示。
// 另外锁住"连点只弹一条"（壳 Toast 对同一段文本 1.5s 内去重）与非视频项不触发持久化。
//
// 用法：node tests/js/media_player_stage_persist.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';
import { loadShellScripts } from './shell_scripts.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const PLUGIN_JS = path.join(ROOT, 'plugins', 'media-player', 'frontend', 'js');

const SAVE_FAILED = '已切换，但「仅声音 / 画面」偏好没能保存，重启后会恢复原样';

let failed = 0;
async function check(name, fn) {
  try {
    await fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

/** 让 `_persistVideoMode` 的 then/catch 跑完（只差微任务，不依赖定时器）。 */
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

/**
 * 装一个媒体播放器的舞台分片环境。
 *
 * `window` 就是全局对象本身（浏览器语义），因此 toast.js 里的 `window.Toast` 与插件里
 * 的裸 `Toast` 是同一个东西；`Bridge.call` 换成可控替身，返回什么由用例决定。
 */
function createEnv({ callImpl }) {
  const { document, makeEl } = createDom();
  // 真实 toast.js 的消息只走文本节点；dom_stub 只做元素，这里补一个最小文本节点。
  document.createTextNode = (text) => ({ nodeType: 3, textContent: String(text), parentNode: null });
  const btn = makeEl('button');
  btn.id = 'btn-video-mode';

  const calls = [];
  const sandbox = {
    console,
    document,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    // 图标集替身：只需能看出按钮换了哪个图标（真实图标集由引导片段注入）。
    Icons: { html: (name) => `<svg data-icon="${name}"></svg>` },
    Utils: { escapeHtml: (value) => String(value == null ? '' : value) },
    Bridge: {
      call(method, ...args) {
        calls.push([method, ...args]);
        return callImpl(method, args);
      },
    },
    encodeURIComponent, decodeURIComponent, String, Number, Math, JSON, Object, Array,
    Error, Promise, Date,
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  // 壳注入的唯一 Toast 实现（含 1.5s 同文本去重）
  loadShellScripts(ctx, ['toast.js']);
  vm.runInContext(fs.readFileSync(path.join(PLUGIN_JS, 'utils.js'), 'utf8'), ctx, { filename: 'utils.js' });
  vm.runInContext('globalThis.MediaPlayerApp = class MediaPlayerApp { };', ctx);
  vm.runInContext(fs.readFileSync(path.join(PLUGIN_JS, 'app-stage.js'), 'utf8'), ctx, {
    filename: 'app-stage.js',
  });
  const app = vm.runInContext('new MediaPlayerApp()', ctx);
  app.core = {
    currentItem: { kind: 'video', title: '演示视频' },
    videoMode: false,
    setVideoMode(value) { this.videoMode = !!value; },
  };
  return { app, btn, document, calls };
}

/** 读出此刻页面上所有 Toast（容器 → 每条 → 文本节点拼接）。 */
function toasts(document) {
  const container = document.body.children.find(
    (el) => String(el.className).includes('toast-container'));
  if (!container) return [];
  return container.children.map((el) => ({
    className: String(el.className),
    text: el.children
      .filter((child) => child && child.nodeType === 3)
      .map((child) => child.textContent)
      .join(''),
  }));
}

function assertSaveFailedToast(env, label) {
  const shown = toasts(env.document);
  assert.equal(shown.length, 1, `${label}：应当只弹一条提示，实际 ${shown.length}`);
  assert.match(shown[0].className, /toast-error/, `${label}：失败提示必须是 error 档`);
  assert.ok(shown[0].text.includes('已切换'), `${label}：提示要说清本次已切换：${shown[0].text}`);
  assert.ok(shown[0].text.includes('没能保存'), `${label}：提示要说清偏好没保存：${shown[0].text}`);
  assert.ok(shown[0].text.includes('重启'), `${label}：提示要说清重启后会恢复原样：${shown[0].text}`);
}

// ---------------------------------------------------------------- 1. 接口成功但 success:false

await check('success:false：提示可见、切换已生效、写入的是新偏好', async () => {
  const env = createEnv({ callImpl: async () => ({ success: false, error: 'admin required' }) });
  env.app._toggleVideoMode();
  await flush();
  assert.equal(env.app.core.videoMode, true, '切换必须立即生效，不能因为保存失败而回退');
  assert.equal(env.btn.title, '切换到仅声音', '按钮要反映切换后的模式');
  assert.match(env.btn.innerHTML, /clapperboard/, '按钮图标要换成切换后的模式');
  assertSaveFailedToast(env, 'success:false');
  assert.deepEqual(env.calls, [['media_set_config', 'default_video_mode', 'video']],
    '写的是本次切换后的偏好值');
});

// ---------------------------------------------------------------- 2. Promise 被拒

await check('Promise 被拒：同样给出提示，切换不回退', async () => {
  const env = createEnv({ callImpl: async () => { throw new Error('PyWebView API 不可用'); } });
  env.app.core.videoMode = true;   // 从「画面」切回「仅声音」
  env.app._toggleVideoMode();
  await flush();
  assert.equal(env.app.core.videoMode, false, '切换必须立即生效');
  assert.equal(env.btn.title, '切换到画面');
  assertSaveFailedToast(env, 'reject');
  assert.deepEqual(env.calls, [['media_set_config', 'default_video_mode', 'audio']]);
});

// ---------------------------------------------------------------- 3. 成功路径

await check('success:true：不出现错误提示', async () => {
  const env = createEnv({ callImpl: async () => ({ success: true }) });
  env.app._toggleVideoMode();
  await flush();
  assert.equal(env.app.core.videoMode, true);
  assert.deepEqual(toasts(env.document), [], '保存成功时不该有任何提示');
  assert.deepEqual(env.calls, [['media_set_config', 'default_video_mode', 'video']]);
});

// ---------------------------------------------------------------- 4. 连点不刷屏

await check('连续两次失败只弹一条（同一段文本由壳 Toast 去重）', async () => {
  const env = createEnv({ callImpl: async () => ({ success: false }) });
  env.app._toggleVideoMode();
  await flush();
  env.app._toggleVideoMode();
  await flush();
  const shown = toasts(env.document);
  assert.equal(shown.length, 1, `连点两次失败只该留一条提示，实际 ${shown.length}`);
  assert.equal(shown[0].text, SAVE_FAILED, '提示文本与切换方向无关，去重才成立');
});

// ---------------------------------------------------------------- 5. 非视频项不写偏好

await check('当前不是视频时不切换、不写偏好、不提示', async () => {
  const env = createEnv({ callImpl: async () => ({ success: false }) });
  env.app.core.currentItem = { kind: 'audio', title: '一首歌' };
  env.app._toggleVideoMode();
  await flush();
  assert.equal(env.app.core.videoMode, false);
  assert.deepEqual(env.calls, []);
  assert.deepEqual(toasts(env.document), []);
});

console.log(failed ? `\nmedia_player_stage_persist: ${failed} 项失败` : '\nmedia_player_stage_persist: 全部通过');
process.exit(failed ? 1 : 0);
