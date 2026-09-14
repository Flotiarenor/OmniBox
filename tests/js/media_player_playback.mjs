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

// 恢复播放用例用：媒体库按 id 提供的条目（各场景自行填充）
let itemsById = {};

const ctx = vm.createContext({
    window: { Utils: undefined, MediaFrameExtractor: undefined },
    document: { getElementById: id => (id === 'video-player' ? videoEl : null) },
    localStorage,
    Bridge: {
        originalUrl: p => '/file?path=' + encodeURIComponent(p),
        thumbUrl: id => '/thumbs/' + id,
        call: (method, ...args) => {
            if (method === 'media_get_items') {
                return Promise.resolve((args[0] || []).map(id => itemsById[id]).filter(Boolean));
            }
            return Promise.resolve({});
        },
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
const C = { id: 'c', kind: 'audio', title: 'C', path: '/m/c.mp3' };
const D = { id: 'd', kind: 'audio', title: 'D', path: '/m/d.mp3' };
const V = { id: 'v', kind: 'video', title: 'V', path: '/m/v.mp4' };

function makeCore(resumeMode) {
    const app = {
        onTrackChange() { }, onPlayStateChange() { }, onTimeUpdate() { },
        updatePlayModeUI() { }, updateVolumeUI() { }, settings: {},
    };
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

console.log('场景 8：随机模式下 next/prev 沿同一份排列进退（历史行为：每次随机取下标）');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    const items = [A, B, C, D].map((item, i) => ({ ...item, id: `s${i}`, title: `S${i}` }));
    core.setQueue(items, 0, true);
    await tick();
    core.playMode = 1;              // 等价于点模式按钮切到随机播放
    core._invalidateShuffleOrder();
    core.playIndex(0, true);        // 直接选曲：排列以该条目为首位
    await tick();

    const start = core.currentItem.id;
    const forward = [];
    for (let i = 0; i < 3; i++) {
        core.next(false);
        await tick();
        forward.push(core.currentItem.id);
    }
    const back = [];
    for (let i = 0; i < 3; i++) {
        core.prev();
        await tick();
        back.push(core.currentItem.id);
    }
    // 播放路径为 [start, ...forward]，回退 3 次应依次回到 forward[1]、forward[0]、start
    const expectedBack = forward.slice(0, -1).reverse().concat(start);
    check('随机模式：连续前进 3 次互不重复', new Set(forward).size === 3, `forward=${forward}`);
    check('随机模式：上一首按原路返回（回退序列 = 播放路径的逆序）',
        JSON.stringify(back) === JSON.stringify(expectedBack),
        `forward=${forward} back=${back} 期望=${expectedBack}`);
    check('随机模式：回退到排列首位即当前条目', core.currentItem.id === 's0', `实际 ${core.currentItem.id}`);
}

console.log('场景 9：随机模式走完一轮后重新生成一段，不立刻重播刚播完的条目');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    const items = [A, B, C, D].map((item, i) => ({ ...item, id: `w${i}`, title: `W${i}` }));
    core.setQueue(items, 0, true);
    await tick();
    core.playMode = 1;
    core._invalidateShuffleOrder();
    core.playIndex(0, true);
    await tick();

    const played = [core.currentItem.id];
    for (let i = 0; i < 4; i++) {
        core.next(false);
        await tick();
        played.push(core.currentItem.id);
    }
    check('一轮内 4 条各出现一次', new Set(played.slice(0, 4)).size === 4, `played=${played}`);
    check('进入下一轮时不立刻重播刚播完的条目', played[4] !== played[3], `played=${played}`);
}

console.log('场景 10：恢复播放按持久化队列恢复整条队列（历史行为：只恢复当前条目）');
{
    reset();
    DURATION = 100;
    const core = makeCore('restart');
    const items = [A, B, C, D].map((item, i) => ({ ...item, id: `r${i}`, title: `R${i}` }));
    itemsById = Object.fromEntries(items.map(i => [i.id, i]));

    await core.restorePlayback(items[1], {
        item_id: 'r1', loop_mode: 'all', shuffle: false, volume: 1, video_mode: 'video',
        queue_ids: items.map(i => i.id), queue_index: 1,
    });
    await tick();
    check('恢复后队列长度为持久化长度', core.queue.length === 4, `实际 ${core.queue.length}`);
    check('恢复后下标指向当前条目', core.currentIndex === 1 && core.currentItem.id === 'r1',
        `index=${core.currentIndex} item=${core.currentItem && core.currentItem.id}`);

    core.next(false);
    await tick();
    check('恢复后下一首跟随持久化队列', core.currentItem.id === 'r2', `实际 ${core.currentItem.id}`);
    core.prev();
    await tick();
    check('恢复后上一首回到持久化队列的上一首', core.currentItem.id === 'r1', `实际 ${core.currentItem.id}`);
    itemsById = {};
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
