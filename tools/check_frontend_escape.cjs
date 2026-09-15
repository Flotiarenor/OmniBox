/**
 * 前端转义一致性检查：所有 HTML 转义实现都必须转义引号（& < > " '）。
 *
 * 为什么需要这个检查（docs/code-review.md §4.3）：
 *   同一个框架里曾经有 4 份 escapeHtml，其中 2 份只转义 &<>（textContent →
 *   innerHTML 的写法），却被塞进 data-folder="${...}" 这类属性里 —— 目录名里的
 *   一个引号就能逃出属性并注入标记。看代码很难发现"哪一份是属性安全的"，
 *   所以把要求变成可执行的检查：不管实现放在内核还是插件里，都必须过这一关。
 *
 * 用法：node tools/check_frontend_escape.cjs   （退出码 0 = 通过）
 * CI：见 .github/workflows/ci.yml 的"前端转义一致性"步骤。
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
// 同时含 " ' < > & 的载荷：任意一个没被转义，插进属性就能逃逸
const PAYLOAD = '\'"><img src=x onerror=alert(1)>&';
const AMP = '&amp;';

const failures = [];

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

/** 从源码里按大括号配对截出一个 `名字(...) { ... }` 方法块（用于类方法/对象方法）。 */
function extractBlock(source, needle) {
  const start = source.indexOf(needle);
  if (start < 0) return null;
  const open = source.indexOf('{', start + needle.length);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  return null;
}

/** 在当前 Node 里跑一段浏览器脚本，返回它挂到 globalThis 上的对象。 */
function runScript(source, exportsName) {
  const sandbox = makeSandbox({ Bridge: {} });
  vm.createContext(sandbox);
  vm.runInContext(`${source}\n;globalThis.__exports = ${exportsName};`, sandbox);
  return sandbox.__exports;
}

/**
 * 构造一个"像浏览器一点"的全局对象：window 就是全局对象本身，
 * 这样脚本里的 window.Utils = {...} 与裸标识符 Utils 指向同一个东西。
 */
/**
 * 极简 DOM 替身：让历史实现（textContent → innerHTML）也能跑起来，于是检查
 * 能给出"没转义引号"这种具体结论，而不是一句"抛错了"。行为与浏览器一致：
 * textContent → innerHTML 只实体化 & < >，不碰引号。
 */
function domStub() {
  return {
    createElement: () => ({
      _text: '',
      set textContent(value) {
        this._text = String(value == null ? '' : value);
      },
      get innerHTML() {
        return this._text
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
      },
    }),
  };
}

function makeSandbox(extra) {
  const sandbox = { console, document: domStub(), ...extra };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.Utils = loadKernelUtils();
  return sandbox;
}

/** 内核 window.Utils 对象字面量：直接在沙箱里跑真实的 base.js 片段。 */
let kernelUtils = null;
function loadKernelUtils() {
  if (kernelUtils) return kernelUtils;
  const source = read('shell/frontend/public/shell/base.js');
  const block = extractBlock(source, 'window.Utils =');
  if (!block) {
    failures.push('base.js: 解析不出 window.Utils 定义');
    kernelUtils = {};
    return kernelUtils;
  }
  const sandbox = { console, document: domStub() };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox);
  kernelUtils = sandbox.Utils;
  return kernelUtils;
}

/** 用对象方法块构造一个只含该方法的最小对象（类方法场景）。 */
function loadMethod(rel, needle, name) {
  const block = extractBlock(read(rel), needle);
  if (!block) {
    failures.push(`${rel}: 找不到 ${needle}`);
    return null;
  }
  const sandbox = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(`globalThis.__obj = ({ ${block} });`, sandbox);
  return sandbox.__obj[name];
}

function checkEscaper(label, fn) {
  if (typeof fn !== 'function') {
    failures.push(`${label}: 没取到函数`);
    return;
  }
  let escaped;
  try {
    escaped = fn(PAYLOAD);
  } catch (e) {
    failures.push(`${label}: 调用即抛错（实现可能依赖了浏览器环境）: ${e.message}`);
    return;
  }
  for (const ch of ['<', '>', '"', "'"]) {
    if (escaped.includes(ch)) {
      failures.push(`${label}: 转义结果仍含原始 ${ch} —— 放进属性就能逃逸：${escaped}`);
    }
  }
  if (!escaped.includes('&lt;') || !escaped.includes('&quot;') || !escaped.includes('&#39;')) {
    failures.push(`${label}: 没有产出 &lt; / &quot; / &#39; 实体：${escaped}`);
  }
  // & 必须最先转义，否则会把已有实体的 & 二次转义成 &amp;lt;
  if (fn(AMP) !== '&amp;amp;') {
    failures.push(`${label}: & 转义不正确（期望 &amp;amp;，实得 ${fn(AMP)}）`);
  }
  if (fn(null) !== '' || fn(undefined) !== '') {
    failures.push(`${label}: null/undefined 应当返回空字符串`);
  }
}

function checkAttributeContext(label, html) {
  // 攻击载荷若原样出现（未实体化），说明值已经逃出属性，可以插入新标记/标签
  if (html.includes('\'"><img') || html.includes('"><img')) {
    failures.push(`${label}: 恶意值逃出了属性：${html}`);
  }
  if (!html.includes('&quot;') || !html.includes('&lt;img')) {
    failures.push(`${label}: 值没有被实体化：${html}`);
  }
}

// ---- 1. 各处 HTML 转义实现 ----
checkEscaper('kernel Utils.escapeHtml', loadKernelUtils().escapeHtml);
checkEscaper('media-player MPUtils.escapeHtml',
  runScript(read('plugins/media-player/frontend/js/utils.js'), 'MPUtils').escapeHtml);
checkEscaper('manga-library MangaUtils.escapeHtml',
  runScript(read('plugins/manga-library/frontend/js/utils.js'), 'MangaUtils').escapeHtml);
checkEscaper('netease-music esc', loadMethod('plugins/netease-music/frontend/js/app.js', 'esc(str)', 'esc'));
checkEscaper('image-viewer _escapeHtml',
  loadMethod('plugins/image-viewer/frontend/js/app-utils.js', '_escapeHtml(str)', '_escapeHtml'));
checkEscaper('image-cleaner _escapeHtml',
  loadMethod('plugins/image-cleaner/frontend/js/app.js', '_escapeHtml(str)', '_escapeHtml'));

// ---- 2. 属性拼接场景：生成的 HTML 不能被打断 ----
const mpUtils = runScript(read('plugins/media-player/frontend/js/utils.js'), 'MPUtils');
checkAttributeContext('MPUtils.coverImg(src)',
  mpUtils.coverImg(PAYLOAD, PAYLOAD, '', PAYLOAD));
const mangaUtils = runScript(read('plugins/manga-library/frontend/js/utils.js'), 'MangaUtils');
checkAttributeContext('MangaUtils.coverImg(src)',
  mangaUtils.coverImg(PAYLOAD, PAYLOAD));

/** 取多个方法块拼成对象（方法之间可能互相调用，如 _escapeAttr → _escapeHtml）。 */
function loadObjectWith(rel, needles) {
  const source = read(rel);
  const blocks = needles.map((needle) => extractBlock(source, needle));
  if (blocks.some((b) => !b)) {
    failures.push(`${rel}: 找不到 ${needles.filter((n, i) => !blocks[i]).join(', ')}`);
    return null;
  }
  const sandbox = makeSandbox();
  vm.createContext(sandbox);
  vm.runInContext(`globalThis.__obj = ({ ${blocks.join(',')} });`, sandbox);
  return sandbox.__obj;
}

// ---- 3. image-viewer 的 _escapeAttr 必须与 _escapeHtml 同样严格 ----
// 两个方法在 app-utils.js 分片里（app.js 拆成多个原型分片后位置变更，见 docs/image-viewer-design.md）
const iv = loadObjectWith('plugins/image-viewer/frontend/js/app-utils.js', ['_escapeHtml(str)', '_escapeAttr(str)']);
if (iv) {
  const out = iv._escapeAttr(PAYLOAD);
  if (out.includes('"') || out.includes("'") || out.includes('<')) {
    failures.push(`image-viewer _escapeAttr: 结果仍含可逃逸字符：${out}`);
  }
}

// ---- 汇总 ----
if (failures.length) {
  console.error(`check_frontend_escape: 发现 ${failures.length} 处问题`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log('check_frontend_escape: OK（内核 + 5 个插件的转义实现均属性安全）');
