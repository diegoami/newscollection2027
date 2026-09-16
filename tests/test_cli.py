"""Tests for nc.cli."""

from __future__ import annotations

from importlib import metadata

import pytest

from nc import __version__
from nc.cli import main


def test_version_returns_zero_and_prints_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--version"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == __version__


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    assert "usage" in capsys.readouterr().out


def test_no_args_returns_zero_and_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])

    assert exit_code == 0
    assert "usage" in capsys.readouterr().out


def test_unknown_command_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])

    assert excinfo.value.code != 0
    assert excinfo.value.code == 2


def test_version_matches_package_metadata() -> None:
    """__version__ and the version in pyproject.toml must not drift apart."""
    assert metadata.version("newscollection2027") == __version__
