"""List fields render editable controls without changing names or stored values."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from app.bot.nav_inline import parse_topup_presets
from app.services.setup_wizard import parse_admin_ids
from app.services.users import SETTING_GROUPS


class RenderedControls(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []
        self.tags: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "input":
            self.inputs.append(dict(attrs))


@pytest.fixture
def template_env() -> Environment:
    return Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}{% block auth %}{% endblock %}"}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
        ]),
        autoescape=select_autoescape(),
    )


def test_wallet_presets_render_an_editable_money_list(template_env: Environment) -> None:
    field = next(field for fields in SETTING_GROUPS.values() for field in fields if field[0] == "wallet_topup_presets")
    stored = "50000,100000,200000,500000"
    html = template_env.get_template("_settings_field.html").render(field=field, values={field[0]: stored})
    control = RenderedControls(html).inputs[0]
    assert control["data-list-editor"] == "money"
    assert control["name"] == "s_wallet_topup_presets"
    assert control["value"] == stored
    assert parse_topup_presets({field[0]: control["value"]}) == [50000, 100000, 200000, 500000]


@pytest.mark.parametrize(("name", "required"), [("ADMIN_IDS", True), ("bot_admin_ids", False)])
def test_admin_lists_keep_their_existing_form_contract(template_env: Environment, name: str, required: bool) -> None:
    macro = template_env.get_template("_list_input.html").module.list_input
    stored = "123456789,9223372036854775807"
    control = RenderedControls(str(macro(name, "آیدی ادمین‌ها", stored, kind="telegram", required=required))).inputs[0]
    assert control["name"] == name
    assert control["value"] == stored
    assert control["data-list-editor"] == "telegram"
    assert control["data-normalize-digits"] == "0"
    assert ("required" in control) == required
    assert parse_admin_ids(control["value"] or "") == [123456789, 9223372036854775807]


def test_setup_admin_ids_render_an_editable_required_list(template_env: Environment) -> None:
    html = template_env.get_template("setup.html").render(values={"ADMIN_IDS": "123,456"}, show_done=False)
    control = next(item for item in RenderedControls(html).inputs if item.get("name") == "admin_ids")
    assert control["data-list-editor"] == "telegram"
    assert control["value"] == "123,456"
    assert "required" in control


def test_bot_settings_admin_ids_render_an_editable_required_list(template_env: Environment) -> None:
    html = template_env.get_template("_settings_bot.html").render(
        request=SimpleNamespace(query_params={}), env_values={"ADMIN_IDS": "123,456"},
        bot_status=None, bot_update_mode="polling",
    )
    control = next(item for item in RenderedControls(html).inputs if item.get("name") == "ADMIN_IDS")
    assert control["data-list-editor"] == "telegram"
    assert control["value"] == "123,456"
    assert "required" in control


def test_host_create_and_edit_share_the_same_address_list_control(template_env: Environment) -> None:
    html = template_env.get_template("pg_hosts.html").render(
        hosts=[], inbound_tags=["vless"], can_create=True, can_update=True, can_delete=False,
    )
    controls = [item for item in RenderedControls(html).inputs if item.get("name") == "address"]
    assert len(controls) == 2
    assert {item["id"] for item in controls} == {"pg-host-create-address", "pg-host-edit-address"}
    assert all(item["data-list-editor"] == "text" and "required" in item for item in controls)


def test_hostile_list_values_and_labels_remain_escaped(template_env: Environment) -> None:
    macro = template_env.get_template("_list_input.html").module.list_input
    hostile = '<img src=x onerror="alert(1)">'
    rendered = RenderedControls(str(macro("address", hostile, hostile, help=hostile, placeholder=hostile)))
    assert "img" not in rendered.tags
    assert rendered.inputs[0]["value"] == hostile
    assert rendered.inputs[0]["aria-label"] == hostile
    assert rendered.inputs[0]["placeholder"] == hostile


def test_single_value_settings_keep_their_ordinary_input(template_env: Environment) -> None:
    html = template_env.get_template("_settings_field.html").render(
        field=("btn_wallet", "کیف پول", "text"), values={"btn_wallet": "کیف پول"},
    )
    control = RenderedControls(html).inputs[0]
    assert "data-list-editor" not in control
    assert control["value"] == "کیف پول"
