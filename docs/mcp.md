# MCP server

`id42-work[mcp]` exposes governed execution to a Model Context Protocol client.
The server sits on `WorkRunner`. The client can start, inspect, resume, list and
cancel executions of works the host registered. It cannot supply an actor, a grant
or an approval, and it cannot call a capability except by running a registered
executor.

```bash
pip install "id42-work[mcp]"
```

The core package does not depend on the MCP SDK. `import id42_work` succeeds
without the extra. `id42_work.mcp` raises `ImportError` until the extra is
installed, and names the install command.

## What the client can do

Tools:

| Tool | Arguments | Effect |
|---|---|---|
| `list_works` | optional `limit`, optional `cursor` | One page of registered works, with schemas. No executable code. |
| `start_execution` | `work_id`, `version`, `input_data`, optional `idempotency_key` | `runner.run` as the request actor. |
| `get_execution` | `execution_id` | Status and verified output. |
| `resume_execution` | `execution_id` | `runner.resume`. A waiting approval stays waiting. |
| `list_executions` | optional `status`, `limit`, `cursor` | One page of summaries for executions the request actor owns. |
| `cancel_execution` | `execution_id`, optional `reason` | `runner.cancel`. Cancellation is not an approval. |

Resources, all `application/json`:

- `id42://executions/{execution_id}/journal`
- `id42://executions/{execution_id}/events`
- `id42://executions/{execution_id}/evidence`
- `id42://executions/{execution_id}/approvals`

There is no approve, reject or reconcile tool. `id42-work approvals` and
`id42-work reconcile` are separate commands, not tools on this server. The
optional [operator UI](web.md) calls those same consoles and does not add a
tool either. Extra arguments such as `actor_id`, `grants` or a credential are
ignored. Amounts that the work schema types as `Decimal` must be JSON strings.
A float would change the action digest.

`limit` defaults to 50 and cannot exceed 100. `cursor` is opaque (`id42c1.`).
Pass the previous response's `next_cursor` to read the next page. `next_cursor`
is null on the last page. A cursor is a position, not a capability: the server
still checks the actor.

## What stays gated

The process that serves MCP is the host. It registers works, policies and
providers, and it issues actors.

- By default the actor is re-issued on every call from `issue_actor`. The client
  cannot name one. The first successful issue pins `actor_id` and `tenant_id`
  for the process. A later issue with a different identity is refused. A
  credential header is ignored in this mode, so another process on the loopback
  interface cannot become a different actor.
- A host that needs one actor per HTTP session passes `session_actor`.
  `credential_session_actor` reads the `Id42-Actor-Credential` header
  (`TrustedActor.to_credential`). The server still runs `ActorAuthority.verify`.
  On HTTP a missing header is refused and is not replaced by the factory actor.
  Stdio has no headers, so the fallback (pass `issue_actor`) stays one pinned
  actor. The first verified identity of a session (`mcp-session-id`) is pinned.
  A later credential for a different actor in that session is refused. A new
  seal for the same actor and tenant is accepted. A request with no session id
  does not share the stdio pin.
- A sealed actor that this server's `ActorAuthority` does not accept is refused
  before the runner sees it.
- Reads and writes are limited to executions with that actor id and tenant. The
  execution page is filtered in storage, before the limit. An execution owned by
  someone else is reported as not found, including to `resume_execution` and
  `cancel_execution`. A `resume:` or `cancel:` grant does not widen this. The
  runner itself is less strict: those grants can resume or cancel another
  actor's execution when you call it directly.
- `host_actor()` stays the factory actor from `issue_actor`. `id42-work approvals --resume`
  and `id42-work reconcile` keep using that actor, not the session credential.
- Policies, validators and approval gates run inside the runner. `start_execution`
  returns `WAITING_APPROVAL` or `WAITING_REVIEW` and does not wait for the person.
  `resume_execution` does not decide. The host approves or rejects with
  `id42-work approvals` or `ApprovalConsole`, outside the MCP server, then the
  client may resume. An `UNCERTAIN` capability call is reconciled the same way,
  with `id42-work reconcile` or `ReconciliationConsole`. `resume_execution` does
  not mark it applied. The operator is a different actor from the one this server
  issues.
- Journal results pass through the default redactor. Approval parameter previews
  and action digests are omitted. Events and evidence are the records the runner
  already stored.

`WorkRunner.list_executions()` with no query returns every execution in storage,
with no actor filter. Pass an `ExecutionListQuery` before handing rows to an
untrusted caller. The MCP tools do that for the verified actor. Do not hand the
unfiltered list to a remote client.

## Serve

The host factory is `module:callable` and returns a `WorkMcpServer`:

```python
def server() -> WorkMcpServer:
    return WorkMcpServer(runner, authority=authority, issue_actor=issue)
```

That server has one actor, the factory's. To give each HTTP session its own
verified actor, pass `session_actor`. Stdio still uses `issue`:

```python
from id42_work.mcp import credential_session_actor


def server() -> WorkMcpServer:
    return WorkMcpServer(
        runner,
        authority=authority,
        issue_actor=issue,
        session_actor=credential_session_actor(fallback=issue),
    )
```

The HTTP client sends `Id42-Actor-Credential` with the value of
`TrustedActor.to_credential()`. The header is a bearer token. `--http` still
binds `127.0.0.1` only. Do not publish that port.

`issue` calls `authority.issue(...)` and returns a `TrustedActor`. Keep the
authority, the runner and the factory in the host process. Pass the same
`key_path` into `ActorAuthority` when `id42-work approvals` or
`id42-work reconcile` must verify a seal issued by an earlier process. The
constructor does not read the environment; the host reads
`ID42_WORK_AUTHORITY_KEY_PATH` with `authority_key_path_from_env` and passes
that path. See [Security](security.md).

```bash
id42-work mcp serve --factory myapp.governed:server
id42-work mcp serve --factory myapp.governed:server --http --port 8766
```

Stdio is the default. `--http` serves streamable HTTP on `127.0.0.1` only. The
bind address is not a flag. The default port is 8766, and the MCP path is `/mcp`.
`--port` without `--http` exits 2. A missing extra exits 1. A factory that cannot
be imported, is not callable, or does not return a `WorkMcpServer` exits 2.

`examples/mcp_greeting.py` is a complete factory. Running the file starts one
execution and prints `OK`. Serving it:

```bash
id42-work mcp serve --factory examples.mcp_greeting:server
```

## nanobot 0.4.2

[id42-agent](https://github.com/ID42Studio/id42-agent) runs nanobot `v0.4.2`
(commit `0dc8679edcc6061fbcdf3de13c7e16000e9d1b65`) in Docker with a local Ollama
model. nanobot reads MCP servers from `tools.mcpServers`. Keys are camelCase.
`type` may be `stdio`, `sse` or `streamableHttp`. When `type` is omitted, a
`command` is stdio, a `url` ending in `/sse` is SSE, and any other `url` is
streamable HTTP.

Stdio is the shape to use once `id42-work` is on `PATH` inside the nanobot
process. This repository does not build that image.

```json
{
  "tools": {
    "mcpServers": {
      "id42-work": {
        "command": "id42-work",
        "args": ["mcp", "serve", "--factory", "myapp.governed:server"],
        "toolTimeout": 120
      }
    }
  }
}
```

On one machine, without Docker, streamable HTTP also works. nanobot blocks
loopback and private addresses unless `tools.ssrfWhitelist` names them. Prefer
a single-host CIDR.

```json
{
  "tools": {
    "ssrfWhitelist": ["127.0.0.1/32"],
    "mcpServers": {
      "id42-work": {
        "type": "streamableHttp",
        "url": "http://127.0.0.1:8766/mcp",
        "toolTimeout": 120
      }
    }
  }
}
```

`toolTimeout` defaults to 30 seconds. A tool call returns when the runner
returns, including at `WAITING_APPROVAL`, so a human decision is not inside that
timeout. A long executor is. Raise `toolTimeout` for those works.

Decide in another process, with the same factory. `--resume` continues as the
host actor that factory issues, not as `--actor`:

```bash
id42-work approvals list --factory myapp.governed:server
id42-work approvals approve APPROVAL_ID \
  --factory myapp.governed:server --actor id42-operator --reason "checked" --resume
```

See [Approval](concepts/approval.md). The command is not registered with the MCP
client. A host that today shells out to its own helper can drop that helper once
this command is what it runs.

Omit `enabledTools`, or set it to `["*"]`, so tools and the journal resources are
both registered. Any other list registers only those tools and skips resources.

Inside the id42-agent container, `127.0.0.1` is the container, not the host.
`host.docker.internal` is a private address and is blocked unless its CIDR is
listed in `ssrfWhitelist`. Publishing the MCP server beyond loopback is not a
supported configuration: `--http` binds `127.0.0.1` and that address cannot be
changed.
