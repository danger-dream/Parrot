"""Bidirectional current-overlay coverage; archived hashes stay in manifest gate."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.tests.tg_contract import StrictMismatch, assert_strict_equal, load_jsonl
from src.tests.tg_contract import current
from src.tests import test_tg_contract_channels_support as channels
from src.tests import test_tg_contract_main_help as main
from src.tests import test_tg_contract_oauth_support as oauth

ROOT = Path(__file__).parent / 'fixtures/tg_contract'
# channels_apikey grew from 11 to 24 for the API-key MCP changes, then to 25
# when the reviewed cold-statistics channel entry was changed from a blocking
# prompt to an operable empty management page, then to 27 for edit/discovery traces.
COUNTS = {'channels_apikey': 27, 'main_status': 11, 'oauth': 53,
          'auxiliary': 18, 'model_routing': 6, 'system': 9, 'core': 22}


def test_current_overlay_case_coverage_is_bidirectional_and_unique():
    directory = ROOT / 'model-center-2026-09-13/overrides'
    assert {p.stem for p in directory.glob('*.jsonl')} == set(COUNTS)
    seen = set()
    for segment, count in COUNTS.items():
        archived_path = ROOT / f'v0.31.13/segments/{segment}.jsonl'
        archived = load_jsonl(archived_path)
        overlay = load_jsonl(directory / f'{segment}.jsonl')
        assert len(overlay) == count
        ids = [row['caseId'] for row in overlay]
        assert len(ids) == len(set(ids)) and not seen.intersection(ids)
        seen.update(ids)
        loaded = current.load_current_jsonl(archived_path)
        extra_ids = list(current.OAUTH_ADDITION_CASE_IDS) if segment == 'oauth' else []
        assert [c['caseId'] for c in loaded] == [c['caseId'] for c in archived] + extra_ids
        assert [c['capabilityId'] for c in loaded] == [c['capabilityId'] for c in archived] + ['TG-OA-03'] * len(extra_ids)
    assert len(seen) == 146  # includes MCP UI, cold-stat page, notification and discovery traces


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown', 'capability'])
def test_current_loader_rejects_invalid_overlay(monkeypatch, mutation):
    archived_path = ROOT / 'v0.31.13/segments/core.jsonl'
    original = load_jsonl(archived_path)
    row = deepcopy(original[0])
    overrides = [row, deepcopy(row)] if mutation == 'duplicate' else [row]
    if mutation == 'unknown':
        row['caseId'] = 'TG-CORE-01.unknown-current-case'
    if mutation == 'capability':
        row['capabilityId'] = 'TG-CORE-06'
    monkeypatch.setattr(current, 'load_jsonl', lambda path: original if Path(path) == archived_path else overrides)
    with pytest.raises(AssertionError, match=mutation):
        current.load_current_jsonl(archived_path)


@pytest.mark.parametrize('module', [channels, main, oauth], ids=['channels', 'main', 'oauth'])
@pytest.mark.parametrize('mutation', ['duplicate', 'unknown'])
def test_existing_overlay_loaders_still_reject_duplicate_unknown(module, mutation, monkeypatch, tmp_path):
    row = deepcopy(load_jsonl(module.SEGMENT)[0])
    overrides = [row, deepcopy(row)] if mutation == 'duplicate' else [row]
    if mutation == 'unknown':
        row['caseId'] += '.unknown-current-case'
    path = tmp_path / 'invalid.jsonl'
    path.write_text(''.join(json.dumps(value, ensure_ascii=False) + '\n' for value in overrides))
    monkeypatch.setattr(module, 'CURRENT_OVERRIDES', path)
    with pytest.raises(AssertionError, match=mutation):
        loader = channels.current_cases if module is channels else module._current_cases
        loader()


def test_current_additions_have_exact_ownership_runners_and_both_loaders_agree():
    from src.tests.tg_contract.claude_reset_runner import SCENARIOS
    additions = load_jsonl(ROOT / 'claude-reset-2026-09-23/additions/oauth.jsonl')
    assert tuple(case['caseId'] for case in additions) == current.OAUTH_ADDITION_CASE_IDS
    assert {case['capabilityId'] for case in additions} == {'TG-OA-03'}
    assert {case['entry']['scenario'] for case in additions} == set(SCENARIOS)
    assert all(case['caseId'] == 'TG-OA-03.' + case['entry']['scenario'] for case in additions)
    assert_strict_equal(oauth._current_cases(), current.load_current_jsonl(oauth.SEGMENT))
    for case in additions:
        expected = set(case['entry']['callbackFamilies'])
        actual = {':'.join(item['callback'].split(':')[:2]) + ':*'
                  for item in case['finalBusinessState']['callbacks']}
        assert expected == actual == {'oa:claude_reset_ask:*', 'oa:claude_reset_confirm:*', 'oa:claude_reset_execute:*'}


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'unknown', 'capability', 'replacement'])
def test_current_additions_reject_unreviewed_or_replacing_cases(monkeypatch, mutation):
    additions = load_jsonl(ROOT / 'claude-reset-2026-09-23/additions/oauth.jsonl')
    existing = []
    if mutation == 'duplicate':
        additions.append(deepcopy(additions[0]))
    elif mutation == 'missing':
        additions.pop()
    elif mutation == 'unknown':
        additions[0]['caseId'] += '.unknown'
    elif mutation == 'capability':
        additions[0]['capabilityId'] = 'TG-OA-07'
    elif mutation == 'replacement':
        existing = [deepcopy(additions[0])]
    monkeypatch.setattr(current, 'load_jsonl', lambda path: additions)
    with pytest.raises(AssertionError):
        current.load_current_additions(oauth.SEGMENT, existing)


def test_current_overlay_does_not_weaken_business_state_comparison():
    case = current.load_current_jsonl(ROOT / 'v0.31.13/segments/core.jsonl')[0]
    changed = deepcopy(case)
    changed['finalBusinessState']['unexpectedBusinessWrite'] = True
    with pytest.raises(StrictMismatch):
        assert_strict_equal(case, changed)
