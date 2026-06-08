from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.event_sink.filesystem import FilesystemEventSink
from app.adapters.llm.fake import FakeEchoLLM
from app.api import openai_compat
from app.api.openai_compat import _split_model_id
from app.application.agents.fake_echo_v0 import FakeEchoAgentRunner
from app.application.events.recorder import EventRecorder
from app.config.profiles import AppContainer
from app.config.settings import Settings
from app.domain.agents import VariantSpec

_FAKE_SPEC = VariantSpec(variant_id="fake_echo_v0", compatible_llms=("fake-echo",))


def _make_app(runners: dict, llm_pool: dict, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(openai_compat.router)
    container = AppContainer(
        settings=settings,
        runners=runners,
        llm_pool=llm_pool,
        event_sink=None,
    )
    app.state.container = container
    return app


@pytest.fixture()
def fake_app():
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
        recorder = EventRecorder(sink, app_profile="local")
        runners = {"fake_echo_v0": FakeEchoAgentRunner(recorder=recorder, spec=_FAKE_SPEC)}
        llm_pool = {"fake-echo": FakeEchoLLM(model_id="fake-echo")}
        settings = Settings(
            agent_variants_enabled=["fake_echo_v0"],
            default_variant="fake_echo_v0",
            default_llm="fake-echo",
            utility_llm="fake-echo",
        )
        yield _make_app(runners, llm_pool, settings)


def test_split_model_id_full():
    assert _split_model_id(
        "agentic_finder_v4@gpt-4o",
        default_variant="seq",
        default_llm="fake-echo",
    ) == ("agentic_finder_v4", "gpt-4o")


def test_split_model_id_empty_falls_back_to_defaults():
    assert _split_model_id("", default_variant="seq", default_llm="fake") == (
        "seq",
        "fake",
    )


def test_split_model_id_bare_variant_uses_default_llm():
    assert _split_model_id(
        "fake_echo_v0", default_variant="seq", default_llm="fake"
    ) == ("fake_echo_v0", "fake")


def test_split_model_id_bare_llm_falls_back_to_default_variant():
    """Bare id matching a known LLM (not a variant) pairs with default_variant.

    Convenience for OpenAI-compatible clients (e.g. OpenWebUI) that send
    bare LLM ids for side-by-side comparison.
    """
    assert _split_model_id(
        "claude-opus-4-7",
        default_variant="seq",
        default_llm="fake",
        known_llms=frozenset({"claude-opus-4-7", "fake"}),
        known_variants=frozenset({"seq", "fake_echo_v0"}),
    ) == ("seq", "claude-opus-4-7")


def test_split_model_id_bare_unknown_still_treated_as_variant():
    """A bare id that's neither a known LLM nor variant flows through as variant
    (preserving the explicit 400 unknown_variant signal)."""
    assert _split_model_id(
        "claude-haiku-4-6",
        default_variant="seq",
        default_llm="fake",
        known_llms=frozenset({"fake"}),
        known_variants=frozenset({"seq"}),
    ) == ("claude-haiku-4-6", "fake")


def test_models_endpoint_lists_cartesian_with_default_first(fake_app):
    client = TestClient(fake_app)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = [m["id"] for m in data]
    # fake_echo_v0 has compatible_llms={"fake-echo"} so only one combo possible
    assert ids[0] == "fake_echo_v0@fake-echo"
    assert all(m["object"] == "model" for m in data)


def test_chat_completions_routes_to_fake_echo(fake_app):
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake_echo_v0@fake-echo",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "fake_echo_v0@fake-echo"
    assert body["smr_agent"]["agent_variant"] == "fake_echo_v0"
    assert body["smr_agent"]["llm_id"] == "fake-echo"
    assert body["choices"][0]["message"]["content"].startswith("[echo]")


def test_chat_completions_unknown_variant_returns_400(fake_app):
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "nonexistent@fake-echo",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "unknown_variant"


def test_chat_completions_unknown_llm_returns_400(fake_app):
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake_echo_v0@gpt-9000",
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["code"] == "unknown_llm"


def test_chat_completions_bare_llm_pairs_with_default_variant(fake_app):
    """A bare known-LLM id (no `@`) is treated as `(default_variant, <id>)`."""
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "fake-echo", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "fake_echo_v0@fake-echo"
    assert body["smr_agent"]["agent_variant"] == "fake_echo_v0"


def test_chat_completions_empty_model_uses_defaults(fake_app):
    client = TestClient(fake_app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "fake_echo_v0@fake-echo"


def test_models_endpoint_filters_incompatible_combos():
    """fake_echo_v0 declares compatible_llms={'fake-echo'} — adding another LLM
    to the pool must NOT produce a fake_echo_v0@<other> combination."""
    with tempfile.TemporaryDirectory() as tmp:
        sink = FilesystemEventSink(root=str(Path(tmp) / "events"), prefix="t")
        recorder = EventRecorder(sink, app_profile="local")
        runners = {"fake_echo_v0": FakeEchoAgentRunner(recorder=recorder, spec=_FAKE_SPEC)}
        llm_pool = {
            "fake-echo": FakeEchoLLM(model_id="fake-echo"),
            "claude-haiku-4-5": FakeEchoLLM(model_id="claude-haiku-4-5"),
        }
        settings = Settings(
            agent_variants_enabled=["fake_echo_v0"],
            default_variant="fake_echo_v0",
            default_llm="fake-echo",
            utility_llm="fake-echo",
        )
        app = _make_app(runners, llm_pool, settings)
        client = TestClient(app)
        ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
        assert ids == ["fake_echo_v0@fake-echo"]
