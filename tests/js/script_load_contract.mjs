// 插件前端「脚本装载契约」的共享检查器（各插件各有一个薄配置入口）。
//
// 为什么需要它：插件前端没有构建步骤，index.html 里的 <script src> 顺序就是唯一的依赖
// 声明；而 tools/check_plugins.py 的 _check_frontend_assets 只校验"文件存在且不越界"，
// 既不校验顺序，也不校验磁盘上的某个脚本是否真的被引用。于是下面三类改动在门禁里完全
// 看不见，只会在运行时表现成 `X is not defined` 或某个方法凭空消失：
//
//   1. 新增的分片没加进 index.html（分片里的方法全部不存在）；
//   2. 分片顺序错（装载期 ReferenceError）；
//   3. 方法搬走后原文件里的副本没删（静默覆盖，两个实现同时存在）。
//
// 它同时是"把上千行的 app.js 按分节注释拆成多个分片"这件事的护栏：拆分前后都应通过，
// 而且**新增分片只要出现在 index.html 里就会被自动纳入检查**，不需要改这个文件。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

/**
 * 按 index.html 的声明顺序返回插件前端的本地脚本（相对 frontend 的路径）。
 *
 * "这个插件的前端有哪些脚本、按什么顺序"只在这里定义一次：装载契约检查器与各插件的
 * 逻辑用例（在 vm 里跑真实实现）都用它，避免各处各写一份 index.html 解析。
 */
export function declaredScripts(frontendDir) {
    const html = fs.readFileSync(path.join(frontendDir, 'index.html'), 'utf8');
    const declared = [];
    for (const match of html.matchAll(/<script[^>]*\ssrc="([^"]+)"/g)) {
        const src = match[1];
        // 服务端注入的 /shell/*.js、远程脚本、data: 都不在插件自己的文件里
        if (/^(?:\/\/|https?:|\/|data:|#)/.test(src)) continue;
        declared.push(src.split('?')[0].split('#')[0]);
    }
    return declared;
}

/** 同上，但返回 `{ rel, source }`，供用例直接装载或做源码断言。 */
export function readDeclaredScripts(frontendDir) {
    return declaredScripts(frontendDir).map(rel => ({
        rel,
        source: fs.readFileSync(path.join(frontendDir, rel), 'utf8'),
    }));
}

/** 与浏览器像一点的全局替身：只保证"脚本能装载"，不模拟真实交互。 */
function makeSandbox() {
    const noop = () => { };
    const element = () => ({
        style: { setProperty: noop, removeProperty: noop },
        classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
        dataset: {}, children: [], childNodes: [], files: [],
        setAttribute: noop, removeAttribute: noop, getAttribute: () => null,
        appendChild: noop, removeChild: noop, remove: noop, insertBefore: noop,
        addEventListener: noop, removeEventListener: noop, dispatchEvent: noop,
        querySelector: () => null, querySelectorAll: () => [],
        getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }),
        focus: noop, blur: noop, click: noop, scrollIntoView: noop, animate: () => ({ cancel: noop }),
    });
    const documentStub = {
        getElementById: () => null,
        querySelector: () => null,
        querySelectorAll: () => [],
        createElement: element,
        createDocumentFragment: element,
        addEventListener: noop,
        removeEventListener: noop,
        documentElement: element(),
        body: element(),
        head: element(),
        hidden: false,
        visibilityState: 'visible',
    };
    const sandbox = {
        window: {},                     // `window.X = ...` 的写入落点
        document: documentStub,
        localStorage: { getItem: () => null, setItem: noop, removeItem: noop },
        sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
        Bridge: {
            call: () => Promise.resolve({}),
            callPlugin: () => Promise.resolve({}),
            originalUrl: p => `/file?path=${encodeURIComponent(p)}`,
            thumbUrl: id => `/thumbs/${id}`,
        },
        Utils: {},
        Toast: { error: noop, info: noop, success: noop },
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        requestAnimationFrame: () => 0, cancelAnimationFrame: noop,
        MutationObserver: class { observe() { } disconnect() { } takeRecords() { return []; } },
        IntersectionObserver: class { observe() { } unobserve() { } disconnect() { } },
        ResizeObserver: class { observe() { } unobserve() { } disconnect() { } },
        Audio: function () {
            return { addEventListener: noop, removeEventListener: noop, load: noop,
                     play: () => Promise.resolve(), pause: noop };
        },
        URL, URLSearchParams, Number, Math, JSON, String, Boolean, Object, Array, Promise,
        Map, Set, WeakMap, WeakSet, Date, RegExp, Error, TypeError, Symbol, Proxy, Reflect,
        isFinite, isNaN, NaN, Infinity, undefined,
        parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
    };
    return sandbox;
}

/**
 * 「资源装载契约」：不假定入口是类，只检查 index.html 与 js/ 的一致性 + 装载期错误。
 *
 * 为什么需要它：插件的分片数量不一样（有的只有 1 个脚本、有的是 8~12 个），
 * 而"磁盘上有脚本没被 index.html 引用"或"引用了不存在的脚本"在任何插件上
 * 都是静默故障（方法凭空消失 / 装载期 ReferenceError）。类成员契约由各插件自己的
 * `runScriptLoadContract` 入口覆盖（需要入口类名），这里覆盖**全部**插件前端。
 *
 * @returns {number} 失败项数（0 = 全部通过）
 */
export function runAssetContract({ label, frontendDir }) {
    let failures = 0;
    const check = (name, ok, extra = '') => {
        if (ok) {
            console.log(`  PASS  ${name}`);
            return;
        }
        failures += 1;
        console.log(`  FAIL  ${name}${extra ? `  — ${extra}` : ''}`);
    };

    console.log(`[${label}] 前端资源装载契约`);
    const declared = declaredScripts(frontendDir);
    const jsDir = path.join(frontendDir, 'js');
    const onDisk = fs.existsSync(jsDir)
        ? fs.readdirSync(jsDir).filter(f => f.endsWith('.js')).map(f => `js/${f}`)
        : [];
    // 像 pixiv-sync 这样"没有自己的脚本、由服务端注入 /shell/*.js"的前端是合法的；
    // 只要磁盘上有 js/*.js，就必须至少被引用了（否则下面的孤立脚本检查会失败）。
    check('index.html 的本地脚本声明与 js/ 一致',
        declared.length > 0 || onDisk.length === 0,
        `declared=${JSON.stringify(declared)} onDisk=${JSON.stringify(onDisk)}`);

    const missing = declared.filter(rel => !fs.existsSync(path.join(frontendDir, rel)));
    check('声明的脚本文件都存在', missing.length === 0, `缺失: ${JSON.stringify(missing)}`);

    const orphans = onDisk.filter(rel => !declared.includes(rel));
    check('js/ 下没有未被 index.html 引用的孤立脚本', orphans.length === 0,
        `孤立: ${JSON.stringify(orphans)}（新分片必须加进 index.html）`);

    const ctx = vm.createContext(makeSandbox());
    const errors = [];
    for (const rel of declared) {
        try {
            vm.runInContext(fs.readFileSync(path.join(frontendDir, rel), 'utf8'), ctx, { filename: rel });
        } catch (err) {
            errors.push(`${rel}: ${err && err.message ? err.message : err}`);
            break;
        }
    }
    check('全部本地脚本按声明顺序装载成功', errors.length === 0, errors.join('; '));
    console.log(`  共 ${declared.length} 个脚本`);
    return failures;
}

/**
 * 按 index.html 的声明顺序装载插件前端的全部本地脚本，并检查装载契约。
 *
 * @param {object} options
 * @param {string} options.label          用于输出的插件名
 * @param {string} options.frontendDir    frontend 目录（含 index.html 与 js/）
 * @param {string} options.className      入口类名（挂在 context 词法环境里）
 * @param {string[]} options.requiredMethods  必须仍存在于原型上的成员（子集断言）
 * @returns {number} 失败项数（0 = 全部通过）
 */
export function runScriptLoadContract({ label, frontendDir, className, requiredMethods }) {
    const FRONTEND = frontendDir;
    const JS_DIR = path.join(FRONTEND, 'js');
    const INDEX = path.join(FRONTEND, 'index.html');
    let failures = 0;

    const check = (name, ok, extra = '') => {
        if (ok) {
            console.log(`  PASS  ${name}`);
            return;
        }
        failures += 1;
        console.log(`  FAIL  ${name}${extra ? `  — ${extra}` : ''}`);
    };

    console.log(`[${label}] 脚本装载契约`);

    // 1. index.html 声明了哪些本地脚本（顺序即依赖顺序）
    const declared = declaredScripts(FRONTEND);
    check('index.html 声明了本地脚本', declared.length > 0, `declared=${JSON.stringify(declared)}`);

    const missingFiles = declared.filter(rel => !fs.existsSync(path.join(FRONTEND, rel)));
    check('声明的脚本文件都存在', missingFiles.length === 0, `缺失: ${JSON.stringify(missingFiles)}`);

    // js/ 下的每个脚本都必须被 index.html 引用 —— 新分片忘了挂上就会在这里失败
    const onDisk = fs.readdirSync(JS_DIR).filter(f => f.endsWith('.js')).map(f => `js/${f}`);
    const orphans = onDisk.filter(rel => !declared.includes(rel));
    check('js/ 下没有未被 index.html 引用的孤立脚本', orphans.length === 0,
        `孤立: ${JSON.stringify(orphans)}（新分片必须加进 index.html）`);

    // 2. 按声明顺序装载，并逐脚本比对原型成员值以发现"重复定义/静默覆盖"
    const ctx = vm.createContext(makeSandbox());
    const snapshot = () => {
        try {
            return vm.runInContext(
                `Object.fromEntries(Object.getOwnPropertyNames(${className}.prototype)`
                + `.map(n => [n, ${className}.prototype[n]]))`, ctx);
        } catch {
            return {};          // 入口类还没装载
        }
    };
    const loaded = [];
    const owner = new Map();
    const overrides = [];
    let loadError = null;
    for (const rel of declared) {
        const source = fs.readFileSync(path.join(FRONTEND, rel), 'utf8');
        const before = snapshot();
        try {
            vm.runInContext(source, ctx, { filename: rel });
        } catch (err) {
            loadError = `${rel}: ${err && err.message ? err.message : err}`;
            break;
        }
        loaded.push(rel);
        const after = snapshot();
        for (const [name, value] of Object.entries(after)) {
            if (name === 'constructor') continue;   // 每个类都有自己的 constructor
            if (!(name in before)) {
                owner.set(name, rel);
            } else if (before[name] !== value) {
                overrides.push(`${name}（先由 ${owner.get(name) || '未知脚本'} 定义，被 ${rel} 覆盖）`);
            }
        }
    }
    check('全部本地脚本按声明顺序装载成功', loadError === null, loadError || '');
    if (loadError) {
        console.log(`\n${failures} 项失败`);
        return failures;
    }

    // 3. 方法契约：拆分 / 搬移不得丢方法（子集断言 —— 新增方法不必更新清单）
    const protoNames = Object.keys(snapshot());
    check(`${className} 已定义`, protoNames.length > 0);
    const absent = requiredMethods.filter(name => !protoNames.includes(name));
    check(`${className}.prototype 上 ${requiredMethods.length} 个成员都存在`, absent.length === 0,
        `丢失: ${JSON.stringify(absent)}`);

    // 4. 同名成员不得被两个脚本重复定义
    check('没有成员被两个脚本重复定义', overrides.length === 0, overrides.join('; '));

    console.log(`  装载 ${loaded.length} 个脚本，${className}.prototype 共 ${protoNames.length} 个成员`);
    if (failures) console.log(`  ${failures} 项失败`);
    return failures;
}
