/**
 * 视图管理器 — 处理导航切换
 */
class ViewManager {
    constructor() {
        this.currentViewId = null;
        this.initialized = {};  // 视图ID → 是否已初始化
        this.initCallbacks = {}; // 视图ID → 初始化函数

        this.bindNavigation();
    }

    bindNavigation() {
        document.querySelectorAll('.nav-item[data-view]').forEach(item => {
            item.addEventListener('click', () => {
                const viewId = item.dataset.view;
                this.switchTo(viewId);
            });
        });
    }

    register(viewId, initCallback) {
        this.initCallbacks[viewId] = initCallback;
    }

    async switchTo(viewId) {
        if (this.currentViewId === viewId) return;

        // 隐藏当前视图
        if (this.currentViewId) {
            const oldView = document.getElementById(`view-${this.currentViewId}`);
            if (oldView) oldView.classList.remove('active');
        }

        // 显示新视图
        const newView = document.getElementById(`view-${viewId}`);
        if (newView) newView.classList.add('active');

        // 更新导航高亮
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.toggle('active', item.dataset.view === viewId);
        });

        this.currentViewId = viewId;

        // 首次切换时初始化
        if (!this.initialized[viewId] && this.initCallbacks[viewId]) {
            await this.initCallbacks[viewId]();
            this.initialized[viewId] = true;
        }
    }
}