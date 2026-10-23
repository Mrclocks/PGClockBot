"""Money remains numeric in forms and uses comma grouping in web displays."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, Undefined, select_autoescape

from app.services.formatting import format_gb, format_money, format_number, format_toman
from app.services.panel_assets import get_panel_asset_cache_key


class RenderedHtml(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.inputs: list[dict[str, str | None]] = []
        self.scripts: list[str] = []
        self.text: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input":
            self.inputs.append(dict(attrs))
        if tag == "script" and dict(attrs).get("src"):
            self.scripts.append(str(dict(attrs)["src"]))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


@pytest.fixture
def template_env() -> Environment:
    env = Environment(
        loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
        ]),
        autoescape=select_autoescape(),
    )
    env.filters.update(money=format_money, num=format_number, gb=format_gb)
    return env


@pytest.mark.parametrize(("amount", "expected"), [
    (0, "0"), (999, "999"), (1000, "1,000"), (150000, "150,000"),
    (1234567890, "1,234,567,890"), (-1250000, "-1,250,000"),
    (9223372036854775807, "9,223,372,036,854,775,807"),
    (None, "—"), (Undefined(), "—"), ("<img src=x>", "—"),
])
def test_web_amounts_use_comma_groups(amount: object, expected: str) -> None:
    assert format_money(amount) == expected


def test_existing_bot_money_format_is_preserved() -> None:
    assert format_toman(150000) == "\u200f150٬000 تومان"


def test_refund_display_and_retry_form_preserve_the_same_amount(template_env: Environment) -> None:
    row = SimpleNamespace(
        id=1, status="approved", refund_amount=1250000,
        notification_status="sent", reason="<img src=x>", operator_note="",
    )
    context = {"cancel_requests": [(row, "customer")], "cancel_labels": {"approved": "تأیید", "review": "بررسی"},
               "can_approve_cancel": True, "csrf_token": "test-csrf"}
    template = template_env.get_template("_finance_cancellations.html")
    html = template.render(context)
    assert "1,250,000" in "".join(RenderedHtml(html).text)
    assert "&lt;img" in html
    assert "<img" not in html

    row.status = "review"
    form = RenderedHtml(template.render(context))
    amount = next(field for field in form.inputs if field.get("name") == "amount")
    assert amount["value"] == "1250000"
    assert "readonly" in amount
    assert "data-money" in amount
    assert amount["max"] == "2147483647"


def test_plan_prices_use_grouping_while_price_inputs_keep_numeric_defaults(template_env: Environment) -> None:
    plan = SimpleNamespace(id=1, name="<svg>", description="", price=1250000, is_active=True)
    html = template_env.get_template("reseller_plans.html").render(plans=[plan])
    rendered = RenderedHtml(html)
    assert "1,250,000" in "".join(rendered.text)
    assert "&lt;svg&gt;" in html
    price = next(field for field in rendered.inputs if field.get("name") == "price")
    assert price["value"] == "0"
    assert "data-money" in price

    gift_fields = template_env.get_template("_gift_code_fields.html").module.gift_code_fields()
    gift_amount = next(field for field in RenderedHtml(str(gift_fields)).inputs if field.get("name") == "amount")
    assert gift_amount["placeholder"] == "50,000"
    assert gift_amount["value"] == ""


def test_financial_report_groups_revenue_and_payment_totals(template_env: Environment) -> None:
    report = {"previous": {"revenue": 1234567}, "change_percent": None, "ops": {}, "timeline": [],
              "active": {"revenue": 2345678, "avg_order": 125000},
              "payment_methods": [{"label": "کیف پول", "amount": 3456789, "count": 1}]}
    html = template_env.get_template("_finance_reports.html").render(report=report)
    text = "".join(RenderedHtml(html).text)
    for amount in ("1,234,567", "2,345,678", "125,000", "3,456,789"):
        assert amount in text


@pytest.mark.parametrize("key", ["referral_bonus", "stars_toman_per_star", "billing_low_balance", "billing_price_per_gb"])
def test_money_settings_enable_grouped_input_without_changing_value(template_env: Environment, key: str) -> None:
    html = template_env.get_template("_settings_field.html").render(
        field=(key, "مبلغ", "number"), values={key: "1250000"},
    )
    field = next(item for item in RenderedHtml(html).inputs if item.get("name") == f"s_{key}")
    assert "data-money" in field
    assert field["value"] == "1250000"


def test_quota_settings_do_not_enable_money_grouping(template_env: Environment) -> None:
    html = template_env.get_template("_settings_field.html").render(
        field=("user_alert_low_traffic_pct", "آستانه حجم", "number"),
        values={"user_alert_low_traffic_pct": "20"},
    )
    field = RenderedHtml(html).inputs[0]
    assert "data-money" not in field
    assert field["min"] == "1"
    assert field["max"] == "99"


def test_catalog_reductions_group_amounts_and_keep_percentages_plain(template_env: Environment) -> None:
    def discount(kind: str, amount: int) -> SimpleNamespace:
        return SimpleNamespace(id=1, title="تخفیف", scope="all", mode="permanent", campaign_id=None,
                               reduction_kind=kind, reduction_value=amount, ends_at=None, is_active=False)

    html = template_env.get_template("catalog_discounts.html").render(
        request=SimpleNamespace(query_params={}), scopes={}, modes={}, announcement_status={},
        discounts=[discount("amount", 1250000), discount("percent", 20)],
    )
    rendered = RenderedHtml(html)
    assert "1,250,000" in "".join(rendered.text)
    control = next(item for item in rendered.inputs if item.get("name") == "reduction_value")
    assert control["data-money-kind"] == "reduction_kind"
    assert control["data-money-value"] == "amount"
    assert "data-money" not in control
    assert control["max"] == "2000000000"


@pytest.mark.parametrize(("template", "prefix", "items_key"), [
    ("_loyalty_rewards_panel.html", "reward", "rewards"),
    ("_loyalty_wheel_panel.html", "prize", "wheel_prizes"),
])
@pytest.mark.parametrize("kind", ["wallet_credit", "discount_percent", "traffic_gb"])
def test_loyalty_values_only_group_wallet_credit(
    template_env: Environment, template: str, prefix: str, items_key: str, kind: str,
) -> None:
    item = SimpleNamespace(id=1, archived=False, enabled=True, name="جایزه", label="جایزه", description="",
                           reward_type=kind, prize_type=kind, reward_value=1250000, prize_value=1250000,
                           points_cost=10, redemption_count=0, win_count=0, weight=10, sort_order=1,
                           max_redemptions_global=None, max_redemptions_per_user=None,
                           max_wins_global=None, max_wins_per_user=None, min_purchase_toman=0,
                           max_discount_toman=None, expires_days=None)
    html = template_env.get_template(template).render(
        **{items_key: [item]}, reward_type_labels={}, wheel_prize_type_labels={},
    )
    controls = [field for field in RenderedHtml(html).inputs if field.get("name") == f"{prefix}_value"]
    create, edit = controls
    assert create["data-money-kind"] == f"{prefix}_type"
    assert create["data-money-value"] == "wallet_credit"
    assert ("data-money" in edit) == (kind == "wallet_credit")
    assert edit["value"] == "1250000"


def test_grouping_scripts_render_urls_for_their_current_content() -> None:
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
        autoescape=select_autoescape(),
    )
    env.globals["template_variable_specs"] = lambda actor: {}
    context = {"staff": {"role": "reseller", "permissions": []}, "app_version": "unchanged-release",
               "request": SimpleNamespace(url=SimpleNamespace(path="/home"), query_params={}),
               "money_inputs_cache_key": get_panel_asset_cache_key("money-inputs.js"),
               "list_inputs_cache_key": get_panel_asset_cache_key("list-inputs.js")}
    rendered = RenderedHtml(env.get_template("base.html").render(context))
    for name in ("money-inputs.js", "list-inputs.js"):
        assert f"/static/{name}?v={get_panel_asset_cache_key(name)}" in rendered.scripts
