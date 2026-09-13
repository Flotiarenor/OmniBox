// Shell 缩略图 URL 编码的无头验证脚本（由 test_shell_thumb_url_js.py 调用）。
//
// 背景：thumbUrl() 原先直接把相对路径拼进 URL（`/thumbs/${path}?plugin=…`），
// 目录名里只要出现 % / # / ? 就会坏掉 —— 实测画师目录 `pixiv/29%/`：
//   * 前端发出的 /thumbs/pixiv/29%/135504321.png 被 nginx 判为非法百分号转义，
//     在反代层直接返回 400（直连应用进程反而 200）；
//   * 缩略图因此永远加载不出来，删缓存 / 全量重建都不管用（请求没到应用）；
//   * 原图走 originalUrl() 有 encodeURIComponent，所以点进去能看。
// 这里锁住「逐段编码、保留 / 分隔符、不重复编码」这三件事。
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const SHELL_BASE_JS = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../shell/frontend/public/shell/base.js');

const win = { addEventListener() { } };
const sandbox = {
    window: win,
    parent: null,
    document: { addEventListener() { }, querySelector() { return null } },
    console,
    encodeURIComponent, decodeURIComponent, String, Math, Date, JSON, Object, Array, Error,
    setTimeout, clearTimeout,
};
const ctx = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(SHELL_BASE_JS, 'utf8'), ctx, { filename: 'shell/base.js' });

const Bridge = win.Bridge;
Bridge.setPrefix('image-viewer');

let failures = 0;
function check(name, cond, extra = '') {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name} ${extra}`);
    }
}

console.log('场景 1：实测触发问题的路径（画师目录名带 %）');
check('构建出 %25 而不是裸 %',
    Bridge.thumbUrl('pixiv/29%/135504321.png') === '/thumbs/pixiv/29%25/135504321.png?plugin=image-viewer',
    `实际 ${Bridge.thumbUrl('pixiv/29%/135504321.png')}`);

console.log('场景 2：URL 里不能出现非法字符（# 会截断、? 会变成查询串、空格需转义）');
check('空格 → %20', Bridge.thumbUrl('a b/c.png') === '/thumbs/a%20b/c.png?plugin=image-viewer',
    `实际 ${Bridge.thumbUrl('a b/c.png')}`);
check('# → %23', Bridge.thumbUrl('a#b/c.png') === '/thumbs/a%23b/c.png?plugin=image-viewer',
    `实际 ${Bridge.thumbUrl('a#b/c.png')}`);
check('? → %3F', Bridge.thumbUrl('a?b/c.png') === '/thumbs/a%3Fb/c.png?plugin=image-viewer',
    `实际 ${Bridge.thumbUrl('a?b/c.png')}`);

console.log('场景 3：分隔符保留、非 ASCII 不重复编码、空值安全');
check('目录分隔符保留', Bridge.thumbUrl('pixiv/Freehoney/1.png') === '/thumbs/pixiv/Freehoney/1.png?plugin=image-viewer');
check('多级分隔符保留', Bridge.thumbUrl('pixiv/29%/sub/1.png').split('/').length === 6,
    `实际 ${Bridge.thumbUrl('pixiv/29%/sub/1.png')}`);
check('非 ASCII 编成 UTF-8 百分号转义（与浏览器行为一致）',
    Bridge.thumbUrl('pixiv/Egami(えがみ)/1.png') === '/thumbs/pixiv/Egami(%E3%81%88%E3%81%8C%E3%81%BF)/1.png?plugin=image-viewer',
    `实际 ${Bridge.thumbUrl('pixiv/Egami(えがみ)/1.png')}`);
check('空路径不炸', Bridge.thumbUrl('') === '/thumbs/?plugin=image-viewer');
check('null 不炸', Bridge.thumbUrl(null) === '/thumbs/?plugin=image-viewer');

console.log('场景 4：往返一致性（每个路径段都能解回原值，且没有裸 %）');
const nasty = [
    'pixiv/29%/135504321.png',
    'pixiv/100%25/1.png',
    'a b/c#d/e?f&g=1.png',
    'pixiv/宮前まひろ/1.png',
    'pixiv/L.H.B@FANBOX開設中/1.png',
    'pixiv/(paren)/+plus+.png',
];
for (const p of nasty) {
    const url = Bridge.thumbUrl(p);
    const pathPart = url.slice('/thumbs/'.length, url.indexOf('?plugin='));
    const roundTrip = pathPart.split('/').map(decodeURIComponent).join('/');
    check(`往返一致: ${p}`, roundTrip === p, `实际 ${roundTrip}`);
    check(`无裸 %: ${p}`, !/%(?![0-9A-Fa-f]{2})/.test(pathPart), `实际 ${pathPart}`);
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
