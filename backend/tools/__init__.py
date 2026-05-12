# backend/tools/__init__.py
# Allowed dependencies: core, models, config

from backend.tools.llm_client import LLMClient, LLMResponse, llm_client
from backend.tools.model_router import ModelConfig, get_model_config, get_all_configs
from backend.tools.tool_registry import ToolRegistry, ToolSchema, ToolDefinition, tool_registry
from backend.tools.sandbox import Sandbox, SandboxConfig, SandboxResult, SandboxViolationError
from backend.tools.capability_scope import (
    CapabilityScope,
    CapabilityViolationError,
    CAPABILITY_MAP,
    get_allowed_tools,
    check_capability,
    raise_if_not_allowed,
)

__all__ = [
    # LLM client
    "LLMClient",
    "LLMResponse",
    "llm_client",
    # Model router
    "ModelConfig",
    "get_model_config",
    "get_all_configs",
    # Tool registry (Phase 7)
    "ToolRegistry",
    "ToolSchema",
    "ToolDefinition",
    "tool_registry",
    # Sandbox (Phase 7)
    "Sandbox",
    "SandboxConfig",
    "SandboxResult",
    "SandboxViolationError",
    # Capability scope (Phase 7)
    "CapabilityScope",
    "CapabilityViolationError",
    "CAPABILITY_MAP",
    "get_allowed_tools",
    "check_capability",
    "raise_if_not_allowed",
]
