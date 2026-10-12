// 壳偏好纯数据层（shell/frontend/src/core/prefs-model.ts）的契约用例。
//
// 为什么用 Node 直接跑 .ts：这一层是"前端归一化规则"的唯一来源，而它必须与后端
// `shell/backend/shell_prefs.py` 的判据一致 —— 两边各写一遍、谁也不跑对方的用例，
// 迟早会出现"前端认为合法、后端整条拒收"，用户看到的现象是"改了没反应"。
// 这里因此同时做两件事：
//
//   1. 用真实源码跑前端规则（`node --experimental-strip-types` 只做类型擦除，
//      所以 prefs-model.ts 刻意不 import 任何模块）；
//   2. 读后端源码里的正则与区间常量，与前端逐条比对（`--re` 与 GLASS_RANGES）。
//
// 用法：node --experimental-strip-types tests/js/shell_prefs_model.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const MODEL = path.join(ROOT, 'shell', 'frontend', 'src', 'core', 'prefs-model.ts');
const VAR_RULES = path.join(ROOT, 'shell', 'frontend', 'src', 'core', 'var-rules.ts');
const BACKEND = path.join(ROOT, 'shell', 'backend', 'shell_prefs.py');

// Windows 上必须转成 file:// URL：默认 ESM 加载器不认 `d:\...` 这种盘符路径
const model = await import(pathToFileURL(MODEL).href);
const rules = await import(pathToFileURL(VAR_RULES).href);

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

// ---------------------------------------------------------------- 归一化

check('默认值是深色 + 上次离开的页面', () => {
  const prefs = model.normalizePrefs(undefined, model.DEFAULT_PREFS);
  assert.equal(prefs.theme, 'dark');
  assert.equal(prefs.startup.page, 'last');
  assert.equal(prefs.startup.fullscreen, false);
  assert.equal(prefs.last_route, '');
  assert.deepEqual(prefs.colors, {});
});

check('注入状态缺字段时逐项回退，不让整份作废', () => {
  const prefs = model.normalizePrefs({
    theme: '紫色',                                    // 非法取值
    colors: { '--accent': '#8b5cf6', '--x': 5 },      // 第二个不是字符串
    startup: { page: 'plugin', route: '/media-player', fullscreen: 'yes' },
    background: { fit: 'stretch', zoom: '200' },
  }, model.DEFAULT_PREFS);
  assert.equal(prefs.theme, 'dark');
  assert.deepEqual(prefs.colors, { '--accent': '#8b5cf6' });
  assert.equal(prefs.startup.page, 'plugin');
  assert.equal(prefs.startup.route, '/media-player');
  assert.equal(prefs.startup.fullscreen, false, '非布尔不按真值处理');
  assert.equal(prefs.background.fit, 'cover', '非法显示方式回退默认');
  assert.equal(prefs.background.zoom, 200, '字符串数字收敛成数字');
});

check('归一化结果不共享默认值的嵌套对象', () => {
  const first = model.normalizePrefs(undefined, model.DEFAULT_PREFS);
  first.colors['--accent'] = '#fff';
  const second = model.normalizePrefs(undefined, model.DEFAULT_PREFS);
  assert.deepEqual(second.colors, {}, '两次归一化不该共用同一张变量表');
});

// ---------------------------------------------------------------- 补丁合并

check('补丁合并时 colors 按键合并而不是整段覆盖', () => {
  const merged = model.mergePatch(
    { colors: { '--radius': '10px' } },
    { colors: { '--nav-width': '240px' } });
  assert.deepEqual(merged.colors, { '--radius': '10px', '--nav-width': '240px' });
});

check('补丁合并保留互不相关的键', () => {
  const merged = model.mergePatch({ theme: 'light' }, { last_route: '/settings' });
  assert.deepEqual(merged, { theme: 'light', last_route: '/settings' });
});

// ---------------------------------------------------------------- 玻璃取值

check('玻璃变量收敛到区间内的整数', () => {
  assert.equal(model.clampGlassValue('--glass-blur', '99px'), '40px');
  assert.equal(model.clampGlassValue('--glass-blur', '-5px'), '0px');
  assert.equal(model.clampGlassValue('--glass-alpha', '10%'), '30%');
  assert.equal(model.clampGlassValue('--glass-alpha', '84.6%'), '85%');
});

check('非玻璃变量与畸形取值原样保留（由名字/取值校验去拦）', () => {
  assert.equal(model.clampGlassValue('--nav-width', '200px'), '200px');
  assert.equal(model.clampGlassValue('--glass-blur', 'auto'), 'auto');
});

// ---------------------------------------------------------------- 与后端一致

const backend = fs.readFileSync(BACKEND, 'utf8');

check('变量名正则与后端逐字符一致', () => {
  // 后端：_VAR_NAME_RE = re.compile(r'^--[a-z0-9-]{1,40}$')
  assert.match(backend, /_VAR_NAME_RE = re\.compile\(r'\^--\[a-z0-9-\]\{1,40\}\$'\)/);
  const frontend = fs.readFileSync(VAR_RULES, 'utf8');
  assert.match(frontend, /VAR_NAME_RE = \/\^--\[a-z0-9-\]\{1,40\}\$\//);
});

check('取值长度上限一致', () => {
  const limit = /_VAR_VALUE_MAX = (\d+)/.exec(backend);
  assert.ok(limit, '后端没有 _VAR_VALUE_MAX');
  assert.equal(rules.MAX_VALUE_LEN, Number(limit[1]));
});

check('玻璃区间与后端一致', () => {
  const ranges = /_GLASS_VAR_RANGES = \{([^}]*)\}/.exec(backend);
  assert.ok(ranges, '后端没有 _GLASS_VAR_RANGES');
  const blur = /'--glass-blur': \((\d+), (\d+)\)/.exec(ranges[1]);
  const alpha = /'--glass-alpha': \((\d+), (\d+)\)/.exec(ranges[1]);
  assert.ok(blur && alpha, `后端玻璃区间解析失败: ${ranges[1]}`);
  assert.equal(model.GLASS_LIMITS.blurMin, Number(blur[1]));
  assert.equal(model.GLASS_LIMITS.blurMax, Number(blur[2]));
  assert.equal(model.GLASS_LIMITS.alphaMin, Number(alpha[1]));
  assert.equal(model.GLASS_LIMITS.alphaMax, Number(alpha[2]));
});

check('显示方式与启动页取值与后端一致', () => {
  const fits = /FITS = \(([^)]*)\)/.exec(backend);
  const pages = /START_PAGES = \(([^)]*)\)/.exec(backend);
  assert.ok(fits && pages, '后端缺少 FITS / START_PAGES');
  const names = text => [...text.matchAll(/'([a-z]+)'/g)].map(m => m[1]);
  assert.deepEqual([...model.BACKGROUND_FIT_VALUES].sort(), names(fits[1]).sort());
  assert.deepEqual([...model.START_PAGE_VALUES].sort(), names(pages[1]).sort());
});

check('后端也认后端自己的默认偏好键集', () => {
  for (const key of ['theme', 'colors', 'background', 'startup', 'last_route']) {
    assert.ok(backend.includes(`'${key}'`), `后端默认偏好缺少 ${key}`);
    assert.ok(key in model.DEFAULT_PREFS, `前端默认偏好缺少 ${key}`);
  }
});

console.log(failed ? `\nshell_prefs_model: ${failed} 项失败` : '\nshell_prefs_model: 全部通过');
process.exit(failed ? 1 : 0);
