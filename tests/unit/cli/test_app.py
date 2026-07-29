"""Unit tests for chainwake.cli.app — top-level cyclopts wiring."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chainwake import __version__
from chainwake.cli.app import _apply_render_flags, build_app
from chainwake.cli.chains.dispatch import _parse_out_uris
from chainwake.output.adapters import DefaultAdapter, FileAdapter
from chainwake.output.render import RENDER_MODE_ENV_VAR, RenderMode

pytestmark = pytest.mark.unit


class TestAppConstruction:
    def test_app_has_expected_subcommands(self) -> None:
        app = build_app()
        names = list(app._commands.keys())
        assert "bt" in names
        assert "mcp" in names

    def test_bittensor_alias_registered_for_bt(self) -> None:
        # spec.md line 244 documents both ``bt`` (short) and ``bittensor`` (long)
        # as valid chain selectors. Both must resolve to the same App instance.
        app = build_app()
        assert "bittensor" in app._commands
        assert app._commands["bittensor"] is app._commands["bt"]

    def test_install_completion_registered(self) -> None:
        # docs/installation.md instructs users to run `chainwake
        # --install-completion`; cyclopts has to register that command.
        app = build_app()
        assert "--install-completion" in app._commands

    def test_help_flags_registered(self) -> None:
        app = build_app()
        names = list(app._commands.keys())
        assert "--help" in names
        assert "-h" in names

    def test_version_flag_registered(self) -> None:
        app = build_app()
        assert "--version" in app._commands

    def test_app_print_error_disabled(self) -> None:
        # The CLI emits structured JSON error envelopes via dispatchers, so
        # cyclopts' default plain-text error output must stay disabled.
        app = build_app()
        assert app.print_error is False

    def test_app_version_matches_package(self) -> None:
        app = build_app()
        assert app.version == __version__


class TestHelpOutputNotHijacked:
    """Regression: bittensor SDK's argparse hijack must not eat ``--help``.

    Importing ``bittensor.utils.btlogging`` at module-load time builds an
    ``argparse.ArgumentParser`` and calls ``parse_known_args(sys.argv[1:])``.
    With ``sys.argv == [..., "--help"]`` that parser printed bittensor's own
    ``--logging.debug --strict --no_version_checking`` banner and exited
    before cyclopts ever ran. The provider module sets
    ``BT_NO_PARSE_CLI_ARGS=1`` (bittensor's documented escape hatch) before
    importing the SDK to suppress that parse. These tests run the CLI as a
    subprocess so the import order matches a real invocation.
    """

    BITTENSOR_BANNER_MARKERS = (
        "--logging.debug",
        "--logging.trace",
        "--no_version_checking",
        "Turn on bittensor debugging information",
    )

    def _run_help(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "chainwake", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _assert_no_bittensor_banner(self, combined: str) -> None:
        for marker in self.BITTENSOR_BANNER_MARKERS:
            assert marker not in combined, (
                f"bittensor argparse hijack leaked into --help output (marker: {marker!r})"
            )

    def test_top_level_help_is_cyclopts(self) -> None:
        result = self._run_help("--help")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "Usage: chainwake" in combined
        self._assert_no_bittensor_banner(combined)

    def test_bt_subcommand_help_is_cyclopts(self) -> None:
        # The id-first meta-app from task #40 owns help for resource sub-apps,
        # so the breadcrumb is "Usage: subnet ..." rather than the full
        # "Usage: chainwake bt subnet ...". The test invariant is that help
        # comes from cyclopts (not bittensor's argparse) and that no bittensor
        # banner markers leak through. Both are checked below.
        result = self._run_help("bt", "subnet", "--help")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "Usage:" in combined
        assert "subnet" in combined
        self._assert_no_bittensor_banner(combined)


class TestRenderFlags:
    """`--json` / `--no-json` translate to the render-mode env var."""

    def test_json_flag_sets_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        _apply_render_flags(json_flag=True, human_flag=False)
        assert os.environ.get(RENDER_MODE_ENV_VAR) == RenderMode.JSON.value

    def test_no_json_flag_sets_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        _apply_render_flags(json_flag=False, human_flag=True)
        assert os.environ.get(RENDER_MODE_ENV_VAR) == RenderMode.HUMAN.value

    def test_neither_flag_leaves_env_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        _apply_render_flags(json_flag=False, human_flag=False)
        assert RENDER_MODE_ENV_VAR not in os.environ

    def test_both_flags_emit_user_error_and_exit_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        with pytest.raises(SystemExit) as excinfo:
            _apply_render_flags(json_flag=True, human_flag=True)
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["status"] == "user_error"
        assert payload["reason"] == "invalid_input"
        assert "mutually exclusive" in payload["message"]


class TestParseOutUrisRenderMode:
    """`_parse_out_uris` reads the render-mode env var when no --out is given."""

    def test_default_adapter_picks_up_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, RenderMode.HUMAN.value)
        adapters = _parse_out_uris([])
        assert len(adapters) == 1
        adapter = adapters[0]
        assert isinstance(adapter, DefaultAdapter)
        assert adapter.render_mode is RenderMode.HUMAN

    def test_default_adapter_with_no_env_var_is_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        adapters = _parse_out_uris([])
        adapter = adapters[0]
        assert isinstance(adapter, DefaultAdapter)
        assert adapter.render_mode is RenderMode.AUTO

    def test_explicit_out_uri_skips_default_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # When --out is given, the default-stdout adapter is not built, so
        # the env var has no effect on the resulting pipeline.
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, RenderMode.HUMAN.value)
        target = f"file://{tmp_path}/log.jsonl"
        adapters = _parse_out_uris([target])
        assert all(isinstance(a, FileAdapter) for a in adapters)


class TestRenderFlagsEndToEnd:
    """Subprocess tests — `--json` and `--no-json` round-trip through the meta-app."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "chainwake", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_double_render_flag_is_user_error(self) -> None:
        result = self._run(
            "--json",
            "--no-json",
            "bt",
            "subnet",
            "price",
            "1",
            "--below",
            "0.05",
            "--rpc-url",
            "ws://127.0.0.1:9999",
            "--max-runtime",
            "1s",
        )
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["status"] == "user_error"
        assert "mutually exclusive" in payload["message"]

    def test_help_lists_render_flags(self) -> None:
        result = self._run("--help")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--json" in combined
        assert "--no-json" in combined


class TestLeafHelpRouting:
    """Bare leaf invocation and ``--help`` after a leaf both show leaf help.

    These exercise the id-first meta dispatch (subnet/validator/neuron/
    account). When the user supplies ``<resource> <id> <leaf>`` without
    further flags, or appends ``--help``, the leaf's parameter list
    must appear (not the resource's command list).
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "chainwake", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_leaf_dash_help_shows_leaf_parameters(self) -> None:
        result = self._run("bt", "subnet", "19", "price", "--help")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        # Leaf-specific parameters appear; the resource command list does not.
        assert "--below" in combined
        assert "--drop-pct" in combined
        assert "Alpha price in TAO" in combined

    def test_bare_leaf_shows_leaf_parameters(self) -> None:
        result = self._run("bt", "subnet", "19", "price")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--below" in combined
        assert "Alpha price in TAO" in combined

    def test_leaf_help_usage_advertises_id_first_form(self) -> None:
        # The leaf's usage line must show the canonical id-first form
        # (``bt subnet <netuid> price``), not cyclopts' default
        # ``price <netuid>`` ordering — otherwise users copy a deprecated
        # invocation that ``_dispatch`` rejects.
        result = self._run("bt", "subnet", "19", "price", "--help")
        combined = result.stdout + result.stderr
        assert "chainwake bt subnet <netuid> price" in combined

    def test_leaf_help_usage_for_two_id_resource(self) -> None:
        # Neuron has two id positionals (<netuid> <hotkey>) — usage line
        # must reflect both.
        result = self._run(
            "bt",
            "neuron",
            "19",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "incentive",
            "--help",
        )
        combined = result.stdout + result.stderr
        assert "chainwake bt neuron <netuid> <hotkey> incentive" in combined

    def test_leaf_help_hides_parent_supplied_ids(self) -> None:
        # The id positional(s) shown in the usage line are injected by the
        # meta-app from the parent path; listing them again as required
        # parameters in the help table misleads users into supplying them
        # twice (which then collides with the meta injection).
        subnet = self._run("bt", "subnet", "19", "price", "--help")
        subnet_out = subnet.stdout + subnet.stderr
        assert subnet.returncode == 0, subnet_out
        assert "NETUID" not in subnet_out
        assert "--netuid" not in subnet_out

        neuron = self._run(
            "bt",
            "neuron",
            "19",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "incentive",
            "--help",
        )
        neuron_out = neuron.stdout + neuron.stderr
        assert neuron.returncode == 0, neuron_out
        assert "NETUID" not in neuron_out
        assert "HOTKEY" not in neuron_out
        assert "--netuid" not in neuron_out
        assert "--hotkey" not in neuron_out

        account = self._run(
            "bt",
            "account",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "balance",
            "--help",
        )
        account_out = account.stdout + account.stderr
        assert account.returncode == 0, account_out
        assert "COLDKEY" not in account_out
        assert "--coldkey" not in account_out


class TestCycloptsErrorRenderMode:
    """Cyclopts parse errors flow through ``emit_user_error`` and honour mode."""

    def _run(self, env_render_mode: str | None, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if env_render_mode is None:
            env.pop("CHAINWAKE_RENDER_MODE", None)
        else:
            env["CHAINWAKE_RENDER_MODE"] = env_render_mode
        return subprocess.run(
            [sys.executable, "-m", "chainwake", *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_human_mode_prints_prose_error(self) -> None:
        # ``bt tx`` requires ``--tx-hash`` and the bare leaf does not pass
        # through id-first meta-dispatch (no id positional), so cyclopts
        # raises ``MissingArgumentError`` directly. Human mode must print
        # prose.
        result = self._run("human", "bt", "tx")
        assert result.returncode == 2
        assert "{" not in result.stdout
        assert "error:" in result.stdout
        assert "--tx-hash" in result.stdout

    def test_json_mode_prints_envelope(self) -> None:
        result = self._run("json", "bt", "tx")
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["status"] == "user_error"
        assert "--tx-hash" in payload["message"]
