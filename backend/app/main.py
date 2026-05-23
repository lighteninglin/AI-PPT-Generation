"""
PPT-Web FastAPI 应用 — 极简，只做配置/生成/下载
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ConfigManager, LLMConfig
from .ppt_engine import PPTEngine, PROJECTS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PPT-Web", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── 前端静态文件 ──
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# ── 任务管理(单用户足够) ──
tasks: dict[str, dict] = {}
ws_connections: dict[str, list[WebSocket]] = {}


# ── 数据模型 ──

class ConfigPayload(BaseModel):
    enable_thinking: bool = False

class GeneratePayload(BaseModel):
    source_text: str
    topic: str
    source_type: str = "text"   # text / markdown
    format: str = "ppt169"      # ppt169 / ppt43
    enable_thinking: bool = False  # 每次请求独立传入


# ── API 路由 ──

@app.get("/api/config")
def get_config():
    return ConfigManager.get_masked()


@app.post("/api/config")
def save_config(payload: ConfigPayload):
    cfg = ConfigManager.load()
    cfg.enable_thinking = payload.enable_thinking
    ConfigManager.save(cfg)
    return {"ok": True, "message": "配置已保存"}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文档，提取文本内容返回"""
    from .file_parser import extract_text, SUPPORTED_EXTENSIONS

    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}，支持: {', '.join(SUPPORTED_EXTENSIONS)}")

    # 限制文件大小 50MB
    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过 50MB")

    # 写临时文件
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        tmp.write(contents)
        tmp.close()
        text = extract_text(Path(tmp.name), filename)
    except (ImportError, ValueError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("文件解析失败: %s", e)
        raise HTTPException(500, f"文件解析失败: {e}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if not text.strip():
        raise HTTPException(400, "未能从文件中提取到文本内容")

    return {
        "filename": filename,
        "text": text,
        "char_count": len(text),
    }


@app.post("/api/generate")
async def start_generate(payload: GeneratePayload):
    cfg = ConfigManager.load()
    if not cfg.is_configured():
        raise HTTPException(400, "请先配置 LLM API Key")

    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        "id": task_id,
        "status": "running",
        "progress": 0,
        "stage": "init",
        "message": "任务已创建",
        "result_path": None,
        "topic": payload.topic,
    }

    # 后台执行
    asyncio.create_task(_run_task(task_id, cfg, payload))
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/download/{task_id}")
def download_pptx(task_id: str):
    task = tasks.get(task_id)
    if not task or task["status"] != "done":
        raise HTTPException(404, "PPTX 尚未生成完成")
    path = Path(task["result_path"])
    if not path.exists():
        raise HTTPException(404, "文件已丢失")
    return FileResponse(str(path), filename=path.name, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@app.get("/api/preview/{task_id}")
def preview_svg(task_id: str):
    """返回SVG预览页面(HTML)"""
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    project_dir = Path(task["result_path"]).parent.parent if task.get("result_path") else None
    if not project_dir or not project_dir.exists():
        raise HTTPException(404, "项目目录不存在")

    svg_dir = project_dir / "svg_output"
    if not svg_dir.exists():
        raise HTTPException(404, "SVG 尚未生成")

    svgs = sorted(svg_dir.glob("*.svg"))
    html = '<html><head><meta charset="utf-8"><title>PPT预览</title><style>'
    html += 'body{background:#1a1a2e;display:flex;flex-wrap:wrap;gap:12px;padding:20px;justify-content:center;}'
    html += 'img{max-width:400px;height:auto;aspect-ratio:16/9;object-fit:contain;border:2px solid #333;border-radius:8px;background:#fff;}</style></head><body>'
    for svg in svgs:
        html += f'<img src="/api/svg/{task_id}/{svg.name}" />'
    html += '</body></html>'
    return HTMLResponse(html)


@app.get("/api/svg/{task_id}/{svg_name}")
def serve_svg(task_id: str, svg_name: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404)
    project_dir = Path(task["result_path"]).parent.parent if task.get("result_path") else None
    if not project_dir:
        raise HTTPException(404)
    svg_path = project_dir / "svg_output" / svg_name
    if not svg_path.exists():
        raise HTTPException(404)
    return FileResponse(str(svg_path), media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(ws: WebSocket, task_id: str):
    await ws.accept()
    if task_id not in ws_connections:
        ws_connections[task_id] = []
    ws_connections[task_id].append(ws)
    try:
        while True:
            await ws.receive_text()  # 保持连接
    except WebSocketDisconnect:
        ws_connections[task_id].remove(ws)


# ── 前端 catch-all ──

@app.get("/")
async def serve_index():
    idx = FRONTEND_DIST / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse(_dev_placeholder())


# ── 内部函数 ──

async def _run_task(task_id: str, cfg: LLMConfig, payload: GeneratePayload):
    """后台运行生成任务"""
    task = tasks[task_id]
    try:
        engine = PPTEngine(cfg)

        def on_progress(stage, msg, pct):
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = pct
            # 推送 WebSocket
            for ws in ws_connections.get(task_id, []):
                try:
                    asyncio.get_event_loop().create_task(
                        ws.send_json({"stage": stage, "message": msg, "progress": pct})
                    )
                except Exception:
                    pass

        # 在线程池中运行同步代码
        loop = asyncio.get_event_loop()
        result_path = await loop.run_in_executor(
            None,
            lambda: engine.generate(
                source_text=payload.source_text,
                topic=payload.topic,
                fmt=payload.format,
                enable_thinking=payload.enable_thinking,
                progress=on_progress,
            )
        )
        task["status"] = "done"
        task["progress"] = 100
        task["message"] = "PPT生成完成!"
        task["result_path"] = str(result_path)

    except Exception as e:
        logger.exception("任务 %s 失败", task_id)
        task["status"] = "error"
        task["message"] = f"生成失败: {e}"
        task["progress"] = 0


def _dev_placeholder() -> str:
    """开发模式占位页面"""
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PPT-Web</title></head>
<body style="font-family:sans-serif;text-align:center;padding:80px;background:#0f172a;color:#e2e8f0">
<h1>🎨 PPT-Web</h1><p>前端未构建，请先 <code>cd frontend && npm run build</code></p>
<p>或直接访问 <a href="/docs" style="color:#60a5fa">/docs</a> 查看API文档</p>
</body></html>"""
