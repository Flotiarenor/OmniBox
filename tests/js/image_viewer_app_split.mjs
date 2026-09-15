// image-viewer 前端脚本装载契约（共享检查器见 script_load_contract.mjs）。
//
// 这份清单是"拆图片相册 app.js（1500 行，67 个成员）"时的搬移契约：逐个搬走都必须仍然
// 存在。它是子集断言 —— 新增方法不必更新本文件，只有**丢方法或改名**才会失败。
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runScriptLoadContract } from './script_load_contract.mjs';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../plugins/image-viewer/frontend');

const REQUIRED_METHODS = [
    'constructor', 'init', '_bindPluginLifecycle', 'loadSettings',
    '_applyAlbumSortSettings', 'loadExtensions', 'openExtensionView', 'closeExtensionView',
    '_bindUI', 'loadAlbums', 'showAlbums', '_renderTimeline',
    '_renderAlbumCards', '_isCollapsed', '_filterVisibleAlbums', '_ensureGrid',
    '_gridConfigPath', '_gridIsPixiv', '_albumPageIsPixiv', '_sortAlbums',
    'openAlbum', '_showFolder', '_rememberScroll', '_restoreScroll',
    '_popNavStack', '_handleBack', 'showAlbumMenu', '_closeAlbumMenu',
    '_updateStats', '_resetRootsList', '_rootPaths', 'openNewAlbumModal',
    'closeNewAlbumModal', 'createAlbum', 'loadImages', 'renderJustifiedLayout',
    'filterCurrentImages', 'toggleSlideshow', '_stopSlideshow', '_cancelSlideshow',
    'toggleMultiSelectMode', '_updateSelectionCount', 'toggleSelectImage', 'clearSelection',
    'handleContextAction', 'deleteSelectedImages', 'openMoveModal', 'closeMoveModal',
    'confirmMove', 'refreshView', 'rebuildAll', 'rebuildFolder',
    '_startRebuildTask', '_waitRebuildDone', '_updateRebuildProgress', 'hideRebuildProgress',
    'cancelRebuild', 'refreshSelectedThumbs', 'openSettingsModal', 'closeSettingsModal',
    'saveSettings', '_formatDuration', '_monthKey', '_timeAgo',
    '_emptyHtml', '_escapeHtml', '_escapeAttr',
];

process.exit(runScriptLoadContract({
    label: 'image-viewer',
    frontendDir: FRONTEND,
    className: 'ImageViewer',
    requiredMethods: REQUIRED_METHODS,
}) === 0 ? 0 : 1);
