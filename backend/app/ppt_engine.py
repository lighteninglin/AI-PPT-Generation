"""
PPT生成引擎 — 完整7步流水线，复用 ppt-master 脚本

Step 1: 源内容处理 (file_parser / 文本)
Step 2: 项目初始化 (project_manager.py init)
Step 3: 模板选项 (默认跳过)
Step 4: Strategist — Eight Confirmations + design_spec + spec_lock ⛔BLOCKING
Step 5: 图片获取 (离线环境跳过)
Step 6: Executor — LLM 逐页生成 SVG
Step 7: 后处理 + 导出 PPTX
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Optional
import xml.etree.ElementTree as ET

from .config import LLMConfig
from .llm_client import LLMClient

logger = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "ppt-master"
SCRIPTS_DIR = SKILL_DIR / "scripts"
PROJECTS_DIR = Path(__file__).resolve().parents[2] / "projects"


def _run_script(args: list[str], cwd: Optional[str] = None, timeout: int = 120) -> str:
    logger.info("运行脚本: %s", " ".join(args))
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        logger.error("脚本失败 (rc=%d): %s", result.returncode, result.stderr)
        raise RuntimeError(f"脚本执行失败: {result.stderr[:2000]}")
    return result.stdout


class PPTEngine:
    """PPT生成引擎 — 完整7步流水线

    对外暴露两个主要方法:
    - preview() — Steps 1-4: 初始化 + Strategist, 返回 Eight Confirmations 供用户确认
    - execute() — Steps 5-7: SVG生成 + 后处理 + 导出
    - generate() — 一步到位(跳过确认, 向后兼容)
    """

    def __init__(self, llm_config: LLMConfig) -> None:
        self.llm = LLMClient(llm_config)
        # 只读一次参考文档
        self._strategist_ref = (SKILL_DIR / "references" / "strategist.md").read_text(encoding="utf-8")
        self._design_spec_ref = (SKILL_DIR / "templates" / "design_spec_reference.md").read_text(encoding="utf-8")
        self._spec_lock_ref = (SKILL_DIR / "templates" / "spec_lock_reference.md").read_text(encoding="utf-8")
        self._executor_base = (SKILL_DIR / "references" / "executor-base.md").read_text(encoding="utf-8")
        self._shared_standards = (SKILL_DIR / "references" / "shared-standards.md").read_text(encoding="utf-8")
        self._executor_general = (SKILL_DIR / "references" / "executor-general.md").read_text(encoding="utf-8")

    # ══════════════════════════════════════════════════════════
    #  公开 API
    # ══════════════════════════════════════════════════════════

    def preview(
        self,
        source_text: str,
        topic: str,
        fmt: str = "ppt169",
        enable_thinking: bool = False,
        progress: Optional[Callable] = None,
    ) -> dict:
        """Steps 1-4: 创建项目 + Strategist 设计规划

        Returns:
            {
                "project_path": str,
                "project_name": str,
                "eight_confirmations": dict,  # 八项确认(结构化)
                "design_spec": str,           # design_spec.md 内容
                "spec_lock": str,             # spec_lock.md 内容
                "page_plan": list[dict],      # 解析后的页面计划
            }
        """
        _prog = _make_progress(progress)

        # ── Step 1: 源内容已就绪(由上层处理) ──
        _prog("step1", "Step 1: 源内容已就绪", 5)

        # ── Step 2: 项目初始化 ──
        _prog("step2", "Step 2: 初始化项目...", 8)
        project_name = f"{topic[:30].replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        result = _run_script(
            [sys.executable, str(SCRIPTS_DIR / "project_manager.py"), "init", project_name, "--format", fmt, "--dir", str(PROJECTS_DIR)],
            timeout=30,
        )
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
        _prog("step2", f"项目已创建: {actual_name}", 10)

        # ── Step 3: 模板选项(默认跳过, free design) ──
        _prog("step3", "Step 3: 使用自由设计模式", 12)

        # ── Step 4: Strategist — Eight Confirmations + design_spec + spec_lock ──
        _prog("step4", "Step 4: AI 正在规划设计方案...", 15)

        eight_confirmations, design_spec, spec_lock = self._run_strategist(
            source_text, topic, fmt, enable_thinking=enable_thinking
        )

        # 持久化到项目目录
        (project_path / "design_spec.md").write_text(design_spec, encoding="utf-8")
        (project_path / "spec_lock.md").write_text(spec_lock, encoding="utf-8")

        pages = self._parse_page_plan(spec_lock)
        logger.info("preview page_plan: %d 页", len(pages))

        # ── 质量检查: 如果 page_plan 过于简略，自动调用 replan 补全 ──
        if pages and self._is_low_quality_page_plan(pages):
            logger.warning("page_plan 质量差(title简略/缺字段)，自动 replan 补全")
            _prog("step4", "正在补全页面规划详情...", 20)
            better_pages = self.replan_page_plan(
                source_text, topic, spec_lock, len(pages), enable_thinking=enable_thinking
            )
            if better_pages and len(better_pages) == len(pages):
                pages = better_pages
                logger.info("replan 补全成功: %d 页", len(pages))

        # 同步 eight_confirmations.page_count 与实际 page_plan 页数
        actual_n = len(pages)
        if actual_n > 0:
            pc = eight_confirmations.get("page_count", {})
            pc["recommended"] = actual_n
            pc["min"] = min(pc.get("min", actual_n), actual_n)
            pc["max"] = max(pc.get("max", actual_n), actual_n)
            eight_confirmations["page_count"] = pc

        if len(pages) <= 3:
            # 调试: 打印 spec_lock 中 page_plan 部分
            pp_match = __import__('re').search(r'##\s*page_plan(.*?)(?=\n##|\Z)', spec_lock, __import__('re').IGNORECASE | __import__('re').DOTALL)
            if pp_match:
                logger.warning("page_plan 原始内容(前800字):\n%s", pp_match.group(1)[:800])
            else:
                logger.warning("spec_lock 中未找到 page_plan section")
        _prog("step4", f"设计方案完成, 规划 {len(pages)} 页", 25)

        return {
            "project_path": str(project_path),
            "project_name": actual_name,
            "eight_confirmations": eight_confirmations,
            "design_spec": design_spec,
            "spec_lock": spec_lock,
            "page_plan": pages,
            "source_text": source_text,
            "topic": topic,
        }

    def confirm_and_regenerate(
        self,
        project_path_str: str,
        user_modifications: dict,
        enable_thinking: bool = False,
        progress: Optional[Callable] = None,
    ) -> dict:
        """Step 4 续: 用户修改了 Eight Confirmations, 重新生成 spec_lock

        Args:
            project_path_str: 项目路径
            user_modifications: 用户修改后的 Eight Confirmations JSON
        """
        _prog = _make_progress(progress)
        project_path = Path(project_path_str)

        _prog("step4_revise", "根据您的修改重新生成设计方案...", 18)

        source_text = (project_path / "sources" / "source.md").read_text(encoding="utf-8")
        topic = project_path.name.split("_")[0]
        fmt = "ppt169"

        spec_lock = self._regenerate_spec_lock(
            source_text, topic, fmt, user_modifications, enable_thinking=enable_thinking
        )

        (project_path / "spec_lock.md").write_text(spec_lock, encoding="utf-8")
        pages = self._parse_page_plan(spec_lock)
        _prog("step4_revise", f"方案已更新, 规划 {len(pages)} 页", 25)

        return {
            "spec_lock": spec_lock,
            "page_plan": pages,
        }

    def execute(
        self,
        project_path_str: str,
        spec_lock: str,
        page_plan: Optional[list[dict]] = None,
        enable_thinking: bool = False,
        progress: Optional[Callable] = None,
    ) -> Path:
        """Steps 5-7: Executor SVG生成 + 后处理 + 导出

        Args:
            project_path_str: 项目路径
            spec_lock: spec_lock.md 内容(可能经过用户修改后重新生成)
            page_plan: 用户确认的页面规划（优先于spec_lock中解析）
        Returns:
            生成的 .pptx 文件路径
        """
        _prog = _make_progress(progress)
        project_path = Path(project_path_str)

        # ── Step 5: 图片获取(离线环境跳过) ──
        _prog("step5", "Step 5: 离线环境, 跳过图片获取", 27)

        # ── Step 6: Executor — LLM 逐页生成 SVG ──
        _prog("step6", "Step 6: AI 正在生成PPT页面...", 30)
        # 优先使用用户确认的 page_plan，否则从 spec_lock 解析
        pages = page_plan if page_plan else self._parse_page_plan(spec_lock)
        # 防御：过滤掉非 dict 元素（LLM 有时返回混合类型）
        if pages:
            bad = [p for p in pages if not isinstance(p, dict)]
            if bad:
                logger.warning("page_plan 中有 %d 个非 dict 元素，已过滤: %s", len(bad), bad[:3])
            pages = [p for p in pages if isinstance(p, dict)]
        # 安全排序：确保按 page_num 升序（封面在前）
        pages.sort(key=lambda p: int(p.get("page_num", 0)))
        logger.info("page_plan: %d 页 (来源: %s)", len(pages), "用户确认" if page_plan else "spec_lock解析")

        # 读取源文本（后续 _generate_svg_page 需要）
        source_text = ""
        src_file = project_path / "sources" / "source.md"
        if src_file.exists():
            source_text = src_file.read_text(encoding="utf-8")

        # 最小页数保护 — 仅当 page_plan 来自 spec_lock 自动解析（非用户确认）时生效
        if not page_plan:
            min_pages = 5
            if len(source_text) > 500:
                min_pages = max(min_pages, 6)
            if len(source_text) > 1000:
                min_pages = max(min_pages, 8)
            if len(pages) < min_pages and len(source_text) > 200:
                logger.warning("page_plan 只有 %d 页但源内容有 %d 字符，自动补充到 %d 页",
                               len(pages), len(source_text), min_pages)
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
            pct = 30 + int((i / total_pages) * 45)
            _prog("step6", f"正在生成第 {i+1}/{total_pages} 页: {page_info.get('title', '')}", pct)
            svg_content = self._generate_svg_page(
                spec_lock, page_info, i, total_pages, source_text, enable_thinking=enable_thinking
            )
            # 移除SVG动画元素（svg_to_pptx不支持 animate/animateTransform/animateMotion）
            svg_content = re.sub(r"<animate(?:Transform|Motion)?\b[^>]*/>", "", svg_content, flags=re.DOTALL)
            svg_content = re.sub(r"<animate(?:Transform|Motion)?\b[^>]*>.*?</animate(?:Transform|Motion)?>", "", svg_content, flags=re.DOTALL)

            # 兜底：如果SVG缺少背景rect，自动补一个铺满viewBox的背景
            if not re.search(r'<rect\b[^>]*\bwidth\s*=\s*["\']?1280["\']?', svg_content) and \
               not re.search(r'<rect\b[^>]*\bwidth\s*=\s*["\']?960["\']?', svg_content):
                svg_fmt = "ppt43" if "ppt43" in spec_lock else "ppt169"
                _W, _H = ("960", "720") if svg_fmt == "ppt43" else ("1280", "720")
                bg_color = self._extract_bg_color(spec_lock)
                bg_rect = f'<rect x="0" y="0" width="{_W}" height="{_H}" fill="{bg_color}"/>'
                # 插入到顶层 <g> 标签后
                g_match = re.search(r'(<g\b[^>]*>)', svg_content)
                if g_match:
                    svg_content = svg_content[:g_match.end()] + "\n  " + bg_rect + svg_content[g_match.end():]
                    logger.info("已为第 %d 页自动补充背景: %s", i+1, bg_color)

            fname = page_info.get("filename", f"slide_{i+1:02d}.svg")
            # 安全校验：文件名不能含 JSON 残片、路径分隔符、引号等非法字符
            if not fname or any(c in fname for c in '{}":\\/') or not fname.replace('_','').replace('-','').replace('.','').replace(' ','').isalnum():
                fname = f"slide_{int(page_info.get('page_num', i+1)):02d}.svg"
                logger.warning("非法文件名已修正: %s → %s", page_info.get('filename',''), fname)
            if not fname.endswith(".svg"):
                fname += ".svg"
            # 强制在文件名前加零填充序号，确保排序正确: 01_cover.svg, 02_summary.svg ...
            fname_no_ext = fname[:-4]  # 去掉 .svg
            # 如果已经有前导 NN_ 则跳过
            if not re.match(r'^\d{2}_', fname_no_ext):
                fname = f"{i+1:02d}_{fname_no_ext}.svg"
            # ── SVG 修复 (弱模型常见错误) ──
            svg_content = self._fix_svg(svg_content)

            # ── XML 校验: 确保保存的 SVG 是合法 XML ──
            try:
                ET.fromstring(svg_content)
            except ET.ParseError as e:
                logger.warning("第 %d 页SVG XML非法，尝试修复: %s", i+1, e)
                # 尝试修复：移除非法字符、补全缺失标签
                fixed = svg_content
                # 移除 XML 声明前的非法字节
                fixed = re.sub(r'^[^<]+', '', fixed)
                # 确保以 </svg> 结尾
                if '</svg>' not in fixed:
                    fixed = fixed.rstrip() + '\n</svg>'
                # 尝试再次解析
                try:
                    ET.fromstring(fixed)
                    svg_content = fixed
                    logger.info("第 %d 页SVG XML修复成功", i+1)
                except ET.ParseError:
                    logger.error("第 %d 页SVG XML修复失败，跳过此页", i+1)
                    continue
            (svg_output_dir / fname).write_text(svg_content, encoding="utf-8")

        _prog("step6", f"全部 {total_pages} 页SVG生成完成", 76)

        # ── Step 6 续: 演讲备注 ──
        notes_md = self._generate_notes(spec_lock, source_text, project_path.name, enable_thinking=enable_thinking)
        (project_path / "notes").mkdir(exist_ok=True)
        (project_path / "notes" / "total.md").write_text(notes_md, encoding="utf-8")

        # ── Step 7: 后处理 + 导出 ──
        _prog("step7", "Step 7: 后处理...", 78)

        # 7.1 拆分演讲备注（非关键，失败不阻断）
        try:
            _run_script([sys.executable, str(SCRIPTS_DIR / "total_md_split.py"), str(project_path)], timeout=30)
        except RuntimeError as e:
            logger.warning("Notes split skipped: %s", e)

        # 7.2 SVG后处理
        _prog("step7", "SVG后处理...", 82)
        _run_script([sys.executable, str(SCRIPTS_DIR / "finalize_svg.py"), str(project_path)], timeout=60)

        # 清理 svg_final 中可能残留的 animate/animateTransform/animateMotion 元素
        svg_final_dir = project_path / "svg_final"
        if svg_final_dir.exists():
            for svg_file in svg_final_dir.glob("*.svg"):
                content = svg_file.read_text(encoding="utf-8")
                cleaned = re.sub(r"<animate(?:Transform|Motion)?\b[^>]*/>", "", content, flags=re.DOTALL)
                cleaned = re.sub(r"<animate(?:Transform|Motion)?\b[^>]*>.*?</animate(?:Transform|Motion)?>", "", cleaned, flags=re.DOTALL)
                if cleaned != content:
                    logger.info("清除 %s 中的动画元素", svg_file.name)
                    svg_file.write_text(cleaned, encoding="utf-8")

        # 7.3 导出PPTX
        _prog("step7", "导出PPTX...", 90)
        try:
            _run_script(
                [sys.executable, str(SCRIPTS_DIR / "svg_to_pptx.py"), str(project_path)],
                timeout=120,
            )
        except RuntimeError as e:
            err_msg = str(e)
            # native模式失败(如不支持的SVG元素、XML解析错误), fallback到legacy模式
            fallback_keywords = [
                "unsupported visual SVG element",
                "SvgNativeConversionError",
                "ParseError",
                "not well-formed",
                "unclosed token",
                "no element found",
                "mismatched tag",
            ]
            if any(kw in err_msg for kw in fallback_keywords):
                logger.warning("native模式失败, fallback到legacy模式: %s", err_msg[:200])
                _prog("step7", "native模式失败, 使用兼容模式导出...", 91)
                _run_script(
                    [sys.executable, str(SCRIPTS_DIR / "svg_to_pptx.py"), str(project_path), "--only", "legacy"],
                    timeout=120,
                )
            else:
                raise
        _prog("step7", "导出完成!", 95)

        # 找到生成的 pptx
        # native模式输出到 exports/, legacy模式输出到 backup/
        # 所以在 project_path 下递归搜索所有 pptx 文件
        exports_dir = project_path / "exports"
        all_pptx = []
        if exports_dir.exists():
            all_pptx.extend(exports_dir.glob("*.pptx"))
        # 也搜索 backup/ 目录 (legacy模式输出)
        backup_dir = project_path / "backup"
        if backup_dir.exists():
            all_pptx.extend(backup_dir.rglob("*.pptx"))
        # 还搜索项目根目录 (某些模式直接输出到根目录)
        all_pptx.extend(project_path.glob("*.pptx"))

        # 按修改时间排序，取最新的
        all_pptx = sorted(list(set(all_pptx)), key=lambda f: f.stat().st_mtime, reverse=True)
        if all_pptx:
            _prog("done", "PPT生成完成!", 100)
            return all_pptx[0]

        raise RuntimeError(f"PPTX 导出失败: 未找到输出文件 (搜索路径: {project_path})")

    def generate(
        self,
        source_text: str,
        topic: str,
        fmt: str = "ppt169",
        enable_thinking: bool = False,
        progress: Optional[Callable] = None,
    ) -> Path:
        """一步到位(向后兼容) — 完整流水线跳过用户确认"""
        preview_result = self.preview(source_text, topic, fmt, enable_thinking, progress)
        return self.execute(
            preview_result["project_path"],
            preview_result["spec_lock"],
            preview_result.get("page_plan"),
            enable_thinking,
            progress,
        )

    # ══════════════════════════════════════════════════════════
    #  Step 4: Strategist
    # ══════════════════════════════════════════════════════════

    def _run_strategist(
        self, source_text: str, topic: str, fmt: str, *, enable_thinking: bool = False
    ) -> tuple[dict, str, str]:
        """调用 LLM 执行 Strategist 角色

        Returns: (eight_confirmations_dict, design_spec_md, spec_lock_md)
        """
        system = (
            "你是顶级PPT策略规划师(Strategist)。你的任务是根据源内容生成PPT设计方案。\n\n"
            "## Strategist 角色定义\n"
            f"{self._strategist_ref[:6000]}\n\n"
            "## 设计规范模板(必须严格遵循此结构)\n"
            f"{self._design_spec_ref[:8000]}\n\n"
            "## spec_lock 模板\n"
            f"{self._spec_lock_ref[:4000]}\n"
        )

        truncated = source_text[:15000] + ("\n...(内容已截断)" if len(source_text) > 15000 else "")

        user_msg = (
            f"请为以下内容生成PPT设计方案。\n\n"
            f"## 主题: {topic}\n## 格式: {fmt}\n\n"
            f"## 源内容\n{truncated}\n\n"
            "请严格按照以下格式输出，分三部分:\n\n"
            "===EIGHT_CONFIRMATIONS===\n"
            "输出JSON格式的八项确认(严格遵守):\n"
            "{\n"
            '  "canvas_format": "PPT 16:9 (1280×720)",\n'
            '  "page_count": {"min": 5, "max": 15, "recommended": 8},\n'
            '  "target_audience": "目标受众描述",\n'
            '  "style_objective": "风格目标描述",\n'
            '  "color_scheme": {\n'
            '    "primary": "#hex", "secondary": "#hex",\n'
            '    "accent": "#hex", "background": "#hex",\n'
            '    "text": "#hex", "description": "配色说明"\n'
            '  },\n'
            '  "icon_usage": "图标使用策略",\n'
            '  "typography": {\n'
            '    "heading_font": "字体名", "heading_size": "28-36px",\n'
            '    "body_font": "字体名", "body_size": "14-18px",\n'
            '    "description": "字体方案说明"\n'
            '  },\n'
            '  "image_usage": "图片使用策略"\n'
            "}\n\n"
            "===DESIGN_SPEC===\n"
            "完整的 design_spec.md (包含所有XI个章节)\n\n"
            "===SPEC_LOCK===\n"
            "完整的 spec_lock.md (机器可读的执行锁定文件)\n\n"
            "要求:\n"
            f"- 画布格式: {fmt}\n"
            "- 页数规则(严格遵守):\n"
            "  · 先统计源内容的自然段落数/章节数/列表项数，每个有实质内容的段落/章节至少独立成页\n"
            "  · 最少5页(1封面+3内容+1结束)，最多25页\n"
            "  · 不要把多个主题堆在一页，宁可多拆几页保持每页信息量清晰\n"
            "  · 如果源内容超过500字，页数不应少于6页；超过1000字不应少于8页\n"
            "  · 如果源内容有明确的章节/小标题，每个章节至少1页\n"
            "- 风格: 专业商务风(通用), 配色协调\n"
            "- spec_lock 的 page_plan 中每页必须有: page_num, title, filename, layout_hint, key_points\n"
            "- page_plan 必须包含: 1页封面(cover) + 若干内容页 + 1页结束页(ending)\n"
        )

        resp = self.llm.chat(
            [{"role": "user", "content": user_msg}],
            system_prompt=system,
            enable_thinking=enable_thinking,
        )

        # 解析三部分
        eight_cf, design_spec, spec_lock = self._split_strategist_response(resp)
        return eight_cf, design_spec, spec_lock

    def _regenerate_spec_lock(
        self,
        source_text: str,
        topic: str,
        fmt: str,
        user_modifications: dict,
        *,
        enable_thinking: bool = False,
    ) -> str:
        """用户修改 Eight Confirmations 后, 重新生成 spec_lock"""
        system = (
            "你是顶级PPT策略规划师(Strategist)。用户已确认并修改了设计方案。\n"
            "请根据用户确认的方案生成新的 spec_lock.md。\n\n"
            "## spec_lock 模板\n"
            f"{self._spec_lock_ref[:4000]}\n"
        )

        truncated = source_text[:15000] + ("\n...(内容已截断)" if len(source_text) > 15000 else "")

        user_msg = (
            f"## 主题: {topic}\n## 格式: {fmt}\n\n"
            f"## 源内容\n{truncated}\n\n"
            f"## 用户确认的设计方案\n{json.dumps(user_modifications, ensure_ascii=False, indent=2)}\n\n"
            "请输出完整的 spec_lock.md，严格遵循用户确认的配色、字体、页数等方案。\n"
            "- page_plan 每页必须有: page_num, title, filename, layout_hint, key_points\n"
            "- 只输出 spec_lock 内容，不要其他解释\n"
        )

        resp = self.llm.chat(
            [{"role": "user", "content": user_msg}],
            system_prompt=system,
            enable_thinking=enable_thinking,
        )
        return resp.strip()

    def replan_page_plan(
        self, source_text: str, topic: str, spec_lock: str, target_pages: int, *, enable_thinking: bool = False
    ) -> list[dict]:
        """用户调整页数后，让LLM重新规划 page_plan"""
        truncated = source_text[:12000] + ("\n...(内容已截断)" if len(source_text) > 12000 else "")

        system = (
            "你是PPT策略规划师。用户调整了页数，请根据新的页数要求重新规划页面内容分配。\n\n"
            "## 原始 spec_lock (颜色/字体/画布等保持不变)\n"
            f"{spec_lock[:6000]}\n"
        )

        user_msg = (
            f"## 主题: {topic}\n"
            f"## 源内容\n{truncated}\n\n"
            f"## 要求\n"
            f"- 总页数必须是 **{target_pages}** 页（包含封面和结束页）\n"
            f"- 第1页必须是封面(cover)，最后一页必须是结束页(ending)\n"
            f"- 根据源内容合理分配每页的主题，不要遗漏重要内容\n"
            f"- 每页要有明确的标题和2-3个要点\n\n"
            "## 输出格式\n"
            "严格输出JSON数组，每个元素包含:\n"
            '```json\n'
            '[\n'
            '  {"page_num": 1, "title": "封面标题", "filename": "slide_01_cover.svg", '
            '"layout_hint": "cover", "key_points": "副标题/日期等"},\n'
            '  {"page_num": 2, "title": "内容页标题", "filename": "slide_02_content.svg", '
            '"layout_hint": "content", "key_points": "要点1, 要点2, 要点3"},\n'
            '  ...\n'
            ']\n'
            '```\n'
            "只输出JSON数组，不要其他文字。\n"
        )

        resp = self.llm.chat(
            messages=[{"role": "user", "content": user_msg}],
            system_prompt=system,
            enable_thinking=enable_thinking,
        )

        # 解析LLM返回的JSON数组
        try:
            # 尝试直接解析
            pages = json.loads(resp.strip())
            if isinstance(pages, list) and pages:
                # 只保留 dict 元素，过滤字符串等
                pages = [p for p in pages if isinstance(p, dict)]
                if pages:
                    pages.sort(key=lambda p: int(p.get("page_num", 0)))
                    return pages
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取JSON数组（括号平衡匹配，防止截断）
        json_start = resp.find('[')
        if json_start >= 0:
            depth = 0
            end = json_start
            for idx, ch in enumerate(resp[json_start:], json_start):
                if ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                    if depth == 0:
                        end = idx + 1
                        break
            try:
                pages = json.loads(resp[json_start:end])
                if isinstance(pages, list) and pages:
                    pages = [p for p in pages if isinstance(p, dict)]
                    if pages:
                        pages.sort(key=lambda p: int(p.get("page_num", 0)))
                        return pages
            except json.JSONDecodeError:
                pass

        logger.warning("replan LLM返回解析失败，回退到前端调整")
        return []

    # ══════════════════════════════════════════════════════════
    #  Step 6: Executor
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _is_low_quality_page_plan(pages: list[dict]) -> bool:
        """检测 page_plan 是否质量差（title全是英文标识符、缺少关键字段等）

        判定标准：
        - 超过一半页的 title 是纯英文短标识符（如 cover, summary, ending）
        - 或超过一半页缺少 filename 或 key_points
        """
        import unicodedata
        weak_count = 0
        for p in pages:
            if not isinstance(p, dict):
                continue
            title = (p.get("title") or "").strip()
            fname = (p.get("filename") or "").strip()
            kp = (p.get("key_points") or "").strip()

            is_weak = False
            # title 是纯英文短词（长度 ≤ 30，且无中文字符）
            if title:
                has_cjk = any('CJK' in unicodedata.name(ch, '') for ch in title)
                if not has_cjk and len(title) <= 30:
                    is_weak = True
            else:
                is_weak = True

            # filename 是自动生成的（slide_NN.svg 格式，无语义）
            if fname and re.match(r'^slide_\d+\.svg$', fname):
                is_weak = True

            # 缺少 key_points
            if not kp:
                is_weak = True

            if is_weak:
                weak_count += 1

        # 超过一半的页质量差 → 判定为低质量
        return weak_count > len(pages) * 0.5

    def _parse_page_plan(self, spec_lock: str) -> list[dict]:
        """从 spec_lock 中解析页面计划 (支持 JSON/YAML/Markdown 三种格式)"""
        pages: list[dict] = []

        # 提取 page_plan section 文本
        pp_section = re.search(
            r"##\s*page_plan\s*\n(.*?)(?=\n##\s|\Z)",
            spec_lock, re.IGNORECASE | re.DOTALL,
        )
        section_text = pp_section.group(1) if pp_section else ""

        # 方式1: JSON 数组 (用括号平衡匹配, 不用非贪婪)
        if section_text:
            json_match = re.search(r"\[", section_text)
            if json_match:
                # 括号平衡找到完整的 JSON 数组
                start = json_match.start()
                depth = 0
                end = start
                for i, ch in enumerate(section_text[start:], start):
                    if ch == '[':
                        depth += 1
                    elif ch == ']':
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                try:
                    pages = json.loads(section_text[start:end])
                    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
                        # 清理字段值：确保 title/filename 是字符串
                        for p in pages:
                            for k, v in p.items():
                                if isinstance(v, str):
                                    p[k] = v.strip().strip('"').strip("'")
                        pages.sort(key=lambda p: int(p.get("page_num", 0)))
                        logger.info("JSON page_plan 解析成功: %d 页", len(pages))
                        return pages
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning("JSON page_plan 解析失败: %s", e)

        # 方式2: YAML 列表格式 (- page_num: N ...)
        try:
            if pp_section:
                # 只有包含 "page_num:" 才走 YAML 路径
                section_text_2 = pp_section.group(1)
                if "page_num:" in section_text_2 or "page_num :" in section_text_2:
                    blocks = re.split(r"\n(?=-\s*page_num\s*:)", section_text_2.strip())
                    for block in blocks:
                        block = block.strip()
                        if not block.startswith("-"):
                            continue
                        info: dict = {}
                        for line in block.split("\n"):
                            line = line.strip().lstrip("- ")
                            if not line or ":" not in line:
                                continue
                            key, _, val = line.partition(":")
                            key = key.strip().lower().replace(" ", "_")
                            val = val.strip().strip('"').strip("'")
                            if val.startswith("[") and val.endswith("]"):
                                # key_points: ["a", "b"] → "a, b"
                                val = ", ".join(re.findall(r'"([^"]*)"', val))
                            info[key] = val
                        if info:
                            try:
                                info["page_num"] = int(re.sub(r"\D", "", str(info.get("page_num", "0"))))
                            except (ValueError, TypeError):
                                pass
                            pages.append(info)
                    if pages:
                        for p in pages:
                            fn = p.get("filename", "")
                            if fn and not fn.endswith(".svg"):
                                p["filename"] = fn + ".svg"
                        pages = [p for p in pages if isinstance(p, dict)]
                        logger.info("YAML page_plan 解析成功: %d 页", len(pages))
                        pages.sort(key=lambda p: int(p.get("page_num", 0)))
                        return pages
        except Exception as e:
            logger.warning("YAML page_plan 解析失败: %s", e)

        # 方式2.5: YAML inline-object 格式: - page_NN: { title: "...", filename: "...", ... }
        try:
            if pp_section:
                section_text_25 = pp_section.group(1)
                if re.search(r'-\s*page[_\s]*\d+\s*:\s*\{', section_text_25):
                    for line in section_text_25.strip().split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        m = re.match(r'-\s*page[_\s]*(\d+)\s*:\s*\{(.+)\}\s*$', line)
                        if not m:
                            continue
                        page_num = int(m.group(1))
                        obj_str = m.group(2)
                        info: dict = {"page_num": page_num}
                        # 提取各字段（用正则从 inline object 中提取）
                        for field, default in [
                            ("title", f"第{page_num}页"),
                            ("filename", f"slide_{page_num:02d}.svg"),
                            ("layout_hint", "content"),
                        ]:
                            fm = re.search(rf'{field}\s*:\s*"([^"]*)"', obj_str)
                            if fm:
                                val = fm.group(1).strip()
                                info[field] = val if val else default
                            elif field not in info:
                                info[field] = default
                        # key_points: ["...", "..."] → 逗号连接
                        kp_m = re.search(r'key_points\s*:\s*\[(.+?)\]', obj_str)
                        if kp_m:
                            points = re.findall(r'"([^"]*)"', kp_m.group(1))
                            info["key_points"] = ", ".join(points) if points else ""
                        else:
                            info["key_points"] = ""
                        # filename 补 .svg
                        if not info["filename"].endswith(".svg"):
                            info["filename"] += ".svg"
                        pages.append(info)
                    if pages:
                        logger.info("YAML inline-object page_plan 解析成功: %d 页", len(pages))
                        pages.sort(key=lambda p: int(p.get("page_num", 0)))
                        return pages
        except Exception as e:
            logger.warning("YAML inline-object page_plan 解析失败: %s", e)

        # 方式3: 逗号分隔格式 "- N: title, filename, layout, desc"
        try:
            if pp_section:
                for line in pp_section.group(1).strip().split("\n"):
                    line = line.strip()
                    # 匹配 "- 1: cover, ..." 或 "- page_num: 1, ..."
                    m = re.match(r"-\s*(\d+)\s*:\s*(.+)", line)
                    if m:
                        parts = [p.strip().strip('"').strip("'") for p in m.group(2).split(",")]
                        title = parts[0] if parts else f"第{m.group(1)}页"
                        filename = parts[1] if len(parts) > 1 else f"slide_{int(m.group(1)):02d}.svg"
                        layout = parts[2] if len(parts) > 2 else "content"
                        kp = parts[3] if len(parts) > 3 else ""
                        pages.append({
                            "page_num": int(m.group(1)),
                            "title": title,
                            "filename": filename if filename.endswith(".svg") else filename + ".svg",
                            "layout_hint": layout,
                            "key_points": kp,
                        })
                if pages:
                    logger.info("逗号分隔 page_plan 解析成功: %d 页", len(pages))
                    pages.sort(key=lambda p: int(p.get("page_num", 0)))
                    return pages
        except Exception as e:
            logger.warning("逗号分隔 page_plan 解析失败: %s", e)

        # 方式4: Markdown 列表 "1. xxx" 或 "- Page N"
        for i, line in enumerate(spec_lock.split("\n")):
            line = line.strip()
            if re.match(r"^\d+\.\s+|^-?\s*\*?\s*Page\s+\d+", line, re.IGNORECASE):
                title = re.sub(r"^[-*\d.\s]+", "", line).strip()
                pages.append({
                    "page_num": i + 1, "title": title,
                    "filename": f"slide_{i+1:02d}.svg",
                    "layout_hint": "content", "key_points": title,
                })
        if pages:
            return pages

        # 回退: 从标题数推断
        headings = re.findall(r"^#{1,3}\s+\S", spec_lock, re.MULTILINE)
        estimated = max(3, min(len(headings), 20))
        logger.warning("page_plan 解析失败, 从标题推断 %d 页", estimated)
        return [
            {"page_num": i, "title": f"第{i}页", "filename": f"slide_{i:02d}.svg",
             "layout_hint": "cover" if i == 1 else ("end" if i == estimated else "content"),
             "key_points": ""}
            for i in range(1, estimated + 1)
        ]

    @staticmethod
    def _extract_bg_color(spec_lock: str) -> str:
        """从 spec_lock 中提取背景色，找不到则返回白色"""
        # 匹配 background: #XXX 或 bg_color: #XXX 或 background_color: ...
        m = re.search(r'(?:background|bg[_ ]?color)\s*[:=]\s*["\']?\s*(#[0-9a-fA-F]{3,8})', spec_lock, re.IGNORECASE)
        if m:
            return m.group(1)
        # 匹配 color_scheme 里的 background 字段
        m = re.search(r'"background"\s*:\s*"(#[0-9a-fA-F]{3,8})"', spec_lock)
        if m:
            return m.group(1)
        return "#FFFFFF"

    def _generate_svg_page(
        self, spec_lock: str, page_info: dict, page_idx: int, total: int, source_text: str = "", *, enable_thinking: bool = False
    ) -> str:
        """调用 LLM 为单页生成 SVG"""
        fmt = "ppt43" if "ppt43" in spec_lock else "ppt169"
        W, H = ("960", "720") if fmt == "ppt43" else ("1280", "720")

        system = (
            "你是PPT执行者(Executor)，负责生成单个SVG页面。\n\n"
            "## SVG技术规范(必须严格遵守)\n"
            f"{self._shared_standards[:6000]}\n\n"
            "## 执行者通用指南\n"
            f"{self._executor_base[:4000]}\n\n"
            "## 通用风格指南\n"
            f"{self._executor_general[:3000]}\n"
        )

        # 从源内容中提取与当前页相关的段落
        source_excerpt = ""
        if source_text:
            title = page_info.get("title", "")
            kp = page_info.get("key_points", "")
            # 优先取包含标题/关键词的段落，最多3000字
            relevant = []
            for para in source_text.split("\n\n"):
                if any(kw in para for kw in title.split() + kp.split(",")[:5]) or not relevant:
                    relevant.append(para)
                if len("\n\n".join(relevant)) > 3000:
                    break
            source_excerpt = "\n\n".join(relevant[:8])[:3000]

        user_msg = (
            f"请为以下页面生成完整的SVG代码。\n\n"
            f"## 当前项目 spec_lock\n{spec_lock[:8000]}\n\n"
            f"## 当前页信息\n"
            f"- 页码: {page_info.get('page_num', page_idx+1)}/{total}\n"
            f"- 标题: {page_info.get('title', '')}\n"
            f"- 布局提示: {page_info.get('layout_hint', 'content')}\n"
            f"- 要点: {page_info.get('key_points', '')}\n\n"
        )
        if source_excerpt:
            user_msg += (
                f"## 源内容参考(必须从中提取实际文字填入SVG)\n{source_excerpt}\n\n"
                "**重要**: 必须从上面源内容中提取真实的文字内容填入SVG的<text>元素，"
                "不要自己编造内容。每页至少包含3-5个文字元素（标题、副标题、要点、数据等）。\n\n"
            )
        user_msg += (
            f"## 要求\n"
            f"- SVG viewBox='0 0 {W} {H}', xmlns='http://www.w3.org/2000/svg'\n"
            f"- 使用spec_lock中定义的颜色、字体\n"
            f"- 必须有 id='page_{page_info.get('page_num', page_idx+1)}' 的顶层 <g> 元素\n"
            f"- **必须包含背景**: 第一个子元素必须是铺满整个viewBox的<rect>(x=0 y=0 width={W} height={H}), 使用spec_lock中的背景色或渐变\n"
            f"- 文字使用 <text> 元素, 不用 <foreignObject>\n"
            f"- 只输出SVG代码, 不要任何解释\n\n"
            f"## 禁止事项(会导致生成失败)\n"
            f"- 禁止使用 <animate>, <animateTransform>, <animateMotion> 元素\n"
            f"- 禁止使用 <foreignObject> 元素\n"
            f"- 禁止使用 <use> 元素（svg_to_pptx不支持）\n"
            f"- 禁止中英文混用: 源内容是中文则所有文字必须是中文\n"
            f"- 禁止自行添加英文翻译或英文注释\n"
            f"- 禁止让图表/图形超出 viewBox 范围, 所有坐标和尺寸必须控制在 0~{W}(宽) 和 0~{H}(高) 之间\n"
        )

        # 封面页特殊要求
        if page_info.get("layout_hint") == "cover" or page_idx == 0:
            user_msg += (
                "\n## 封面页特殊要求\n"
                "- 必须包含: 主标题(<text>元素,字号≥36px), 副标题(<text>元素,字号≥18px)\n"
                "- 主标题文字来自源内容主题, 副标题可以是日期/单位等\n"
                "- 封面背景必须充满整个viewBox(width=100% height=100% 或等效坐标)\n"
                "- 绝对不能只有背景没有文字\n"
            )

        resp = self.llm.chat(
            [{"role": "user", "content": user_msg}],
            system_prompt=system,
            enable_thinking=enable_thinking,
        )
        return self._extract_svg(resp)

    # ══════════════════════════════════════════════════════════
    #  SVG 修复 (弱模型常见错误)
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _fix_svg(svg: str) -> str:
        """修复弱模型生成的常见 SVG 格式错误。

        已知问题:
        - id 属性值含空格 (XML 不允许)  → id="edit 1" → id="edit_1"
        - 属性值缺少引号              → xmlns=http://... → xmlns="http://..."
        - circle 用了 x/y 而非 cx/cy → 修正属性名
        - rect 用了 x2/y2 而非 width/height → 移除非法属性
        - font-family 值溢出引号      → "SimSun" serif → "SimSun, serif"
        - <g> 标签未闭合              → 补全 </g>
        - <use> 元素                   → 移除 (svg_to_pptx 不支持)
        - <animate*> 元素              → 移除
        """
        # 1. 移除 animate / animateTransform / animateMotion / use
        svg = re.sub(r'<animate(?:Transform|Motion)?\b[^>]*/>', '', svg, flags=re.DOTALL)
        svg = re.sub(r'<animate(?:Transform|Motion)?\b[^>]*>.*?</animate(?:Transform|Motion)?>', '', svg, flags=re.DOTALL)
        svg = re.sub(r'<use\b[^>]*/>', '', svg, flags=re.DOTALL)
        svg = re.sub(r'<use\b[^>]*>.*?</use>', '', svg, flags=re.DOTALL)

        # 2. 修复 id 属性中的空格: id="edit 1" → id="edit_1"
        def _fix_id(m):
            val = m.group(1)
            if ' ' in val:
                return f'id="{val.replace(" ", "_")}"'
            return m.group(0)
        svg = re.sub(r'id="([^"]*)"', _fix_id, svg)

        # 3. 修复属性值缺少引号: xmlns=http://... → xmlns="http://..."
        # 匹配 属性名=非引号值 (后面跟空格或>)
        svg = re.sub(
            r'(\b\w+)=([^"\s>]+)([\s>])',
            lambda m: f'{m.group(1)}="{m.group(2)}"{m.group(3)}',
            svg,
        )

        # 4. circle 的 x/y 属性修正为 cx/cy
        svg = re.sub(r'<circle\b([^>]*)\bx=', lambda m: m.group(0).replace('x=', 'cx='), svg)
        svg = re.sub(r'<circle\b([^>]*)\by=', lambda m: m.group(0).replace('y=', 'cy='), svg)

        # 5. 移除 rect 的非法 x2/y2 属性
        svg = re.sub(r'(<rect\b[^>]*?)\s+x2="[^"]*"', r'\1', svg)
        svg = re.sub(r'(<rect\b[^>]*?)\s+y2="[^"]*"', r'\1', svg)

        # 6. 修复 font-family 值溢出引号
        # "Microsoft YaHei, SimSun" serif → "Microsoft YaHei, SimSun, serif"
        def _fix_font_family(m):
            prefix = m.group(1)
            quoted = m.group(2)
            trailing = m.group(3)
            if trailing.strip():
                # 把引号外的部分追加到引号内
                inner = quoted.rstrip('"')
                return f'{prefix}{inner} {trailing.strip()}"'
            return m.group(0)
        svg = re.sub(
            r'(font-family=")([^"]*")(\s+[^<]*?)(?=[\s/])',
            _fix_font_family,
            svg,
        )

        # 7. 补全未闭合的 <g> 标签
        open_g = len(re.findall(r'<g\b[^>]*/?>', svg)) - len(re.findall(r'<g\b[^>]*/>', svg))  # 排除自闭合
        open_g = len(re.findall(r'<g\b[^>]*>(?!.*?/>)', svg))
        close_g = len(re.findall(r'</g>', svg))
        if open_g > close_g:
            # 在 </svg> 前补全缺失的 </g>
            svg = svg.replace('</svg>', '</g>' * (open_g - close_g) + '\n</svg>')

        return svg

    # ══════════════════════════════════════════════════════════
    #  Notes
    # ══════════════════════════════════════════════════════════

    def _generate_notes(self, spec_lock: str, source_text: str, topic: str, *, enable_thinking: bool = False) -> str:
        system = "你是PPT演讲备注撰写专家。为每页PPT撰写简洁的演讲备注。"
        user_msg = (
            f"为以下PPT生成演讲备注(total.md格式)。\n\n"
            f"## 主题: {topic}\n"
            f"## spec_lock 页面计划\n{spec_lock[:5000]}\n\n"
            "格式: 每页以 '## 第N页: 标题' 开头, 然后写2-4句备注。\n"
            "最后用 '## 全文备注' 汇总所有页。"
        )
        return self.llm.chat(
            [{"role": "user", "content": user_msg}],
            system_prompt=system,
            enable_thinking=enable_thinking,
        )

    # ══════════════════════════════════════════════════════════
    #  辅助
    # ══════════════════════════════════════════════════════════

    @staticmethod
    def _split_strategist_response(resp: str) -> tuple[dict, str, str]:
        """拆分 Strategist 返回的三部分: Eight Confirmations + design_spec + spec_lock"""
        # 默认值
        eight_cf = {}
        design_spec = ""
        spec_lock = ""

        # 提取 Eight Confirmations JSON
        ec_match = re.search(r"===EIGHT_CONFIRMATIONS===\s*\n(.*?)(?====DESIGN_SPEC===|\Z)", resp, re.DOTALL)
        if ec_match:
            ec_text = ec_match.group(1).strip()
            # 提取 JSON 块
            json_match = re.search(r"\{.*\}", ec_text, re.DOTALL)
            if json_match:
                try:
                    eight_cf = json.loads(json_match.group())
                except json.JSONDecodeError:
                    logger.warning("Eight Confirmations JSON 解析失败")

        # 补全缺失字段为合理默认值（弱模型可能返回不完整的JSON）
        _EC_DEFAULTS = {
            "canvas_format": "PPT 16:9 (1280×720)",
            "page_count": {"min": 5, "max": 15, "recommended": 8},
            "target_audience": "领导/高管",
            "style_objective": "B) General Consulting — 数据清晰优先",
            "color_scheme": {
                "primary": "#1A56DB", "secondary": "#0E9F6E",
                "accent": "#F59E0B", "background": "#0F172A",
                "text": "#F8FAFC", "description": "专业商务深蓝配色",
            },
            "icon_usage": "C) Built-in icon library — 专业场景(推荐)",
            "typography": {
                "heading_font": "Microsoft YaHei, PingFang SC, sans-serif",
                "heading_size": "32-40px",
                "body_font": "Microsoft YaHei, PingFang SC, sans-serif",
                "body_size": "18-22px",
                "description": "微软雅黑系列，清晰专业",
            },
            "image_usage": "A) No images — 数据报告、流程文档",
        }
        for k, v in _EC_DEFAULTS.items():
            if k not in eight_cf:
                eight_cf[k] = v
            elif isinstance(v, dict) and isinstance(eight_cf.get(k), dict):
                for sk, sv in v.items():
                    if sk not in eight_cf[k]:
                        eight_cf[k][sk] = sv

        # 提取 design_spec
        ds_match = re.search(r"===DESIGN_SPEC===\s*\n(.*?)(?====SPEC_LOCK===|\Z)", resp, re.DOTALL)
        if ds_match:
            design_spec = ds_match.group(1).strip()

        # 提取 spec_lock
        sl_match = re.search(r"===SPEC_LOCK===\s*\n(.*)", resp, re.DOTALL)
        if sl_match:
            spec_lock = sl_match.group(1).strip()

        # 回退: 如果没找到标记，用旧逻辑
        if not spec_lock and not design_spec:
            design_spec, spec_lock = PPTEngine._split_response(resp)

        return eight_cf, design_spec, spec_lock

    @staticmethod
    def _split_response(resp: str) -> tuple[str, str]:
        """旧逻辑回退: 拆分 design_spec + spec_lock"""
        sep = "===SPEC_LOCK==="
        if sep in resp:
            parts = resp.split(sep, 1)
            return parts[0].replace("===DESIGN_SPEC===", "").strip(), parts[1].strip()
        mid = len(resp) // 2
        return resp[:mid], resp[mid:]

    @staticmethod
    def _extract_svg(text: str) -> str:
        if "<svg" in text:
            start = text.index("<svg")
            end = text.rindex("</svg>") + 6
            return text[start:end]
        m = re.search(r"```(?:xml|svg)?\s*\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return text.strip()


def _make_progress(progress: Optional[Callable] = None) -> Callable:
    """创建进度回调"""
    def _prog(stage: str, msg: str, pct: int):
        logger.info("[%s] %s (%d%%)", stage, msg, pct)
        if progress:
            progress(stage, msg, pct)
    return _prog
