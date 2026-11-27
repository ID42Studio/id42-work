"""``id42-work reconcile`` loads the MCP factory and does not decide as the host actor."""

import argparse
import asyncio
import json
from uuid import UUID, uuid4

import pytest

import reconcile_host
from id42_work import EvidenceType, ExecutionStatus
from id42_work.cli import main
from id42_work.reconciliation.commands import run_reconcile
from id42_work.reconciliation.console import console_from_server

FACTORY = "reconcile_host:server"


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    code = raised.value.code
    assert isinstance(code, int)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _field(line: str, name: str) -> str:
    prefix = f"{name}="
    for token in line.split():
        if token.startswith(prefix):
            return token[len(prefix) :]
    raise AssertionError(line)


async def _start(key: str) -> tuple[str, str]:
    hosted = reconcile_host.server()
    execution = await hosted.runner.run(
        work_id=reconcile_host.WORK_ID,
        version=reconcile_host.WORK_VERSION,
        input_data={"text": "hello"},
        actor=hosted.host_actor(),
        idempotency_key=key,
    )
    assert execution.status is ExecutionStatus.FAILED
    assert execution.requires_reconciliation
    return str(execution.execution_id), "append-note"


def test_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = _invoke(["reconcile", "--help"], capsys)
    assert code == 0
    assert "Not an MCP tool" in out
    for name in ("list", "show", "applied", "not-applied", "abandon"):
        assert name in out
    code, out, _err = _invoke(["reconcile", "applied", "--help"], capsys)
    assert code == 0
    assert "--resume" in out
    assert "--factory" in out
    assert "--result" in out
    code, out, _err = _invoke(["reconcile", "abandon", "--help"], capsys)
    assert code == 0
    assert "--resume" not in out


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["reconcile"], capsys)
    assert code == 2
    assert err
    code, _out, err = _invoke(["reconcile", "list"], capsys)
    assert code == 2
    assert "factory" in err
    code, _out, err = _invoke(
        ["reconcile", "applied", "x", "append-note", "--factory", FACTORY], capsys
    )
    assert code == 2
    assert "actor" in err


def test_a_bad_factory_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["reconcile", "list", "--factory", "not a factory"], capsys)
    assert code == 2
    assert "module:callable" in err
    code, _out, err = _invoke(
        ["reconcile", "list", "--factory", "reconcile_host_missing:server"], capsys
    )
    assert code == 2
    assert "cannot import" in err
    code, _out, err = _invoke(["reconcile", "list", "--factory", "reconcile_host:missing"], capsys)
    assert code == 2
    assert "was not found" in err
    code, _out, err = _invoke(
        ["reconcile", "list", "--factory", "reconcile_host:not_a_server"], capsys
    )
    assert code == 2
    assert "WorkMcpServer" in err


def test_missing_extra_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("id42_work.mcp._dependency.importlib.import_module", _missing)
    code, _out, err = _invoke(["reconcile", "list", "--factory", FACTORY], capsys)
    assert code == 1
    assert "id42-work[mcp]" in err


def test_list_is_empty_until_a_call_is_uncertain(capsys: pytest.CaptureFixture[str]) -> None:
    reconcile_host.reset()
    code, out, err = _invoke(["reconcile", "list", "--factory", FACTORY], capsys)
    assert (code, out.strip(), err) == (0, "uncertain-none", "")


def test_applied_without_resume_records_the_operator(capsys: pytest.CaptureFixture[str]) -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_start("n-1"))
    code, out, err = _invoke(["reconcile", "list", "--factory", FACTORY], capsys)
    assert code == 0
    assert err == ""
    assert _field(out, "action_id") == action_id
    assert _field(out, "execution_status") == "FAILED"
    code, out, err = _invoke(
        [
            "reconcile",
            "applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "confirmed in the store",
            "--result",
            "null",
        ],
        capsys,
    )
    assert code == 0
    assert err == ""
    assert _field(out, "journal_status") == "SUCCEEDED"
    assert _field(out, "resumed") == "false"
    assert _field(out, "decision_actor_id") == "id42-operator"
    assert reconcile_host.calls() == 1
    hosted = reconcile_host.server()
    evidence = asyncio.run(hosted.runner.evidence(UUID(execution_id)))
    recorded = [
        item for item in evidence if item.evidence_type is EvidenceType.RECONCILIATION_DECISION
    ]
    assert recorded[-1].metadata["decision_actor_id"] == "id42-operator"
    assert recorded[-1].metadata["reason"] == "confirmed in the store"
    code, out, err = _invoke(
        ["reconcile", "show", execution_id, action_id, "--factory", FACTORY], capsys
    )
    assert code == 0
    shown = json.loads(out)
    assert shown["journal_status"] == "SUCCEEDED"
    assert shown["result_digest"]
    assert "result" not in shown


def test_not_applied_resume_retries_as_the_host_actor(capsys: pytest.CaptureFixture[str]) -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_start("n-retry"))
    code, out, err = _invoke(
        [
            "reconcile",
            "not-applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "the note was not stored",
            "--resume",
        ],
        capsys,
    )
    assert code == 0
    assert err == ""
    assert _field(out, "resumed") == "true"
    assert _field(out, "execution_status") == "COMPLETED"
    assert _field(out, "decision_actor_id") == "id42-operator"
    assert reconcile_host.calls() == 2


def test_abandon_and_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_start("n-stop"))
    code, out, err = _invoke(
        [
            "reconcile",
            "abandon",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "cannot be known",
        ],
        capsys,
    )
    assert code == 0
    assert err == ""
    assert _field(out, "journal_status") == "ABANDONED"
    assert _field(out, "execution_status") == "FAILED"
    assert _field(out, "status_reason") == "reconciliation_abandoned"
    assert reconcile_host.calls() == 1
    code, _out, err = _invoke(
        [
            "reconcile",
            "not-applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "later",
        ],
        capsys,
    )
    assert code == 1
    assert "closed" in err
    code, _out, err = _invoke(
        [
            "reconcile",
            "applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-agent",
            "--reason",
            "self",
            "--result",
            "{}",
        ],
        capsys,
    )
    assert code == 1
    assert "own execution" in err


def test_bad_ids_and_a_failed_resume_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    reconcile_host.reset()
    code, _out, err = _invoke(
        ["reconcile", "show", "not-a-uuid", "append-note", "--factory", FACTORY], capsys
    )
    assert code == 2
    assert "UUID" in err
    code, _out, err = _invoke(
        [
            "reconcile",
            "applied",
            "00000000-0000-0000-0000-000000000000",
            "append-note",
            "--factory",
            FACTORY,
            "--actor",
            "   ",
            "--reason",
            "x",
            "--result",
            "null",
        ],
        capsys,
    )
    assert code == 2
    assert "actor" in err
    code, _out, err = _invoke(
        [
            "reconcile",
            "applied",
            "00000000-0000-0000-0000-000000000000",
            "append-note",
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "x",
            "--result",
            "not-json",
        ],
        capsys,
    )
    assert code == 2
    assert "JSON" in err
    code, _out, err = _invoke(
        [
            "reconcile",
            "not-applied",
            "00000000-0000-0000-0000-000000000000",
            "append-note",
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "x",
        ],
        capsys,
    )
    assert code == 1
    assert "is missing" in err

    execution_id, action_id = asyncio.run(_start("n-broken"))
    reconcile_host.break_resume()
    code, out, err = _invoke(
        [
            "reconcile",
            "not-applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "absent",
            "--resume",
        ],
        capsys,
    )
    assert code == 1
    assert _field(out, "journal_status") == "NOT_APPLIED"
    assert _field(out, "resumed") == "false"
    assert "resume failed" in err
    assert reconcile_host.calls() == 1

    reconcile_host._STATE.pop("broken_resume", None)
    execution_id, action_id = asyncio.run(_start("n-applied-broken"))
    reconcile_host.break_resume()
    code, out, err = _invoke(
        [
            "reconcile",
            "applied",
            execution_id,
            action_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "confirmed",
            "--result",
            '{"stored": true}',
            "--resume",
        ],
        capsys,
    )
    assert code == 1
    assert _field(out, "journal_status") == "SUCCEEDED"
    assert "resume failed" in err


def test_command_helpers_reject_malformed_values() -> None:
    hosted = reconcile_host.server()

    async def check() -> None:
        with pytest.raises(ValueError, match="unknown reconcile"):
            await run_reconcile(
                hosted,
                argparse.Namespace(
                    reconcile_command="nope",
                    execution_id=str(uuid4()),
                    action_id="append-note",
                    actor="operator",
                    reason="because",
                ),
            )
        with pytest.raises(ValueError, match="UUID"):
            await run_reconcile(
                hosted,
                argparse.Namespace(
                    reconcile_command="show", execution_id=uuid4(), action_id="append-note"
                ),
            )
        with pytest.raises(ValueError, match="action id"):
            await run_reconcile(
                hosted,
                argparse.Namespace(
                    reconcile_command="show",
                    execution_id=str(uuid4()),
                    action_id="  ",
                ),
            )
        with pytest.raises(ValueError, match="resume must be a flag"):
            await run_reconcile(
                hosted,
                argparse.Namespace(
                    reconcile_command="not-applied",
                    execution_id=str(uuid4()),
                    action_id="append-note",
                    actor="operator",
                    reason="because",
                    resume="yes",
                ),
            )
        with pytest.raises(ValueError, match="result must be a JSON"):
            await run_reconcile(
                hosted,
                argparse.Namespace(
                    reconcile_command="applied",
                    execution_id=str(uuid4()),
                    action_id="append-note",
                    actor="operator",
                    reason="because",
                    result=None,
                    resume=False,
                ),
            )

    asyncio.run(check())
    with pytest.raises(TypeError, match="WorkMcpServer"):
        console_from_server(object())

    class Incomplete:
        runner = hosted.runner
        authority = hosted.authority
        host_actor = None

    with pytest.raises(TypeError, match="WorkMcpServer"):
        console_from_server(Incomplete())
