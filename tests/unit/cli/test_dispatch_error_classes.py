"""Unit tests for ``_run_with_error_handling``'s error classification.

Spec §11 maps exceptions to exit codes/payload statuses:

  - UserError → user_error / exit 2
  - provider error subclasses → provider_error / exit 3
  - everything else → internal_error / exit 4 (last-resort catchall)

These tests exercise the dispatch wrapper directly with a stubbed provider
and adapter so the classifier ladder is the only thing under test.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from async_substrate_interface.errors import SubstrateRequestException
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from chainwake.cli.chains import dispatch
from chainwake.core.errors import (
    AuthError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    UserError,
)
from chainwake.core.primitives.base import Primitive, PrimitiveInput
from chainwake.core.primitives.threshold import ThresholdPrimitive
from chainwake.core.runtime import WatcherSpec
from chainwake.output.schema import ThresholdCondition

pytestmark = pytest.mark.unit


class _RecordingAdapter:
    """Minimal adapter that records every dispatched payload."""

    name = "recording"
    should_exit_after_dispatch = False

    def __init__(self) -> None:
        self.received: list[Any] = []

    def dispatch(self, payload: Any) -> None:
        self.received.append(payload)

    def close(self) -> None:
        pass


def _make_spec(*, path_params: dict[str, str] | None = None) -> WatcherSpec:
    return WatcherSpec(
        chain="bt",
        resource="subnet",
        path_params=path_params if path_params is not None else {"netuid": "1"},
        sub_resource="pool.price",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="below", target=0.5),
        invocation=["chainwake"],
        poll_seconds=0.0001,
        max_runtime_seconds=1.0,
    )


def _make_primitive() -> Primitive[PrimitiveInput]:
    # Cast to Primitive[PrimitiveInput] via the runtime's actual class.
    return ThresholdPrimitive(operator="below", target=0.5)  # type: ignore[return-value]


@pytest.fixture
def recording_adapter(monkeypatch: pytest.MonkeyPatch) -> _RecordingAdapter:
    """Replace the dispatch URI parser to always return one recording adapter."""
    adapter = _RecordingAdapter()
    monkeypatch.setattr(dispatch, "_parse_out_uris", lambda _uris: [adapter])
    return adapter


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the Bittensor backend with a stub that connects cleanly."""
    provider = AsyncMock()
    provider.connect = AsyncMock(return_value=None)
    provider.disconnect = AsyncMock(return_value=None)
    runtime = dispatch.backend_for("bt").runtime

    class _Backend:
        def __init__(self) -> None:
            self.runtime = runtime

        def create_provider(self) -> AsyncMock:
            return provider

    backend = _Backend()
    monkeypatch.setattr(dispatch, "backend_for", lambda _chain: backend)
    return provider


@pytest.mark.asyncio
async def test_user_error_from_render_path_returns_exit_2(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """A WatcherSpec missing path params must produce user_error exit 2."""
    spec = _make_spec(path_params={})  # missing required 'netuid'

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 2
    assert len(recording_adapter.received) == 1
    payload = recording_adapter.received[0]
    assert payload.status == "user_error"
    assert payload.reason == "invalid_path_params"
    # The KeyError's underlying message ("missing path params [...]")
    # propagates verbatim — operators see *what* went wrong.
    assert "missing path params" in payload.message
    assert "netuid" in payload.message


@pytest.mark.asyncio
async def test_unknown_observable_returns_user_error_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """An entry_path with no registry entry must classify as user_error."""
    monkeypatch.setattr(dispatch, "_submit_if_durable", lambda _spec, _out: 0)
    spec = _make_spec()

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.does.not.exist",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 2
    payload = recording_adapter.received[0]
    assert payload.status == "user_error"
    assert payload.reason == "unknown_observable"


@pytest.mark.asyncio
async def test_dispatch_selects_provider_and_observable_for_spec_chain(
    monkeypatch: pytest.MonkeyPatch,
    recording_adapter: _RecordingAdapter,
) -> None:
    provider = AsyncMock()
    provider.connect = AsyncMock(side_effect=RuntimeError("selected"))
    provider.disconnect = AsyncMock()
    selected_chains: list[str] = []
    looked_up: list[tuple[str, str]] = []
    original_lookup = dispatch.lookup

    class _Backend:
        runtime = object()

        def create_provider(self) -> AsyncMock:
            return provider

    def backend_for(chain: str) -> _Backend:
        selected_chains.append(chain)
        return _Backend()

    def lookup_for_chain(path: str, *, chain: str = "bt"):
        looked_up.append((chain, path))
        return original_lookup(path, chain="bt")

    monkeypatch.setattr(dispatch, "backend_for", backend_for)
    monkeypatch.setattr(dispatch, "lookup", lookup_for_chain)
    spec = _make_spec()
    spec.chain = "eth"

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 4
    assert selected_chains == ["eth"]
    assert looked_up == [("eth", "subnet.{netuid}.pool.price")]


@pytest.mark.asyncio
async def test_unknown_backend_returns_user_error(
    monkeypatch: pytest.MonkeyPatch,
    recording_adapter: _RecordingAdapter,
) -> None:
    def unknown_backend(_chain: str) -> None:
        raise KeyError("unknown chain backend")

    monkeypatch.setattr(dispatch, "backend_for", unknown_backend)
    monkeypatch.setattr(dispatch, "_submit_if_durable", lambda _spec, _out: 0)
    spec = _make_spec()

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 2
    payload = recording_adapter.received[0]
    assert payload.status == "user_error"
    assert payload.reason == "unsupported_chain"


@pytest.mark.asyncio
async def test_durable_rejects_malformed_out_before_adapter_parsing(
    monkeypatch: pytest.MonkeyPatch,
    stub_provider: AsyncMock,
) -> None:
    monkeypatch.setattr(dispatch, "_submit_if_durable", lambda _spec, _out: 2)
    spec = _make_spec()

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=["file://"],
    )

    assert code == 2
    stub_provider.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_user_error_propagates_with_reason(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """A UserError raised mid-run lands in the user_error branch verbatim."""
    spec = _make_spec()
    stub_provider.connect.side_effect = UserError("nope", reason="custom_reason")

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 2
    payload = recording_adapter.received[0]
    assert payload.status == "user_error"
    assert payload.reason == "custom_reason"
    assert "nope" in payload.message


@pytest.mark.asyncio
async def test_unexpected_exception_returns_internal_error_exit_4(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """Non-classified exceptions still route to the internal_error catchall."""
    spec = _make_spec()
    stub_provider.connect.side_effect = RuntimeError("whoa")

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 4
    payload = recording_adapter.received[0]
    assert payload.status == "internal_error"
    assert payload.reason == "RuntimeError"
    assert "whoa" in payload.message


@pytest.mark.asyncio
async def test_cancellation_during_connect_returns_stopped_context(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    spec = _make_spec()
    stub_provider.connect.side_effect = asyncio.CancelledError

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 1
    payload = recording_adapter.received[0]
    assert payload.status == "stopped"
    assert payload.watcher.invocation == ["chainwake"]
    assert payload.condition.operator == "below"


@pytest.mark.asyncio
async def test_connection_error_returns_provider_error_exit_3(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """Network-level exceptions stay on the provider_error branch."""
    spec = _make_spec()
    stub_provider.connect.side_effect = ConnectionError("refused")

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "provider_error"
    assert payload.reason == "rpc_unreachable"


@pytest.mark.asyncio
async def test_auth_error_returns_auth_error_payload_exit_3(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """AuthError at connect emits auth_error payload (spec Appendix D)."""
    spec = _make_spec()
    stub_provider.connect.side_effect = AuthError("invalid api key")

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "auth_error"
    assert payload.reason == "auth_failed"


@pytest.mark.asyncio
async def test_auth_error_payload_lists_env_vars_in_precedence_order(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """Per spec §10.2: per-chain env var first, global fallback second."""
    spec = _make_spec()
    stub_provider.connect.side_effect = AuthError("invalid api key")

    await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    payload = recording_adapter.received[0]
    assert payload.api_key_env_vars == ["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"]


@pytest.mark.asyncio
async def test_auth_error_message_names_both_env_vars(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """The user-facing message must reference --api-key and both env vars."""
    spec = _make_spec()
    stub_provider.connect.side_effect = AuthError("invalid api key")

    await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    message = recording_adapter.received[0].message
    assert "--api-key" in message
    assert "CHAINWAKE_BT_API_KEY" in message
    assert "CHAINWAKE_API_KEY" in message


@pytest.mark.asyncio
async def test_auth_error_writes_actionable_stderr_line(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A single stderr line precedes the JSON payload so interactive users
    see the recovery hint without parsing JSON."""
    spec = _make_spec()
    stub_provider.connect.side_effect = AuthError("invalid api key")

    await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    captured = capsys.readouterr()
    assert "chainwake: authentication failed for chain 'bt'" in captured.err
    assert "--api-key" in captured.err
    assert "CHAINWAKE_BT_API_KEY" in captured.err


@pytest.mark.asyncio
async def test_auth_error_payload_carries_docs_url(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """The payload's docs_url field points at the provider signup page."""
    spec = _make_spec()
    stub_provider.connect.side_effect = AuthError("invalid api key")

    await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="ws://x",
        out_uris=[],
    )

    payload = recording_adapter.received[0]
    assert payload.docs_url == "https://blockmachine.io"


def _make_invalid_status(status_code: int) -> InvalidStatus:
    """Construct a ``websockets.exceptions.InvalidStatus`` for a status code."""
    response = Response(status_code=status_code, reason_phrase="", headers=Headers())
    return InvalidStatus(response)


@pytest.mark.asyncio
async def test_websocket_401_classifies_as_auth_error(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """A 401 handshake rejection emits AuthErrorPayload, not internal_error.

    Regression: previously this fell into the catchall and surfaced as
    ``internal_error`` with "this is a bug in chainwake", masking the
    actionable "set --api-key" recovery path.
    """
    spec = _make_spec()
    stub_provider.connect.side_effect = _make_invalid_status(401)

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="wss://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "auth_error"
    assert payload.api_key_env_vars == ["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"]


@pytest.mark.asyncio
async def test_websocket_502_classifies_as_provider_error(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """A 5xx handshake rejection emits provider_error / rpc_unreachable."""
    spec = _make_spec()
    stub_provider.connect.side_effect = _make_invalid_status(502)

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="wss://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "provider_error"
    assert payload.reason == "rpc_unreachable"


@pytest.mark.asyncio
async def test_websocket_418_falls_through_to_internal_error(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
) -> None:
    """Other status codes (e.g. 418) cannot be classified — internal_error.

    We only know how to recover from 401 (re-auth) and 5xx (retry the
    upstream). A teapot is genuinely unexpected.
    """
    spec = _make_spec()
    stub_provider.connect.side_effect = _make_invalid_status(418)

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="wss://x",
        out_uris=[],
    )

    assert code == 4
    payload = recording_adapter.received[0]
    assert payload.status == "internal_error"
    assert payload.reason == "InvalidStatus"


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        ("rate limit exceeded", "rate_limited"),
        ("Too Many Requests", "rate_limited"),
        ("HTTP 429: throttled", "rate_limited"),
        ("subscribe: connection closed", "subscription_failed"),
        ("subscription dropped mid-stream", "subscription_failed"),
        ("connection refused", "rpc_unreachable"),
        ("unknown method", "rpc_unreachable"),
    ],
)
@pytest.mark.asyncio
async def test_substrate_request_exception_classifies_by_message(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
    message: str,
    expected_reason: str,
) -> None:
    """SubstrateRequestException must route to provider_error, not internal_error.

    Regression: a paid RPC endpoint that returns "rate limit exceeded"
    surfaced as ``internal_error`` ("this is a bug in chainwake; please
    report it"), masking the actionable upstream signal. The classifier
    text-matches well-known markers and falls back to ``rpc_unreachable``;
    any substrate RPC failure is upstream by definition.
    """
    spec = _make_spec()
    stub_provider.connect.side_effect = SubstrateRequestException(message)

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="wss://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "provider_error"
    assert payload.reason == expected_reason
    assert message in payload.message


@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (RateLimitError("rl"), "rate_limited"),
        (RPCUnreachableError("rpc"), "rpc_unreachable"),
        (SubscriptionFailedError("sub"), "subscription_failed"),
    ],
)
@pytest.mark.asyncio
async def test_provider_error_subclasses_classify_to_provider_error(
    recording_adapter: _RecordingAdapter,
    stub_provider: AsyncMock,
    exc: Exception,
    expected_reason: str,
) -> None:
    """ProviderError subclasses raised at the provider boundary route to
    provider_error with their declared reason — never internal_error.

    Regression: the provider's connect() now wraps SubstrateRequestException
    into a RateLimitError / RPCUnreachableError / SubscriptionFailedError
    so the runtime ladder can route it. But _PROVIDER_ERRORS in dispatch
    only lists stdlib exceptions, so without an explicit
    ``except ProviderError`` clause these wrapped errors fell through to
    the catchall and surfaced as ``internal_error`` ("this is a bug in
    chainwake; please report it").
    """
    spec = _make_spec()
    stub_provider.connect.side_effect = exc

    code = await dispatch._run_with_error_handling(
        spec,
        entry_path="subnet.{netuid}.pool.price",
        primitive=_make_primitive(),
        rpc_url="wss://x",
        out_uris=[],
    )

    assert code == 3
    payload = recording_adapter.received[0]
    assert payload.status == "provider_error"
    assert payload.reason == expected_reason
