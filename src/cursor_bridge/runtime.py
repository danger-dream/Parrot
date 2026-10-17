"""Private loopback HTTP runtime for Parrot Cursor OAuth channels.

The bridge is deliberately not a public API.  It lets Parrot reuse its existing
OpenAI Chat transport, SSE translators, failover, accounting, and protocol
bridges while keeping Cursor's bidirectional H2 sessions in a dedicated thread
runtime.  A process-random bearer secret and loopback-only bind protect the
internal endpoint.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import select
import socket
import threading
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .client import CursorClient
from .errors import CursorError
from .openai_messages import terminal_tool_messages

_ACCOUNT_HEADER = "X-Parrot-Cursor-Account"
_SESSION_HEADER = "X-Parrot-Cursor-Session"
_PRINCIPAL_HEADER = "X-Parrot-Cursor-Principal"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: "CursorBridgeRuntime") -> None:
        self.runtime = runtime
        super().__init__(address, _Handler)


    def service_actions(self) -> None:
        self.runtime.reap_sessions()


class CursorBridgeRuntime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._secret = secrets.token_urlsafe(32)
        self._clients: dict[str, CursorClient] = {}
        self._tool_sessions: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._session_tools: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._active_sessions: dict[tuple[str, str], str] = {}

    def ensure_started(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            from .. import config

            cfg = config.get().get("cursorOAuth") or {}
            host = str(cfg.get("bridgeHost") or "127.0.0.1")
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Cursor internal bridge must bind to loopback")
            try:
                port = int(cfg.get("bridgePort", 0) or 0)
            except (TypeError, ValueError):
                port = 0
            server = _Server((host, max(0, port)), self)
            thread = threading.Thread(
                target=server.serve_forever,
                name="parrot-cursor-bridge",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
            print(f"[cursor] internal bridge listening on {self.base_url}")

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            return ""
        host, port = server.server_address[:2]
        if host == "::1":
            return f"http://[::1]:{port}"
        return f"http://{host}:{port}"

    @property
    def bearer_secret(self) -> str:
        return self._secret

    def update_account(self, account_key: str, access_token: str) -> CursorClient:
        if not account_key or not access_token:
            raise ValueError("Cursor bridge requires account_key and access token")
        self.ensure_started()
        with self._lock:
            client = self._clients.get(account_key)
            if client is None:
                from .. import config

                cfg = config.get().get("cursorOAuth") or {}
                client = CursorClient(
                    access_token,
                    # oauth_manager owns refresh and durable token rotation.
                    refresh_token=None,
                    max_retries=int(cfg.get("maxRetries", 2) or 2),
                    request_timeout_s=float(cfg.get("requestTimeoutSeconds", 300) or 300),
                    account_key=account_key,
                    channel_key=f"oauth:{account_key}",
                )
                self._clients[account_key] = client
            else:
                client.update_access_token(access_token)
            return client

    def get_client(self, account_key: str) -> CursorClient | None:
        with self._lock:
            return self._clients.get(account_key)

    def session_for(self, account_key: str, body: dict[str, Any], explicit: str = "", *, principal: str = "") -> str:
        messages = body.get("messages") if isinstance(body.get("messages"), list) else []
        tool_ids = [str(message.get("tool_call_id")) for message in terminal_tool_messages(messages)
                    if message.get("tool_call_id")]
        stable = "stable-" + hashlib.sha256(json.dumps([account_key, principal, explicit]).encode()).hexdigest()[:32] if explicit else ""
        with self._lock:
            candidates = [self._tool_sessions[(account_key, principal, tool_id)]
                          for tool_id in tool_ids if (account_key, principal, tool_id) in self._tool_sessions]
            matches = set.intersection(*(set(ids) for ids in candidates)) if candidates else set()
            if candidates and not matches:
                raise CursorError("Tool results belong to different conversations", code="invalid_request", status=400)
            if stable in matches:
                return stable
            if len(matches) == 1:
                return next(iter(matches))
            if len(matches) > 1:
                raise CursorError("Ambiguous tool call ID; supply the conversation anchor", code="invalid_request", status=400)
        return stable or "turn-" + uuid.uuid4().hex

    def begin_session(self, account_key, body, explicit, principal):
        with self._lock:
            session_id = self.session_for(account_key, body, explicit, principal=principal)
            if (account_key, session_id) in self._active_sessions:
                if terminal_tool_messages(body.get("messages") or []):
                    raise CursorError("Tool response is already being consumed", code="conversation_busy", status=409)
                session_id = "turn-" + uuid.uuid4().hex
            owner = uuid.uuid4().hex
            self._active_sessions[(account_key, session_id)] = owner
            return session_id, owner

    def end_session(self, account_key, session_id, owner):
        with self._lock:
            if self._active_sessions.get((account_key, session_id)) == owner:
                self._active_sessions.pop((account_key, session_id), None)

    def register_tool_call(self, account_key: str, session_id: str, tool_call_id: str, *, principal: str = "") -> None:
        if not tool_call_id:
            return
        with self._lock:
            self._tool_sessions[(account_key, principal, tool_call_id)].add(session_id)
            self._session_tools[(account_key, principal, session_id)].add(tool_call_id)

    def _forget_tools(self, account_key, session_id):
        for key in list(self._session_tools):
            if key[0] != account_key or key[2] != session_id:
                continue
            for tool_id in self._session_tools.pop(key):
                index = (account_key, key[1], tool_id)
                sessions = self._tool_sessions.get(index, set())
                sessions.discard(session_id)
                if not sessions:
                    self._tool_sessions.pop(index, None)

    def finish_session(self, account_key: str, session_id: str, *, cancel: bool = False, owner: str | None = None) -> None:
        with self._lock:
            if owner is not None and self._active_sessions.get((account_key, session_id)) != owner:
                return
            client = self._clients.get(account_key)
            self._forget_tools(account_key, session_id)
            # The runtime lock prevents a replacement request claiming this ID
            # between owner validation and cancellation of its actual instance.
            if client is not None:
                client.discard_conversation(session_id, cancel=cancel)

    def reap_sessions(self):
        with self._lock:
            for account, client in list(self._clients.items()):
                if not hasattr(client, "reap_conversations"):
                    continue
                protected = {sid for acc, sid in self._active_sessions if acc == account}
                for sid in client.reap_conversations(protected=protected):
                    self._forget_tools(account, sid)

    def drop_account(self, account_key: str) -> None:
        with self._lock:
            client = self._clients.pop(account_key, None)
            for key in list(self._session_tools):
                if key[0] == account_key:
                    self._forget_tools(account_key, key[2])
            for key in list(self._active_sessions):
                if key[0] == account_key:
                    self._active_sessions.pop(key, None)
        if client is not None:
            client.close()

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            clients = list(self._clients.values())
            self._clients.clear()
            self._tool_sessions.clear()
            self._session_tools.clear()
            self._active_sessions.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        for client in clients:
            client.close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return

    @property
    def runtime(self) -> CursorBridgeRuntime:
        return self.server.runtime

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            if value:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization", "") == f"Bearer {self.runtime.bearer_secret}":
            return True
        self._json(401, {"error": {"message": "unauthorized", "type": "authentication_error", "code": "unauthorized"}})
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlparse(self.path).path == "/health":
            self._json(200, {"ok": True, "service": "parrot-cursor-bridge"})
            return
        self._json(404, {"error": {"message": "Not Found", "type": "invalid_request_error", "code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if urlparse(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "Not Found", "type": "invalid_request_error", "code": "not_found"}})
            return
        account_key = str(self.headers.get(_ACCOUNT_HEADER) or "").strip()
        client = self.runtime.get_client(account_key)
        if client is None:
            self._json(401, {"error": {"message": "Cursor account runtime is not initialized", "type": "authentication_error", "code": "cursor_account_missing"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error", "code": "invalid_request"}})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": {"message": "Invalid chat completion request", "type": "invalid_request_error", "code": "invalid_request"}})
            return

        explicit = str(self.headers.get(_SESSION_HEADER) or body.get("session_id") or "").strip()
        principal = str(self.headers.get(_PRINCIPAL_HEADER) or "")
        try:
            session_id, owner = self.runtime.begin_session(account_key, body, explicit, principal)
        except CursorError as exc:
            self._json(exc.status, exc.to_openai_error())
            return
        stopped = threading.Event()
        disconnected = threading.Event()

        def watch_disconnect():
            while not stopped.wait(0.05):
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK) and not stopped.is_set():
                        disconnected.set()
                except (OSError, ValueError):
                    if not stopped.is_set():
                        disconnected.set()
                if disconnected.is_set() and not stopped.is_set():
                    self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)

        watcher = threading.Thread(target=watch_disconnect, name="cursor-http-disconnect", daemon=True)
        watcher.start()
        try:
            self._complete(client, account_key, session_id, owner, principal, body, stopped)
        except (BrokenPipeError, ConnectionResetError):
            self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
        except BaseException:
            self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
            raise
        finally:
            stopped.set()
            watcher.join(timeout=1)
            if disconnected.is_set():
                self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
            self.runtime.end_session(account_key, session_id, owner)

    def _complete(self, client, account_key, session_id, owner, principal, body, watcher_stop):
        try:
            result = client.chat_completions(
                model=str(body.get("model") or ""),
                messages=body.get("messages") or [],
                tools=body.get("tools"),
                tool_choice=body.get("tool_choice"),
                stream=bool(body.get("stream", True)),
                session_id=session_id,
                long_context=bool(body.get("cursor_long_context", False)),
            )
        except CursorError as exc:
            self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
            observation = getattr(exc, "oauth_refresh_observation", None)
            # Private loopback metadata: the rejected H2 session may predate
            # this HTTP turn or the client's latest token update.
            headers = {"X-Parrot-OAuth-Observation": json.dumps(observation)} if observation else None
            self._json(exc.status, exc.to_openai_error(), headers=headers)
            return
        except (ValueError, KeyError, TypeError) as exc:
            self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
            self._json(400, {"error": {"message": str(exc), "type": "invalid_request_error", "code": "invalid_request"}})
            return

        if isinstance(result, dict):
            message = (((result.get("choices") or [{}])[0] or {}).get("message") or {})
            tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
            for call in tool_calls or []:
                if isinstance(call, dict):
                    self.runtime.register_tool_call(account_key, session_id, str(call.get("id") or ""), principal=principal)
            if not tool_calls:
                self.runtime.finish_session(account_key, session_id, owner=owner)
            # A completed HTTP turn may legitimately close its socket while
            # the native tool session remains paused for the next request.
            watcher_stop.set()
            self._json(200, result)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        paused_for_tools = False
        try:
            for chunk in result:
                choices = chunk.get("choices") if isinstance(chunk, dict) else None
                for choice in choices or []:
                    delta = choice.get("delta") if isinstance(choice, dict) else None
                    for call in (delta.get("tool_calls") if isinstance(delta, dict) else []) or []:
                        if isinstance(call, dict):
                            self.runtime.register_tool_call(account_key, session_id, str(call.get("id") or ""), principal=principal)
                    if isinstance(choice, dict) and choice.get("finish_reason") == "tool_calls":
                        paused_for_tools = True
                        watcher_stop.set()
                self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except CursorError as exc:
            try:
                self.wfile.write(f"data: {json.dumps(exc.to_openai_error(), ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            finally:
                self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
        except (BrokenPipeError, ConnectionResetError):
            self.runtime.finish_session(account_key, session_id, cancel=True, owner=owner)
        finally:
            result.close()
            if not paused_for_tools:
                self.runtime.finish_session(account_key, session_id, owner=owner)


runtime = CursorBridgeRuntime()


def ensure_started() -> None:
    runtime.ensure_started()


def stop() -> None:
    runtime.stop()


def base_url() -> str:
    runtime.ensure_started()
    return runtime.base_url


def bearer_secret() -> str:
    return runtime.bearer_secret


def update_account(account_key: str, access_token: str) -> None:
    runtime.update_account(account_key, access_token)


def drop_account(account_key: str) -> None:
    runtime.drop_account(account_key)


__all__ = [
    "_ACCOUNT_HEADER",
    "_SESSION_HEADER",
    "_PRINCIPAL_HEADER",
    "base_url",
    "bearer_secret",
    "drop_account",
    "ensure_started",
    "runtime",
    "stop",
    "update_account",
]
