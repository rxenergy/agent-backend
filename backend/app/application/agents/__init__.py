# Importing each variant module here triggers its `@register_variant(...)`
# decorator (ADR-0004). Adding a new variant = new module + import line here +
# `variants/registry.yaml` entry — no edits to `config/profiles.py`.
from app.application.agents import fake_echo_v0  # noqa: F401
from app.application.agents import sequential_tool_routed_v2  # noqa: F401
