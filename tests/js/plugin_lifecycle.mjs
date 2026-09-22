// 插件前端生命周期（onShow / onHide / onDispose）与宿主可见性状态机的无头验证脚本。
//
// 背景：插件 iframe 默认常驻（v-show 隐藏），切走后
// 定时器 / rAF 自循环 / 轮询继续跑；插件前端也没有统一的销毁钩子，监听器只增不减
// （实测 plugins/**/*.js 合计 add 171 : remove 4）。宿主因此在可见性变化时 postMessage
// 通知，base.js 把它转成三个注册钩子。
//
// 这里锁住四件事：
//   1. 三个消息类型各自触发对应钩子，且只发"状态真变化"的通知（不重复触发清理）；
//   2. 消息来源校验：非父窗口（伪造来源）与跨源消息一律忽略；
//   3. 钩子注册时机（已可见时立即补一次；隐藏期间注册的 onShow 不立刻跑）；
//   4. 状态机与消息类型的映射：shown/hidden/dispose ↔ omnibox:plugin-*。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const BASE_JS = path.join(ROOT, 'shell/frontend/public/shell/base.js');
const VISIBILITY_JS = path.join(ROOT, 'shell/frontend/src/core/plugin-visibility.js');

let failures = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

// ---------- 场景 A：base.js 的钩子与消息校验 ----------
function loadPluginRuntime({ sameOrientation = true } = {}) {
    const listeners = { message: [] };
    const location = { origin: 'http://127.0.0.1:18080', reload() { } };
    const redirects = [];
    // 用访问器取代原始 href：只记录跳转目标，不真的导航
    let href = 'http://127.0.0.1:18080/plugins/probe/frontend/index.html';
    Object.defineProperty(location, 'href', {
        get: () => href,
        set: (v) => { href = String(v); redirects.push(String(v)); },
    });
    const body = { children: [], appendChild(el) { this.children.push(el); } };
    const win = {
        location,
        addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
        removeEventListener() { },
    };
    // 插件自身就是父窗口的子 frame：window.parent 指向宿主窗口
    const fakeParent = { location: { origin: location.origin } };
    win.parent = fakeParent;
    const sandbox = {
        window: win,
        parent: fakeParent,
        document: {
            location,
            body,
            addEventListener() { },
            createElement: () => ({ style: {}, classList: { add() { }, remove() { } }, appendChild() { }, remove() { }, setAttribute() { } }),
            querySelector: () => null,
            documentElement: { getAttribute: () => null, setAttribute() { }, style: { setProperty() { } } },
        },
        console,
        setTimeout, clearTimeout, setInterval, clearInterval, requestAnimationFrame: (fn) => setTimeout(fn, 0),
        encodeURIComponent, decodeURIComponent, String, Math, Date, JSON, Object, Array, Error, Promise,
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(BASE_JS, 'utf8'), sandbox, { filename: 'shell/base.js' });

    const fromParent = (data, opts = {}) => {
        const event = {
            data,
            origin: opts.origin ?? (sameOrientation ? location.origin : 'http://evil.tld'),
            source: opts.source === 'other' ? { fake: true } : fakeParent,
        };
        listeners.message.forEach(fn => fn(event));
    };
    return { win, fromParent, redirects };
}

console.log('场景 A：base.js 三个钩子 + 来源校验');
{
    const { win, fromParent, redirects } = loadPluginRuntime();
    const calls = { show: 0, hide: 0, dispose: 0 };
    win.onShow(() => { calls.show++; });
    win.onHide(() => { calls.hide++; });
    win.onDispose(() => { calls.dispose++; });

    check('注册入口挂在 window 上（onShow/onHide/onDispose）',
        typeof win.onShow === 'function' && typeof win.onHide === 'function' && typeof win.onDispose === 'function'
        && typeof win.PluginLifecycle?.onShow === 'function');

    check('注册时初始可见 → onShow 立即补一次', calls.show === 1, `实际 ${calls.show}`);

    fromParent({ type: 'omnibox:plugin-hidden' });
    check('hidden 消息触发 onHide', calls.hide === 1, `实际 ${calls.hide}`);
    fromParent({ type: 'omnibox:plugin-hidden' });
    check('重复 hidden 不重复触发（只发状态变化）', calls.hide === 1, `实际 ${calls.hide}`);

    fromParent({ type: 'omnibox:plugin-shown' });
    check('shown 消息触发 onShow', calls.show === 2, `实际 ${calls.show}`);
    fromParent({ type: 'omnibox:plugin-shown' });
    check('重复 shown 不重复触发', calls.show === 2, `实际 ${calls.show}`);

    fromParent({ type: 'omnibox:plugin-dispose' });
    check('dispose 消息触发 onDispose', calls.dispose === 1, `实际 ${calls.dispose}`);
    fromParent({ type: 'omnibox:plugin-shown' });
    fromParent({ type: 'omnibox:plugin-hidden' });
    check('dispose 后不再触发 show/hide', calls.show === 2 && calls.hide === 1,
        `show=${calls.show} hide=${calls.hide}`);

    fromParent({ type: 'omnibox:plugin-hidden' }, { source: 'other' });
    check('非父窗口来源的消息被忽略', calls.hide === 1, `实际 ${calls.hide}`);
    fromParent({ type: 'omnibox:plugin-hidden' }, { origin: 'http://evil.tld' });
    check('跨源消息被忽略', calls.hide === 1, `实际 ${calls.hide}`);

    fromParent({ type: 'omnibox:settings-changed' });
    check('settings-changed 仍然整页重载', redirects.length === 1 && redirects[0].includes('_t='),
        `实际 ${JSON.stringify(redirects)}`);
    fromParent({ type: 'omnibox:settings-changed' }, { source: 'other' });
    check('settings-changed 也校验来源', redirects.length === 1, `实际 ${redirects.length}`);
}

console.log('场景 B：注册时机（插件在隐藏期间才开始注册钩子）');
{
    const { win, fromParent } = loadPluginRuntime();
    fromParent({ type: 'omnibox:plugin-hidden' });

    let shown = 0;
    win.onShow(() => { shown++; });
    check('隐藏期间注册 onShow 不立刻执行', shown === 0, `实际 ${shown}`);

    let hidden = 0;
    win.onHide(() => { hidden++; });
    check('隐藏期间注册 onHide 立即补一次', hidden === 1, `实际 ${hidden}`);

    fromParent({ type: 'omnibox:plugin-shown' });
    check('之后 shown 正常触发', shown === 1 && hidden === 1, `show=${shown} hide=${hidden}`);
}

console.log('场景 C：钩子抛异常不影响其他钩子');
{
    const { win, fromParent } = loadPluginRuntime();
    let second = 0;
    win.onHide(() => { throw new Error('插件自身 bug'); });
    win.onHide(() => { second++; });
    fromParent({ type: 'omnibox:plugin-hidden' });
    check('前一个钩子抛异常，后一个仍然执行', second === 1, `实际 ${second}`);
}

// ---------- 场景 D：可见性状态机（App.vue 侧判定） ----------
const visibility = await import(pathToFileURL(VISIBILITY_JS).href);
const M = visibility.LIFECYCLE_MESSAGES;
const SHOWN = JSON.stringify([M.shown]);
const HIDDEN = JSON.stringify([M.hidden]);
const DISPOSE = JSON.stringify([M.dispose]);
// 便捷调用：refreshFrame(name, { mounted, active, windowVisible })
const see = (name, mounted, active, windowVisible) =>
    visibility.refreshFrame(name, { mounted, active, windowVisible });

console.log('场景 D：可见性状态机（挂载 × 活动 × 窗口可见）');
{
    check('协议串是完整的 postMessage type',
        M.shown === 'omnibox:plugin-shown' && M.hidden === 'omnibox:plugin-hidden'
        && M.dispose === 'omnibox:plugin-dispose',
        JSON.stringify(M));
    visibility.disposeFrames();
    check('活动插件首次判定 → shown', JSON.stringify(see('image-viewer', true, true, true)) === SHOWN);
    check('再次判定不重复发', JSON.stringify(see('image-viewer', true, true, true)) === '[]');

    check('切换到其他插件 → hidden', JSON.stringify(see('image-viewer', true, false, true)) === HIDDEN);
    // 另一个插件先显示，再最小化窗口 → hidden（未显示过的 frame 不该发 hidden）
    check('另一插件显示 → shown', JSON.stringify(see('media-player', true, true, true)) === SHOWN);
    check('最小化窗口 → 活动插件变 hidden', JSON.stringify(see('media-player', true, true, false)) === HIDDEN);
    check('窗口恢复可见 → shown', JSON.stringify(see('media-player', true, true, true)) === SHOWN);
}

console.log('场景 E：默认（不保活）形态 —— iframe 加载完成前就切走');
{
    visibility.disposeFrames();
    // 插件还没加载完成（未登记），就被切走：不得发出"可见"
    check('未挂载 frame 判定为不可见且不发通知',
        JSON.stringify(see('lazy-plugin', false, false, true)) === '[]');
    check('登记为未挂载也不发通知',
        JSON.stringify(visibility.ensureFrame('lazy-plugin', false)) === '[]');
    check('之后真正成为活动插件才 shown',
        JSON.stringify(visibility.ensureFrame('lazy-plugin', true)) === SHOWN);
    check('离开（卸载）→ dispose 由调用方发送',
        JSON.stringify(visibility.disposeFrames(['lazy-plugin'])) === DISPOSE);
    check('dispose 后重新挂载能再次 shown（状态已清空）',
        JSON.stringify(visibility.ensureFrame('lazy-plugin', true)) === SHOWN);
}

console.log('场景 E2：文档代次（不保活插件每次进来都是新文档 / 重载）');
{
    visibility.disposeFrames();
    check('首次登记代次视为变化', visibility.noteFrameEpoch('reload-plugin', 1) === true);
    check('同一代次再登记不算变化', visibility.noteFrameEpoch('reload-plugin', 1) === false);
    check('代次递增视为重载', visibility.noteFrameEpoch('reload-plugin', 2) === true);
    check('代次回退（新文档计数从 1 开始）也算重载', visibility.noteFrameEpoch('reload-plugin', 1) === true);
    // 重载后旧结论作废：活动插件的新文档必须重新收到 shown
    visibility.disposeFrames();
    visibility.noteFrameEpoch('reload-plugin', 1);
    visibility.ensureFrame('reload-plugin', true);
    visibility.noteFrameEpoch('reload-plugin', 2);
    check('重载后重新判定仍会产生通知（旧结论已作废）',
        JSON.stringify(visibility.ensureFrame('reload-plugin', true)) === SHOWN);
    // 后台重载（不是活动插件）：不产生 shown，插件不会误以为自己可见
    visibility.disposeFrames();
    visibility.noteFrameEpoch('bg-plugin', 1);
    check('后台重载不得产生 shown',
        JSON.stringify(visibility.ensureFrame('bg-plugin', false)) === '[]');
}

// ---------- 场景 F：状态机 → 消息类型映射，并与 base.js 端到端对齐 ----------
console.log('场景 F：状态机输出与 base.js 钩子端到端对齐');
{
    visibility.disposeFrames();
    const { win, fromParent } = loadPluginRuntime();
    const fired = [];
    win.onShow(() => fired.push('shown'));
    win.onHide(() => fired.push('hidden'));

    // 纯中继：把状态机的输出原样 postMessage 给插件（与 App.vue 的 sendToFrame 等价）
    const relay = (messages) => messages.forEach(m => fromParent({ type: m }));

    relay(see('probe', true, true, true));
    relay(see('probe', true, false, true));
    relay(see('probe', true, true, true));
    check('状态机通知驱动插件钩子顺序为 shown → hidden → shown',
        JSON.stringify(fired) === '["shown","hidden","shown"]', `实际 ${JSON.stringify(fired)}`);

    let disposed = 0;
    win.onDispose(() => { disposed++; });
    relay(visibility.disposeFrames(['probe']));
    check('dispose 通知驱动 onDispose', disposed === 1, `实际 ${disposed}`);
}

console.log(failures === 0 ? '\n插件生命周期用例全部通过' : `\n插件生命周期用例失败 ${failures} 项`);
process.exit(failures === 0 ? 0 : 1);
