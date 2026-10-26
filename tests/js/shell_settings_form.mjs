// 壳的设置表单（createSettingsForm）渲染与提交契约：真实 base.js + 极简 stub DOM。
//
// 重点锁 `type: "info"`（只读信息行，image-cleaner 用它显示"相册根目录"）：
//   1) 值要渲染出来（优先取后端 get_settings() 的 values，其次 schema 里的 value）；
//   2) 有 label 与值两行，但**没有输入控件** —— 它不是能改的东西；
//   3) 不参与提交：getValues() 里不能出现这个键。后端 `plugin_base.save_settings` 同样
//      把它排除在可写键之外（两侧都不认，一层漏了另一层仍然拦得住）。
// 同时用 range / text 字段做对照，确认普通字段照常渲染与提交。
//
// 用法：node tests/js/shell_settings_form.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const BASE_JS = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'base.js');

let failed = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  PASS  ${name}`);
  } catch (e) {
    failed++;
    console.log(`  FAIL  ${name} — ${e.message}`);
  }
}

/** 把真实 base.js 装进 vm，取回 createSettingsForm（顶层函数声明即上下文的全局）。 */
function loadShell() {
  const { document, makeEl } = createDom();
  const win = {
    label: 'plugin',
    location: { origin: 'http://127.0.0.1:1', href: 'http://127.0.0.1:1/plugins/x/frontend/index.html' },
    addEventListener() { }, removeEventListener() { }, postMessage() { },
  };
  win.parent = win;   // 没有上层：HostChannel.self() 因此为 null
  const sandbox = {
    window: win, document, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });
  return { createSettingsForm: vm.runInContext('createSettingsForm', sandbox), makeEl, win };
}

const { createSettingsForm, makeEl, win } = loadShell();

const SCHEMA = [
  { key: 'root_dir', label: '相册根目录', type: 'info', help: '扫描范围取自「图片相册」的根目录' },
  { key: 'threshold', label: '相似判定阈值', type: 'range', min: 0, max: 16, default: 8 },
  { key: 'note', label: '备注', type: 'text', default: '' },
];
const VALUES = { root_dir: 'D:\\omnibox\\data', threshold: 4 };

function render(schema = SCHEMA, values = VALUES) {
  const container = makeEl('div');
  const form = createSettingsForm(container, schema, values);
  return { container, form };
}
function fieldWrap(container, key) {
  const wrap = container.children.find((c) => c.dataset && c.dataset.key === key);
  assert.ok(wrap, `没有渲染出 ${key} 字段`);
  return wrap;
}

// ---------- info：渲染出值、没有控件 ----------

check('info 字段渲染出后端给的值与 label，且不含输入控件', () => {
  const { container } = render();
  const wrap = fieldWrap(container, 'root_dir');
  const label = wrap.querySelector('.field-label');
  const info = wrap.querySelector('.field-info');
  assert.equal(label.textContent, '相册根目录');
  assert.equal(info.textContent, 'D:\\omnibox\\data');
  assert.equal(wrap.querySelectorAll('input').length, 0, 'info 行不该出现输入控件');
  assert.equal(wrap.querySelector('.field-help').textContent, '扫描范围取自「图片相册」的根目录');
});

check('info 的值也可以直接写在 schema 的 value 上（后端不经过 values 时）', () => {
  const { container } = render([{ key: 'root_dir', label: '相册根目录', type: 'info', value: 'X:\\图库' }], {});
  assert.equal(fieldWrap(container, 'root_dir').querySelector('.field-info').textContent, 'X:\\图库');
});

check('info 缺值时渲染空文本，不出现 "undefined"', () => {
  const { container } = render([{ key: 'root_dir', label: '相册根目录', type: 'info' }], {});
  assert.equal(fieldWrap(container, 'root_dir').querySelector('.field-info').textContent, '');
});

// ---------- info：不参与提交 ----------

check('info 字段不进 getValues()，普通字段照常提交', () => {
  const { form } = render();
  const values = form.getValues();
  assert.ok(!('root_dir' in values), `info 字段被当成设置提交了：${JSON.stringify(values)}`);
  assert.equal(values.threshold, 4, 'range 字段要照常提交');
  assert.equal(values.note, '', 'text 字段要照常提交');
});

check('setValues 跳过 info 字段（它没有控件可回填），其余字段照常回填', () => {
  const { container, form } = render();
  form.setValues({ root_dir: '换一个目录', threshold: 9, note: 'hello' });
  assert.equal(container.children.find((c) => c.dataset.key === 'root_dir')
    .querySelector('.field-info').textContent, 'D:\\omnibox\\data', 'info 行显示的是后端值，不被 setValues 改写');
  assert.equal(fieldWrap(container, 'threshold').querySelector('input').value, 9);
  assert.equal(fieldWrap(container, 'note').querySelector('input').value, 'hello');
});

// ---------- directory：单值字段必须用单值模式，否则用户能加多行、保存却只留第一行 ----------

/** 最小 FolderPicker 替身：只记录 createList 收到的选项与当前路径。 */
const pickerCalls = [];
win.FolderPicker = {
  createList(opts) {
    const state = { opts: opts || {}, paths: ((opts && opts.paths) || []).slice() };
    pickerCalls.push(state);
    return {
      element: makeEl('div'),
      getPaths: () => state.paths.slice(),
      setPaths: (paths) => { state.paths = (paths || []).slice(); },
      addPath: (path) => { state.paths = [path]; return true; },
    };
  },
};

check('单值 directory 字段走 single 模式，多值字段保留多行', () => {
  pickerCalls.length = 0;
  const schema = [
    { key: 'download_dir', label: '下载目录', type: 'directory' },
    { key: 'media_roots', label: '媒体文件夹', type: 'directory', multi: true },
  ];
  const values = { download_dir: 'D:\\下载', media_roots: 'D:\\媒体\nE:\\备份' };
  const form = createSettingsForm(makeEl('div'), schema, values);
  assert.equal(pickerCalls.length, 2, '两个 directory 字段都应由 FolderPicker 渲染');
  assert.equal(pickerCalls[0].opts.single, true,
    '非 multi 字段是单值设置，必须禁止加出第二行（否则保存时第二行被静默丢弃）');
  assert.ok(!pickerCalls[1].opts.single, 'multi 字段不能进入 single 模式');
  // 跨 realm 的数组不能用 deepEqual（sandbox 的 Array 与宿主不同），逐项断言
  assert.equal(pickerCalls[0].opts.paths.length, 1);
  assert.equal(pickerCalls[0].opts.paths[0], 'D:\\下载');
  assert.equal(pickerCalls[1].opts.paths.length, 2);
  assert.equal(pickerCalls[1].opts.paths[0], 'D:\\媒体');
  assert.equal(pickerCalls[1].opts.paths[1], 'E:\\备份');
  const submitted = form.getValues();
  assert.equal(submitted.download_dir, 'D:\\下载');
  assert.equal(submitted.media_roots, 'D:\\媒体\nE:\\备份');
});

console.log(failed ? `\n${failed} 例失败` : '\n设置表单 6 例通过');
process.exit(failed ? 1 : 0);
