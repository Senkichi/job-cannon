"""Provider adapter implementations.

Concrete providers live in sibling modules and are imported lazily by
``job_finder.web.model_provider._make_adapter`` (one branch per provider).
There is no canonical package-root entry point — always import the
specific module:

    from job_finder.web.providers.ollama_provider import OllamaProvider
"""
