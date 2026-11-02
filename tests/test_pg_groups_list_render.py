"""The PasarGuard groups list shows a group's inbound tags as readable text."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from app.services.formatting import format_number


@pytest.fixture
def template_env() -> Environment:
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
        ]),
        autoescape=select_autoescape(),
    )
    env.filters["num"] = format_number
    return env


def render_groups(env: Environment, groups: list[dict]) -> str:
    return env.get_template("pg_groups.html").render(
        groups=groups, inbound_tags=[], can_create=False, can_update=True, can_delete=True,
    )


def test_inbound_tags_are_joined_not_printed_as_a_python_list(template_env: Environment) -> None:
    html = render_groups(template_env, [{"id": 1, "name": "main", "inbound_tags": ["vless-ws", "trojan-grpc"]}])
    assert "vless-ws · trojan-grpc" in html
    assert "[&#39;vless-ws&#39;" not in html


def test_a_group_without_tags_shows_a_dash_and_hostile_tags_stay_escaped(template_env: Environment) -> None:
    html = render_groups(template_env, [
        {"id": 1, "name": "empty", "inbound_tags": []},
        {"id": 2, "name": "bad", "inbound_tags": ["<script>x</script>"]},
    ])
    assert '<bdi dir="ltr">—</bdi>' in html
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
