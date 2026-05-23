# PPT-Web

基于 [ppt-master](https://github.com/hugohe3/ppt-master) 的网页版 AI PPT 生成工具。

用户只需配置一个 OpenAI 兼容的 LLM API Key，即可一键生成可编辑的 PPTX 文件。

## 快速开始

```bash
cd ~/ppt-web
python start.py
# 浏览器打开 http://localhost:8000
```

## 手动启动

```bash
# 安装依赖
pip install -r backend/requirements.txt
pip install -r skills/ppt-master/requirements.txt

# 启动
python backend/run.py
```

## 使用说明

1. **配置 API** — 填入 OpenAI 兼容的 API Key、Base URL、模型名称
2. **输入主题** — PPT 的标题/主题
3. **粘贴内容** — Markdown 或纯文本均可
4. **点击生成** — AI 自动规划→生成→导出 PPTX

## 架构

```
前端(单HTML) → FastAPI → LLM API(OpenAI兼容) + ppt-master脚本
```

- `backend/app/main.py` — FastAPI 路由
- `backend/app/ppt_engine.py` — 核心引擎，编排 ppt-master 流水线
- `backend/app/llm_client.py` — OpenAI 兼容 LLM 客户端
- `backend/app/config.py` — 配置管理
- `skills/ppt-master/` — 原项目脚本(直接复用，不改动)

## API 文档

启动后访问 `http://localhost:8000/docs` 查看 Swagger 文档。

| 接口 | 说明 |
|------|------|
| `GET /api/config` | 获取当前配置(脱敏) |
| `POST /api/config` | 保存配置 |
| `POST /api/generate` | 开始生成任务 |
| `GET /api/tasks/{id}` | 查询任务进度 |
| `GET /api/download/{id}` | 下载 PPTX |
| `GET /api/preview/{id}` | SVG 预览 |
| `WS /ws/tasks/{id}` | WebSocket 实时进度 |
