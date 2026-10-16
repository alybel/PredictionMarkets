"""Common preset values offered for the minimum-liquidity input fields."""
import re

from fastapi.testclient import TestClient

from app.api import app


def _html() -> str:
    return TestClient(app).get("/").text


def test_preset_list_offers_common_values():
    html = _html()
    block = re.search(r'<datalist id="liq-presets">(.*?)</datalist>', html, re.S)
    assert block, "datalist liq-presets missing"
    values = [int(v) for v in re.findall(r'<option value="(\d+)"', block.group(1))]
    assert values == sorted(values) and values[0] == 0 and len(values) >= 5
    assert {10_000, 100_000, 1_000_000} <= set(values)


def test_both_liquidity_inputs_use_presets_and_stay_free_input():
    html = _html()
    for field in ("mi-minliq", "metrics-minliq"):
        tag = re.search(rf'<input id="{field}"[^>]*>', html).group(0)
        assert 'list="liq-presets"' in tag and 'type="number"' in tag and 'value="0"' in tag
    assert html.count('id="liq-presets"') == 1
