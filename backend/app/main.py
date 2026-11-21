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
import threading
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

import time
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PPT-Web", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# ── 任务管理 ──
tasks: dict[str, dict] = {}
ws_connections: dict[str, list[WebSocket]] = {}

# ── 任务持久化 ──
STATE_DIR = PROJECTS_DIR / ".task_states"
STATE_SCHEMA_VERSION = 1
_state_lock = threading.RLock()

_PERSIST_KEYS = (
    "id", "status", "progress", "stage", "message", "topic",
    "result_path", "project_path", "created_at", "last_active",
    "preview_payload", "preview_data", "execution", "replan",
)


def _projects_roots() -> list[Path]:
    """返回所有可能存放项目的根目录（去重、只保留已存在的）"""
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in (PROJECTS_DIR, PROJECTS_DIR / "projects", Path("/app/projects")):
        try:
            if not candidate.is_dir():
                continue
            key = str(candidate.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return roots


def _state_path(task_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))[:128] or "task"
    return STATE_DIR / f"{safe_id}.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入 JSON，避免进程中断时留下半个文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _task_project_path(task: dict) -> Optional[Path]:
    """从任务字段推导项目目录，优先 preview_data.project_path"""
    pd = task.get("preview_data") or {}
    for candidate in (task.get("project_path"), pd.get("project_path")):
        if candidate:
            p = Path(candidate)
            if p.is_dir():
                return p
    rp = task.get("result_path")
    if rp:
        p = Path(rp)
        for parent in p.parents:
            if (parent / "task_meta.json").exists():
                return parent
            # 无 task_meta 时用项目结构识别根目录（避免把 backup/ 子目录当项目根）
            if (parent / "svg_output").is_dir() and (parent / "sources").is_dir():
                return parent
        if p.parent.parent.is_dir():
            return p.parent.parent
    return None


def _find_project_dir_for_task(task_id: str, hint: Optional[str] = None) -> Optional[Path]:
    """按 task_id 扫描项目目录；兼容容器迁移后绝对路径失效的情况"""
    if hint:
        p = Path(hint)
        if p.is_dir():
            return p
    if not task_id:
        return None
    candidates: list[Path] = []
    for root in _projects_roots():
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for d in children:
            if not d.is_dir() or d.name.startswith("."):
                continue
            meta_path = d / "task_meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("task_id") == task_id:
                        return d
                except Exception:
                    pass
            if task_id in d.name:
                candidates.append(d)
    if candidates:
        candidates.sort(
            key=lambda d: ((d / "sources" / "source.md").exists(), d.stat().st_mtime),
            reverse=True,
        )
        return candidates[0]
    return None


def _task_state_payload(task_id: str, task: dict, touch_last_active: bool = True) -> dict:
    now_iso = datetime.now().isoformat(timespec="seconds")
    state: dict = {"schema": STATE_SCHEMA_VERSION, "id": task_id}
    for key in _PERSIST_KEYS:
        if key in task:
            state[key] = copy.deepcopy(task[key])
    state["last_active"] = (
        now_iso if touch_last_active
        else (task.get("last_active") or task.get("created_at") or now_iso)
    )
    if not state.get("created_at"):
        state["created_at"] = task.get("created_at") or now_iso

    pd = state.get("preview_data")
    if isinstance(pd, dict):
        # 执行/完成后不再需要原文；状态文件保持轻量，也避免反复写大文本
        if state.get("status") in ("executing", "done"):
            pd.pop("source_text", None)
        if not pd.get("project_path") and state.get("project_path"):
            pd["project_path"] = state["project_path"]
    return state


def _persist_task(
    task_id: str,
    task: Optional[dict] = None,
    throttle_seconds: float = 0,
    touch_last_active: bool = True,
) -> None:
    """持久化任务完整状态；throttle_seconds > 0 时用于高频进度回调节流"""
    task = task if task is not None else tasks.get(task_id)
    if not task:
        return
    now_ts = time.time()
    last_write = float(task.get("_last_state_write") or 0)
    if throttle_seconds and last_write and (now_ts - last_write) < throttle_seconds:
        return

    try:
        with _state_lock:
            state = _task_state_payload(task_id, task, touch_last_active=touch_last_active)
            _atomic_write_json(_state_path(task_id), state)

            project_dir = _task_project_path(task)
            if project_dir:
                meta_path = project_dir / "task_meta.json"
                meta: dict = {}
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    except Exception:
                        meta = {}
                meta.update({
                    "task_id": task_id,
                    "status": task.get("status", ""),
                    "topic": task.get("topic", ""),
                    "result_path": task.get("result_path"),
                    "project_path": str(project_dir),
                    "last_active": state["last_active"],
                })
                _atomic_write_json(meta_path, meta)
            if touch_last_active:
                task["last_active"] = state["last_active"]
            task["_last_state_write"] = now_ts
    except Exception as e:
        # 持久化失败不应中断正在进行的生成任务
        logger.warning("持久化任务状态失败 %s: %s", task_id, e)


def _mark_task_error(task_id: str, message: str, progress: Optional[int] = None) -> None:
    task = tasks.get(task_id)
    if not task:
        return
    task["status"] = "error"
    task["message"] = message
    if progress is not None:
        task["progress"] = progress
    _persist_task(task_id)


def _resolve_preview_payload(task_id: str, task: dict) -> Optional[dict]:
    """恢复预览所需输入：优先状态里的 payload，其次项目 sources/source.md"""
    payload = copy.deepcopy(task.get("preview_payload") or {})
    project_dir = _task_project_path(task) or _find_project_dir_for_task(task_id)
    if project_dir:
        src = project_dir / "sources" / "source.md"
        if src.exists() and not payload.get("source_text"):
            payload["source_text"] = src.read_text(encoding="utf-8", errors="replace")
        payload.setdefault("topic", task.get("topic") or project_dir.name.split("_")[0])
        task["project_path"] = str(project_dir)
    if not payload.get("source_text"):
        return None
    payload.setdefault("topic", task.get("topic", ""))
    payload.setdefault("source_type", "text")
    payload.setdefault("format", "ppt169")
    payload.setdefault("enable_thinking", False)
    return payload


def _restore_tasks() -> list[tuple[str, str]]:
    """启动时从状态文件/旧 task_meta 恢复任务。

    Returns: 需要自动续跑的任务列表 [(kind, task_id)]，kind 为 preview/execute。
    """
    restored = 0
    resume_jobs: list[tuple[str, str]] = []
    seen_dirs: set[str] = set()

    # 1) 新状态文件（完整状态）
    if STATE_DIR.is_dir():
        for state_file in sorted(STATE_DIR.glob("*.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("恢复状态文件 %s 失败: %s", state_file.name, e)
                continue
            tid = state.get("id") or state_file.stem
            if not tid or tid in tasks:
                continue

            task: dict = {"id": tid}
            for key in _PERSIST_KEYS:
                if key in state:
                    task[key] = state[key]
            task.setdefault("status", "error")
            task.setdefault("progress", 0)
            task.setdefault("stage", "")
            task.setdefault("message", "")
            task.setdefault("topic", "")
            task.setdefault("result_path", None)
            task.setdefault("preview_payload", None)
            task.setdefault("preview_data", None)
            task.setdefault("execution", None)
            task.setdefault("replan", None)
            task.setdefault("created_at", state.get("created_at")
                            or datetime.fromtimestamp(state_file.stat().st_mtime).isoformat(timespec="seconds"))
            task.setdefault("last_active", state.get("last_active"))

            status = task.get("status") or "error"
            hint = task.get("project_path") or (task.get("preview_data") or {}).get("project_path")
            project_dir = _find_project_dir_for_task(tid, hint=hint)
            if project_dir:
                task["project_path"] = str(project_dir)
                pd = task.get("preview_data")
                if isinstance(pd, dict):
                    pd["project_path"] = str(project_dir)
                seen_dirs.add(str(project_dir.resolve()))

            # 重启时仍在运行的 replan 无法继续，转为失败态供前端重试
            replan = task.get("replan")
            if isinstance(replan, dict) and replan.get("status") == "running":
                replan.update({
                    "status": "failed",
                    "error": "服务重启导致重新规划中断，请重试",
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })
                task["replan"] = replan

            if status == "awaiting_confirm":
                pd = task.get("preview_data") or {}
                if not pd.get("eight_confirmations") or not pd.get("page_plan"):
                    task["status"] = "error"
                    task["message"] = "旧任务缺少方案确认数据（升级前未完整持久化），请重新生成设计方案"
                    task["progress"] = 0
                elif not project_dir:
                    task["status"] = "error"
                    task["message"] = "任务的项目目录不存在，无法继续确认生成"
                    task["progress"] = 0
            elif status == "previewing":
                resume_jobs.append(("preview", tid))
            elif status == "running":
                task["status"] = "error"
                task["message"] = "旧的一步生成任务无法断点续跑，已中断；请重新发起任务"
                task["progress"] = 0
            elif status == "executing":
                if project_dir:
                    resume_jobs.append(("execute", tid))
                else:
                    task["status"] = "error"
                    task["message"] = "服务重启后项目目录不存在，无法继续生成"
            elif status == "done":
                if not task.get("result_path") and not project_dir:
                    logger.warning("跳过无结果且无项目目录的完成任务: %s", tid)
                    continue
            elif status == "error":
                if not task.get("result_path") and not project_dir:
                    logger.warning("跳过无项目目录的错误任务: %s", tid)
                    continue

            tasks[tid] = task
            _persist_task(tid, touch_last_active=False)
            restored += 1

    # 2) 旧 task_meta.json 兼容
    for root in _projects_roots():
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for d in children:
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                resolved = str(d.resolve())
            except OSError:
                resolved = str(d)
            if resolved in seen_dirs:
                continue
            meta_path = d / "task_meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("恢复task_meta %s 失败: %s", d.name, e)
                continue
            tid = meta.get("task_id") or d.name
            if not tid or tid in tasks:
                continue

            status = meta.get("status", "") or "error"
            preview_data: dict = {"project_path": str(d)}
            src = d / "sources" / "source.md"
            if src.exists():
                try:
                    preview_data["source_text"] = src.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
            preview_data["topic"] = meta.get("topic", "")

            message = ""
            if status in ("awaiting_confirm",):
                # 旧格式只有项目路径，没有八项确认/生效页规划；不能安全地继续确认
                status = "error"
                message = "旧任务缺少方案确认数据（升级前未完整持久化），请重新生成设计方案"
            elif status in ("previewing", "executing", "running"):
                status = "error"
                message = "旧任务缺少续跑所需状态，已中断；请重新发起任务"
            elif status == "done":
                pass
            elif status == "error":
                message = meta.get("message", "") or "任务此前已失败"

            tasks[tid] = {
                "id": tid,
                "status": status,
                "progress": 100 if status == "done" else 0,
                "stage": "done" if status == "done" else status,
                "message": message or ("任务已恢复(重启后)" if status == "done" else ""),
                "result_path": meta.get("result_path"),
                "topic": meta.get("topic", ""),
                "project_path": str(d),
                "preview_payload": None,
                "preview_data": preview_data,
                "execution": None,
                "replan": None,
                "last_active": meta.get("last_active")
                or datetime.fromtimestamp(meta_path.stat().st_mtime).isoformat(timespec="seconds"),
                "created_at": meta.get("created_at"),
            }
            seen_dirs.add(resolved)
            _persist_task(tid, touch_last_active=False)
            restored += 1

    if restored:
        logger.info("从磁盘恢复了 %d 个任务", restored)
    return resume_jobs

# ── 过期任务清理（7天） ──
CLEANUP_DAYS = 7

def _touch_task_meta(project_dir: Path):
    """更新 task_meta.json 的 last_active 时间"""
    meta_path = project_dir / "task_meta.json"
    if not meta_path.exists():
        return
    try:
        with _state_lock:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            now_iso = datetime.now().isoformat(timespec="seconds")
            meta["last_active"] = now_iso
            _atomic_write_json(meta_path, meta)
            tid = meta.get("task_id")
            if tid:
                state_path = _state_path(tid)
                if state_path.exists():
                    try:
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        state["last_active"] = now_iso
                        _atomic_write_json(state_path, state)
                    except Exception:
                        pass
                if tid in tasks:
                    tasks[tid]["last_active"] = now_iso
    except json.JSONDecodeError:
        logger.warning("_touch_task_meta: %s JSON损坏，跳过", meta_path)
    except OSError as e:
        logger.warning("_touch_task_meta: 写入失败 %s: %s", meta_path, e)

def _is_task_project(d: Path) -> bool:
    """判断目录是否为PPT项目（避免误删非任务目录）"""
    if (d / "task_meta.json").exists():
        return True
    # 无meta但包含svg_output/或exports/，视为项目目录
    if (d / "svg_output").is_dir() or (d / "exports").is_dir():
        return True
    return False

def _parse_last_active(meta: dict, fallback_dir: Path) -> Optional[datetime]:
    """安全解析last_active时间"""
    last_str = meta.get("last_active", "")
    if last_str:
        try:
            return datetime.fromisoformat(last_str)
        except (ValueError, TypeError):
            pass
    # 兼容旧任务：用目录修改时间
    try:
        return datetime.fromtimestamp(fallback_dir.stat().st_mtime)
    except OSError:
        return None

def _cleanup_expired_tasks():
    """清理超过7天未活跃的任务及其文件"""
    now = datetime.now()
    cutoff = now - timedelta(days=CLEANUP_DAYS)
    removed = 0
    skipped_running = 0
    errors = 0
    if not PROJECTS_DIR.exists():
        return 0
    import shutil
    dirs = list(PROJECTS_DIR.iterdir())
    for d in dirs:
        if not d.is_dir() or d.name.startswith("."):
            continue
        if not _is_task_project(d):
            continue
        meta_path = d / "task_meta.json"
        if not meta_path.exists():
            # 无meta的项目目录，用目录修改时间判断
            try:
                mtime = datetime.fromtimestamp(d.stat().st_mtime)
                if mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
                    logger.info("清理无meta过期目录: %s (mtime=%s)", d.name, mtime.isoformat())
            except OSError:
                pass
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            task_id = meta.get("task_id", d.name)
            live = tasks.get(task_id)
            # 跳过任何仍在运行/生成中的任务；状态以内存为准，meta 可能滞后
            if live and live.get("status") in ("previewing", "executing", "running"):
                skipped_running += 1
                continue
            status = live.get("status") if live else meta.get("status", "")
            if status in ("running", "pending", "previewing", "executing"):
                skipped_running += 1
                continue
            last_active = _parse_last_active(meta, d)
            if last_active is None:
                logger.warning("清理: %s 无法解析时间，跳过", d.name)
                continue
            if last_active < cutoff:
                tasks.pop(task_id, None)
                _state_path(task_id).unlink(missing_ok=True)
                try:
                    shutil.rmtree(d)
                except OSError as e:
                    logger.error("清理失败（磁盘/权限?）: %s: %s", d.name, e)
                    errors += 1
                    continue
                removed += 1
                logger.info("清理过期任务: %s (last_active=%s)", d.name,
                            meta.get("last_active", "目录mtime"))
        except json.JSONDecodeError:
            logger.warning("清理: %s meta JSON损坏，跳过", d.name)
            errors += 1
        except Exception as e:
            logger.warning("清理检查失败 %s: %s", d.name, e)
            errors += 1

    # 清理没有对应项目的陈旧状态文件
    if STATE_DIR.is_dir():
        for state_file in STATE_DIR.glob("*.json"):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                task_id = state.get("id") or state_file.stem
                live = tasks.get(task_id)
                if live and live.get("status") in ("previewing", "executing", "running"):
                    skipped_running += 1
                    continue
                last_str = state.get("last_active", "")
                try:
                    last_active = datetime.fromisoformat(last_str) if last_str else None
                except (ValueError, TypeError):
                    last_active = None
                if last_active is None:
                    last_active = datetime.fromtimestamp(state_file.stat().st_mtime)
                if last_active < cutoff:
                    tasks.pop(task_id, None)
                    state_file.unlink(missing_ok=True)
                    removed += 1
                    logger.info("清理过期状态文件: %s", state_file.name)
            except Exception as e:
                logger.warning("清理状态文件失败 %s: %s", state_file.name, e)
                errors += 1

    # 清理崩溃时可能残留的原子写临时文件
    if STATE_DIR.is_dir():
        for tmp_file in STATE_DIR.glob("*.tmp"):
            try:
                if datetime.fromtimestamp(tmp_file.stat().st_mtime) < cutoff:
                    tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

    if removed or errors:
        logger.info("清理完成: 删除%d个, 跳过运行中%d个, 失败%d个", removed, skipped_running, errors)
    return removed

def _start_resume_jobs(jobs: list[tuple[str, str]]) -> None:
    """启动时调度自动续跑（预览/逐页生成）"""
    if not jobs:
        return
    cfg = ConfigManager.load()
    loop = asyncio.get_event_loop()
    for kind, task_id in jobs:
        task = tasks.get(task_id)
        if not task:
            continue
        if not cfg.is_configured():
            _mark_task_error(
                task_id,
                "服务重启后无法自动继续：LLM 尚未配置。请配置后重新发起任务。",
                progress=task.get("progress") or 0,
            )
            continue

        project_dir = _task_project_path(task) or _find_project_dir_for_task(task_id)
        if project_dir:
            task["project_path"] = str(project_dir)

        if kind == "preview":
            payload_dict = _resolve_preview_payload(task_id, task)
            if not payload_dict:
                _mark_task_error(task_id, "服务重启后无法恢复设计方案：缺少源内容")
                continue
            task["status"] = "previewing"
            task["stage"] = "restore"
            task["message"] = "服务重启，正在恢复设计方案..."
            task["progress"] = max(int(task.get("progress") or 0), 5)
            task["replan"] = None
            _persist_task(task_id)
            try:
                payload = PreviewPayload(**payload_dict)
            except Exception as e:
                _mark_task_error(task_id, f"服务重启后无法恢复设计方案：{e}")
                continue
            loop.create_task(
                _run_preview(
                    task_id, cfg, payload,
                    resume_project_path=str(project_dir) if project_dir else None,
                )
            )

        elif kind == "execute":
            pd = task.get("preview_data") or {}
            execution = task.get("execution") or {}
            if not project_dir:
                _mark_task_error(task_id, "服务重启后项目目录不存在，无法继续生成")
                continue
            if not execution and not (pd.get("spec_lock") and (pd.get("page_plan") or pd.get("replanned_pages"))):
                _mark_task_error(task_id, "服务重启后缺少生成所需的方案数据，无法继续生成")
                continue
            task["status"] = "executing"
            task["stage"] = "restore"
            task["message"] = "服务重启，正在继续生成未完成页面..."
            task["progress"] = max(int(task.get("progress") or 0), 25)
            _persist_task(task_id)
            payload = ConfirmPayload(
                task_id=task_id,
                confirmations=copy.deepcopy(execution.get("confirmations") or pd.get("eight_confirmations") or {}),
                spec_lock=execution.get("spec_lock"),
                enable_thinking=bool(execution.get("enable_thinking", False)),
            )
            loop.create_task(_run_execute(task_id, payload, resume=True))


@app.on_event("startup")
def _on_startup():
    resume_jobs = _restore_tasks()
    _cleanup_expired_tasks()
    logger.info("启动完成, 内存中 %d 个任务", len(tasks))
    _start_resume_jobs(resume_jobs)

    # 每小时执行一次清理（带异常保护，防止协程崩溃）
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(3600)
            try:
                _cleanup_expired_tasks()
            except Exception as e:
                logger.error("定时清理异常（不影响后续执行）: %s", e)
    asyncio.get_event_loop().create_task(_periodic_cleanup())


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
        "project_path": None,
        "preview_payload": payload.model_dump(),
        "preview_data": None,  # 待填充
        "execution": None,
        "replan": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "last_active": datetime.now().isoformat(timespec="seconds"),
    }
    _persist_task(task_id)

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
    if task.get("status") != "awaiting_confirm":
        raise HTTPException(400, "任务当前不在待确认状态，无法重新规划")
    pd = task.get("preview_data")
    if not pd:
        raise HTTPException(400, "无预览数据")

    running = task.get("replan") or {}
    if isinstance(running, dict) and running.get("status") == "running":
        raise HTTPException(409, "正在重新规划中，请稍候")

    original_plan = pd.get("page_plan") or []
    target_pages = max(1, min(50, int(payload.target_pages)))
    mode = "restore" if (original_plan and target_pages == len(original_plan)) else "plan"
    task["replan"] = {
        "status": "running",
        "mode": mode,
        "target": target_pages,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    _persist_task(payload.task_id)

    try:
        # 用户把页数改回原始规划时，直接清除 replanned_pages，
        # 保证确认生成时使用的页面与前端右侧大纲一致，且无需再调用 LLM。
        if mode == "restore":
            pd.pop("replanned_pages", None)
            cf = pd.get("eight_confirmations", {})
            pc = cf.get("page_count", {})
            pc["recommended"] = target_pages
            cf["page_count"] = pc
            pd["eight_confirmations"] = cf
            task["preview_data"] = pd
            task["replan"] = None
            _persist_task(payload.task_id)
            logger.info("页数改回原始规划 %d 页，已清除 replanned_pages", target_pages)
            return {"page_plan": original_plan, "restored": True}

        cfg = ConfigManager.load()
        if not cfg.is_configured():
            raise HTTPException(400, "请先配置 LLM API Key")
        engine = PPTEngine(cfg)
        loop = asyncio.get_event_loop()

        new_pages = await loop.run_in_executor(
            None,
            lambda: engine.replan_page_plan(
                source_text=pd.get("source_text", ""),
                topic=pd.get("topic", task.get("topic", "")),
                spec_lock=pd.get("spec_lock", ""),
                target_pages=target_pages,
                enable_thinking=payload.enable_thinking,
            )
        )

        new_pages = new_pages or []
        if not new_pages:
            raise RuntimeError("AI 未返回页面规划，请重试")

        pd["replanned_pages"] = new_pages  # 存到独立key，不覆盖原始page_plan
        # 同步更新 eight_confirmations 的 page_count
        cf = pd.get("eight_confirmations", {})
        pc = cf.get("page_count", {})
        pc["recommended"] = target_pages
        if pc.get("min", 0) > target_pages:
            pc["min"] = target_pages
        if pc.get("max", 0) < target_pages:
            pc["max"] = target_pages
        cf["page_count"] = pc
        pd["eight_confirmations"] = cf
        task["preview_data"] = pd
        task["replan"] = None
        _persist_task(payload.task_id)
        return {"page_plan": new_pages}

    except HTTPException as e:
        task["replan"] = {
            "status": "failed",
            "mode": mode,
            "target": target_pages,
            "error": e.detail if isinstance(e.detail, str) else "重新规划失败",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        _persist_task(payload.task_id)
        raise
    except Exception as e:
        logger.exception("重新规划任务 %s 失败", payload.task_id)
        task["replan"] = {
            "status": "failed",
            "mode": mode,
            "target": target_pages,
            "error": f"重新规划失败: {e}",
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        _persist_task(payload.task_id)
        raise HTTPException(500, f"重新规划失败: {e}")
    finally:
        current = task.get("replan")
        if isinstance(current, dict) and current.get("status") == "running":
            current.update({
                "status": "failed",
                "error": "重新规划被中断，请重试",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
            task["replan"] = current
            _persist_task(payload.task_id)


@app.post("/api/generate/confirm")
async def confirm_and_execute(payload: ConfirmPayload):
    """用户确认 Eight Confirmations → Steps 5-7: 生成SVG + 导出PPTX"""
    task = tasks.get(payload.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] != "awaiting_confirm":
        raise HTTPException(400, f"任务状态不对: {task['status']}，需要先完成设计预览")
    replan = task.get("replan") or {}
    if isinstance(replan, dict) and replan.get("status") == "running":
        raise HTTPException(409, "页面规划仍在生成中，请等待完成后再确认")

    task["status"] = "executing"
    task["stage"] = "step5"
    task["message"] = "正在生成PPT..."
    task["replan"] = None
    task["execution"] = {
        "confirmations": copy.deepcopy(payload.confirmations),
        "requested_spec_lock": payload.spec_lock,
        "enable_thinking": payload.enable_thinking,
        "prepared": False,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    _persist_task(payload.task_id)

    asyncio.create_task(_run_execute(payload.task_id, payload))
    return {"task_id": payload.task_id}


def _task_public(task_id: str, task: dict) -> dict:
    """任务轻量投影：轮询用，不含完整 preview_data / 服务器绝对路径"""
    pd = task.get("preview_data") or {}
    replan = task.get("replan")
    return {
        "id": task_id,
        "status": task.get("status", ""),
        "progress": task.get("progress", 0),
        "stage": task.get("stage", ""),
        "message": task.get("message", ""),
        "topic": task.get("topic", ""),
        "created_at": task.get("created_at"),
        "last_active": task.get("last_active"),
        "replan": copy.deepcopy(replan) if isinstance(replan, dict) else None,
        "has_preview": bool(pd),
    }


@app.get("/api/tasks/recent")
def recent_tasks(limit: int = 8):
    """最近的服务器任务，供页面换浏览器/清缓存后继续"""
    limit = max(1, min(20, int(limit)))
    items = [_task_public(tid, t) for tid, t in tasks.items() if t]
    items.sort(key=lambda t: (t.get("last_active") or t.get("created_at") or ""), reverse=True)
    return {"tasks": items[:limit]}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return _task_public(task_id, task)


@app.get("/api/tasks/{task_id}/preview")
def get_task_preview(task_id: str):
    """返回确认/恢复页面需要的方案数据"""
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    pd = task.get("preview_data") or {}
    if not pd:
        raise HTTPException(404, "该任务没有可恢复的方案数据")

    project_dir = _task_project_path(task)
    source_text = pd.get("source_text") or ""
    if not source_text and project_dir:
        src = project_dir / "sources" / "source.md"
        if src.exists():
            try:
                source_text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source_text = ""

    return {
        "preview_data": {
            "project_path": str(project_dir) if project_dir else pd.get("project_path"),
            "eight_confirmations": pd.get("eight_confirmations") or {},
            "page_plan": pd.get("page_plan") or [],
            "replanned_pages": pd.get("replanned_pages") or None,
            "source_text": source_text,
            "topic": pd.get("topic") or task.get("topic", ""),
        }
    }


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

    # 清空已保存的标注 & 续期
    _annotation_store.pop(task_id, None)
    if modified:
        _touch_task_meta(project_dir)
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

    # 续期：批注操作视为活跃，重置7天倒计时
    try:
        project_dir = _get_project_dir(task_id)
        _touch_task_meta(project_dir)
    except Exception:
        pass

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
        task = tasks.get(task_id)
        if task:
            task["result_path"] = str(all_pptx[0])
            _persist_task(task_id)

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
        task = tasks.get(task_id)
        if task:
            task["result_path"] = str(all_pptx[0])
            _persist_task(task_id)

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


async def _run_preview(task_id: str, cfg: LLMConfig, payload: PreviewPayload, resume_project_path: Optional[str] = None):
    """Steps 1-4: 生成设计方案"""
    task = tasks.get(task_id)
    if not task:
        return
    task["status"] = "previewing"
    task.setdefault("message", "正在生成设计方案...")
    _persist_task(task_id)

    try:
        engine = PPTEngine(cfg)

        loop = asyncio.get_event_loop()

        def on_progress(stage, msg, pct):
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = pct
            # 项目目录创建后立刻关联 task_id；确认源文已落盘后再移除状态里的大原文
            if stage == "step2":
                pp = task.get("preview_payload")
                if isinstance(pp, dict) and pp.get("source_text"):
                    found = _find_project_dir_for_task(task_id, hint=resume_project_path)
                    if found:
                        task["project_path"] = str(found)
                        src_file = found / "sources" / "source.md"
                        if src_file.exists() and src_file.stat().st_size > 0:
                            pp.pop("source_text", None)
                            task["preview_payload"] = pp
            _persist_task(task_id, throttle_seconds=8)
            _notify_ws(task_id, {"stage": stage, "message": msg, "progress": pct}, loop=loop)

        preview_data = await loop.run_in_executor(
            None,
            lambda: engine.preview(
                source_text=payload.source_text,
                topic=payload.topic,
                fmt=payload.format,
                enable_thinking=payload.enable_thinking,
                progress=on_progress,
                task_tag=task_id,
                resume_project_path=resume_project_path,
            )
        )

        task["project_path"] = preview_data.get("project_path")
        pp = task.get("preview_payload")
        if isinstance(pp, dict):
            pp.pop("source_text", None)
            task["preview_payload"] = pp
        task["status"] = "awaiting_confirm"
        task["stage"] = "awaiting_confirm"
        task["message"] = "设计方案已生成，请确认"
        task["progress"] = 25
        task["preview_data"] = preview_data
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "awaiting_confirm", "message": "设计方案已生成，请确认", "progress": 25, "status": "awaiting_confirm"}, loop=loop)

    except Exception as e:
        logger.exception("Preview 任务 %s 失败", task_id)
        task["status"] = "error"
        task["stage"] = "error"
        task["message"] = f"设计方案生成失败: {e}"
        task["progress"] = 0
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": 0, "status": "error"}, loop=asyncio.get_event_loop())


async def _run_execute(task_id: str, payload: Optional[ConfirmPayload] = None, resume: bool = False):
    """Steps 5-7: 执行生成（resume=True 时跳过已完成的页面）"""
    task = tasks.get(task_id)
    if not task:
        return
    preview_data = task.get("preview_data") or {}
    execution = task.get("execution") or {}

    try:
        cfg = ConfigManager.load()
        if not cfg.is_configured():
            raise RuntimeError("LLM 尚未配置，无法继续生成")
        engine = PPTEngine(cfg)

        loop = asyncio.get_event_loop()

        def on_progress(stage, msg, pct):
            # 映射到全局进度 (25-100)；续跑时进度不回退
            global_pct = 25 + int(pct * 0.75)
            if resume:
                global_pct = max(global_pct, int(task.get("progress") or 0))
            task["stage"] = stage
            task["message"] = msg
            task["progress"] = global_pct
            _persist_task(task_id, throttle_seconds=8)
            _notify_ws(task_id, {"stage": stage, "message": msg, "progress": global_pct}, loop=loop)

        # ── 定位项目目录（兼容状态文件迁移后绝对路径失效） ──
        project_path = preview_data.get("project_path") or execution.get("project_path") or task.get("project_path")
        project_dir = Path(project_path) if project_path else None
        if not project_dir or not project_dir.is_dir():
            project_dir = _find_project_dir_for_task(
                task_id, hint=str(project_dir) if project_dir else None
            )
        if not project_dir:
            raise RuntimeError("项目目录不存在，无法继续生成")
        project_path = str(project_dir)
        task["project_path"] = project_path
        preview_data["project_path"] = project_path

        # ── 已 prepare 的计划（重启续跑）直接复用 ──
        if execution.get("prepared") and (execution.get("spec_lock") or preview_data.get("spec_lock")):
            spec_lock = execution.get("spec_lock") or preview_data.get("spec_lock", "")
            user_page_plan = (
                execution.get("page_plan")
                or preview_data.get("replanned_pages")
                or preview_data.get("page_plan")
                or []
            )
            confirmations = execution.get("confirmations") or preview_data.get("eight_confirmations") or {}
            enable_thinking = bool(execution.get("enable_thinking", False))
            logger.info("复用已准备的生成计划: %d 页 (resume=%s)", len(user_page_plan), resume)
        else:
            # ── 首次 / 重启发生在 prepare 之前：完成参数整理 ──
            if payload is not None:
                confirmations = copy.deepcopy(payload.confirmations or {})
                enable_thinking = bool(payload.enable_thinking)
                spec_lock = payload.spec_lock or preview_data.get("spec_lock", "")
            else:
                confirmations = copy.deepcopy(
                    execution.get("confirmations")
                    or preview_data.get("eight_confirmations")
                    or {}
                )
                enable_thinking = bool(execution.get("enable_thinking", False))
                spec_lock = (
                    execution.get("requested_spec_lock")
                    or execution.get("spec_lock")
                    or preview_data.get("spec_lock", "")
                )

            # 检查用户是否修改了 Eight Confirmations（排除 page_count，它已通过 replan 处理）
            original_cf = preview_data.get("eight_confirmations", {}) or {}
            cf_without_pages = copy.deepcopy(confirmations)
            orig_without_pages = copy.deepcopy(original_cf)
            cf_without_pages.pop("page_count", None)
            orig_without_pages.pop("page_count", None)
            need_regenerate = (cf_without_pages != orig_without_pages)
            regen_result = None

            if need_regenerate:
                logger.info("用户修改了 Eight Confirmations, 重新生成 spec_lock")
                regen_result = await loop.run_in_executor(
                    None,
                    lambda: engine.confirm_and_regenerate(
                        project_path_str=project_path,
                        user_modifications=confirmations,
                        enable_thinking=enable_thinking,
                        progress=on_progress,
                    )
                )
                spec_lock = regen_result["spec_lock"]

            # 优先用 replan 结果 > regenerate 结果 > preview 原始
            replanned_page_plan = preview_data.get("replanned_pages")
            if replanned_page_plan:
                user_page_plan = replanned_page_plan
                logger.info("使用用户 replan 后的 page_plan (%d 页)", len(replanned_page_plan))
            elif need_regenerate and regen_result and regen_result.get("page_plan"):
                user_page_plan = regen_result["page_plan"]
                preview_data["page_plan"] = user_page_plan
                logger.info("使用 regenerate 的 page_plan (%d 页)", len(user_page_plan or []))
            else:
                user_page_plan = preview_data.get("page_plan") or []
                logger.info("使用 preview 原始 page_plan (%d 页)", len(user_page_plan or []))

            task["execution"] = {
                "confirmations": copy.deepcopy(confirmations),
                "spec_lock": spec_lock,
                "page_plan": copy.deepcopy(user_page_plan),
                "enable_thinking": enable_thinking,
                "project_path": project_path,
                "prepared": True,
                "prepared_at": datetime.now().isoformat(timespec="seconds"),
            }
            execution = task["execution"]
            _persist_task(task_id)

        # 执行阶段不再需要原文；从内存状态移除，避免状态文件/轮询携带大文本
        if isinstance(preview_data, dict):
            preview_data.pop("source_text", None)
        task["preview_data"] = preview_data
        task["status"] = "executing"
        task["stage"] = "step5"
        task["message"] = "正在继续生成未完成页面..." if resume else "正在生成PPT..."
        _persist_task(task_id)

        result_path = await loop.run_in_executor(
            None,
            lambda: engine.execute(
                project_path_str=project_path,
                spec_lock=spec_lock,
                page_plan=user_page_plan,
                enable_thinking=enable_thinking,
                progress=on_progress,
                resume=resume,
            )
        )

        task["status"] = "done"
        task["stage"] = "done"
        task["progress"] = 100
        task["message"] = "PPT生成完成!"
        task["result_path"] = str(result_path)
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "done", "message": "PPT生成完成!", "progress": 100, "status": "done"}, loop=loop)

    except Exception as e:
        logger.exception("Execute 任务 %s 失败", task_id)
        task["status"] = "error"
        task["stage"] = "error"
        task["message"] = f"生成失败: {e}"
        task["progress"] = int(task.get("progress") or 0)
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": task["progress"], "status": "error"}, loop=asyncio.get_event_loop())


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
        task["stage"] = "done"
        task["progress"] = 100
        task["message"] = "PPT生成完成!"
        task["result_path"] = str(result_path)
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "done", "message": "PPT生成完成!", "progress": 100, "status": "done"}, loop=loop)

    except Exception as e:
        logger.exception("任务 %s 失败", task_id)
        task["status"] = "error"
        task["stage"] = "error"
        task["message"] = f"生成失败: {e}"
        task["progress"] = 0
        _persist_task(task_id)
        _notify_ws(task_id, {"stage": "error", "message": task["message"], "progress": 0, "status": "error"}, loop=asyncio.get_event_loop())
