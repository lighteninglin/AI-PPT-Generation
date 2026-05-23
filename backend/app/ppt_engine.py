"""
PPT生成引擎 — 直接复用 ppt-master 原项目脚本(通过subprocess调用)
LLM 仅负责 Strategist(设计规划) 和 Executor(SVG生成) 两个环节
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional

from .config import LLMConfig
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

# ppt-master 技能目录
SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "ppt-master"
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROJECTS_DIR = Path(__file__).resolve().parents[2] / "projects"


def _run_script(args: list[str], cwd: Optional[str] = None, timeout: int = 120) -> str:
    """运行 ppt-master 脚本，返回 stdout"""
    logger.info("运行脚本: %s", " ".join(args))
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        logger.error("脚本失败 (rc=%d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"脚本执行失败: {result.stderr[:500]}")
    return result.stdout


class PPTEngine:
    """PPT生成引擎

    流水线:
      源内容 → 创建项目 → Strategist(LLM设计规划) → Executor(LLM逐页生成SVG)
      → 后处理(脚本) → 导出PPTX(脚本)
    """

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm = LLMClient(llm_config)
        # 加载 strategist / executor 参考文档(只读一次)
        self._strategist_ref = (SKILL_DIR / "references" / "strategist.md").read_text(encoding="utf-8")
        self._design_spec_ref = (SKILL_DIR / "templates" / "design_spec_reference.md").read_text(encoding="utf-8")
        self._spec_lock_ref = (SKILL_DIR / "templates" / "spec_lock_reference.md").read_text(encoding="utf-8")
        self._executor_base = (SKILL_DIR / "references" / "executor-base.md").read_text(encoding="utf-8")
        self._shared_standards = (SKILL_DIR / "references" / "shared-standards.md").read_text(encoding="utf-8")
        self._executor_general = (SKILL_DIR / "references" / "executor-general.md").read_text(encoding="utf-8")

    def generate(
        self,
        source_text: str,
        topic: str,
        fmt: str = "ppt169",
        enable_thinking: bool = False,
        progress: Optional[Callable[[str, str, int], None]] = None,
    ) -> Path:
        """执行完整 PPT 生成流水线

        Args:
            source_text: 源内容(Markdown或纯文本)
            topic: PPT主题/标题
            fmt: 画布格式, 默认 ppt169
            progress: 进度回调 fn(stage, message, pct)
        Returns:
            生成的 .pptx 文件路径
        """
        def _prog(stage: str, msg: str, pct: int):
            logger.info("[%s] %s (%d%%)", stage, msg, pct)
            if progress:
                progress(stage, msg, pct)

        # ── Step 1: 创建项目 ──
        _prog("init", "创建项目目录...", 5)
        project_name = f"{topic[:30].replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        result = _run_script(
            [sys.executable, str(SCRIPTS_DIR / "project_manager.py"), "init", project_name, "--format", fmt, "--dir", str(PROJECTS_DIR)],
            timeout=30,
        )
        # project_manager 返回实际目录名可能带后缀 (_ppt169_YYYYMMDD)
        actual_name = project_name
        for line in result.splitlines():
            if "Project created:" in line or "[OK] Project initialized:" in line:
                parts = line.rsplit("/", 1)
                if len(parts) == 2:
                    actual_name = parts[1].strip()
                break
        project_path = PROJECTS_DIR / actual_name

        # 写入源内容
        sources_dir = project_path / "sources"
        sources_dir.mkdir(exist_ok=True)
        (sources_dir / "source.md").write_text(source_text, encoding="utf-8")
        _prog("init", "项目创建完成", 10)

        # ── Step 2: Strategist — LLM 生成 design_spec + spec_lock ──
        _prog("strategist", "AI 正在规划PPT设计方案...", 15)
        design_spec, spec_lock = self._run_strategist(source_text, topic, fmt, enable_thinking=enable_thinking)
        (project_path / "design_spec.md").write_text(design_spec, encoding="utf-8")
        (project_path / "spec_lock.md").write_text(spec_lock, encoding="utf-8")
        _prog("strategist", "设计方案生成完成", 25)

        # ── Step 3: Executor — LLM 逐页生成 SVG ──
        _prog("executor", "AI 正在生成PPT页面...", 30)
        pages = self._parse_page_plan(spec_lock)
        logger.info("page_plan 解析结果: %d 页, 源内容长度: %d", len(pages), len(source_text))
        # 最小页数保护: 根据源内容长度确保足够页数
        min_pages = 5  # 至少 cover + 3内容 + ending
        if len(source_text) > 500:
            min_pages = max(min_pages, 6)
        if len(source_text) > 1000:
            min_pages = max(min_pages, 8)
        if len(pages) < min_pages and len(source_text) > 200:
            logger.warning("page_plan 只有 %d 页但源内容有 %d 字符，自动补充到 %d 页", len(pages), len(source_text), min_pages)
            while len(pages) < min_pages:
                idx = len(pages) + 1
                pages.append({
                    "page_num": idx,
                    "title": f"第{idx}页",
                    "filename": f"slide_{idx:02d}.svg",
                    "layout_hint": "end" if idx == min_pages else "content",
                    "key_points": source_text[:500],
                })
        svg_output_dir = project_path / "svg_output"
        svg_output_dir.mkdir(exist_ok=True)

        total_pages = len(pages)
        for i, page_info in enumerate(pages):
            pct = 30 + int((i / total_pages) * 50)
            _prog("executor", f"正在生成第 {i+1}/{total_pages} 页: {page_info.get('title', '')}", pct)
            svg_content = self._generate_svg_page(spec_lock, page_info, i, total_pages, enable_thinking=enable_thinking)
            # 移除SVG动画元素（svg_to_pptx不支持animate）
            svg_content = re.sub(r"<animate\b[^>]*/>", "", svg_content, flags=re.DOTALL)
            svg_content = re.sub(r"<animate\b[^>]*>.*?</animate>", "", svg_content, flags=re.DOTALL)
            fname = page_info.get("filename", f"slide_{i+1:02d}.svg")
            if not fname.endswith(".svg"):
                fname += ".svg"
            (svg_output_dir / fname).write_text(svg_content, encoding="utf-8")

        _prog("executor", f"全部 {total_pages} 页SVG生成完成", 80)

        # ── Step 4: 后处理(直接调用原项目脚本) ──
        _prog("postprocess", "正在后处理...", 82)
        # 生成演讲备注
        notes_md = self._generate_notes(spec_lock, source_text, topic, enable_thinking=enable_thinking)
        (project_path / "notes" / "total.md").parent.mkdir(exist_ok=True)
        (project_path / "notes" / "total.md").write_text(notes_md, encoding="utf-8")

        # 拆分演讲备注（非关键步骤，失败不阻断流程）
        try:
            _run_script([sys.executable, str(SCRIPTS_DIR / "total_md_split.py"), str(project_path)], timeout=30)
        except RuntimeError as e:
            logger.warning(f"Notes split skipped: {e}")

        _prog("postprocess", "SVG后处理...", 86)

        _run_script([sys.executable, str(SCRIPTS_DIR / "finalize_svg.py"), str(project_path)], timeout=60)

        # 清理 svg_final 中可能残留的 animate 元素
        for svg_file in (project_path / "svg_final").glob("*.svg"):
            content = svg_file.read_text(encoding="utf-8")
            cleaned = re.sub(r"<animate\b[^>]*/>", "", content, flags=re.DOTALL)
            cleaned = re.sub(r"<animate\b[^>]*>.*?</animate>", "", cleaned, flags=re.DOTALL)
            if cleaned != content:
                svg_file.write_text(cleaned, encoding="utf-8")

        _prog("postprocess", "导出PPTX...", 90)

        _run_script(
            [sys.executable, str(SCRIPTS_DIR / "svg_to_pptx.py"), str(project_path)],
            timeout=60,
        )
        _prog("postprocess", "导出完成", 95)

        # 找到生成的 pptx
        exports_dir = project_path / "exports"
        if exports_dir.exists():
            pptx_files = sorted(exports_dir.glob("*.pptx"), key=lambda f: f.stat().st_mtime, reverse=True)
            if pptx_files:
                _prog("done", "PPT生成完成!", 100)
                return pptx_files[0]

        raise RuntimeError("PPTX 导出失败: 未找到输出文件")

    # ────────────────── Strategist ──────────────────

    def _run_strategist(self, source_text: str, topic: str, fmt: str, *, enable_thinking: bool = False) -> tuple[str, str]:
        """调用 LLM 执行 Strategist 角色, 返回 (design_spec_md, spec_lock_md)"""

        system = (
            "你是顶级PPT策略规划师(Strategist)。你的任务是根据源内容生成PPT设计方案。\n\n"
            "## 参考文档\n"
            f"{self._strategist_ref[:6000]}\n\n"
            "## 设计规范模板(必须严格遵循此结构)\n"
            f"{self._design_spec_ref[:8000]}\n\n"
            "## spec_lock 模板\n"
            f"{self._spec_lock_ref[:4000]}\n"
        )

        # 限制源内容长度避免超token
        truncated = source_text[:15000] + ("\n...(内容已截断)" if len(source_text) > 15000 else "")

        user_msg = (
            f"请为以下内容生成PPT设计方案。\n\n"
            f"## 主题: {topic}\n## 格式: {fmt}\n\n"
            f"## 源内容\n{truncated}\n\n"
            "请输出两部分, 用 ===DESIGN_SPEC=== 和 ===SPEC_LOCK=== 分隔:\n\n"
            "第一部分: design_spec.md (完整的设计规范文档, 包含所有XI个章节)\n"
            "第二部分: spec_lock.md (机器可读的执行锁定文件)\n\n"
            "要求:\n"
            f"- 画布格式: {fmt}\n"
            "- 页数规则(严格遵守):\n"
            "  · 先统计源内容的自然段落数/章节数/列表项数，每个有实质内容的段落/章节至少独立成页\n"
            "  · 最少5页(1封面+3内容+1结束)，最多25页\n"
            "  · 不要把多个主题堆在一页，宁可多拆几页保持每页信息量清晰\n"
            "  · 如果源内容超过500字，页数不应少于6页；超过1000字不应少于8页\n"
            "  · 如果源内容有明确的章节/小标题，每个章节至少1页\n"
            "- 风格: 专业商务风(通用), 配色协调\n"
            "- 每页在 spec_lock 的 page_plan 中必须有: page_num, title, filename, layout_hint, key_points\n"
            "- 不需要用户确认, 直接生成最佳方案\n"
            "- page_plan 中必须包含至少: 1页封面(cover) + 若干内容页 + 1页结束页(ending)。\n"
        )

        resp = self.llm.chat([{"role": "user", "content": user_msg}], system_prompt=system, enable_thinking=enable_thinking)

        # 拆分两部分
        design_spec, spec_lock = self._split_response(resp)
        return design_spec, spec_lock

    # ────────────────── Executor ──────────────────

    def _parse_page_plan(self, spec_lock: str) -> list[dict]:
        """从 spec_lock 中解析页面计划

        支持三种格式:
        1. JSON 数组: page_plan: [...]
        2. YAML 列表: ## page_plan 后跟多个 - page_num: N 条目
        3. Markdown 列表: 1. xxx 或 - Page N
        """
        pages: list[dict] = []

        # ── 方式1: JSON 数组 ──
        try:
            match = re.search(r"page_plan\s*[:=]\s*", spec_lock, re.IGNORECASE)
            if match:
                rest = spec_lock[match.end():]
                json_match = re.search(r"(\[.*?\])", rest, re.DOTALL)
                if json_match:
                    pages = json.loads(json_match.group(1))
                    if pages:
                        return pages
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("JSON page_plan 解析失败: %s", e)

        # ── 方式2: YAML 列表格式 - page_num: N ... ──
        # 匹配 ## page_plan 之后的内容，提取每个 "- page_num:" 开头的块
        try:
            pp_section = re.search(
                r"##\s*page_plan\s*\n(.*?)(?=\n##\s|\Z)",
                spec_lock, re.IGNORECASE | re.DOTALL,
            )
            if pp_section:
                blocks = re.split(r"\n(?=-\s+page_num\s*:)", pp_section.group(1).strip())
                for block in blocks:
                    block = block.strip()
                    if not block.startswith("-"):
                        continue
                    info: dict = {}
                    for line in block.split("\n"):
                        line = line.strip().lstrip("- ")
                        if not line:
                            continue
                        if ":" not in line:
                            continue
                        key, _, val = line.partition(":")
                        key = key.strip().lower().replace(" ", "_")
                        val = val.strip()
                        info[key] = val
                    if info:
                        try:
                            info["page_num"] = int(re.sub(r"\D", "", str(info.get("page_num", "0"))))
                        except (ValueError, TypeError):
                            pass
                        pages.append(info)
                if pages:
                    logger.info("YAML page_plan 解析成功: %d 页", len(pages))
                    return pages
        except Exception as e:
            logger.warning("YAML page_plan 解析失败: %s", e)

        # ── 方式3: Markdown 列表 ──
        for i, line in enumerate(spec_lock.split("\n")):
            line = line.strip()
            if re.match(r"^\d+\.\s+|^-?\s*\*?\s*Page\s+\d+", line, re.IGNORECASE):
                title = re.sub(r"^[-*\d.\s]+", "", line).strip()
                pages.append({
                    "page_num": i + 1,
                    "title": title,
                    "filename": f"slide_{i+1:02d}.svg",
                    "layout_hint": "content",
                    "key_points": title,
                })

        if pages:
            return pages

        # ── 最终回退: 从标题数推断 ──
        heading_pattern = re.compile(r"^#{1,3}\s+\S", re.MULTILINE)
        headings = heading_pattern.findall(spec_lock)
        estimated = max(3, min(len(headings), 20))
        logger.warning("page_plan 解析失败, 从标题推断 %d 页", estimated)
        pages = [
            {"page_num": i, "title": f"第{i}页", "filename": f"slide_{i:02d}.svg",
             "layout_hint": "cover" if i == 1 else ("end" if i == estimated else "content"),
             "key_points": ""}
            for i in range(1, estimated + 1)
        ]
        return pages

    def _generate_svg_page(
        self, spec_lock: str, page_info: dict, page_idx: int, total: int, *, enable_thinking: bool = False
    ) -> str:
        """调用 LLM 为单页生成 SVG"""

        # 画布尺寸
        fmt = "ppt169"
        if "ppt43" in spec_lock:
            fmt = "ppt43"
        W, H = ("1280", "720") if fmt == "ppt169" else ("960", "720")

        system = (
            "你是PPT执行者(Executor)，负责生成单个SVG页面。\n\n"
            "## SVG技术规范(必须严格遵守)\n"
            f"{self._shared_standards[:6000]}\n\n"
            "## 执行者通用指南\n"
            f"{self._executor_base[:4000]}\n\n"
            "## 通用风格指南\n"
            f"{self._executor_general[:3000]}\n"
        )

        user_msg = (
            f"请为以下页面生成完整的SVG代码。\n\n"
            f"## 当前项目 spec_lock\n{spec_lock[:8000]}\n\n"
            f"## 当前页信息\n"
            f"- 页码: {page_idx+1}/{total}\n"
            f"- 标题: {page_info.get('title', '')}\n"
            f"- 布局提示: {page_info.get('layout_hint', 'content')}\n"
            f"- 要点: {page_info.get('key_points', '')}\n\n"
            f"## 要求\n"
            f"- SVG viewBox='0 0 {W} {H}', xmlns='http://www.w3.org/2000/svg'\n"
            f"- 使用spec_lock中定义的颜色、字体\n"
            f"- 必须有 id='page_{page_idx+1}' 的顶层 <g> 元素\n"
            f"- 文字使用 <text> 元素, 不用 <foreignObject>\n"
            f"- 所有元素必须有 id 属性用于动画\n"
            f"- 只输出SVG代码, 不要任何解释\n"
        )

        resp = self.llm.chat([{"role": "user", "content": user_msg}], system_prompt=system, enable_thinking=enable_thinking)

        # 提取 SVG
        return self._extract_svg(resp)

    # ────────────────── Notes ──────────────────

    def _generate_notes(self, spec_lock: str, source_text: str, topic: str, *, enable_thinking: bool = False) -> str:
        """LLM 生成演讲备注"""
        system = "你是PPT演讲备注撰写专家。为每页PPT撰写简洁的演讲备注。"
        user_msg = (
            f"为以下PPT生成演讲备注(total.md格式)。\n\n"
            f"## 主题: {topic}\n"
            f"## spec_lock 页面计划\n{spec_lock[:5000]}\n\n"
            "格式: 每页以 '## 第N页: 标题' 开头, 然后写2-4句备注。\n"
            "最后用 '## 全文备注' 汇总所有页。"
        )
        return self.llm.chat([{"role": "user", "content": user_msg}], system_prompt=system, enable_thinking=enable_thinking)

    # ────────────────── 辅助 ──────────────────

    @staticmethod
    def _split_response(resp: str) -> tuple[str, str]:
        """拆分 LLM 返回的 design_spec + spec_lock"""
        sep = "===SPEC_LOCK==="
        if sep in resp:
            parts = resp.split(sep, 1)
            design = parts[0].replace("===DESIGN_SPEC===", "").strip()
            lock = parts[1].strip()
            return design, lock
        # 回退: 按长度大致对半分
        mid = len(resp) // 2
        return resp[:mid], resp[mid:]

    @staticmethod
    def _extract_svg(text: str) -> str:
        """从 LLM 回复中提取 SVG 内容"""
        # 直接是 <svg ...>...</svg>
        if "<svg" in text:
            start = text.index("<svg")
            end = text.rindex("</svg>") + 6
            return text[start:end]
        # markdown 代码块
        m = re.search(r"```(?:xml|svg)?\s*\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return text.strip()
