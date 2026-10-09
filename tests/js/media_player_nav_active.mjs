// media-player 侧栏高亮的互斥用例（真实 app.js 分片 + DOM 替身）。
//
// 回归对象：作用域项（`.mp-nav-item`）与扩展入口（`.obx-extension`）原先各清各的 ——
// `switchView` 只清 `.mp-nav-item`，扩展点击只清扩展项。点过「网易云登录」再点
// 「全部音乐」，两个会同时高亮（实测用户就是这样看到"全部音乐 + 网易云登录同时亮"）。
// 现在只有 `_setNavActive` 一个入口，两个组永远互斥。
//
// 用法：node tests/js/media_player_nav_active.mjs
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { createDom } from './dom_stub.mjs';
import { readDeclaredScripts } from './script_load_contract.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(here, '..', '..', 'plugins', 'media-player', 'frontend');

let failed = 0;
let total = 0;
function check(name, fn) {
  total++;
  try {
    fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

function setup() {
  const { document, makeEl } = createDom();
  const navViews = ['recent', 'all-audio', 'all-video'];
  const navs = navViews.map(view => {
    const el = makeEl('button');
    el.className = 'obx-nav-item mp-nav-item';
    el.dataset.view = view;
    document.body.appendChild(el);
    return el;
  });
  const wrap = makeEl('div');
  wrap.id = 'mp-extensions';
  document.body.appendChild(wrap);
  const exts = ['ncm-login', 'ncm-daily'].map(view => {
    const el = makeEl('button');
    el.className = 'obx-extension';
    el.dataset.view = view;
    wrap.appendChild(el);
    return el;
  });

  globalThis.document = document;
  globalThis.window = {};
  globalThis.Bridge = { async call() { return null; } };
  globalThis.Toast = { info() { }, error() { }, success() { }, warning() { } };
  const source = readDeclaredScripts(FRONTEND).map(s => s.source).join('\n;\n');
  const MediaPlayerApp = new Function(`${source}\nreturn MediaPlayerApp;`)();
  return { app: new MediaPlayerApp(), navs, exts };
}

const active = els => els.filter(el => el.classList.contains('active')).map(el => el.dataset.view);

check('作用域项：只亮一个', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });
  assert.deepEqual(active(env.navs), ['all-audio']);
  assert.deepEqual(active(env.exts), []);
});

check('扩展入口：只亮一个，作用域项全灭', () => {
  const env = setup();
  env.app._setNavActive({ ext: env.exts[0] });
  assert.deepEqual(active(env.exts), ['ncm-login']);
  assert.deepEqual(active(env.navs), [], '扩展入口亮起时作用域项必须灭');
});

check('从扩展切回作用域：扩展入口必须灭（回归点）', () => {
  const env = setup();
  env.app._setNavActive({ ext: env.exts[0] });     // 先点「网易云登录」
  env.app._setNavActive({ view: 'all-audio' });    // 再点「全部音乐」

  assert.deepEqual(active(env.navs), ['all-audio']);
  assert.deepEqual(active(env.exts), [], '两个同时高亮就是这次的 bug');
});

check('二级页（专辑 / 歌单详情）全不亮', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });
  env.app._setNavActive({});
  assert.deepEqual(active(env.navs), []);
  assert.deepEqual(active(env.exts), []);
});

console.log(failed ? `\n${failed} 例失败` : `\nmedia-player 侧栏高亮 ${total} 例通过`);
process.exit(failed ? 1 : 0);
