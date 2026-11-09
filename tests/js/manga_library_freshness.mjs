// manga-library 统一刷新接入用例。
//
// 守四件事：
//   1. 切回前台（onShow）按**当前作用域**同步一次：书架是全部根，详情/章节/图片层是
//      该漫画目录；
//   2. 同步报告真有变化时就地重载，层级不变；阅读器打开时不关闭、不重绘它背后的网格；
//   3. 报告的零变化与 `skip` 不触发重载；
//   4. 阅读器打开的那一页交给 `manga_update_recent`，2 秒任务轮询只在任务变化时重报 chrome。
//
// 壳的 `cubby:freshness {plugin, scope, report}` 与旧的 `cubby:action: refresh`
// 两条通知路径都在这里覆盖。
import assert from 'node:assert/strict';
import { loadManga, defaultHandlers, createApp } from './manga_library_harness.mjs';

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

const multiDetail = () => ({
    folder_name: 'A', title: '漫画A', author: '作者A', is_fav: false,
    is_multi_chapter: true, info: {},
    chapters: [{ name: '第 1 话', path: 'ch1', cover_url: '' }, { name: '第 2 话', path: 'ch2', cover_url: '' }],
});

const singleDetail = () => ({
    folder_name: 'S', title: '单章漫画', author: '', is_fav: false,
    is_multi_chapter: false, info: {},
    chapters: [{ name: '全一话', path: 'only', cover_url: '' }],
});

function makeEnv(options = {}) {
    const state = { detail: multiDetail(), report: { action: 'sync', added: 0, changed: 0, removed: 0 } };
    const env = loadManga({
        freshness: options.freshness || (() => state.report),
        handlers: defaultHandlers({
            manga_list: () => [{ folder_name: 'A', title: '漫画A', author: '', page_count: 2, cover_url: '', is_fav: false }],
            manga_get_detail: () => state.detail,
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg', 'pages/3.jpg'],
        }),
    });
    return { env, state };
}

// ---- 1. onShow 按当前作用域同步 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    env.setVisible(true);
    await env.tick(10);
    app._stopPoll();
    check('切回前台同步一次', env.freshnessCalls.length === 1, JSON.stringify(env.freshnessCalls));
    check('书架态同步全部根', env.freshnessCalls[0].scope === '', JSON.stringify(env.freshnessCalls[0]));
    check('同步用的是本插件名', env.freshnessCalls[0].plugin === 'manga-library', env.freshnessCalls[0].plugin);

    await app.openMangaDetail('A');
    await env.tick();
    env.setVisible(true);
    await env.tick(10);
    app._stopPoll();
    check('详情层按漫画目录同步', env.freshnessCalls[1].scope === 'A', JSON.stringify(env.freshnessCalls[1]));
    check('chrome 上报同步作用域', env.chrome().freshness_scope === 'A',
        JSON.stringify(env.chrome().freshness_scope));

    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    app._pushChrome();
    check('图片层按同一漫画目录同步', env.chrome().freshness_scope === 'A');
}

// ---- 2. 报告有变化：章节层就地重取详情，不退回书架 ----
{
    const { env, state } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    check('打开详情在章节层', app.viewLevel === 'chapters', app.viewLevel);

    const detailCalls = env.calls('manga_get_detail').length;
    state.detail = {
        ...multiDetail(),
        chapters: [
            { name: '第 1 话', path: 'ch1', cover_url: '' },
            { name: '第 2 话', path: 'ch2', cover_url: '' },
            { name: '第 3 话', path: 'ch3', cover_url: '' },
        ],
    };
    env.sendFreshness('manga-library', { action: 'sync', added: 2, changed: 0, removed: 0 });
    await env.tick(20);

    check('章节层重取详情', env.calls('manga_get_detail').length === detailCalls + 1);
    const chapterCards = env.document.getElementById('ml-chapters').querySelectorAll('.ml-card');
    check('新增章节出现在原地', chapterCards.length === 3, String(chapterCards.length));
    check('刷新后仍在章节层', app.viewLevel === 'chapters', app.viewLevel);
    check('刷新不退回书架', app.currentView === 'all' && app.currentFolderName === 'A',
        JSON.stringify({ view: app.currentView, folder: app.currentFolderName }));
    check('刷新后仍上报二级页（无导航高亮）', env.chrome().view === '');
}

// ---- 3. 阅读器打开：不关闭、不换层级、不重绘背后网格 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();
    const pageCalls = env.calls('manga_get_pages').length;
    const detailCalls = env.calls('manga_get_detail').length;
    const taskCalls = env.calls('download_list').length;

    let open = true;
    let closed = 0;
    app.reader = { isOpen: () => open, close() { closed += 1; open = false; }, open() { } };
    env.sendFreshness('manga-library', { action: 'sync', changed: 1 });
    await env.tick(20);

    check('阅读器保持打开', open === true && closed === 0, `closed=${closed}`);
    check('阅读器打开时不重绘图片网格', env.calls('manga_get_pages').length === pageCalls);
    check('阅读器打开时不重取详情', env.calls('manga_get_detail').length === detailCalls);
    check('阅读器打开时仍重读下载任务', env.calls('download_list').length === taskCalls + 1);
    check('阅读器打开时层级不变', app.viewLevel === 'images', app.viewLevel);
}

// ---- 4. 零变化与 skip 不重载 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const detailCalls = env.calls('manga_get_detail').length;
    const listCalls = env.calls('manga_list').length;

    env.sendFreshness('manga-library', { action: 'sync', added: 0, changed: 0, removed: 0 });
    await env.tick(20);
    check('零变化不重载详情', env.calls('manga_get_detail').length === detailCalls);
    check('零变化不重绘视图', env.calls('manga_list').length === listCalls);

    env.sendFreshness('manga-library', { action: 'skip', reason: 'throttled' });
    await env.tick(20);
    check('skip 不重载', env.calls('manga_get_detail').length === detailCalls
        && env.calls('manga_list').length === listCalls);

    env.sendFreshness('other-plugin', { action: 'sync', changed: 1 });
    await env.tick(20);
    check('别的插件的报告不触发本页重载', env.calls('manga_get_detail').length === detailCalls);
}

// ---- 4b. 详情目录在刷新后已经不在了：退回书架 ----
{
    const { env, state } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    check('进入详情', app.viewLevel === 'chapters', app.viewLevel);
    state.detail = {};
    env.sendFreshness('manga-library', { action: 'sync', removed: 2 });
    await env.tick(30);
    check('目录消失后退回书架', app.viewLevel === 'home', app.viewLevel);
    check('退回书架后高亮恢复', env.chrome().view === 'all', env.chrome().view);
}

// ---- 5. 旧的 cubby:action: refresh（无 payload）仍然重载 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const detailCalls = env.calls('manga_get_detail').length;
    env.sendAction('refresh');
    await env.tick(20);
    check('旧 refresh 动作仍触发重载', env.calls('manga_get_detail').length === detailCalls + 1);
    check('旧 refresh 动作不改变层级', app.viewLevel === 'chapters', app.viewLevel);
}

// ---- 5b. 新壳两条都发：富事件 + 紧随其后的回声动作（带 payload）只重载一次 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const detailCalls = env.calls('manga_get_detail').length;
    env.sendFreshness('manga-library', { action: 'sync', changed: 1 });
    env.sendAction('refresh', { plugin: 'manga-library', scope: '', report: { action: 'sync', changed: 1 } });
    await env.tick(30);
    check('富事件之后的回声动作不重复重载', env.calls('manga_get_detail').length === detailCalls + 1,
        String(env.calls('manga_get_detail').length - detailCalls));
}
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const detailCalls = env.calls('manga_get_detail').length;
    env.sendFreshness('manga-library', { action: 'sync', added: 0, changed: 0, removed: 0 });
    env.sendAction('refresh', { plugin: 'manga-library', scope: '', report: { action: 'sync' } });
    await env.tick(30);
    check('零变化 + 回声动作也不重载', env.calls('manga_get_detail').length === detailCalls);
}
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const detailCalls = env.calls('manga_get_detail').length;
    // 回声按 payload 判定，与两条消息的到达间隔无关
    env.sendFreshness('manga-library', { action: 'sync', changed: 1 });
    await env.tick(1500);
    env.sendAction('refresh', { plugin: 'manga-library', scope: '', report: { action: 'sync', changed: 1 } });
    await env.tick(30);
    check('回声判定与到达间隔无关', env.calls('manga_get_detail').length === detailCalls + 1,
        String(env.calls('manga_get_detail').length - detailCalls));
}

// ---- 6. 阅读器页码交给 manga_update_recent ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    const opened = [];
    app.reader = {
        isOpen: () => false,
        close() { },
        open(pages, start) { opened.push({ count: pages.length, start }); },
    };
    const cards = env.document.getElementById('ml-pages').querySelectorAll('.ml-card');
    check('图片层渲染出全部页', cards.length === 3, String(cards.length));
    cards[2].click();
    await env.tick(10);

    check('阅读器从点中的页打开', opened.length === 1 && opened[0].start === 2,
        JSON.stringify(opened));
    const recent = env.calls('manga_update_recent');
    const last = recent[recent.length - 1];
    check('页码上报给「最近阅读」', last && last.args[0] === 'A' && last.args[1] === 2,
        JSON.stringify(last && last.args));
}

// ---- 6b. 浏览图片层本身不写阅读进度 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    check('浏览图片层不写阅读进度', env.calls('manga_update_recent').length === 0,
        JSON.stringify(env.calls('manga_update_recent')));
}

// ---- 6c. 灯箱自己的 ✕ / Esc 关闭也写一次最后停留的页码 ----
{
    const { env } = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    const cards = env.document.getElementById('ml-pages').querySelectorAll('.ml-card');
    cards[0].click();
    await env.tick(10);
    app.reader.next();
    app.reader.next();
    const beforeClose = env.calls('manga_update_recent').length;

    app.reader._lightbox.hide();      // 用户点灯箱自己的关闭按钮
    await env.tick(10);

    check('灯箱关闭后阅读器状态同步', app.reader.isOpen() === false);
    check('关闭写一次页码', env.calls('manga_update_recent').length === beforeClose + 1,
        `${beforeClose} -> ${env.calls('manga_update_recent').length}`);
    const last = env.calls('manga_update_recent').slice(-1)[0];
    check('关闭写的是最后停留的页码', last && last.args[1] === 2, JSON.stringify(last && last.args));
}

// ---- 6d. 快速开关：打开与关闭的两次写串行发出，后写（关闭页码）覆盖前写 ----
{
    const gates = [];
    const env = loadManga({
        handlers: defaultHandlers({
            manga_list: () => [{ folder_name: 'A', title: '漫画A', author: '', page_count: 2, cover_url: '', is_fav: false }],
            manga_get_detail: () => multiDetail(),
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg', 'pages/3.jpg'],
            // 每次写都挂起，由用例决定何时落地 —— 模拟宿主线程的异步执行
            manga_update_recent: (folder, page) => new Promise((resolve) => {
                gates.push({ folder, page, done: () => resolve({ status: 'ok' }) });
            }),
        }),
    });
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    const cards = env.document.getElementById('ml-pages').querySelectorAll('.ml-card');
    cards[0].click();                       // 打开：第 0 页 → 第一次写（挂起）
    await env.tick(0);
    check('打开时的写已发出', env.calls('manga_update_recent').length === 1,
        JSON.stringify(env.calls('manga_update_recent').map(call => call.args)));
    check('打开写的是进入的页码', gates[0] && gates[0].page === 0,
        JSON.stringify(gates[0] && gates[0].page));

    app.reader.next();
    app.reader.next();                      // 用户翻到第 2 页
    app.reader.close();                     // 立刻关闭（灯箱 ✕ 路径）
    await env.tick(0);
    check('关闭的写不并发发出（串行排在前一次之后）',
        env.calls('manga_update_recent').length === 1,
        JSON.stringify(env.calls('manga_update_recent').map(call => call.args)));

    gates[0].done();                        // 前一次写落地
    await env.tick(10);
    const recent = env.calls('manga_update_recent');
    check('前一次落地后关闭的写才发出', recent.length === 2,
        JSON.stringify(recent.map(call => call.args)));
    check('后写是关闭时停留的页码', recent[1] && recent[1].args[0] === 'A' && recent[1].args[1] === 2,
        JSON.stringify(recent[1] && recent[1].args));
    gates[1].done();
    await env.tick(10);
}

// ---- 7. 2 秒轮询：任务没变就不重报 chrome ----
{
    let tasks = [];
    const env = loadManga({
        handlers: defaultHandlers({ download_list: () => ({ tasks }) }),
    });
    const app = await createApp(env);
    await env.tick();
    const before = env.chromeAll().length;

    await app.loadTasks();
    await env.tick();
    check('任务未变化时不重报 chrome', env.chromeAll().length === before,
        `${before} -> ${env.chromeAll().length}`);

    tasks = [{ id: 't1', status: 'downloading', title: '任务一', totalImages: 10, completedImages: 1 }];
    await app.loadTasks();
    await env.tick();
    check('任务变化时重报 chrome', env.chromeAll().length === before + 1,
        `${before} -> ${env.chromeAll().length}`);
    check('重报的状态同步了新任务', env.chrome().status.text.includes('任务 1'),
        env.chrome().status.text);
}

// ---- 7b. 有任务刚完成：停在书架时重读书架（下载中心与书架共用数据根）----
{
    let tasks = [{ id: 't0', status: 'completed', title: '旧任务', totalImages: 1, completedImages: 1 }];
    const env = loadManga({ handlers: defaultHandlers({ download_list: () => ({ tasks }) }) });
    const app = await createApp(env);
    await env.tick();
    const listCalls = env.calls('manga_list').length;

    tasks = tasks.concat([
        { id: 't1', status: 'completed', title: '新书', totalImages: 2, completedImages: 2 },
    ]);
    await app.loadTasks();
    await env.tick(10);

    check('新完成任务后重读书架', env.calls('manga_list').length === listCalls + 1,
        `${listCalls} -> ${env.calls('manga_list').length}`);
}

// ---- 8. 从「最近阅读」进入：按后端记下的页码续读 ----
{
    const env = loadManga({
        handlers: defaultHandlers({
            manga_get_state: () => ({
                favorites: [],
                recent: [{
                    folder_name: 'S', title: '单章漫画', author: '', page_count: 3,
                    cover_url: '', is_fav: false, page: 2,
                }],
            }),
            manga_get_detail: () => singleDetail(),
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg', 'pages/3.jpg'],
        }),
    });
    const app = await createApp(env);
    env.sendView('recent');
    await env.tick();

    const opened = [];
    app.reader = {
        isOpen: () => false,
        close() { },
        open(pages, start) { opened.push({ count: pages.length, start }); },
    };
    env.document.getElementById('ml-content').querySelector('.ml-shelf-card').click();
    await env.tick(30);

    check('单章本子按上次页码打开阅读器', opened.length === 1 && opened[0].start === 2,
        JSON.stringify(opened));
    const recent = env.calls('manga_update_recent');
    const last = recent[recent.length - 1];
    check('续读页码上报给「最近阅读」', last && last.args[0] === 'S' && last.args[1] === 2,
        JSON.stringify(last && last.args));
}

// ---- 9. 多章本子：先选章节，记录的单页页码不跨章使用 ----
{
    const env = loadManga({
        handlers: defaultHandlers({
            manga_get_state: () => ({
                favorites: [],
                recent: [{
                    folder_name: 'A', title: '漫画A', author: '', page_count: 3,
                    cover_url: '', is_fav: false, page: 1,
                }],
            }),
            manga_get_detail: () => multiDetail(),
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg', 'pages/3.jpg'],
        }),
    });
    const app = await createApp(env);
    env.sendView('recent');
    await env.tick();

    const opened = [];
    app.reader = {
        isOpen: () => false,
        close() { },
        open(pages, start) { opened.push(start); },
    };
    env.document.getElementById('ml-content').querySelector('.ml-shelf-card').click();
    await env.tick(20);
    check('多章本子先停在章节层', app.viewLevel === 'chapters', app.viewLevel);
    check('章节层不自动打开阅读器', opened.length === 0, JSON.stringify(opened));

    env.document.getElementById('ml-chapters').querySelector('.ml-card').click();
    await env.tick(30);
    check('点进章节后停留在图片层', app.viewLevel === 'images', app.viewLevel);
    check('记录的单页页码不被用到别的章节', opened.length === 0, JSON.stringify(opened));
}

// ---- 9b. 最近阅读里搜索：搜索结果不带 page，不自动打开阅读器 ----
{
    const env = loadManga({
        handlers: defaultHandlers({
            manga_search: () => [{
                folder_name: 'S', title: '单章漫画', author: '', page_count: 3,
                cover_url: '', is_fav: false,
            }],
            manga_get_detail: () => singleDetail(),
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg', 'pages/3.jpg'],
        }),
    });
    const app = await createApp(env);
    env.sendView('recent');
    await env.tick();
    env.sendQuery('单章');
    await env.tick(10);

    const opened = [];
    app.reader = {
        isOpen: () => false,
        close() { },
        open(pages, start) { opened.push(start); },
    };
    const cards = env.document.getElementById('ml-content').querySelectorAll('.ml-shelf-card');
    check('搜索后书架渲染出结果', cards.length === 1, String(cards.length));
    cards[0].click();
    await env.tick(30);

    check('搜索结果不自动打开阅读器', opened.length === 0, JSON.stringify(opened));
}

console.log('manga-library 统一刷新用例全部通过');

