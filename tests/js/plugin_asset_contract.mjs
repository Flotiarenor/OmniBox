// plugin 前端「资源装载契约」的统一入口：覆盖**所有**插件前端（含只有 1 个脚本的）。
//
// 与 tests/js/image_viewer_app_split.mjs / media_player_app_split.mjs 的分工：
//   - 那两个用 runScriptLoadContract（需要入口类名）额外校验原型成员契约；
//   - 这里用 runAssetContract 覆盖 index.html 与 js/ 的一致性 + 装载期错误，
//     于是 novel-reader / manga-library / image-cleaner / netease-music 这些
//     没有专门入口的插件也被门禁覆盖（此前只覆盖 6 个前端里的 2 个）。
//
// 用法：node tests/js/plugin_asset_contract.mjs
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runAssetContract } from './script_load_contract.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const PLUGINS = path.join(here, '..', '..', 'plugins');

const frontends = fs.readdirSync(PLUGINS)
    .map(name => ({ name, dir: path.join(PLUGINS, name, 'frontend') }))
    .filter(({ dir }) => fs.existsSync(path.join(dir, 'index.html')))
    .sort((a, b) => a.name.localeCompare(b.name));

if (frontends.length < 2) {
    console.error(`plugin_asset_contract: 只发现 ${frontends.length} 个插件前端，路径可能不对`);
    process.exit(1);
}

let failures = 0;
for (const { name, dir } of frontends) {
    failures += runAssetContract({ label: name, frontendDir: dir });
}

console.log(`\nplugin_asset_contract: ${frontends.length} 个插件前端，${failures} 项失败`);
process.exit(failures ? 1 : 0);
