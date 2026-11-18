"""
OpenAI 兼容 LLM 客户端模块

封装 openai 库，提供 chat 和 chat_json 两种调用方式，
内置重试机制和错误处理。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from openai import OpenAI, APIError, APIConnectionError, RateLimitError

from .config import LLMConfig

logger = logging.getLogger(__name__)

# 最大重试次数
MAX_RETRIES = 3
# 重试间隔（秒）
RETRY_DELAY = 2.0
# 重试间隔倍增因子
RETRY_BACKOFF = 2.0


class LLMTruncatedError(RuntimeError):
    """模型输出因 max_tokens/上下文限制被截断。

    保留已生成的部分文本，便于上层决定是重试、精简提示词还是降级处理。
    """

    def __init__(self, partial_text: str, max_tokens: Optional[int]) -> None:
        self.partial_text = partial_text
        self.max_tokens = max_tokens
        super().__init__(
            f"模型输出被截断 (finish_reason=length, max_tokens={max_tokens}, "
            f"已生成 {len(partial_text)} 字符)"
        )


def _is_qwen38(model: str) -> bool:
    """识别 Qwen3.8 系列（含 Qwen/Qwen3.8-27B-FP8）。"""
    return bool(re.search(r"qwen[\s._/-]*3[\s._-]*8", model, re.IGNORECASE))


class LLMClient:
    """OpenAI 兼容 LLM 客户端

    封装 OpenAI API 调用，提供同步 chat 和 chat_json 方法。
    内部处理流式响应、重试和错误。

    Args:
        config: LLM API 配置对象
    """

    def __init__(self, config: LLMConfig) -> None:
        if not config.is_configured():
            raise ValueError("LLM 未配置：请先设置 api_key")

        self.config = config
        self.client = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self.model = config.model_name
        # 弱模型标记: 来自配置 or 自动检测
        self.is_weak = config.is_weak
        # Qwen3.8 模型画像: 原生 262144 上下文；模型卡建议最终回答 131072，这里保守取 128000
        self.is_qwen38 = _is_qwen38(self.model)
        # 上下文长度限制 (0=不限)；Qwen3.8 未显式配置时使用模型能力
        self.max_context = config.max_context or (262144 if self.is_qwen38 else 0)
        # 单次输出上限 (0=使用各调用场景的默认值)
        self.max_output_limit = max(
            0, config.max_output or (128000 if self.is_qwen38 else 0)
        )
        # 思考深度: Qwen3.8 模板默认 xhigh，这里默认 medium 以平衡质量与耗时
        effort = config.reasoning_effort or ("medium" if self.is_qwen38 else "")
        self.reasoning_effort = effort if effort in ("xhigh", "medium", "low") else (
            "medium" if self.is_qwen38 else ""
        )
        # 思考开关透传方式: vLLM/SGLang=chat_template, Qwen Cloud=top_level
        style = config.thinking_param_style or "chat_template"
        self.thinking_param_style = (
            style if style in ("chat_template", "top_level", "both") else "chat_template"
        )
        logger.info(
            "LLM 客户端初始化完成: base_url=%s, model=%s, is_weak=%s, qwen3.8=%s, "
            "max_context=%s, max_output=%s, reasoning_effort=%s, thinking_style=%s",
            config.base_url,
            config.model_name,
            self.is_weak,
            self.is_qwen38,
            self.max_context or "auto",
            self.max_output_limit or "scene-default",
            self.reasoning_effort or "model-default",
            self.thinking_param_style,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        enable_thinking: bool = False,
        max_output: int = 16384,
    ) -> str:
        """调用 LLM 进行对话

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}]
            system_prompt: 可选的系统提示词
            enable_thinking: 是否启用思考模式
            max_output: 场景期望的最大输出tokens上限 (SVG单页=16384, Strategist=32768);
                        若配置了 LLM_MAX_OUTPUT 则以该全局值为准

        Returns:
            模型回复的文本内容

        Raises:
            RuntimeError: 重试次数用尽后仍失败
        """
        full_messages = self._build_messages(messages, system_prompt)
        return self._call_with_retry(full_messages, enable_thinking=enable_thinking, max_output=max_output)

    def chat_json(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
        enable_thinking: bool = False,
        max_output: int = 16384,
    ) -> Any:
        """调用 LLM 并解析 JSON 响应"""
        system_suffix = "\n\n请以纯 JSON 格式回复，不要包含 markdown 代码块标记。"
        effective_system = (system_prompt or "") + system_suffix

        full_messages = self._build_messages(messages, effective_system)
        response_text = self._call_with_retry(full_messages, enable_thinking=enable_thinking, max_output=max_output)

        return self._extract_json(response_text)

    def _build_messages(
        self,
        messages: list[dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """构建完整的消息列表（含系统提示词）"""
        result: list[dict[str, str]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend(messages)
        return result

    def _estimate_tokens(self, messages: list[dict[str, str]]) -> int:
        """粗略估算消息的token数。

        中文/CJK 约 1 token/字，ASCII 约 4 字符/token。
        旧公式按 1.5 字符/token 估算，对以中文提示词为主的场景会低估输入，
        从而把输出预算算得过大。
        """
        total_chars = 0
        cjk_chars = 0
        for m in messages:
            content = m.get("content", "") or ""
            total_chars += len(content)
            cjk_chars += len(re.findall(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]", content))
        other_chars = max(0, total_chars - cjk_chars)
        return int(cjk_chars + other_chars / 4) + 200  # +200 for message overhead

    def _calc_max_tokens(self, messages: list[dict[str, str]], max_output: int = 16384) -> Optional[int]:
        """根据上下文长度和输出上限计算 max_tokens。

        - LLM_MAX_OUTPUT > 0 时作为全局输出上限（覆盖场景默认值）
        - LLM_MAX_CONTEXT > 0 时: max_tokens = max_context - input_tokens - safety_margin
        - 两者都为 0 时返回 None（保持旧行为，由 API 自行决定）

        Args:
            messages: 消息列表
            max_output: 调用方期望的最大输出tokens上限
        """
        # 全局输出上限优先；否则使用场景默认值
        budget = self.max_output_limit or max_output
        if budget <= 0:
            return None

        if not self.max_context:
            # 未配置上下文长度时：只有显式配置了输出上限才发送 max_tokens，
            # 避免向不支持大输出的模型传入非法值。
            return self.max_output_limit or None

        input_tokens = self._estimate_tokens(messages)
        safety_margin = 500  # 留出安全余量
        available = self.max_context - input_tokens - safety_margin

        if available <= 0:
            logger.warning(
                "输入已超过上下文限制! input≈%d, max_context=%d, 强制max_tokens=1024",
                input_tokens, self.max_context,
            )
            return 1024

        # 下限1024, 上限由输出预算决定
        return max(1024, min(available, budget))

    def _build_request_params(
        self,
        messages: list[dict[str, str]],
        enable_thinking: bool,
        max_output: int,
    ) -> dict[str, Any]:
        """按模型类型组装请求参数。

        Qwen3.8 依据 ModelScope 模型卡:
          - 思考默认开启，必须显式传 enable_thinking=false 才能关闭
          - 思考深度 reasoning_effort: xhigh/medium/low
          - 思考模式推荐 temperature=1.0/top_p=0.95，
            非思考推荐 temperature=0.7/top_p=0.80/presence_penalty=1.5
          - vLLM/SGLang 用 chat_template_kwargs，Qwen Cloud 用顶层参数
        """
        thinking = bool(enable_thinking)
        extra_body: dict[str, Any] = {}
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,  # type: ignore
            "stream": False,
        }

        if self.is_qwen38:
            if thinking:
                params["temperature"] = 1.0
                params["top_p"] = 0.95
                params["presence_penalty"] = 0.0
            else:
                params["temperature"] = 0.7
                params["top_p"] = 0.80
                params["presence_penalty"] = 1.5
            extra_body["top_k"] = 20

            if self.thinking_param_style in ("chat_template", "both"):
                extra_body["chat_template_kwargs"] = {
                    "enable_thinking": thinking,
                    # 单轮生成不需要保留历史思考，减少上下文占用
                    "preserve_thinking": False,
                }
            if self.thinking_param_style in ("top_level", "both"):
                extra_body["enable_thinking"] = thinking
                extra_body["preserve_thinking"] = False
            if thinking and self.reasoning_effort:
                extra_body["reasoning_effort"] = self.reasoning_effort
        else:
            # 其它 OpenAI 兼容模型保持原有行为
            params["temperature"] = 0.7
            extra_body["chat_template_kwargs"] = {"enable_thinking": thinking}

        max_tokens = self._calc_max_tokens(messages, max_output=max_output)
        if max_tokens:
            params["max_tokens"] = max_tokens
            logger.debug("max_context=%d, max_tokens=%d", self.max_context, max_tokens)
        params["extra_body"] = extra_body
        return params

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """移除模型输出中的思考块；未闭合的思考块视为没有最终回答。"""
        if not text:
            return ""
        cleaned = re.sub(
            r"<think\b[^>]*>.*?</think\s*>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # 只剩未闭合的 <think>：说明输出在思考阶段就被截断了
        if re.match(r"^\s*<think\b", cleaned, re.IGNORECASE):
            return ""
        cleaned = re.sub(r"</?think\b[^>]*>", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _call_with_retry(
        self,
        messages: list[dict[str, str]],
        enable_thinking: bool = False,
        max_output: int = 16384,
    ) -> str:
        """带重试机制的 API 调用"""
        last_error: Optional[Exception] = None
        delay = RETRY_DELAY

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(
                    "API 调用 (尝试 %d/%d), messages=%d 条, thinking=%s",
                    attempt,
                    MAX_RETRIES,
                    len(messages),
                    enable_thinking,
                )
                kwargs = self._build_request_params(messages, enable_thinking, max_output)
                response = self.client.chat.completions.create(**kwargs)
                if not response.choices:
                    raise RuntimeError("模型返回了空响应（choices 为空）")

                choice = response.choices[0]
                message = choice.message
                finish_reason = choice.finish_reason
                used_max_tokens = kwargs.get("max_tokens")

                raw_content = message.content or ""
                reasoning = (
                    getattr(message, "reasoning_content", None)
                    or getattr(message, "reasoning", None)
                    or ""
                )
                content = self._strip_thinking(raw_content)
                partial = content or reasoning or raw_content

                usage = getattr(response, "usage", None)
                details = getattr(usage, "completion_tokens_details", None)
                logger.info(
                    "API 调用成功: finish_reason=%s, content=%d 字符, reasoning=%d 字符, "
                    "max_tokens=%s, prompt_tokens=%s, completion_tokens=%s, reasoning_tokens=%s",
                    finish_reason,
                    len(content),
                    len(reasoning),
                    used_max_tokens if used_max_tokens is not None else "auto",
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "completion_tokens", None),
                    getattr(details, "reasoning_tokens", None),
                )

                # 输出被截断时不能静默返回半成品：上层需要知道并决定重试/精简/降级。
                if finish_reason in ("length", "max_tokens"):
                    logger.warning(
                        "模型输出被截断: model=%s, thinking=%s, max_tokens=%s, 已生成 %d 字符。"
                        "请检查 LLM_MAX_CONTEXT/LLM_MAX_OUTPUT 是否小于模型实际能力。",
                        self.model, enable_thinking, used_max_tokens, len(partial),
                    )
                    raise LLMTruncatedError(partial.strip(), used_max_tokens)

                if not content:
                    if reasoning:
                        raise RuntimeError(
                            "模型只返回了思考内容，没有最终回答"
                            "（思考可能占满了 max_tokens，可降低 LLM_REASONING_EFFORT 或增大输出上限）"
                        )
                    raise RuntimeError("模型返回了空响应")

                return content

            except LLMTruncatedError:
                # 截断不是网络/服务端错误，重试同一预算通常无意义，交给上层处理。
                raise
            except RateLimitError as e:
                last_error = e
                logger.warning("触发速率限制 (尝试 %d/%d): %s", attempt, MAX_RETRIES, e)
            except APIConnectionError as e:
                last_error = e
                logger.warning("连接错误 (尝试 %d/%d): %s", attempt, MAX_RETRIES, e)
            except APIError as e:
                last_error = e
                logger.warning("API 错误 (尝试 %d/%d): %s", attempt, MAX_RETRIES, e)
            except Exception as e:
                last_error = e
                logger.error("未知错误 (尝试 %d/%d): %s", attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                logger.info("等待 %.1f 秒后重试...", delay)
                time.sleep(delay)
                delay *= RETRY_BACKOFF

        raise RuntimeError(
            f"LLM API 调用失败，已重试 {MAX_RETRIES} 次: {last_error}"
        )

    @staticmethod
    def _extract_json(text: str) -> Any:
        """从文本中提取 JSON

        支持纯 JSON、markdown 代码块包裹的 JSON、以及前后有说明文字的 JSON。

        Args:
            text: 可能包含 JSON 的文本

        Returns:
            解析后的 Python 对象

        Raises:
            ValueError: 无法解析 JSON
        """
        # 尝试直接解析
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 markdown 代码块中的 JSON
        json_block_pattern = re.compile(
            r"```(?:json)?\s*\n?(.*?)\n?\s*```",
            re.DOTALL,
        )
        match = json_block_pattern.search(text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试提取第一个 { ... } 或 [ ... ] 块
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start_idx = text.find(start_char)
            if start_idx == -1:
                continue
            # 从末尾找最后一个匹配的结束符
            end_idx = text.rfind(end_char)
            if end_idx <= start_idx:
                continue
            try:
                return json.loads(text[start_idx : end_idx + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法从响应中解析 JSON: {text[:200]}...")
