// ===== 视频封面前端抽帧器：隐藏 <video> seek + canvas 截图 =====
// 与播放共用浏览器解码器：系统能放的视频就能抽帧，解码能力与播放严格对齐。
// 抽帧结果经 media_put_thumb 回写 ThumbCache，之后 /thumbs/<id> 直接命中缓存。
// 并发 2 个 worker + 按 itemId 去重；所有等待均有超时，避免队列死锁。
// 注意：必须挂到 window（const 声明的全局变量不属于 window，window.MediaFrameExtractor 会是 undefined）。
const MediaFrameExtractor = (function () {
    const WORKERS = 2;              // 并发抽帧数（浏览器解码资源有限，2 个较稳）
    const EVENT_TIMEOUT = 15000;    // loadedmetadata / seeked 超时
    const RAF_TIMEOUT = 3000;       // rAF 挂起超时（iframe 不可见时 rAF 不触发）

    const videos = [];              // video 元素池
    let queue = [];
    let active = 0;
    let seq = 0;
    const pending = new Map();      // itemId -> { imgs: Set, fallback: fn }

    function acquireVideo() {
        for (const v of videos) {
            if (!v.dataset.mpBusy) { v.dataset.mpBusy = '1'; return v; }
        }
        const v = document.createElement('video');
        v.preload = 'metadata';
        v.muted = true;
        // 离屏定位（display:none 可能不触发解码，fixed 移出视口更稳）
        v.style.cssText = 'position:fixed;left:-9999px;top:0;width:320px;height:180px;pointer-events:none;opacity:0;';
        v.dataset.mpBusy = '1';
        document.body.appendChild(v);
        videos.push(v);
        return v;
    }

    function releaseVideo(v) {
        v.pause();
        v.removeAttribute('src');
        try { v.load(); } catch (e) { }   // 释放解码资源
        delete v.dataset.mpBusy;
    }

    // 等待目标事件或 error/超时；返回是否成功
    function waitEvent(v, name, timeout) {
        return new Promise((resolve) => {
            let done = false;
            const timer = setTimeout(() => finish(false), timeout);
            const finish = (ok) => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                v.removeEventListener(name, onOk);
                v.removeEventListener('error', onErr);
                resolve(ok);
            };
            const onOk = () => finish(true);
            const onErr = () => finish(false);
            v.addEventListener(name, onOk);
            v.addEventListener('error', onErr);
        });
    }

    // 双 rAF 防黑帧；iframe 不可见时 rAF 挂起 → 超时返回 false（放弃本次，队列继续）
    function nextFrame() {
        return new Promise((resolve) => {
            let done = false;
            const timer = setTimeout(() => {
                if (!done) { done = true; resolve(false); }
            }, RAF_TIMEOUT);
            requestAnimationFrame(() => requestAnimationFrame(() => {
                if (done) return;
                done = true;
                clearTimeout(timer);
                resolve(true);
            }));
        });
    }

    // 单次抽帧尝试（含资源获取/释放）
    async function extractOnce(itemId) {
        const v = acquireVideo();
        try {
            const item = await Bridge.call('media_get_item', itemId).catch(() => null);
            if (!item || !item.path || item.kind !== 'video') return null;
            v.src = MPUtils.mediaUrl(item.path);
            if (!await waitEvent(v, 'loadedmetadata', EVENT_TIMEOUT)) return null;
            const dur = v.duration || 0;
            let seek;
            if (!isFinite(dur) || dur <= 0) seek = 10;          // 时长未知：默认取 10s 处
            else seek = Math.max(0.5, Math.min(dur * 0.1, 120)); // 10% 处，钳制 [0.5, 120]
            v.currentTime = seek;
            if (!await waitEvent(v, 'seeked', EVENT_TIMEOUT)) return null;
            if (!await nextFrame()) return null;
            if (!v.videoWidth || !v.videoHeight) return null;
            const scale = Math.min(1, 640 / v.videoWidth);
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(v.videoWidth * scale));
            canvas.height = Math.max(1, Math.round(v.videoHeight * scale));
            canvas.getContext('2d').drawImage(v, 0, 0, canvas.width, canvas.height);
            const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
            if (!dataUrl || dataUrl.length < 100) return null;
            const res = await Bridge.call('media_put_thumb', itemId, dataUrl).catch(() => null);
            return res && res.success ? dataUrl : null;
        } catch (e) {
            return null;
        } finally {
            releaseVideo(v);
        }
    }

    // 抽帧（失败重试一次：服务器瞬时繁忙/文件首次打开慢等场景）
    async function extractOne(itemId) {
        const ok = await extractOnce(itemId);
        if (ok) return ok;
        await new Promise(r => setTimeout(r, 1500));
        return extractOnce(itemId);
    }

    function settle(itemId, ok) {
        const entry = pending.get(itemId);
        pending.delete(itemId);
        if (!entry) return;
        entry.imgs.forEach((img) => {
            if (!img.isConnected) return;   // 页面已移除该元素
            img.classList.remove('mp-thumb-pending');
            if (ok) {
                // 命中缓存后重设 src（带版本参数防 404 缓存）
                img.onerror = null;
                img.src = Bridge.thumbUrl(itemId) + '&v=' + (++seq);
            } else if (typeof entry.fallback === 'function') {
                entry.fallback(img);
            }
        });
    }

    function pump() {
        while (active < WORKERS && queue.length) {
            const itemId = queue.shift();
            if (!pending.has(itemId)) continue;
            active++;
            extractOne(itemId).then(
                (ok) => { active--; settle(itemId, ok); pump(); },
                () => { active--; settle(itemId, false); pump(); }
            );
        }
    }

    return {
        // 请求为某视频生成封面；同一 itemId 合并（多 img 同时 404 只抽一次）。
        // priority=true 时插队到队首（播放中的视频封面优先出图）。
        request(itemId, img, onFallback, priority = false) {
            if (!itemId || !img) return;
            let entry = pending.get(itemId);
            if (!entry) {
                entry = { imgs: new Set(), fallback: onFallback };
                pending.set(itemId, entry);
                if (priority) queue.unshift(itemId);
                else queue.push(itemId);
            }
            entry.imgs.add(img);
            img.classList.add('mp-thumb-pending');
            pump();
        },
    };
})();

// 挂到 window：utils.js / player-core.js 等通过 window.MediaFrameExtractor 访问
window.MediaFrameExtractor = MediaFrameExtractor;
