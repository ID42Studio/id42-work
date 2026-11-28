# Operator UI

`id42-work[web]` is a small server-rendered page for the decisions in
[Approval](concepts/approval.md) and [Reconciliation](concepts/reconciliation.md).
It lists what is waiting and posts the decision to `ApprovalConsole` or
`ReconciliationConsole`. It does not call the approval manager or the
reconciliation service itself, and it does not add a tool to the
[MCP server](mcp.md).

```bash
pip install "id42-work[web,mcp]"
```

The `web` extra is Starlette, Jinja2 and uvicorn. The command also needs the
`mcp` extra, because `--factory` returns a `WorkMcpServer`, the same contract
as `id42-work mcp serve`. Importing `id42_work` does not import either extra.

## Serve

```bash
ID42_WORK_WEB_TOKEN=replace-me \
  id42-work web serve --factory myapp.governed:server
```

Open `http://127.0.0.1:8767/`. The default address is loopback port 8767, so it
can run beside MCP HTTP on port 8766. `--host` may name another loopback IP,
such as `::1`. Any other address is refused, including `localhost`, `0.0.0.0`
and an IPv4-mapped address. A browser on another machine needs a tunnel.

The process exits with status 2 when the factory, the bind address or the
authentication settings are invalid, and with status 1 when the `web` or `mcp`
extra is not installed.

## Authentication

One of these is required. With none of them, the process does not start.

| Source | Role |
|---|---|
| `--token-file` | A regular file, mode `0600`, not a symlink. One UTF-8 line, at most 4096 bytes. |
| `ID42_WORK_WEB_TOKEN_FILE` | The same kind of file, used when `--token-file` is omitted. |
| `ID42_WORK_WEB_TOKEN` | The token itself, used when neither file is set. |
| `--credential-auth` | Accept a `TrustedActor` credential that this factory's `ActorAuthority.verify` accepts. |

`--token-file` wins over the file environment variable, which wins over
`ID42_WORK_WEB_TOKEN`. The process keeps a SHA-256 digest and compares it with
`hmac.compare_digest`. The token is not logged and not written into a page.

When `--credential-auth` is set and a token is also configured, the token must
still match. The credential chooses the actor id. A typed actor id, if present,
must be that same id. The credential does not choose the grant. Each decision
seals a new actor with only `approve:<work_id>` (or the gate's reviewer grant)
or `reconcile:<work_id>`.

The factory should pass the same `key_path` the MCP server uses, so a
credential issued earlier still verifies. See [Security](security.md).

The session cookie is `HttpOnly` and `SameSite=Strict`, and it expires after
eight hours. Sign out drops it. Every POST also sends the CSRF token from the
page, and `Origin` or `Referer` must match this server. A failed check changes
nothing.

## What the pages do

Approvals are listed at `/`. Uncertain calls are listed at `/reconcile`.
Each row shows a status and the time it was requested. An approval page and a
call page show the same fields as `id42-work approvals show` and
`id42-work reconcile show`, as a labeled summary. The parameter preview is the
redacted preview. The provider result appears only as its digest. The header
names the factory passed to `web serve`.

Approve and reject match the CLI. Reject and abandon ask for confirmation
before the console is called. Reject, applied, not-applied and abandon need a
reason. Applied also needs a JSON result (`null` is allowed). Resume is off
unless the form asks for it, and resume runs as the factory's host actor, not
as the operator. Abandon has no resume. A decision that the console refuses is
shown on the page and is not stored twice. The pages use one local stylesheet
and do not require JavaScript.

Do not put the operator token in a URL, a bookmark or a screenshot.
