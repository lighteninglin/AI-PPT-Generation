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
        # 上下文长度限制 (0=不限)
        self.max_context = config.max_context
        logger.info(
            "LLM 客户端初始化完成: base_url=%s, model=%s, is_weak=%s, max_context=%s",
            config.base_url,
            config.model_name,
            self.is_weak,
            self.max_context or "auto",
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
            max_output: 最大输出tokens上限 (不同场景不同: SVG单页=16384, Strategist=32768)

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
        """粗略估算消息的token数（中文约1.5字/token，英文约4字符/token）"""
        total_chars = sum(len(m.get("content", "")) for m in messages)
        # 保守估计: 中文为主时约1.5字符/token
        return int(total_chars / 1.5) + 200  # +200 for message overhead

    def _calc_max_tokens(self, messages: list[dict[str, str]], max_output: int = 16384) -> Optional[int]:
        """根据上下文长度计算 max_tokens

        如果设置了 max_context, 则:
          max_tokens = max_context - input_tokens - safety_margin
        否则返回 None (由API自行决定)

        Args:
            messages: 消息列表
            max_output: 调用方期望的最大输出tokens上限
        """
        if not self.max_context:
            return None

        input_tokens = self._estimate_tokens(messages)
        safety_margin = 500  # 留出安全余量
        available = self.max_context - input_tokens - safety_margin

        if available <= 0:
            logger.warning(
                "输入已超过上下文限制! input≈%d, max_context=%d, 强制max_tokens=1024",
                input_tokens, self.max_context,
            )
            return 1024

        # 下限1024, 上限由调用方决定(不同场景输出量不同)
        return max(1024, min(available, max_output))

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
                    "API 调用 (尝试 %d/%d), messages=%d 条",
                    attempt,
                    MAX_RETRIES,
                    len(messages),
                )
                kwargs = dict(
                    model=self.model,
                    messages=messages,  # type: ignore
                    temperature=0.7,
                    stream=False,
                    extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
                )
                max_tokens = self._calc_max_tokens(messages, max_output=max_output)
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                    logger.debug("max_context=%d, max_tokens=%d", self.max_context, max_tokens)
                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content is None:
                    raise RuntimeError("模型返回了空响应")
                # 记录max_tokens限制是否生效
                finish_reason = response.choices[0].finish_reason
                logger.info("API 调用成功，响应长度: %d 字符, finish_reason: %s, max_tokens限制: %s",
                           len(content), finish_reason,
                           kwargs.get("max_tokens", "auto"))
                return content.strip()

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
