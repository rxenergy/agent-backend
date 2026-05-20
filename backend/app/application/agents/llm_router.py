from __future__ import annotations

from app.ports.llm import LLMPort


class UnknownLLMError(KeyError):
    """`llm_id` is not registered in the pool."""


class LLMRouter:
    """Application-layer dispatcher that maps a `llm_id` string to a concrete
    `LLMPort` instance. The pool is constructed once at boot
    (`profiles._build_llm_pool`); the router only does lookups."""

    def __init__(self, pool: dict[str, LLMPort], default_id: str) -> None:
        if default_id not in pool:
            raise ValueError(
                f"default_id={default_id!r} not in pool ids={sorted(pool)}"
            )
        self._pool = dict(pool)
        self._default_id = default_id

    @property
    def default_id(self) -> str:
        return self._default_id

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._pool.keys())

    def has(self, llm_id: str) -> bool:
        return llm_id in self._pool

    def resolve(self, llm_id: str | None) -> tuple[str, LLMPort]:
        resolved = llm_id or self._default_id
        try:
            return resolved, self._pool[resolved]
        except KeyError as exc:
            raise UnknownLLMError(resolved) from exc
