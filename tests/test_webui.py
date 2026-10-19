import copy
import json
import os
from pathlib import Path
from contextlib import contextmanager
import shutil
import time
import unittest
from unittest.mock import patch
import uuid

from webui import Console, DEFAULTS, ROOT, create_app, validate_run_settings


@contextmanager
def test_workspace():
    base = (ROOT / ".webui" / "tests").resolve()
    folder = base / uuid.uuid4().hex
    folder.mkdir(parents=True)
    try:
        yield folder
    finally:
        if folder.resolve().parent != base or base != (ROOT / ".webui" / "tests").resolve():
            raise RuntimeError("Unsafe test cleanup path")
        shutil.rmtree(folder)


class WebUITest(unittest.TestCase):
    def setUp(self):
        self.workspace = test_workspace()
        self.folder = self.workspace.__enter__()
        self.app = create_app(self.folder, demo=True)
        self.console = self.app.extensions["console"]
        self.client = self.app.test_client()
        self.headers = {"X-Console-Token": self.console.token}

    def tearDown(self):
        self.console.stop()
        self.wait_idle()
        self.workspace.__exit__(None, None, None)

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def wait_idle(self, limit=8):
        end = time.monotonic() + limit
        while self.console.busy and time.monotonic() < end:
            time.sleep(0.025)
        self.assertFalse(self.console.busy, "worker did not exit")

    def login(self):
        self.assertEqual(self.post("/api/action", {"action": "login"}).status_code, 202)
        self.wait_idle()
        self.assertTrue(self.console.authenticated)

    def test_local_routes_and_access_controls(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.client.get("/static/console.js") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/state").status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "attacker.test"}).status_code, 403)
        headers = {**self.headers, "Origin": "https://attacker.test"}
        self.assertEqual(self.client.post("/api/stop", json={}, headers=headers).status_code, 403)
        self.assertIn("no-store", self.client.get("/").headers["Cache-Control"])

    def test_settings_validation_and_secret_storage(self):
        data = {"common": {"username": "12345678901", "password": "test-password"}, "tiku": {"key": "secret-key", "tokens": "one-token,two-token"}, "notification": {"url": "https://example.test/secret"}}
        self.assertEqual(self.post("/api/settings", data).status_code, 200)
        saved = (self.folder / "settings.json").read_text(encoding="utf-8")
        for secret in ("test-password", "secret-key", "one-token", "https://example.test/secret"):
            self.assertNotIn(secret, saved)
        state = self.client.get("/api/state", headers=self.headers).get_json()
        self.assertEqual(state["settings"]["common"]["password"], "")
        self.assertTrue(state["secret_flags"]["common.password"])
        self.console.log("ERROR", "test-password secret-key one-token 12345678901")
        message = self.console.logs[-1]["message"]
        self.assertNotIn("test-password", message)
        self.assertNotIn("secret-key", message)
        self.assertNotIn("one-token", message)
        self.assertNotIn("12345678901", message)
        restarted = Console(self.folder)
        self.assertEqual(restarted.settings["common"]["username"], "12345678901")
        self.assertEqual(restarted.settings["common"]["password"], "")
        for invalid in ({"common": {"speed": 3}}, {"common": {"jobs": 1.5}}, {"common": {"use_cookies": "false"}}, {"tiku": {"provider": "os"}}, {"tiku": {"cover_rate": float("nan")}}, {"common": {"password": False}}, {"unknown": {}}, []):
            self.assertEqual(self.post("/api/settings", invalid).status_code, 400, str(invalid))

    def test_login_selection_preview_and_stop(self):
        self.assertEqual(self.post("/api/action", {"action": "courses"}).status_code, 400)
        self.login()
        self.assertEqual(len(self.console.courses), 4)
        for keys in ([], ["not-a-course"], "100001"):
            self.assertEqual(self.post("/api/action", {"action": "run", "keys": keys}).status_code, 400)
        key = self.console.courses[0]["key"]
        self.assertEqual(self.post("/api/action", {"action": "chapters", "keys": [key]}).status_code, 202)
        self.wait_idle()
        self.assertEqual(len(self.console.chapters[key]), 6)
        self.assertEqual(self.post("/api/action", {"action": "run", "keys": [key]}).status_code, 202)
        self.assertEqual(self.post("/api/action", {"action": "courses"}).status_code, 400)
        self.assertEqual(self.post("/api/settings", {"common": {"jobs": 1}}).status_code, 400)
        self.assertEqual(self.post("/api/logout", {}).status_code, 400)
        time.sleep(0.1)
        self.assertEqual(self.post("/api/stop", {}).status_code, 200)
        self.wait_idle()
        self.assertEqual(self.console.state["status"], "stopped")

    def test_demo_completion_progress_and_logout(self):
        self.login()
        key = self.console.courses[1]["key"]
        self.post("/api/action", {"action": "run", "keys": [key]})
        self.wait_idle(limit=10)
        self.assertEqual(self.console.state["status"], "completed")
        self.assertEqual(self.console.state["progress"], {"total": 6, "done": 6, "success": 6, "failed": 0, "skipped": 0})
        self.console.settings["common"]["password"] = "password-to-clear"
        (self.folder / "cookies.txt").write_text("_uid=fake-cookie")
        self.assertEqual(self.post("/api/logout", {}).status_code, 200)
        self.assertFalse(self.console.authenticated)
        self.assertEqual(self.console.settings["common"]["password"], "")
        self.assertFalse((self.folder / "cookies.txt").exists())
        self.assertEqual(self.console.courses, [])

    def test_account_changes_invalidate_loaded_courses(self):
        self.login()
        self.post("/api/settings", {"common": {"username": "different-account"}})
        self.assertFalse(self.console.authenticated)
        self.assertEqual(self.console.courses, [])

    def test_optional_providers_require_credentials_only_for_running(self):
        settings = copy.deepcopy(DEFAULTS)
        validate_run_settings(settings)
        settings["tiku"]["provider"] = "AI"
        with self.assertRaisesRegex(ValueError, "endpoint"):
            validate_run_settings(settings)
        settings["tiku"].update(endpoint="https://example.test/v1", key="test-key", model="test-model")
        validate_run_settings(settings)
        settings["notification"]["provider"] = "Telegram"
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_run_settings(settings)
        settings["notification"]["url"] = "https://example.test/sendMessage"
        with self.assertRaisesRegex(ValueError, "Chat ID"):
            validate_run_settings(settings)


class WorkerTest(unittest.TestCase):
    def test_cookie_values_can_contain_equals_and_empty_items(self):
        with patch.dict(os.environ, {"CHAOXING_GUI_WORKER": "1"}):
            from api.cookies import use_cookies
        with test_workspace() as folder:
            path = folder / "cookies.txt"
            path.write_text("_uid=test; token=abc==; ; malformed; other=x=y;", encoding="utf-8")
            with patch("api.cookies.gc.COOKIES_PATH", str(path)):
                self.assertEqual(use_cookies(), {"_uid": "test", "token": "abc==", "other": "x=y"})
                path.write_text("")
                self.assertEqual(use_cookies(), {})

    def test_real_adapter_retries_and_reports_partial(self):
        import gui_worker
        with patch.dict(os.environ, {"CHAOXING_GUI_WORKER": "1"}):
            from main import ChapterResult
        events = []
        with test_workspace() as folder:
            Path(folder, "cookies.txt").write_text("_uid=test")
            course = {"key": "one", "courseId": "1", "clazzId": "2", "cpi": "3", "title": "Test course"}
            points = [{"id": key, "title": key, "has_finished": False} for key in ("success", "closed", "retry", "exception")]
            calls = {}

            def chapter(cx, course, point, speed):
                key = point["id"]
                calls[key] = calls.get(key, 0) + 1
                if key == "exception":
                    raise RuntimeError("simulated failure")
                return {"success": ChapterResult.SUCCESS, "closed": ChapterResult.NOT_OPEN, "retry": ChapterResult.ERROR}[key]

            settings = copy.deepcopy(DEFAULTS)
            with patch("api.base.Chaoxing") as cx, patch("main.process_chapter", side_effect=chapter), patch("gui_worker.time.sleep"), patch("gui_worker.emit", side_effect=lambda kind, **kw: events.append({"type": kind, **kw})):
                cx.return_value.login.return_value = {"status": True}
                cx.return_value.get_course_point.return_value = {"points": points}
                with patch.dict(os.environ):
                    gui_worker.run({"demo": False, "action": "run", "data_dir": str(folder), "courses": [course], "settings": settings})
            self.assertEqual(calls["retry"], 3)
            self.assertEqual(calls["closed"], 1)
            self.assertEqual([e for e in events if e["type"] == "progress"][-1]["progress"], {"total": 4, "done": 4, "success": 1, "skipped": 1, "failed": 2})
            self.assertEqual(events[-1]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
