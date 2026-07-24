"""Unified multimedia business-log facade.

The physical database and legacy table stay in :mod:`src.image_db` so existing
installations retain every GPT image row in place.  New code imports this module
to make the image/video scope explicit without creating a second log store.
"""

from __future__ import annotations

from . import image_db as _db


init = _db.init
checkpoint = _db.checkpoint
start_call = _db.start_media_call
finish_call = _db.finish_media_call
update_job = _db.update_media_job
get_log = _db.get_log
get_by_upstream_request_id = _db.media_log_for_upstream
recent = _db.media_recent
count = _db.media_count
summary = _db.media_summary
account_top = _db.media_account_top
cleanup_expired = _db.cleanup_expired_media
fmt_bjt = _db.fmt_bjt
seconds_since = _db.seconds_since
