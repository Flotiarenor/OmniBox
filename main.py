# main.py
import os
import threading
from backend.api import AppAPI
from backend.file_server import create_file_server


def main():
    # 配置路径
    IMAGE_DIR = r"G:\图库"
    MANGA_DIR = r"G:\图库\本子"
    NOVEL_DIR = r"G:\小说"
    
    # 确保目录存在
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(MANGA_DIR, exist_ok=True)
    os.makedirs(NOVEL_DIR, exist_ok=True)
    
    # 初始化 API（注意参数顺序）
    api = AppAPI(
        image_dir=IMAGE_DIR,
        manga_dir=MANGA_DIR,
        novel_dir=NOVEL_DIR
    )
    
    # 启动文件服务器
    thumb_dir = os.path.join(IMAGE_DIR, '.thumbs')
    cover_dir = os.path.join(MANGA_DIR, '.covers')
    os.makedirs(thumb_dir, exist_ok=True)
    os.makedirs(cover_dir, exist_ok=True)
    
    app = create_file_server(IMAGE_DIR, thumb_dir, MANGA_DIR, cover_dir)
    
    # 启动 Flask 服务器线程
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host='127.0.0.1',
            port=18080,
            debug=False,
            use_reloader=False
        ),
        daemon=True
    )
    flask_thread.start()
    
    # 启动 PyWebView
    import webview
    webview.create_window(
        'OmniBox',
        'frontend/index.html',
        width=1400,
        height=900,
        js_api=api,
        text_select=True
    )
    webview.start()


if __name__ == '__main__':
    main()