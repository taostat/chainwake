"""Unit tests for chainwake.cli.duration."""

from __future__ import annotations

import pytest

from chainwake.cli.duration import InvalidDurationError, duration_to_seconds, parse_duration
from chainwake.core.duration import parse_duration_components


@pytest.mark.unit
class TestParseDuration:
    def test_seconds(self) -> None:
        assert parse_duration("30s") == "30s"

    def test_minutes(self) -> None:
        assert parse_duration("5m") == "5m"

    def test_hours(self) -> None:
        assert parse_duration("2h") == "2h"

    def test_days(self) -> None:
        assert parse_duration("7d") == "7d"

    def test_blocks_singular(self) -> None:
        assert parse_duration("1block") == "1blocks"

    def test_blocks_plural(self) -> None:
        assert parse_duration("100blocks") == "100blocks"

    def test_epochs_singular(self) -> None:
        assert parse_duration("1epoch") == "1epochs"

    def test_epochs_plural(self) -> None:
        assert parse_duration("3epochs") == "3epochs"

    def test_case_insensitive(self) -> None:
        assert parse_duration("30S") == "30s"
        assert parse_duration("5M") == "5m"

    def test_leading_whitespace(self) -> None:
        assert parse_duration("  30s") == "30s"

    def test_invalid_no_unit(self) -> None:
        with pytest.raises(InvalidDurationError):
            parse_duration("30")

    def test_invalid_no_number(self) -> None:
        with pytest.raises(InvalidDurationError):
            parse_duration("m")

    def test_invalid_empty(self) -> None:
        with pytest.raises(InvalidDurationError):
            parse_duration("")

    def test_decimal_is_valid(self) -> None:
        assert parse_duration("1.5h") == "1.5h"


@pytest.mark.unit
class TestDurationToSeconds:
    def test_seconds(self) -> None:
        assert duration_to_seconds("30s") == 30.0

    def test_minutes(self) -> None:
        assert duration_to_seconds("5m") == 300.0

    def test_hours(self) -> None:
        assert duration_to_seconds("2h") == 7200.0

    def test_days(self) -> None:
        assert duration_to_seconds("7d") == 604800.0

    def test_chain_native_blocks_raises(self) -> None:
        with pytest.raises(InvalidDurationError, match="chain-native"):
            duration_to_seconds("100blocks")

    def test_chain_native_epochs_raises(self) -> None:
        with pytest.raises(InvalidDurationError, match="chain-native"):
            duration_to_seconds("3epochs")


def test_epoch_components_remain_epoch_native() -> None:
    """Epoch durations must not be projected through a global block constant."""
    assert parse_duration_components("3epochs") == ("epochs", 3.0)
