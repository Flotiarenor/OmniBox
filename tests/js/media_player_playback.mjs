// 媒体播放器"播放起点 / 进度记忆"逻辑的无头验证脚本（由 test_media_player_playback_js.py 调用）。
//
// 背景：曲目播完后 currentTime 停在 duration，切歌瞬间 _loadItem 先 _saveProgress()，
// 把"已播完"的位置写回 localStorage，覆盖掉 ended 里的 clear()；下次点击该曲目就续播到
// 末尾，元素停在末尾 play() 无声 —— 表现为"点击曲目从最后开始 / 无法播放"。
//
// 这里用 stub 的媒体元素 + 内存 localStorage 跑真实 player-core.js，锁住：
//   1. 播完切歌不写回末尾位置；2. 残留的末尾记录不再续播；3. 剩余不足 5s 从头；
//   4. 卡在末尾时播放键退回首帧；5. 视频条目同规则。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const JS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../plugins/media-player/frontend/js');

// ---- 最小事件目标 ----
class Emitter {
    constructor() { this._l = new Map(); }
    addEventListener(type, fn) {
        if (!this._l.has(type)) this._l.set(type, []);
        this._l.get(type).push(fn);
    }
    removeEventListener(type, fn) {
        const arr = this._l.get(type) || [];
        const i = arr.indexOf(fn);
        if (i >= 0) arr.splice(i, 1);
    }
    dispatch(type) {
        (this._l.get(type) || []).slice().forEach(fn => fn.call(this, { type }));
    }
}

let DURATION = 100;
class FakeMedia extends Emitter {
    constructor() {
        super();
        this._src = '';
        this._currentTime = 0;
        this.duration = NaN;
        this.paused = true;
        this.ended = false;
        this.volume = 1;
        this.muted = false;
        this.preload = '';
        this.error = null;
        this.playCalls = 0;
    }
    get src() { return this._src; }
    set src(v) { this._src = v; }
    get currentTime() { return this._currentTime; }
    set currentTime(v) {
        // 真实元素行为：seek 到末尾即进入 ended 并停止播放
        this._currentTime = v;
        if (isFinite(this.duration) && v >= this.duration) {
            this.paused = true;
            this.ended = true;
        } else if (v < (this.duration || Infinity)) {
            this.ended = false;
        }
    }
    load() {
        // 真实元素 load() 会重置播放位置并清 ended，metadata 异步到达
        this._currentTime = 0;
        this.ended = false;
        this.duration = NaN;
        setTimeout(() => {
            this.duration = DURATION;
            this.dispatch('loadedmetadata');
        }, 0);
    }
    play() { this.playCalls++; this.paused = false; if (!this.ended) this.dispatch('play'); return Promise.resolve(); }
    pause() { if (!this.paused) { this.paused = true; this.dispatch('pause'); } }
    removeAttribute(name) { if (name === 'src') this._src = ''; }
}

const audioEl = new FakeMedia();
const videoEl = new FakeMedia();
const memory = new Map();
const localStorage = {
    getItem: k => (memory.has(k) ? memory.get(k) : null),
    setItem: (k, v) => memory.set(k, String(v)),
    removeItem: k => memory.delete(k),
};

const ctx = vm.createContext({
    window: { Utils: undefined, MediaFrameExtractor: undefined },
    document: { getElementById: id => (id === 'video-player' ? videoEl : null) },
    localStorage,
    Bridge: {
        originalUrl: p => '/file?path=' + encodeURIComponent(p),
        thumbUrl: id => '/thumbs/' + id,
        call: () => Promise.resolve({}),
        callPlugin: () => Promise.resolve({}),
    },
    Toast: { error() { }, info() { }, success() { } },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    Audio: function () { return audioEl; },
    Number, Math, JSON, String, Boolean, Object, Array, Promise,
    isFinite, isNaN, NaN, Infinity, Error,
});

for (const file of ['utils.js', 'progress-store.js', 'player-core.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, file), 'utf8'), ctx, { filename: file });
}
const { MediaPlayerCore, MediaProgressStore } = vm.runInContext(
    'globalThis.__mp = { MediaPlayerCore, MediaProgressStore }; globalThis.__mp', ctx);

const tick = () => new Promise(resolve => setTimeout(resolve, 5));

let failures = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

function reset() {
    memory.clear();
    for (const el of [audioEl, videoEl]) {
        el._l.clear();   // 元素实例在场景间复用，丢弃上一个 core 的监听器
        el.pause();
        el._src = '';
        el._currentTime = 0;
        el.duration = NaN;
        el.ended = false;
        el.playCalls = 0;
        el.error = null;
    }
}

const A = { id: 'a', kind: 'audio', title: 'A', path: '/m/a.mp3' };
const B = { id: 'b', kind: 'audio', title: 'B', path: '/m/b.mp3' };
const V = { id: 'v', kind: 'video', title: 'V', path: '/m/v.mp4' };

function makeCore(resumeMode) {
    const app = { onTrackChange() { }, onPlayStateChange() { }, onTimeUpdate() { }, settings: {} };
    const core = new MediaPlayerCore(app);
    core.setResumeMode(resumeMode);
    return core;
}

console.log('场景 1：A 播完后自动跳 B，A 的进度存储应保持为空（历史 bug：被写成 duration）');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    core.setQueue([A, B], 0, true);
    await tick();
    audioEl._currentTime = 100;
    audioEl.ended = true;
    audioEl.paused = true;
    audioEl.dispatch('ended');
    await tick();
    check('ended 后进度记录为空', MediaProgressStore.get(A) === 0,
        `实际 ${JSON.stringify(memory.get('omniboxMediaProgress'))}`);
    check('已切到 B', core.currentItem && core.currentItem.id === 'b');
}

console.log('场景 2：默认（从头播放）再次点击 A，元素不应被 seek 到末尾');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    core.setQueue([A, B], 0, true);
    await tick();
    audioEl._currentTime = 100;
    audioEl.ended = true;
    audioEl.dispatch('ended');
    await tick();                    // 现在停在 B
    core.playIndex(0, true);         // 用户重新点击 A
    await tick();
    check('未续播 seek 到末尾', audioEl._currentTime === 0, `实际 ${audioEl._currentTime}`);
    check('元素不处于 ended 状态', audioEl.ended === false);
}

console.log('场景 3：存储中残留"已播完"位置时，续播模式也应从头开始');
{
    reset();
    DURATION = 100;
    MediaProgressStore.save('a', 100);
    const core = makeCore('resume');
    core.setQueue([A], 0, true);
    await tick();
    check('未 seek 到末尾', audioEl._currentTime === 0, `实际 ${audioEl._currentTime}`);
    check('残留记录已清除', MediaProgressStore.get(A) === 0);
}

console.log('场景 4：续播模式边界（时长 100s，"低于 5s 从头"）');
{
    const cases = [
        [90, 90, '剩余 10s：续播到 90s'],
        [97, 0, '剩余 3s：从头开始'],
        [95, 95, '剩余 5s（不低于 5s）：续播到 95s'],
        [94.9, 94.9, '剩余 5.1s：续播到 94.9s'],
    ];
    for (const [saved, expected, name] of cases) {
        reset();
        DURATION = 100;
        MediaProgressStore.save('a', saved);
        const core = makeCore('resume');
        core.setQueue([A], 0, true);
        await tick();
        check(name, Math.abs(audioEl._currentTime - expected) < 1e-6, `实际 ${audioEl._currentTime}`);
    }
}

console.log('场景 5：元素卡在末尾（ended）时 togglePlay 应退回首帧');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    core.setQueue([A], 0, true);
    await tick();
    audioEl._currentTime = 100;
    audioEl.ended = true;
    audioEl.paused = true;
    core.togglePlay();
    check('currentTime 归零', audioEl._currentTime === 0, `实际 ${audioEl._currentTime}`);
    check('触发了 play()', audioEl.playCalls >= 2, `play 次数 ${audioEl.playCalls}`);
}

console.log('场景 6：播放到剩余不足 5s 时，进度保存不应留下接近末尾的记录');
{
    reset();
    DURATION = 100;
    const core = makeCore('resume');
    core.setQueue([A], 0, true);
    await tick();
    audioEl._currentTime = 96;
    core._saveProgress();
    check('剩余 4s 不落盘', MediaProgressStore.get(A) === 0,
        `实际 ${JSON.stringify(memory.get('omniboxMediaProgress'))}`);
    audioEl._currentTime = 80;
    core._saveProgress();
    check('剩余 20s 正常落盘', MediaProgressStore.get(A) === 80);
}

console.log('场景 7：视频条目走同一套规则');
{
    reset();
    DURATION = 300;
    const core = makeCore('restart');
    core.setQueue([V], 0, true);
    await tick();
    check('使用 video 元素', videoEl._src.includes('v.mp4'));
    videoEl._currentTime = 300;
    videoEl.ended = true;
    videoEl.dispatch('ended');
    await tick();
    check('视频播完后不写回末尾', MediaProgressStore.get(V) === 0,
        `实际 ${JSON.stringify(memory.get('omniboxMediaProgress'))}`);
    MediaProgressStore.save('v', 299);
    core.playIndex(0, true);
    await tick();
    check('残留 299s 也不续播到末尾', videoEl._currentTime === 0, `实际 ${videoEl._currentTime}`);
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
