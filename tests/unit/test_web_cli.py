"""``id42-work web serve`` loads the extras and refuses a non-loopback bind."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import approval_host
from id42_work import web
from id42_work.cli import main
from id42_work.web.config import DEFAULT_HOST, DEFAULT_PORT
from id42_work.web.serve import serve_web

FACTORY = "approval_host:server"


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    code = raised.value.code
    assert isinstance(code, int)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_help_does_not_load_a_factory(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _invoke(["web", "--help"], capsys)
    assert code == 0
    assert "Not an MCP tool" in out
    code, out, _err = _invoke(["web", "serve", "--help"], capsys)
    assert code == 0
    assert "--factory" in out
    assert "--credential-auth" in out
    assert "127.0.0.1" in out
    assert str(DEFAULT_PORT) in out


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["web"], capsys)
    assert code == 2
    assert err
    code, _out, err = _invoke(["web", "serve"], capsys)
    assert code == 2
    assert "factory" in err


def test_a_bad_factory_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["web", "serve", "--factory", "not a factory"], capsys)
    assert code == 2
    assert "module:callable" in err
    code, _out, err = _invoke(["web", "serve", "--factory", "approval_host_missing:server"], capsys)
    assert code == 2
    assert "cannot import" in err
    code, _out, err = _invoke(["web", "serve", "--factory", "approval_host:missing"], capsys)
    assert code == 2
    assert "was not found" in err
    code, _out, err = _invoke(["web", "serve", "--factory", "approval_host:not_a_server"], capsys)
    assert code == 2
    assert "WorkMcpServer" in err


def test_missing_web_extra_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("id42_work.web._dependency.importlib.import_module", _missing)
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY], capsys)
    assert code == 1
    assert "id42-work[web]" in err


def test_missing_mcp_extra_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing() -> None:
        msg = "MCP support is not installed. Install it with: pip install 'id42-work[mcp]'"
        raise ImportError(msg)

    monkeypatch.setattr("id42_work.mcp._dependency.require_mcp", _missing)
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY], capsys)
    assert code == 1
    assert "id42-work[mcp]" in err


def test_serve_binds_loopback_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def _run(app: object, **kwargs: object) -> None:
        del app
        captured.update(kwargs)

    monkeypatch.setenv("ID42_WORK_WEB_TOKEN", "cli-operator")
    monkeypatch.setattr("uvicorn.run", _run)
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY], capsys)
    assert code == 0
    assert err == ""
    assert captured["host"] == DEFAULT_HOST == "127.0.0.1"
    assert captured["port"] == DEFAULT_PORT
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
    assert captured["server_header"] is False


def test_a_public_address_is_refused_before_the_server_starts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _run(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "uvicorn should not start"
        raise AssertionError(msg)

    monkeypatch.setattr("uvicorn.run", _run)
    code, _out, err = _invoke(
        ["web", "serve", "--factory", FACTORY, "--host", "0.0.0.0"],  # noqa: S104
        capsys,
    )
    assert code == 2
    assert "loopback" in err
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY, "--port", "0"], capsys)
    assert code == 2
    assert "port" in err


def test_the_server_refuses_to_start_without_authentication(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ID42_WORK_WEB_TOKEN", raising=False)
    monkeypatch.delenv("ID42_WORK_WEB_TOKEN_FILE", raising=False)
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY], capsys)
    assert code == 2
    assert "not configured" in err
    assert "cli-operator" not in err


def test_credential_auth_can_replace_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ID42_WORK_WEB_TOKEN", raising=False)
    monkeypatch.delenv("ID42_WORK_WEB_TOKEN_FILE", raising=False)
    seen: dict[str, object] = {}

    def _run(app: object, **kwargs: object) -> None:
        del app
        seen.update(kwargs)

    monkeypatch.setattr("uvicorn.run", _run)
    code, _out, err = _invoke(["web", "serve", "--factory", FACTORY, "--credential-auth"], capsys)
    assert (code, err) == (0, "")
    assert seen["host"] == "127.0.0.1"


def test_a_private_token_file_is_accepted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "operator.token"
    path.write_text("file-token\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("ID42_WORK_WEB_TOKEN", "should-not-be-used")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    code, _out, err = _invoke(
        ["web", "serve", "--factory", FACTORY, "--token-file", str(path)], capsys
    )
    assert (code, err) == (0, "")
    loose = tmp_path / "loose.token"
    loose.write_text("visible-token\n", encoding="utf-8")
    loose.chmod(0o644)
    code, _out, err = _invoke(
        ["web", "serve", "--factory", FACTORY, "--token-file", str(loose)], capsys
    )
    assert code == 2
    assert "group or others" in err
    assert "visible-token" not in err


def test_bad_server_options_are_type_errors() -> None:
    approval_host.reset()
    server = approval_host.server()
    with pytest.raises(TypeError, match="flag"):
        serve_web(server, credential_auth="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="path"):
        serve_web(server, token_file=1)  # type: ignore[arg-type]


def test_package_import_does_not_pull_the_web_stack() -> None:
    script = (
        "import id42_work, id42_work.cli, sys; "
        "assert 'starlette' not in sys.modules; "
        "assert 'id42_work.web' not in sys.modules"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert os.environ.get("ID42_WORK_WEB_TOKEN", "") != "loaded"


def test_public_attributes_load_the_extra() -> None:
    assert callable(web.create_app)
    assert callable(web.serve_web)
    with pytest.raises(AttributeError):
        web.__getattr__("missing")
