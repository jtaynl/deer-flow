"""WRI fork patch (carried): SandboxMiddleware adds no per-round graph node.

Upstream 16d36ccd2 (#6998) made SandboxMiddleware always override before_model/abefore_model for AIO
prewarming. With sandbox.prewarm off (always, on this deployment) the hooks do nothing, but LangChain still
adds one graph node per model round, so every recursion_limit buys ~7 % fewer tool rounds (13 -> 14 steps per
round on our lead chain). The fork deletes both overrides; prewarm is therefore unsupported here — never set
sandbox.prewarm. See docs/OPS.md 'Local patches currently carried'.
"""

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from deerflow.sandbox.middleware import SandboxMiddleware


def test_sandbox_middleware_does_not_override_model_hooks():
    assert SandboxMiddleware.before_model is AgentMiddleware.before_model
    assert SandboxMiddleware.abefore_model is AgentMiddleware.abefore_model


def test_compiled_graph_has_no_sandbox_before_model_node():
    model = GenericFakeChatModel(messages=iter([AIMessage(content="done")]))
    graph = create_agent(model, tools=[], middleware=[SandboxMiddleware()])
    nodes = set(graph.get_graph().nodes)
    assert "SandboxMiddleware.before_model" not in nodes
    # The run-scoped hooks that do real work stay.
    assert "SandboxMiddleware.before_agent" in nodes
    assert "SandboxMiddleware.after_agent" in nodes
