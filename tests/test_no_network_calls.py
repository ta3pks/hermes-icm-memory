"""Architectural invariant test (S11 / AC3).

Enforces NFR-SEC-1: zero plugin-originated network I/O during the
Hermes-side lifecycle methods ``is_available``, ``get_config_schema``, and
``save_config``. We patch ``socket.socket`` to raise on construction; if any
lifecycle method dials home (directly or via a higher-level library such as
``urllib`` / ``http.client`` / ``requests``), socket construction will fail
and the test will catch it.

Forward-compat note: this branch is parallel to S07 (which lands the real
``IcmMemoryProvider`` with the three lifecycle methods). On this branch the
S01 stub provider has only ``name = "icm"`` and none of the lifecycle
methods exist yet. Tests 3-5 are therefore gated on ``_HAS_LIFECYCLE`` via
``pytest.mark.skipif``; the skip predicate flips automatically once S07
lands the methods, requiring no follow-up edit to this file.
"""

from __future__ import annotations

import socket
from typing import Any, NoReturn

import pytest

import hermes_icm_memory


class _CapturingCtx:
    """Minimal stand-in for the Hermes plugin context used during register()."""

    def __init__(self) -> None:
        self.provider: Any | None = None

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider


def _register_and_capture() -> Any:
    ctx = _CapturingCtx()
    hermes_icm_memory.register(ctx)
    assert ctx.provider is not None, "register(ctx) did not capture a provider"
    return ctx.provider


_PROVIDER = _register_and_capture()
_HAS_LIFECYCLE: bool = (
    callable(getattr(_PROVIDER, "is_available", None))
    and callable(getattr(_PROVIDER, "get_config_schema", None))
    and callable(getattr(_PROVIDER, "save_config", None))
)
_SKIP_REASON = (
    "provider lifecycle methods (is_available/get_config_schema/save_config) "
    "land in S07; this skipif disappears once they exist"
)


def _raise_on_socket(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("network forbidden during plugin lifecycle (NFR-SEC-1)")


def test_register_returns_provider() -> None:
    """Sanity: ``register(ctx)`` produces a provider with a ``name`` attribute."""
    provider = _register_and_capture()
    assert provider is not None
    assert hasattr(provider, "name")


def test_lifecycle_predicate_smoke() -> None:
    """Document the skip-gate behaviour: ``_HAS_LIFECYCLE`` is a bool."""
    assert isinstance(_HAS_LIFECYCLE, bool)


@pytest.mark.skipif(not _HAS_LIFECYCLE, reason=_SKIP_REASON)
def test_is_available_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """``is_available()`` must not construct a socket (NFR-SEC-1)."""
    monkeypatch.setattr(socket, "socket", _raise_on_socket)
    provider = _register_and_capture()
    provider.is_available()


@pytest.mark.skipif(not _HAS_LIFECYCLE, reason=_SKIP_REASON)
def test_get_config_schema_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_config_schema()`` must not construct a socket (NFR-SEC-1)."""
    monkeypatch.setattr(socket, "socket", _raise_on_socket)
    provider = _register_and_capture()
    provider.get_config_schema()


@pytest.mark.skipif(not _HAS_LIFECYCLE, reason=_SKIP_REASON)
def test_save_config_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """``save_config({})`` must not construct a socket (NFR-SEC-1)."""
    monkeypatch.setattr(socket, "socket", _raise_on_socket)
    provider = _register_and_capture()
    provider.save_config({})
