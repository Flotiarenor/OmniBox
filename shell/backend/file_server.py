from flask import Flask, send_from_directory, abort
from pathlib import Path

def create_app(config, plugin_manager):
    app = Flask(__name__)
    frontend_dist = Path(__file__).parent.parent / 'frontend' / 'dist'

    @app.route('/health')
    def health(): return 'OK'

    @app.route('/')
    @app.route('/<path:filename>')
    def serve_shell(filename='index.html'):
        if not (frontend_dist / filename).exists() and not filename.startswith('assets'):
            return send_from_directory(frontend_dist, 'index.html')
        return send_from_directory(frontend_dist, filename)

    @app.route('/plugins/<plugin_name>/frontend/<path:filename>')
    def serve_plugin_frontend(plugin_name, filename):
        plugin_dir = plugin_manager.plugins_dir / plugin_name / 'frontend'
        if not plugin_dir.exists(): abort(404)
        return send_from_directory(plugin_dir, filename)

    @app.route('/files/<path:filepath>')
    def serve_user_file(filepath):
        data_root = Path(config['directories']['data_root'])
        file_path = data_root / filepath
        if file_path.exists() and file_path.is_file():
            return send_from_directory(data_root, filepath)
        abort(404)

    return app