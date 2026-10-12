/**
 * This product includes software developed by flotiarenor.Copyright 2026 flotiarenor
 * OmniBox Shell 通用可拖拽分隔条（splitter）
 * 配合 /shell/splitter.css 使用：注入所有插件 iframe，壳页面同样引入一份。
 * 用法与契约见 docs/plugin-ui-guide.md。
 *
 * 声明式接入：给"尺寸由某个 CSS 变量决定"的元素加一个属性即可
 *   <aside class="view-sub-sidebar" data-obx-splitter="--sub-sidebar-width"
 *          data-obx-splitter-min="180" data-obx-splitter-reserve="320">
 * 属性（除 data-obx-splitter 外都可省略）：
 *   -axis="x|y"      拖动方向，默认 x（x 改宽度，y 改高度）
 *   -offset="76"     变量值与元素实际尺寸的差值，默认 0。元素尺寸不由变量直接给出时
 *                    必须声明，否则起手值会差出这个量（如媒体播放器舞台：
 *                    高度 = calc(--mp-stage-vh + 播放条 76px)）。
 *   -min="180"       下限，默认横向 140 / 纵向 80
 *   -max="480|auto"  上限，默认 auto（按父容器当期可用空间算，窗口变化自动跟随）
 *   -reserve="0"     auto 上限时给同容器其它内容保留的空间
 *   -default="240"   双击 / 回车复位到的值，未声明则禁用复位
 *
 * 两个值分层（本内核的核心）：
 *   - **用户值**由壳持久化（`<config_dir>/shell.json` 的偏好表 + html 的内联变量），
 *     只有用户拖动才改写；
 *   - **渲染值**由本内核写在目标元素自己的内联样式上，按当前可用空间 clamp。
 *   元素内联声明优先于 html 继承、也优先于元素上的类选择器，于是：
 *     · 窗口变小只压低渲染值，用户值不受损，窗口恢复后自动回到用户拖出来的大小；
 *     · 用户值能压过"模式默认"（如媒体播放器 `.video-on` 的 300px），
 *       因为那条规则是类选择器，而内核写在元素内联上。
 *   未拖过时**不写内联**，CSS 的模式默认值照常生效 —— 所以接入本内核
 *   不需要为任何既有默认值加分支。
 */
window.ShellSplitter = (function () {
    'use strict';

    const ATTR = 'data-obx-splitter';
    const AXIS_ATTR = 'data-obx-splitter-axis';
    const OFFSET_ATTR = 'data-obx-splitter-offset';
    const MIN_ATTR = 'data-obx-splitter-min';
    const MAX_ATTR = 'data-obx-splitter-max';
    const RESERVE_ATTR = 'data-obx-splitter-reserve';
    const DEFAULT_ATTR = 'data-obx-splitter-default';
    const HANDLE_ATTR = 'data-obx-splitter-handle';
    const DRAG_ATTR = 'data-obx-dragging';
    const HANDLE_CLASS = 'obx-splitter';
    const ACTIVE_CLASS = 'obx-splitter-active';
    // 拖动期间加在**被拖元素**上：splitter.css 据此停掉该元素的尺寸过渡。
    // 只作用于正在拖的那一个元素 —— 停过渡不该牵连同文档里别的侧栏。
    const LIVE_CLASS = 'obx-splitter-live';
    // 变量名形状校验：这个值会经宿主写进 CSS 变量，放行任意字符串等于开一个注入面。
    const VAR_NAME_RE = /^--[a-z0-9-]+$/;
    const STEP = 4;
    const STEP_LARGE = 16;
    const SCAN_DELAY = 80;
    const DEFAULT_MIN_X = 140;
    const DEFAULT_MIN_Y = 80;
    const COMMIT_DELAY = 120;

    const items = [];
    const registered = new WeakSet();
    const resizeObserver = typeof ResizeObserver === 'function'
        ? new ResizeObserver(() => scheduleRelayout())
        : null;
    let scanTimer = 0;
    let layoutFrame = 0;

    function toPx(value) {
        if (value === null || value === undefined) return null;
        const n = parseFloat(value);
        return Number.isFinite(n) ? n : null;
    }

    function sizeOf(el, axis) {
        const rect = el.getBoundingClientRect();
        return axis === 'y' ? rect.height : rect.width;
    }

    /** 元素起始边到父容器内容区起始边的距离（含父容器 padding 与元素自身外边距）。 */
    function startIn(el, parent, axis) {
        const a = el.getBoundingClientRect();
        const b = parent.getBoundingClientRect();
        return axis === 'y'
            ? a.top - b.top - parent.clientTop
            : a.left - b.left - parent.clientLeft;
    }

    function availOf(parent, axis) {
        return axis === 'y' ? parent.clientHeight : parent.clientWidth;
    }

    /**
     * 读**用户值**：只看 html 的内联样式。
     *
     * 不能用 getComputedStyle：那会把 `variables.css` 里的 `:root` 默认值一并读进来，
     * 于是"用户从没拖过"与"用户拖到了默认值"变得无法区分 —— 前者会让内核抢在
     * 插件自己的模式默认值之前写死一个内联值（媒体播放器舞台的音乐/视频两档高度
     * 就是这么丢的）。壳写入用户值走的一律是 html 内联，这里也只认内联。
     */
    function preferredFromRoot(name) {
        return toPx(document.documentElement.style.getPropertyValue(name));
    }

    /** 落盘时用的变量名：就是声明在 `data-obx-splitter` 上的那一个。
     *
     * 留成函数是为了收口：将来若要支持"同一个变量在不同插件里各自取值"
     * （见 docs/plugin-ui-guide.md §3.6 关于变量名与取值的约定），换算只改这一处，
     * 调用方（commit / flushCommit / commitValue）都不必动。 */
    function commitNameOf(item) {
        return item.name;
    }

    /**
     * 把用户值交给宿主持久化。
     * 壳页面自己拖动时走 App.vue 注入的 window.__obxVarCommit；
     * 插件 iframe 里发 postMessage，由壳校验形状后写入。
     */
    function writeThrough(name, value) {
        if (typeof window.__obxVarCommit === 'function') {
            try {
                window.__obxVarCommit(name, value);
                return;
            } catch (e) {
                // 落到 postMessage：壳的回调抛异常时不该让拖动看起来"没保存"
            }
        }
        if (window.parent === window) return;
        try {
            window.parent.postMessage(
                { type: 'omnibox:set-var', key: name, value },
                window.location.origin
            );
        } catch (e) {
            // 父窗口已销毁：值仍留在当前文档，等下次重载回落默认值
        }
    }

    /**
     * 落盘一次（尾沿去抖）。
     *
     * 拖动与键盘微调都会连续改值，逐次上报等于逐次写一次 `shell.json`（后端
     * 每次 `os.replace`）并触发一次全插件 `data-custom-colors` 广播。去抖只保留
     * 最后一次取值：拖动过程的中间值没有任何保留价值 —— 它既不是用户的选择，
     * 也是"松手后宽度停在半路"这类现象的来源。
     * `flushCommit` 在指针抬起时立刻把待上报值送出去，松手不会等这个窗口。
     */
    function commit(item) {
        const name = commitNameOf(item);
        const value = item.pendingValue;
        item.pendingName = name;
        if (item.commitTimer) clearTimeout(item.commitTimer);
        item.commitTimer = setTimeout(() => flushCommit(item), COMMIT_DELAY);
    }

    function flushCommit(item) {
        if (item.commitTimer) {
            clearTimeout(item.commitTimer);
            item.commitTimer = 0;
        }
        if (item.pendingValue === null) return;
        const name = item.pendingName || commitNameOf(item);
        const value = item.pendingValue;
        item.pendingValue = null;
        item.pendingName = null;
        writeThrough(name, value);
        // 落盘后这条变量就是用户值：本次会话内不依赖壳的广播也能继续跟上
        item.lastUser = { name, value: toPx(value) };
    }

    /** 上限：声明了数字就用它，否则按父容器当期可用空间扣掉起手距离、尺寸差与保留量。
     *
     * 两条边界：
     *  · 声明了 `-max` 的元素**不**按可用空间兜底 —— `-max` 是"用户最多能拖到多大"，
     *    "当前窗口放不放得下"是插件自己的布局问题（媒体播放器的视频舞台写死
     *    `--mp-stage-vh: 300px`，矮窗口要靠 `.video-on` 自己收，见 media-player.css）。
     *  · 没声明 `-max` 时按可用空间算，但**只在 relayout 里用**：这个函数读的是元素
     *    当期量到的尺寸，写内联又会改变它，写进 `render()` 就会自激（见那里的说明）。
     */
    function maxOf(item) {
        if (item.fixedMax !== null) return item.fixedMax;
        const parent = item.el.parentElement;
        if (!parent) return Number.POSITIVE_INFINITY;
        const space = availOf(parent, item.axis)
            - startIn(item.el, parent, item.axis)
            - item.offset
            - item.reserve;
        return Math.max(item.min, space);
    }

    /** 用户值来源：壳写在 html 内联上的取值优先，其次是本次会话里拖过的值。
     *
     * 读的一律是 `item.name`（声明在 `data-obx-splitter` 上的那一支）：值从哪来、
     * 谁写的都不必在这里判断。 */
    function userValueOf(item) {
        const fromRoot = preferredFromRoot(item.name);
        if (fromRoot !== null) return fromRoot;
        return item.lastUser && item.lastUser.name === item.name ? item.lastUser.value : null;
    }

    /** 元素当前渲染出来的尺寸折回变量口径（拖动起手、键盘微调的基准）。 */
    function currentValueOf(item) {
        return Math.max(item.min, sizeOf(item.el, item.axis) - item.offset);
    }

    /**
     * 写渲染值。value 为 null 表示"没有用户值" → 清掉内联，交还给 CSS 默认值。
     *
     * **只在取值变化时写**，不能每次都先摘再写：`maxOf()` 是拿元素**当期量到的尺寸**
     * 去算可用空间的，而写内联又会改变这个尺寸 —— 每次重排都写一遍就成了"量到被压过
     * 的值、再按它算一次上限"，尺寸会一轮轮涨上去（实测媒体播放器的音乐模式舞台从
     * 300px 一路涨到 590px，把播放栏顶出视口）。只在变化时写则最多收敛到一次。
     */
    function render(item, value) {
        if (value === null) {
            if (item.current !== undefined) {
                item.el.style.removeProperty(item.name);
                item.current = undefined;
            }
            return null;
        }
        const bounded = Math.round(Math.min(maxOf(item), Math.max(item.min, value)));
        if (item.current !== bounded) {
            item.current = bounded;
            item.el.style.setProperty(item.name, bounded + 'px');
        }
        item.handle.setAttribute('aria-valuenow', String(bounded));
        return bounded;
    }

    /**
     * 用户确认的值：记住 → 渲染 → 排队落盘，返回实际写下的值。
     *
     * **只排队，不落盘**：调用方在此之后还要补写最后一帧的取值（拖动路径就是
     * 这样），落盘统一交给 `flushCommit`。在这里就落一次的话，拖动结束时先写出的
     * 会是拖动中间某一帧的值 —— 界面上表现为"松手瞬间宽度跳一下再回到指针位置"。
     */
    function commitValue(item, value) {
        const bounded = render(item, value);
        const written = bounded === null ? Math.round(value) : bounded;
        item.lastUser = { name: commitNameOf(item), value: written };
        item.pendingValue = written + 'px';
        item.handle.setAttribute('aria-valuenow', String(written));
        commit(item);
        return written;
    }

    /** 拖动 / 键盘调整期间在被拖元素上挂标记：splitter.css 据此停掉它的尺寸过渡。 */
    function setLive(item, live) {
        if (live) item.el.classList.add(LIVE_CLASS);
        else item.el.classList.remove(LIVE_CLASS);
    }

    function syncHandleVisibility(item) {
        const visible = sizeOf(item.el, item.axis) > 0;
        item.handle.style.display = visible ? '' : 'none';
    }

    /**
     * 元素当前是否**真的**有尺寸。
     *
     * 被切回前台的那一帧，iframe 里的元素会量到 0（文档刚挂上、父级高度还没算出来）。
     * 这时不能把它当成"没有用户值"去清内联：清掉之后重新量到的尺寸要**过渡**过去，
     * 表现就是"切回来先缩成一条、再慢慢滑回原来的位置"。
     */
    function measurable(item) {
        return sizeOf(item.el, item.axis) > 0;
    }

    function relayout() {
        layoutFrame = 0;
        items.forEach((item) => {
            if (!item.el.isConnected) return;
            syncHandleVisibility(item);
            // 拖动中不重算：此时尺寸由指针驱动，重算会把值拉回上一次落盘的值。
            if (item.dragging) return;
            // 尺寸还没量出来（切回前台的第一帧）时不动内联值：见 measurable 的说明。
            if (!measurable(item)) return;
            render(item, userValueOf(item));
        });
    }

    function scheduleRelayout() {
        if (layoutFrame) return;
        layoutFrame = requestAnimationFrame(relayout);
    }

    function onPointerDown(item, event) {
        if (event.button !== 0) return;
        const axis = item.axis;
        const pointerId = event.pointerId;
        const startPointer = axis === 'y' ? event.clientY : event.clientX;
        // 起手值取元素**实际尺寸**再折回变量口径：CSS 里的默认值、媒体查询、
        // 插件脚本写过的值都不必在这里重复推导。
        const startValue = currentValueOf(item);
        let value = render(item, startValue);

        item.dragging = true;
        setLive(item, true);
        // 指针捕获是这类控件的成败点：不捕获的话，鼠标一旦移出被拖的元素
        // （拖出 iframe 也一样）就再也收不到 pointermove，拖动会中途卡住。
        item.handle.setPointerCapture(pointerId);
        item.handle.classList.add(ACTIVE_CLASS);
        document.documentElement.setAttribute(DRAG_ATTR, axis);

        function onMove(moveEvent) {
            if (moveEvent.pointerId !== pointerId) return;
            const delta = (axis === 'y' ? moveEvent.clientY : moveEvent.clientX) - startPointer;
            value = render(item, startValue + delta);
            // 拖动中只改渲染值，用户值沿用同一个去抖窗口；松手那一次立刻落盘。
            item.pendingValue = Math.round(value) + 'px';
            moveEvent.preventDefault();
        }

        function onEnd(endEvent) {
            if (endEvent.pointerId !== pointerId) return;
            item.handle.removeEventListener('pointermove', onMove);
            item.handle.removeEventListener('pointerup', onEnd);
            item.handle.removeEventListener('pointercancel', onEnd);
            try {
                item.handle.releasePointerCapture(pointerId);
            } catch (e) {
                // 捕获已因元素移除而失效
            }
            item.handle.classList.remove(ACTIVE_CLASS);
            document.documentElement.removeAttribute(DRAG_ATTR);
            item.dragging = false;
            // 顺序是硬约束：先摘掉 live 标记放回过渡，再写最终值（这一步带上过渡
            // 滑到位），最后落盘。落盘必须排在最后 —— 它送出的是 pendingValue，
            // 早一步就会把拖动中间那一帧写进偏好。
            setLive(item, false);
            commitValue(item, value);
            flushCommit(item);
        }

        item.handle.addEventListener('pointermove', onMove);
        item.handle.addEventListener('pointerup', onEnd);
        item.handle.addEventListener('pointercancel', onEnd);
    }

    function onKeyDown(item, event) {
        const step = event.shiftKey ? STEP_LARGE : STEP;
        const isY = item.axis === 'y';
        const dec = isY ? 'ArrowUp' : 'ArrowLeft';
        const inc = isY ? 'ArrowDown' : 'ArrowRight';
        let delta = 0;

        if (event.key === dec) delta = -step;
        else if (event.key === inc) delta = step;
        else if ((event.key === 'Enter' || event.key === ' ') && item.fallback !== null) {
            event.preventDefault();
            commitValue(item, item.fallback);
            flushCommit(item);
            return;
        } else return;

        event.preventDefault();
        const base = item.current ?? currentValueOf(item);
        // 键盘是离散操作：没有"拖动中"这个阶段，过渡留着更自然（每一格滑过去），
        // 因此这里不挂 live 标记。
        commitValue(item, base + delta);
    }

    function register(el) {
        if (registered.has(el)) return null;
        const name = (el.getAttribute(ATTR) || '').trim();
        if (!VAR_NAME_RE.test(name)) {
            console.warn('[ShellSplitter] 非法变量名，已忽略:', name || '(空)');
            return null;
        }
        registered.add(el);

        const axis = el.getAttribute(AXIS_ATTR) === 'y' ? 'y' : 'x';
        const maxAttr = (el.getAttribute(MAX_ATTR) || 'auto').trim();
        const item = {
            el,
            name,
            axis,
            offset: toPx(el.getAttribute(OFFSET_ATTR)) || 0,
            min: toPx(el.getAttribute(MIN_ATTR)) ?? (axis === 'y' ? DEFAULT_MIN_Y : DEFAULT_MIN_X),
            reserve: toPx(el.getAttribute(RESERVE_ATTR)) || 0,
            fixedMax: maxAttr === 'auto' ? null : toPx(maxAttr),
            fallback: toPx(el.getAttribute(DEFAULT_ATTR)),
            lastUser: null,
            current: undefined,
            dragging: false,
            handle: null,
            pendingValue: null,
            pendingName: null,
            commitTimer: 0,
        };

        const handle = document.createElement('div');
        handle.className = HANDLE_CLASS + (axis === 'y' ? ' obx-splitter-y' : ' obx-splitter-x');
        handle.setAttribute(HANDLE_ATTR, '');
        handle.setAttribute('role', 'separator');
        handle.setAttribute('aria-orientation', axis === 'y' ? 'horizontal' : 'vertical');
        handle.setAttribute('aria-label', '拖动调整大小');
        handle.setAttribute('aria-valuenow', String(Math.round(currentValueOf(item))));
        handle.tabIndex = 0;
        item.handle = handle;
        // 插在目标元素之后：目标元素的父容器就是那条 flex 行 / 列，
        // 因此把手天然成为同容器的一个成员，不需要知道容器是谁。
        el.insertAdjacentElement('afterend', handle);

        handle.addEventListener('pointerdown', (e) => onPointerDown(item, e));
        handle.addEventListener('keydown', (e) => onKeyDown(item, e));
        handle.addEventListener('dblclick', () => {
            if (item.fallback === null) return;
            // 复位是离散动作，立刻落盘，不等去抖窗口
            commitValue(item, item.fallback);
            flushCommit(item);
        });

        items.push(item);
        // 有持久用户值且当前窗口放不下时，这里会顺手把渲染值压回可用范围；
        // 从未拖过则什么也不写，CSS 的默认值（含插件的模式默认）保持生效。
        // 只写元素内联、**不碰根元素**：根元素上是壳的权威值，在这里补一份初值
        // 等于用 `data-obx-splitter-default` 盖掉它。
        render(item, userValueOf(item));
        syncHandleVisibility(item);
        if (resizeObserver) {
            // 父容器变化（窗口尺寸、并排内容增减）与元素自身变化（被折叠成
            // display:none）都要重算：只观察父容器的话，侧栏收起时把手会留在原地。
            resizeObserver.observe(el);
            if (el.parentElement) resizeObserver.observe(el.parentElement);
        }
        return item;
    }

    /** 扫描并注册（重复调用安全：已注册的元素直接跳过）。 */
    function scan(root) {
        const scope = root && root.querySelectorAll ? root : document;
        if (scope.matches && scope.matches('[' + ATTR + ']')) register(scope);
        scope.querySelectorAll('[' + ATTR + ']').forEach(register);
    }

    function scheduleScan() {
        if (scanTimer) return;
        // 插件前端大量动态渲染，childList 回调很密；节流后一次扫描覆盖一批。
        scanTimer = setTimeout(() => {
            scanTimer = 0;
            scan(document);
        }, SCAN_DELAY);
    }

    function boot() {
        scan(document);
        new MutationObserver(scheduleScan).observe(document.documentElement, {
            childList: true,
            subtree: true,
        });
        // 壳把用户值广播到 html（内联样式与 data-custom-colors 属性）后重算一次：
        // 共享变量（--sub-sidebar-width 有 5 个插件在用）在别的 iframe 里被拖动时，
        // 本 iframe 靠这条观察跟上，不需要各自轮询或互相发消息。
        new MutationObserver(scheduleRelayout).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['style', 'data-custom-colors'],
        });
        window.addEventListener('resize', scheduleRelayout);
        // 页面正在卸载时把待落盘的值补发出去：去抖窗口正好被"关掉插件 / 刷新"截断
        // 时，最后一次拖动不该跟着文档一起消失。
        window.addEventListener('pagehide', () => {
            items.forEach(flushCommit);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }

    return { scan, relayout: scheduleRelayout };
})();
