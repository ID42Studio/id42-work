"""Server-rendered pages. Jinja autoescape is on. There is no script."""

from __future__ import annotations

from jinja2 import DictLoader, Environment, StrictUndefined

_CSS = """
:root {
  color-scheme: light;
  --bg: #f3f5f4;
  --paper: #ffffff;
  --ink: #1b2421;
  --muted: #5c6b66;
  --line: #d5deda;
  --accent: #0e6b58;
  --accent-ink: #ffffff;
  --danger: #8d2f2f;
  --danger-bg: #f8ecec;
  --ok-bg: #e7f4ef;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.5 ui-sans-serif, system-ui, sans-serif;
}
header, main {
  width: min(960px, calc(100% - 32px));
  margin: 0 auto;
}
header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  padding: 24px 0 8px;
}
h1, h2 { margin: 0 0 8px; }
h1 { font-size: 1.35rem; }
h2 { font-size: 1.05rem; }
p { margin: 0 0 12px; }
.muted { color: var(--muted); }
main { padding-bottom: 48px; }
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--paper);
}
th, td {
  padding: 8px 10px;
  text-align: left;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}
a { color: var(--accent); }
form.card, .card {
  display: grid;
  gap: 12px;
  max-width: 40rem;
  margin: 16px 0;
  padding: 16px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
label { display: grid; gap: 4px; font-weight: 600; }
input, textarea {
  font: inherit;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
}
textarea { min-height: 6rem; }
button, .button {
  font: inherit;
  width: fit-content;
  padding: 8px 14px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: var(--accent);
  color: var(--accent-ink);
  text-decoration: none;
}
button.danger { background: var(--danger); }
button.quiet {
  background: transparent;
  color: var(--ink);
  border-color: var(--line);
}
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.flash {
  padding: 10px 12px;
  border-radius: 6px;
  background: var(--ok-bg);
}
.flash.error { background: var(--danger-bg); }
pre.record {
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 12px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 8px;
}
header form { margin: 0; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
"""

_LAYOUT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{{ title }} · ID42 Work</title>
<style>{{ css }}</style>
</head>
<body>
<header>
<div>
<h1>ID42 Work</h1>
<p class="muted">Operator decisions</p>
</div>
{% if actor_id %}
<form method="post" action="/logout">
<input type="hidden" name="csrf" value="{{ csrf }}">
<p class="muted">Signed in as {{ actor_id }}</p>
<button class="quiet" type="submit">Sign out</button>
</form>
{% endif %}
</header>
<main>
{% if flash %}
<p class="flash{% if flash_error %} error{% endif %}" role="status">{{ flash }}</p>
{% endif %}
{% block body %}{% endblock %}
</main>
</body>
</html>
"""

_LOGIN = """{% extends "layout.html" %}
{% block body %}
<h2>Sign in</h2>
<p class="muted">Decisions use the same consoles as the command line.
This page is not an MCP tool.</p>
<form class="card" method="post" action="/login">
<input type="hidden" name="csrf" value="{{ csrf }}">
{% if token_auth %}
<label>Operator token
<input type="password" name="token" autocomplete="off" required>
</label>
<label>Actor id
<input type="text" name="actor_id" autocomplete="off" maxlength="256" required>
</label>
{% endif %}
{% if credential_auth %}
<label>Actor credential
<input type="password" name="credential" autocomplete="off">
</label>
<p class="muted">The credential is checked with this process's authority.
When a token is configured, the token is required as well.</p>
{% endif %}
<button type="submit">Sign in</button>
</form>
{% endblock %}
"""

_HOME = """{% extends "layout.html" %}
{% block body %}
<h2>Pending approvals</h2>
{% if approvals %}
<table>
<thead><tr>
<th>Work</th><th>Gate</th><th>Capability</th><th>Status</th>
</tr></thead>
<tbody>
{% for row in approvals %}
<tr>
<td><a href="/approvals/{{ row.approval_id }}">{{ row.work_id }}</a></td>
<td>{{ row.gate }}</td>
<td>{{ row.capability }}</td>
<td>{{ row.execution_status }}</td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<p>No approvals are waiting.</p>
{% endif %}
<h2>Uncertain calls</h2>
{% if calls %}
<table>
<thead><tr>
<th>Work</th><th>Action</th><th>Capability</th><th>Status</th>
</tr></thead>
<tbody>
{% for row in calls %}
<tr>
<td><a href="{{ row.href }}">{{ row.work_id }}</a></td>
<td>{{ row.action_id }}</td>
<td>{{ row.capability }}</td>
<td>{{ row.execution_status }}</td>
</tr>
{% endfor %}
</tbody>
</table>
{% else %}
<p>No uncertain calls are waiting.</p>
{% endif %}
{% endblock %}
"""

_APPROVAL = """{% extends "layout.html" %}
{% block body %}
<p><a href="/">All decisions</a></p>
<h2>Approval</h2>
<pre class="record">{{ record_json }}</pre>
{% if pending %}
<form class="card" method="post" action="/approvals/{{ approval_id }}/approve">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Approve</h2>
<label>Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off">
</label>
<label class="row"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Approve</button>
</form>
<form class="card" method="post" action="/approvals/{{ approval_id }}/reject">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Reject</h2>
<label>Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<button class="danger" type="submit">Reject</button>
</form>
{% else %}
<p>This approval is no longer waiting.</p>
{% endif %}
{% endblock %}
"""

_RECONCILE = """{% extends "layout.html" %}
{% block body %}
<p><a href="/">All decisions</a></p>
<h2>Uncertain call</h2>
<pre class="record">{{ record_json }}</pre>
{% if open_call %}
<form class="card" method="post" action="{{ action }}/applied">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Applied</h2>
<label>Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<label>Result JSON
<textarea name="result" autocomplete="off" required></textarea>
</label>
<label class="row"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Mark applied</button>
</form>
<form class="card" method="post" action="{{ action }}/not-applied">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Not applied</h2>
<label>Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<label class="row"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Mark not applied</button>
</form>
<form class="card" method="post" action="{{ action }}/abandon">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Abandon</h2>
<p class="muted">The execution stays failed.</p>
<label>Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<button class="danger" type="submit">Abandon</button>
</form>
{% else %}
<p>This call is no longer uncertain.</p>
{% endif %}
{% endblock %}
"""

_MESSAGE = """{% extends "layout.html" %}
{% block body %}
<h2>{{ heading }}</h2>
<p>{{ message }}</p>
{% if show_login %}
<p><a href="/login">Sign in</a></p>
{% endif %}
{% endblock %}
"""

_ENV = Environment(
    loader=DictLoader(
        {
            "layout.html": _LAYOUT,
            "login.html": _LOGIN,
            "home.html": _HOME,
            "approval.html": _APPROVAL,
            "reconcile.html": _RECONCILE,
            "message.html": _MESSAGE,
        }
    ),
    autoescape=True,
    undefined=StrictUndefined,
)


def render(template: str, **context: object) -> str:
    """Render one page. ``css`` is injected so templates stay free of the stylesheet."""
    context.setdefault("css", _CSS)
    return _ENV.get_template(template).render(**context)
