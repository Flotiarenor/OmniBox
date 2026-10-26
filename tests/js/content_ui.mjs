// 共享内容卡片/行：纯逻辑回归（真实 content-ui.js + 最小 DOM 替身）。
// 覆盖 API、结构、懒加载、selected、Enter/Space、操作按钮不误触卡片。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const source = fs.readFileSync(
  path.join(ROOT, 'shell/frontend/public/shell/content-ui.js'), 'utf8');

const dom = createDom();
const originalCreate = dom.document.createElement;
dom.document.createElement = (tag) => {
  const element = originalCreate(tag);
  element.attributes = {};
  element.nodeType = 1;
  element.setAttribute = (key, value) => {
    element.attributes[key] = String(value);
    if (key === 'id') element.id = String(value);
  };
  element.getAttribute = (key) => (
    Object.prototype.hasOwnProperty.call(element.attributes, key)
      ? element.attributes[key] : null
  );
  element.removeAttribute = (key) => { delete element.attributes[key]; };
  element.focus = () => { element.__focused = true; };
  // 记录 CSS 自定义属性：grid({min}) 只通过 style.setProperty('--cui-min', …) 生效，
  // 断言"传进去了"才是在测行为，而不是只测类名存在。
  element.style = {
    props: {},
    setProperty(key, value) { this.props[key] = String(value); },
    removeProperty(key) { delete this.props[key]; },
  };
  return element;
};

const sandbox = { console, document: dom.document };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.Utils = {
  iconHtml: (name) => `<svg data-icon="${name}"></svg>`,
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const UI = sandbox.ContentUI;
assert.ok(UI && typeof UI.card === 'function' && typeof UI.row === 'function');
assert.equal(UI.createCard, UI.card);
assert.equal(UI.createRow, UI.row);
assert.equal(UI.createGrid, UI.grid);
assert.equal(UI.createList, UI.list);

let activated = 0;
let actionCalls = 0;
const card = UI.card({
  variant: 'album',
  image: { src: 'cover.jpg', alt: '封面' },
  title: '标题',
  sub: '副标题',
  meta: '12 项',
  badge: 'NEW',
  ariaLabel: '内容卡片',
  selected: true,
  actions: [{ label: '打开', onClick: () => { actionCalls += 1; } }],
  onActivate: () => { activated += 1; },
});
assert.ok(card.className.includes('cui-card'));
// variant 已废弃（4 个插件仍会传，但它早已没有消费方）：不得再生成变体类名。
assert.ok(!card.className.includes('cui-card--'), `不得再生卡片变体类名：${card.className}`);
assert.ok(card.className.includes('is-selected'));
assert.equal(card.getAttribute('role'), 'button');
assert.equal(card.tabIndex, 0);
assert.equal(card.getAttribute('aria-label'), '内容卡片');
assert.equal(card.getAttribute('aria-pressed'), 'true');

const img = card.querySelector('img');
assert.ok(img, 'card 应包含 img');
assert.equal(img.loading, 'lazy');
assert.equal(img.decoding, 'async');
assert.equal(img.src, 'cover.jpg');
assert.equal(img.alt, '封面');
assert.equal(card.querySelector('.cui-title').textContent, '标题');
assert.equal(card.querySelector('.cui-sub').textContent, '副标题');
assert.equal(card.querySelector('.cui-meta').textContent, '12 项');
assert.equal(card.querySelector('.cui-badge').textContent, 'NEW');

const action = card.querySelector('.cui-action');
assert.ok(action, 'card 应包含操作按钮');
assert.equal(action.type, 'button');
action.click();
assert.equal(actionCalls, 1);
assert.equal(activated, 0, '操作按钮点击不应激活卡片');

function key(cardElement, keyName, target) {
  const event = {
    key: keyName,
    target: target || cardElement,
    defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
  };
  (cardElement.handlers.keydown || []).forEach((handler) => handler(event));
  return event;
}

const enter = key(card, 'Enter');
assert.equal(activated, 1);
assert.equal(enter.defaultPrevented, true);
key(card, ' ');
assert.equal(activated, 2);
const actionTarget = { closest: (selector) => (selector === '.cui-actions' ? {} : null) };
key(card, 'Enter', actionTarget);
assert.equal(activated, 2, '操作区内的 Enter 不应激活卡片');

(card.handlers.click || []).forEach((handler) => handler({ target: card }));
assert.equal(activated, 3);
(card.handlers.click || []).forEach((handler) => handler({ target: actionTarget }));
assert.equal(activated, 3, '操作区内的 click 不应激活卡片');

UI.setSelected(card, false);
assert.equal(card.classList.contains('is-selected'), false);
assert.equal(card.getAttribute('aria-pressed'), 'false');
assert.equal(card.dataset.selected, 'false');

const row = UI.row({
  variant: 'media-row',
  title: '媒体行',
  sub: '艺术家',
  image: 'thumb.jpg',
  actions: [{ label: '播放' }],
});
assert.ok(row.className.includes('cui-row'));
assert.equal(row.querySelector('.cui-media--media-row'), null, '媒体层不得再生成 variant 类名');
assert.ok(row.querySelector('.cui-media--empty') === null);
assert.equal(row.querySelector('img').loading, 'lazy');
assert.equal(row.getAttribute('role'), null, '没有 onActivate 时不应声明 button');

const rowWithActivate = UI.row({ title: '可激活行', onActivate: () => {} });
assert.equal(rowWithActivate.getAttribute('role'), 'button');
assert.equal(rowWithActivate.tabIndex, 0);

const empty = UI.card({ title: '无图' });
assert.ok(empty.querySelector('.cui-media--empty'));
assert.equal(empty.getAttribute('role'), null);

// ---------- grid / list：选项必须真的落到元素上，而不只是返回一个带类名的 div ----------
const grid = UI.grid({ min: '200px', ariaLabel: '相册网格' });
assert.ok(grid.className.includes('cui-grid'));
assert.equal(grid.getAttribute('aria-label'), '相册网格');
assert.equal(grid.style.props['--cui-min'], '200px', 'grid({min}) 应写 --cui-min 变量');
grid.appendChild(UI.card({ title: '网格卡片' }));
assert.equal(grid.children.length, 1, 'grid 应能直接收卡片');
assert.ok(!grid.className.includes('cui-collection'), '只传 min 不应进入集合模式');

const list = UI.list({ ariaLabel: '媒体列表' });
assert.ok(list.className.includes('cui-list'));
assert.equal(list.getAttribute('aria-label'), '媒体列表');
const listRow = UI.row({ title: '列表行' });
list.appendChild(listRow);
assert.equal(list.children[0], listRow);

// className 允许空格分隔的多个 token（旧实现 classList.add(整串) 会抛 InvalidCharacterError）
const multiClassCard = UI.card({ title: '多类名', className: 'plugin-card plugin-extra' });
assert.ok(multiClassCard.classList.contains('plugin-card'));
assert.ok(multiClassCard.classList.contains('plugin-extra'));

// ---------- 统一卡片的类型/状态/密度/布局 ----------
const unified = UI.card({
  title: '统一卡片',
  type: '漫画',
  status: { text: '已下载', tone: 'ok' },
  density: 'compact',
});
assert.ok(unified.className.includes('cui-card'));
assert.ok(unified.className.includes('cui-density-compact'));
assert.equal(unified.querySelector('.cui-type').textContent, '漫画');
assert.equal(unified.querySelector('.cui-status').textContent, '已下载');
assert.ok(unified.querySelector('.cui-status').className.includes('cui-status--ok'));
const rowLayout = UI.card({ title: '行布局', layout: 'row', density: 'spacious' });
assert.ok(rowLayout.className.includes('cui-row'));
assert.ok(rowLayout.className.includes('cui-density-spacious'));

// ---------- mediaOverlay ----------
const overlayCard = UI.card({
  image: { src: 'cover.jpg', alt: '封面', data: { kind: 'comic' } },
  title: '封面信息',
  mediaOverlay: { badge: 'NEW', count: '12', time: '03:21', progress: 42 },
});
const media = overlayCard.querySelector('.cui-media');
assert.equal(overlayCard.querySelector('img').dataset.kind, 'comic', 'image.data 应写入 img.dataset');
assert.equal(media.querySelector('.cui-media-badge').textContent, 'NEW');
assert.equal(media.querySelector('.cui-media-count').textContent, '12');
assert.equal(media.querySelector('.cui-media-time').textContent, '03:21');
const progress = media.querySelector('.cui-media-progress');
assert.equal(progress.getAttribute('role'), 'progressbar');
assert.equal(progress.getAttribute('aria-valuenow'), '42');
assert.equal(media.querySelector('.cui-media-progress-bar').style.width, '42%');
const objectProgress = UI.card({
  image: 'x.jpg',
  mediaOverlay: { progress: { value: 3, total: 10 } },
});
assert.equal(objectProgress.querySelector('.cui-media-progress').getAttribute('aria-valuenow'), '30');

// image onError + broken 状态
let imageErrors = 0;
const brokenCard = UI.card({
  image: { src: 'bad.jpg', onError: () => { imageErrors += 1; } },
  title: '坏图',
});
const brokenMedia = brokenCard.querySelector('.cui-media');
brokenCard.querySelector('img').handlers.error[0]({ type: 'error' });
assert.equal(imageErrors, 1);
assert.ok(brokenMedia.classList.contains('cui-media--broken'));

// ---------- actionsPlacement / action icon/class/data/aria ----------
const bottomCard = UI.card({
  title: '底部操作',
  actions: [{ label: '打开' }],
});
const bottomActions = bottomCard.querySelector('.cui-actions');
assert.equal(bottomActions.parentElement, bottomCard, '默认 bottom 仍挂在卡片上');
const topCard = UI.card({
  image: 'cover.jpg',
  title: '右上操作',
  actionsPlacement: 'top-right',
  actions: [{ label: '更多', icon: 'icon:more', className: 'custom-action', data: { id: '7' }, aria: { expanded: 'false' } }],
});
const topActions = topCard.querySelector('.cui-actions');
assert.ok(topActions.className.includes('cui-actions--top-right'));
assert.equal(topActions.parentElement, topCard.querySelector('.cui-media'), 'top-right 应挂在封面上');
const iconAction = topActions.querySelector('.cui-action');
assert.ok(iconAction.className.includes('custom-action'));
assert.ok(iconAction.innerHTML.includes('data-icon="icon:more"'));
assert.equal(iconAction.querySelector('.cui-action-label').textContent, '更多');
assert.equal(iconAction.dataset.id, '7');
assert.equal(iconAction.getAttribute('aria-expanded'), 'false');
const overlayActionCard = UI.card({
  image: 'cover.jpg',
  actionsPlacement: 'overlay',
  actions: [{ label: '播放' }],
});
assert.ok(overlayActionCard.querySelector('.cui-actions--overlay'));
assert.equal(overlayActionCard.querySelector('.cui-actions').parentElement,
  overlayActionCard.querySelector('.cui-media'));

// ---------- tone / aria 白名单：非法值忽略，不能让渲染抛 InvalidCharacterError ----------
// 真实 DOM 里 `classList.add('a b')` 与 `setAttribute('aria-a b', …)` 都会抛
// InvalidCharacterError（整张卡片渲染中断）；tone/aria key 来自调用方，必须先过滤。
const invalidTone = UI.card({ title: '状态', status: { text: '异常', tone: 'ok extra' } });
const invalidToneNode = invalidTone.querySelector('.cui-status');
assert.equal(invalidToneNode.className.trim(), 'cui-status', '非法 tone 不得拼进类名');
assert.equal(invalidToneNode.textContent, '异常', '文本本身仍要显示');

const safeAria = UI.card({
  title: 'aria',
  actions: [{ label: 'a', aria: { 'aria-label': 'A', expanded: 'true', 'a b': 'x', 'x"y': 'z' } }],
});
const safeAriaBtn = safeAria.querySelector('.cui-action');
assert.equal(safeAriaBtn.getAttribute('aria-label'), 'A', '合法 aria key 正常写入');
assert.equal(safeAriaBtn.getAttribute('aria-expanded'), 'true');
assert.equal(safeAriaBtn.getAttribute('aria-a b'), null, '非法 key（空格）必须忽略');
assert.equal(safeAriaBtn.getAttribute('aria-x"y'), null, '非法 key（引号）必须忽略');

// ---------- 集合：visibleCount / expandable / 卡片结构不变 ----------
let expandCalls = 0;
const collection = UI.grid({
  visibleCount: 2,
  expandable: true,
  expandLabel: '展开 {hidden}',
  collapsedLabel: '收起',
  onExpandChange: (expanded) => { expandCalls += 1; assert.equal(typeof expanded, 'boolean'); },
});
const cards = [1, 2, 3, 4].map((n) => UI.card({ title: `卡片 ${n}` }));
cards.forEach((item) => collection.appendChild(item));
assert.ok(collection.className.includes('cui-collection'));
assert.ok(collection.itemsEl.className.includes('cui-grid'));
assert.equal(collection.itemsEl.children.length, 4);
assert.ok(cards[2].classList.contains('cui-item--hidden'), '超出 visibleCount 的卡片应隐藏');
assert.equal(cards[2].getAttribute('aria-hidden'), 'true');
assert.equal(collection.toggleEl.hidden, false);
assert.equal(collection.toggleEl.getAttribute('aria-expanded'), 'false');
assert.equal(collection.toggleEl.textContent, '展开 2');
assert.equal(collection.isExpanded(), false);
collection.toggleEl.click();
assert.equal(collection.isExpanded(), true);
assert.equal(cards[2].classList.contains('cui-item--hidden'), false);
assert.equal(cards[2].getAttribute('aria-hidden'), null);
assert.equal(collection.toggleEl.getAttribute('aria-expanded'), 'true');
assert.equal(collection.toggleEl.textContent, '收起');
assert.equal(collection.itemsEl.children.length, 4, '展开不应重建卡片');
assert.equal(expandCalls, 1);
collection.toggleEl.click();
assert.ok(cards[3].classList.contains('cui-item--hidden'));
assert.equal(collection.toggleEl.textContent, '展开 2');

const listCollection = UI.collection({ layout: 'list', visibleCount: 1, expandable: true });
listCollection.appendChild(UI.card({ title: '列表卡 1' }));
listCollection.appendChild(UI.card({ title: '列表卡 2' }));
assert.ok(listCollection.className.includes('cui-collection--list'));
assert.ok(listCollection.itemsEl.className.includes('cui-list'));
assert.equal(listCollection.toggleEl.hidden, false);
listCollection.setItems([UI.card({ title: '替换' })]);
assert.equal(listCollection.itemsEl.children.length, 1);
assert.equal(listCollection.toggleEl.hidden, true, '数量不超过 visibleCount 时折叠按钮隐藏');

assert.ok(!UI.grid({ min: '200px' }).className.includes('cui-collection'), '旧 grid 调用不进入集合模式');

console.log('content_ui: OK');
