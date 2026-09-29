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

/** 极简 DOM：够 createSettingsForm 走完渲染与取值（与 host_channel_host.mjs 同一手法）。 */
function makeDom() {
  function matches(el, sel) {
    if (sel.startsWith('.')) return String(el.className).split(/\s+/).includes(sel.slice(1));
    return el.tagName === sel;
  }
  function find(el, sel) {
    for (const child of el.children) {
      if (matches(child, sel)) return child;
      const deep = find(child, sel);
      if (deep) return deep;
    }
    return null;
  }
  function findAll(el, sel, out = []) {
    for (const child of el.children) {
      if (matches(child, sel)) out.push(child);
      findAll(child, sel, out);
    }
    return out;
  }
  function makeEl(tag) {
    const el = {
      tagName: tag, id: '', className: '', textContent: '', value: '', type: '', htmlFor: '',
      style: {}, dataset: {}, children: [], handlers: {}, parentElement: null,
      classList: {
        add(c) { if (!String(el.className).split(/\s+/).includes(c)) el.className = `${el.className} ${c}`.trim(); },
        remove(c) { el.className = String(el.className).split(/\s+/).filter((x) => x && x !== c).join(' '); },
        toggle() { },
        contains: (c) => String(el.className).split(/\s+/).includes(c),
      },
      append(...nodes) { nodes.forEach((n) => el.appendChild(n)); },
      appendChild(child) {
        if (child.__owner) child.__owner.children = child.__owner.children.filter((c) => c !== child);
        child.__owner = el;
        child.parentElement = el;
        el.children.push(child);
        return child;
      },
      addEventListener(type, fn) { (el.handlers[type] = el.handlers[type] || []).push(fn); },
      querySelector: (sel) => find(el, sel),
      querySelectorAll: (sel) => findAll(el, sel),
    };
    return el;
  }
  const document = {
    readyState: 'complete',
    body: makeEl('body'),
    createElement: makeEl,
    addEventListener() { }, removeEventListener() { },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    documentElement: {
      getAttribute: () => null, setAttribute() { }, style: { setProperty() { } },
      classList: { add() { }, remove() { }, contains: () => false },
    },
  };
  return { document, makeEl };
}

/** 把真实 base.js 装进 vm，取回 createSettingsForm（顶层函数声明即上下文的全局）。 */
function loadShell() {
  const { document, makeEl } = makeDom();
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
  return { createSettingsForm: vm.runInContext('createSettingsForm', sandbox), makeEl };
}

const { createSettingsForm, makeEl } = loadShell();

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

console.log(failed ? `\n${failed} 例失败` : '\n设置表单 5 例通过');
process.exit(failed ? 1 : 0);
