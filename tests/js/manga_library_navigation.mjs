// manga-library 多级返回与阅读器用例。
//
// 返回链：书架 → 章节（多章）→ 图片；图片返回要看是否多章。壳的左栏返回与内容页
// 独立打开时的按钮必须落到同一套状态机上。阅读器（共享灯箱或旧覆盖层）是最内层，
// 壳返回/切导航时应先关掉它 —— 迁移前返回按钮在内容页，被阅读器盖住点不到；迁到
// 壳顶栏后这条路径变成可达，不处理就会出现"点返回没反应"（内容在阅读器背后变了）。
import assert from 'node:assert/strict';
import { loadManga, defaultHandlers, createApp } from './manga_library_harness.mjs';

const check = (name, ok, detail = '') => {
    assert.ok(ok, `${name}${detail ? `：${detail}` : ''}`);
    console.log(`ok - ${name}`);
};

const multiDetail = {
    folder_name: 'A', title: '漫画A', author: '作者A', is_fav: false,
    is_multi_chapter: true, info: {},
    chapters: [{ name: '第 1 话', path: 'ch1', cover_url: '' }, { name: '第 2 话', path: 'ch2', cover_url: '' }],
};
const singleDetail = {
    folder_name: 'S', title: '单章漫画', author: '', is_fav: false,
    is_multi_chapter: false, info: {},
    chapters: [{ name: '全一话', path: 'only', cover_url: '' }],
};

function makeEnv(detail = multiDetail) {
    return loadManga({
        handlers: defaultHandlers({
            manga_list: () => [{ folder_name: 'A', title: '漫画A', author: '', page_count: 2, cover_url: '', is_fav: false }],
            manga_get_detail: (folder) => (folder === 'S' ? singleDetail : detail),
            manga_get_pages: () => ['pages/1.jpg', 'pages/2.jpg'],
        }),
    });
}

// ---- 多章：书架 → 章节 → 图片 → 章节 → 书架 ----
{
    const env = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    check('打开多章漫画进入章节层', app.viewLevel === 'chapters', app.viewLevel);
    let chrome = env.chrome();
    check('二级页清掉左栏高亮', chrome.view === '', JSON.stringify(chrome.view));
    check('二级页显示壳返回', chrome.actions.back.visible === true);
    check('二级页隐藏搜索', chrome.search.enabled === false);

    // 进入图片层（真实流程是点章节卡片；这里直接驱动状态机的下一层）
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();
    chrome = env.chrome();
    check('图片层仍是二级页高亮为空', chrome.view === '');
    check('图片层保留壳返回', chrome.actions.back.visible === true);
    check('图片层加载页面图片', env.calls('manga_get_pages').length === 1);
    check('看图时记录到「最近阅读」', env.calls('manga_update_recent').length === 1);

    env.sendAction('back');
    await env.tick();
    check('图片层返回章节层（多章）', app.viewLevel === 'chapters', app.viewLevel);
    check('回到章节层返回按钮仍在', env.chrome().actions.back.visible === true);

    env.sendAction('back');
    await env.tick();
    check('章节层返回书架', app.viewLevel === 'home' && app.currentView === 'all',
        JSON.stringify({ level: app.viewLevel, view: app.currentView }));
    chrome = env.chrome();
    check('书架层恢复导航高亮', chrome.view === 'all', chrome.view);
    check('书架层隐藏返回', chrome.actions.back.visible === false);
}

// ---- 单章：书架 → 图片 → 书架（不经过章节层） ----
{
    const env = makeEnv(singleDetail);
    const app = await createApp(env);
    await app.openMangaDetail('S');
    await env.tick();
    check('单章漫画直接进入图片层', app.viewLevel === 'images', app.viewLevel);
    env.sendAction('back');
    await env.tick();
    check('单章图片层直接返回书架', app.viewLevel === 'home' && app.currentView === 'all');
}

// ---- 阅读器打开时：壳返回先关阅读器，不改变层级 ----
{
    const env = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    let open = true;
    let closed = 0;
    app.reader = {
        isOpen: () => open,
        close() { closed += 1; open = false; },
    };
    let loadViewCalls = 0;
    const realLoadView = app.loadView.bind(app);
    app.loadView = async () => { loadViewCalls += 1; return realLoadView(); };

    env.sendAction('back');
    await env.tick();
    check('阅读器打开时返回先关阅读器', closed === 1, `closed=${closed}`);
    check('关阅读器不改变返回层级', app.viewLevel === 'images', app.viewLevel);
    check('关阅读器不重绘内容', loadViewCalls === 0, `loadView=${loadViewCalls}`);

    // 再点返回才退到章节层
    env.sendAction('back');
    await env.tick();
    check('阅读器关闭后返回退到章节层', app.viewLevel === 'chapters', app.viewLevel);
}

// ---- 阅读器打开时点左栏导航：先关阅读器再切视图 ----
{
    const env = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    app.viewLevel = 'images';
    app._chapterPath = 'ch1';
    await app.loadView();
    await env.tick();

    let open = true;
    let closed = 0;
    app.reader = { isOpen: () => open, close() { closed += 1; open = false; } };
    env.sendView('favorites');
    await env.tick();
    check('切导航时关闭阅读器', closed === 1, `closed=${closed}`);
    check('切导航后到达目标视图', app.currentView === 'favorites' && app.viewLevel === 'home',
        JSON.stringify({ view: app.currentView, level: app.viewLevel }));
    check('切导航后左栏高亮同步', env.chrome().view === 'favorites');
}

// ---- 独立打开（无壳）：内容内保留返回按钮，壳内不输出 ----
{
    const env = makeEnv();
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    check('壳内不输出内容返回按钮',
        !env.document.getElementById('ml-content').innerHTML.includes('ml-detail-back'));
}
{
    const env = makeEnv();
    env.sandbox.parent = env.sandbox;                    // 脱离壳：window.parent === window
    const app = await createApp(env);
    await app.openMangaDetail('A');
    await env.tick();
    const back = env.document.getElementById('ml-detail-back');
    check('独立打开时输出内容返回按钮', !!back);
    back.click();
    await env.tick();
    check('独立打开的返回走同一状态机', app.viewLevel === 'home' && app.currentView === 'all',
        JSON.stringify({ level: app.viewLevel, view: app.currentView }));
}

// ---- 阅读器两个分支：共享灯箱 / 旧覆盖层回退 ----
{
    const env = loadManga({ handlers: defaultHandlers() });
    const reader = new env.MangaReader();
    check('阅读器暴露打开状态查询', typeof reader.isOpen === 'function');
    check('初始未打开', reader.isOpen() === false);
    reader.open(['p/1.jpg', 'p/2.jpg', 'p/3.jpg'], 1);
    check('有共享灯箱时走灯箱',
        env.lightboxEvents.some((event) => event.type === 'show'
            && event.count === 3 && event.index === 1),
        JSON.stringify(env.lightboxEvents));
    check('灯箱模式打开状态为真', reader.isOpen() === true);
    check('走灯箱时隐藏旧覆盖层', reader.container.style.display === 'none');
    reader.next();
    check('灯箱模式翻页交给共享组件',
        env.lightboxEvents.some((event) => event.type === 'navigate' && event.index === 2));
    reader.close();
    check('关闭灯箱', env.lightboxEvents.some((event) => event.type === 'hide'));
    check('关闭后状态为假', reader.isOpen() === false);
    check('关闭后序列清空', reader.pages.length === 0);
}
{
    const env = loadManga({ handlers: defaultHandlers(), noLightbox: true });
    const reader = new env.MangaReader();
    reader.open(['p/1.jpg', 'p/2.jpg'], 0);
    check('缺少共享组件时回退旧覆盖层',
        reader.container.style.display === 'flex' && reader.isOpen() === true);
    const img = reader.container.querySelector('img');
    check('回退层用 /file 包装路径',
        img.src === '/file?path=p%2F1.jpg', img.src);
    check('回退层显示页码',
        reader.container.querySelector('.reader-info').textContent === '1 / 2',
        reader.container.querySelector('.reader-info').textContent);
    reader.next();
    check('回退层翻页换图',
        img.src === '/file?path=p%2F2.jpg'
        && reader.container.querySelector('.reader-info').textContent === '2 / 2');
    reader.close();
    check('回退层关闭后隐藏',
        reader.container.style.display === 'none' && reader.isOpen() === false);
}

console.log('manga-library 返回/阅读器用例全部通过');
