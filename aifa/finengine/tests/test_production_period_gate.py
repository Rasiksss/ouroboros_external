from types import SimpleNamespace

from finengine.skills.production_runtime import _select_published


def test_period_gate_uses_each_metrics_own_time_axis():
    values = [
        SimpleNamespace(concept="TOTAL_ASSETS", period_ref="30.06.2026"),
        SimpleNamespace(concept="SALES_REVENUE", period_ref="6M 2026"),
    ]

    published, found = _select_published(values, "30.06.2026")

    assert published == values
    assert found == ["30.06.2026", "6M 2026"]


def test_period_gate_honours_ad_hoc_stock_semantics():
    # Stage 2 can inherit the report-wide duration label even when it read a
    # balance-sheet row.  The ad-hoc kind is the authoritative time axis.
    value = SimpleNamespace(concept="TRADEMARK_CARRYING_VALUE", period_ref="6M 2026")

    published, _found = _select_published(
        [value], "30.06.2026", value_kinds={"TRADEMARK_CARRYING_VALUE": "stock"},
    )

    assert published == [value]


def test_period_gate_keeps_container_output_beside_matching_scalars():
    form = SimpleNamespace(output_type="FormOutput")
    metric = SimpleNamespace(concept="TOTAL_ASSETS", period_ref="30.06.2026")

    published, found = _select_published([form, metric], "30.06.2026")

    assert published == [form, metric]
    assert found == ["30.06.2026"]
