"""暂停必须真的中断下载循环：界面显示"已暂停"而线程继续写盘是原来的缺陷。

原来的实现里 `download_pause` 只做了两件事 —— 置 `_stop_event`、把状态改成
`paused`；而下载循环（jmcomic 的调度器）从头到尾没有读过这个信号，唯一一次判断在
`download_album` 之前。于是暂停之后线程照旧一张一张写盘，直到整本下完为止。

修法是在调度器真正调用的两个函数上各放一个块级检查点（章节级 + 图片级），
本用例钉住的正是这个行为：

  * 真实临时目录 + 真实线程（插件自己的工作线程 + 假客户端的图片线程池）；
  * 用插件自己的 `download_submit` / `download_pause` API，不绕过状态机；
  * 断言"暂停后不再出现新的写盘、且最后一次写入距暂停不到 1 秒"，而不是观察
    mock 调用次数。

第三方下载客户端换成假实现是**测试环境限制**（真实下载要连网络），不是被测对象
被替换：写盘、线程、取消点、状态机都是仓库自己的代码。假实现按 jmcomic 的顺序
调用钩子（章节 → 图片 → after_image），因此取消点确实落在"落盘之前"。
"""

import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'manga-library'
PLUGIN_BACKEND = PLUGIN_DIR / 'backend'

from shell.backend.plugin_utils import load_sibling
from shell.backend.settings_store import SettingsStore
from tools.check_plugins import _load_backend_class

downloader = load_sibling(str(PLUGIN_BACKEND / 'downloader.py'), 'downloader',
                          'manga_library_cancel_test')

#: 假客户端的规模：图片数量足够多，暂停一定发生在下完之前。
IMAGES_PER_PHOTO = 120
PHOTO_COUNT = 1
#: 单张图片的"落盘"耗时（秒）。真实实现里这段是网络传输，假实现用可控延迟代替，
#: 也顺带保证"暂停后 1 秒内停止写盘"这条断言有可观测的窗口。
WRITE_DELAY = 0.02


def _build_client(writes: list, write_delay: float = WRITE_DELAY,
                  images: int = IMAGES_PER_PHOTO):
    """构造 jmcomic 的最小替身：只实现 `execute_download` 用到的符号与钩子顺序。

    `writes` 追加 `(序号, 写盘完成时刻)`，供用例判断暂停之后是否还有新写入。
    """
    image_total = images * PHOTO_COUNT

    class _AlbumText:
        @staticmethod
        def get_album_cover_url(album_id):
            return f'https://example.invalid/{album_id}.jpg'

    class _Photo:
        def __init__(self, number):
            self.photo_id = str(number)
            self.name = f'chapter-{number}'
            self.page_arr = list(range(images))
            self.from_album = None

        def __len__(self):
            return len(self.page_arr)

        def __iter__(self):
            return iter(self.page_arr)

    class _Album:
        album_id = '123456'

        def __init__(self):
            self.oname = 'test-album'
            self.name = '测试漫画'
            self.author = 'tester'
            self.tags = []
            self.page_count = image_total
            self._photos = [_Photo(number) for number in range(PHOTO_COUNT)]

        def __len__(self):
            return len(self._photos)

        def __iter__(self):
            return iter(self._photos)

    class _Option:
        def __init__(self, base_dir: Path, image_threads: int):
            self.base_dir = base_dir
            self.image_threads = image_threads
            self.dir_rule = None

        @staticmethod
        def construct(option_dict):
            rule = option_dict['dir_rule']
            threading_spec = option_dict['download']['threading']
            return _Option(Path(rule['base_dir']), int(threading_spec['image']))

    class _DirRule:
        def __init__(self, rule, base_dir):
            self.rule = rule
            self.base_dir = base_dir

    class _Downloader:
        """与 jmcomic.JmDownloader 同名的调度与钩子，顺序一致。"""

        def __init__(self, option):
            self.option = option
            self._album = _Album()
            self._album_dir = option.base_dir

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return None

        def before_album(self, album):
            pass

        def before_photo(self, photo):
            pass

        def after_image(self, image, img_save_path):
            pass

        def download_album(self, album_id):
            album = self._album
            self._album_dir = self.option.base_dir / album.album_id
            self.before_album(album)
            for photo in album:
                self.download_by_photo_detail(photo)
            return album

        def download_by_photo_detail(self, photo):
            self.before_photo(photo)
            with ThreadPoolExecutor(max_workers=self.option.image_threads) as pool:
                list(pool.map(self.download_by_image_detail, photo.page_arr))

        def download_by_image_detail(self, image):
            # 真实 jmcomic 在这里把图片写到磁盘；这一行的位置就是"落盘"。
            path = self._album_dir / f'{image:05d}.jpg'
            path.parent.mkdir(parents=True, exist_ok=True)
            time.sleep(write_delay)
            path.write_bytes(b'x' * 1024)
            writes.append((image, time.time()))
            self.after_image(image, str(path))

    return SimpleNamespace(JmcomicText=_AlbumText,
                           JmOption=_Option,
                           DirRule=_DirRule,
                           JmDownloader=_Downloader)


class DownloadPauseCancelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'manga'
        self.root.mkdir(parents=True, exist_ok=True)
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'MangaLibraryPlugin')
        cls._resolved_config = {'root_dir': str(self.root)}
        self.addCleanup(setattr, cls, '_resolved_config', {})
        self.plugin = cls({'name': 'manga-library'},
                          {'directories': {'data_root': str(self.root)}})
        self.plugin._settings_store = SettingsStore(
            str(Path(self._tmp.name) / 'settings'))
        self.addCleanup(self.plugin.on_unload)
        self.writes: list = []
        self.api = self.plugin.register_api()

    def _use_fake_client(self, images: int = IMAGES_PER_PHOTO):
        """把插件装配的下载执行器换成"假客户端 + 真实执行器"。"""
        writes = self.writes

        def run(task, manga_dir, state_dir, lock, logger):
            return downloader.execute_download(
                task, manga_dir, state_dir, lock, logger,
                client_module=_build_client(writes, images=images))

        self.plugin.downloads._run_download = run

    def _submit(self, concurrency: int = 2, auto_start: bool = True,
                images: int = IMAGES_PER_PHOTO) -> str:
        self._use_fake_client(images=images)
        result = self.api['download_submit']('123456', concurrency=concurrency,
                                             auto_start=auto_start)
        self.assertTrue(result.get('success'), result)
        self.addCleanup(self._stop_worker, result['taskId'])
        return result['taskId']

    def _call_within(self, func, *args, timeout: float = 5.0):
        """在独立线程里调一个可能卡住的 API，返回 (是否返回, 返回值)。

        锁重入死锁不会抛异常，只会永远不返回 —— 直接把 API 调在测试线程上会让整个用例
        挂死（CI 里表现为 job 超时），因此用带超时的看护线程把它变成一次失败。
        """
        box: dict = {}

        def call():
            try:
                box['value'] = func(*args)
            except Exception as error:      # pragma: no cover - 失败会被下面的断言看到
                box['error'] = error

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(timeout)
        self.assertFalse(caller.is_alive(), f'{func.__name__} 没有返回（疑似锁重入死锁）')
        self.assertNotIn('error', box, f'{func.__name__} 抛异常: {box.get("error")}')
        return box.get('value')

    def _stop_worker(self, task_id: str) -> None:
        task = self.plugin.downloads.tasks.get(task_id)
        if task is not None and task._thread is not None:
            task._stop_event.set()
            task._thread.join(10)

    def _wait_until(self, predicate, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def _status(self, task_id: str) -> str:
        tasks = {task['id']: task for task in self.api['download_list']()['tasks']}
        return tasks.get(task_id, {}).get('status', '')

    def _image_files(self) -> list:
        download_dir = self.root / '123456'
        if not download_dir.is_dir():
            return []
        return sorted(download_dir.glob('*.jpg'))

    def test_pause_stops_writing_within_one_second(self):
        task_id = self._submit()

        # 等到"下载已开始且真的写了盘"：此时线程一定在循环中，暂停才有意义。
        self.assertTrue(self._wait_until(
            lambda: self._status(task_id) == 'downloading' and len(self.writes) >= 2),
            f'下载线程没有开始写盘：status={self._status(task_id)} writes={len(self.writes)}')

        paused_at = time.time()
        result = self.api['download_pause'](task_id)
        self.assertTrue(result.get('success'), result)

        task = self.plugin.downloads.tasks[task_id]
        task._thread.join(5)
        self.assertFalse(task._thread.is_alive(),
                         '暂停后下载线程必须结束，不能停在循环里继续写盘')

        late = [stamp for _index, stamp in self.writes if stamp > paused_at]
        self.assertLess(max(late or [paused_at]) - paused_at, 1.0,
                        f'暂停后仍有写盘落在 1 秒之外（共 {len(late)} 次）')
        # 停在半路：真暂停而不是"刚好下完了"
        self.assertLess(len(self.writes), IMAGES_PER_PHOTO * PHOTO_COUNT,
                        '暂停时整本已经下完，这条用例没有验证到取消')
        self.assertEqual(len(self._image_files()), len(self.writes),
                         '磁盘上的图片数应与记录的写盘次数一致')
        self.assertEqual(task.completed_images, len(self.writes),
                         '暂停后进度计数也不该继续涨')
        self.assertEqual(self._status(task_id), 'paused',
                         '被暂停的任务不能被改回 completed')

    def test_unpaused_download_completes_all_images(self):
        """对照：不暂停时假客户端会把全部图片写完 —— 证明上一条的"停"来自取消点。"""
        task_id = self._submit()
        task = self.plugin.downloads.tasks[task_id]
        task._thread.join(30)
        self.assertFalse(task._thread.is_alive())
        self.assertEqual(len(self.writes), IMAGES_PER_PHOTO * PHOTO_COUNT)
        self.assertEqual(len(self._image_files()), IMAGES_PER_PHOTO * PHOTO_COUNT)
        self.assertEqual(self._status(task_id), 'completed')
        self.assertTrue((self.root / '123456' / 'album_info.json').is_file(),
                        '正常完成的任务应写出 album_info.json')

    def test_pause_before_first_write_keeps_directory_empty(self):
        """暂停落在第一张图之前：一张都不该写。"""
        self._use_fake_client()
        result = self.api['download_submit']('123456', concurrency=2,
                                             auto_start=False)
        self.assertTrue(result.get('success'), result)
        task_id = result['taskId']
        self.addCleanup(self._stop_worker, task_id)
        self.assertEqual(self._status(task_id), 'paused', 'auto_start=False 应停在 paused')
        self.assertEqual(self.writes, [])

    def test_resume_after_pause_starts_a_working_thread(self):
        """恢复必须能返回并真的把线程跑起来。

        旧实现把 `_start` 写在**已持有**的 `threading.Lock` 里，而 `_start` 自己又进一次
        同一把锁 —— 恢复 / 重试 / 全部开始三个入口会永久挂住请求线程。这类缺陷不抛异常，
        只有"调用必须返回"的断言能抓到。
        """
        task_id = self._submit(auto_start=False, images=12)
        self.assertEqual(self._status(task_id), 'paused')

        resumed = self._call_within(self.api['download_resume'], task_id)
        self.assertTrue(resumed.get('success'), resumed)

        task = self.plugin.downloads.tasks[task_id]
        task._thread.join(30)
        self.assertFalse(task._thread.is_alive())
        self.assertEqual(self._status(task_id), 'completed')
        self.assertEqual(len(self.writes), 12, '恢复后应把整本下完')

    def test_retry_after_failure_starts_a_working_thread(self):
        """重试同样在锁内起线程：失败一次后重试必须能返回并跑完。"""
        writes = self.writes
        attempts = {'value': 0}

        def run(task, manga_dir, state_dir, lock, logger):
            attempts['value'] += 1
            if attempts['value'] == 1:
                raise RuntimeError('假客户端第一次失败')
            return downloader.execute_download(
                task, manga_dir, state_dir, lock, logger,
                client_module=_build_client(writes, images=12))

        self.plugin.downloads._run_download = run
        result = self.api['download_submit']('123456', concurrency=2)
        self.assertTrue(result.get('success'), result)
        task_id = result['taskId']
        self.addCleanup(self._stop_worker, task_id)
        self.plugin.downloads.tasks[task_id]._thread.join(30)
        self.assertTrue(self._wait_until(lambda: self._status(task_id) == 'failed'),
                        f'第一次运行应失败：status={self._status(task_id)}')

        retried = self._call_within(self.api['download_retry'], task_id)
        self.assertTrue(retried.get('success'), retried)

        task = self.plugin.downloads.tasks[task_id]
        task._thread.join(30)
        self.assertFalse(task._thread.is_alive())
        self.assertEqual(self._status(task_id), 'completed')
        self.assertEqual(len(self.writes), 12)

    def test_start_all_starts_paused_tasks(self):
        """「全部开始」一次起多个线程，同样在锁内调用，必须能返回。"""
        first = self._submit(auto_start=False, images=8)
        second = self._submit(auto_start=False, images=8)
        self.assertEqual(self._status(first), 'paused')
        self.assertEqual(self._status(second), 'paused')

        started = self._call_within(self.api['download_start_all'], timeout=10.0)
        self.assertTrue(started.get('success'), started)

        for task_id in (first, second):
            task = self.plugin.downloads.tasks[task_id]
            task._thread.join(30)
            self.assertFalse(task._thread.is_alive(), f'{task_id} 的线程没有结束')
            self.assertEqual(self._status(task_id), 'completed')
        self.assertEqual(len(self.writes), 16)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
