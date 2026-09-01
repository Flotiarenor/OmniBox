"""media-player 后台扫描任务冒烟测试：启动 → 进度 → 完成 → 断点恢复 → 封面。

运行：venv\\Scripts\\python.exe tests\\test_media_scan_task.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.util  # noqa: E402

from shell.backend.tasks import BackgroundTask  # noqa: E402

# 测试数据放工作区内（沙箱/CI 下系统临时目录可能不可写）
_TMP_BASE = PROJECT_ROOT / '.build' / 'mp-scan-test'

# 最小 JPEG 魔数（文件夹封面透传，不经过 Pillow 解码）
_FAKE_JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64


def _load_plugin_module():
    entry = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('media_player_test', str(entry))
    assert spec and spec.loader, '无法加载插件模块'
    module = importlib.util.module_from_spec(spec)
    sys.modules['media_player_test'] = module
    spec.loader.exec_module(module)
    return module


_plugin_mod = _load_plugin_module()
MediaPlayerPlugin = _plugin_mod.MediaPlayerPlugin


def make_media_dir(base: Path, name: str, files: int, cover: bool = False) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (d / f'track{i:02d}.mp3').write_bytes(b'ID3\x04\x00' + bytes([i]) * 64)
    if cover:
        (d / 'folder.jpg').write_bytes(_FAKE_JPEG)
    return d


def wait_done(plugin, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = plugin.scan_status()
        if st['state'] in ('done', 'cancelled', 'none'):
            return st
        time.sleep(0.2)
    raise TimeoutError('扫描未在超时内完成')


def main():
    _TMP_BASE.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix='run-', dir=str(_TMP_BASE)))
    try:
        root = tmp / 'root'
        root.mkdir()
        d1 = make_media_dir(root, 'album-a', 3, cover=True)   # 有文件夹封面
        d2 = make_media_dir(root, 'album-b', 2)               # 无封面
        manifest = {'name': 'media-player'}
        config = {'directories': {'data_root': str(root)}}

        # 1. 首次增量扫描
        p = MediaPlayerPlugin(manifest, config)
        r = p.scan(False)
        assert r.get('started'), f'扫描未启动: {r}'
        st = wait_done(p)
        assert st['state'] == 'done', f'状态异常: {st}'
        assert st['extra']['audio'] == 5, f'音频数错误: {st}'
        assert p.stats()['total'] == 5, f'索引总数错误: {p.stats()}'
        assert p.scan_status()['state'] == 'done'
        print(f'[1] 首次扫描 OK: audio={st["extra"]["audio"]} video={st["extra"]["video"]}')

        # 2. 封面：有文件夹封面的条目 has_cover=True，/thumbs 数据可生成；无封面为 False
        items = p.all_audio()
        a_items = [i for i in items if 'album-a' in i['path']]
        b_items = [i for i in items if 'album-b' in i['path']]
        assert all(i['has_cover'] for i in a_items), 'album-a 应有封面标记'
        assert all(not i['has_cover'] for i in b_items), 'album-b 不应有封面标记'
        data, mime = p.get_thumb_data(a_items[0]['id'])
        assert data and mime == 'image/jpeg', f'封面生成失败: {mime}'
        assert p.get_thumb_data(b_items[0]['id']) is None, '无封面条目应返回 None'
        assert p.get_thumb_data('unknown_id_123') is None, '未知 id 应返回 None'
        albums = p.audio_albums()
        assert any(a['cover_item_id'] for a in albums), '专辑应带 cover_item_id'
        print('[2] 封面懒生成 OK（has_cover / get_thumb_data / cover_item_id）')

        # 2.5 前端 canvas 抽帧回写（media_put_thumb → ThumbCache.put）与失效
        import base64
        b64 = base64.b64encode(_FAKE_JPEG).decode()
        r = p.put_thumb(b_items[0]['id'], f'data:image/jpeg;base64,{b64}')
        assert r['success'], f'回写失败: {r}'
        data2, mime2 = p.get_thumb_data(b_items[0]['id'])
        assert data2 == _FAKE_JPEG and mime2 == 'image/jpeg', '回写后应命中缓存'
        # 参数校验
        assert not p.put_thumb('', 'data:image/jpeg;base64,xxx')['success']
        assert not p.put_thumb('unknown_id', f'data:image/jpeg;base64,{b64}')['success']
        assert not p.put_thumb(b_items[0]['id'], 'http://x/y.jpg')['success']
        # 源文件替换 → mtime/size 失效 → 无封面源 → None
        b_path = Path(b_items[0]['path'])
        b_path.write_bytes(b'ID3\x04\x00' + b'Y' * 128)
        assert p.get_thumb_data(b_items[0]['id']) is None, '源文件替换后封面应失效'
        print('[2.5] 前端回写与失效 OK')

        # 2.6 预取查询（thumb_missing）与孤儿封面清理（prune）
        assert p._thumb_cache.has(a_items[0]['id'], Path(a_items[0]['path'])) is True, '已生成封面 has 应为 True'
        res = p.thumb_missing([a_items[0]['id'], b_items[0]['id'], 'unknown_id_123'])
        assert a_items[0]['id'] not in res['missing'], '已缓存的不应列为缺失'
        assert b_items[0]['id'] in res['missing'], '未缓存/已失效的应列为缺失'
        # 模拟已删除文件遗留的孤儿封面条目
        orphan_src = Path(b_items[0]['path'])
        p._thumb_cache.put('orphan_key_1', _FAKE_JPEG, 'image/jpeg', orphan_src)
        p._thumb_cache.put('orphan_key_2', _FAKE_JPEG, 'image/jpeg', orphan_src)
        pruned = p._thumb_cache.prune(set(p._items.keys()))
        assert pruned >= 2, f'应清理孤儿条目: {pruned}'
        assert p._thumb_cache.has('orphan_key_1', orphan_src) is False, '孤儿条目应已删除'
        print('[2.6] 预取查询与孤儿清理 OK')

        # 3. 后补文件夹封面：增量扫描应重建条目（封面图比媒体文件新）
        (d2 / 'folder.jpg').write_bytes(_FAKE_JPEG)
        r = p.scan(False)
        assert r.get('started')
        st = wait_done(p)
        assert st['state'] == 'done'
        b_items2 = [i for i in p.all_audio() if 'album-b' in i['path']]
        assert all(i['has_cover'] for i in b_items2), '后补封面后增量扫描应刷新 has_cover'
        print('[3] 后补封面增量刷新 OK')

        # 4. 任务文件已落盘（done 态，重启时会被清理）
        task_file = root / '.cache' / 'scan_task.json'
        assert task_file.exists(), '任务文件未生成'
        p2 = MediaPlayerPlugin(manifest, config)
        assert p2._scan_task is None, 'done 态任务应被清理'
        assert not task_file.exists(), 'done 态任务文件应被删除'
        print('[4] done 态任务清理 OK')

        # 5. 断点续传：手工构造 paused 任务（两个根目录已完成），新实例应跳过它们
        task = BackgroundTask(kind='scan', persist_path=task_file,
                              extra={'force': False, 'completed_roots': [str(d1), str(d2)]})
        task.update(processed=2, total=2, extra={'completed_roots': [str(d1), str(d2)]})
        task.persist()  # queued → 落盘为 paused
        p3 = MediaPlayerPlugin(manifest, config)
        assert p3._scan_task is not None and p3._scan_task.state == 'paused', 'paused 任务未恢复'
        r3 = p3.scan(False)
        assert r3.get('started'), f'续扫未启动: {r3}'
        st3 = wait_done(p3)
        assert st3['state'] == 'done'
        assert st3['extra']['audio'] == 5
        assert p3.stats()['total'] == 5
        print('[5] 断点续传 OK（paused 恢复 → 续扫完成）')

        # 6. 深度扫描（force）丢弃断点全量重扫
        p4 = MediaPlayerPlugin(manifest, config)
        r4 = p4.scan(True)
        assert r4.get('started'), f'深度扫描未启动: {r4}'
        st4 = wait_done(p4)
        assert st4['state'] == 'done'
        assert st4['extra']['audio'] == 5
        print('[6] 深度扫描 OK')

        # 7. 运行中重复调用应报错
        p5 = MediaPlayerPlugin(manifest, config)
        r5 = p5.scan(False)
        assert r5.get('started')
        r5b = p5.scan(False)
        assert r5b.get('error') == '扫描正在进行中', f'重复启动未拦截: {r5b}'
        st5 = wait_done(p5)
        assert st5['state'] == 'done'
        print('[7] 重复启动拦截 OK')

        # 8. 取消
        p6 = MediaPlayerPlugin(manifest, config)
        r6 = p6.scan(False)
        assert r6.get('started')
        assert p6.scan_cancel()['success'] is True
        st6 = wait_done(p6)
        assert st6['state'] == 'cancelled', f'取消后状态异常: {st6}'
        # 取消后任务文件应为 paused（下次可续跑）
        assert task_file.exists()
        print('[8] 取消 OK（检查点保留，可续跑）')

        print('\n全部通过 (PASS)')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
