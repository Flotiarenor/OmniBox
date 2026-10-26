// manga-library 下载中心迁移回归：过滤视图、任务行、行内动作、空态与添加任务弹窗。
//
// 下载中心的导航项是复合 id（`downloads:<filter>`），顶栏四个动作只在下载中心可见、
// 按任务状态置 disabled；这些都由内容页通过 omnibox 消息上报。迁移把按钮搬到了壳，
// 所以断言看的是上报的 actions / view，而不是内容页里是否还有壳 DOM。
import assert from 'node:assert/strict';
import { loadManga, defaultHandlers, createApp } from './manga_library_harness.mjs';

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

const tasks = [
    { id: 't1', albumId: '111', title: '漫1', status: 'downloading', totalImages: 10, completedImages: 5, speed: 2048, eta: 60, thumbUrl: '' },
    { id: 't2', albumId: '222', title: '漫2', status: 'paused', totalImages: 0, completedImages: 0 },
    { id: 't3', albumId: '333', title: '漫3', status: 'completed', totalImages: 4, completedImages: 4 },
    { id: 't4', albumId: '444', title: '漫4', status: 'failed', totalImages: 2, completedImages: 1, thumbUrl: 'thumbs/4.jpg' },
];

const env = loadManga({
    handlers: defaultHandlers({
        download_list: () => ({ tasks }),
        download_get_album_info: (albumId) => ({
            title: `漫画${albumId}`, author: '作者3', total_page_count: 4, exists: true,
        }),
    }),
});
const app = await createApp(env);
const content = () => env.document.getElementById('ml-content');

// ---- 壳左栏切到下载中心：复合 id 决定过滤项 ----
env.sendView('downloads:all');
await env.tick();
let chrome = env.chrome();
check('导航复合 id 切到下载中心', app.currentView === 'downloads' && app.downloadFilter === 'all');
check('高亮 id 用复合形式', chrome.view === 'downloads:all', chrome.view);
check('下载中心隐藏搜索', chrome.search.enabled === false);
check('下载中心的顶栏动作全部就位',
    chrome.actions['add-task'].visible === true
    && chrome.actions['start-all'].visible === true
    && chrome.actions['pause-all'].visible === true
    && chrome.actions['clear-completed'].visible === true
    && chrome.actions.settings.visible === true,
    JSON.stringify(chrome.actions));
check('返回按钮在下载中心隐藏', chrome.actions.back.visible === false);
check('有暂停任务时「全部开始」可用', chrome.actions['start-all'].disabled === false);
check('有下载中任务时「全部暂停」可用', chrome.actions['pause-all'].disabled === false);
check('有已完成任务时「清除已完成」可用', chrome.actions['clear-completed'].disabled === false);
check('下载任务总数进导航徽标',
    env.nav()['downloads:all'] && env.nav()['downloads:all'].badge === '4',
    JSON.stringify(env.nav()['downloads:all']));
check('状态栏统计四类任务',
    chrome.status.text === '任务 4 · 下载中 1 · 完成 1 · 排队 0', chrome.status.text);

let rows = content().querySelectorAll('.ml-task');
check('下载中心按任务渲染行', rows.length === 4, `实际 ${rows.length}`);
const row = (id) => content().querySelectorAll('.ml-task').find((node) => node.dataset.taskId === id);
check('行标题来自任务', row('t1').querySelector('.ml-task-title').textContent === '漫1');
check('状态文案走共享工具',
    row('t1').querySelector('.ml-status').textContent === '下载中'
    && row('t4').querySelector('.ml-status').textContent === '失败');
check('进度按完成比例画',
    row('t1').querySelector('.ml-progress-fill').style.width === '50%'
    && row('t3').querySelector('.ml-progress-fill').style.width === '100%',
    JSON.stringify({ t1: row('t1').querySelector('.ml-progress-fill').style.width,
                     t3: row('t3').querySelector('.ml-progress-fill').style.width }));
check('无缩略图的行用图标占位',
    !!row('t1').querySelector('.ml-task-thumb') && !row('t1').querySelector('.ml-task-thumb img'));
check('有缩略图的行直接展示',
    row('t4').querySelector('img.ml-task-thumb').src === 'thumbs/4.jpg');
check('行内动作按状态出现',
    !!row('t1').querySelector('[data-act="pause"]')
    && !!row('t2').querySelector('[data-act="resume"]')
    && !!row('t4').querySelector('[data-act="retry"]')
    && !!row('t3').querySelector('[data-act="detail"]'));

// ---- 行内暂停：调后端并重拉任务 ----
const listCallsBefore = env.calls('download_list').length;
row('t1').querySelector('[data-act="pause"]').click();
await env.tick();
check('暂停按钮调用 download_pause',
    env.calls('download_pause').length === 1 && env.calls('download_pause')[0].args[0] === 't1');
check('行内动作后重新拉任务列表', env.calls('download_list').length > listCallsBefore);

// ---- 行内删除：先确认再调后端 ----
row('t1').querySelector('[data-act="delete"]').click();
await env.tick();
check('删除按钮先确认再调用后端',
    env.calls('download_delete').length === 1 && env.calls('download_delete')[0].args[0] === 't1');

// ---- 行内详情：拉 album_info 并填充任务详情弹窗 ----
row('t3').querySelector('[data-act="detail"]').click();
await env.tick();
check('详情按钮读取已下载漫画信息',
    env.calls('download_get_album_info').length === 1
    && env.calls('download_get_album_info')[0].args[0] === '333');
const detailModal = env.document.getElementById('ml-detail-modal');
const detailText = env.document.getElementById('ml-detail-content').textContent;
check('任务详情弹窗打开', detailModal.classList.contains('active'));
check('任务详情展示作者与标题',
    detailText.includes('作者3') && detailText.includes('漫画333'), detailText.slice(0, 120));

// ---- 过滤到空分类：空态提示添加任务 ----
env.sendView('downloads:failed');
await env.tick();
check('切到失败过滤项', app.downloadFilter === 'failed');
rows = content().querySelectorAll('.ml-task');
check('失败过滤只剩失败任务', rows.length === 1 && rows[0].dataset.taskId === 't4');
env.sendView('downloads:queued');
await env.tick();
check('空过滤项显示空态', content().innerHTML.includes('暂无下载任务'));
check('空态提示添加任务入口', content().innerHTML.includes('添加任务'));
check('空过滤项也保持复合高亮', env.chrome().view === 'downloads:queued');

// ---- 全部任务都完成时下载动作的 disabled 状态 ----
const onlyCompleted = [{ id: 'c1', albumId: '1', title: '已完成', status: 'completed', totalImages: 1, completedImages: 1 }];
const env2 = loadManga({
    handlers: defaultHandlers({ download_list: () => ({ tasks: onlyCompleted }) }),
});
const app2 = await createApp(env2);
env2.sendView('downloads:all');
await env2.tick();
const actions2 = env2.chrome().actions;
check('只有已完成任务时「全部开始」禁用', actions2['start-all'].disabled === true);
check('只有已完成任务时「全部暂停」禁用', actions2['pause-all'].disabled === true);
check('只有已完成任务时「清除已完成」可用', actions2['clear-completed'].disabled === false);
check('只有已完成任务时仍渲染任务行',
    env2.document.getElementById('ml-content').querySelectorAll('.ml-task').length === 1);

// ---- 添加任务弹窗 ----
env.sendView('downloads:all');
await env.tick();
const addModal = env.document.getElementById('ml-add-modal');
env.sendAction('add-task');
await env.tick();
check('壳动作打开添加任务弹窗', addModal.classList.contains('active'));
check('打开时清空上次输入的 ID', env.document.getElementById('ml-input-id').value === '');

const submitCalls = () => env.calls('download_submit');
await app.confirmAdd();
check('ID 为空时警告且不提交',
    submitCalls().length === 0
    && env.toasts.some((toast) => toast.type === 'warning' && toast.message.includes('漫画 ID')),
    JSON.stringify(env.toasts));

env.document.getElementById('ml-input-id').value = '987654';
env.document.getElementById('ml-input-concurrency').value = 'abc';
env.document.getElementById('ml-input-priority').value = 'high';
env.document.getElementById('ml-input-auto-start').checked = false;
await app.confirmAdd();
await env.tick();
check('非法并发数回落到 3',
    submitCalls().length === 1
    && JSON.stringify(submitCalls()[0].args) === JSON.stringify(['987654', 3, 'high', false]),
    JSON.stringify(submitCalls()[0] && submitCalls()[0].args));
check('提交成功后关闭弹窗', !addModal.classList.contains('active'));
check('提交成功后刷新任务列表', env.calls('download_list').length >= 2);

console.log('manga-library 下载中心用例全部通过');
