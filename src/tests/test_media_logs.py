"""Unified multimedia log persistence, upgrade, and Telegram UI tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(_ROOT))
from src.tests import _isolation

_isolation.isolate()

from src import config, image_db, log_db, media_db
from src.telegram import bot, states, ui
from src.telegram.menus import image_menu, logs_menu, media_logs_menu, xai_imagine_menu


class _ApiRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}

    def last(self, method: str) -> dict | None:
        matches = [payload for name, payload in self.calls if name == method]
        return matches[-1] if matches else None


@pytest.fixture(autouse=True)
def _clean_media_logs(monkeypatch):
    image_db.init()
    log_db.init()
    conn = image_db._get_conn()
    conn.execute("DELETE FROM image_attempt_logs")
    conn.execute("DELETE FROM image_call_logs")
    conn.commit()
    states.clear_all()
    ui.configure("TOKEN", [42])
    recorder = _ApiRecorder()
    monkeypatch.setattr(ui, "api", recorder)

    def _send_photo(chat_id: int, path: str, caption: str = ""):
        payload = {"chat_id": chat_id, "photo": path, "caption": caption}
        recorder.calls.append(("sendPhoto", payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ui, "send_photo", _send_photo)

    def _send_video(chat_id: int, path: str, caption: str = ""):
        payload = {"chat_id": chat_id, "video": path, "caption": caption}
        recorder.calls.append(("sendVideo", payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ui, "send_video", _send_video)
    yield recorder


def _create_gpt_image(*, cache_path: str | None = None) -> int:
    log_id = image_db.start_call(
        request_id="gpt-image-local",
        api_key_name="client-a",
        action="generate",
        main_model="gpt-5.4-mini",
        tool_model="gpt-image-2",
        size="1024x1024",
        prompt_preview="a blue square",
        prompt_hash="hash-gpt",
    )
    image_db.finish_call(
        log_id,
        status="success",
        account_key="openai:gpt@example.test",
        account_email="gpt@example.test",
        duration_ms=1200,
        image_count=1,
        cached_images=1 if cache_path else 0,
        image_bytes=1234 if cache_path else 0,
        cache_paths=[cache_path] if cache_path else [],
    )
    return log_id


def test_unified_media_rows_keep_gpt_history_and_update_video_in_place():
    gpt_id = _create_gpt_image()
    prompt = "x" * 400
    xai_image_id = media_db.start_call(
        request_id="xai-image-local",
        api_key_name="client-b",
        provider="xai",
        media_type="image",
        action="edit",
        model="grok-imagine-image-quality",
        prompt=prompt,
        aspect_ratio="16:9",
        resolution="2k",
        requested_count=2,
    )
    media_db.finish_call(
        xai_image_id,
        status="success",
        account_key="xai:grok@example.test",
        account_email="grok@example.test",
        duration_ms=2300,
        request_duration_ms=2300,
        image_count=2,
        usage={"cost_in_usd_ticks": 200_000_000},
        http_status=200,
    )

    video_id = media_db.start_call(
        request_id="video-local",
        api_key_name="client-b",
        provider="xai",
        media_type="video",
        action="generate",
        model="grok-imagine-video",
        prompt="a dot moves",
        aspect_ratio="16:9",
        resolution="480p",
        media_duration_seconds=1,
    )
    media_db.finish_call(
        video_id,
        status="pending",
        account_key="xai:grok@example.test",
        account_email="grok@example.test",
        upstream_request_id="video-job-1",
        upstream_status="pending",
        progress=10,
        request_duration_ms=140,
        expires_at=9_999_999_999,
        http_status=200,
    )
    assert media_db.update_job(
        "video-job-1",
        status="success",
        upstream_status="done",
        progress=100,
        media_duration_seconds=1,
        usage={"cost_in_usd_ticks": 500_000_000},
        last_polled_at=123456,
        http_status=200,
    ) is True

    assert media_db.count() == 3
    summary = media_db.summary()
    assert summary["openai_images"] == 1
    assert summary["xai_images"] == 1
    assert summary["videos"] == 1
    assert summary["success_count"] == 3
    assert summary["cost_usd_ticks"] == 700_000_000

    gpt = media_db.get_log(gpt_id)
    assert gpt["provider"] == "openai"
    assert gpt["media_type"] == "image"
    assert gpt["model"] == "gpt-image-2"
    assert gpt["request_duration_ms"] == 1200

    xai_image = media_db.get_log(xai_image_id)
    assert xai_image["prompt_preview"].endswith("…")
    assert len(xai_image["prompt_preview"]) == 181
    assert xai_image["prompt_preview"] != prompt

    video = media_db.get_by_upstream_request_id("video-job-1")
    assert video is not None
    assert video["id"] == video_id
    assert video["status"] == "success"
    assert video["upstream_status"] == "done"
    assert video["progress"] == 100
    assert video["finished_at"] is not None
    first_finished_at = video["finished_at"]
    first_duration_ms = video["duration_ms"]
    assert media_db.update_job(
        "video-job-1",
        status="success",
        upstream_status="done",
        progress=100,
        last_polled_at=123457,
    ) is True
    video_again = media_db.get_by_upstream_request_id("video-job-1")
    assert video_again["finished_at"] == first_finished_at
    assert video_again["duration_ms"] == first_duration_ms
    assert media_db.count() == 3  # polling updated the original task, no extra row


def test_media_log_menu_is_separate_from_request_logs_and_can_send_cached_image(tmp_path, _clean_media_logs):
    cached = tmp_path / "cached.png"
    cached.write_bytes(b"not-a-real-png-but-present")
    log_id = _create_gpt_image(cache_path=str(cached))

    media_db.finish_call(
        media_db.start_call(
            request_id="video-pending",
            api_key_name="client-video",
            provider="xai",
            media_type="video",
            action="generate",
            model="grok-imagine-video",
            prompt="moving dot",
            resolution="480p",
            media_duration_seconds=1,
        ),
        status="pending",
        account_key="xai:grok@example.test",
        account_email="grok@example.test",
        upstream_request_id="pending-job",
        upstream_status="pending",
        progress=64,
        request_duration_ms=100,
        expires_at=9_999_999_999,
    )

    media_logs_menu.show(42, 100, "cb-media")
    message = _clean_media_logs.last("editMessageText")
    assert message is not None
    assert "最近日志 · 多媒体" in message["text"]
    assert "GPT 1 · Grok 0 · 🎬 视频 1" in message["text"]
    assert "gpt-image-2" in message["text"]
    assert "grok-imagine-video" in message["text"]
    assert "进度 64%" in message["text"]
    keyboard = message["reply_markup"]["inline_keyboard"]
    callbacks = {
        button["callback_data"]
        for row in keyboard
        for button in row
    }
    labels = {button["text"] for row in keyboard for button in row}
    assert "menu:logs" in callbacks
    assert any(value.startswith("media:detail:") for value in callbacks)
    assert {"🏠 首页", "◀ 上一页", "1/1", "下一页 ▶"} <= labels
    assert {"⏮ 首页", "◀", "▶"}.isdisjoint(labels)

    short = ui.register_code(f"medialog:{log_id}")
    media_logs_menu.show_detail(42, 100, "cb-detail", short, page=1)
    detail = _clean_media_logs.last("editMessageText")
    assert "多媒体日志详情" in detail["text"]
    assert "gpt@example.test" in detail["text"]
    detail_callbacks = {
        button["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert f"media:view:{short}:1" in detail_callbacks
    assert "menu:logs" not in detail_callbacks

    media_logs_menu.send_cached_images(42, "cb-view", short)
    assert _clean_media_logs.last("sendPhoto")["photo"] == str(cached)

    logs_menu.show(42, 100, "cb-requests")
    request_message = _clean_media_logs.last("editMessageText")
    request_callbacks = {
        button["callback_data"]
        for row in request_message["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "media:logs" in request_callbacks


def test_media_detail_can_send_cached_grok_video(tmp_path, _clean_media_logs):
    cached = tmp_path / "grok-result.mp4"
    cached.write_bytes(b"small-test-video")
    log_id = media_db.start_call(
        request_id="grok-video-cached",
        api_key_name="client-video",
        provider="xai",
        media_type="video",
        action="generate",
        model="grok-imagine-video",
        prompt="moving dot",
        resolution="480p",
        media_duration_seconds=1,
    )
    media_db.finish_call(
        log_id,
        status="success",
        account_key="xai:subject-current",
        account_email="current@example.test",
        upstream_request_id="video-cached-job",
        upstream_status="done",
        progress=100,
        cached_media_count=1,
        media_bytes=cached.stat().st_size,
        cache_paths=[str(cached)],
    )

    short = ui.register_code(f"medialog:{log_id}")
    media_logs_menu.show_detail(42, 100, "cb-video-detail", short, page=1)
    detail = _clean_media_logs.last("editMessageText")
    assert "查看缓存视频" in str(detail["reply_markup"])
    callbacks = {
        button["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for button in row
    }
    assert "menu:logs" not in callbacks
    assert f"media:view:{short}:1" in callbacks

    media_logs_menu.send_cached_media(42, "cb-video-view", short)
    assert _clean_media_logs.last("sendVideo")["video"] == str(cached)


def test_oauth_top_excludes_accounts_removed_from_current_config(monkeypatch):
    monkeypatch.setitem(config._cache, "oauthAccounts", [{
        "provider": "xai",
        "email": "current@example.test",
        "subject": "subject-current",
        "enabled": True,
    }])

    for index in range(4):
        removed_id = media_db.start_call(
            request_id=f"removed-{index}",
            api_key_name="client",
            provider="xai",
            media_type="image",
            action="generate",
            model="grok-imagine-image",
        )
        media_db.finish_call(
            removed_id,
            status="success",
            account_key="xai:subject-removed",
            account_email="removed@example.test",
        )

    current_id = media_db.start_call(
        request_id="current-account",
        api_key_name="client",
        provider="xai",
        media_type="image",
        action="generate",
        model="grok-imagine-image",
    )
    media_db.finish_call(
        current_id,
        status="success",
        account_key="xai:subject-current",
        account_email="current@example.test",
    )

    top = media_logs_menu._current_account_top(3)
    assert [row["account_key"] for row in top] == ["xai:subject-current"]
    assert all(row["account_email"] != "removed@example.test" for row in top)


def test_settings_pages_link_to_unified_media_logs_and_bot_routes_callback(_clean_media_logs):
    image_menu.show(42, 100, "cb-image")
    image_message = _clean_media_logs.last("editMessageText")
    assert "统一到多媒体日志" in image_message["text"]
    assert any(
        button.get("callback_data") == "media:logs"
        for row in image_message["reply_markup"]["inline_keyboard"]
        for button in row
    )

    xai_imagine_menu.show(42, 100, "cb-xai")
    xai_message = _clean_media_logs.last("editMessageText")
    assert "统一统计、费用和任务详情" in xai_message["text"]
    assert any(
        button.get("callback_data") == "media:logs"
        for row in xai_message["reply_markup"]["inline_keyboard"]
        for button in row
    )

    bot._handle_callback({
        "id": "cb-route",
        "message": {"chat": {"id": 42}, "message_id": 100},
        "data": "media:logs",
    })
    assert "最近日志 · 多媒体" in _clean_media_logs.last("editMessageText")["text"]


def test_legacy_image_database_is_extended_in_place(tmp_path):
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    config_path = data_dir / "config.json"
    image_path = data_dir / "image_logs.db"
    config_path.write_text(
        json.dumps({
            "listen": {"host": "127.0.0.1", "port": 0},
            "apiKeys": {},
            "oauthAccounts": [],
            "channels": [],
            "stateDbPath": "state.db",
            "logDir": "logs",
            "telegram": {"botToken": "", "adminIds": []},
            "images": {"dbPath": "image_logs.db"},
            "oauth": {"mockMode": True},
        }),
        encoding="utf-8",
    )

    script = r'''
import os
import sqlite3

path = os.path.join(os.environ["ANTHROPIC_PROXY_DATA_DIR"], "image_logs.db")
conn = sqlite3.connect(path)
conn.executescript("""
CREATE TABLE image_call_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT UNIQUE NOT NULL,
  created_at REAL NOT NULL,
  finished_at REAL,
  api_key_name TEXT,
  action TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  account_key TEXT,
  account_email TEXT,
  main_model TEXT,
  tool_model TEXT,
  size TEXT,
  prompt_preview TEXT,
  prompt_hash TEXT,
  duration_ms INTEGER,
  image_count INTEGER DEFAULT 0,
  cached_images INTEGER DEFAULT 0,
  image_bytes INTEGER DEFAULT 0,
  cache_paths TEXT,
  usage_json TEXT,
  error_type TEXT,
  error_message TEXT
);
CREATE TABLE image_attempt_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  image_log_id INTEGER NOT NULL,
  request_id TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  account_key TEXT,
  account_email TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  duration_ms INTEGER,
  image_count INTEGER DEFAULT 0,
  image_bytes INTEGER DEFAULT 0,
  error_type TEXT,
  error_message TEXT
);
""")
conn.execute(
    "INSERT INTO image_call_logs "
    "(request_id, created_at, finished_at, api_key_name, action, status, "
    " account_key, account_email, main_model, tool_model, duration_ms, image_count) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    ("legacy-row", 100.0, 101.0, "legacy-key", "generate", "success",
     "openai:legacy@example.test", "legacy@example.test", "gpt-main", "gpt-image-old", 1000, 1),
)
conn.commit()
conn.close()

from src import image_db
image_db.init()
row = image_db._get_conn().execute(
    "SELECT request_id, provider, media_type, model, request_duration_ms "
    "FROM image_call_logs WHERE request_id='legacy-row'"
).fetchone()
assert tuple(row) == ("legacy-row", "openai", "image", "gpt-image-old", 1000), tuple(row)
cols = {item[1] for item in image_db._get_conn().execute("PRAGMA table_info(image_call_logs)")}
assert {"provider", "media_type", "upstream_request_id", "cost_usd_ticks", "progress"} <= cols
'''
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_PROXY_DATA_DIR": str(data_dir),
        "ANTHROPIC_PROXY_CONFIG": str(config_path),
        "DISABLE_OAUTH_NETWORK_CALLS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout
