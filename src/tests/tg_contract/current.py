"""Load reviewed current overlays without changing archived traces or comparison."""
from pathlib import Path

from .harness import load_jsonl


# Reviewed additions are not overrides of historical case IDs. Keep an exact
# registry so missing/unowned additions fail just like unknown overlays.
OAUTH_ADDITION_CASE_IDS = (
    'TG-OA-03.claude_reset_cedar_success_duplicate',
    'TG-OA-03.claude_reset_juniper_unconfirmed_duplicate',
    'TG-OA-03.claude_reset_stage_and_expiry_guard',
)


def load_current_additions(segment: str | Path, existing: list[dict]) -> list[dict]:
    segment = Path(segment)
    if segment.name != 'oauth.jsonl':
        return []
    path = segment.parents[2] / 'claude-reset-2026-09-23/additions/oauth.jsonl'
    additions = load_jsonl(path)
    ids = [case['caseId'] for case in additions]
    if tuple(ids) != OAUTH_ADDITION_CASE_IDS:
        raise AssertionError('current OAuth addition case coverage differs')
    if set(ids) & {case['caseId'] for case in existing}:
        raise AssertionError('current OAuth addition replaces an existing case')
    if any(case['capabilityId'] != 'TG-OA-03' for case in additions):
        raise AssertionError('current OAuth addition changes capability ownership')
    return additions


def load_current_jsonl(segment: str | Path) -> list[dict]:
    segment = Path(segment)
    archived = load_jsonl(segment)
    overlay = segment.parents[2] / 'model-center-2026-09-13' / 'overrides' / segment.name
    overrides = load_jsonl(overlay) if overlay.exists() else []
    by_id = {case['caseId']: case for case in overrides}
    archived_ids = {case['caseId'] for case in archived}
    if len(by_id) != len(overrides):
        raise AssertionError('duplicate current TG override caseId')
    if set(by_id) - archived_ids:
        raise AssertionError(f'unknown current TG overrides: {sorted(set(by_id) - archived_ids)}')
    for case in overrides:
        original = next(row for row in archived if row['caseId'] == case['caseId'])
        if case['capabilityId'] != original['capabilityId']:
            raise AssertionError('current overlay changes capability identity')
    result = [by_id.get(case['caseId'], case) for case in archived]
    return result + load_current_additions(segment, result)
