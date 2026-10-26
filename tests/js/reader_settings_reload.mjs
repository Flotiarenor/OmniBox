// 设置弹窗保存后，阅读偏好必须重新读一遍并应用（正文底色 / 主题 / 字号 / 阅读模式）。
//
// 为什么值得单测：壳的设置弹窗只把值写进后端，既不重载页面也不通知插件页
// （`omnibox:settings-changed` 在壳里只有接收分支，见 docs/plugin-guide.md §4.3）。
// 插件必须自己收尾，否则「正文底色 = 透出壳背景」这一项改完毫无变化，而页内下一次
// save() 又会把内存里的旧值整份写回后端 —— 用户看到的就是"这一项永远不生效"。
//
// 断言只碰 `ReaderSettingsStore` 的取值与应用结果（class），布局与像素另有真实浏览器
// 用例（tests/test_document_reader_browser_e2e.py）。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..', '..', 'plugins', 'document-reader', 'frontend');

const { document, makeEl } = createDom();

globalThis.document = document;
globalThis.localStorage = {
  getItem: () => null,       // 没有旧版本偏好：用例只测"读后端"
  setItem() {},
  removeItem() {},
};

// 后端设置：用例中途改写，模拟"用户在设置弹窗里保存"
const backend = {
  settings: {
    reader_font_size: 16, reader_line_height: 1.8, reader_letter_spacing: 0,
    reader_theme: 'auto', reader_mode: 'scroll',
    reader_bg_mode: 'show', reader_body_color: '#ffffff',
  },
  calls: [],
  failGet: false,
};

globalThis.Bridge = {
  async call(method, ...args) {
    backend.calls.push({ method, args });
    if (method === 'get_settings') {
      if (backend.failGet) throw new Error('后端不可用');
      return { ...backend.settings };
    }
    if (method === 'save_settings') return { success: true };
    return {};
  },
};

const contentArea = makeEl('div');
contentArea.className = 'document-content-area';
contentArea.id = 'document-content-area';
// 共享替身的 style 只有 setProperty，真实 DOM 还有 removeProperty（apply 用它清变量）
const cssVars = new Map();
contentArea.style = {
  setProperty: (key, value) => cssVars.set(key, String(value)),
  removeProperty: key => cssVars.delete(key),
};
document.body.appendChild(contentArea);

const els = {};
for (const [tag, id] of [['select', 'document-bg-mode'], ['input', 'document-body-color'],
                         ['select', 'document-theme-select'], ['select', 'document-mode-select'],
                         ['input', 'document-font-size']]) {
  els[id] = makeEl(tag);
  els[id].id = id;
  document.body.appendChild(els[id]);
}

const src = fs.readFileSync(path.join(FRONTEND, 'js', 'settings.js'), 'utf8');
const { ReaderSettingsStore } = new Function(`${src}; return { ReaderSettingsStore };`)();

const app = {
  _dom: {
    contentArea,
    bgModeSelect: els['document-bg-mode'],
    bodyColorInput: els['document-body-color'],
    themeSelect: els['document-theme-select'],
    modeSelect: els['document-mode-select'],
    fontSizeSlider: els['document-font-size'],
  },
};
const store = new ReaderSettingsStore(app);

const classes = () => String(contentArea.className).split(/\s+/);
const check = (name, ok, detail = '') => {
  assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
  console.log(`ok - ${name}`);
};

// ---- 1. 加载：后端值原样应用 ----
check('load 之前不算已读（不许拿默认值回写）', store.loaded === false);
await store.load();
check('load 之后才算已读', store.loaded === true);
check('load 应用了 body-bg-show', classes().includes('body-bg-show'), contentArea.className);
check('load 应用了主题类', classes().includes('theme-auto'), contentArea.className);
check('load 回填了正文底色下拉框', els['document-bg-mode'].value === 'show',
      els['document-bg-mode'].value);

// ---- 2. 设置弹窗保存后 reload：内存值、class、控件一起换 ----
backend.settings = {
  reader_font_size: 20, reader_line_height: 2.1, reader_letter_spacing: 1,
  reader_theme: 'dark', reader_mode: 'page',
  reader_bg_mode: 'solid', reader_body_color: '#123456',
};
const savedBefore = backend.calls.filter(c => c.method === 'save_settings').length;
const ok = await store.reload();
check('reload 成功返回 true', ok === true, String(ok));
check('reload 换上了新档位', classes().includes('body-bg-solid')
      && !classes().includes('body-bg-show'), contentArea.className);
check('reload 换上了新主题', classes().includes('theme-dark') && !classes().includes('theme-auto'),
      contentArea.className);
check('reload 更新了内存里的字号/模式/配色',
      app.fontSize === 20 && app.mode === 'page' && app.theme === 'dark'
      && app.bgMode === 'solid' && app.bodyColor === '#123456',
      JSON.stringify({ fontSize: app.fontSize, mode: app.mode, theme: app.theme,
                       bgMode: app.bgMode, bodyColor: app.bodyColor }));
check('reload 回填了页内控件',
      els['document-bg-mode'].value === 'solid'
      && els['document-body-color'].value === '#123456'
      && els['document-theme-select'].value === 'dark'
      && els['document-mode-select'].value === 'page',
      JSON.stringify({ bgMode: els['document-bg-mode'].value,
                       bodyColor: els['document-body-color'].value,
                       theme: els['document-theme-select'].value,
                       mode: els['document-mode-select'].value }));
check('reload 不回写后端', backend.calls.filter(c => c.method === 'save_settings').length
      === savedBefore, 'reload 期间调用了 save_settings');
check('reload 把「指定色彩」写进 --reader-body-bg',
      cssVars.get('--reader-body-bg') === '#123456', String(cssVars.get('--reader-body-bg')));

// ---- 3. 读失败时保持现状，不退回默认值 ----
const realConsoleError = console.error;
console.error = () => {};        // reload 的失败分支会打一条错误日志，这里不刷屏
backend.failGet = true;
const failed = await store.reload();
check('读失败返回 false', failed === false, String(failed));
check('读失败不改动已应用的档位', classes().includes('body-bg-solid') && app.bgMode === 'solid',
      JSON.stringify({ cls: contentArea.className, bgMode: app.bgMode }));
backend.failGet = false;
console.error = realConsoleError;

// ---- 4. save(true) 返回 Promise，供"打开弹窗前先落盘"等待 ----
backend.settings = { ...backend.settings };
const pending = store.save(true);
check('save(true) 返回可等待的 Promise', pending && typeof pending.then === 'function');
await pending;
const written = backend.calls.filter(c => c.method === 'save_settings').pop();
check('save(true) 落盘了正文底色', written && written.args[0].reader_bg_mode === 'solid',
      JSON.stringify(written && written.args[0]));
check('防抖的 save() 不返回 Promise', store.save() === null);

console.log('全部通过');
