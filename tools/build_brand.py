# -*- coding: utf-8 -*-
"""生成应用图标（6 套配色）与安装器向导图。

产出
----
1. `res/brand/cubby-<主题>.ico` —— 每套一份多尺寸 ICO（9 档，逐档独立渲染）；
2. `res/brand/png/cubby-<主题>-<尺寸>.png` —— 同一套图的 PNG 导出（Linux 桌面项、
   关于页、README 用；透明背景，不含底板之外的内容）；
3. `docs/Releases/installer-banner-<档>.png` —— Inno Setup 向导左侧大图；
4. `docs/Releases/installer-small-<档>.png` —— 向导右上角小图。

为什么图标要逐档渲染而不是缩放
------------------------------
16px 与 256px 是两张不同的画：小尺寸必须去掉内部记号、加粗间隙，否则整格糊成一块。
同一份矢量缩到 16px 会得到"看起来像、但认不出来"的结果，这也是任务栏图标最常见的失败。

为什么安装器图要分 DPI 档
-------------------------
Inno Setup 6.6 起，100% DPI 的向导图区是 202×386（更早的版本是 164×314），125%/150%
还要放大。只给一张 164×314 会被放大 1.23 倍显示，文字与线条最先糊。

为什么两套向导图各自分档、且大图不放副标题
------------------------------------------
左侧大图在每一页都出现，作用是"让用户记住这个标"，不是讲产品；文案属于右侧说明页。
所以它只有：标、三张插件卡、产品名，没有第三行字。

为什么校验分三档、深浅不同
--------------------------
图标（`res/brand/**`）只有几何绘制，不读字体，任何平台逐字节可复现；向导图里有
"格子铺 / Cubby"文字，用 `C:/Windows/Fonts` 下的系统字体（微软雅黑 / Segoe UI）渲染。
没有这些字体的机器上 `ImageFont.truetype` 直接抛 `OSError: cannot open resource`；
而且不同 Windows 版本 / Server 镜像自带的字体版本不同，同一份脚本渲染出的 PNG 字节
也会不同 —— "逐字节比对向导图"因此只在生成它们的机器上有意义，不能当 CI 门禁：

- `--check-icons`：逐字节比对图标；不读字体，任何平台都能跑（CI 的 lint 用这条）；
- `--check-banners`：向导图的**形态**校验 —— 每份文件存在、能解码、尺寸等于
  `BANNER_DPI` / `SMALL_DPI` 里登记的值；不读字体、不比对内容，所以 Linux 上也能跑
  （CI 的 lint 用这条）。它兜的是"文件被删 / 改名 / 换错档位 / 截断"这类打包事故；
- `--check`：图标与向导图都逐字节比对。向导图那一半要读系统字体，只在本机
  （Windows）提交前跑。

用法
----
    venv/Scripts/python tools/build_brand.py                   # 生成全部产物（要 Windows 字体）
    venv/Scripts/python tools/build_brand.py --check           # 逐字节校验全部（本地提交前，Windows）
    venv/Scripts/python tools/build_brand.py --check-icons     # 逐字节校验图标（不读字体，任何平台）
    venv/Scripts/python tools/build_brand.py --check-banners   # 向导图形态：存在/可解码/尺寸（不读字体）
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = PROJECT_ROOT / 'res' / 'brand'
PNG_DIR = BRAND_DIR / 'png'
RELEASE_DIR = PROJECT_ROOT / 'docs' / 'Releases'

FONT_CN = 'C:/Windows/Fonts/msyh.ttc'
FONT_CN_BOLD = 'C:/Windows/Fonts/msyhbd.ttc'
FONT_LATIN = 'C:/Windows/Fonts/segoeui.ttf'

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
BANNER_DPI = {'100': (202, 386), '125': (269, 515), '150': (336, 643)}
SMALL_DPI = {'100': (58, 58), '125': (77, 77), '150': (97, 97)}

# 向导图底色：与 .iss 的 WizardImageBackColor 必须一致，否则留边时露色差。
WIZARD_BACK = (27, 30, 39)
CARD = (30, 33, 42)
CARD_EDGE = (46, 50, 62)
INK = (233, 236, 245)


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _theme(base, accent, light=False, mark=None):
    """一套配色 → 渲染参数。"""
    if light:
        return dict(tile_hi=(243, 246, 252), tile_lo=(224, 228, 238),
                    cell_hi=_mix(accent, (255, 255, 255), 0.16),
                    cell_lo=_mix(accent, (0, 0, 0), 0.14),
                    mark=mark or _mix(accent, (255, 255, 255), 0.72),
                    empty=(212, 217, 229), ring=(152, 166, 197), rim=(0, 0, 0, 22))
    return dict(tile_hi=_mix(base, (255, 255, 255), 0.10), tile_lo=_mix(base, (0, 0, 0), 0.42),
                cell_hi=_mix(accent, (255, 255, 255), 0.06), cell_lo=_mix(accent, (0, 0, 0), 0.20),
                mark=mark or _mix(accent, (0, 0, 0), 0.80),
                empty=_mix(base, (0, 0, 0), 0.52), ring=_mix(accent, base, 0.30),
                rim=(255, 255, 255, 46))


THEMES = {
    'blue': _theme((35, 40, 56), (124, 156, 245)),
    'indigo': _theme((36, 38, 56), (139, 124, 240)),
    'cyan': _theme((29, 42, 52), (78, 195, 217)),
    'amber': _theme((43, 38, 32), (224, 164, 85)),
    'light': _theme((238, 241, 247), (91, 120, 216), light=True, mark=(201, 214, 255)),
    'gold': _theme((21, 21, 26), (233, 196, 106)),
}
DEFAULT_THEME = 'blue'

# 三个记号：文档封面（两道横杆）、相册卡（图块 + 标题线）、播放器（三角）。
# 墨量已配平到 9%～11%，形心都在格子中心 ±0.05 内；改动时请重新配平，否则整块会歪。
DOC = (('r', 0.22, 0.33, 0.80, 0.42), ('r', 0.22, 0.56, 0.66, 0.65))
PHOTO = (('r', 0.29, 0.31, 0.71, 0.54), ('r', 0.29, 0.62, 0.59, 0.665))
MEDIA = (('t', 0.32, 0.26, 0.32, 0.74, 0.74, 0.50),)
CELL_MARKS = {(0, 0): DOC, (1, 0): PHOTO, (0, 1): MEDIA}


def _vgrad(w, h, top, bot, y0, y1):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(max(0, y0), min(h, y1)):
        t = (y - y0) / max(1, y1 - y0 - 1)
        d.line([(0, y), (w, y)], fill=(*_mix(top, bot, t), 255))
    return img


def _draw_marks(d, items, x, y, cw, color):
    for item in items:
        if item[0] == 'r':
            _, x0, y0, x1, y1 = item
            d.rounded_rectangle([x + x0 * cw, y + y0 * cw, x + x1 * cw, y + y1 * cw],
                                radius=int(cw * 0.035), fill=(*color, 240))
        else:
            _, x0, y0, x1, y1, x2, y2 = item
            d.polygon([(x + x0 * cw, y + y0 * cw), (x + x1 * cw, y + y1 * cw),
                       (x + x2 * cw, y + y2 * cw)], fill=(*color, 240))


def render_icon(size: int, theme: str) -> Image.Image:
    p = THEMES[theme]
    ss = 8 if size <= 64 else 4
    S = size * ss
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    rad = int(S * 0.235)
    base = _vgrad(S, S, p['tile_hi'], p['tile_lo'], 0, S)
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=rad, fill=255)
    base.putalpha(mask)
    img.alpha_composite(base)

    # 框架顶光：贴内侧一圈亮边，向下淡出。这是全图唯一的光源。
    rim = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    rw = max(1, int(S * 0.006))
    ImageDraw.Draw(rim).rounded_rectangle([rw, rw, S - 1 - rw, S - 1 - rw], radius=rad - rw,
                                          outline=p['rim'], width=rw)
    fade = Image.new('L', (S, S), 0)
    fd = ImageDraw.Draw(fade)
    for y in range(S):
        fd.line([(0, y), (S, y)], fill=int(255 * max(0.0, 1 - y / (S * 0.55))))
    rim.putalpha(Image.composite(rim.getchannel('A'), Image.new('L', (S, S), 0), fade))
    img.alpha_composite(rim)

    d = ImageDraw.Draw(img)
    pad = S * 0.15
    gap = S * 0.10
    cw = (S - 2 * pad - gap) / 2
    crad = int(cw * 0.20)
    for j in range(2):
        for i in range(2):
            x = pad + i * (cw + gap)
            y = pad + j * (cw + gap)
            box = [x, y, x + cw, y + cw]
            if (i, j) == (1, 1):
                d.rounded_rectangle(box, radius=crad, fill=(*p['empty'], 255))
                d.rounded_rectangle(box, radius=crad,
                                    outline=(*_mix(p['ring'], p['empty'], 0.45), 200),
                                    width=max(2, int(cw * 0.07)))
                continue
            lay = _vgrad(S, S, p['cell_hi'], p['cell_lo'], int(y), int(y + cw))
            m = Image.new('L', (S, S), 0)
            ImageDraw.Draw(m).rounded_rectangle(box, radius=crad, fill=255)
            lay.putalpha(m)
            img.alpha_composite(lay)
            d = ImageDraw.Draw(img)
            # 16/24 不放记号：每格只有 6～9px，放进去只会糊成噪点。
            if size >= 32 and (i, j) in CELL_MARKS:
                _draw_marks(d, CELL_MARKS[(i, j)], x, y, cw, p['mark'])
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _ico_bytes(theme: str) -> bytes:
    blobs = []
    for size in ICO_SIZES:
        buf = io.BytesIO()
        render_icon(size, theme).save(buf, format='PNG')
        blobs.append((size, buf.getvalue()))
    out = struct.pack('<HHH', 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    dirs = b''
    for size, data in blobs:
        v = size if size < 256 else 0
        dirs += struct.pack('<BBBBHHII', v, v, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + dirs + b''.join(data for _, data in blobs)


def _wizard_mark(d, x, y, s, color):
    pad = s * 0.15
    gap = s * 0.10
    cw = (s - 2 * pad - gap) / 2
    rad = int(cw * 0.30)
    for j in range(2):
        for i in range(2):
            bx = x + pad + i * (cw + gap)
            by = y + pad + j * (cw + gap)
            if (i, j) == (1, 1):
                d.rounded_rectangle([bx, by, bx + cw, by + cw], radius=rad,
                                    outline=_mix(color, (0, 0, 0), 0.45),
                                    width=max(1, int(cw * 0.12)))
            else:
                d.rounded_rectangle([bx, by, bx + cw, by + cw], radius=rad, fill=color)


def _card(d, x, y, w, h, kind, color):
    d.rounded_rectangle([x, y, x + w, y + h], radius=int(h * 0.22), fill=CARD,
                        outline=CARD_EDGE, width=1)
    ix = x + h * 0.28
    iy = y + h * 0.26
    size = h * 0.46
    if kind == 'doc':
        d.rounded_rectangle([ix, iy, ix + size * 0.15, iy + size], radius=int(size * 0.06), fill=color)
        for k in range(2):
            d.rounded_rectangle([ix + size * 0.34, iy + size * (0.08 + k * 0.44),
                                 ix + size * (1.0 - k * 0.26), iy + size * (0.34 + k * 0.44)],
                                radius=int(size * 0.06), fill=color)
    elif kind == 'photo':
        d.rounded_rectangle([ix, iy + size * 0.05, ix + size, iy + size * 0.70],
                            radius=int(size * 0.09), fill=color)
        d.rounded_rectangle([ix, iy + size * 0.82, ix + size * 0.60, iy + size * 0.94],
                            radius=int(size * 0.05), fill=_mix(color, CARD, 0.35))
    else:
        d.polygon([(ix + size * 0.10, iy + size * 0.08), (ix + size * 0.10, iy + size * 0.92),
                   (ix + size * 0.88, iy + size * 0.50)], fill=color)
    tx = ix + size + h * 0.24
    d.rounded_rectangle([tx, y + h * 0.33, tx + w * 0.28, y + h * 0.45], radius=3,
                        fill=_mix(color, CARD, 0.55))
    d.rounded_rectangle([tx, y + h * 0.56, tx + w * 0.17, y + h * 0.65], radius=3,
                        fill=_mix(color, CARD, 0.72))


def render_banner(w: int, h: int, theme: str = DEFAULT_THEME) -> Image.Image:
    """向导左侧大图：标 + 三张插件卡 + 产品名。第三行字刻意不要。"""
    ss = 3
    W, H = w * ss, h * ss
    color = THEMES[theme]['cell_hi']
    img = Image.new('RGB', (W, H), WIZARD_BACK)
    d = ImageDraw.Draw(img)

    mark = int(W * 0.24)
    _wizard_mark(d, (W - mark) // 2, int(H * 0.055), mark, color)

    cw, ch = int(W * 0.70), int(W * 0.185)
    y = int(H * 0.195)
    for kind in ('doc', 'photo', 'media'):
        _card(d, (W - cw) // 2, y, cw, ch, kind, color)
        y += ch + int(W * 0.035)

    d.text((W // 2, int(H * 0.680)), '格子铺', font=ImageFont.truetype(FONT_CN_BOLD, int(W * 0.115)),
           fill=INK, anchor='mm')
    d.text((W // 2, int(H * 0.745)), 'Cubby', font=ImageFont.truetype(FONT_LATIN, int(W * 0.055)),
           fill=(138, 144, 164), anchor='mm')
    d.rounded_rectangle([W // 2 - W * 0.10, int(H * 0.790), W // 2 + W * 0.10, int(H * 0.795)],
                        radius=int(H * 0.003), fill=color)
    return img.resize((w, h), Image.Resampling.LANCZOS)


def build_icon_outputs() -> dict[Path, bytes]:
    """图标产物：6 套配色 ×（1 份多尺寸 ICO + 8 份 PNG）。

    只用几何绘制，不读字体，任何平台逐字节可复现 —— 所以可以挂在 Linux 的 lint 上。
    """
    outputs: dict[Path, bytes] = {}
    for theme in THEMES:
        outputs[BRAND_DIR / f'cubby-{theme}.ico'] = _ico_bytes(theme)
        for size in PNG_SIZES:
            buf = io.BytesIO()
            render_icon(size, theme).save(buf, format='PNG')
            outputs[PNG_DIR / f'cubby-{theme}-{size}.png'] = buf.getvalue()
    return outputs


def build_banner_outputs() -> dict[Path, bytes]:
    """安装器向导图：3 档 DPI 的大图与小图。

    大图里的"格子铺 / Cubby"由 `render_banner()` 用系统字体渲染，离开 Windows 字体
    就没有可用的字形（`ImageFont.truetype` 抛 OSError）：这一支只在 Windows 上跑。
    """
    outputs: dict[Path, bytes] = {}
    for tag, (w, h) in BANNER_DPI.items():
        buf = io.BytesIO()
        render_banner(w, h).save(buf, format='PNG')
        outputs[RELEASE_DIR / f'installer-banner-{tag}.png'] = buf.getvalue()
    for tag, (w, h) in SMALL_DPI.items():
        buf = io.BytesIO()
        render_icon(w, DEFAULT_THEME).resize((w, h), Image.Resampling.LANCZOS).save(buf, format='PNG')
        outputs[RELEASE_DIR / f'installer-small-{tag}.png'] = buf.getvalue()
    return outputs


def build_outputs() -> dict[Path, bytes]:
    """全部产物（生成模式用；`--check` 也等价于分别校验上面两组）。"""
    outputs = build_icon_outputs()
    outputs.update(build_banner_outputs())
    return outputs


def _label(path: Path) -> str:
    """消息里的路径用仓库相对形式；不在仓库里（临时目录 / 用例）时退回绝对路径。"""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _check_outputs(build: Callable[[], dict[Path, bytes]], what: str) -> int:
    """逐字节比对一组产物与生成脚本；0 = 一致，1 = 有差异（不写文件）。"""
    outputs = build()
    stale = [path for path, data in outputs.items()
             if not path.is_file() or path.read_bytes() != data]
    if stale:
        for path in stale:
            print(f'[error] {_label(path)} 与生成脚本不一致', file=sys.stderr)
        print('        运行 venv/Scripts/python tools/build_brand.py 重新生成', file=sys.stderr)
        return 1
    print(f'brand: OK（{what}：{len(outputs)} 份产物均与脚本一致）')
    return 0


def _png_shape_error(path: Path, expected: tuple[int, int]) -> str | None:
    """一份向导图的形态问题：不存在 / 解不开 / 尺寸不对时返回一句可读的错误。"""
    rel = _label(path)
    if not path.is_file():
        return f'缺少向导图：{rel}'
    try:
        with Image.open(path) as image:
            size = image.size
            image.verify()
    except Exception as exc:  # Pillow 对坏文件抛 OSError / ValueError / UnidentifiedImageError
        return f'向导图无法解码：{rel}（{type(exc).__name__}: {exc}）'
    if size != expected:
        return (f'向导图尺寸不对：{rel} 是 {size[0]}×{size[1]}，'
                f'应为 {expected[0]}×{expected[1]}')
    return None


def _check_banner_shape() -> int:
    """向导图形态校验：存在、可解码、尺寸正确。不读字体、不比对内容，任何平台都能跑。"""
    problems: list[str] = []
    count = 0
    for tag, (w, h) in BANNER_DPI.items():
        count += 1
        problem = _png_shape_error(RELEASE_DIR / f'installer-banner-{tag}.png', (w, h))
        if problem:
            problems.append(problem)
    for tag, (w, h) in SMALL_DPI.items():
        count += 1
        problem = _png_shape_error(RELEASE_DIR / f'installer-small-{tag}.png', (w, h))
        if problem:
            problems.append(problem)
    if problems:
        for problem in problems:
            print(f'[error] {problem}', file=sys.stderr)
        print('        重新生成：venv/Scripts/python tools/build_brand.py', file=sys.stderr)
        return 1
    print(f'brand: OK（安装器向导图：{count} 份文件存在、可解码、尺寸正确）')
    return 0


def _check_banner_bytes() -> int:
    """逐字节比对向导图：读系统字体，只在生成它们的机器（Windows）上有意义。"""
    try:
        return _check_outputs(build_banner_outputs, '安装器向导图')
    except OSError as exc:
        print('[error] 打不开渲染向导图用的系统字体：'
              f'{FONT_CN} / {FONT_CN_BOLD} / {FONT_LATIN}（{exc}）\n'
              '        --check 的向导图那一半只在本机（Windows）提交前跑；'
              'CI 用 --check-banners 做形态校验，不需要字体。',
              file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='生成应用图标与安装器向导图；--check* 只校验、不写文件')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='逐字节校验全部产物（图标 + 向导图）；向导图读系统字体，仅本机 Windows')
    mode.add_argument('--check-icons', action='store_true',
                      help='逐字节校验图标（不读字体，任何平台都能跑；CI 的 lint 用这条）')
    mode.add_argument('--check-banners', action='store_true',
                      help='校验安装器向导图的形态：存在、可解码、尺寸正确（不读字体；CI 的 lint 用这条）')
    args = parser.parse_args(argv)

    if args.check_icons:
        return _check_outputs(build_icon_outputs, '图标')
    if args.check_banners:
        return _check_banner_shape()
    if args.check:
        code = _check_outputs(build_icon_outputs, '图标')
        if code:
            return code
        return _check_banner_bytes()

    outputs = build_outputs()
    for path, data in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f'brand: 已生成 {len(outputs)} 份产物（{len(THEMES)} 套配色 + 向导图 {len(BANNER_DPI)} 档）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
