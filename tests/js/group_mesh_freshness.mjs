// group-mesh 统一刷新与共享项用量展示用例。
//
// 守四件事：
//   1. 切回前台（onShow）按全部共享根同步一次；
//   2. 同步报告真有变化时才重读后端状态（`cubby:freshness` 与旧的 `refresh` 动作都覆盖）；
//   3. 共享项卡片展示用量与条目数（后端刷新钩子算出来的字段，可能只在 `share_roots` 里）；
//   4. 最近同步失败在节点卡片上可见；字段缺失时静默，不显示 0 或占位符。
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

import { createDom } from './dom_stub.mjs';
import { loadShellScripts } from './shell_scripts.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const FRONTEND = path.join(ROOT, 'plugins', 'group-mesh', 'frontend');

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

// app.js 的 bind() 直接给这些 id 挂监听；真实页面里它们都在 index.html 里。
const ELEMENT_IDS = [
    'kernel-missing',
    'identity-body', 'identity-actions',
    'roster-body', 'roster-actions',
    'shares-body', 'node-body', 'node-actions',
    'unsupported-list', 'gm-panels', 'gm-content',
    'btn-add-share', 'share-id', 'share-path', 'share-write',
    'btn-copy-invite', 'invite-text', 'btn-close-invite',
    'btn-close-join', 'btn-do-create', 'create-group-name', 'btn-close-create',
    'btn-do-join', 'join-text',
    'btn-close-member', 'btn-do-add-member',
    'member-principal', 'member-device', 'member-name',
];

function baseStatus(overrides = {}) {
    return {
        kernel: { available: true, version: '0.1.0', package: 'shell.groupmesh', path: '/opt/groupmesh' },
        data_root: 'D:/data/group-mesh',
        identity_dir: 'D:/data/group-mesh/identity',
        locations: {
            identity: 'D:/data/group-mesh/identity',
            downloads: 'D:/data/group-mesh/downloads',
            downloads_custom: false,
            cache: 'D:/data/group-mesh/.cache',
            remote_cache: 'D:/data/group-mesh/.cache/remote',
        },
        settings: {
            port: 7801, bind: '::', group_name: '', principal_name: '',
            download_dir: '', sync_interval_seconds: 60,
        },
        node: { running: true, published: true, listening: '[::]:7801', error: '', auto_start_error: '' },
        sync: { interval: 60, last_sync_at: 1700000000, last_error: '', roster_dirty: false, registry_dirty: false },
        identity: null,
        roster: null,
        shares: [],
        share_roots: [],
        unsupported: [],
        ...overrides,
    };
}

function loadApp(options = {}) {
    const { document, makeEl } = createDom();
    const messages = [];
    const calls = [];
    const lifecycle = { show: [], hide: [], dispose: [] };
    const freshnessCalls = [];
    const docListeners = {};
    const windowListeners = {};
    const statuses = options.statuses || [baseStatus()];
    let statusIndex = 0;

    // 首屏骨架读 `body.childNodes.length`；dom_stub 只维护 children，这里补一个只读视图。
    ELEMENT_IDS.forEach((id) => {
        const el = makeEl('div');
        Object.defineProperty(el, 'childNodes', { get: () => el.children });
        el.id = id;
    });
    document.addEventListener = (type, fn) => { docListeners[type] = fn; };
    document.removeEventListener = () => { };

    const origin = 'http://127.0.0.1:18080';
    const parent = { postMessage(payload) { messages.push(payload); } };
    const sandbox = {
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        document,
        location: { origin, href: `${origin}/plugins/group-mesh/frontend/index.html?content=1` },
        localStorage: { getItem: () => null, setItem() { }, removeItem() { } },
        Utils: { escapeHtml: (value) => String(value == null ? '' : value) },
        Icons: { html: () => '' },
        Toast: { success() { }, error() { }, warning() { }, info() { } },
        Motion: { stagger() { }, retrigger() { } },
        confirmDialog: async () => true,
        PluginLifecycle: {
            onShow(fn) { lifecycle.show.push(fn); },
            onHide(fn) { lifecycle.hide.push(fn); },
            onDispose(fn) { lifecycle.dispose.push(fn); },
        },
        Freshness: {
            sync(plugin, scope) {
                freshnessCalls.push({ plugin, scope });
                const report = options.report ? options.report(plugin, scope)
                    : { action: 'sync', added: 0, changed: 0, removed: 0 };
                return Promise.resolve(report);
            },
        },
        Bridge: {
            call(method, ...args) {
                calls.push({ method, args });
                if (method === 'get_status') {
                    const status = statuses[Math.min(statusIndex, statuses.length - 1)];
                    statusIndex += 1;
                    return Promise.resolve(status);
                }
                return Promise.resolve({ success: true });
            },
        },
    };
    sandbox.window = sandbox;
    sandbox.parent = parent;
    sandbox.addEventListener = (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); };
    sandbox.removeEventListener = (type, fn) => {
        const list = windowListeners[type] || [];
        const at = list.indexOf(fn);
        if (at >= 0) list.splice(at, 1);
    };
    vm.createContext(sandbox);
    loadShellScripts(sandbox, ['chrome-client.js']);
    vm.runInContext(fs.readFileSync(path.join(FRONTEND, 'js', 'app.js'), 'utf8'), sandbox,
        { filename: 'plugins/group-mesh/frontend/js/app.js' });

    return {
        document, sandbox, messages, calls, lifecycle, freshnessCalls,
        chrome() {
            for (let i = messages.length - 1; i >= 0; i -= 1) {
                if (messages[i] && messages[i].type === 'cubby:chrome') return messages[i];
            }
            return null;
        },
        html(id) {
            const node = document.getElementById(id);
            return node ? node.innerHTML : '';
        },
        init() { docListeners.DOMContentLoaded(); },
        show() { lifecycle.show.forEach((fn) => fn()); },
        send(data) {
            (windowListeners.message || []).slice().forEach((fn) => fn({ origin, source: parent, data }));
        },
        statusCalls() { return calls.filter((call) => call.method === 'get_status').length; },
        tick,
    };
}

const changed = { action: 'sync', added: 1, changed: 0, removed: 0 };
const zero = { action: 'sync', added: 0, changed: 0, removed: 0 };

// ---- 1. 共享项用量/条目数与最近错误 ----
{
    const env = loadApp({
        statuses: [baseStatus({
            shares: [{
                share_id: 's1', path: 'D:/share', acl: { read: 'group', write: 'owner' },
                max_bytes: null, available: true, reason: null,
            }],
            share_roots: [{
                share_id: 's1', path: 'D:/share', available: true,
                used_bytes: 1572864, entries: 42, truncated: false,
            }],
            sync: {
                interval: 60, last_sync_at: 1700000000,
                last_error: 'TransportError: 连接超时',
                errors: ['D:/share/sub 读取失败: PermissionError'],
                unreachable: [{ endpoint: '[::1]:7802', error: '连接被拒绝' }],
            },
        })],
    });
    env.init();
    await env.tick(20);
    const shares = env.html('shares-body');
    check('共享项表格渲染出用量', shares.includes('1.5 MiB'), shares.slice(0, 200));
    check('共享项表格渲染出条目数', shares.includes('42'), shares.slice(0, 200));
    check('按当前目录指纹算过的用量不标待更新', !shares.includes('待更新'), shares.slice(0, 200));
    check('共享项表格保留状态与 ACL', shares.includes('可用') && shares.includes('读 group / 写 owner'));
    check('节点卡片展示最近同步失败', env.html('node-body').includes('最近同步失败'));
    check('最近同步失败带原因', env.html('node-body').includes('TransportError: 连接超时'));
    check('节点卡片展示共享根读取失败', env.html('node-body').includes('共享根读取失败 1 项'));
    check('节点卡片展示连不上的对端', env.html('node-body').includes('连不上的对端 1 个'));
}

// ---- 2. 字段缺失时静默 ----
{
    const env = loadApp({
        statuses: [baseStatus({
            shares: [{
                share_id: 's1', path: 'D:/share', acl: { read: 'group', write: 'owner' },
                max_bytes: null, available: true, reason: null,
            }],
            share_roots: [{ share_id: 's1', path: 'D:/share', available: true }],
        })],
    });
    env.init();
    await env.tick(20);
    const shares = env.html('shares-body');
    check('没有用量数据时不显示 0 B', !shares.includes('0 B'), shares.slice(0, 200));
    check('字段缺失时节点卡片不显示同步失败', !env.html('node-body').includes('最近同步失败'));
    check('共享项仍正常渲染', shares.includes('s1') && shares.includes('D:/share'));
}

// ---- 3. 共享项行自身带用量时也认；过期的用量要标待更新 ----
{
    const env = loadApp({
        statuses: [baseStatus({
            shares: [{
                share_id: 's2', path: 'D:/share2', acl: { read: 'group', write: 'owner' },
                max_bytes: null, available: true, reason: null,
                used_bytes: 2048, entries: 3, usage_stale: true, last_error: '共享根不可读',
            }],
            share_roots: [],
        })],
    });
    env.init();
    await env.tick(20);
    const shares = env.html('shares-body');
    check('行内用量优先于 share_roots', shares.includes('2.0 KiB'), shares.slice(0, 200));
    check('行内条目数可见', shares.includes('3'));
    check('共享项自身的最近错误可见', shares.includes('共享根不可读'));
    check('目录指纹已变的用量标成待更新', shares.includes('待更新'), shares.slice(0, 300));
}

// ---- 4. onShow 同步全部根，有变化才重读 ----
{
    const env = loadApp({ report: () => changed });
    env.init();
    await env.tick(20);
    const before = env.statusCalls();
    env.show();
    await env.tick(20);
    check('切回前台同步一次', env.freshnessCalls.length === 1, JSON.stringify(env.freshnessCalls));
    check('同步用本插件名', env.freshnessCalls[0].plugin === 'group-mesh', env.freshnessCalls[0].plugin);
    check('同步覆盖全部共享根', env.freshnessCalls[0].scope === '', JSON.stringify(env.freshnessCalls[0]));
    check('有变化重读状态', env.statusCalls() === before + 1, `${before} -> ${env.statusCalls()}`);
}

{
    const env = loadApp({ report: () => zero });
    env.init();
    await env.tick(20);
    const before = env.statusCalls();
    env.show();
    await env.tick(20);
    check('零变化不重读状态', env.statusCalls() === before, `${before} -> ${env.statusCalls()}`);
}

// ---- 5. cubby:freshness 与旧 refresh 动作（按 payload 判重） ----
{
    const env = loadApp();
    env.init();
    await env.tick(20);
    const before = env.statusCalls();

    env.send({ type: 'cubby:freshness', plugin: 'group-mesh', scope: '', report: zero });
    await env.tick(10);
    check('零变化报告不重读', env.statusCalls() === before);

    env.send({ type: 'cubby:freshness', plugin: 'group-mesh', scope: '', report: { action: 'skip', reason: 'busy' } });
    await env.tick(10);
    check('skip 不重读', env.statusCalls() === before);

    env.send({ type: 'cubby:freshness', plugin: 'other-plugin', scope: '', report: changed });
    await env.tick(10);
    check('别的插件的报告不重读', env.statusCalls() === before);

    env.send({ type: 'cubby:freshness', plugin: 'group-mesh', scope: '', report: changed });
    await env.tick(20);
    check('有变化报告重读状态', env.statusCalls() === before + 1, `${before} -> ${env.statusCalls()}`);

    // 壳在同一次同步后补发的旧 refresh 带着同一份 payload：富事件已经重读过，跳过。
    env.send({
        type: 'cubby:action', action: 'refresh',
        payload: { plugin: 'group-mesh', scope: '', report: changed },
    });
    await env.tick(20);
    check('带 payload 的 refresh（回声）不重复重读', env.statusCalls() === before + 1,
        `${before} -> ${env.statusCalls()}`);

    // 用户点顶栏「刷新」：动作上没有 payload，必须重读。
    env.send({ type: 'cubby:action', action: 'refresh' });
    await env.tick(20);
    check('无 payload 的 refresh（用户点击）正常重读', env.statusCalls() === before + 2,
        `${before} -> ${env.statusCalls()}`);
}

// ---- 5b. 目录集合变化与校验完成也算变化（track_entries=False 下仅有的两个信号） ----
{
    const env = loadApp();
    env.init();
    await env.tick(20);
    let before = env.statusCalls();

    env.send({
        type: 'cubby:freshness', plugin: 'group-mesh', scope: '',
        report: { action: 'sync', added: 0, changed: 0, removed: 0, new_dirs: ['sub'] },
    });
    await env.tick(20);
    check('new_dirs 报告重读状态', env.statusCalls() === before + 1,
        `${before} -> ${env.statusCalls()}`);

    before = env.statusCalls();
    env.send({
        type: 'cubby:freshness', plugin: 'group-mesh', scope: '',
        report: { action: 'sync', added: 0, changed: 0, removed: 0, vanished_dirs: ['old'] },
    });
    await env.tick(20);
    check('vanished_dirs 报告重读状态', env.statusCalls() === before + 1,
        `${before} -> ${env.statusCalls()}`);

    before = env.statusCalls();
    env.send({
        type: 'cubby:freshness', plugin: 'group-mesh', scope: '',
        report: { action: 'verify', added: 0, changed: 0, removed: 0, invalidated: false, task: { state: 'done' } },
    });
    await env.tick(20);
    check('校验完成（零计数）也重读状态', env.statusCalls() === before + 1,
        `${before} -> ${env.statusCalls()}`);

    before = env.statusCalls();
    env.send({
        type: 'cubby:freshness', plugin: 'group-mesh', scope: '',
        report: { action: 'sync', added: 0, changed: 0, removed: 0, new_dirs: [], vanished_dirs: [] },
    });
    await env.tick(20);
    check('目录数组为空时仍算零变化', env.statusCalls() === before,
        `${before} -> ${env.statusCalls()}`);
}

{
    // 同一条判据也用于切回前台的被动同步：只有目录变化时也要重读。
    const env = loadApp({ report: () => ({ action: 'sync', added: 0, changed: 0, removed: 0, new_dirs: ['sub'] }) });
    env.init();
    await env.tick(20);
    const before = env.statusCalls();
    env.show();
    await env.tick(20);
    check('onShow 只有目录变化也重读状态', env.statusCalls() === before + 1,
        `${before} -> ${env.statusCalls()}`);
}

// ---- 6. 旧壳只发 refresh 动作（没有富事件、没有 payload）时仍重读 ----
{
    const env = loadApp();
    env.init();
    await env.tick(20);
    const before = env.statusCalls();
    env.send({ type: 'cubby:action', action: 'refresh' });
    await env.tick(20);
    check('旧 refresh 动作仍重读状态', env.statusCalls() === before + 1, `${before} -> ${env.statusCalls()}`);
    check('chrome 上报全部根作用域', env.chrome().freshness_scope === '',
        JSON.stringify(env.chrome().freshness_scope));
}

console.log('group-mesh 统一刷新/用量用例全部通过');
