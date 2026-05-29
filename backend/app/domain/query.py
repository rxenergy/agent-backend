from __future__ import annotations

from dataclasses import dataclass, field

# v3.1 (hierarchical_corrective) — Phase A Query Understanding domain models.
#
# These are frozen dataclasses (not pydantic) because `QueryPlan` reproducibility
# fields are surfaced into `InteractionEvent` via `dataclasses.asdict()`, which
# only recurses into dataclasses / dict / list / tuple. A pydantic model left in
# an event field would be stringified to its repr by `json.dumps(default=str)`.
# See `domain/interaction.py` ToolCallRecord for the established pattern.


@dataclass(frozen=True)
class SubQuestion:
    """One decomposed sub-question (Node 3, multi-intent path)."""

    id: str
    text: str
    entities: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryPlan:
    """Node 3 output — normalized entities, optional sub-questions, intents,
    and the version (effective_on) constraint that downstream Hard gate (G3)
    and Claim version-match step consume.

    `decompose_prompt_hash` is populated only when the multi-intent LLM
    decomposition actually ran — its presence in the event records that an
    LLM call occurred at this node (reproducibility)."""

    sub_questions: tuple[SubQuestion, ...] = ()
    normalized_entities: dict[str, list[str]] = field(default_factory=dict)
    intents: tuple[str, ...] = ()
    version_constraint: str | None = None  # e.g. "2024-06-01" (effective_on)
    multi_intent: bool = False
    ner_dict_version: str | None = None
    normalizer_version: str | None = None
    decompose_prompt_hash: str | None = None
