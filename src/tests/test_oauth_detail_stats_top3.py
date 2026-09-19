"""Only the OAuth detail model breakdown is shortened, never its totals/data."""
import copy

import pytest

from src.telegram.menus import oauth_menu as menu


@pytest.mark.parametrize("provider", ["cursor", "workbuddy"])
@pytest.mark.parametrize("count", [0, 2, 5])
def test_oauth_detail_keeps_top_three_and_full_totals(monkeypatch, provider, count):
    account_key = f"{provider}:fixture"
    metrics = dict(total=150, success_count=140, error_count=10,
                   input=300000, output=80000, cache_creation=0, cache_read=900000,
                   avg_tps=58.2, max_tps=86.4, min_tps=31.7,
                   costed_success=140, unpriced_success=0, cost_ticks=1240000000)
    # Existing query sorts by request count descending, not alphabetically.
    names = ["z-model", "a-model", "y-model", "b-model", "x-model"][:count]
    rows = [dict(metrics, final_model=name, total=50-index*10) for index, name in enumerate(names)]
    original = copy.deepcopy(rows)
    monkeypatch.setattr(menu.oauth_control, "account_snapshot", lambda key: {"provider": provider})
    monkeypatch.setattr(menu.oauth_control, "quota_snapshot", lambda key: None)
    monkeypatch.setattr(menu.oauth_control, "provider_of_snapshot", lambda key: provider)
    monkeypatch.setattr(menu, "_oauth_local_period", lambda *a, **k: {
        "since": 1, "detail_title": "本地自然月", "stats_window": None,
    })
    monkeypatch.setattr(menu, "_account_period_stats", lambda *a, **k: metrics)
    monkeypatch.setattr(menu.model_names, "for_channel", lambda channel, name: name)
    text = menu._format_month_stats_block(account_key, month_snapshot={}, by_model=rows)
    assert "总体: 150 次 · ✅ 140 · ❌ 10" in text
    assert "峰值" in text and "缓存" in text and "累计金额" in text
    assert text.count("  • <code>") == min(count, 3)
    for name in names[:3]:
        assert f"<code>{name}</code>" in text
    for name in names[3:]:
        assert f"<code>{name}</code>" not in text
    if count:
        assert "按模型: Top 3" in text
        assert text.index(names[0]) < text.index(names[1])
    else:
        assert "按模型" not in text
    assert ("其余 2 个模型未展开" in text) == (count == 5)
    assert rows == original
