import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import importlib.util

def load_module(name, filepath):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

backend_dir = Path(__file__).parent
models = load_module('models', str(backend_dir / 'models.py'))
metadata = load_module('metadata', str(backend_dir / 'metadata.py'))
scanner_mod = load_module('scanner', str(backend_dir / 'scanner.py'))

MetadataReader = metadata.MetadataReader

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='音乐文件元数据调试工具')
    parser.add_argument('path', help='音频文件路径')
    args = parser.parse_args()

    file_path = Path(args.path)
    if not file_path.exists():
        print(f'ERROR: 文件不存在: {file_path}')
        sys.exit(1)

    print(f'=== 文件信息 ===')
    print(f'  路径: {file_path}')
    print(f'  大小: {file_path.stat().st_size} bytes')
    print(f'  后缀: {file_path.suffix.lower()}')
    print(f'  mutagen 可用: {metadata.HAS_MUTAGEN}')
    print()

    print(f'=== 解析结果 ===')
    result = MetadataReader.read(file_path)
    for k, v in result.items():
        print(f'  {k}: {v}')
    print()

    print(f'=== 元数据调试 (debug_meta) ===')
    debug = MetadataReader.debug_meta(file_path)
    print(json.dumps(debug, indent=2, ensure_ascii=False, default=str))
