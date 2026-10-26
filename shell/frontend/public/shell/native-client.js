/* 附属插件 native 逻辑页的统一客户端。
 *
 * 由宿主注入的 `/shell/native-client.js` 提供；逻辑页只实现 onInit / onAction，
 * ready、init、schema、action、error、pagehide 清理都由这里统一处理。 */
(function () {
  'use strict';

  function clone(value) {
    try {
      return value === undefined ? value : JSON.parse(JSON.stringify(value));
    } catch (e) {
      return undefined;
    }
  }

  function isFromShell(event) {
    if (!event) return false;
    try {
      if (event.source && window.parent && event.source !== window.parent) return false;
      if (window.parent === window) return false;
    } catch (e) {
      return false;
    }
    if (event.origin && event.origin !== window.location.origin) return false;
    return true;
  }

  function errorText(error) {
    if (error && typeof error.message === 'string') return error.message;
    return String(error == null ? '' : error);
  }

  function create(options) {
    const opts = options || {};
    const retryMs = Number(opts.readyRetryMs) > 0 ? Number(opts.readyRetryMs) : 500;
    const retries = Number(opts.readyRetries) > 0 ? Number(opts.readyRetries) : 10;
    let initialized = false;
    let disposed = false;
    let readyTimer = null;
    let readyAttempts = 0;

    function post(payload) {
      if (!window.parent || window.parent === window) return;
      try {
        window.parent.postMessage(payload, window.location.origin);
      } catch (e) {
        /* 壳窗口已销毁：没有接收方，不再重试 */
      }
    }

    function sendReady() {
      post({ type: 'omnibox:native-ready' });
    }

    function stopReady() {
      if (readyTimer) {
        clearInterval(readyTimer);
        readyTimer = null;
      }
    }

    function render(blocks, toolbar) {
      post({
        type: 'omnibox:schema',
        blocks: clone(blocks) || [],
        toolbar: Array.isArray(toolbar) ? clone(toolbar) || [] : [],
      });
    }

    function fail(error) {
      post({ type: 'omnibox:native-error', error: errorText(error) || '内容加载失败' });
    }

    async function runInit() {
      if (initialized || disposed) return;
      initialized = true;
      stopReady();
      try {
        if (typeof opts.onInit === 'function') await opts.onInit(api);
      } catch (error) {
        fail(error);
      }
    }

    async function runAction(action, payload) {
      const plain = clone(payload);
      try {
        if (typeof opts.onAction === 'function') await opts.onAction(action, plain, api);
      } catch (error) {
        fail(error);
      }
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      stopReady();
      try {
        if (typeof opts.onDispose === 'function') opts.onDispose();
      } catch (e) {
        /* 清理失败不向壳报错：文档即将销毁 */
      }
    }

    const api = { post: post, render: render, error: fail, dispose: dispose };

    window.addEventListener('message', function (event) {
      if (!isFromShell(event)) return;
      const data = event.data;
      if (!data || typeof data.type !== 'string') return;
      if (data.type === 'omnibox:native-init') {
        void runInit();
        return;
      }
      if (data.type === 'omnibox:native-action') {
        void runAction(data.action, data.payload);
      }
    });

    window.addEventListener('pagehide', dispose);
    window.addEventListener('beforeunload', dispose);

    sendReady();
    readyTimer = setInterval(function () {
      if (initialized || disposed || readyAttempts >= retries) {
        stopReady();
        return;
      }
      readyAttempts += 1;
      sendReady();
    }, retryMs);

    return api;
  }

  window.NativeExtension = { create: create };
})();
