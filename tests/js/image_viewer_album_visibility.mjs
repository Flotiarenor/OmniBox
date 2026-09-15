// image-viewer 相册可见性的纯逻辑用例（不经浏览器）：
//   - 子相册默认折叠：只有显式「展开」过的目录才显示下级
//   - 递归都没有可读图片的目录不显示，但「新建相册」保留可见的（含其上级）要显示
// 直接复用插件前端的真实实现，避免用例与实现各写一套判断。
//
// 装载顺序取自 index.html（见 script_load_contract.mjs）：app.js 拆成分片后本用例
// 自动跟着走，不需要在这里维护文件名清单。
//
// 用法：node tests/js/image_viewer_album_visibility.mjs
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { readDeclaredScripts } from './script_load_contract.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(here, '..', '..', 'plugins', 'image-viewer', 'frontend');

// 按声明顺序拼接插件自己的全部脚本；末尾有 `class ImageViewer {}`，
// 包一层导出以便在 node 里实例化。
const source = readDeclaredScripts(FRONTEND).map(s => s.source).join('\n;\n');
const ImageViewer = new Function(`${source}\nreturn ImageViewer;`)();

function makeViewer(albumConfig = {}) {
  const viewer = new ImageViewer();
  viewer.albumConfig = {
    collapsed: [], promoted: [], expanded: [], visible_empty_dirs: [],
    ...albumConfig,
  };
  return viewer;
}

function album(path, opts = {}) {
  const parts = path.split('/');
  return {
    path,
    name: parts[parts.length - 1],
    depth: opts.depth !== undefined ? opts.depth : parts.length,
    has_children: !!opts.has_children,
    direct_count: opts.direct_count || 0,
    readable: opts.readable !== undefined ? opts.readable : 0,
    image_count: opts.readable !== undefined ? opts.readable : 0,
  };
}

// ---------- 默认折叠 ----------

{
  const albums = [
    album('', { depth: 0, readable: 5 }),
    album('分类', { depth: 1, has_children: true, readable: 5 }),
    album('分类/作者', { depth: 2, has_children: true, readable: 5 }),
    album('分类/作者/作品', { depth: 3, readable: 5 }),
    album('单图相册', { depth: 1, readable: 2 }),
  ];
  const viewer = makeViewer();
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path),
                   ['分类', '单图相册'],
                   '默认折叠：只显示顶层，下级被收纳');
  assert.ok(viewer._isCollapsed(albums[1]), '含子目录的目录默认折叠');
  assert.ok(!viewer._isCollapsed(albums[4]), '无子目录的目录不参与折叠');
}

{
  const albums = [
    album('分类', { depth: 1, has_children: true, readable: 5 }),
    album('分类/作者', { depth: 2, has_children: true, readable: 5 }),
    album('分类/作者/作品', { depth: 3, readable: 5 }),
  ];
  const viewer = makeViewer({ expanded: ['分类'] });
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path),
                   ['分类', '分类/作者'],
                   '显式展开一层后显示它的直接下级，孙级仍折叠');
}

{
  const albums = [
    album('分类', { depth: 1, has_children: true, readable: 5 }),
    album('分类/作者', { depth: 2, has_children: true, readable: 5 }),
  ];
  const viewer = makeViewer({ expanded: ['分类'], collapsed: ['分类'] });
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path), ['分类'],
                   'collapsed 优先于 expanded（「收纳」覆盖旧配置）');
}

// ---------- 空目录隐藏 ----------

{
  const albums = [
    album('有图', { depth: 1, readable: 3 }),
    album('空目录', { depth: 1, readable: 0 }),
    album('空容器', { depth: 1, has_children: true, readable: 0 }),
    album('空容器/深一层', { depth: 2, readable: 0 }),
    album('混合容器', { depth: 1, has_children: true, readable: 2 }),
    album('混合容器/有图', { depth: 2, readable: 2 }),
  ];
  const viewer = makeViewer({ expanded: ['空容器', '混合容器'] });
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path),
                   ['有图', '混合容器', '混合容器/有图'],
                   '递归都没有可读图片的目录整棵隐藏');
}

{
  const albums = [
    album('新相册', { depth: 1, readable: 0 }),
    album('新相册/子目录', { depth: 2, readable: 0 }),
  ];
  const viewer = makeViewer({ visible_empty_dirs: ['新相册', '新相册/子目录'] });
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path),
                   ['新相册', '新相册/子目录'],
                   '新建的空相册（含其上级标记）保留可见');
}

{
  const albums = [
    album('新相册', { depth: 1, readable: 0 }),
    album('新相册/子目录', { depth: 2, readable: 0 }),
  ];
  // 只标记了子目录：上级也要跟着显示，否则点不进去
  const viewer = makeViewer({ visible_empty_dirs: ['新相册/子目录'] });
  assert.deepEqual(viewer._filterVisibleAlbums(albums).map(a => a.path),
                   ['新相册', '新相册/子目录'],
                   '只标记深层空目录时，其上级同样保留可见');
}

// ---------- 提升 ----------

{
  const albums = [
    album('分类', { depth: 1, has_children: true, readable: 1 }),
    album('分类/作者', { depth: 2, has_children: true, readable: 1 }),
    album('分类/作者/作品', { depth: 3, readable: 1 }),
  ];
  const viewer = makeViewer({ promoted: ['分类/作者/作品'] });
  const visible = viewer._filterVisibleAlbums(albums).map(a => a.path);
  assert.ok(visible.includes('分类/作者/作品'), '提升的相册即使祖先折叠也显示');
  assert.ok(!visible.includes('分类/作者'), '未提升的中间层仍被折叠隐藏');
}

// ---------- 根目录 ----------

{
  const albums = [album('', { depth: 0, direct_count: 0, readable: 4 })];
  const viewer = makeViewer();
  assert.deepEqual(viewer._filterVisibleAlbums(albums), [], '纯容器根目录不显示');
}

console.log('image_viewer_album_visibility: OK');
