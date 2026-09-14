// image-viewer「图片文件夹」列表的纯逻辑用例（不经浏览器）。
//
// 用户反馈的两点在这里固化：
//   - 主目录行也要有 ✕，且宽度/高度与「额外」行一致（以前主行没有删除按钮，
//     行更矮、标签只有一个字）；
//   - 删除主行后，下一行自动成为主目录，列表清空时提示会回退到默认数据目录。
//
// 用法：node tests/js/image_viewer_roots_list.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', '..', 'plugins', 'image-viewer',
                                'frontend', 'js', 'app.js'), 'utf8');
const ImageViewer = new Function(`${source}\nreturn ImageViewer;`)();

// 极简 DOM 替身：只实现 _renderRoots 用到的 innerHTML / querySelectorAll
function fakeBox() {
  return {
    _html: '',
    _buttons: [],
    set innerHTML(html) {
      this._html = html;
      const count = (html.match(/iv-root-remove/g) || []).length;
      this._buttons = Array.from({ length: count }, (_, i) => ({
        dataset: { index: String(i) },
        addEventListener: (event, handler) => { this._handlers = this._handlers || []; this._handlers.push([event, handler]); },
        click: () => (this._handlers || []).forEach(([e, h]) => e === 'click' && h()),
      }));
    },
    get innerHTML() { return this._html; },
    querySelectorAll() { return this._buttons; },
  };
}

function viewerWith(roots, box) {
  // app.js 的转义走内核 window.Utils.escapeHtml，这里给个等价实现
  global.window = {
    Utils: {
      escapeHtml: (str) => String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;'),
    },
  };
  global.document = { getElementById: () => box };
  const viewer = new ImageViewer();
  viewer.roots = roots;
  return viewer;
}

const box = fakeBox();
const viewer = viewerWith([{ path: 'D:\\主图库' }, { path: 'E:\\额外图库' }], box);
viewer._renderRoots();

assert.equal((box.innerHTML.match(/iv-root-remove/g) || []).length, 2,
             '主目录行和额外目录行都要有 ✕（行高才能一致）');
assert.equal((box.innerHTML.match(/is-primary/g) || []).length, 1,
             '只有第一行是主目录');
assert.ok(box.innerHTML.includes('>主要<'), '主目录标签是「主要」而不是单个「主」');
assert.ok(box.innerHTML.includes('>额外<'), '其余行标签是「额外」');
assert.ok(box.innerHTML.indexOf('主要') < box.innerHTML.indexOf('额外'),
          '主目录在最前');

// 删掉主行：下一行顶上成为主目录，而不是留下一个没有主目录的列表
box._buttons[0].click();
assert.equal(viewer.roots.length, 1);
assert.equal(viewer.roots[0].path, 'E:\\额外图库');
assert.ok(box.innerHTML.includes('>主要<') && !box.innerHTML.includes('>额外<'),
          '删掉主行后，剩下的第一行成为主目录');

// 再删掉最后一行：列表为空并提示回退默认目录
box._buttons[0].click();
assert.equal(viewer.roots.length, 0);
assert.ok(box.innerHTML.includes('默认数据目录'), '清空列表时提示回退到默认数据目录');

const oneBox = fakeBox();
global.document = { getElementById: () => oneBox };
const one = viewerWith([{ path: 'D:\\唯一' }], oneBox);
one._renderRoots();
assert.equal((oneBox.innerHTML.match(/iv-root-remove/g) || []).length, 1,
             '只剩一行时它既是主目录也带 ✕');

console.log('image_viewer_roots_list: OK');
