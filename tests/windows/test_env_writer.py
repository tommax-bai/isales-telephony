"""env_writer tests — cross-platform.

Covers token / endpoint validation, in-place updates that preserve
unrelated keys, and atomic write via os.replace().
"""

from __future__ import annotations

from pathlib import Path

import pytest

from isales_telephony.ui.env_writer import (
    EnvFileError,
    default_env_path,
    read_env,
    validate_endpoint,
    validate_token,
    write_token_and_endpoint,
)


# --------------------------------------------------------------- validators


@pytest.mark.parametrize(
    "token",
    [
        "abcdef1234567890",  # exactly min length 16
        "a" * 64,  # typical bearer token length
        "ABC.def-XYZ_123+456=/abc:def",  # all allowed punctuation
    ],
)
def test_validate_token_accepts_valid(token: str) -> None:
    validate_token(token)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "   ",
        "short",  # < 16
        "a" * 513,  # > 512
        "with space inside",
        "newline\nhere",
        "tab\there",
        "weird#char",
    ],
)
def test_validate_token_rejects_invalid(token: str) -> None:
    with pytest.raises(EnvFileError):
        validate_token(token)


@pytest.mark.parametrize(
    "endpoint",
    [
        "isales.example.com:443",
        "isales.example.com",
        "grpcs://isales.example.com:443",
        "grpc://localhost:7000",
        "10.0.0.1:443",
        "edge-cn1.isales.ai",
    ],
)
def test_validate_endpoint_accepts_valid(endpoint: str) -> None:
    validate_endpoint(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "   ",
        "http://example.com",  # scheme must be grpc/grpcs/none
        "://example.com",
        "no spaces allowed:443",
    ],
)
def test_validate_endpoint_rejects_invalid(endpoint: str) -> None:
    with pytest.raises(EnvFileError):
        validate_endpoint(endpoint)


# --------------------------------------------------------------- read_env


def test_read_env_missing_file_returns_empty(tmp_path: Path) -> None:
    out = read_env(tmp_path / "doesnt-exist.env")
    assert out == {}


def test_read_env_parses_lines(tmp_path: Path) -> None:
    path = tmp_path / "telephony.env"
    path.write_text(
        "# comment\n"
        "ISALES_EDGE_DEVICE_TOKEN=abcdef1234567890\n"
        "ISALES_CLOUD_GRPC_ENDPOINT=\"isales.example.com:443\"\n"
        "MALFORMED_NO_EQUALS\n"
        "EXTRA_KEY=value\n"
        "\n",
        encoding="utf-8",
    )
    out = read_env(path)
    assert out == {
        "ISALES_EDGE_DEVICE_TOKEN": "abcdef1234567890",
        "ISALES_CLOUD_GRPC_ENDPOINT": "isales.example.com:443",
        "EXTRA_KEY": "value",
    }


# --------------------------------------------------------------- write


def test_write_creates_file_and_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "telephony.env"
    out_path = write_token_and_endpoint(
        token="abcdef1234567890",
        endpoint="isales.example.com:443",
        path=target,
    )
    assert out_path == target
    assert target.exists()
    parsed = read_env(target)
    assert parsed["ISALES_EDGE_DEVICE_TOKEN"] == "abcdef1234567890"
    assert parsed["ISALES_CLOUD_GRPC_ENDPOINT"] == "isales.example.com:443"


def test_write_preserves_unrelated_keys(tmp_path: Path) -> None:
    target = tmp_path / "telephony.env"
    target.write_text(
        "ISALES_LOG_LEVEL=DEBUG\n"
        "ISALES_EDGE_DEVICE_TOKEN=old_token_value_xxx\n",
        encoding="utf-8",
    )
    write_token_and_endpoint(
        token="brandnewtoken123456",
        endpoint="new.example.com:443",
        path=target,
    )
    parsed = read_env(target)
    assert parsed["ISALES_LOG_LEVEL"] == "DEBUG"
    assert parsed["ISALES_EDGE_DEVICE_TOKEN"] == "brandnewtoken123456"
    assert parsed["ISALES_CLOUD_GRPC_ENDPOINT"] == "new.example.com:443"


def test_write_rejects_invalid_token(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError):
        write_token_and_endpoint(
            token="short",
            endpoint="isales.example.com:443",
            path=tmp_path / "telephony.env",
        )


def test_write_rejects_invalid_endpoint(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError):
        write_token_and_endpoint(
            token="abcdef1234567890",
            endpoint="http://no-http-scheme.example.com",
            path=tmp_path / "telephony.env",
        )


def test_write_strips_token_whitespace(tmp_path: Path) -> None:
    target = tmp_path / "telephony.env"
    write_token_and_endpoint(
        token="   abcdef1234567890   ",
        endpoint="  isales.example.com:443  ",
        path=target,
    )
    parsed = read_env(target)
    assert parsed["ISALES_EDGE_DEVICE_TOKEN"] == "abcdef1234567890"
    assert parsed["ISALES_CLOUD_GRPC_ENDPOINT"] == "isales.example.com:443"


def test_default_env_path_respects_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    p = default_env_path()
    # Use forward-slash form for cross-platform assert (Path normalises).
    assert "isales" in p.parts
    assert "env" in p.parts
    assert p.name == "telephony.env"


def test_default_env_path_falls_back_when_no_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPDATA", raising=False)
    p = default_env_path()
    assert p.name == "telephony.env"
