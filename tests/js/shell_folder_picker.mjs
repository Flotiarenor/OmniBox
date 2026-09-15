// Shell 共享目录组件（shell/frontend/public/shell/folder-picker.js）的回归用例。
//
// 为什么单独守它：image-viewer / media-player / manga-library / novel-reader 的
// 「多位置文件夹」界面都走这一份实现（`window.FolderPicker.createList`），而它渲染的
// 是**磁盘目录名与完整路径** —— 名字里带 `<`/`"`/`&` 的目录在 Linux 上完全合法
// （Windows 文件名禁用这几个字符），未转义就是一个注入点。这里把真实内核
// `Utils.escapeHtml` 装进沙箱，喂恶意数据后断言产物里没有原始载荷。
//
// 本文件曾被 tests/js/image_viewer_roots_list.mjs 引用却不存在（引用了一个不存在的
// 文件，等于这条守护一直是空的），所以它同时是"引用即存在"的锁。
//
// 用法：node tests/js/shell_folder_picker.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const SHELL_PUBLIC = join(here, '..', '..', 'shell', 'frontend', 'public', 'shell');
const PICKER = join(SHELL_PUBLIC, 'folder-picker.js');
const BASE = join(SHELL_PUBLIC, 'base.js');

const PAYLOAD = '\'"><img src=x onerror=alert(1)>&';

let passed = 0;
const check = (name, fn) => {
    try {
        fn();
        passed += 1;
        console.log(`  PASS  ${name}`);
    } catch (err) {
        console.error(`  FAIL  ${name}  — ${err.message}`);
        process.exitCode = 1;
    }
};

/** 从 base.js 里截出 `window.Utils = { ... }` 并求值，拿到真实内核转义实现。 */
function loadKernelUtils(sandbox) {
    const source = readFileSync(BASE, 'utf8');
    const start = source.indexOf('window.Utils =');
    assert.ok(start >= 0, 'base.js 里找不到 window.Utils 定义');
    const open = source.indexOf('{', start);
    let depth = 0;
    let end = -1;
    for (let i = open; i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        else if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) { end = i + 1; break; }
        }
    }
    assert.ok(end > 0, 'base.js 的 window.Utils 大括号不配对');
    vm.runInContext(`window.Utils = ${source.slice(open, end)};`, sandbox);
    return sandbox.window.Utils;
}

/** 够用的 DOM 替身：记录 innerHTML、把 append 的子节点收进 children。 */
function makeElement(tag = 'div') {
    const el = {
        tagName: String(tag).toUpperCase(),
        className: '',
        innerHTML: '',
        textContent: '',
        value: '',
        disabled: false,
        style: { setProperty() { }, removeProperty() { } },
        dataset: {},
        children: [],
        classList: { add() { }, remove() { }, toggle() { }, contains: () => false },
        append(...nodes) { el.children.push(...nodes); },
        appendChild(node) { el.children.push(node); return node; },
        remove() { },
        addEventListener() { },
        removeEventListener() { },
        setAttribute() { },
        getAttribute: () => null,
        querySelector: (selector) => makeElement(selector === 'input' ? 'input' : 'div'),
        querySelectorAll: () => [],
    };
    return el;
}

function makeSandbox() {
    const sandbox = {
        console,
        document: {
            createElement: makeElement,
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            body: makeElement('body'),
            documentElement: makeElement('html'),
            addEventListener() { },
        },
        Bridge: { callSystem: async () => ({ path: '', parent: null, entries: [] }) },
        Toast: { warning() { }, info() { }, error() { }, success() { } },
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    sandbox.setTimeout = setTimeout;
    sandbox.clearTimeout = clearTimeout;
    return sandbox;
}

const sandbox = makeSandbox();
vm.createContext(sandbox);
loadKernelUtils(sandbox);
vm.runInContext(readFileSync(PICKER, 'utf8'), sandbox, { filename: 'folder-picker.js' });
const FolderPicker = sandbox.window.FolderPicker;

console.log('[shell] 共享目录组件');

check('组件挂在 window.FolderPicker 上并导出约定成员', () => {
    assert.ok(FolderPicker, 'window.FolderPicker 不存在');
    for (const name of ['DRIVES_SENTINEL', 'KIND_LABELS', 'normalize', 'openDirBrowser', 'createList']) {
        assert.ok(name in FolderPicker, `缺少导出 ${name}`);
    }
    assert.equal(typeof FolderPicker.createList, 'function');
});

check('normalize 去掉首尾空白与末尾分隔符', () => {
    assert.equal(FolderPicker.normalize('  D:\\图库\\  '), 'D:\\图库');
    assert.equal(FolderPicker.normalize('/home/me/pics///'), '/home/me/pics');
    assert.equal(FolderPicker.normalize(null), '');
});

check('渲染目录名/路径/占位符时转义载荷（文本与属性都不逃逸）', () => {
    const list = FolderPicker.createList({
        paths: [],
        placeholder: PAYLOAD,
        emptyText: PAYLOAD,
        labels: () => PAYLOAD,
    });
    const listBox = list.element.children[0];
    assert.ok(listBox, 'createList 没有产出列表容器');
    // 空列表 → 渲染 emptyText
    assert.ok(!listBox.innerHTML.includes('<img src=x'), `空态未转义：${listBox.innerHTML}`);
    assert.ok(listBox.innerHTML.includes('&lt;img'), '空态应产出实体化后的载荷');

    list.addPath(PAYLOAD);
    assert.deepEqual(list.getPaths(), [PAYLOAD]);
    const html = listBox.innerHTML;
    assert.ok(!html.includes('<img src=x'), `列表未转义：${html}`);
    assert.ok(!/['"]><img/.test(html), `载荷逃出了属性：${html}`);
    assert.ok(html.includes('&quot;') && html.includes('&lt;img'), '值没有被实体化');
    // 行内 title 属性也必须转义（悬停显示完整路径）
    assert.ok(!html.includes(`title="${PAYLOAD}"`), 'title 属性里的路径未转义');

    // 输入框占位符来自插件 schema，同样是外部数据
    const addRow = list.element.children[1];
    assert.ok(!addRow.innerHTML.includes('<img src=x'), `占位符未转义：${addRow.innerHTML}`);
    assert.ok(addRow.innerHTML.includes('&lt;img'), '占位符应产出实体化后的载荷');
});

check('addPath 去重并拒绝空值', () => {
    const list = FolderPicker.createList({ paths: [] });
    assert.equal(list.addPath('   '), false, '空路径应被拒绝');
    assert.equal(list.addPath('/data/a/'), true);
    assert.equal(list.addPath('/data/a'), false, '同一个目录（尾斜杠不同）应被去重');
    assert.deepEqual(list.getPaths(), ['/data/a']);
});

check('setPaths 覆盖式替换并重新渲染', () => {
    const list = FolderPicker.createList({ paths: ['/old'] });
    list.setPaths(['/a', '/b']);
    assert.deepEqual(list.getPaths(), ['/a', '/b']);
    const listBox = list.element.children[0];
    assert.ok(listBox.innerHTML.includes('/a') && listBox.innerHTML.includes('/b'));
    assert.ok(!listBox.innerHTML.includes('/old'), '旧路径应被移除');
});

console.log(`\nshell_folder_picker: ${passed} 项检查${process.exitCode ? '（有失败）' : '全部通过'}`);
