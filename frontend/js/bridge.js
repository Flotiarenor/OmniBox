// 与后端通信的唯一通道
const bridge = {
    FILE_SERVER: 'http://127.0.0.1:18080',
    _api: null,

    async init() {
        this._api = await this._waitForApi();
    },

    _waitForApi(maxWait = 5000) {
        return new Promise((resolve, reject) => {
            const start = Date.now();
            const check = () => {
                if (window.pywebview && window.pywebview.api) {
                    resolve(window.pywebview.api);
                } else if (Date.now() - start > maxWait) {
                    reject(new Error('PyWebView API 不可用'));
                } else {
                    setTimeout(check, 100);
                }
            };
            check();
        });
    },

    async call(method, ...args) {
        if (!this._api) await this.init();
        try {
            return await this._api[method](...args);
        } catch (e) {
            console.error(`API调用失败: ${method}`, e);
            throw e;
        }
    },

    // 便捷方法：获取原图URL
    originalUrl(path) {
        return `${this.FILE_SERVER}/images/${path}`;
    },

    // 便捷方法：获取缩略图URL
    thumbUrl(path) {
        return `${this.FILE_SERVER}/thumbs/${path}`;
    }
};