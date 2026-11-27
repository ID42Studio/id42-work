"""``id42-work approvals`` loads the MCP factory and does not decide as the host actor."""

import asyncio
import json
from uuid import UUID

import pytest

import approval_host
from id42_work import EvidenceType, ExecutionStatus
from id42_work.cli import main

FACTORY = "approval_host:server"


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


async def _start(key: str) -> str:
    hosted = approval_host.server()
    execution = await hosted.runner.run(
        work_id=approval_host.WORK_ID,
        version=approval_host.WORK_VERSION,
        input_data={"text": "hello"},
        actor=hosted.host_actor(),
        idempotency_key=key,
    )
    assert execution.status is ExecutionStatus.WAITING_APPROVAL
    (approval,) = await hosted.runner.approvals.list_pending(execution.execution_id)
    return str(approval.approval_id)


def test_help_lists_the_commands_and_does_not_load_a_factory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _err = _invoke(["approvals", "--help"], capsys)
    assert code == 0
    assert "list" in out
    assert "approve" in out
    assert "reject" in out
    assert "Not an MCP tool" in out
    code, out, _err = _invoke(["approvals", "approve", "--help"], capsys)
    assert code == 0
    assert "--resume" in out
    assert "--factory" in out
    assert "--actor" in out


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["approvals"], capsys)
    assert code == 2
    assert err
    code, _out, err = _invoke(["approvals", "list"], capsys)
    assert code == 2
    assert "factory" in err
    code, _out, err = _invoke(["approvals", "approve", "x", "--factory", FACTORY], capsys)
    assert code == 2
    assert "actor" in err
    code, _out, err = _invoke(
        ["approvals", "reject", "x", "--factory", FACTORY, "--actor", "id42-operator"], capsys
    )
    assert code == 2
    assert "reason" in err


def test_a_bad_factory_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = _invoke(["approvals", "list", "--factory", "not a factory"], capsys)
    assert code == 2
    assert "module:callable" in err
    code, _out, err = _invoke(
        ["approvals", "list", "--factory", "approval_host_missing:server"], capsys
    )
    assert code == 2
    assert "cannot import" in err
    code, _out, err = _invoke(["approvals", "list", "--factory", "approval_host:missing"], capsys)
    assert code == 2
    assert "was not found" in err
    code, _out, err = _invoke(
        ["approvals", "list", "--factory", "approval_host:not_a_server"], capsys
    )
    assert code == 2
    assert "WorkMcpServer" in err


def test_missing_extra_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("id42_work.mcp._dependency.importlib.import_module", _missing)
    code, _out, err = _invoke(["approvals", "list", "--factory", FACTORY], capsys)
    assert code == 1
    assert "id42-work[mcp]" in err


def test_list_is_empty_until_an_execution_is_waiting(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    code, out, err = _invoke(["approvals", "list", "--factory", FACTORY], capsys)
    assert (code, out.strip(), err) == (0, "pending-none", "")


def test_approve_without_resume_records_the_operator(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_start("n-1"))
    code, out, err = _invoke(["approvals", "list", "--factory", FACTORY], capsys)
    assert code == 0
    assert err == ""
    assert _field(out, "approval_id") == approval_id
    assert _field(out, "actor_id") == "id42-agent"
    assert _field(out, "reviewer_capability") == "approve:note"
    assert _field(out, "capability") == "notes.append"
    assert _field(out, "execution_status") == "WAITING_APPROVAL"

    code, out, err = _invoke(["approvals", "show", approval_id, "--factory", FACTORY], capsys)
    assert code == 0
    shown = json.loads(out)
    assert shown["params_preview"] == {"text": "hello"}
    assert shown["reason"] is None
    assert shown["decision_actor_id"] is None

    code, out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            " id42-operator ",
            "--reason",
            "checked",
        ],
        capsys,
    )
    assert code == 0
    assert err == ""
    assert _field(out, "approval_status") == "GRANTED"
    assert _field(out, "execution_status") == "READY"
    assert _field(out, "resumed") == "false"
    assert _field(out, "decision_actor_id") == "id42-operator"
    assert approval_host.calls() == 0
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    evidence = asyncio.run(hosted.runner.evidence(approval.execution_id))
    recorded = [item for item in evidence if item.evidence_type is EvidenceType.APPROVAL_DECISION]
    assert recorded[-1].metadata["decision_actor_id"] == "id42-operator"
    assert recorded[-1].metadata["reason"] == "checked"


def test_show_after_reject_and_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_start("n-reject"))
    code, out, err = _invoke(
        [
            "approvals",
            "reject",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "no",
        ],
        capsys,
    )
    assert code == 0
    assert _field(out, "approval_status") == "REJECTED"
    assert _field(out, "execution_status") == "REJECTED"
    assert _field(out, "resumed") == "false"
    assert _field(out, "decision_actor_id") == "id42-operator"
    assert approval_host.calls() == 0
    assert err == ""

    code, out, _err = _invoke(["approvals", "show", approval_id, "--factory", FACTORY], capsys)
    assert code == 0
    shown = json.loads(out)
    assert shown["reason"] == "no"
    assert shown["decision_actor_id"] == "id42-operator"
    assert shown["decided_at"] is not None
    assert shown["execution_status"] == "REJECTED"

    code, _out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
        ],
        capsys,
    )
    assert code == 1
    assert "not PENDING" in err
    assert approval_host.calls() == 0


def test_resume_runs_as_the_host_actor(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_start("n-resume"))
    code, out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "checked",
            "--resume",
        ],
        capsys,
    )
    assert code == 0
    assert err == ""
    assert _field(out, "execution_status") == "COMPLETED"
    assert _field(out, "resumed") == "true"
    assert _field(out, "decision_actor_id") == "id42-operator"
    assert approval_host.calls() == 1


def test_the_execution_actor_cannot_decide(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_start("n-self"))
    code, _out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-agent",
            "--reason",
            "no",
        ],
        capsys,
    )
    assert code == 1
    assert "own execution" in err
    assert approval_host.calls() == 0
    code, out, _err = _invoke(["approvals", "list", "--factory", FACTORY], capsys)
    assert code == 0
    assert _field(out, "execution_status") == "WAITING_APPROVAL"


def test_bad_ids_and_a_failed_resume_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    approval_host.reset()
    code, _out, err = _invoke(["approvals", "show", "not-a-uuid", "--factory", FACTORY], capsys)
    assert code == 2
    assert "UUID" in err
    code, _out, err = _invoke(
        [
            "approvals",
            "approve",
            "00000000-0000-0000-0000-000000000000",
            "--factory",
            FACTORY,
            "--actor",
            "   ",
        ],
        capsys,
    )
    assert code == 2
    assert "actor" in err
    code, _out, err = _invoke(
        [
            "approvals",
            "approve",
            "00000000-0000-0000-0000-000000000000",
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
        ],
        capsys,
    )
    assert code == 1
    assert "does not exist" in err

    approval_id = asyncio.run(_start("n-broken"))
    code, _out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--reason",
            "   ",
        ],
        capsys,
    )
    assert code == 1
    assert "reason" in err
    approval_host.break_resume()
    code, out, err = _invoke(
        [
            "approvals",
            "approve",
            approval_id,
            "--factory",
            FACTORY,
            "--actor",
            "id42-operator",
            "--resume",
        ],
        capsys,
    )
    assert code == 1
    assert _field(out, "approval_status") == "GRANTED"
    assert _field(out, "execution_status") == "READY"
    assert _field(out, "resumed") == "false"
    assert "was granted" in err
    assert approval_host.calls() == 0
    approval_host.reset()
