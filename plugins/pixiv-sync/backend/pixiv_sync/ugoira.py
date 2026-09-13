"""ugoira（动图）作品：下载 zip 帧序列并转换为动画 WebP 保存。

Pixiv 的 ugoira 作品：
  - illust.type == "ugoira"；
  - original_image_url 指向 **zip**（帧 PNG 序列，经 i.pximg.net CDN 提供）；
  - 每帧的播放时长（delay, 毫秒）**不在 zip 里**，需另调
    `pixiv_mini.PixivClient.ugoira_metadata()`（v1/ugoira/metadata）获取
    `frames: [{file, delay}, ...]` 与 `zip_urls.medium`。

本模块只负责「zip 字节 → 动画 WebP 文件」的纯转换，网络下载与任务编排
（去重/失败语义/限流）在 download.py 的 process_ugoira 中完成。

输出使用动画 WebP（save_all）：画质好、体积小，浏览器 <img> 原生播放；
image-viewer 扫描白名单已含 .webp，缩略图生成取首帧静态，无需改宿主。
转换依赖 Pillow（宿主 image-viewer 依赖它，运行时懒加载）。

防御上限（对网络 zip 的帧数/单文件/总像素做硬限制，避免内存被恶意或
异常文件打爆）：
  - MAX_FRAMES       单作品最多帧数
  - MAX_FILE_BYTES   解压出的单帧最大字节数
  - MAX_TOTAL_PIXELS 全部帧累计像素上限（近似内存预算）
超过上限一律抛 PixivError（调用方按“失败下次重试”处理，不会崩溃）。
"""

import io
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pixiv_mini import PixivError

# 元数据缺失 delay 时的默认帧时长（毫秒，约 10fps）
DEFAULT_DELAY_MS = 100
# WebP 有损质量（ugoira 多为线稿/扁平色，q90 视觉无损且体积可控）
WEBP_QUALITY = 90

MAX_FRAMES = 1000
MAX_FILE_BYTES = 64 * 1024 * 1024  # 单帧解压上限 64MB
MAX_TOTAL_PIXELS = 400_000_000  # 累计像素上限（约 1080p×190 帧），编码内存保护


def parse_metadata(meta: Any) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """从 ugoira_metadata 响应中提取 (zip_url, frames)。

    zip_url 兼容 zip_urls.medium（新）与 zip_url（旧）；frames 每项含
    file（zip 内帧文件名）与 delay（毫秒）。
    解析失败/为空时返回 (None, [])，由调用方报错。
    """
    try:
        inner = (meta or {}).get("ugoira_metadata") or {}
        frames = inner.get("frames") or []
        zip_urls = inner.get("zip_urls") or {}
        zip_url = zip_urls.get("medium") or inner.get("zip_url") or ""
        out: List[Dict[str, Any]] = []
        for f in frames:
            if not isinstance(f, dict):
                continue
            fname = str(f.get("file") or "").strip()
            if not fname:
                continue
            out.append({"file": fname, "delay": int(f.get("delay") or 0)})
        return (str(zip_url).strip() or None), out
    except Exception:
        return None, []


def _load_pil_image():
    """懒加载 Pillow；宿主 image-viewer 依赖它，正常运行时必然可用。"""
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise PixivError("Pillow 不可用，无法把 ugoira 动图转换为 WebP") from e
    return Image


def convert(zip_path: Path, frames: List[Dict[str, Any]], out_path: Path) -> int:
    """把 ugoira zip 按 frames 顺序解码为动画 WebP 写入 out_path。

    返回成功写入的帧数。zip 缺失帧 / 超上限 / Pillow 编码失败均抛
    PixivError。out_path 若已存在会被覆盖（调用方自行保证幂等语义）。
    """
    Image = _load_pil_image()
    if not frames:
        raise PixivError("ugoira 帧清单为空，无法转换")
    if len(frames) > MAX_FRAMES:
        raise PixivError(f"ugoira 帧数 {len(frames)} 超过上限 {MAX_FRAMES}")
    try:
        with zipfile.ZipFile(str(zip_path)) as zf:
            # zip 内文件名可能带目录；按 basename 建立映射便于按 frames[].file 取帧
            name_map: Dict[str, str] = {}
            for arc in zf.namelist():
                if arc.endswith("/"):
                    continue
                name_map.setdefault(arc.rsplit("/", 1)[-1], arc)

            frames_data: List[bytes] = []
            delays: List[int] = []
            for f in frames:
                fname = f["file"].rsplit("/", 1)[-1]
                arc = name_map.get(fname)
                if arc is None:
                    raise PixivError(f"ugoira 帧 {fname!r} 不在 zip 内")
                info = zf.getinfo(arc)
                if info.file_size > MAX_FILE_BYTES:
                    raise PixivError(
                        f"ugoira 帧 {fname} 体积 {info.file_size} 超上限 {MAX_FILE_BYTES}"
                    )
                frames_data.append(zf.read(arc))
                delay = int(f.get("delay") or 0)
                delays.append(delay if delay > 0 else DEFAULT_DELAY_MS)

            if not frames_data:
                raise PixivError("ugoira zip 为空")
    except (zipfile.BadZipFile, FileNotFoundError, OSError) as e:
        raise PixivError(f"ugoira zip 损坏或不可读: {e}") from None

    # 逐帧解码并转为 RGB（ugoira 帧为 PNG，通常不透明；去掉 alpha 减小内存与体积）
    try:
        imgs = []
        total_pixels = 0
        for data in frames_data:
            im = Image.open(io.BytesIO(data))
            im.load()
            total_pixels += im.width * im.height
            if total_pixels > MAX_TOTAL_PIXELS:
                raise PixivError(
                    "ugoira 累计像素超上限"
                    f"（{total_pixels} > {MAX_TOTAL_PIXELS}），请拆分或忽略该动图"
                )
            imgs.append(im.convert("RGB"))
    except PixivError:
        raise
    except Exception as e:
        raise PixivError(f"ugoira 帧解码失败: {e}") from None

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        imgs[0].save(
            out_path,
            format="WEBP",
            save_all=True,
            append_images=imgs[1:],
            duration=delays,
            loop=0,
            quality=WEBP_QUALITY,
            method=4,
        )
    except PixivError:
        raise
    except Exception as e:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise PixivError(f"动画 WebP 编码失败: {e}") from None
    finally:
        del imgs  # 立即释放大对象

    if not out_path.exists() or out_path.stat().st_size <= 0:
        raise PixivError("动画 WebP 输出为空")
    return len(frames_data)
