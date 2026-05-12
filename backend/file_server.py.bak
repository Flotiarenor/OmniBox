import hashlib
from pathlib import Path
from flask import Flask, send_file, abort
from PIL import Image


def create_file_server(image_dir: Path, thumb_dir: Path):
    app = Flask(__name__)
    image_dir = Path(image_dir)
    thumb_dir = Path(thumb_dir)

    @app.route('/images/<path:filename>')
    def serve_original(filename):
        file_path = image_dir / filename
        if file_path.exists() and file_path.is_file():
            return send_file(file_path, conditional=True)
        abort(404)

    @app.route('/thumbs/<path:filename>')
    def serve_thumbnail(filename):
        abs_path = image_dir / filename
        if not abs_path.exists():
            abort(404)

        file_hash = hashlib.md5(filename.encode('utf-8')).hexdigest()
        thumb_path = thumb_dir / f"{file_hash}.jpg"

        # 缓存命中且比源文件新
        if thumb_path.exists() and thumb_path.stat().st_mtime >= abs_path.stat().st_mtime:
            return send_file(thumb_path, mimetype='image/jpeg')

        # 生成缩略图
        try:
            img = Image.open(abs_path)
            img.thumbnail((300, 300))
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.save(thumb_path, 'JPEG', quality=85, optimize=True)
            return send_file(thumb_path, mimetype='image/jpeg')
        except Exception:
            abort(500)

    return app