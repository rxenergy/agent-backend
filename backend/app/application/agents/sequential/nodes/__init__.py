# Sequential workflow nodes (ADR-0003).
#
# Each module under this package corresponds to one of the 15 steps in
# `agent_expreiment_platform_architecture.mvp.v2.md` §7.1. Modules expose
# free `async def` functions over `RunState` + per-node deps so the
# conductor (`sequential_tool_routed_v2.SequentialToolRoutedRunner.run`)
# becomes a straight sequence of calls and each node is independently
# testable.
#
# Migration status (Phase 3.2c):
#   [done] classify  — extracted as a demonstration of the pattern
#   [todo] memory_load, retrieve, resolve_citation, build_context,
#          resolve_prompt, render, generate, verify_citation,
#          verify_faithfulness, memory_update, build_event
from app.application.agents.sequential.nodes import classify  # noqa: F401
