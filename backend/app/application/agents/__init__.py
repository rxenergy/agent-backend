# Importing each variant module here triggers its `@register_variant(...)`
# decorator (ADR-0004). Adding a new variant = new module + import line here +
# `variants/registry.yaml` entry — no edits to `config/profiles.py`.
from app.application.agents import composer  # noqa: F401
from app.application.agents import composer_pipelined  # noqa: F401
