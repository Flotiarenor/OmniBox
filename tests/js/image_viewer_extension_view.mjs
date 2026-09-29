// image-viewer 扩展面板头部的纯逻辑用例（不经浏览器）：
//   - 主标题取扩展声明的 label，说明取扩展声明的 description（宿主不写插件专有文案）
//   - iframe 加 ?embed=1，面板去掉 hidden；换一个扩展时两行文字都要被覆盖
//   - 头部只有「标题行 + 插件挂载点」两个子项，不自带按钮（「返回相册」已去掉）
// 说明是头部两行结构里的第二行（`.iv-view-sub`）：缺了它扩展头只有一行标题，
// 比主工具栏矮一档；按钮的字号/内边距/高度/间隙是否与主工具栏一致，几何实测在
// tests/debug_extension_header_ui.py。
//
// 装载顺序取自 index.html（见 script_load_contract.mjs）：app.js 拆成分片后本用例
// 自动跟着走，不需要在这里维护文件名清单。
//
// 用法：node tests/js/image_viewer_extension_view.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { readDeclaredScripts } from './script_load_contract.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(here, '..', '..', 'plugins', 'image-viewer', 'frontend');

// 按声明顺序拼接插件自己的全部脚本；末尾有 `class ImageViewer {}`，
// 包一层导出以便在 node 里实例化。
const source = readDeclaredScripts(FRONTEND).map(s => s.source).join('\n;\n');
const ImageViewer = new Function(`${source}\nreturn ImageViewer;`)();

// openExtensionView 只碰这四个节点，给最小替身而不是造一个 DOM：
// 它不做 DOM 模拟，只记录 textContent / src / classList 的最终状态。
function makeViewer() {
  const nodes = {};
  const add = (id, extra = {}) => {
    const classes = new Set(['hidden']);
    nodes[id] = {
      id, textContent: '', ...extra,
      classList: {
        add: (c) => classes.add(c),
        remove: (c) => classes.delete(c),
        contains: (c) => classes.has(c),
      },
    };
  };
  add('extension-view');
  add('extension-frame', { src: 'about:blank' });
  add('extension-view-title');
  add('extension-view-sub');
  globalThis.document = { getElementById: (id) => nodes[id] || null };
  return { viewer: new ImageViewer(), nodes };
}

// ---------- 标题与说明都来自扩展声明 ----------

{
  const { viewer, nodes } = makeViewer();
  viewer.openExtensionView({
    label: '相册清理',
    description: '扫描全部相册中的重复 / 相似图片',
    embedUrl: '/plugins/image-cleaner/frontend/index.html',
  });
  assert.equal(nodes['extension-view-title'].textContent, '相册清理', '标题取 label');
  assert.equal(nodes['extension-view-sub'].textContent, '扫描全部相册中的重复 / 相似图片',
               '说明取 description：头部第二行不写宿主自造的文案');
  assert.equal(nodes['extension-frame'].src,
               '/plugins/image-cleaner/frontend/index.html?embed=1',
               '内嵌页要带 ?embed=1 才会收起自己的工具栏');
  assert.ok(!nodes['extension-view'].classList.contains('hidden'), '打开时去掉 hidden');
}

// ---------- 换成另一个扩展：两行文字都被覆盖，不残留上一个的 ----------

{
  const { viewer, nodes } = makeViewer();
  viewer.openExtensionView({
    label: '相册清理', description: '扫描全部相册中的重复 / 相似图片',
    embedUrl: '/plugins/image-cleaner/frontend/index.html',
  });
  viewer.openExtensionView({
    label: 'Pixiv 同步', description: '同步下载关注画师新作与收藏画作',
    embedUrl: '/plugins/pixiv-sync/frontend/index.html',
  });
  assert.equal(nodes['extension-view-title'].textContent, 'Pixiv 同步');
  assert.equal(nodes['extension-view-sub'].textContent, '同步下载关注画师新作与收藏画作');
  assert.equal(nodes['extension-frame'].src, '/plugins/pixiv-sync/frontend/index.html?embed=1');
}

// ---------- 边界：没有 description / 空扩展对象 ----------

{
  const { viewer, nodes } = makeViewer();
  viewer.openExtensionView({ label: '没声明说明的扩展', embedUrl: '/plugins/x/frontend/index.html' });
  assert.equal(nodes['extension-view-sub'].textContent, '', '缺 description 时留空，不显示 undefined');
}

{
  const { viewer, nodes } = makeViewer();
  viewer.openExtensionView({});
  assert.equal(nodes['extension-view-title'].textContent, '扩展', '没有 label 时退回占位标题');
}

// ---------- 头部结构：宿主不自带按钮，挂载点就是右端 ----------

{
  // 头部按字符切片：index.html 里 `extension-view-header` 到 `extension-view-body` 之间
  // 就是整条头部，够用来锁"头部里没有宿主自己的按钮"这条约定
  const html = readFileSync(join(FRONTEND, 'index.html'), 'utf8');
  const from = html.indexOf('extension-view-header');
  const to = html.indexOf('extension-view-body');
  assert.ok(from > 0 && to > from, 'index.html 里应当有扩展面板头部与 body');
  const header = html.slice(from, to);
  assert.ok(!/<button/.test(header), '头部不自带按钮：操作全部由内嵌插件挂上来');
  assert.match(header, /id="extension-view-actions"/, '头部保留插件按钮的挂载点');
  assert.match(header, /extension-view-heading/, '头部保留标题行（两行结构）');
}

console.log('image_viewer_extension_view.mjs: OK');
