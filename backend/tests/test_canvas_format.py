"""画布格式（4:3/16:9）解析与强制校正的回归测试。

背景：执行阶段曾用 `"ppt43" in spec_lock` 判断画布，弱模型照抄模板写出的
"PPT 16:9 (1280×720)" 导致 4:3 项目每页 SVG 仍是 16:9。
"""
from pathlib import Path

import pytest

import app.ppt_engine as engine_mod
from app.config import LLMConfig
from app.ppt_engine import PPTEngine

SPEC_LOCK_169 = """# Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## page_plan
- page 1
"""


@pytest.fixture()
def engine():
    return PPTEngine(LLMConfig(api_key="test"))


def test_resolve_from_explicit_and_text():
    assert PPTEngine.resolve_canvas_format("ppt43") == "ppt43"
    # 用户确认页/模型输出的显示字符串也要能识别
    assert PPTEngine.resolve_canvas_format("PPT 4:3 (960×720)") == "ppt43"
    assert PPTEngine.resolve_canvas_format("PPT 16:9 (1280×720)") == "ppt169"
    assert PPTEngine.resolve_canvas_format(None, spec_lock="viewBox: 0 0 1280 720") == "ppt169"
    assert PPTEngine.resolve_canvas_format(None) == "ppt169"


def test_resolve_from_project_dir_wins_over_spec_lock(tmp_path):
    # 项目是 4:3，但 spec_lock 被模型写成了 16:9 —— 必须以项目为准
    project = tmp_path / "demo_t1_ppt43_20261120"
    project.mkdir()
    assert PPTEngine.resolve_canvas_format(None, project_path=str(project), spec_lock=SPEC_LOCK_169) == "ppt43"


def test_force_canvas_in_markdown_fixes_43():
    md = PPTEngine._force_canvas_in_markdown(SPEC_LOCK_169, "ppt43")
    assert "0 0 1024 768" in md
    assert "format: PPT 4:3" in md
    assert "0 0 1280 720" not in md
    assert "PPT 16:9" not in md


def test_force_canvas_in_markdown_idempotent_for_169():
    md = PPTEngine._force_canvas_in_markdown(SPEC_LOCK_169, "ppt169")
    assert md == SPEC_LOCK_169


def test_force_canvas_fixes_design_spec_table():
    design_spec = (
        "## II. Canvas Specification\n"
        "| **Canvas Format** | PPT 16:9 (1280×720) |\n"
        "| **Dimensions** | 1280×720 |\n"
        "| **viewBox** | `0 0 1280 720` |\n"
    )
    md = PPTEngine._force_canvas_in_markdown(design_spec, "ppt43")
    assert "| **Canvas Format** | PPT 4:3 (1024×768) |" in md
    assert "| **Dimensions** | 1024×768 |" in md
    assert "| **viewBox** | `0 0 1024 768` |" in md


def test_enforce_canvas_fixes_wrong_viewbox_only(engine):
    # 内容按 1024×768 画，但根 viewBox 照抄了 1280×720 —— 只纠正根属性
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect x="0" y="0" width="1024" height="768" fill="#fff"/>'
        "</svg>"
    )
    fixed = engine._enforce_canvas(svg, 1024, 768, page_no=1)
    assert 'viewBox="0 0 1024 768"' in fixed
    assert 'width="1024"' in fixed and 'height="768"' in fixed
    assert "<g transform=" not in fixed


def test_enforce_canvas_scales_mismatched_layout(engine):
    # 整页按 16:9 设计 —— 等比缩放居中到 4:3，不能裁掉内容
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect x="0" y="0" width="1280" height="720" fill="#123456"/>'
        '<text x="600" y="300">标题</text>'
        "</svg>"
    )
    fixed = engine._enforce_canvas(svg, 1024, 768, page_no=2)
    assert 'viewBox="0 0 1024 768"' in fixed
    assert "<g transform=" in fixed
    assert "scale(" in fixed
    assert "标题" in fixed


def test_enforce_canvas_adds_missing_viewbox(engine):
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1024" height="768"/></svg>'
    fixed = engine._enforce_canvas(svg, 1024, 768, page_no=3)
    assert 'viewBox="0 0 1024 768"' in fixed


def test_enforce_canvas_keeps_correct_svg(engine):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="768" viewBox="0 0 1024 768">'
        '<rect width="1024" height="768"/></svg>'
    )
    assert engine._enforce_canvas(svg, 1024, 768, page_no=4) == svg


def test_enforce_canvas_resume_mode_leaves_correct_viewbox_alone(engine):
    # 续跑/用户编辑页：viewBox 已正确但缺 width/height 时不再补属性
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 768"><rect width="1024" height="768"/></svg>'
    assert engine._enforce_canvas(svg, 1024, 768, page_no=5, add_if_missing=False) == svg
    # 新生成页仍会补齐 width/height
    fixed = engine._enforce_canvas(svg, 1024, 768, page_no=5)
    assert 'width="1024"' in fixed and 'height="768"' in fixed


def test_execute_writes_43_svg_in_43_project(tmp_path, monkeypatch):
    """端到端：4:3 项目 + 模型照抄 16:9 模板 → 落盘 SVG 必须是 4:3。"""
    project = tmp_path / "demo_t2_ppt43_20261120"
    for sub in ("sources", "svg_output", "exports", "notes"):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / "sources" / "source.md").write_text("hello source", encoding="utf-8")

    eng = PPTEngine(LLMConfig(api_key="test"))

    def fake_generate(spec_lock, page_info, index, total, source_text, fmt=None, enable_thinking=False):
        # 模拟弱模型：无视 4:3 要求，输出 16:9 模板
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="0" y="0" width="1280" height="720" fill="#0f172a"/>'
            "</svg>"
        )

    eng._generate_svg_page = fake_generate
    eng._generate_notes = lambda *args, **kwargs: "notes"
    (project / "exports" / "out.pptx").write_text("fake pptx", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "_run_script", lambda *args, **kwargs: "")

    eng.execute(str(project), SPEC_LOCK_169, [{"page_num": 1, "title": "封面", "layout_hint": "cover"}])

    written = (project / "svg_output" / "01_slide_01.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 1024 768"' in written
    assert "<g transform=" in written  # 16:9 内容被等比缩放进 4:3 画布


def test_execute_passes_fmt_to_page_generator(tmp_path, monkeypatch):
    """execute 解析到的画布格式必须传给 _generate_svg_page。"""
    project = tmp_path / "demo_t3_ppt43_20261120"
    for sub in ("sources", "svg_output", "exports", "notes"):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / "sources" / "source.md").write_text("hello source", encoding="utf-8")

    eng = PPTEngine(LLMConfig(api_key="test"))
    received = []

    def fake_generate(spec_lock, page_info, index, total, source_text, fmt=None, enable_thinking=False):
        received.append(fmt)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 768">'
            '<rect x="0" y="0" width="1024" height="768" fill="#0f172a"/>'
            "</svg>"
        )

    eng._generate_svg_page = fake_generate
    eng._generate_notes = lambda *args, **kwargs: "notes"
    (project / "exports" / "out.pptx").write_text("fake pptx", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "_run_script", lambda *args, **kwargs: "")

    eng.execute(str(project), SPEC_LOCK_169, [{"page_num": 1, "title": "封面", "layout_hint": "cover"}])
    assert received == ["ppt43"]

    written = (project / "svg_output" / "01_slide_01.svg").read_text(encoding="utf-8")
    assert 'viewBox="0 0 1024 768"' in written


def test_execute_16_9_project_keeps_1280_720(tmp_path, monkeypatch):
    """回归保护：16:9 项目的画布必须是 1280×720，与修复前完全一致。"""
    assert engine_mod._canvas_info("ppt169") == (1280, 720, "PPT 16:9", "16:9")

    project = tmp_path / "demo_t4_ppt169_20261120"
    for sub in ("sources", "svg_output", "exports", "notes"):
        (project / sub).mkdir(parents=True, exist_ok=True)
    (project / "sources" / "source.md").write_text("hello source", encoding="utf-8")

    eng = PPTEngine(LLMConfig(api_key="test"))
    received = []

    def fake_generate(spec_lock, page_info, index, total, source_text, fmt=None, enable_thinking=False):
        received.append(fmt)
        # 模型正常输出 16:9 页面（与修复前行为相同）
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="0" y="0" width="1280" height="720" fill="#0f172a"/>'
            "</svg>"
        )

    eng._generate_svg_page = fake_generate
    eng._generate_notes = lambda *args, **kwargs: "notes"
    (project / "exports" / "out.pptx").write_text("fake pptx", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "_run_script", lambda *args, **kwargs: "")

    eng.execute(str(project), SPEC_LOCK_169, [{"page_num": 1, "title": "封面", "layout_hint": "cover"}])
    assert received == ["ppt169"]

    written = (project / "svg_output" / "01_slide_01.svg").read_text(encoding="utf-8")
    # viewBox 仍是 1280×720，且没有被缩放包装
    assert 'viewBox="0 0 1280 720"' in written
    assert "<g transform=" not in written
    assert 'width="1280"' in written and 'height="720"' in written
