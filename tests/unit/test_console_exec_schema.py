"""ConsoleExecIn's validators — the last line before a stuck exec pins
the agent loop indefinitely. Also the last line before an empty command
turns into an accidental `docker exec sh -c ''` with unpredictable
shell semantics."""

from __future__ import annotations

import pytest

from labtris_api.schemas import ConsoleExecIn, ConsoleExecOut


def test_default_timeout_is_ten_seconds() -> None:
    m = ConsoleExecIn(command="ls")
    assert m.timeout_s == 10


def test_timeout_clamps_up_to_the_minimum() -> None:
    """Anything below 1s is clamped to 1s rather than refused — the caller
    almost certainly meant 'as fast as possible' and a 0 would loop
    forever waiting for a queue read that returns immediately."""
    m = ConsoleExecIn(command="ls", timeout_s=0)
    assert m.timeout_s == 1
    m = ConsoleExecIn(command="ls", timeout_s=-5)
    assert m.timeout_s == 1


def test_timeout_clamps_down_to_the_maximum() -> None:
    """Anything above 30s is capped — the agent has its own per-call
    deadline and holding a request handler open for minutes is worse
    than reporting a partial answer."""
    m = ConsoleExecIn(command="ls", timeout_s=999)
    assert m.timeout_s == 30


def test_empty_command_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ConsoleExecIn(command="")


def test_whitespace_only_command_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ConsoleExecIn(command="   \t\n  ")


def test_output_model_default_has_no_warnings() -> None:
    """Docker path with a happy result — no warnings, real exit code."""
    o = ConsoleExecOut(stdout="hello\n", exit_code=0, runtime="docker")
    assert o.warnings == []
    assert o.stderr == ""


def test_output_model_carries_warnings_when_provided() -> None:
    """QEMU path — warning about no shell-prompt detection is how the
    model sees the caveat."""
    o = ConsoleExecOut(
        stdout="cirros login: ",
        runtime="qemu",
        warnings=["no shell-prompt detection"],
    )
    assert o.warnings == ["no shell-prompt detection"]
    assert o.exit_code is None
