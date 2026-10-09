// media-player 侧栏高亮的用例（真实 app.js 分片 + DOM 替身）。
//
// 契约（见 shell/frontend/public/shell/base.js 的 renderExtensions）：
//   - **扩展入口**的高亮归共享组件管：点击时它自己高亮自己、并清掉 `.mp-nav-item`；
//     宿主切视图时调 `controller.clearActive()`。组件侧的互斥由
//     tests/js/shell_extensions_active.mjs 直接测;
//   - 插件这一侧只剩两件事：`_setNavActive({view} | {})` 让侧栏项互斥、并清掉扩展高亮；
//     `openNeteaseView(ext, btn)` 转交给组件 `activate(btn)`。
//
// 回归对象：`switchView` 原先只清 `.mp-nav-item`，扩展点击只清扩展项 —— 先点
// 「网易云登录」再点「全部音乐」两个同时高亮。
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
  const navs = ['recent', 'all-audio', 'all-video'].map(view => {
    const el = makeEl('button');
    el.className = 'obx-nav-item mp-nav-item';
    el.dataset.view = view;
    document.body.appendChild(el);
    return el;
  });
  for (const id of ['media-search', 'btn-search-clear']) {
    document.body.appendChild(Object.assign(makeEl('div'), { id }));
  }

  globalThis.document = document;
  globalThis.window = {};
  globalThis.Bridge = { async call() { return null; } };
  globalThis.Toast = { info() { }, error() { }, success() { }, warning() { } };
  const source = readDeclaredScripts(FRONTEND).map(s => s.source).join('\n;\n');
  const MediaPlayerApp = new Function(`${source}\nreturn MediaPlayerApp;`)();
  const app = new MediaPlayerApp();
  // 共享组件的替身：只记录被调用的动作（真实互斥逻辑在 shell 用例里测）
  const extCalls = [];
  app.extensions = {
    buttons: [],
    activate(btn) { extCalls.push(['activate', btn]); },
    clearActive() { extCalls.push(['clearActive']); },
  };
  return { app, navs, extCalls };
}

const active = els => els.filter(el => el.classList.contains('active')).map(el => el.dataset.view);

check('作用域项：只亮一个', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });
  assert.deepEqual(active(env.navs), ['all-audio']);
});

check('切作用域时清掉扩展高亮（回归点）', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });

  assert.deepEqual(env.extCalls, [['clearActive']],
                   '扩展入口的高亮必须由宿主这一侧清掉，否则两个同时亮');
});

check('打开网易云视图：交给共享组件 activate，并清掉作用域项', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });
  env.app.playlists = { renderSidebar() { }, currentId: '' };
  env.app._loadCurrentView = () => { };
  const btn = { id: 'ext-btn' };
  env.extCalls.length = 0;              // 只看 openNeteaseView 这一下的动作

  env.app.openNeteaseView({ view: 'ncm-login' }, btn);

  assert.deepEqual(active(env.navs), [], '作用域项要灭');
  assert.deepEqual(env.extCalls, [['clearActive'], ['activate', btn]],
                   '先清侧栏项、再把按钮交给组件高亮');
  assert.equal(env.app.currentView, 'ncm-login');
});

check('二级页（专辑 / 歌单详情）全不亮', () => {
  const env = setup();
  env.app._setNavActive({ view: 'all-audio' });
  env.app._setNavActive({});
  assert.deepEqual(active(env.navs), []);
});

console.log(failed ? `\n${failed} 例失败` : `\nmedia-player 侧栏高亮 ${total} 例通过`);
process.exit(failed ? 1 : 0);
