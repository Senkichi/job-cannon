"""Provider adapter implementations (added in Phase 25)."""

from job_finder.web.providers.anthropic_provider import AnthropicProvider
from job_finder.web.providers.gemini_provider import GeminiProvider
from job_finder.web.providers.ollama_provider import OllamaProvider
from job_finder.web.providers.openrouter_provider import OpenRouterProvider

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenRouterProvider",
]
