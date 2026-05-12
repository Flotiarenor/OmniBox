# backend/modules/jm_tool.py
import os
import json
import logging
from typing import Dict, Any, Optional
import jmcomic

logger = logging.getLogger(__name__)

class JMTool:
    """极简 JMComic 工具集，直接调用客户端API，无插件依赖"""
    
    def __init__(self, download_dir: str):
        self.download_dir = os.path.abspath(download_dir)
        os.makedirs(self.download_dir, exist_ok=True)
        self._client = None

    @property
    def client(self):
        """懒加载单例客户端"""
        if self._client is None:
            # 使用最基础的配置构建客户端
            option = jmcomic.JmOption.construct({
                "dir_rule": {"base_dir": self.download_dir, "rule": "Bd"},
                "download": {"image": {"decode": True, "suffix": ".jpg"}}
            })
            self._client = option.build_jm_client()
        return self._client

    def fetch_metadata(self, album_id: str) -> Dict[str, Any]:
        """
        人工拼装元数据：通过直接请求获取详情和真实页数
        注意：统计真实页数需要遍历章节发请求，耗时较长
        """
        logger.info(f"开始获取元数据: {album_id}")
        album = self.client.get_album_detail(album_id)
        
        chapters = []
        total_pages = 0
        
        # 遍历章节获取真实图片数
        for chapter in album.chapter_list:
            photo = self.client.get_photo_detail(chapter.photo_id)
            page_count = len(photo)
            total_pages += page_count
            chapters.append({
                "chapter_id": getattr(chapter, 'photo_id', ''),
                "title": getattr(chapter, 'name', ''),
                "page_count": page_count
            })

        metadata = {
            "comic_id": str(album.album_id),
            "title": getattr(album, 'name', '未知'),
            "author": getattr(album, 'author', '未知'),
            "tags": list(getattr(album, 'tags', [])),
            "total_page_count": total_pages,
            "chapters": chapters
        }
        logger.info(f"元数据获取完成: {album.name}, 总页数: {total_pages}")
        return metadata

    def download_images(self, album_id: str) -> str:
        """
        仅下载原图到指定目录，不关心元数据
        返回下载目录的绝对路径
        """
        save_dir = os.path.join(self.download_dir, album_id)
        os.makedirs(save_dir, exist_ok=True)
        
        option_dict = {
            "dir_rule": {"base_dir": save_dir, "rule": "Bd"},
            "download": {
                "cache": True, 
                "image": {"decode": True, "suffix": ".jpg"},
                "threading": {"image": 10}  # 开启多线程加速下载
            }
        }
        option = jmcomic.JmOption.construct(option_dict)
        
        logger.info(f"开始下载图片: {album_id} -> {save_dir}")
        jmcomic.download_album(album_id, option)
        logger.info(f"图片下载完成: {album_id}")
        return save_dir

    def save_metadata(self, metadata: Dict[str, Any], save_dir: str) -> str:
        """将元数据字典持久化为 album_info.json"""
        from datetime import datetime
        metadata["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        os.makedirs(save_dir, exist_ok=True)
        json_path = os.path.join(save_dir, "album_info.json")
        
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2, default=str)
            
        logger.info(f"元数据已保存: {json_path}")
        return json_path

    def full_process(self, album_id: str) -> Dict[str, Any]:
        """一站式流程：获取信息 -> 下载图片 -> 保存JSON"""
        try:
            # 1. 抓信息
            metadata = self.fetch_metadata(album_id)
            # 2. 下图片
            save_dir = self.download_images(album_id)
            # 3. 存JSON
            self.save_metadata(metadata, save_dir)
            return {"success": True, "data": metadata}
        except Exception as e:
            logger.error(f"处理 {album_id} 失败: {e}", exc_info=True)
            return {"success": False, "message": str(e)}