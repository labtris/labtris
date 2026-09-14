"""The client-held agent surface.

The point of these two endpoints is that Labtris never holds the user's model
key: the browser runs the conversation with its own provider and comes back
only to execute what the model chose. That makes two properties worth pinning.

The tool schemas must be complete enough for a model to call them without any
Labtris-specific knowledge — a schema that omits a required argument produces a
call the server then refuses, which the model reads as its own mistake and
retries identically.

And a tool must run as the person who asked. An agent that can do more than the
user could is a privilege-escalation feature with a chat box on it.
"""

from __future__ import annotations

from labtris_mcp.tools import TOOLS


def test_every_tool_has_a_description_and_a_schema() -> None:
    """These go straight into the request body the browser sends its provider.
    A tool with an empty description is one the model will never pick."""
    assert TOOLS, "no tools registered"
    for name, (description, schema, handler) in TOOLS.items():
        assert description.strip(), f"{name} has no description"
        assert schema["type"] == "object", f"{name} has a non-object schema"
        assert isinstance(schema.get("properties"), dict), f"{name} has no properties"
        assert callable(handler), f"{name} has no handler"


def test_required_arguments_are_actually_declared() -> None:
    """`required` naming a property that does not exist makes a schema the
    provider will reject outright, and the error names neither the tool nor the
    argument."""
    for name, (_desc, schema, _fn) in TOOLS.items():
        props = set(schema.get("properties", {}))
        for arg in schema.get("required", []):
            assert arg in props, f"{name} requires {arg!r}, which it does not define"


def test_the_openai_shape_is_what_a_provider_expects() -> None:
    """The browser passes this through untouched, so it has to be right here."""
    from labtris_api.agent import _openai_tools

    tools = _openai_tools()
    assert len(tools) == len(TOOLS)
    for t in tools:
        assert t["type"] == "function"
        fn = t["function"]
        assert fn["name"] in TOOLS
        assert fn["description"]
        assert fn["parameters"]["type"] == "object"
