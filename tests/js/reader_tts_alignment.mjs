// 朗读播放器的「音频与句子必须对得上」契约（reader-tts.js）。
//
// 真实事故（2026-10-04 07:27~07:28 的日志）：用户在读第三段时听到了第二段的音频，
// 同一句音频还被请求了两次。机制是预取结果**按序号**存：`_prefetchNext()` 的回调跑到时
// `this.index` 已经前进，于是第 N 句的音频被塞进第 N+1 格；`_playCurrent()` 又只按序号取用，
// 从不核对"这是不是这一句的"。合成一次要 7 秒（慢端点），这段窗口足够让序号跑掉。
//
// 这里用真实的 reader-tts.js + 一个记录调用的假 Bridge 复现，并守住三条：
//   1. 预取结果与当前句指纹不符 → 丢弃并重取（不能播别的句子）；
//   2. 指纹相符 → 用预取结果，不打第二次请求（预取的意义就在这）；
//   3. 音频元素挂的句子与当前位置不符时，迟到的 ended 不推进位置。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..', '..', 'plugins', 'document-reader', 'frontend');

const { document, makeEl } = createDom();
const audio = makeEl('audio');
globalThis.document = document;
globalThis.window = {
  Icons: { html: () => '' },
  // 固定摘要替身：同一文本必得同一值（真实实现是 SHA-1，长度与形状一致）
  crypto: {
    subtle: {
      digest: async (_alg, bytes) => {
        const text = new TextDecoder().decode(bytes);
        let a = 0;
        let b = 0;
        for (let i = 0; i < text.length; i += 1) {
          a = (a * 31 + text.charCodeAt(i)) | 0;
          b = (b * 17 + text.charCodeAt(i) * 7) | 0;
        }
        const hex = `${(a >>> 0).toString(16).padStart(8, '0')}${(b >>> 0).toString(16).padStart(8, '0')}`;
        const out = new Uint8Array(20);
        for (let i = 0; i < 20; i += 1) out[i] = parseInt(hex[i % 16] + hex[(i + 3) % 16], 16) || 0;
        return out.buffer;
      },
    },
  },
};
globalThis.TextEncoder = TextEncoder;
globalThis.TextDecoder = TextDecoder;
globalThis.Toast = { info() {}, error() {}, success() {} };
globalThis.fetch = async () => ({ ok: true });

const calls = [];
globalThis.Bridge = {
  async call(method, ...args) {
    calls.push({ method, args });
    const [text] = args;
    return { url: `/file?path=${encodeURIComponent(`C:\\tts\\${text.slice(0, 6)}.mp3`)}`,
             engine: 'openai', marks: [], cached: false };
  },
};

// 播放元素：不需要真的解码，只记录被挂上的地址
document.createElement = (tag) => {
  const el = tag === 'audio' ? audio : makeEl(tag);
  return el;
};
let played = 0;
audio.play = async () => { played += 1; };
audio.pause = () => {};
audio.removeAttribute = () => {};

const src = fs.readFileSync(path.join(FRONTEND, 'js', 'reader-tts.js'), 'utf8');
const clsStart = src.indexOf('class ReaderTts');
const ReaderTts = new Function(`${src.slice(clsStart)}; return ReaderTts;`)();

const app = {
  engine: { chapters: [0, 1], getChapterContext: () => null },
  debug: { status_debug: true },
  _updateStatus() {},
  _saveCurrentProgress() {},
};

function makePlayer(pieces) {
  calls.length = 0;
  played = 0;
  const tts = new ReaderTts(app);
  tts.pieces = pieces;
  tts.index = 0;
  tts._seq = 1;
  tts._chapterElement = null;      // 不画高亮：这里只验音频与句子的对应
  tts._prefetched = new Map();
  tts._state = 'loading';
  return tts;
}

const P1 = { text: '第一句。', start: 0, end: 4 };
const P2 = { text: '第二句。', start: 4, end: 8 };
const P3 = { text: '第三句。', start: 8, end: 12 };

// 1) 预取结果属于别的句子 → 丢弃，重取当前句，绝不能把别人的音频挂上去
{
  const tts = makePlayer([P1, P2, P3]);
  const wrongKey = await tts._cacheKey('第二句。');
  const rightKey = await tts._cacheKey('第三句。');
  assert.notEqual(wrongKey, rightKey, '不同句子的指纹必须不同');
  tts.index = 2;
  tts._prefetched.set(2, { cacheKey: wrongKey, result: { url: '/file?path=wrong.mp3', engine: 'openai' } });

  await tts._playCurrent();

  assert.equal(calls.length, 1, '丢弃错位预取后应当重取一次');
  assert.equal(calls[0].args[0], '第三句。', '重取的内容必须是当前句');
  assert.equal(calls[0].args[1], rightKey, '请求要带当前句的指纹');
  assert.equal(audio.src, `/file?url=`.replace('?url=', '?path=') + encodeURIComponent('C:\\tts\\第三句。.mp3'),
    '挂上去的音频必须是当前句的');
  assert.ok(!audio.src.includes('wrong'), '错位的预取地址绝不能进播放器');
  assert.equal(tts._audioIndex, 2, '要记下当前音频属于哪一句');
}

// 2) 预取结果与当前句一致 → 直接用，不再请求（预取的意义）
{
  const tts = makePlayer([P1, P2, P3]);
  tts.index = 1;
  const key = await tts._cacheKey('第二句。');
  tts._prefetched.set(1, { cacheKey: key, result: { url: '/file?path=prefetched.mp3', engine: 'openai' } });

  await tts._playCurrent();

  // 开播后本来就会预取下一句，所以这里只看"当前句"有没有被重新请求
  const own = calls.filter((call) => call.args[2] === 1);
  assert.equal(own.length, 0, '指纹相符时不该再为当前句打一次合成请求');
  assert.equal(audio.src, '/file?path=prefetched.mp3', '应当直接用预取结果');
  assert.equal(calls.length, 1, '只应剩"预取下一句"这一次调用');
}

// 3) 迟到的 ended：音频属于别的句子时不推进位置
{
  const tts = makePlayer([P1, P2, P3]);
  tts._state = 'playing';
  tts.index = 0;
  tts._audioIndex = 2;                       // 音频已经换到第 3 句，位置却还停在第 1 句
  tts._playCurrent = async () => { played += 100; };
  tts._onEnded();
  assert.equal(tts.index, 0, '迟到的 ended 不能推进播放位置');

  tts._audioIndex = 0;                       // 同一声道的正常 ended：照旧前进
  tts._onEnded();
  assert.equal(tts.index, 1, '同一句的 ended 应当前进一句');
}

// 4) 预取落格时用的指纹，必须与取用时比对的那把一致
{
  const tts = makePlayer([P1, P2, P3]);
  tts.index = 0;
  tts._playCurrent = async () => {};
  tts._prefetchNext();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const entry = tts._prefetched.get(1);
  assert.ok(entry, '预取结果应当落在第 2 格');
  assert.equal(entry.cacheKey, await tts._cacheKey('第二句。'),
    '落格时的指纹必须等于该句的指纹，否则取用时永远对不上、预取等于白做');
}

console.log('reader_tts_alignment: OK');
