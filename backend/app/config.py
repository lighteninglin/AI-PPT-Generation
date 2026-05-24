"""
LLM API 配置管理模块

优先级: 环境变量 > config.json 文件 > 默认值
环境变量在 .env 中由管理员配置，容器启动前设定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


# 配置文件路径（仅作环境变量的 fallback）
CONFIG_DIR = Path.home() / ".ppt-web"
CONFIG_FILE = CONFIG_DIR / "config.json"


class LLMConfig(BaseModel):
    """LLM API 配置模型"""

    api_key: str = Field(default="", description="API 密钥")
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="API 基础地址",
    )
    model_name: str = Field(
        default="gpt-4o",
        description="模型名称",
    )

    def is_configured(self) -> bool:
        return bool(self.api_key.strip())

    def masked_api_key(self) -> str:
        if not self.api_key:
            return ""
        key = self.api_key
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 8) + key[-4:]


class ConfigManager:
    """配置管理器 — 优先读环境变量"""

    @staticmethod
    def load() -> LLMConfig:
        """加载配置: 环境变量优先，其次 config.json"""
        # 先读 config.json 作为基础
        file_cfg = {}
        if CONFIG_FILE.exists():
            try:
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception):
                pass

        # 环境变量覆盖
        env_key = os.environ.get("LLM_API_KEY", "").strip()
        env_url = os.environ.get("LLM_BASE_URL", "").strip()
        env_model = os.environ.get("LLM_MODEL", "").strip()

        cfg = LLMConfig(
            api_key=env_key or file_cfg.get("api_key", ""),
            base_url=env_url or file_cfg.get("base_url", "https://api.openai.com/v1"),
            model_name=env_model or file_cfg.get("model_name", "gpt-4o"),
        )

        # 环境变量存在时，同步更新 config.json（避免显示旧值）
        if env_key or env_url or env_model:
            try:
                ConfigManager.save(cfg)
            except Exception:
                pass

        return cfg

    @staticmethod
    def save(config: LLMConfig) -> None:
        """保存配置到文件（环境变量存在时不建议使用）"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            config.model_dump_json(indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def is_env_configured() -> bool:
        """检查是否通过环境变量配置了 API Key"""
        return bool(os.environ.get("LLM_API_KEY", "").strip())

    @staticmethod
    def get_masked() -> dict:
        """获取脱敏的配置信息（用于 API 返回）"""
        config = ConfigManager.load()
        return {
            "api_key": config.masked_api_key(),
            "base_url": config.base_url,
            "model_name": config.model_name,
            "is_configured": config.is_configured(),
            "configured_via": "env" if ConfigManager.is_env_configured() else "file",
        }
