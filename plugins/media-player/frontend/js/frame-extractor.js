// ===== 视频封面前端抽帧器：隐藏 <video> seek + canvas 截图 =====
// 与播放共用浏览器解码器：系统能放的视频就能抽帧，解码能力与播放严格对齐。
// 抽帧结果经 media_put_thumb 回写 ThumbCache，之后 /thumbs/<id> 直接命中缓存。
// 并发 2 个 worker + 按 itemId 去重；所有等待均有超时，避免队列死锁。
// 注意：必须挂到 window（const 声明的全局变量不属于 window，window.MediaFrameExtractor 会是 undefined）。
const MediaFrameExtractor = (function () {
    const WORKERS = 2;                 // 并发抽帧数（浏览器解码资源有限，2 个较稳）
    const EVENT_TIMEOUT = 15000;       // loadedmetadata / seeked 超时
    const RAF_TIMEOUT = 3000;          // rAF 挂起超时（iframe 不可见时 rAF 不触发）
    const MAX_QUEUE = 100;             // 队列上限：超出丢弃队尾，防止快速滚动压爆解码队列
    const FAIL_BACKOFF_MS = 10 * 60 * 1000;  // 坏文件退避：硬失败后 10 分钟内不再空转
    const LOCK_KEY = 'omniboxMediaExtractLock';
    const LOCK_TTL = 60 * 1000;        // 跨标签页抽帧锁 TTL（防止双标签页并发抽同一视频）

    const videos = [];                 // video 元素池
    let queue = [];
    let active = 0;
    let seq = 0;
    const pending = new Map();         // itemId -> { imgs: Set, fallback: fn }
    const failedAt = new Map();        // itemId -> 上次硬失败时间戳（退避用）

    // ---- 失败退避：损坏文件不反复解码 ----
    function isBlocked(itemId) {
        const t = failedAt.get(itemId);
        return t !== undefined && (Date.now() - t) < FAIL_BACKOFF_MS;
    }

    // ---- 跨标签页抽帧锁（localStorage + TTL，尽力而为，不做强一致） ----
    function tryLock(itemId) {
        try {
            const raw = JSON.parse(localStorage.getItem(LOCK_KEY) || '{}');
            const t = raw[itemId];
            if (t && (Date.now() - t) < LOCK_TTL) return false;
            raw[itemId] = Date.now();
            for (const k of Object.keys(raw)) {
                if (Date.now() - raw[k] > LOCK_TTL) delete raw[k];
            }
            localStorage.setItem(LOCK_KEY, JSON.stringify(raw));
            return true;
        } catch (e) { return true; }
    }

    function releaseLock(itemId) {
        try {
            const raw = JSON.parse(localStorage.getItem(LOCK_KEY) || '{}');
            delete raw[itemId];
            localStorage.setItem(LOCK_KEY, JSON.stringify(raw));
        } catch (e) { }
    }

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

    // 取帧位置：已知时长取 10%（钳 [0.5, 120]）；时长未知（如部分 mkv）依次尝试 10/30/60s
    function pickSeekPositions(dur) {
        if (isFinite(dur) && dur > 0) return [Math.max(0.5, Math.min(dur * 0.1, 120))];
        return [10, 30, 60];
    }

    // 画面是否基本全黑（片头黑场/字幕时换位置重试，避免黑屏封面）
    function frameIsBlack(video, vw, vh) {
        try {
            const sample = document.createElement('canvas');
            const sw = 64;
            const sh = Math.max(1, Math.round(vh * sw / vw));
            sample.width = sw;
            sample.height = sh;
            const sctx = sample.getContext('2d', { willReadFrequently: true });
            sctx.drawImage(video, 0, 0, sw, sh);
            const data = sctx.getImageData(0, 0, sw, sh).data;
            let sum = 0;
            for (let i = 0; i < data.length; i += 4) sum += data[i] + data[i + 1] + data[i + 2];
            return sum / ((data.length / 4) * 3) < 16;
        } catch (e) { return false; }
    }

    // 主播放器是否正在画面模式播放该视频（是则主 <video> 已解码同一文件，可直接取帧）
    function mainPlayerFor(itemId) {
        try {
            const app = window.mediaPlayerApp;
            if (!app || !app.core || !app.core.videoMode) return null;
            const cur = app.core.currentItem;
            if (!cur || cur.id !== itemId || cur.kind !== 'video') return null;
            const main = document.getElementById('video-player');
            if (main && main.videoWidth > 0 && main.videoHeight > 0) return main;
        } catch (e) { }
        return null;
    }

    // 单次抽帧尝试（含资源获取/释放）；返回 dataUrl / 'cached' / 'locked' / null
    async function extractOnce(itemId) {
        // 已被其他来源写入缓存（另一标签页/播放中抽帧先完成）：直接回填，不再解码
        try {
            const miss = await Bridge.call('media_thumb_missing', [itemId]);
            if (miss && Array.isArray(miss.missing) && miss.missing.length === 0) return 'cached';
        } catch (e) { }

        // 播放中的视频：主元素零额外解码直接取帧
        const main = mainPlayerFor(itemId);
        if (main) {
            const scale = Math.min(1, 640 / main.videoWidth);
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(main.videoWidth * scale));
            canvas.height = Math.max(1, Math.round(main.videoHeight * scale));
            canvas.getContext('2d').drawImage(main, 0, 0, canvas.width, canvas.height);
            const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
            if (dataUrl && dataUrl.length >= 100) {
                const res = await Bridge.call('media_put_thumb', itemId, dataUrl).catch(() => null);
                return res && res.success ? dataUrl : null;
            }
        }

        // 隐藏 video 抽帧（跨标签页锁防重复解码）
        if (!tryLock(itemId)) return 'locked';
        const v = acquireVideo();
        try {
            const item = await Bridge.call('media_get_item', itemId).catch(() => null);
            if (!item || !item.path || item.kind !== 'video') return null;
            v.src = MPUtils.mediaUrl(item.path);
            if (!await waitEvent(v, 'loadedmetadata', EVENT_TIMEOUT)) return null;
            const dur = v.duration || 0;
            const positions = pickSeekPositions(dur);
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');
            let drew = 0;
            for (const seek of positions) {
                v.currentTime = seek;
                if (!await waitEvent(v, 'seeked', EVENT_TIMEOUT)) break;
                if (!await nextFrame()) break;
                if (!v.videoWidth || !v.videoHeight) break;
                const scale = Math.min(1, 640 / v.videoWidth);
                canvas.width = Math.max(1, Math.round(v.videoWidth * scale));
                canvas.height = Math.max(1, Math.round(v.videoHeight * scale));
                ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
                drew++;
                if (!frameIsBlack(v, canvas.width, canvas.height)) break;
            }
            if (!drew) return null;
            const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
            if (!dataUrl || dataUrl.length < 100) return null;
            const res = await Bridge.call('media_put_thumb', itemId, dataUrl).catch(() => null);
            return res && res.success ? dataUrl : null;
        } catch (e) {
            return null;
        } finally {
            releaseVideo(v);
            releaseLock(itemId);
        }
    }

    // 抽帧（真失败重试一次：文件首次打开慢等瞬态场景；
    // 跨标签页锁占用则稍等重试一次，仍占用则软失败——不记退避）
    async function extractOne(itemId) {
        const r1 = await extractOnce(itemId);
        if (r1 && r1 !== 'locked') return r1;
        const delay = r1 === 'locked' ? 3000 : 1500;
        await new Promise(r => setTimeout(r, delay));
        const r2 = await extractOnce(itemId);
        return (r2 && r2 !== 'locked') ? r2 : (r2 === 'locked' ? 'locked' : null);
    }

    function settle(itemId, result, hard) {
        const entry = pending.get(itemId);
        pending.delete(itemId);
        if (result && result !== 'locked') failedAt.delete(itemId);
        else if (hard) failedAt.set(itemId, Date.now());
        if (!entry) return;
        entry.imgs.forEach((img) => {
            if (!img.isConnected) return;   // 页面已移除该元素
            img.classList.remove('mp-thumb-pending');
            if (result && result !== 'locked') {
                if (result === 'cached') {
                    // 缓存已存在（另一标签页/后端 ffmpeg 刚写入）：回读一次 /thumbs
                    // 万一仍失败则走 fallback 兜底，不留静默破图
                    img.onerror = () => { if (typeof entry.fallback === 'function') entry.fallback(img); };
                    img.src = Bridge.thumbUrl(itemId) + '&v=' + (++seq);
                } else {
                    // 抽帧成功：dataUrl 直接回填，省掉「回读 /thumbs」一次往返；
                    // put_thumb 已异步写库，下次浏览直接命中 SQLite 缓存
                    img.src = result;
                }
            } else if (typeof entry.fallback === 'function') {
                entry.fallback(img);
            }
        });
    }

    function pump() {
        // 页面隐藏时 rAF 不触发、抽帧必失败：暂停派发，恢复可见后继续
        if (document.hidden) return;
        while (active < WORKERS && queue.length) {
            const itemId = queue.shift();
            if (!pending.has(itemId)) continue;
            active++;
            extractOne(itemId).then(
                (res) => {
                    active--;
                    if (res === 'locked') settle(itemId, 'locked', false);  // 跨标签页占用：软失败
                    else settle(itemId, res, !res);                          // dataUrl/'cached' 成功 / null 硬失败
                    pump();
                },
                () => { active--; settle(itemId, null, true); pump(); }
            );
        }
    }

    // 页面重新可见时恢复派发
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) pump();
    });

    return {
        // 请求为某视频生成封面；同一 itemId 合并（多 img 同时 404 只抽一次）。
        // priority=true 时插队到队首（播放中的视频封面优先出图）。
        request(itemId, img, onFallback, priority = false) {
            if (!itemId || !img) return;
            // 失败退避：近期硬失败过的文件直接降级，不重复解码空转
            if (isBlocked(itemId)) {
                if (typeof onFallback === 'function') onFallback(img);
                return;
            }
            let entry = pending.get(itemId);
            if (!entry) {
                entry = { imgs: new Set(), fallback: onFallback };
                pending.set(itemId, entry);
                if (queue.indexOf(itemId) === -1) {
                    if (priority) queue.unshift(itemId);
                    else queue.push(itemId);
                }
            }
            // 已排队但请求升级为 priority：移到队首（不等整队，见 D1）
            if (priority) {
                const qi = queue.indexOf(itemId);
                if (qi > 0) { queue.splice(qi, 1); queue.unshift(itemId); }
            }
            // 队列上限：丢弃队尾（优先级最低的最近加入项），其封面直接降级
            while (queue.length > MAX_QUEUE) {
                const dropped = queue.pop();
                if (dropped && dropped !== itemId) settle(dropped, false, false);
                else break;
            }
            entry.imgs.add(img);
            img.classList.add('mp-thumb-pending');
            pump();
        },
    };
})();

// 挂到 window：utils.js / player-core.js 等通过 window.MediaFrameExtractor 访问
window.MediaFrameExtractor = MediaFrameExtractor;
