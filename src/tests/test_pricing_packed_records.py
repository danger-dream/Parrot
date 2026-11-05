import copy

from src import model_pricing as mp


def _records():
    return {
        "openai/gpt-x": {"id": "gpt-x", "name": "GPT X", "cost": {"input": 1e-05, "output": 2.5}, "limit": {"context": 400000}},
        "openai/gpt-y": {"id": "gpt-y", "cost": {"input": 0.1}},
        "acme/中文模型": {"id": "中文模型", "name": "中文 🦜", "modalities": {"input": ["text", "image"]}},
    }


def test_packed_records_behave_like_the_dict_they_replace():
    plain = _records()
    packed = mp._PackedRecords()
    for key, record in plain.items():
        packed[key] = record
    assert dict(packed.items()) == plain
    assert len(packed) == 3 and "openai/gpt-x" in packed and "missing" not in packed
    assert packed.get("missing") is None
    first = packed["openai/gpt-x"]
    first["cost"]["input"] = 99
    assert packed["openai/gpt-x"]["cost"]["input"] == 1e-05
    assert [mp._record_name(packed, key) for key in packed] == [mp._record_name(plain, key) for key in plain]

    copied = copy.deepcopy(packed)
    del packed["openai/gpt-y"]
    assert "openai/gpt-y" in copied and "openai/gpt-y" not in packed
    assert mp._record_name(copied, "openai/gpt-y") is None


def test_catalog_listings_match_plain_records(monkeypatch):
    plain = _records()
    packed = mp._PackedRecords()
    for key, record in plain.items():
        packed[key] = record
    monkeypatch.setattr(mp, "_ensure_catalog", lambda: True)
    monkeypatch.setattr(mp, "_catalog_provider_names", {"openai": "OpenAI", "acme": "Acme"})
    results = []
    for records in (plain, packed):
        monkeypatch.setattr(mp, "_catalog_models", records)
        results.append((mp.catalog_models(), mp.catalog_provider_models("openai"), mp.catalog_model("acme/中文模型")))
    assert results[0] == results[1]
