from __future__ import annotations

from pathlib import Path

from ...config import LLMConfig
from .base import BugVerdict, LLMAdapter, TestabilityVerdict, TestGenerationTask
from .claude_code import ClaudeCodeAdapter


def get_llm_adapter(llm_config: LLMConfig, usage_log_path: Path | None = None) -> LLMAdapter:
    """llm_config.provider に対応する LLMAdapter インスタンスを返す。未対応は ValueError。"""
    if llm_config.provider == "claude_code":
        return ClaudeCodeAdapter(llm_config.claude_code, usage_log_path=usage_log_path)
    raise ValueError(f"Unknown LLM provider: {llm_config.provider!r}")


__all__ = ["BugVerdict", "LLMAdapter", "TestabilityVerdict", "TestGenerationTask", "ClaudeCodeAdapter", "get_llm_adapter"]
