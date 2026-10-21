"""Unified model registry: naming, grouping, aliases and per-source upstream choice."""
from src.model_registry import names, pipeline, resolver
from src.model_registry.offers import Offer


CATALOG = pipeline.CatalogIndex([
    {"key": "zhipuai/glm-5.3", "id": "glm-5.3", "name": "GLM-5.3"},
    {"key": "moonshotai/kimi-k3", "id": "kimi-k3", "name": "Kimi K3"},
    {"key": "openai/gpt-5.3-codex", "id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
    {"key": "google/gemini-2.5-flash", "id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
    {"key": "google/gemini-3.5-flash-lite", "id": "gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite"},
    {"key": "tencent/hy3", "id": "hy3", "name": "Hy3"},
    {"key": "deepseek/deepseek-flash", "id": "deepseek-flash", "name": "DeepSeek V4.1 Flash 1017"},
])


def offer(source, wire, name=None, basis="id", **kwargs):
    provider = kwargs.pop("provider", source.split(":")[1] if source.startswith("oauth:") else "api")
    return Offer(source=source, provider=provider, wire=wire, name=name or wire, basis=basis, **kwargs)


def build(*offers, rules=()):
    return pipeline.build(offers, rules=rules, catalog=CATALOG)


def test_names_fold_spelling_and_split_traits():
    assert names.fold("GLM 5p3_Flash") == "glm-5.3-flash"
    assert names.compare_key("claude-opus-4-6") == names.compare_key("Claude Opus 4.6")
    assert names.display_traits("Gemini 3.8 Flash (High)") == ("gemini-3.8-flash", {"effort": "high"})
    assert names.display_traits("Claude Opus 4.6 (Thinking)") == ("claude-opus-4.6", {"thinking": "on"})
    assert names.display_traits("Gemini 3.1 Pro (Preview)") == ("gemini-3.1-pro-preview", {})
    assert names.suffix_traits("gemini-4-argon-disabled") == ("gemini-4-argon", {"thinking": "off"})
    assert names.suffix_traits("claude-opus-4-7-thinking-high-fast") == (
        "claude-opus-4-7", {"speed": "fast", "effort": "high", "thinking": "on"})
    assert names.nearest_effort("high", ["low", "medium"]) == "medium"
    assert names.nearest_effort("high", ["xhigh", "max"]) == "xhigh"
    assert names.nearest_effort("high", []) is None


def test_same_model_from_every_source_gets_one_lower_case_name():
    reg = build(
        offer("oauth:zhipu:a", "glm-5.3", "GLM-5.3", legacy="glm-5.3"),
        offer("oauth:cursor:a", "glm-5p3", "GLM 5.3"),
        offer("oauth:qoder:a", "gmodel", "GLM-5.3", basis="name", legacy="qoder-glm-5.3"),
        offer("oauth:workbuddy:a", "glm-5.3", "GLM-5.3", basis="both"),
        offer("api:MiniMax", "MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash", basis="name", legacy="MiniMax-M3.1-Flash"),
        offer("oauth:workbuddy:a", "minimax-m3.1-flash", "MiniMax-M3.1-Flash", basis="both", legacy="minimax-m3.1-flash"),
    )
    assert sorted(reg.models) == ["glm-5.3", "minimax-m3.1-flash"]
    assert {o.wire for o in reg.models["glm-5.3"].offers} == {"glm-5.3", "glm-5p3", "gmodel"}
    assert reg.models["glm-5.3"].name == "GLM-5.3"
    for alias in ("GLM-5.3", "glm-5p3", "qoder-glm-5.3", "MiniMax-M3.1-Flash"):
        assert resolver.resolve(reg, alias) is not None, alias
    # A name-based source's own code is not a public name.
    assert resolver.resolve(reg, "gmodel") is None


def test_products_are_namespaced_and_never_merge_across_providers():
    reg = build(
        offer("oauth:qoder:a", "fast", "Fast", basis="name", product=True),
        offer("oauth:workbuddy:a", "fast-model", "Fast", basis="both", product=True, legacy="fast-model"),
        offer("oauth:cursor:a", "composer-2.5", "Composer 2.5", product=True, legacy="composer-2.5"),
        offer("oauth:qoder:a", "smodel", "Sonus", basis="name", product=True),
    )
    assert sorted(reg.models) == ["cursor-composer-2.5", "qoder-fast", "qoder-sonus", "workbuddy-fast"]
    assert reg.models["qoder-sonus"].name == "Sonus"
    assert resolver.resolve(reg, "composer-2.5").model == "cursor-composer-2.5"
    assert resolver.resolve(reg, "fast") is None


def test_identity_basis_decides_between_id_and_display_name():
    reg = build(
        offer("oauth:cursor:a", "gpt-5.3-codex", "Codex 5.3"),
        offer("oauth:workbuddy:a", "kimi-k3-1", "Kimi-K3", basis="both", legacy="kimi-k3-1"),
        offer("api:Kimi", "kimi-k3", "kimi-k3", basis="name", legacy="kimi-k3"),
        offer("oauth:antigravity:a", "gemini-2.5-flash", "Gemini 3.5 Flash Lite", basis="name", legacy="gemini-2.5-flash"),
        offer("oauth:claude:a", "claude-sonnet-4-5-20250929", "Claude Sonnet 4.5"),
    )
    assert "gpt-5.3-codex" in reg.models
    assert {o.wire for o in reg.models["kimi-k3"].offers} == {"kimi-k3-1", "kimi-k3"}
    pending = reg.models["gemini-2.5-flash"].pending
    assert len(pending) == 1 and "gemini-3.5-flash-lite" in pending[0].reason
    assert "claude-sonnet-4-5-20250929" in reg.models


def test_two_ids_of_one_source_with_one_name_stay_pending():
    reg = build(
        offer("oauth:workbuddy:a", "hy3", "Hy3", basis="both", legacy="hy3"),
        offer("oauth:workbuddy:a", "hy3-x", "Hy3", basis="both", legacy="hy3-x"),
        offer("oauth:workbuddy:b", "hy3", "Hy3", basis="both", legacy="hy3"),
    )
    assert [o.wire for o in reg.models["hy3"].offers] == ["hy3", "hy3"]
    assert reg.models["hy3-x"].pending and resolver.resolve(reg, "hy3-x").model == "hy3-x"


def test_manual_merge_keeps_the_floating_name_as_an_alias_and_suggestions_stay_hints():
    offers = (
        offer("api:DeepSeek", "deepseek-flash", "deepseek-flash", basis="name", legacy="deepseek-flash"),
        offer("oauth:workbuddy:a", "deepseek-v4.1-flash", "Deepseek-V4.1-Flash", basis="both", legacy="deepseek-v4.1-flash"),
        offer("oauth:claude:a", "claude-opus-4-5-20251101", "claude-opus-4-5-20251101"),
        offer("oauth:cursor:a", "claude-opus-4-5", "Claude Opus 4.5"),
    )
    hinted = build(*offers)
    assert "deepseek-flash" in hinted.models
    assert any(s.model == "deepseek-flash" and s.into == "deepseek-v4.1-flash" for s in hinted.suggestions)
    assert any(s.text == "`claude-opus-4-5` 可能是 `claude-opus-4-5-20251101` 的浮动名" for s in hinted.suggestions)
    merged = build(*offers, rules=[{"type": "merge", "from": "deepseek-flash", "into": "deepseek-v4.1-flash"}])
    assert "deepseek-flash" not in merged.models
    assert {o.source for o in merged.models["deepseek-v4.1-flash"].offers} == {"api:DeepSeek", "oauth:workbuddy:a"}
    assert resolver.resolve(merged, "deepseek-flash").model == "deepseek-v4.1-flash"


ARGON = (
    offer("oauth:cursor:a", "gemini-4-argon", "Gemini 4 Argon", efforts=("low", "medium", "high")),
    offer("oauth:antigravity:a", "gemini-4-argon-low", "Gemini 4 Argon (Low)", basis="name", traits={"effort": "low"}),
    offer("oauth:antigravity:a", "gemini-4-argon-high", "Gemini 4 Argon (High)", basis="name", traits={"effort": "high"}),
    offer("oauth:antigravity:b", "gemini-4-argon-disabled", "Gemini 4 Argon", basis="name", traits={"thinking": "off"},
          legacy="gemini-4-argon-disabled"),
    offer("oauth:zhipu:a", "glm-5.3", "GLM-5.3", efforts=("low", "high", "max"), thinking="toggle"),
    offer("api:Plain", "plain-1", "plain-1", basis="name"),
    offer("oauth:workbuddy:a", "lite-1", "Lite-1", basis="both", efforts=("low", "medium")),
)


def picks(reg, name, **kwargs):
    found = resolver.resolve(reg, name)
    return {(c.offer.source, c.offer.wire, c.effort, c.thinking)
            for c in resolver.choose(reg, found, lambda o: True, **kwargs)}


def test_requests_without_effort_default_to_high_fitted_to_each_source():
    reg = build(*ARGON)
    assert picks(reg, "gemini-4-argon") == {
        ("oauth:cursor:a", "gemini-4-argon", "high", None),
        ("oauth:antigravity:a", "gemini-4-argon-high", "high", None),
    }
    assert picks(reg, "lite-1") == {("oauth:workbuddy:a", "lite-1", "medium", None)}
    assert picks(reg, "plain-1") == {("api:Plain", "plain-1", None, None)}
    assert picks(reg, "glm-5.3") == {("oauth:zhipu:a", "glm-5.3", "high", None)}


def test_shortcut_and_parameter_select_the_same_variant_and_explicit_misses_skip_the_source():
    reg = build(*ARGON)
    assert resolver.resolve(reg, "gemini-4-argon-low").via == "shortcut"
    assert picks(reg, "gemini-4-argon-low") == picks(reg, "gemini-4-argon", effort="low") == {
        ("oauth:cursor:a", "gemini-4-argon", "low", None),
        ("oauth:antigravity:a", "gemini-4-argon-low", "low", None),
    }
    assert picks(reg, "gemini-4-argon", effort="medium") == {("oauth:cursor:a", "gemini-4-argon", "medium", None)}
    assert resolver.resolve(reg, "GLM-5.3-max").traits == {"effort": "max"}


def test_thinking_off_only_reaches_ids_that_can_stop_thinking():
    reg = build(*ARGON)
    assert resolver.resolve(reg, "gemini-4-argon-disabled").traits == {"thinking": "off"}
    assert picks(reg, "gemini-4-argon-disabled") == {
        ("oauth:cursor:a", "gemini-4-argon", None, "off"),
        ("oauth:antigravity:b", "gemini-4-argon-disabled", None, "off"),
    }


def test_exact_names_win_over_shortcut_reading():
    reg = build(offer("oauth:qoder:a", "qmodel_38max", "Qwen3.8-Max", basis="name"),
                offer("oauth:qoder:a", "qfmodel", "Qwen3.8-Flash", basis="name"))
    assert resolver.resolve(reg, "qwen3.8-max").via == "id"
    assert resolver.resolve(reg, "Qwen3.8-Max").model == "qwen3.8-max"
