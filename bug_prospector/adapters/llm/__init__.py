from __future__ import annotations

from ...config import LLMConfig
from .base import LLMAdapter, TestGenerationTask
from .claude_code import ClaudeCodeAdapter


def get_llm_adapter(llm_config: LLMConfig) -> LLMAdapter:
    """llm_config.provider に対応する LLMAdapter インスタンスを返す。未対応は ValueError。"""
    if llm_config.provider == "claude_code":
        return ClaudeCodeAdapter(llm_config.claude_code)
    raise ValueError(f"Unknown LLM provider: {llm_config.provider!r}")


__all__ = ["LLMAdapter", "TestGenerationTask", "ClaudeCodeAdapter", "get_llm_adapter"]
