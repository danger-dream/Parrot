"""Load reviewed current overlays without changing archived traces or comparison."""
from pathlib import Path

from .harness import load_jsonl


# Reviewed additions are not overrides of historical case IDs. Keep an exact
# registry so missing/unowned additions fail just like unknown overlays.
CLAUDE_RESET_ADDITION_CASE_IDS = (
    'TG-OA-03.claude_reset_cedar_success_duplicate',
    'TG-OA-03.claude_reset_juniper_unconfirmed_duplicate',
    'TG-OA-03.claude_reset_stage_and_expiry_guard',
)
RELOGIN_ADDITION_CASE_IDS = ('TG-OA-04.relogin_start_cancel',)
OAUTH_ADDITION_CASE_IDS = CLAUDE_RESET_ADDITION_CASE_IDS + RELOGIN_ADDITION_CASE_IDS
# logBodyRetention (2026-10-21): the retention page gained the body-retention
# section and the plan preview no longer treats compaction space as a gate.
BODY_RETENTION_OVERRIDE_CASE_IDS = (
    'TG-SYS-05.001-menu-toggle-busy',
    'TG-SYS-05.002-menu-toggle-busy',
    'TG-SYS-05.006-first-cancel',
    'TG-SYS-05.007-input-cancel',
    'TG-SYS-05.010-plan-paging-cancel',
    'TG-SYS-05.011-commit',
    'TG-SYS-05.012-commit',
    'TG-SYS-05.013-commit',
    'TG-SYS-05.018-plan-empty-cancel',
    'TG-SYS-05.019-plan-preflight-failure',
)
SYSTEM_ADDITION_CASE_IDS = ('TG-SYS-05.body_retention',)


def _load_system_additions(segment: Path, existing: list[dict]) -> list[dict]:
    additions = load_jsonl(segment.parents[2] / 'log-body-retention-2026-10-21/additions/system.jsonl')
    ids = tuple(case['caseId'] for case in additions)
    if ids != SYSTEM_ADDITION_CASE_IDS:
        raise AssertionError('current system addition case coverage differs')
    if set(ids) & {case['caseId'] for case in existing}:
        raise AssertionError('current system addition replaces an existing case')
    if any(case['capabilityId'] != case['caseId'].split('.', 1)[0] for case in additions):
        raise AssertionError('current system addition changes capability ownership')
    return additions


def load_current_additions(segment: str | Path, existing: list[dict]) -> list[dict]:
    segment = Path(segment)
    if segment.name == 'system.jsonl':
        return _load_system_additions(segment, existing)
    if segment.name != 'oauth.jsonl':
        return []
    path = segment.parents[2] / 'claude-reset-2026-09-23/additions/oauth.jsonl'
    reset = load_jsonl(path)
    relogin = load_jsonl(segment.parents[2] / 'oauth-relogin-2026-10-18/additions/oauth.jsonl')
    if (tuple(case['caseId'] for case in reset) != CLAUDE_RESET_ADDITION_CASE_IDS
            or tuple(case['caseId'] for case in relogin) != RELOGIN_ADDITION_CASE_IDS):
        raise AssertionError('current OAuth addition case coverage differs')
    additions = reset + relogin
    ids = [case['caseId'] for case in additions]
    if tuple(ids) != OAUTH_ADDITION_CASE_IDS:
        raise AssertionError('current OAuth addition case coverage differs')
    if set(ids) & {case['caseId'] for case in existing}:
        raise AssertionError('current OAuth addition replaces an existing case')
    if any(case['capabilityId'] != case['caseId'].split('.', 1)[0] for case in additions):
        raise AssertionError('current OAuth addition changes capability ownership')
    return additions


def load_current_jsonl(segment: str | Path) -> list[dict]:
    segment = Path(segment)
    archived = load_jsonl(segment)
    overlay = segment.parents[2] / 'model-center-2026-09-13' / 'overrides' / segment.name
    overrides = load_jsonl(overlay) if overlay.exists() else []
    notification_overlay = segment.parents[2] / 'notification-2026-10-17' / 'overrides' / segment.name
    if notification_overlay.exists():
        additions = load_jsonl(notification_overlay)
        if [c['caseId'] for c in additions] != ['TG-SYS-04.001-notifications']:
            raise AssertionError('unexpected notification overlay scope')
        overrides = [c for c in overrides if c['caseId'] != 'TG-SYS-04.001-notifications'] + additions
    body_overlay = segment.parents[2] / 'log-body-retention-2026-10-21' / 'overrides' / segment.name
    if body_overlay.exists():
        body_cases = load_jsonl(body_overlay)
        if tuple(c['caseId'] for c in body_cases) != BODY_RETENTION_OVERRIDE_CASE_IDS:
            raise AssertionError('unexpected body-retention overlay scope')
        replaced = set(BODY_RETENTION_OVERRIDE_CASE_IDS)
        overrides = [c for c in overrides if c['caseId'] not in replaced] + body_cases
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
