"""``bittensor`` long-form alias for the ``bt`` chain command.

Spec line 244 documents both ``bt`` (short) and ``bittensor`` (long) as valid
chain selectors. Whichever the user types, the canonical ``WatcherSpec.chain``
identifier in the JSON envelope must stay ``"bt"`` so payloads validate
against ``schemas/output.json``.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from chainwake.cli.app import build_app
from chainwake.cli.chains import dispatch
from chainwake.core.runtime import WatcherSpec

pytestmark = pytest.mark.unit


class _SpecCapture:
    """Captures the WatcherSpec built by a dispatch helper, no RPC."""

    def __init__(self) -> None:
        self.spec: WatcherSpec | None = None

    async def stub(self, spec: WatcherSpec, /, **_kwargs: object) -> int:
        self.spec = spec
        return 0


@pytest.fixture
def capture_spec(monkeypatch: pytest.MonkeyPatch) -> _SpecCapture:
    capture = _SpecCapture()
    monkeypatch.setattr(dispatch, "_run_with_error_handling", capture.stub)
    return capture


def _invoke_cli(*args: str) -> int:
    app = build_app()
    stdout = StringIO()
    try:
        with patch("sys.stdout", stdout):
            app(list(args), exit_on_error=False)
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


@pytest.mark.parametrize("alias", ["bt", "bittensor"])
def test_alias_dispatches_with_canonical_chain(alias: str, capture_spec: _SpecCapture) -> None:
    """Both aliases route to the same dispatcher; ``watcher.chain`` stays ``"bt"``."""
    code = _invoke_cli(alias, "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.chain == "bt"
