"""Isolated Management application used by the WebUI backend tests."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import log_db
from src.management_api import (
    ManagementOriginMiddleware,
    ManagementRuntime,
    create_management_router,
    install_management_error_handlers,
)
from src.management_auth import (
    ApprovalService,
    ManagementStateStore,
    SessionPolicy,
    SessionService,
)
from src.management_control import OperationRegistry, OperationStore, StoreAuditSink


FAKE_KEY = "pmk_" + "W" * 64
API = "/api/management/v1"


class FakeNotifier:
    def __init__(self) -> None:
        self.notifications = []

    def send(self, admin_ids, notification):
        self.notifications.append((admin_ids, notification))
        return True


def build_app(
    tmp_path,
    *,
    allowed_origins=(),
    telegram_admins=(42,),
    telegram_configured=True,
    clock=time.time,
    idle_timeout_seconds=3 * 24 * 60 * 60,
):
    store = ManagementStateStore(str(tmp_path / "management-webui.db"), clock=clock)
    sessions = SessionService(
        store,
        management_key=FAKE_KEY,
        policy=SessionPolicy(
            idle_timeout_seconds=idle_timeout_seconds,
            absolute_timeout_seconds=30 * 24 * 60 * 60,
            touch_interval_seconds=300,
        ),
        clock=clock,
        rate_limit_per_source=1000,
        rate_limit_global=1000,
    )
    notifier = FakeNotifier()
    approvals = ApprovalService(
        store,
        clock=clock,
        ttl_seconds=180,
        admin_ids_provider=lambda: tuple(telegram_admins),
        telegram_configured_provider=lambda: telegram_configured,
        notifier=notifier,
        rate_limit_per_source=1000,
        rate_limit_global=1000,
    )
    audit_sink = StoreAuditSink(store)
    operations = OperationStore(audit_sink=audit_sink)
    runtime = ManagementRuntime(
        sessions=sessions,
        approvals=approvals,
        operations=operations,
        operation_registry=OperationRegistry(operations),
        audit_sink=audit_sink,
        state_store=store,
        allowed_origins=frozenset(allowed_origins),
        application_version="0.webui-test",
        documentation_url="/docs",
    )
    app = FastAPI()
    app.state.management_runtime = runtime
    app.include_router(create_management_router())
    install_management_error_handlers(app)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    app.add_middleware(ManagementOriginMiddleware)
    return app, runtime, notifier


def key_session(client, *, transport=None, headers=None, **extra) -> dict:
    body = {"grantType": "managementKey", "managementKey": FAKE_KEY, **extra}
    if transport is not None:
        body["sessionTransport"] = transport
    response = client.post(f"{API}/auth/sessions", json=body, headers=headers or {})
    assert response.status_code == 201, response.text
    return response


def bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


class MonthlyLogStore:
    """Point log_db at a private monthly directory for one test."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        self.dir = tmp_path / "logs"
        self.dir.mkdir()
        monkeypatch.setattr(log_db, "_log_dir", str(self.dir))
        monkeypatch.setattr(log_db, "_initialized", True)
        monkeypatch.setattr(log_db, "_local", threading.local())
        monkeypatch.setattr(log_db, "_write_conn_registry", {})
        monkeypatch.setattr(log_db, "_retired_log_paths", set())
        log_db._get_conn()
        self.current_start = datetime.now(log_db._BJT).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )

    def previous_month_start(self) -> datetime:
        end = self.current_start - timedelta(seconds=1)
        return end.replace(day=1, hour=0, minute=0, second=0)

    def month_conn(self, start: datetime) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.dir / f"{start.strftime('%Y-%m')}.db"))
        conn.executescript(log_db._schema_sql())
        return conn

    def close(self) -> None:
        for connections in list(log_db._write_conn_registry.values()):
            for conn in connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


def insert_request(conn, **values) -> None:
    row = {
        "request_id": values.pop("request_id"),
        "created_at": values.pop("created_at"),
        "status": values.pop("status", "success"),
        "is_stream": values.pop("is_stream", 1),
        "msg_count": 1,
        "tool_count": 0,
        "ingress_protocol": values.pop("ingress_protocol", "anthropic"),
        "requested_model": values.pop("requested_model", "model-a"),
        "final_model": values.pop("final_model", None),
        "final_channel_key": values.pop("final_channel_key", "api:alpha"),
        "api_key_name": values.pop("api_key_name", "client"),
        "input_tokens": values.pop("input_tokens", 0),
        "output_tokens": values.pop("output_tokens", 0),
        "cache_creation_tokens": values.pop("cache_creation_tokens", 0),
        "cache_read_tokens": values.pop("cache_read_tokens", 0),
        "first_token_time_ms": values.pop("first_token_time_ms", None),
        "total_time_ms": values.pop("total_time_ms", None),
        "retry_count": values.pop("retry_count", 0),
        "usage_observed": values.pop("usage_observed", 1),
    }
    assert not values, values
    columns = ",".join(row)
    conn.execute(
        f"INSERT INTO request_log ({columns}) VALUES ({','.join('?' for _ in row)})",
        tuple(row.values()),
    )


def insert_attempt(conn, *, request_id, retry_attempt_id, channel_key="api:alpha",
                   model="model-a", cost_source="actual", cost_ticks=0,
                   input_tokens=0, output_tokens=0) -> None:
    conn.execute(
        """INSERT INTO upstream_attempt_usage (
               retry_attempt_id, root_request_id, call_request_id, attempt_order,
               channel_key, channel_type, model, outcome, usage_observed,
               input_tokens, output_tokens, cost_source, cost_ticks, settled_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            retry_attempt_id, request_id, request_id, 1, channel_key, "api", model,
            "success", 1, input_tokens, output_tokens, cost_source, cost_ticks,
            time.time(),
        ),
    )
