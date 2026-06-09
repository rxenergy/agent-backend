# Importing each variant module here triggers its `@register_variant(...)`
# decorator (ADR-0004). Adding a new variant = new module + import line here +
# `variants/registry.yaml` entry — no edits to `config/profiles.py`.
from app.application.agents import fake_echo_v0  # noqa: F401
from app.application.agents import hierarchical_corrective_v3_1  # noqa: F401
from app.application.agents import agentic_finder_v4  # noqa: F401
from app.application.agents import react_minimal_v1  # noqa: F401
from app.application.agents import react_echo_v1  # noqa: F401
from app.application.agents import spec_driven_v1  # noqa: F401
