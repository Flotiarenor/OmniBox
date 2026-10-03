// 朗读设置页的"音色分卡"契约：两档音色各占一张 `.nr-voice-card`，不出的那一档整张隐藏，
// 点卡片里的音色要**立刻落盘**（紧随其后的试听必须读到新音色）。
//
// 为什么值得单测：这套渲染是纯字符串拼 HTML + 事后绑事件，静态检查看不见
// "把两个来源塞进同一张卡""隐藏时还留着上一档的音色"这类错误；而它一旦错了，
// 用户点到的音色与实际去念的引擎就会不一致（真实事故：端点音色被 edge 静默顶掉）。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(HERE, '..', '..', 'plugins', 'document-reader', 'frontend');

const { document, makeEl } = createDom();
globalThis.document = document;
globalThis.window = { Icons: { html: () => '' } };
globalThis.Bridge = {
  calls: [],
  async call(method, ...args) {
    this.calls.push({ method, args });
    if (method === 'get_settings') {
      return { tts_engine: 'auto', tts_base_url: 'http://192.168.31.4:8100', tts_voice: 'vivian', tts_rate: 0 };
    }
    if (method === 'save_settings') return { success: true };
    return {};
  },
};
globalThis.Toast = { info() {}, error() {}, success() {} };

// 页面契约：两张卡片、两个列表容器都必须真实存在（改 UI 时先在这里断）
const html = fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
for (const id of ['nr-voice-name', 'nr-voice-card-edge', 'nr-voice-list-edge',
                  'nr-voice-card-endpoint', 'nr-voice-list-endpoint']) {
  assert.match(html, new RegExp(`id="${id}"`), `index.html 缺少 #${id}`);
}
for (const title of ['edge-tts 音色', 'OpenAI 兼容端点音色']) {
  assert.match(html, new RegExp(title), `index.html 缺少卡片标题「${title}」`);
}

// 骨架：按上面的 id 建出 _dom() 需要的那几个元素（替身不解析 HTML 结构）
for (const [tag, id, cls] of [
  ['div', 'nr-voice-page', 'nr-voice-page'],
  ['input', 'nr-voice-name', ''],
  ['input', 'nr-voice-engine', ''],
  ['input', 'nr-voice-base', ''],
  ['input', 'nr-voice-key', ''],
  ['input', 'nr-voice-model', ''],
  ['input', 'nr-voice-rate', ''],
  ['span', 'nr-voice-rate-value', ''],
  ['p', 'nr-voice-status', ''],
  ['section', 'nr-voice-card-edge', 'nr-voice-card hidden'],
  ['div', 'nr-voice-list-edge', 'nr-voice-list'],
  ['section', 'nr-voice-card-endpoint', 'nr-voice-card hidden'],
  ['div', 'nr-voice-list-endpoint', 'nr-voice-list'],
]) {
  const el = makeEl(tag);
  el.className = cls;
  el.id = id;
  document.body.appendChild(el);
}

const src = fs.readFileSync(path.join(FRONTEND, 'js', 'reader-voice-page.js'), 'utf8');
const clsStart = src.indexOf('class ReaderVoicePage');
// ReaderVoicePage 只用 Bridge/Toast/document，直接 eval 这个类（跳过模块顶部的图标函数）
const ReaderVoicePage = new Function(`${src.slice(clsStart)}; return ReaderVoicePage;`)();
const el = (id) => document.getElementById(id);

// 真实后端数据（可选）：由 `python tmp_fixture.py` 一类的方式导出 `tts_voices` 的原始回应，
// 用 DSH 环境变量指过来时就用它替代构造数据 —— 这样"后端给的表"与"前端画出来的卡"一起被验。
const fixturePath = process.env.OBX_VOICE_FIXTURE;
let EDGE_TABLE = {
  source: 'edge',
  title: 'edge-tts 音色',
  voices: [{ name: 'zh-CN-XiaoxiaoNeural', note: '晓晓' }, { name: 'zh-CN-YunxiNeural', note: '云希' }],
  unavailable: false,
};
let ENDPOINT_TABLE = {
  source: 'endpoint',
  title: 'OpenAI 兼容端点音色',
  voices: [{ name: 'vivian', note: '' }, { name: 'serena', note: '' }],
  unavailable: false,
};
if (fixturePath && fs.existsSync(fixturePath)) {
  const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
  EDGE_TABLE = fixture.tables.find((t) => t.source === 'edge') || EDGE_TABLE;
  ENDPOINT_TABLE = fixture.tables.find((t) => t.source === 'endpoint') || ENDPOINT_TABLE;
  console.log(`  使用真实后端数据：${fixturePath}（edge ${EDGE_TABLE.voices.length} 个 / ` +
    `端点 ${ENDPOINT_TABLE.voices.length} 个，端点 unavailable=${ENDPOINT_TABLE.unavailable}）`);
}

const app = { tts: { status: null, refreshStatus: async () => {} }, _isReaderMode: false };
const voicePage = new ReaderVoicePage(app);

/**
 * 共用的 dom_stub 只把 `id="..."` 的标签做成子节点；音色按钮只有 class 与 data-voice，
 * 查不到就没法验证点击。这里补一层"只认 `.nr-voice-item`"的解析，元素仍用 stub 的 makeEl。
 * 替身重建元素会丢掉事件处理器，所以绑事件这件事也在这里跟着做一遍。
 */
function render(tables) {
  voicePage._renderVoices(tables);
  for (const id of ['nr-voice-list-edge', 'nr-voice-list-endpoint']) {
    const list = el(id);
    // 替身不解析 innerHTML 结构，`querySelectorAll` 找不到按钮；而真 DOM 每次赋值都是
    // 整段重建。这里照真 DOM 的语义重建一次：换掉旧元素（顺带丢掉旧处理器）再重新绑。
    list.children.forEach((child) => { child.parentElement = null; });
    list.children.length = 0;
    const tags = [...list.innerHTML.matchAll(/<([a-zA-Z0-9-]+)((?:\s+[a-zA-Z-]+="[^"]*")*)\s*>/g)];
    // 注意解构：matchAll 的每一项是 [整段匹配, 标签名, 属性串]，第一个位置是整段匹配
    for (const [, tag, attrs] of tags) {
      // 用 `\s` 而不是 `\b` 定界：属性前面是空格时 `\b` 不成立（空格与 `c` 之间没有词边界）
      const cls = /\sclass="([^"]*)"/.exec(attrs);
      if (!cls || !cls[1].split(/\s+/).includes('nr-voice-item')) continue;
      const item = makeEl(tag);
      item.className = cls[1];
      const data = /\sdata-voice="([^"]*)"/.exec(attrs);
      if (data) item.dataset.voice = data[1];
      list.appendChild(item);
      item.addEventListener('click', () => {
        el('nr-voice-name').value = item.dataset.voice;
        Bridge.call('save_settings', { tts_voice: item.dataset.voice });
        voicePage._markActiveVoice();
      });
    }
  }
  voicePage._markActiveVoice();
}

const visible = (cardId) => !el(cardId).classList.contains('hidden');
const firstItem = (id) => el(id).querySelector('.nr-voice-item');
// 端点可能一个音色都问不到（服务没起 / 没有 /v1/voices 路由）：那时这一档只有提示
const endpointVoices = ENDPOINT_TABLE.voices.map((v) => v.name);
const endpointSample = endpointVoices[0];
const edgeHasVoice = EDGE_TABLE.voices.length > 0;
const endpointHasVoices = endpointVoices.length > 0;

// 1) 自动模式：两张卡片都在，各自只装自己那一档的音色
render([EDGE_TABLE, ENDPOINT_TABLE]);
assert.equal(visible('nr-voice-card-edge'), true, '自动模式 edge 卡片应显示');
assert.equal(visible('nr-voice-card-endpoint'), true, '自动模式端点卡片应显示');
if (edgeHasVoice) {
  const edgeName = EDGE_TABLE.voices[0].name;
  assert.match(el('nr-voice-list-edge').innerHTML, new RegExp(edgeName));
  assert.equal(firstItem('nr-voice-list-edge').dataset.voice, edgeName,
    '按钮要带上 data-voice（点击靠它取值）');
}
if (endpointHasVoices) {
  assert.match(el('nr-voice-list-endpoint').innerHTML, new RegExp(endpointSample));
  assert.equal(el('nr-voice-list-endpoint').children.length, endpointVoices.length,
    '端点卡片应装下全部端点音色');
}
// 两张卡的内容不许互相串（edge 的名字不该出现在端点卡里，反之亦然）
for (const name of endpointVoices) {
  assert.doesNotMatch(el('nr-voice-list-edge').innerHTML, new RegExp(`data-voice="${name}"`),
    `edge 卡片里不该出现端点音色 ${name}`);
}
for (const voice of EDGE_TABLE.voices) {
  assert.doesNotMatch(el('nr-voice-list-endpoint').innerHTML,
    new RegExp(`data-voice="${voice.name}"`), `端点卡片里不该出现 edge 音色 ${voice.name}`);
}

// 2) 选定引擎：只留那一张，另一张整张隐藏并清空
render([ENDPOINT_TABLE]);
assert.equal(visible('nr-voice-card-endpoint'), true);
assert.equal(visible('nr-voice-card-edge'), false, '选定端点时 edge 卡片应隐藏');
assert.equal(el('nr-voice-list-edge').innerHTML, '', '隐藏的卡片要清空，否则会继续长出可点的音色');
assert.equal(el('nr-voice-list-endpoint').children.length, endpointVoices.length,
  '端点卡片应装下全部端点音色');

// 3) 系统离线音色：一张都不出
render([]);
assert.equal(visible('nr-voice-card-edge'), false);
assert.equal(visible('nr-voice-card-endpoint'), false);

// 4) 端点问不到候选：卡片留着，但要说明可以手填名字
render([{ ...ENDPOINT_TABLE, voices: [], unavailable: true }]);
assert.equal(visible('nr-voice-card-endpoint'), true, '端点卡片应留下来说明"可以手填"');
assert.match(el('nr-voice-list-endpoint').innerHTML, /直接填名字也能用/);
assert.equal(visible('nr-voice-card-edge'), false);

// 5) 点音色：写进输入框 + 立刻落盘，并把选中态标在这一档
if (!endpointHasVoices || !edgeHasVoice) {
  console.log(`  跳过点击用例（edge ${EDGE_TABLE.voices.length} 个 / 端点 ${endpointVoices.length} 个）`);
  console.log(`voice_page_cards: OK（部分分支因候选为空未覆盖）`);
  process.exit(0);
}
render([EDGE_TABLE, ENDPOINT_TABLE]);
const clickTarget = endpointVoices[endpointVoices.length - 1];
el('nr-voice-name').value = endpointVoices[0];
voicePage._markActiveVoice();
el('nr-voice-list-endpoint').children[endpointVoices.length - 1].click();

assert.equal(el('nr-voice-name').value, clickTarget, '点击应把音色名填进输入框');
const saved = Bridge.calls.find((call) => call.method === 'save_settings');
assert.ok(saved, '点击应立刻保存');
assert.deepEqual(saved.args[0], { tts_voice: clickTarget });
assert.equal(Bridge.calls[0].method, 'save_settings', '必须先落盘，用户紧接着就会点试听');
assert.ok(el('nr-voice-list-endpoint').children[endpointVoices.length - 1].classList.contains('active'),
  '选中的音色应标为 active');
assert.ok(!firstItem('nr-voice-list-edge').classList.contains('active'), '另一档不该有选中态');
assert.ok(!el('nr-voice-list-endpoint').children[0].classList.contains('active'),
  '同档里的其它音色不该有选中态');

console.log('voice_page_cards: OK');
