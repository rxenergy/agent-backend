from __future__ import annotations

import hashlib
from typing import Any, Protocol

import structlog

from app.application.prompting.resolver import (
    FRAGMENT_KEYS,
    FragmentRef,
    PromptProfile,
)


_log = structlog.get_logger("prompting.phoenix")


class PhoenixPromptClient(Protocol):
    """Narrow protocol over an arize-phoenix prompt management client.

    Defined here rather than importing `phoenix.client` so:
      * the local profile can boot without the `arize-phoenix-client` extra,
      * unit tests can inject a fake without monkeypatching Phoenix.

    A concrete adapter (`build_phoenix_client`) constructs the real client only
    when `PROMPT_SOURCE` selects a Phoenix-backed source.
    """

    def list_profiles(self, *, label: str) -> list[dict[str, Any]]:
        ...

    def get_fragment(self, *, profile_id: str, name: str, label: str) -> dict[str, Any]:
        ...


class PhoenixPromptSource:
    """Prompt source backed by Phoenix prompt management.

    Fragments returned by `get_fragment` MUST already carry a sha256 of their
    own content. The source recomputes the digest defensively and rejects any
    upstream drift, matching `LocalPromptSource`'s invariant.
    """

    source_id = "phoenix"

    def __init__(self, client: PhoenixPromptClient, *, label: str = "mvp") -> None:
        self._client = client
        self._label = label
        self._by_id: dict[str, PromptProfile] = {}
        self._by_scenario: dict[tuple[str, str], str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        for body in self._client.list_profiles(label=self._label):
            profile = self._build_profile(body)
            self._by_id[profile.profile_id] = profile
            self._by_scenario[
                (profile.scenario_object, profile.scenario_depth)
            ] = profile.profile_id
        self._loaded = True

    def _build_profile(self, body: dict[str, Any]) -> PromptProfile:
        profile_id = body["profile_id"]
        fragments: dict[str, FragmentRef] = {}
        for name in FRAGMENT_KEYS:
            if name not in (body.get("fragments") or {}):
                continue
            fragments[name] = self._load_fragment(profile_id, name)
        output_schema = self._load_fragment(profile_id, "output_schema")
        return PromptProfile(
            profile_id=profile_id,
            profile_version=str(body.get("profile_version") or "v1"),
            scenario_object=body["scenario_object"],
            scenario_depth=body["scenario_depth"],
            fragments=fragments,
            output_schema=output_schema,
            model_options=dict(body.get("model_options") or {}),
            source=self.source_id,
        )

    def _load_fragment(self, profile_id: str, name: str) -> FragmentRef:
        payload = self._client.get_fragment(
            profile_id=profile_id, name=name, label=self._label
        )
        content: str = payload["content"]
        declared = payload.get("sha256")
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if declared and declared != actual:
            raise RuntimeError(
                f"phoenix fragment sha mismatch profile={profile_id} name={name}: "
                f"declared={declared[:12]}…, actual={actual[:12]}…"
            )
        return FragmentRef(
            name=name,
            path=payload.get("path") or f"phoenix://{profile_id}/{name}",
            version=str(payload.get("version") or "v1"),
            sha256=actual,
            content=content,
        )

    # ----------------- PromptSourcePort ------------------------------------
    def resolve(self, scenario_object: str, scenario_depth: str) -> PromptProfile | None:
        self._ensure_loaded()
        profile_id = self._by_scenario.get((scenario_object, scenario_depth))
        if profile_id is None:
            return None
        return self._by_id[profile_id]

    def all_profiles(self) -> list[PromptProfile]:
        self._ensure_loaded()
        return list(self._by_id.values())


def build_phoenix_client(endpoint: str, *, api_key: str | None = None) -> PhoenixPromptClient:
    """Construct a real `phoenix.client` adapter, importing lazily.

    Raises `ImportError` if the `arize-phoenix-client` extra is not installed —
    callers should catch this and fall back (or refuse to boot) per profile.
    """
    try:
        from phoenix.client import Client  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via integration
        raise ImportError(
            "arize-phoenix-client is not installed; "
            "install the `phoenix` extra to use PROMPT_SOURCE=phoenix"
        ) from exc

    _client = Client(endpoint=endpoint, api_key=api_key) if api_key else Client(endpoint=endpoint)

    class _Adapter:
        source_id = "phoenix"

        def list_profiles(self, *, label: str) -> list[dict[str, Any]]:
            return list(_client.prompts.list(tag=label))  # type: ignore[attr-defined]

        def get_fragment(
            self, *, profile_id: str, name: str, label: str
        ) -> dict[str, Any]:
            return dict(  # type: ignore[arg-type]
                _client.prompts.get(  # type: ignore[attr-defined]
                    name=f"{profile_id}.{name}", tag=label
                )
            )

    return _Adapter()
