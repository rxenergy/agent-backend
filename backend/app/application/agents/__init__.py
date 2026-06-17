# Importing each variant module here triggers its `@register_variant(...)`
# decorator (ADR-0004). Adding a new variant = new module + import line here +
# `variants/registry.yaml` entry — no edits to `config/profiles.py`.
from app.application.agents import spec_driven_v1  # noqa: F401
from app.application.agents import spec_driven_v2  # noqa: F401
