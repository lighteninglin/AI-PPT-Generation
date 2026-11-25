from pathlib import Path

import pytest

import app.ppt_engine as engine_mod
from app.config import LLMConfig
from app.ppt_engine import PPTEngine


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    project = tmp_path / "demo_task123_ppt169_20261120"
    for sub in ("sources", "svg_output", "exports", "notes"):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / "sources" / "source.md").write_text("hello source", encoding="utf-8")

    eng = PPTEngine(LLMConfig(api_key="test"))
    calls = []

    def fake_generate(spec_lock, page_info, index, total, source_text, fmt=None, enable_thinking=False):
        calls.append(page_info.get("filename"))
        return '<svg xmlns="http://www.w3.org/2000/svg">' + ("z" * 300) + "</svg>"

    eng._generate_svg_page = fake_generate
    eng._generate_notes = lambda *args, **kwargs: "notes content"

    # 不执行真实脚本；预先放一个 pptx，让导出发现阶段有结果
    (project / "exports" / "out.pptx").write_text("fake pptx", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "_run_script", lambda *args, **kwargs: "")
    return project, eng, calls


def test_execute_resume_skips_existing_valid_pages(engine):
    project, eng, calls = engine
    pages = [
        {"page_num": 1, "title": "封面", "filename": "cover.svg", "layout_hint": "cover", "key_points": "a"},
        {"page_num": 2, "title": "内容", "filename": "body.svg", "layout_hint": "content", "key_points": "b"},
    ]
    existing = project / "svg_output" / "01_cover.svg"
    existing.write_text('<svg xmlns="http://www.w3.org/2000/svg">' + ("y" * 300) + "</svg>", encoding="utf-8")

    result = eng.execute(
        str(project), "# spec", pages, resume=True
    )
    assert result.name == "out.pptx"
    assert calls == ["body.svg"]  # 只生成缺失页
    assert existing.read_text(encoding="utf-8").startswith('<svg xmlns="http://www.w3.org/2000/svg">')


def test_execute_resume_regenerates_truncated_page(engine):
    project, eng, calls = engine
    pages = [
        {"page_num": 1, "title": "封面", "filename": "cover.svg", "layout_hint": "cover", "key_points": "a"},
        {"page_num": 2, "title": "内容", "filename": "body.svg", "layout_hint": "content", "key_points": "b"},
    ]
    truncated = project / "svg_output" / "01_cover.svg"
    truncated.write_text("<svg><rect", encoding="utf-8")

    eng.execute(str(project), "# spec", pages, resume=True)
    assert calls == ["cover.svg", "body.svg"]


def test_resolve_page_filename_is_stable():
    page = {"page_num": 3, "filename": "intro.svg", "layout_hint": "content"}
    assert PPTEngine._resolve_page_filename(page, 2) == "03_intro.svg"
    page2 = {"page_num": 3, "filename": "03_intro.svg"}
    assert PPTEngine._resolve_page_filename(page2, 2) == "03_intro.svg"
    page3 = {"page_num": 3, "filename": 'bad{"name".svg'}
    assert PPTEngine._resolve_page_filename(page3, 2) == "03_slide_03.svg"
