# 第三方作品声明

本仓库以 Apache-2.0 发布（见根目录 `LICENSE`）。下面列出**被包含进本仓库源码**的第三方
作品及其许可。只作为运行时依赖安装、没有代码被复制的包不在本文件中声明。

---

## jmcomic 2.6.19

- 出处：https://github.com/hect0x7/JMComic-Crawler-Python
- 许可：MIT License
- 版权：Copyright (c) 2023 hect0x7
- 使用方式：**衍生**。`plugins/manga-library/backend/libs/jm_mini.py`（移动端接口的
  域名引导、请求签名、响应解密、实体解析、图片 URL 拼接，以及乱序图片的分割数计算与
  条带重排）是在阅读 jmcomic 实现并对其抓包对照后精简移植的，只保留下载路径；
  `plugins/manga-library/backend/downloader.py` 的进度与取消语义沿用同一套钩子顺序。
  移植结果与上游逐像素对照的用例见 `tests/test_manga_library_jm_decode.py` 与
  `tests/test_manga_library_jm_protocol.py`。
- 义务：MIT 要求在所有副本或实质性部分中保留上面的版权声明与下面的许可全文。

```
MIT License

Copyright (c) 2023 hect0x7

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## pixivpy 3.13.0

- 出处：https://github.com/upbit/pixivpy
- 许可：The Unlicense（公有领域奉献）
- 使用方式：**衍生**。`plugins/pixiv-sync/backend/libs/pixiv_mini.py` 从 pixivpy 3.13.0
  大幅精简而来，只保留 refresh_token 认证、关注新作流、关注列表、用户作品、收藏与图片
  下载，返回值改为原始 JSON dict。
- 义务：The Unlicense 把著作权奉献给公有领域，**不要求**署名或保留声明；这里记录出处
  与许可，是为了让"这段代码从哪来、受什么约束"可被复核。
- 上游许可全文（`upbit/pixivpy` 的 `LICENSE`）：

```
This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or
distribute this software, either in source code form or as a compiled
binary, for any purpose, commercial or non-commercial, and by any
means.

In jurisdictions that recognize copyright laws, the author or authors
of this software dedicate any and all copyright interest in the
software to the public domain. We make this dedication for the benefit
of the public at large and to the detriment of our heirs and
successors. We intend this dedication to be an overt act of
relinquishment in perpetuity of all present and future rights to this
software under copyright law.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <http://unlicense.org>
```
