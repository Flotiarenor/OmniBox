// media-player 前端脚本装载契约（共享检查器见 script_load_contract.mjs）。
//
// 这份清单是"拆媒体播放器 app.js（1993 行，83 个成员）"时的搬移契约：逐个搬走都必须
// 仍然存在。它是子集断言 —— 新增方法不必更新本文件，只有**丢方法或改名**才会失败。
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runScriptLoadContract } from './script_load_contract.mjs';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)),
    '../../plugins/media-player/frontend');

const REQUIRED_METHODS = [
    '_applyEQPreset', '_bindContentDelegation', '_bindFsMove', '_bindKeyboard',
    '_bindThumbPrefetch', '_bindUI', '_buildEmpty', '_buildEQBands',
    '_clearNeteaseCache', '_clearQueue', '_closePlaylistMenu', '_confirmEQName',
    '_confirmPlaylistModal', '_doScan', '_ensureIndex', '_enterFullscreen',
    '_exitFullscreen', '_findAlbumCover', '_findAlbumName', '_handleRowAction',
    '_hideControls', '_hideEQ', '_highlightRows', '_isSameDay',
    '_isVideoShowing', '_loadCurrentView', '_loadEQPresets', '_ncmCacheGet',
    '_ncmCacheSet', '_neteaseToMediaItem', '_observeThumbImgs', '_onDocumentClick',
    '_onStageClick', '_openAddToPlaylist', '_openPlaylistModal', '_openSettings',
    '_prefetchThumbs', '_prepareNeteaseItems', '_refreshRowState', '_renderAlbums',
    '_renderDetail', '_renderEmpty', '_renderList', '_renderNeteaseCliMissing',
    '_renderNeteaseLogin', '_renderNeteasePlaylists', '_renderQueue', '_resetEQ',
    '_restorePlayback', '_rowHtml', '_scheduleAutoHide', '_setLoading',
    '_showControls', '_stopAutoHide', '_toggleCurrentFavorite', '_toggleEQ',
    '_toggleLyrics', '_toggleQueue', '_toggleVideoMode', '_toggleWideMode',
    '_updateFavButton', '_updateMiniEq', '_updatePlayerCover', '_updateStageBackdrop',
    '_updateStageCover', '_updateStats', '_waitScanDone',
    'init', 'loadExtensions', 'onPlayStateChange', 'onTimeUpdate',
    'onTrackChange', 'openAlbum', 'openNeteasePlaylist', 'openNeteaseView',
    'openPlaylist', 'showPlaylistMenu', 'switchView', 'toggleFullscreen',
    'updatePlayModeUI', 'updateStageLyrics', 'updateVolumeUI',
];

process.exit(runScriptLoadContract({
    label: 'media-player',
    frontendDir: FRONTEND,
    className: 'MediaPlayerApp',
    requiredMethods: REQUIRED_METHODS,
}) === 0 ? 0 : 1);
