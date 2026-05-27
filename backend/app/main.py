"""
PPT-Web FastAPI 应用 — 完整7步流水线
Step 4 (Eight Confirmations) 需要 ⛔BLOCKING 用户交互确认
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .config import ConfigManager, LLMConfig
from .ppt_engine import PPTEngine, PROJECTS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PPT-Web", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# ── 任务管理 ──
tasks: dict[str, dict] = {}
ws_connections: dict[str, list[WebSocket]] = {}

# ── 任务持久化 ──
def _save_task_meta(task_id: str, task: dict):
    """将任务元信息持久化到项目目录，容器重启后可恢复"""
    try:
        project_path = None
        pd = task.get("preview_data") or {}
        project_path = pd.get("project_path")
        if not project_path:
            rp = task.get("result_path")
            if rp:
                project_path = str(Path(rp).parent.parent)
        if project_path and Path(project_path).is_dir():
            meta = {
                "task_id": task_id,
                "status": task.get("status", ""),
                "topic": task.get("topic", ""),
                "result_path": task.get("result_path"),
                "project_path": project_path,
            }
            (Path(project_path) / "task_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
    except Exception as e:
        logger.warning("保存task_meta失败: %s", e)

def _restore_tasks():
    """启动时从 projects/ 目录恢复已完成的任务到内存"""
    restored = 0
    for projects_root in [PROJECTS_DIR, Path("/app/projects")]:
        if not projects_root.is_dir():
            continue
        for d in projects_root.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "task_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                tid = meta.get("task_id")
                if not tid or tid in tasks:
                    continue
                # 只恢复已完成/错误状态的任务（进行中的无法恢复）
                status = meta.get("status", "")
                if status in ("done", "error", "awaiting_confirm"):
                    tasks[tid] = {
                        "id": tid,
                        "status": status,
                        "progress": 100 if status == "done" else 0,
                        "stage": "done" if status == "done" else status,
                        "message": "任务已恢复(重启后)" if status == "done" else "",
                        "result_path": meta.get("result_path"),
                        "topic": meta.get("topic", ""),
                        "preview_data": {"project_path": meta.get("project_path", str(d))},
                    }
                    restored += 1
            except Exception as e:
                logger.warning("恢复task_meta %s 失败: %s", d.name, e)
    if restored:
        logger.info("从磁盘恢复了 %d 个任务", restored)

@app.on_event("startup")
def _on_startup():
    _restore_tasks()
    logger.info("启动完成, 内存中 %d 个任务", len(tasks))


# ── 数据模型 ──

class PreviewPayload(BaseModel):
    source_text: str
    topic: str
    source_type: str = "text"
    format: str = "ppt169"
    enable_thinking: bool = False


class ConfirmPayload(BaseModel):
    task_id: str
    confirmations: dict  # 用户确认/修改后的 Eight Confirmations
    spec_lock: Optional[str] = None  # 如果用户没改, 直接用原始 spec_lock
    enable_thinking: bool = False


class GeneratePayload(BaseModel):
    source_text: str
    topic: str
    source_type: str = "text"
    format: str = "ppt169"
    enable_thinking: bool = False


# ══════════════════════════════════════════════════════════
#  API 路由
# ══════════════════════════════════════════════════════════

@app.get("/api/config")
def get_config():
    return ConfigManager.get_masked()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文档，提取文本内容返回"""
    from .file_parser import extract_text, SUPPORTED_EXTENSIONS

    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"不支持的文件格式: {ext}，支持: {', '.join(SUPPORTED_EXTENSIONS)}")

    contents = await file.read()
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(400, "文件不能超过 50MB")

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

    return {"filename": filename, "text": text, "char_count": len(text)}


# ── Step 4 交互式流程 ──

@app.post("/api/generate/preview")
async def start_preview(payload: PreviewPayload):
    """Steps 1-4: 生成设计方案 + Eight Confirmations (供用户确认)"""
    cfg = ConfigManager.load()
    if not cfg.is_configured():
        raise HTTPException(400, "请先配置 LLM API Key")

    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {
        "id": task_id,
        "status": "previewing",
        "progress": 0,
        "stage": "init",
        "message": "正在生成设计方案...",
        "result_path": None,
        "topic": payload.topic,
        "preview_data": None,  # 待填充
    }

    asyncio.create_task(_run_preview(task_id, cfg, payload))
    return {"task_id": task_id}


class ReplanPayload(BaseModel):
    task_id: str
    target_pages: int
    enable_thinking: bool = False


@app.post("/api/generate/replan")
async def replan_pages(payload: ReplanPayload):
    """用户调整页数后，让LLM重新规划 page_plan"""
    task = tasks.get(payload.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    pd = task.get("preview_data")
    if not pd:
        raise HTTPException(400, "无预览数据")

    cfg = ConfigManager.load()
    engine = PPTEngine(cfg)
    loop = asyncio.get_event_loop()

    new_pages = await loop.run_in_executor(
        None,
        lambda: engine.replan_page_plan(
            source_text=pd.get("source_text", ""),
            topic=pd.get("topic", task.get("topic", "")),
            spec_lock=pd.get("spec_lock", ""),
            target_pages=payload.target_pages,
            enable_thinking=payload.enable_thinking,
        )
    )

    if new_pages:
        pd["replanned_pages"] = new_pages  # 存到独立key，不覆盖原始page_plan
        # 同步更新 eight_confirmations 的 page_count
        cf = pd.get("eight_confirmations", {})
        pc = cf.get("page_count", {})
        pc["recommended"] = payload.target_pages
        if pc.get("min", 0) > payload.target_pages:
            pc["min"] = payload.target_pages
        if pc.get("max", 0) < payload.target_pages:
            pc["max"] = payload.target_pages
        cf["page_count"] = pc
        pd["eight_confirmations"] = cf
        task["preview_data"] = pd

    return {"page_plan": new_pages}


@app.post("/api/generate/confirm")
async def confirm_and_execute(payload: ConfirmPayload):
    """用户确认 Eight Confirmations → Steps 5-7: 生成SVG + 导出PPTX"""
    task = tasks.get(payload.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "awaiting_confirm":
        raise HTTPException(400, f"任务状态不对: {task['status']}，需要先完成设计预览")

    task["status"] = "executing"
    task["message"] = "正在生成PPT..."

    asyncio.create_task(_run_execute(payload.task_id, payload))
    return {"task_id": payload.task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return {"id": task_id, **task}


@app.get("/api/download/{task_id}")
def download_pptx(task_id: str):
    try:
        project_dir = _get_project_dir(task_id)
    except HTTPException:
        # 最后尝试从内存找
        task = tasks.get(task_id)
        if task and task.get("result_path"):
            path = Path(task["result_path"])
            if path.exists():
                return FileResponse(str(path), filename=path.name,
                    media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        raise HTTPException(404, "PPTX 尚未生成完成或任务不存在")

    # 优先从 exports/ 目录按修改时间取最新的（apply后会生成新PPTX）
    exports_dir = project_dir / "exports"
    all_pptx = list(exports_dir.glob("*.pptx")) if exports_dir.exists() else []
    all_pptx.extend(project_dir.glob("*.pptx"))
    all_pptx = sorted(list(set(all_pptx)), key=lambda f: f.stat().st_mtime, reverse=True)
    if all_pptx:
        return FileResponse(
            str(all_pptx[0]), filename=all_pptx[0].name,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    # fallback: 内存中的result_path
    task = tasks.get(task_id)
    if task and task.get("result_path"):
        path = Path(task["result_path"])
        if path.exists():
            return FileResponse(str(path), filename=path.name,
                media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    raise HTTPException(404, "PPTX 文件未找到")


@app.get("/api/preview/{task_id}")
def preview_svg(task_id: str):
    """返回SVG预览编辑页面"""
    # 验证项目目录存在（不依赖内存 tasks）
    project_dir = _get_project_dir(task_id)
    html_path = FRONTEND_DIST / "svg-editor.html"
    if not html_path.exists():
        raise HTTPException(500, "SVG编辑器未部署")
    html = html_path.read_text(encoding="utf-8")
    # 注入 API 路径和 task_id
    api_base = f"/api/preview/{task_id}"
    # 在 <head> 后注入 __API_BASE__ 全局变量
    html = html.replace("<head>", f'<head><script>window.__API_BASE__="{api_base}";</script>', 1)
    html = html.replace('id="download-link" href="#"', f'id="download-link" href="/api/download/{task_id}"')
    return HTMLResponse(html)


# ── ppt-master SVG 编辑器（复用原始前端） ──
@app.get("/editor/{task_id}")
def editor_page(task_id: str):
    """SVG编辑器页面（复用ppt-master的svg_editor/static/）"""
    _get_project_dir(task_id)  # 验证项目存在
    index = FRONTEND_DIST / "svg-editor" / "index.html"
    if not index.exists():
        raise HTTPException(500, "SVG编辑器未部署")
    return FileResponse(str(index), media_type="text/html")


@app.get("/editor/{task_id}/style.css")
def editor_css(task_id: str):
    css = FRONTEND_DIST / "svg-editor" / "style.css"
    if css.exists():
        return FileResponse(str(css), media_type="text/css")
    raise HTTPException(404)


@app.get("/editor/{task_id}/app.js")
def editor_js(task_id: str):
    js = FRONTEND_DIST / "svg-editor" / "app.js"
    if js.exists():
        return FileResponse(str(js), media_type="application/javascript")
    raise HTTPException(404)



# ── 图片/素材路由（SVG中引用的 ../images/* 和 ../assets/*） ──
@app.get("/editor/{task_id}/images/{filename}")
def editor_image(task_id: str, filename: str):
    project_dir = _get_project_dir(task_id)
    img = project_dir / "images" / filename
    if img.exists() and img.is_file():
        return FileResponse(str(img))
    raise HTTPException(404)


@app.get("/editor/{task_id}/assets/{filename}")
def editor_asset(task_id: str, filename: str):
    project_dir = _get_project_dir(task_id)
    asset = project_dir / "assets" / filename
    if asset.exists() and asset.is_file():
        return FileResponse(str(asset))
    raise HTTPException(404)


# 兼容旧版SVG编辑器路由
# SVG编辑器静态文件
@app.get("/svg-editor-style.css")
def serve_editor_css():
    css = FRONTEND_DIST / "svg-editor-style.css"
    if css.exists():
        return FileResponse(str(css), media_type="text/css")
    raise HTTPException(404)

@app.get("/svg-editor-app.js")
def serve_editor_js():
    js = FRONTEND_DIST / "svg-editor-app.js"
    if js.exists():
        return FileResponse(str(js), media_type="application/javascript")
    raise HTTPException(404)


@app.get("/api/svg/{task_id}/{svg_name}")
def serve_svg(task_id: str, svg_name: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404)
    project_dir = Path(task["result_path"]).parent.parent if task.get("result_path") else None
    if not project_dir:
        if task.get("preview_data") and task["preview_data"].get("project_path"):
            project_dir = Path(task["preview_data"]["project_path"])
        if not project_dir:
            raise HTTPException(404)
    svg_path = project_dir / "svg_output" / svg_name
    if not svg_path.exists():
        raise HTTPException(404)
    return FileResponse(str(svg_path), media_type="image/svg+xml")


# ── 兼容旧的一步到位 API ──

@app.post("/api/generate")
async def start_generate(payload: GeneratePayload):
    """一步到位(跳过用户确认, 向后兼容)"""
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

    asyncio.create_task(_run_generate(task_id, cfg, payload))
    return {"task_id": task_id}


# ── WebSocket 进度 ──

@app.websocket("/ws/tasks/{task_id}")
async def ws_task_progress(ws: WebSocket, task_id: str):
    await ws.accept()
    if task_id not in ws_connections:
        ws_connections[task_id] = []
    ws_connections[task_id].append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_connections[task_id].remove(ws)


# ── SVG 预览/编辑 API ──

# ── SVG 编辑 ID 注入（不破坏原始 SVG 文本） ──

_edit_id_counter = 0

def _add_edit_ids_raw(svg_text: str) -> tuple[str, list[dict]]:
    """直接在原始 SVG 文本上用正则给元素添加 _edit_N id，不经过 ET 解析。
    
    这样保留原始引号风格（单引号属性值），避免 ET.tostring 的 &quot; 问题。
    """
    global _edit_id_counter
    _edit_id_counter = 0
    annotations = []
    
    # 匹配 SVG 可编辑元素（排除 defs, linearGradient, stop, style 等）
    editable_tags = {"svg", "g", "rect", "circle", "ellipse", "line", "path", "text", "polygon", "polyline", "image"}
    
    def _add_id(match):
        global _edit_id_counter
        indent = match.group(1) or ""
        tag = match.group(2)
        attrs = match.group(3)
        
        if tag.lower() not in editable_tags:
            return match.group(0)
        
        # 已经有 id 的跳过
        if re.search(r'\bid\s*=\s*[\'"]', attrs):
            return match.group(0)
        
        _edit_id_counter += 1
        new_id = f"_edit_{_edit_id_counter}"
        return f'{indent}<{tag} id="{new_id}"{attrs}>'
    
    # 匹配开始标签：<tag ...> 或 <tag .../>  (不匹配结束标签 </tag>)
    result = re.sub(
        r'^(\s*)<(svg|g|rect|circle|ellipse|line|path|text|polygon|polyline|image)\b((?:[^>]|"[^"]*"|\'[^\']*\')*)>',
        _add_id,
        svg_text,
        flags=re.MULTILINE,
    )
    
    return result, annotations


def _get_project_dir(task_id: str) -> Path:
    """从任务字典获取项目目录，失败抛 HTTPException
    
    支持四种查找方式：
    1. 内存 tasks 字典（容器未重启时有效）
    2. task_meta.json 映射（重启后恢复的持久化数据）
    3. 精确目录名匹配
    4. 模糊匹配（task_id 是目录名的子串，如 '623b10' 匹配 '..._623b10_ppt169_...'）
    """
    task = tasks.get(task_id)
    if task:
        # 优先用 preview_data.project_path（最可靠）
        preview_data = task.get("preview_data") or {}
        project_path = preview_data.get("project_path")
        if project_path and Path(project_path).exists():
            return Path(project_path)
        # fallback: result_path 推算（可能不准，如backup子目录）
        result_path = task.get("result_path")
        if result_path:
            rp = Path(result_path)
            # 从任意深度找到含 task_meta.json 的项目根目录
            for parent in rp.parents:
                if (parent / "task_meta.json").exists():
                    return parent
            # 最终fallback: .parent.parent（旧逻辑）
            project_dir = rp.parent.parent
            if project_dir.exists():
                return project_dir

    # fallback 0: 扫描 task_meta.json 查找 task_id → project_path 映射
    for projects_root in [PROJECTS_DIR, PROJECTS_DIR / "projects", Path("/app/projects")]:
        if not projects_root.is_dir():
            continue
        for d in projects_root.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "task_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("task_id") == task_id:
                    return d
            except Exception:
                pass

    # fallback 1: task_id 可能就是项目目录名，在 projects/ 下直接找
    for projects_root in [PROJECTS_DIR, PROJECTS_DIR / "projects", Path("/app/projects")]:
        candidate = projects_root / task_id
        if candidate.is_dir():
            return candidate
        # fallback 2: 模糊匹配 — task_id 是目录名的子串（如短 UUID 匹配完整目录名）
        if projects_root.is_dir():
            for d in projects_root.iterdir():
                if d.is_dir() and task_id in d.name:
                    return d

    if not task:
        raise HTTPException(404, f"任务不存在 (id={task_id})")
    raise HTTPException(404, "项目目录不存在")


def _safe_svg_name(name: str) -> str:
    """检查SVG文件名安全性，防止路径遍历"""
    if not name or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400, "非法文件名")
    if not name.lower().endswith(".svg"):
        raise HTTPException(400, "仅支持 .svg 文件")
    return name


def _svg_dir_for(project_dir: Path) -> Path:
    """返回可用SVG目录 (svg_output 优先以保留原始引号风格, fallback svg_final)"""
    svg_output = project_dir / "svg_output"
    if svg_output.exists() and any(svg_output.glob("*.svg")):
        return svg_output
    svg_final = project_dir / "svg_final"
    if svg_final.exists() and any(svg_final.glob("*.svg")):
        return svg_final
    raise HTTPException(404, "SVG 尚未生成")


@app.get("/api/preview/{task_id}/slides")
def list_slides(task_id: str):
    """返回项目 SVG 文件列表（复用ppt-master逻辑：合并disk+内存标注计数）"""
    from .svg_annotations import parse_annotations

    project_dir = _get_project_dir(task_id)
    svg_dir = _svg_dir_for(project_dir)
    import re as _re
    def _natural_key(s):
        return [int(c) if c.isdigit() else c.lower() for c in _re.split(r'(\d+)', s)]
    svgs = sorted((p.name for p in svg_dir.glob("*.svg")), key=_natural_key)

    ann_store = _annotation_store.get(task_id, {})
    slides = []
    for name in svgs:
        # disk标注（与ppt-master一致）
        disk_count = 0
        try:
            tree = ET.parse(str(svg_dir / name))
            disk_count = len(parse_annotations(tree.getroot()))
        except Exception:
            pass
        mem_count = len(ann_store.get(name, {}))
        annotation_count = max(disk_count, mem_count)
        slides.append({
            "name": name,
            "annotated": annotation_count > 0,
            "annotation_count": annotation_count,
        })
    return {"slides": slides, "dir": svg_dir.name}


@app.get("/api/preview/{task_id}/slide/{name}")
def get_slide(task_id: str, name: str):
    """返回单个 SVG 文件内容（复用ppt-master逻辑：ET解析+assign_temp_ids+标注合并+icon内联）"""
    from .svg_annotations import assign_temp_ids, parse_annotations

    safe_name = _safe_svg_name(name)
    project_dir = _get_project_dir(task_id)
    svg_dir = _svg_dir_for(project_dir)
    svg_path = svg_dir / safe_name
    if not svg_path.exists():
        raise HTTPException(404, f"SVG 文件不存在: {safe_name}")

    # ── ET解析（与ppt-master一致） ──
    try:
        tree = ET.parse(str(svg_path))
        root = tree.getroot()
    except ET.ParseError as e:
        raise HTTPException(500, f"SVG解析失败: {e}")

    assign_temp_ids(root)
    disk_annotations = parse_annotations(root)

    # 合并内存中的标注（覆盖disk标注）
    mem_annotations = _annotation_store.get(task_id, {}).get(safe_name, {})
    merged = {}
    for ann in disk_annotations:
        merged[ann["element_id"]] = ann["annotation"]
    merged.update(mem_annotations)

    # 构建标注列表（与ppt-master一致：遍历root.iter找标注元素）
    annotations_list = []
    for elem in root.iter():
        eid = elem.get("id")
        if eid and eid in merged:
            tag = elem.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            annotations_list.append({
                "element_id": eid,
                "tag": tag,
                "annotation": merged[eid],
            })

    content = ET.tostring(root, encoding="unicode", xml_declaration=False)
    # 清理ET产生的命名空间前缀
    content = re.sub(r'\bns0:', '', content)
    content = re.sub(r'\s+xmlns:ns0="[^"]*"', '', content)
    content = content.replace('&quot;', "'")
    if 'xmlns="http://www.w3.org/2000/svg"' not in content:
        content = content.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)

    # icon内联（与ppt-master _inline_icons一致）
    try:
        from .embed_icons import (
            parse_use_element, resolve_icon_path,
            extract_paths_from_icon, generate_icon_group,
        )
        _ICONS_DIR = Path(__file__).resolve().parents[2] / "skills" / "ppt-master" / "templates" / "icons"
        _USE_ICON_PATTERN = re.compile(r'<use\s+[^>]*data-icon="[^"]*"[^>]*/>')
        matches = list(_USE_ICON_PATTERN.finditer(content))
        if matches:
            new_content = content
            for match in reversed(matches):
                use_str = match.group(0)
                try:
                    attrs = parse_use_element(use_str)
                    icon_name = attrs.get('icon')
                    if not icon_name:
                        continue
                    icon_path, _ = resolve_icon_path(str(icon_name), str(_ICONS_DIR))
                    color = str(attrs.get('fill', '#000000'))
                    elements, style, base_size = extract_paths_from_icon(icon_path, color)
                    if not elements:
                        continue
                    replacement = generate_icon_group(attrs, elements, style, base_size)
                    id_match = re.search(r'\bid="([^"]+)"', use_str)
                    if id_match:
                        replacement = replacement.replace(
                            '<g ', f'<g id="{id_match.group(1)}" data-icon="{icon_name}" ', 1,
                        )
                    new_content = new_content[:match.start()] + replacement + new_content[match.end():]
                except Exception:
                    continue
            content = new_content
    except ImportError:
        pass  # embed_icons不可用时跳过

    return {"name": safe_name, "content": content, "annotations": annotations_list}


# ── SVG Editor 标注路由 (复用 ppt-master annotations.py) ──

# 每个task一个标注内存存储: {task_id: {filename: {element_id: annotation}}}
_annotation_store: dict[str, dict[str, dict[str, str]]] = {}


@app.post("/api/preview/{task_id}/slide/{name}/annotate")
def annotate_element(task_id: str, name: str, payload: dict):
    """添加/更新标注"""
    safe_name = _safe_svg_name(name)
    element_id = payload.get("element_id", "")
    annotation = payload.get("annotation", "")
    if not element_id or not annotation:
        raise HTTPException(400, "Missing element_id or annotation")

    store = _annotation_store.setdefault(task_id, {})
    file_store = store.setdefault(safe_name, {})
    file_store[element_id] = annotation
    return {"status": "ok", "annotations_count": len(file_store)}


@app.delete("/api/preview/{task_id}/slide/{name}/annotate/{element_id}")
def delete_annotation(task_id: str, name: str, element_id: str):
    """删除标注"""
    safe_name = _safe_svg_name(name)
    store = _annotation_store.get(task_id, {})
    file_store = store.setdefault(safe_name, {})
    file_store.pop(element_id, None)
    return {"status": "ok", "annotations_count": len(file_store)}


@app.post("/api/preview/{task_id}/save-all")
def save_all_annotations(task_id: str):
    """将所有标注写入SVG文件 (复用ppt-master annotations模块)"""
    from .svg_annotations import assign_temp_ids, parse_annotations, set_annotation

    store = _annotation_store.get(task_id, {})
    project_dir = _get_project_dir(task_id)
    svg_dir = _svg_dir_for(project_dir)
    modified = []

    for filename, anns in store.items():
        safe_name = _safe_svg_name(filename)
        svg_file = svg_dir / safe_name
        if not svg_file.exists():
            continue
        try:
            tree = ET.parse(str(svg_file))
            root = tree.getroot()
        except ET.ParseError:
            continue

        assign_temp_ids(root)
        # 清除旧标注
        for elem in root.iter():
            elem.attrib.pop("data-edit-target", None)
            elem.attrib.pop("data-edit-annotation", None)

        # 写入新标注
        for element_id, annotation_text in anns.items():
            set_annotation(root, element_id, annotation_text)

        # 清理未标注元素的临时id
        annotated_ids = set(anns.keys())
        for elem in root.iter():
            eid = elem.get("id", "")
            if eid.startswith("_edit_") and eid not in annotated_ids:
                elem.attrib.pop("id", None)

        tree.write(str(svg_file), encoding="UTF-8", xml_declaration=True)
        modified.append(filename)

    # 清空已保存的标注
    _annotation_store.pop(task_id, None)
    return {"status": "ok", "files_modified": modified}


@app.post("/api/preview/{task_id}/apply-annotations")
async def apply_annotations_api(task_id: str):
    """根据用户标注调用LLM修改SVG, 并重新导出PPTX"""
    from .ppt_engine import apply_annotations, PPTEngine

    pd = tasks.get(task_id)
    if not pd:
        # 尝试从磁盘恢复单个任务
        _restore_tasks()
        pd = tasks.get(task_id)
    if not pd:
        raise HTTPException(404, "任务不存在")

    # 获取标注: 优先内存中的标注, 然后从磁盘读取
    annotations_data = _annotation_store.get(task_id, {})
    if not annotations_data:
        # 尝试从SVG文件中读取已保存的标注
        from .svg_annotations import parse_annotations
        project_dir = _get_project_dir(task_id)
        svg_dir = _svg_dir_for(project_dir)
        for svg_file in svg_dir.glob("*.svg"):
            try:
                tree = ET.parse(str(svg_file))
                root = tree.getroot()
                disk_anns = parse_annotations(root)
                if disk_anns:
                    file_anns = {}
                    for ann in disk_anns:
                        file_anns[ann["element_id"]] = ann["annotation"]
                    annotations_data[svg_file.name] = file_anns
            except Exception:
                pass

    if not annotations_data:
        raise HTTPException(400, "没有找到标注, 请先在编辑器中添加标注并提交")

    # 创建engine实例(复用LLM配置)
    cfg = ConfigManager.load()
    engine = PPTEngine(cfg)

    project_path = str((pd.get("preview_data") or {}).get("project_path", ""))
    if not project_path:
        project_path = str((pd.get("result_path") and str(Path(pd["result_path"]).parent.parent)) or "")
    if not project_path or not Path(project_path).exists():
        # 最终fallback: 用_get_project_dir
        try:
            project_path = str(_get_project_dir(task_id))
        except HTTPException:
            raise HTTPException(500, "项目目录不存在")

    # 通过WebSocket推送进度
    async def _ws_progress(stage: str, msg: str, pct: int):
        try:
            conns = ws_connections.get(task_id, [])
            for ws in conns:
                await ws.send_json({"stage": stage, "message": msg, "progress": pct})
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: apply_annotations(
            engine, project_path, annotations_data,
            progress=lambda s, m, p: asyncio.run_coroutine_threadsafe(
                _ws_progress(s, m, p), loop
            ).result() if loop.is_running() else None,
        ),
    )

    # 清除已应用的标注
    _annotation_store.pop(task_id, None)

    return result


@app.get("/api/preview/{task_id}/config")
def editor_config(task_id: str):
    """编辑器配置"""
    return {"live": False}


@app.post("/api/preview/{task_id}/shutdown")
def editor_shutdown(task_id: str):
    """关闭编辑器 (web版不需要真正关闭, 直接返回ok)"""
    return {"status": "ok"}


class SVGEditPayload(BaseModel):
    name: str
    content: str


@app.post("/api/preview/{task_id}/save")
async def save_slide(task_id: str, payload: SVGEditPayload):
    """保存用户修改后的 SVG，并重新导出 PPTX"""
    safe_name = _safe_svg_name(payload.name)
    project_dir = _get_project_dir(task_id)

    # 确保 svg_final 目录存在
    svg_final = project_dir / "svg_final"
    svg_final.mkdir(parents=True, exist_ok=True)

    target = svg_final / safe_name
    target.write_text(payload.content, encoding="utf-8")
    logger.info("SVG 已保存: %s", target)

    # 重新导出 PPTX (仅native模式)
    from .ppt_engine import SCRIPTS_DIR, PPTEngine
    import sys

    export_cmd = [sys.executable, str(SCRIPTS_DIR / "svg_to_pptx.py"), str(project_dir)]
    result = await asyncio.to_thread(
        subprocess.run, export_cmd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        # native失败 → 修复SVG后重试
        err_msg = result.stderr
        logger.warning("save: native失败, 修复SVG后重试: %s", err_msg[:200])
        svg_final_dir = project_dir / "svg_final"
        if svg_final_dir.exists():
            # _fix_svg不需要LLM调用, 纯正则修复
            from .ppt_engine import PPTEngine
            engine = PPTEngine(ConfigManager.load())
            for svg_file in svg_final_dir.glob("*.svg"):
                try:
                    raw = svg_file.read_text(encoding="utf-8")
                    fixed = engine._fix_svg(raw)
                    if fixed != raw:
                        svg_file.write_text(fixed, encoding="utf-8")
                except Exception:
                    pass
        result2 = await asyncio.to_thread(
            subprocess.run, export_cmd, capture_output=True, text=True, timeout=120
        )
        if result2.returncode != 0:
            raise HTTPException(500, f"svg_to_pptx native重试也失败: {result2.stderr[:500]}")

    # 搜索PPTX文件 (仅native exports/)
    exports_dir = project_dir / "exports"
    all_pptx = list(exports_dir.glob("*.pptx")) if exports_dir.exists() else []
    all_pptx.extend(project_dir.glob("*.pptx"))
    all_pptx = sorted(list(set(all_pptx)), key=lambda f: f.stat().st_mtime, reverse=True)

    if all_pptx:
        task = tasks.get(task_id, {})
        task["result_path"] = str(all_pptx[0])

    return {"ok": True, "saved": safe_name}


@app.post("/api/preview/{task_id}/rebuild")
async def rebuild_pptx(task_id: str):
    """重新执行 finalize_svg + svg_to_pptx 导出 PPTX"""
    project_dir = _get_project_dir(task_id)

    from .ppt_engine import SCRIPTS_DIR
    import sys

    # Step 1: finalize_svg
    finalize_cmd = [sys.executable, str(SCRIPTS_DIR / "finalize_svg.py"), str(project_dir)]
    result = await asyncio.to_thread(
        subprocess.run, finalize_cmd, capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("finalize_svg 失败: %s", result.stderr)
        raise HTTPException(500, f"finalize_svg 失败: {result.stderr[:500]}")

    # Step 2: svg_to_pptx (仅native模式)
    export_cmd = [sys.executable, str(SCRIPTS_DIR / "svg_to_pptx.py"), str(project_dir)]
    result = await asyncio.to_thread(
        subprocess.run, export_cmd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        # native失败 → 修复SVG后重试
        err_msg = result.stderr
        logger.warning("rebuild: native失败, 修复SVG后重试: %s", err_msg[:200])
        svg_final_dir = project_dir / "svg_final"
        if svg_final_dir.exists():
            from .ppt_engine import PPTEngine
            engine = PPTEngine(ConfigManager.load())
            for svg_file in svg_final_dir.glob("*.svg"):
                try:
                    raw = svg_file.read_text(encoding="utf-8")
                    fixed = engine._fix_svg(raw)
                    if fixed != raw:
                        svg_file.write_text(fixed, encoding="utf-8")
                except Exception:
                    pass
        result2 = await asyncio.to_thread(
            subprocess.run, export_cmd, capture_output=True, text=True, timeout=120
        )
        if result2.returncode != 0:
            raise HTTPException(500, f"svg_to_pptx native重试也失败: {result2.stderr[:500]}")

    # 搜索PPTX文件（exports/ + 根目录）
    exports_dir = project_dir / "exports"
    all_pptx = list(exports_dir.glob("*.pptx")) if exports_dir.exists() else []
    all_pptx.extend(project_dir.glob("*.pptx"))
    all_pptx = sorted(list(set(all_pptx)), key=lambda f: f.stat().st_mtime, reverse=True)

    if all_pptx:
        task = tasks.get(task_id, {})
        task["result_path"] = str(all_pptx[0])

    return {"ok": True, "message": "PPTX 重新导出完成"}


# ── 前端 ──

@app.get("/")
async def serve_index():
    idx = FRONTEND_DIST / "index.html"
    if idx.exists():
        return HTMLResponse(idx.read_text())
    return HTMLResponse("<h1>PPT-Web</h1><p>前端未构建</p>")


# ══════════════════════════════════════════════════════════
#  内部函数
# ══════════════════════════════════════════════════════════

def _notify_ws(task_id: str, data: dict, loop=None):
    """推送 WebSocket (线程安全)"""
    conns = ws_connections.get(task_id, [])
    if not conns:
        return
    async def _send():
        for ws in list(conns):
            try:
                await ws.send_json(data)
            except Exception:
                pass
    if loop is None:
        loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_send(), loop)
    else:
        # 兜底：直接创建任务
        try:
            loop.create_task(_send())
        except RuntimeError:
            pass


async def _run_preview(task_id: str, cfg: LLMConfig, payload: PreviewPayload):
    """Steps 1-4: 生成设计方案"""
    task = tasks[task_id]
    try:
        engine = PPTEngine(cfg)

        loop = asyncio.get_event_loop()

        def on_progress(stage, msg, pct):
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = pct
            _notify_ws(task_id, {"stage": stage, "message": msg, "progress": pct}, loop=loop)

        preview_data = await loop.run_in_executor(
            None,
            lambda: engine.preview(
                source_text=payload.source_text,
                topic=payload.topic,
                fmt=payload.format,
                enable_thinking=payload.enable_thinking,
                progress=on_progress,
            )
        )

        task["status"] = "awaiting_confirm"
        task["message"] = "设计方案已生成，请确认"
        task["progress"] = 25
        task["preview_data"] = preview_data
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "awaiting_confirm", "message": "设计方案已生成，请确认", "progress": 25, "status": "awaiting_confirm"}, loop=loop)

    except Exception as e:
        logger.exception("Preview 任务 %s 失败", task_id)
        task["status"] = "error"
        task["message"] = f"设计方案生成失败: {e}"
        task["progress"] = 0
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": 0, "status": "error"}, loop=asyncio.get_event_loop())


async def _run_execute(task_id: str, payload: ConfirmPayload):
    """Steps 5-7: 执行生成"""
    task = tasks[task_id]
    preview_data = task.get("preview_data", {})

    try:
        cfg = ConfigManager.load()
        engine = PPTEngine(cfg)

        loop = asyncio.get_event_loop()

        def on_progress(stage, msg, pct):
            # 映射到全局进度 (25-100)
            global_pct = 25 + int(pct * 0.75)
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = global_pct
            _notify_ws(task_id, {"stage": stage, "message": msg, "progress": global_pct}, loop=loop)

        project_path = preview_data.get("project_path")
        spec_lock = payload.spec_lock or preview_data.get("spec_lock", "")
        confirmations = payload.confirmations

        # 检查用户是否修改了 Eight Confirmations（排除 page_count，它已通过 replan 处理）
        original_cf = preview_data.get("eight_confirmations", {})
        # 深拷贝后去掉 page_count 再比较，因为页数变化已由 replan 处理
        cf_without_pages = copy.deepcopy(confirmations)
        orig_without_pages = copy.deepcopy(original_cf)
        cf_without_pages.pop("page_count", None)
        orig_without_pages.pop("page_count", None)
        need_regenerate = (cf_without_pages != orig_without_pages)
        regen_result = None

        # 用户确认后的 page_plan
        # 策略：如果用户没改过页数(replan没被调用)，始终用 preview 阶段原始解析的 page_plan
        # 只有用户明确通过 replan API 调整了页数，才使用 replan 的结果
        replanned_page_plan = preview_data.get("replanned_pages")  # 只有replan API才会设这个key

        if need_regenerate:
            logger.info("用户修改了 Eight Confirmations, 重新生成 spec_lock")
            regen_result = await loop.run_in_executor(
                None,
                lambda: engine.confirm_and_regenerate(
                    project_path_str=project_path,
                    user_modifications=confirmations,
                    enable_thinking=payload.enable_thinking,
                    progress=on_progress,
                )
            )
            spec_lock = regen_result["spec_lock"]

        # 优先用 replan 结果 > regenerate 结果 > preview 原始
        if replanned_page_plan:
            user_page_plan = replanned_page_plan
            logger.info("使用用户 replan 后的 page_plan (%d 页)", len(replanned_page_plan))
        elif need_regenerate and regen_result:
            user_page_plan = regen_result.get("page_plan")
            preview_data["page_plan"] = user_page_plan
            logger.info("使用 regenerate 的 page_plan (%d 页)", len(user_page_plan or []))
        else:
            user_page_plan = preview_data.get("page_plan")
            logger.info("使用 preview 原始 page_plan (%d 页)", len(user_page_plan or []))

        result_path = await loop.run_in_executor(
            None,
            lambda: engine.execute(
                project_path_str=project_path,
                spec_lock=spec_lock,
                page_plan=user_page_plan,
                enable_thinking=payload.enable_thinking,
                progress=on_progress,
            )
        )

        task["status"] = "done"
        task["progress"] = 100
        task["message"] = "PPT生成完成!"
        task["result_path"] = str(result_path)
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "done", "message": "PPT生成完成!", "progress": 100, "status": "done"}, loop=loop)

    except Exception as e:
        logger.exception("Execute 任务 %s 失败", task_id)
        task["status"] = "error"
        task["message"] = f"生成失败: {e}"
        task["progress"] = 0
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": 0, "status": "error"}, loop=asyncio.get_event_loop())


async def _run_generate(task_id: str, cfg: LLMConfig, payload: GeneratePayload):
    """一步到位(向后兼容)"""
    task = tasks[task_id]
    try:
        engine = PPTEngine(cfg)

        loop = asyncio.get_event_loop()

        def on_progress(stage, msg, pct):
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = pct
            _notify_ws(task_id, {"stage": stage, "message": msg, "progress": pct}, loop=loop)

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
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "done", "message": "PPT生成完成!", "progress": 100, "status": "done"}, loop=loop)

    except Exception as e:
        logger.exception("任务 %s 失败", task_id)
        task["status"] = "error"
        task["message"] = f"生成失败: {e}"
        task["progress"] = 0
        _save_task_meta(task_id, task)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": 0, "status": "error"}, loop=asyncio.get_event_loop())
