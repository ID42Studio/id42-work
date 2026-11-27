"""Server-rendered pages. Jinja autoescape is on. There is no script."""

from __future__ import annotations

from importlib.resources import files

from jinja2 import DictLoader, Environment, StrictUndefined

STYLESHEET = files("id42_work.web").joinpath("static/operator.css").read_text(encoding="utf-8")

_LAYOUT = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{{ title }} · ID42 Work</title>
<link rel="stylesheet" href="/static/operator.css">
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
{% if actor_id %}
<header class="top">
<div class="shell top-inner">
<a class="brand" href="/">
<strong><span>ID42</span> Work</strong>
<small>{{ factory if factory else "Local factory" }}</small>
</a>
<nav class="nav" aria-label="Decisions">
<a href="/" {% if section == "approvals" %}aria-current="page"{% endif %}>
Approvals <span class="count">{{ approval_count }}</span>
</a>
<a href="/reconcile" {% if section == "reconcile" %}aria-current="page"{% endif %}>
Reconciliation <span class="count">{{ call_count }}</span>
</a>
</nav>
<div class="who">
<p>Signed in as <strong>{{ actor_id }}</strong></p>
<form method="post" action="/logout">
<input type="hidden" name="csrf" value="{{ csrf }}">
<button class="quiet" type="submit">Sign out</button>
</form>
</div>
</div>
</header>
{% endif %}
<main id="content" class="shell">
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
<div class="mark">
<strong><span>ID42</span> Work</strong>
<p>{{ factory if factory else "Local factory" }}</p>
</div>
<section class="panel signin">
<h1>Sign in</h1>
<p class="lede">Decisions use the same consoles as the command line.
This page is not an MCP tool.</p>
<form method="post" action="/login">
<input type="hidden" name="csrf" value="{{ csrf }}">
{% if token_auth %}
<label class="field">Operator token
<input type="password" name="token" autocomplete="current-password" required>
</label>
<label class="field">Actor id
<input type="text" name="actor_id" autocomplete="username" maxlength="256" required>
</label>
{% endif %}
{% if credential_auth %}
<label class="field">Actor credential
<input type="password" name="credential" autocomplete="off"
{% if not token_auth %} required{% endif %}>
</label>
<p class="lede">The credential is checked with this process's authority.
When a token is configured, the token is required as well.</p>
{% endif %}
<button type="submit">Sign in</button>
</form>
</section>
{% endblock %}
"""

_HOME = """{% extends "layout.html" %}
{% block body %}
{% if section == "approvals" %}
<h1>Approvals</h1>
<p class="lede">Requests waiting for this operator.</p>
{% if approvals %}
<div class="panel">
<table>
<thead><tr>
<th>Work</th><th>Gate</th><th>Capability</th><th>Requested</th><th>Status</th>
</tr></thead>
<tbody>
{% for row in approvals %}
<tr>
<td><a class="work" href="{{ row.href }}">{{ row.work_id }}</a>
<span class="meta">{{ row.work_version }}</span></td>
<td>{{ row.gate }}</td>
<td>{{ row.capability }}</td>
<td><time datetime="{{ row.iso }}">{{ row.absolute }}</time>
<span class="meta">{{ row.relative }}</span></td>
<td><span class="badge badge-{{ row.badge }}">{{ row.status }}</span></td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
{% else %}
<section class="panel empty">
<h2>No approvals are waiting.</h2>
<p>New requests appear in this list.</p>
</section>
{% endif %}
{% else %}
<h1>Reconciliation</h1>
<p class="lede">Capability calls that are still uncertain.</p>
{% if calls %}
<div class="panel">
<table>
<thead><tr>
<th>Work</th><th>Action</th><th>Capability</th><th>Started</th><th>Status</th>
</tr></thead>
<tbody>
{% for row in calls %}
<tr>
<td><a class="work" href="{{ row.href }}">{{ row.work_id }}</a>
<span class="meta">{{ row.work_version }}</span></td>
<td>{{ row.action_id }}</td>
<td>{{ row.capability }}</td>
<td><time datetime="{{ row.iso }}">{{ row.absolute }}</time>
<span class="meta">{{ row.relative }}</span></td>
<td><span class="badge badge-{{ row.badge }}">{{ row.status }}</span></td>
</tr>
{% endfor %}
</tbody>
</table>
</div>
{% else %}
<section class="panel empty">
<h2>No uncertain calls are waiting.</h2>
<p>A call stays here until an operator records what happened.</p>
</section>
{% endif %}
{% endif %}
{% endblock %}
"""

_FIELDS = """
<dl class="summary">
{% for field in fields %}
<div{% if field.kind == "copy" %} class="wide"{% endif %}>
<dt>{{ field.label }}</dt>
<dd>
{% if field.kind == "badge" %}
<span class="badge badge-{{ field.badge }}">{{ field.value }}</span>
{% elif field.kind == "copy" %}
<input class="copy" type="text" readonly value="{{ field.value }}" aria-label="{{ field.label }}">
{% elif field.kind == "time" %}
<time datetime="{{ field.iso }}">{{ field.value }}</time>
<span class="meta">{{ field.relative }}</span>
{% else %}
{{ field.value }}
{% endif %}
</dd>
</div>
{% endfor %}
</dl>
"""

_APPROVAL = (
    """{% extends "layout.html" %}
{% block body %}
<p class="back"><a href="/">Approvals</a></p>
<h1>Approval</h1>
<section class="panel">
"""
    + _FIELDS
    + """
</section>
<section class="panel block">
<h2>Parameters</h2>
<p class="hint">Redacted the same way as the command line.</p>
{% if preview %}
<pre class="code">{{ preview }}</pre>
{% else %}
<p class="hint">No parameters were recorded.</p>
{% endif %}
</section>
{% if confirm_kind == "reject" %}
<section class="panel confirm">
<h2>Reject this approval?</h2>
<p>The execution ends rejected.</p>
<p class="quoted">{{ confirm_reason }}</p>
<form method="post" action="/approvals/{{ approval_id }}/reject">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="reason" value="{{ confirm_reason }}">
<input type="hidden" name="confirm" value="yes">
<div class="row-actions">
<button class="danger" type="submit">Reject approval</button>
<a class="quiet button" href="/approvals/{{ approval_id }}">Cancel</a>
</div>
</form>
</section>
{% elif pending %}
<div class="actions">
<form class="panel" method="post" action="/approvals/{{ approval_id }}/approve">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Approve</h2>
<label class="field">Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off">
</label>
<label class="check"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Approve</button>
</form>
<form class="panel" method="post" action="/approvals/{{ approval_id }}/reject">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Reject</h2>
<label class="field">Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<button class="danger" type="submit">Reject</button>
</form>
</div>
{% else %}
<p class="lede">This approval is no longer waiting.</p>
{% endif %}
{% endblock %}
"""
)

_RECONCILE = (
    """{% extends "layout.html" %}
{% block body %}
<p class="back"><a href="/reconcile">Reconciliation</a></p>
<h1>Uncertain call</h1>
<section class="panel">
"""
    + _FIELDS
    + """
</section>
{% if confirm_kind == "abandon" %}
<section class="panel confirm">
<h2>Abandon this call?</h2>
<p>The execution stays failed. Nothing is retried.</p>
<p class="quoted">{{ confirm_reason }}</p>
<form method="post" action="{{ action }}/abandon">
<input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="reason" value="{{ confirm_reason }}">
<input type="hidden" name="confirm" value="yes">
<div class="row-actions">
<button class="danger" type="submit">Abandon call</button>
<a class="quiet button" href="{{ action }}">Cancel</a>
</div>
</form>
</section>
{% elif open_call %}
<div class="stack">
<form class="panel block" method="post" action="{{ action }}/applied">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Applied</h2>
<p class="hint">The effect happened. The result is stored with the decision.</p>
<label class="field">Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<label class="field">Result JSON
<textarea name="result" autocomplete="off" required></textarea>
</label>
<label class="check"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Mark applied</button>
</form>
<form class="panel block" method="post" action="{{ action }}/not-applied">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Not applied</h2>
<p class="hint">Nothing was written. Resume can retry the same operation.</p>
<label class="field">Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<label class="check"><input type="checkbox" name="resume" value="on">
Resume as the host actor</label>
<button type="submit">Mark not applied</button>
</form>
<form class="panel block" method="post" action="{{ action }}/abandon">
<input type="hidden" name="csrf" value="{{ csrf }}">
<h2>Abandon</h2>
<p class="hint">The execution stays failed.</p>
<label class="field">Reason
<input type="text" name="reason" maxlength="2000" autocomplete="off" required>
</label>
<button class="danger" type="submit">Abandon</button>
</form>
</div>
{% else %}
<p class="lede">This call is no longer uncertain.</p>
{% endif %}
{% endblock %}
"""
)

_MESSAGE = """{% extends "layout.html" %}
{% block body %}
<section class="panel block">
<h1>{{ heading }}</h1>
<p>{{ message }}</p>
{% if show_login %}
<p><a href="/login">Sign in</a></p>
{% endif %}
</section>
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
    """Render one page. Missing shell values stay empty so error pages still render."""
    context.setdefault("factory", "")
    context.setdefault("section", "")
    context.setdefault("approval_count", 0)
    context.setdefault("call_count", 0)
    context.setdefault("actor_id", None)
    context.setdefault("csrf", "")
    context.setdefault("flash", None)
    context.setdefault("flash_error", False)
    return _ENV.get_template(template).render(**context)
