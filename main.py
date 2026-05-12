# main.py
import threading
import time
import webview

from backend.api import AppAPI
from backend.file_server import create_file_server

# ===== 配置 =====
IMAGE_DIR = r"G:\图库"
MANGA_DIR = r"G:\图库\本子"
FILE_SERVER_PORT = 18080
# ================

def main():
    api = AppAPI(IMAGE_DIR, MANGA_DIR)

    # 启动文件服务器（用于提供图片和缩略图）
    file_app = create_file_server(
        image_dir=api.image_module.image_dir,
        thumb_dir=api.image_module.thumb_dir,
        manga_dir=api.manga_module.manga_dir,
        cover_dir=api.manga_module.cover_dir
    )
    threading.Thread(
        target=lambda: file_app.run(
            host='127.0.0.1', port=FILE_SERVER_PORT,
            debug=False, use_reloader=False
        ),
        daemon=True
    ).start()
    time.sleep(0.5)

    # 创建 WebView 窗口
    webview.create_window(
        title='个人数字中心',
        url='frontend/index.html',
        width=1200, height=800,
        min_size=(800, 600),
        js_api=api
    )
    webview.start(debug=True)

if __name__ == '__main__':
    main()