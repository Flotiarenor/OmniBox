// 共享运行时「扩展入口」渲染的历史实现已删除：壳侧的 `PluginExtensions.vue` 负责
// 扩展入口与"同一时刻只有一个当前项"的互斥。本文件是**死亡 API 守卫**：
// 一旦有人把第二套实现塞回 base.js、或插件/壳源码重新引用它，这里就红。
//
// 历史背景（为什么以前需要它）：高亮原先由每个宿主自己写，media-player 只写了单向、
// image-viewer 写了双向才没暴露，两条入口会同时亮。那套逻辑现在只保留壳侧一份。
//
// 用法：node tests/js/shell_extensions_active.mjs（由 test_shell_extensions_js.py 驱动）
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BASE_JS = path.join(ROOT, 'shell', 'frontend', 'public', 'shell', 'base.js');
const SHELL = fs.readFileSync(BASE_JS, 'utf8');

assert.ok(
  !/function\s+renderExtensions\b/.test(SHELL),
  'base.js 不应再定义 renderExtensions：扩展入口由壳的 PluginExtensions 承担',
);
assert.ok(
  !/window\.renderExtensions\s*=/.test(SHELL),
  'base.js 不得把 renderExtensions 再挂回 window（共享运行时不重复实现壳的组件）',
);

function* walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue;
      yield* walk(full);
    } else if (/\.(js|mjs|html|vue|ts)$/.test(entry.name)) {
      yield full;
    }
  }
}

const offenders = [];
for (const dir of [path.join(ROOT, 'plugins'), path.join(ROOT, 'shell', 'frontend', 'src')]) {
  if (!fs.existsSync(dir)) continue;
  for (const file of walk(dir)) {
    if (fs.readFileSync(file, 'utf8').includes('renderExtensions')) {
      offenders.push(path.relative(ROOT, file));
    }
  }
}
assert.deepEqual(
  offenders, [],
  `这些文件仍引用 renderExtensions（共享运行时已删除该入口）：${offenders.join(', ')}`,
);

console.log('  PASS  共享运行时不提供 renderExtensions，插件与壳源码均无引用');
process.exit(0);
