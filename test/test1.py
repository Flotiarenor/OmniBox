import subprocess
import json

def fetch_jm_album_realtime(album_id: str) -> dict:
    python_exec = r"D:\project\Python\JMComic-Api-master\.venv\Scripts\python.exe"
    
    process = subprocess.Popen(
        [python_exec, "-m", "jmcomic_api", "download", album_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        cwd=r"D:\project\Python\JMComic-Api-master"
    )
    
    while True:
        line = process.stderr.readline()
        if not line:
            break
        
        try:
            event = json.loads(line.strip())
            event_type = event.get("type")
            
            if event_type == "metadata":
                print(f"\n📖 获取到元数据: {event['title']} (共 {event['chapter_count']} 章, 预估 {event['total_page_count']} 页)")
                
            elif event_type == "chapter_progress":
                print(f"\n📂 章节进度: [{event['current_chapter']}/{event['total_chapters']}] {event['chapter_title']}")
                
            elif event_type == "image_progress":
                status_icon = "✅" if event['status'] == 'exists' else "⬇️"
                print(f"  {status_icon} 全局图片进度: [{event['current_image']}/{event['total_images']}] {event['filename']}", end='\r')
                
        except json.JSONDecodeError:
            pass
            
    print("\n下载进程结束，解析最终数据...")
    
    stdout_data = process.stdout.read()
    process.wait()
    
    try:
        if stdout_data.strip():
            return json.loads(stdout_data)
        else:
            return {"success": False, "message": "CLI 无输出"}
    except json.JSONDecodeError:
        return {"success": False, "message": "JSON解析失败", "raw_stdout": stdout_data}

data = fetch_jm_album_realtime("350234")
print("最终获取到的数据:", json.dumps(data, indent=2, ensure_ascii=False))