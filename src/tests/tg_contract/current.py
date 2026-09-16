"""Load reviewed current overlays without changing archived traces or comparison."""
from pathlib import Path

from .harness import load_jsonl


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
    return [by_id.get(case['caseId'], case) for case in archived]
