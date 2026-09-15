// image-viewer「图片文件夹」列表的回归用例（不经浏览器）。
//
// 列表实现已经搬到 Shell 共享组件（shell/frontend/public/shell/folder-picker.js），
// image-viewer 只负责把列表里的路径在保存时写回 root_dir / extra_roots —— 组件
// 本身的行为在 tests/js/shell_folder_picker.mjs 里守。
//
// 这里守的是**没有第二份实现**：插件不再自带 .iv-root-* 的渲染、目录选择器与
// 去重逻辑。以前这套东西长在 app.js 里，媒体播放器 / 漫画 / 小说又各需要一份，
// 结果就是"同一件事三处不一样"；谁把这个实现抄回插件，这个用例就会红。
//
// 源码断言扫的是**插件前端的全部脚本**（按 index.html 的顺序拼接），不是单个 app.js：
// 这样 app.js 拆成分片后本用例自动跟着走，而且"换个文件把实现抄回来"同样会被抓住。
//
// 用法：node tests/js/image_viewer_roots_list.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { readDeclaredScripts } from './script_load_contract.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(here, '..', '..', 'plugins', 'image-viewer', 'frontend');
const htmlPath = join(FRONTEND, 'index.html');
const cssPath = join(FRONTEND, 'image-viewer.css');

const pluginJs = readDeclaredScripts(FRONTEND).map(s => s.source).join('\n;\n');
const html = readFileSync(htmlPath, 'utf8');
const css = readFileSync(cssPath, 'utf8');

// 1) 渲染与目录选择器不再由插件实现
for (const gone of ['_renderRoots', '_addRootFromInput', '_loadDirBrowser', 'openDirBrowser']) {
  assert.ok(!new RegExp(`\\b${gone}\\s*\\(`).test(pluginJs),
            `插件前端不应再自带 ${gone}（已搬到 shell/frontend/public/shell/folder-picker.js）`);
}
assert.ok(!pluginJs.includes('iv-root-remove'),
          '插件前端不应再自己拼 .iv-root-row 的 HTML');
assert.ok(!pluginJs.includes('browse_dir'),
          '目录浏览改走宿主接口 system_browse_dir（共享组件内部调用）');

// 2) 插件改成引用共享组件
assert.ok(pluginJs.includes('window.FolderPicker.createList('),
          '插件前端应通过 window.FolderPicker.createList 建立列表');
assert.ok(pluginJs.includes('_rootPaths()') && pluginJs.includes('rootsPicker.getPaths()'),
          '保存时应从共享组件的 paths 取全部路径');

// 3) 列表容器留空给组件填充，旧的静态输入行与选择器弹窗都已移除
assert.ok(/id="setting-roots"><\/div>/.test(html),
          'HTML 里 #setting-roots 应是空容器（由组件填充）');
for (const gone of ['setting-new-root', 'setting-browse-root', 'setting-add-root', 'dir-browser-modal']) {
  assert.ok(!html.includes(gone), `HTML 里不应再有 ${gone}`);
}

// 4) 样式只有一份：.iv-root-* / .iv-dirbrowser-* 已移出插件 CSS
for (const cls of ['.iv-root-row', '.iv-root-remove', '.iv-roots-add', '.iv-dirbrowser-list']) {
  assert.ok(!css.includes(cls), `image-viewer.css 不应再保留 ${cls}（见 folder-picker.css）`);
}

console.log('image_viewer_roots_list: OK');
