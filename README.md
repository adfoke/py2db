# py2db

消息采集、搜索、导出工具。

## 运行

```bash
uv sync
uv run python main.py
```

打开 `http://127.0.0.1:8000`

## 功能

- 导入网页消息
- 按关键词和时间搜索
- 编辑和删除消息
- 按时间范围导出 Markdown
- 导出时复制图片和视频文件

## 目录

- `main.py`: 入口
- `twitter.db`: 数据库
- `static/media/`: 媒体文件
- `templates/`: 页面模板
- `exports/`: 导出结果
