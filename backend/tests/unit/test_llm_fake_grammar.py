from __future__ import annotations

from app.adapters.llm.fake import FakeEchoLLM
from app.ports.llm import GrammarSpec


async def test_fake_echo_accepts_grammar_kwarg_without_error():
    llm = FakeEchoLLM()
    spec = GrammarSpec(kind="regex", value=r"\[cite-\d+\]")
    result = await llm.generate("hello", grammar=spec)
    assert result.text.startswith("[fake-echo]")
    assert llm.last_grammar is spec


async def test_fake_echo_records_grammar_in_stream():
    llm = FakeEchoLLM()
    spec = GrammarSpec(kind="choice", value=["supported", "unsupported"])
    deltas = [d async for d in llm.generate_stream("x", grammar=spec)]
    assert llm.last_grammar is spec
    # Last delta carries finish_reason in the fake stream.
    assert deltas[-1].finish_reason == "stop"


async def test_fake_echo_without_grammar_keeps_last_grammar_none():
    llm = FakeEchoLLM()
    await llm.generate("x")
    assert llm.last_grammar is None
