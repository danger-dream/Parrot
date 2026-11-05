#!/usr/bin/env python3
"""Exercise the built OAuth page with synthetic accounts and a loopback-only API.

Run after an isolated build: python3 webui/scripts/test-oauth-drag.py /path/to/build
Requires Python Playwright and a local Chrome/Chromium executable.
"""
from __future__ import annotations

import json
import mimetypes
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


class State:
    def __init__(self):
        self.accounts = [
            {"accountId": key, "displayName": f"Account {key}", "identity": f"{key}@example.invalid",
             "provider": provider, "enabled": True, "available": True, "invalid": False,
             "quotaLimited": False, "modelCount": 2, "disabledModelCount": 0,
             "maxConcurrent": 0, "credentialConfigured": True, "revision": "fixture", "card": None}
            for key, provider in [("A", "openai"), ("B", "claude"), ("C", "openai"), ("D", "openai")]
        ]
        self.order = [a["accountId"] for a in self.accounts]
        self.saved = []
        self.fail_next = 0
        self.delay_accounts = 0.0
        self.read_started = threading.Event()


def main(build: Path):
    build = build.resolve()
    assert (build / "index.html").is_file(), f"not a built WebUI: {build}"
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, body, content_type="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/management/v1/"):
                key = path.removeprefix("/api/management/v1")
                if key == "/auth/session":
                    data = {"authMethod": "managementKey", "idleExpiresAt": "2099-01-01T00:00:00Z"}
                elif key == "/oauth/accounts":
                    by_id = {a["accountId"]: a for a in state.accounts}
                    data = {"items": [by_id[key] for key in state.order], "revision": "fixture"}
                    delay = state.delay_accounts
                    if delay:
                        state.read_started.set()
                        time.sleep(delay)
                elif key == "/stats/outcome-timeline":
                    data = {"from": "2026-01-01T00:00:00Z", "bucketSeconds": 3600, "items": []}
                elif key == "/runtime/status":
                    data = {"channels": []}
                elif key == "/overview":
                    data = {"listeners": {"port": 0}}
                elif key == "/load-balancing":
                    data = {"mode": "order"}
                else:
                    self.send(404, {"error": {"code": "NOT_FOUND", "message": "fixture endpoint"}})
                    return
                self.send(200, {"data": data, "meta": {"hasNext": False}})
                return
            if path.startswith("/ui/"):
                relative = path.removeprefix("/ui/")
                file = (build / relative).resolve()
                if not file.is_relative_to(build):
                    self.send(404, b"not found", "text/plain")
                    return
                if not file.is_file():
                    file = build / "index.html"
                self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream")
                return
            self.send(404, b"not found", "text/plain")

        def do_PUT(self):
            if urlsplit(self.path).path != "/api/management/v1/oauth/account-order":
                self.send(404, {"error": {"code": "NOT_FOUND", "message": "fixture endpoint"}})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.saved.append(body["accountIds"])
            if state.fail_next:
                status = state.fail_next
                state.fail_next = 0
                code = "REVISION_CONFLICT" if status == 409 else "FIXTURE_FAILURE"
                self.send(status, {"error": {"code": code, "message": "simulated failure"}})
                return
            state.order = list(body["accountIds"])
            self.send(200, {"data": {"revision": "fixture"}, "meta": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"

    try:
        with sync_playwright() as playwright:
            executable = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
            browser = playwright.chromium.launch(executable_path=executable, headless=True, args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1600, "height": 1100})
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(origin + "/ui/oauth", wait_until="networkidle")
            page.locator(".alist [data-account-id]").first.wait_for()

            def order():
                return page.locator("[data-account-id]").evaluate_all("els => els.map(e => e.dataset.accountId)")

            def coords(key, fx=.5, fy=.5):
                box = page.locator(f'[data-account-id="{key}"]').bounding_box()
                assert box, f"account {key} missing"
                return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy

            def drag(source, target, *, target_fraction=(.5, .75)):
                label = page.locator(f'[data-account-id="{source}"] .name').bounding_box()
                assert label
                sx, sy = label["x"] + min(20, label["width"] / 2), label["y"] + label["height"] / 2
                tx, ty = coords(target, *target_fraction)
                page.mouse.move(sx, sy)
                page.mouse.down()
                page.mouse.move(sx + 8, sy + 8)
                page.mouse.move(tx, ty, steps=6)
                page.wait_for_timeout(60)
                return tx, ty

            def finish():
                page.mouse.up()
                page.wait_for_timeout(350)

            def close_inspector():
                page.locator('.inspector button[aria-label="关闭"]').click()
                page.wait_for_timeout(450)

            # A normal click still opens details.
            page.locator('[data-account-id="A"] .name').click()
            assert page.locator(".inspector.open").count() == 1
            close_inspector()

            # Table: move over B, then hold and wiggle over the same layout location.
            tx, ty = drag("A", "B")
            page.wait_for_timeout(300)
            initial = order()
            assert initial[:2] == ["B", "A"], initial
            page.evaluate("""() => { const now = Date.now; Date.now = () => now() + 61000;
                window.dispatchEvent(new Event('focus')); Date.now = now }""")
            page.wait_for_timeout(200)
            assert order() == initial, ("refresh interrupted drag", initial, order())
            for dy in (0, 2, -2, 1, -1, 0):
                page.mouse.move(tx, ty + dy)
                page.wait_for_timeout(55)
                assert order() == initial, (initial, order())
            assert page.evaluate("window.getSelection().toString()") == ""
            assert page.locator(".inspector.open").count() == 0
            finish()
            assert state.saved[-1] == initial
            assert page.locator(".inspector.open").count() == 0
            page.locator('[data-account-id="B"] .name').click()
            assert page.locator(".inspector.open").count() == 1
            close_inspector()

            # A failed save restores the pre-drag order.
            state.fail_next = 500
            before = order()
            drag("A", "C")
            finish()
            assert order() == before, (before, order())
            assert page.get_by_text("顺序没有保存，已恢复原顺序").count() == 1

            # A conflict reloads the authoritative order after the failed save.
            state.fail_next = 409
            state.order = ["D", "C", "B", "A"]
            drag("A", "C")
            finish()
            assert order() == state.order, (order(), state.order)
            state.order = before
            page.reload(wait_until="networkidle")
            page.locator(".alist [data-account-id]").first.wait_for()

            # An older in-flight refresh must not undo a newer completed drag.
            state.delay_accounts = 1.0
            state.read_started.clear()
            page.evaluate("""() => { const now = Date.now; Date.now = () => now() + 125000;
                window.dispatchEvent(new Event('focus')); Date.now = now }""")
            assert state.read_started.wait(timeout=2), "refresh did not start"
            state.delay_accounts = 0
            drag("B", "A")
            finish()
            newer = order()
            page.wait_for_timeout(1000)
            assert order() == newer == state.order, (newer, order(), state.order)
            state.order = before
            page.reload(wait_until="networkidle")
            page.locator(".alist [data-account-id]").first.wait_for()

            # Keyboard sorting still saves one step.
            page.locator('[data-account-id="A"]').focus()
            page.keyboard.press("Alt+ArrowDown")
            page.wait_for_timeout(300)
            assert order()[:3] == ["B", "C", "A"], order()
            assert state.saved[-1] == order()

            # Filtered reordering preserves the hidden account in the full request.
            page.locator(".ptabs").get_by_role("button", name="OpenAI", exact=False).click()
            page.wait_for_timeout(350)
            assert order() == ["C", "A", "D"], order()
            drag("C", "A")
            finish()
            assert order() == ["A", "C", "D"], order()
            assert state.saved[-1] == ["B", "A", "C", "D"], state.saved[-1]
            page.locator(".ptabs").get_by_role("button", name="全部", exact=False).click()
            page.wait_for_timeout(350)

            # Card grid: swap horizontally and hold over the target while it animates.
            page.get_by_role("tab", name="卡片", exact=True).click()
            page.locator(".grid [data-account-id]").first.wait_for()
            first, second = order()[:2]
            tx, ty = drag(first, second, target_fraction=(.75, .5))
            page.wait_for_timeout(300)
            initial = order()
            assert initial[:2] == [second, first], initial
            for dx in (0, 2, -2, 1, -1, 0):
                page.mouse.move(tx + dx, ty)
                page.wait_for_timeout(55)
                assert order() == initial
            finish()
            assert state.saved[-1] == initial, (state.saved[-1], initial, order())
            assert page.locator(".inspector.open").count() == 0

            # A pointer cancellation restores the saved order and does not save.
            before, saved = order(), len(state.saved)
            drag(before[0], before[1], target_fraction=(.75, .5))
            page.evaluate("window.dispatchEvent(new PointerEvent('pointercancel', {pointerId: 1, bubbles: true}))")
            page.mouse.up()
            page.wait_for_timeout(300)
            assert order() == before and len(state.saved) == saved

            # In a multi-row grid, vertical movement uses the target row's midpoint.
            page.set_viewport_size({"width": 1100, "height": 1100})
            page.wait_for_timeout(350)
            before = order()
            first_box = page.locator(f'[data-account-id="{before[0]}"]').bounding_box()
            last_box = page.locator(f'[data-account-id="{before[-1]}"]').bounding_box()
            assert first_box and last_box and last_box["y"] > first_box["y"]
            drag(before[0], before[-1], target_fraction=(.5, .8))
            finish()
            assert order()[-1] == before[0], (before, order())
            assert state.saved[-1] == order()
            assert not errors, errors
            context.close()

            mobile = browser.new_context(viewport={"width": 430, "height": 900}, has_touch=True, is_mobile=True)
            mobile.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
            touch = mobile.new_page()
            touch_errors = []
            touch.on("pageerror", lambda exc: touch_errors.append(str(exc)))
            touch.goto(origin + "/ui/oauth", wait_until="networkidle")
            touch.locator(".grid [data-account-id]").first.wait_for()
            keys = touch.locator(".grid [data-account-id]").evaluate_all("els => els.map(e => e.dataset.accountId)")
            touch.locator(f'[data-account-id="{keys[1]}"]').scroll_into_view_if_needed()
            first = touch.locator(f'[data-account-id="{keys[0]}"]').bounding_box()
            second = touch.locator(f'[data-account-id="{keys[1]}"]').bounding_box()
            assert first and second
            source = {"x": first["x"] + first["width"] * .35, "y": first["y"] + first["height"] * .5, "id": 1}
            target = {"x": second["x"] + second["width"] * .35, "y": second["y"] + second["height"] * .75, "id": 1}
            cdp = mobile.new_cdp_session(touch)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [source]})
            touch.wait_for_timeout(460)
            assert touch.locator(".grid.sorting").count() == 1, "touch long-press did not start drag"
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [target]})
            touch.wait_for_timeout(250)
            actual = touch.locator(".grid [data-account-id]").evaluate_all("els => els.map(e => e.dataset.accountId)")
            assert actual[:2] == [keys[1], keys[0]], (keys, actual)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            touch.wait_for_timeout(400)
            assert state.saved[-1] == actual
            assert touch.locator(".inspector.open").count() == 0
            assert touch.evaluate("window.getSelection().toString()") == ""
            touch.locator(f'[data-account-id="{actual[0]}"] .name').tap()
            assert touch.locator(".inspector.open").count() == 1
            touch.locator('.inspector button[aria-label="关闭"]').tap()
            touch.wait_for_timeout(450)
            current = touch.locator(f'[data-account-id="{actual[0]}"]')
            current.scroll_into_view_if_needed()
            rect = current.bounding_box()
            assert rect
            x, y = rect["x"] + rect["width"] * .35, rect["y"] + rect["height"] * .5
            saved = len(state.saved)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 2}]})
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - 30, "id": 2}]})
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            touch.wait_for_timeout(450)
            assert len(state.saved) == saved
            assert touch.locator(".inspector.open").count() == 0
            # Touch-only step buttons remain available without starting a drag.
            last = touch.locator(f'[data-account-id="{actual[-1]}"]')
            last.scroll_into_view_if_needed()
            last.get_by_role("button", name="上移").tap()
            touch.wait_for_timeout(350)
            assert state.saved[-1][-2:] == [actual[-1], actual[-2]]
            assert not touch_errors, touch_errors
            mobile.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("OAuth drag browser checks passed (table/cards, touch, clicks, selection, refresh races, filters, keyboard, rollback, cancel)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]))
