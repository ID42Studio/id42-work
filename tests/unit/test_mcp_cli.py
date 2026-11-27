"""``id42-work mcp serve``."""

import argparse

import pytest

from id42_work.cli import main
from id42_work.mcp import WorkMcpServer


def test_main_without_argv_parses_process_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def _parse(
        self: argparse.ArgumentParser, args: object = None, namespace: object = None
    ) -> object:
        seen.append(args)
        raise SystemExit(0)

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", _parse)
    with pytest.raises(SystemExit):
        main(None)
    assert seen == [None]


def test_help_does_not_need_a_factory() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--help"])
    assert raised.value.code == 0


def test_port_requires_http(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "mcp_support:server", "--port", "9"])
    assert raised.value.code == 2
    assert "--port requires --http" in capsys.readouterr().err


def test_http_rejects_a_bad_port(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "mcp_support:server", "--http", "--port", "0"])
    assert raised.value.code == 2
    assert "port" in capsys.readouterr().err


def test_missing_extra_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("id42_work.mcp._dependency.importlib.import_module", _missing)
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "mcp_support:server"])
    assert raised.value.code == 1
    assert "id42-work[mcp]" in capsys.readouterr().err


def test_a_bad_factory_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "not a factory"])
    assert raised.value.code == 2
    assert "module:callable" in capsys.readouterr().err


def test_http_serve_uses_the_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _serve(hosted: WorkMcpServer, *, http: bool = False, port: int = 8766) -> None:
        seen["http"] = http
        seen["port"] = port
        seen["server"] = hosted

    monkeypatch.setattr("id42_work.mcp.server.serve", _serve)
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "mcp_support:server", "--http", "--port", "8766"])
    assert raised.value.code == 0
    assert seen == {"http": True, "port": 8766, "server": seen["server"]}
    assert isinstance(seen["server"], WorkMcpServer)


def test_stdio_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _serve(hosted: WorkMcpServer, *, http: bool = False, port: int = 8766) -> None:
        seen["http"] = http
        seen["port"] = port

    monkeypatch.setattr("id42_work.mcp.server.serve", _serve)
    with pytest.raises(SystemExit) as raised:
        main(["mcp", "serve", "--factory", "mcp_support:server"])
    assert raised.value.code == 0
    assert seen["http"] is False
    assert seen["port"] == 8766
