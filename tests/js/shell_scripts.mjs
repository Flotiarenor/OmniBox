// Shell 共享运行时（注入插件页的 /shell/*.js）的装载入口。
//
// 为什么需要：共享运行时按区段拆成多个文件（base / dialogs / settings / host-channel /
// tree / lightbox / pagination / context-menu），它们在**同一个全局作用域**里按注入顺序
// 依次执行 —— 用例要在 vm 里复现真实装载才有意义，而"注入了哪些文件、什么顺序"只有
// `shell/backend/file_server.py` 的引导片段一处定义（tests/js/script_load_contract.mjs
// 也按同一处取清单）。
//
// 在用例里再抄一份文件清单的代价是：改注入清单后用例静默地只装载一半脚本，表现为
// "某个方法 undefined"而不是"清单过期"。这里从引导片段里推出来，拆分加段不用改用例。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const SHELL_DIR = path.join(REPO_ROOT, 'shell', 'frontend', 'public', 'shell');
const FILE_SERVER_PY = path.join(REPO_ROOT, 'shell', 'backend', 'file_server.py');

/** 引导片段里按注入顺序出现的 /shell/*.js 文件名（去重后）。 */
export function shellScriptOrder() {
  const bootstrap = fs.readFileSync(FILE_SERVER_PY, 'utf8');
  return [...new Set(
    [...bootstrap.matchAll(/src="\/shell\/([\w.-]+\.js)"/g)].map((match) => match[1]),
  )];
}

/**
 * 共享运行时这一层在引导片段里的范围：toast.js 之后、folder-picker.js 之前。
 *
 * 范围由引导片段的位置推出来，因此新增一层分区、或调整顺序都不用改这里；反过来，
 * 把这批文件挪出这段区间会让用例立刻报"找不到"。
 */
export function shellRuntimeOrder() {
  const order = shellScriptOrder();
  const start = order.indexOf('toast.js');
  const end = order.indexOf('folder-picker.js');
  if (start < 0 || end < 0 || end <= start) {
    throw new Error('引导片段里找不到 toast.js → folder-picker.js 这段共享运行时：'
      + `实际顺序 ${JSON.stringify(order)}`);
  }
  return order.slice(start + 1, end);
}

/** 读取一份被注入的共享脚本源码；名字不在引导片段里就立刻失败（改名/漏注入都会响）。 */
export function shellScriptSource(name) {
  if (!shellScriptOrder().includes(name)) {
    throw new Error(`引导片段里没有 /shell/${name}：注入清单改了但用例没跟上？`);
  }
  return fs.readFileSync(path.join(SHELL_DIR, name), 'utf8');
}

/**
 * 把指定的共享脚本按**引导片段的顺序**跑进一个已 context 化的 vm 沙箱。
 *
 * 用例只需要一两个分段时用它（例如只驱动内容页握手时需要 `chrome-client.js`）：
 * 顺序仍以引导片段为准，且名字必须出现在引导片段里 —— 改名或漏注入会立刻失败。
 *
 * @param {object} sandbox `vm.createContext(...)` 得到的上下文对象
 * @param {string[]} names 需要的文件名
 * @returns {string[]} 实际装载顺序
 */
export function loadShellScripts(sandbox, names) {
  const order = shellScriptOrder();
  const missing = names.filter((name) => !order.includes(name));
  if (missing.length) {
    throw new Error(`引导片段里没有这些脚本: ${missing.join(', ')}（注入清单改了？）`);
  }
  const chosen = order.filter((name) => names.includes(name));
  for (const name of chosen) {
    vm.runInContext(shellScriptSource(name), sandbox, { filename: `shell/${name}` });
  }
  return chosen;
}

/**
 * 把共享运行时整层按注入顺序跑进一个已 context 化的 vm 沙箱。
 *
 * 等价于拆分前的"把 base.js 整个装进去"：同一批代码、同一个顺序。
 *
 * @param {object} sandbox `vm.createContext(...)` 得到的上下文对象
 * @returns {string[]} 实际装载的文件名（顺序即注入顺序）
 */
export function loadShellRuntime(sandbox) {
  const names = shellRuntimeOrder();
  for (const name of names) {
    vm.runInContext(shellScriptSource(name), sandbox, { filename: `shell/${name}` });
  }
  return names;
}
