import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import LLMConfig
from app.main import ConfirmPayload, PreviewPayload, ReplanPayload


class FakeEngine:
    """不调用 LLM 的测试引擎，模拟 preview/execute/replan"""

    generated_pages = []
    last_resume = None
    fail_replan = False

    def __init__(self, cfg):
        self.cfg = cfg

    @classmethod
    def reset(cls):
        cls.generated_pages = []
        cls.last_resume = None
        cls.fail_replan = False

    def preview(self, source_text, topic, fmt="ppt169", enable_thinking=False,
                progress=None, task_tag=None, resume_project_path=None):
        if resume_project_path and Path(resume_project_path).is_dir():
            project = Path(resume_project_path)
        else:
            project = main.PROJECTS_DIR / f"{topic}_{task_tag}_ppt169_20261120"
            for sub in ("sources", "svg_output", "exports", "notes"):
                (project / sub).mkdir(parents=True, exist_ok=True)
        src = project / "sources" / "source.md"
        if not src.exists():
            src.write_text(source_text, encoding="utf-8")

        pages = [
            {"page_num": 1, "title": "封面", "filename": "cover.svg", "layout_hint": "cover", "key_points": "a"},
            {"page_num": 2, "title": "内容", "filename": "body.svg", "layout_hint": "content", "key_points": "b"},
        ]
        cf = {
            "canvas_format": "PPT 16:9 (1280×720)",
            "page_count": {"min": 2, "max": 20, "recommended": 2},
            "target_audience": "内部团队",
            "style_objective": "B) General Consulting — flat (扁平化)",
            "color_scheme": {"primary": "#111111", "secondary": "#222222", "accent": "#333333",
                             "background": "#ffffff", "text": "#000000"},
            "icon_usage": "C) Built-in icon library",
            "typography": {"heading_font": "Arial", "heading_size": "32-40px",
                           "body_font": "Arial", "body_size": "18-22px"},
            "image_usage": "A) No images",
        }
        if progress:
            progress("step4", "设计方案完成", 25)
        return {
            "project_path": str(project),
            "project_name": project.name,
            "eight_confirmations": cf,
            "design_spec": "# design",
            "spec_lock": "# spec",
            "page_plan": pages,
            "source_text": source_text,
            "topic": topic,
        }

    def replan_page_plan(self, source_text, topic, spec_lock, target_pages, enable_thinking=False):
        if self.fail_replan:
            raise RuntimeError("fake replan failure")
        return [
            {"page_num": i + 1, "title": f"新页 {i + 1}", "filename": f"p{i + 1}.svg",
             "layout_hint": "content", "key_points": "x"}
            for i in range(target_pages)
        ]

    def confirm_and_regenerate(self, project_path_str, user_modifications, enable_thinking=False, progress=None, fmt=None):
        pages = [{"page_num": 1, "title": "重生成", "filename": "regen.svg",
                  "layout_hint": "cover", "key_points": "x"}]
        return {"spec_lock": "# spec regen", "page_plan": pages, "format": fmt or "ppt169"}

    def execute(self, project_path_str, spec_lock, page_plan=None, enable_thinking=False,
                progress=None, resume=False, fmt=None):
        project = Path(project_path_str)
        pages = page_plan or []
        FakeEngine.last_resume = resume
        skipped = 0
        for i, page in enumerate(pages):
            fname = page.get("filename", f"slide_{i + 1}.svg")
            target = project / "svg_output" / fname
            if resume and target.exists() and target.stat().st_size > 200:
                skipped += 1
                continue
            target.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">' + ("x" * 300) + "</svg>",
                encoding="utf-8",
            )
            FakeEngine.generated_pages.append(fname)
        notes = project / "notes" / "total.md"
        if not notes.exists():
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text("notes", encoding="utf-8")
        out = project / "exports" / "out.pptx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("fake pptx", encoding="utf-8")
        if progress:
            progress("done", "完成", 100)
        return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(main, "STATE_DIR", tmp_path / ".task_states")
    monkeypatch.setattr(main, "PPTEngine", FakeEngine)
    monkeypatch.setattr(
        main.ConfigManager,
        "load",
        staticmethod(lambda: LLMConfig(api_key="test-key")),
    )
    main.tasks.clear()
    FakeEngine.reset()
    yield tmp_path
    main.tasks.clear()


def _project(tmp_path, task_id):
    project = tmp_path / f"demo_{task_id}_ppt169_20261120"
    for sub in ("sources", "svg_output", "exports", "notes"):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / "sources" / "source.md").write_text("hello source", encoding="utf-8")
    return project


def _base_task(task_id, project, status="awaiting_confirm", preview=True):
    pages = [
        {"page_num": 1, "title": "封面", "filename": "cover.svg", "layout_hint": "cover", "key_points": "a"},
        {"page_num": 2, "title": "内容", "filename": "body.svg", "layout_hint": "content", "key_points": "b"},
    ]
    task = {
        "id": task_id,
        "status": status,
        "progress": 25 if status == "awaiting_confirm" else 50,
        "stage": status,
        "message": "test",
        "topic": "demo",
        "result_path": None,
        "project_path": str(project),
        "preview_payload": {
            "source_text": "hello source", "topic": "demo",
            "source_type": "text", "format": "ppt169", "enable_thinking": False,
        },
        "preview_data": None,
        "execution": None,
        "replan": None,
        "created_at": "2026-11-20T14:00:00",
        "last_active": "2026-11-20T14:00:00",
    }
    if preview:
        task["preview_data"] = {
            "project_path": str(project),
            "eight_confirmations": {
                "canvas_format": "PPT 16:9 (1280×720)",
                "page_count": {"recommended": 2},
                "target_audience": "内部团队",
            },
            "page_plan": pages,
            "spec_lock": "# spec",
            "design_spec": "# design",
            "source_text": "hello source",
            "topic": "demo",
        }
    return task, pages


def test_state_roundtrip_awaiting_confirm(env):
    tmp_path = env
    project = _project(tmp_path, "await0000001")
    task, _ = _base_task("await0000001", project)
    main.tasks[task["id"]] = task
    main._persist_task(task["id"])
    main.tasks.clear()

    jobs = main._restore_tasks()
    assert jobs == []
    restored = main.tasks["await0000001"]
    assert restored["status"] == "awaiting_confirm"
    assert len(restored["preview_data"]["page_plan"]) == 2
    assert restored["preview_data"]["eight_confirmations"]["target_audience"] == "内部团队"

    public = main._task_public("await0000001", restored)
    assert "preview_data" not in public
    assert "project_path" not in public
    assert "result_path" not in public
    assert public["has_preview"] is True

    preview = main.get_task_preview("await0000001")["preview_data"]
    assert preview["source_text"] == "hello source"
    assert len(preview["page_plan"]) == 2
    assert [t["id"] for t in main.recent_tasks()["tasks"]] == ["await0000001"]


def test_execute_resume_skips_existing_pages(env):
    tmp_path = env
    task_id = "exec00000001"
    project = _project(tmp_path, task_id)
    task, pages = _base_task(task_id, project, status="executing")
    (project / "svg_output" / "cover.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">' + ("y" * 300) + "</svg>",
        encoding="utf-8",
    )
    task["execution"] = {
        "confirmations": {"canvas_format": "PPT 16:9"},
        "spec_lock": "# final spec",
        "page_plan": pages,
        "enable_thinking": False,
        "prepared": True,
        "project_path": str(project),
    }
    main.tasks[task_id] = task
    main._persist_task(task_id)
    main.tasks.clear()

    jobs = main._restore_tasks()
    assert jobs == [("execute", task_id)]
    asyncio.run(main._run_execute(task_id, None, resume=True))

    assert main.tasks[task_id]["status"] == "done"
    assert main.tasks[task_id]["result_path"].endswith("out.pptx")
    assert FakeEngine.last_resume is True
    assert FakeEngine.generated_pages == ["body.svg"]
    # 已有页面没有被覆盖
    existing = (project / "svg_output" / "cover.svg").read_text(encoding="utf-8")
    assert existing.startswith('<svg xmlns="http://www.w3.org/2000/svg">')


def test_preview_resume_from_existing_project(env):
    tmp_path = env
    task_id = "prev00000001"
    project = _project(tmp_path, task_id)
    task, _ = _base_task(task_id, project, status="previewing", preview=False)
    main.tasks[task_id] = task
    main._persist_task(task_id)
    main.tasks.clear()

    jobs = main._restore_tasks()
    assert jobs == [("preview", task_id)]
    payload = main._resolve_preview_payload(task_id, main.tasks[task_id])
    assert payload["source_text"] == "hello source"
    asyncio.run(main._run_preview(
        task_id,
        LLMConfig(api_key="test-key"),
        PreviewPayload(**payload),
        resume_project_path=str(project),
    ))
    assert main.tasks[task_id]["status"] == "awaiting_confirm"
    assert main.tasks[task_id]["preview_data"]["project_path"] == str(project)


def test_replan_success_and_persistence(env):
    tmp_path = env
    task_id = "replan000001"
    project = _project(tmp_path, task_id)
    task, _ = _base_task(task_id, project)
    main.tasks[task_id] = task
    main._persist_task(task_id)

    result = asyncio.run(main.replan_pages(
        ReplanPayload(task_id=task_id, target_pages=6, enable_thinking=False)
    ))
    assert len(result["page_plan"]) == 6
    assert len(main.tasks[task_id]["preview_data"]["replanned_pages"]) == 6
    assert main.tasks[task_id]["replan"] is None

    main.tasks.clear()
    main._restore_tasks()
    restored = main.tasks[task_id]
    assert len(restored["preview_data"]["replanned_pages"]) == 6


def test_replan_failure_is_persisted(env):
    tmp_path = env
    task_id = "replanfail01"
    project = _project(tmp_path, task_id)
    task, _ = _base_task(task_id, project)
    main.tasks[task_id] = task
    main._persist_task(task_id)
    FakeEngine.fail_replan = True

    with pytest.raises(Exception):
        asyncio.run(main.replan_pages(
            ReplanPayload(task_id=task_id, target_pages=6, enable_thinking=False)
        ))

    replan = main.tasks[task_id]["replan"]
    assert replan["status"] == "failed"
    assert "fake replan failure" in replan["error"]

    main.tasks.clear()
    main._restore_tasks()
    assert main.tasks[task_id]["replan"]["status"] == "failed"
    assert main.tasks[task_id]["status"] == "awaiting_confirm"


def test_running_replan_becomes_failed_after_restart(env):
    tmp_path = env
    task_id = "replancrash1"
    project = _project(tmp_path, task_id)
    task, _ = _base_task(task_id, project)
    task["replan"] = {"status": "running", "mode": "plan", "target": 6,
                      "started_at": "2026-11-20T14:00:00"}
    main.tasks[task_id] = task
    main._persist_task(task_id)
    main.tasks.clear()

    main._restore_tasks()
    restored = main.tasks[task_id]
    assert restored["status"] == "awaiting_confirm"
    assert restored["replan"]["status"] == "failed"
    assert "服务重启" in restored["replan"]["error"]


def test_task_api_routing_and_projection(env):
    tmp_path = env
    task_id = "api000000001"
    project = _project(tmp_path, task_id)
    task, _ = _base_task(task_id, project)
    main.tasks[task_id] = task

    with TestClient(main.app) as client:
        # TestClient startup 会触发 _restore_tasks，但内存已有任务，不应重复
        assert client.get("/api/tasks/recent").json()["tasks"][0]["id"] == task_id
        public = client.get(f"/api/tasks/{task_id}").json()
        assert "preview_data" not in public
        assert public["has_preview"] is True
        preview = client.get(f"/api/tasks/{task_id}/preview").json()["preview_data"]
        assert preview["source_text"] == "hello source"
        assert len(preview["page_plan"]) == 2
