/**
 * OmniBox Shell 通用动效辅助（自动注入所有插件 iframe）
 * 配合 /shell/effects.css 使用。
 */
window.Motion = (function () {
    /**
     * 给容器内直接子元素设置交错延迟索引 --obx-i。
     * 容器需带 .obx-stagger 类（或子元素带任意动画类）。
     */
    function stagger(container, selector) {
        if (!container) return;
        const nodes = selector
            ? container.querySelectorAll(selector)
            : container.children;
        Array.from(nodes).forEach((el, i) => {
            el.style.setProperty('--obx-i', Math.min(i, 32));
        });
        return nodes;
    }

    /**
     * 重新触发一次弹跳 / 心跳动画。
     * className 默认为 'obx-anim-heart'，元素需要先有对应动画类。
     */
    function retrigger(el, className) {
        if (!el) return;
        const cls = className || 'obx-anim-heart';
        el.classList.remove(cls);
        void el.offsetWidth; // 强制 reflow，重置动画
        el.classList.add(cls);
    }

    /**
     * 淡入 + 上移显示元素（切换 hidden 后调用）。
     */
    function show(el, className) {
        if (!el) return;
        const cls = className || 'obx-anim-fade-up';
        el.classList.add(cls);
        void el.offsetWidth;
        el.classList.remove(cls);
        el.classList.add(cls);
    }

    return { stagger, retrigger, show };
})();
