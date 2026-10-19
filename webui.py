"""Local browser UI. Run with `python webui.py` (Python 3.13+)."""

from __future__ import annotations

import argparse
import atexit
import copy
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent
SECRET_FIELDS = {"common": ("password",), "tiku": ("tokens", "key", "siliconflow_key"), "notification": ("url", "tg_chat_id")}
DEFAULTS = {
    "common": {"username": "", "password": "", "use_cookies": False, "speed": 1.0, "jobs": 2, "notopen_action": "continue"},
    "tiku": {"provider": "", "submit": "false", "cover_rate": 0.9, "delay": 1.0, "tokens": "", "url": "", "endpoint": "", "key": "", "model": "", "http_proxy": "", "min_interval_seconds": 3, "check_llm_connection": "true", "true_list": "正确,对,√,是", "false_list": "错误,错,×,否,不对,不正确", "likeapi_search": "false", "likeapi_vision": "true", "likeapi_model": "glm-4.5-air", "likeapi_retry": "true", "likeapi_retry_times": "3", "siliconflow_key": "", "siliconflow_model": "deepseek-ai/DeepSeek-V3", "siliconflow_endpoint": "https://api.siliconflow.cn/v1/chat/completions"},
    "notification": {"provider": "", "url": "", "tg_chat_id": ""},
}


def validate_settings(data, current):
    if not isinstance(data, dict):
        raise ValueError("设置必须为 JSON 对象")
    result = copy.deepcopy(current)
    for section, values in data.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            raise ValueError("设置分组无效")
        for key, value in values.items():
            if key not in DEFAULTS[section] or not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"不支持的设置：{key}")
            if isinstance(value, str) and len(value) > 8192:
                raise ValueError("设置内容过长")
            result[section][key] = value
    common, tiku, notice = (result[k] for k in DEFAULTS)
    for section, key, low, high, integer in [(common, "speed", 1, 2, False), (common, "jobs", 1, 8, True), (tiku, "cover_rate", 0, 1, False), (tiku, "delay", 0, 60, False), (tiku, "min_interval_seconds", 0, 120, True)]:
        value = section[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} 必须为数字")
        try:
            number = float(value)
        except (ValueError, TypeError):
            raise ValueError(f"{key} 必须为数字") from None
        if not math.isfinite(number) or not low <= number <= high or (integer and not number.is_integer()):
            raise ValueError(f"{key} 的范围为 {low}–{high}" + ("，且必须为整数" if integer else ""))
        section[key] = int(number) if integer else number
    if not isinstance(common["use_cookies"], bool):
        raise ValueError("Cookie 登录开关无效")
    if common["notopen_action"] not in ("retry", "continue"):
        raise ValueError("请选择重试或跳过关闭章节")
    if tiku["provider"] not in ("", "TikuYanxi", "TikuLike", "TikuAdapter", "AI", "SiliconFlow"):
        raise ValueError("不支持的题库")
    if notice["provider"] not in ("", "ServerChan", "Qmsg", "Bark", "Telegram"):
        raise ValueError("不支持的通知服务")
    for key in ("submit", "check_llm_connection", "likeapi_search", "likeapi_vision", "likeapi_retry"):
        if str(tiku[key]).lower() not in ("true", "false"):
            raise ValueError(f"{key} 必须为 true 或 false")
        tiku[key] = str(tiku[key]).lower()
    # All remaining values are text in the original INI contract.
    for section in (common, tiku, notice):
        for key, value in section.items():
            if key not in ("speed", "jobs", "cover_rate", "delay", "min_interval_seconds", "use_cookies") and not isinstance(value, str):
                raise ValueError(f"{key} 必须为文本")
    common["username"] = common["username"].strip()
    return result


def validate_run_settings(settings):
    tiku = settings["tiku"]
    required = {"TikuYanxi": ("tokens",), "TikuLike": ("tokens",), "TikuAdapter": ("url",), "AI": ("endpoint", "key", "model"), "SiliconFlow": ("siliconflow_key", "siliconflow_model", "siliconflow_endpoint")}
    for field in required.get(tiku["provider"], ()):
        if not tiku[field].strip():
            raise ValueError(f"请先补充题库配置：{field}，保存后再运行")
    if settings["notification"]["provider"]:
        if not settings["notification"]["url"].strip():
            raise ValueError("请填写通知服务 URL，或关闭外部通知")
        if settings["notification"]["provider"] == "Telegram" and not settings["notification"]["tg_chat_id"].strip():
            raise ValueError("请填写 Telegram Chat ID")


class Console:
    def __init__(self, data_dir=None, demo=False):
        self.data_dir = Path(data_dir or (ROOT / ".webui" / "demo" if demo else ROOT / ".webui"))
        self.demo = demo
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.settings = copy.deepcopy(DEFAULTS)
        self.courses = []
        self.chapters = {}
        self.authenticated = False
        self.process = None
        self.busy = False
        self.stopping = False
        self.logs = []
        self.sequence = 0
        self.state = {"status": "idle", "action": "", "message": "登录后即可读取课程", "started_at": None, "ended_at": None, "progress": {"total": 0, "done": 0, "success": 0, "failed": 0, "skipped": 0}, "current": ""}
        saved = self.data_dir / "settings.json"
        if saved.exists():
            try:
                self.settings = validate_settings(json.loads(saved.read_text(encoding="utf-8")), self.settings)
            except (ValueError, OSError):
                self.log("WARNING", "已忽略无法读取的界面设置，请重新保存。")

    def public_settings(self):
        result = copy.deepcopy(self.settings)
        flags = {}
        for section, fields in SECRET_FIELDS.items():
            for field in fields:
                flags[f"{section}.{field}"] = bool(result[section][field])
                result[section][field] = ""
        return result, flags

    def save(self, data):
        with self.lock:
            if self.busy:
                raise ValueError("请等待当前操作结束再修改设置")
            updated = validate_settings(data, self.settings)
            previous_account = (self.settings["common"]["username"], self.settings["common"]["password"], self.settings["common"]["use_cookies"])
            new_account = (updated["common"]["username"], updated["common"]["password"], updated["common"]["use_cookies"])
            persisted = copy.deepcopy(updated)
            for section, fields in SECRET_FIELDS.items():
                for field in fields:
                    persisted[section][field] = ""
            self.data_dir.mkdir(exist_ok=True, parents=True)
            tmp = self.data_dir / "settings.tmp"
            tmp.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.data_dir / "settings.json")
            self.settings = updated
            if previous_account != new_account:
                self.authenticated = False
                self.courses = []
                self.chapters = {}

    def redact(self, text):
        for section, fields in SECRET_FIELDS.items():
            for field in fields:
                value = str(self.settings[section][field])
                if value:
                    text = text.replace(value, "[已隐藏]")
                    if field == "tokens":
                        for token in value.split(","):
                            if token.strip():
                                text = text.replace(token.strip(), "[已隐藏]")
        username = self.settings["common"]["username"]
        if username:
            text = text.replace(username, "[账号]")
        return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)[:4000]

    def log(self, level, message):
        with self.lock:
            self.sequence += 1
            self.logs.append({"id": self.sequence, "time": time.time(), "level": level, "message": self.redact(str(message))})
            self.logs = self.logs[-1200:]

    def snapshot(self, after=0):
        with self.lock:
            settings, flags = self.public_settings()
            return {"demo": self.demo, "busy": self.busy, "authenticated": self.authenticated, "settings": settings, "secret_flags": flags, "state": copy.deepcopy(self.state), "courses": copy.deepcopy(self.courses), "chapters": copy.deepcopy(self.chapters), "logs": [x.copy() for x in self.logs if x["id"] > after], "cursor": self.sequence, "has_cookies": (self.data_dir / "cookies.txt").exists() or (ROOT / "cookies.txt").exists()}

    def start(self, action, keys=None):
        with self.lock:
            if self.busy:
                raise ValueError("已有操作正在执行，请等待完成或停止")
            if action not in ("login", "courses", "chapters", "run"):
                raise ValueError("未知操作")
            if action == "login":
                common = self.settings["common"]
                if not self.demo and not common["use_cookies"] and (not common["username"] or not common["password"]):
                    raise ValueError("请先填写账号和密码")
                self.authenticated = False
                self.courses = []
                self.chapters = {}
            elif not self.authenticated:
                raise ValueError("请先登录账号")
            if keys is not None and (not isinstance(keys, list) or not all(isinstance(k, str) for k in keys)):
                raise ValueError("课程选择格式无效")
            wanted = set(keys or [])
            selected = [c for c in self.courses if c["key"] in wanted]
            if action in ("run", "chapters") and (not selected or {c["key"] for c in selected} != wanted):
                raise ValueError("请勾选有效课程；不会自动处理全部课程")
            if action == "chapters" and len(selected) != 1:
                raise ValueError("每次只能查看一门课程的章节")
            if action == "run" and not self.demo:
                validate_run_settings(self.settings)
            self.state.update(status="running", action=action, message={"login": "正在验证账号", "courses": "正在读取课程列表", "chapters": "正在读取章节", "run": "正在准备所选课程"}[action], started_at=time.time(), ended_at=None, current="", progress={"total": 0, "done": 0, "success": 0, "failed": 0, "skipped": 0})
            self.busy = True
            self.stopping = False
            payload = {"action": action, "settings": copy.deepcopy(self.settings), "courses": selected, "data_dir": str(self.data_dir), "demo": self.demo}
            self.log("INFO", self.state["message"])
            threading.Thread(target=self._run, args=(payload,), daemon=True).start()

    def _event(self, event):
        kind = event.get("type")
        with self.lock:
            if kind == "log":
                self.log(event.get("level", "INFO"), event.get("message", ""))
            elif kind == "authenticated":
                self.authenticated = event.get("authenticated", True)
            elif kind == "courses":
                self.courses = event["courses"]
                self.chapters = {}
            elif kind == "chapters":
                self.chapters[event["key"]] = event["points"]
            elif kind == "progress":
                self.state["progress"] = event["progress"]
                self.state["current"] = event.get("current", "")
            elif kind == "result":
                self.state["status"] = event["status"]
                self.state["message"] = self.redact(event["message"])

    def _run(self, payload):
        proc = None
        try:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "gui_worker.py")], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with self.lock:
                self.process = proc
                if self.stopping:
                    proc.terminate()
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.close()
            # Read-only operations must not occupy the console forever.
            deadline = threading.Timer(180, self._timeout, args=(proc,)) if payload["action"] != "run" else None
            if deadline:
                deadline.daemon = True
                deadline.start()
            try:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("Not an event")
                        self._event(event)
                    except (ValueError, KeyError, TypeError):
                        self.log("INFO", line.strip())
                code = proc.wait()
            finally:
                if deadline:
                    deadline.cancel()
            with self.lock:
                if self.stopping:
                    self.state.update(status="stopped", message="操作已停止，可以重新选择课程运行")
                elif self.state["status"] == "running" or code != 0:
                    self.state.update(status="error", message="后台进程异常结束，请查看日志")
        except Exception as exc:
            self.log("ERROR", str(exc))
            with self.lock:
                self.state.update(status="stopped" if self.stopping else "error", message="操作已停止" if self.stopping else "后台启动失败，请查看日志")
        finally:
            if proc:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                for stream in (proc.stdout, proc.stdin):
                    if stream and not stream.closed:
                        stream.close()
            with self.lock:
                self.process = None
                self.busy = False
                self.state["ended_at"] = time.time()
                self.log("INFO", self.state["message"])

    def _timeout(self, proc):
        with self.lock:
            if self.process is proc and proc.poll() is None:
                self.log("ERROR", "请求超过 180 秒，已终止。请检查网络后重试。")
                proc.terminate()

    def stop(self):
        with self.lock:
            if self.busy:
                self.stopping = True
                self.state["message"] = "正在停止后台进程…"
                if self.process and self.process.poll() is None:
                    self.process.terminate()

    def logout(self):
        with self.lock:
            if self.busy:
                raise ValueError("请先停止当前操作")
            self.authenticated = False
            self.courses = []
            self.chapters = {}
            for section, fields in SECRET_FIELDS.items():
                for field in fields:
                    self.settings[section][field] = ""
            cookie_path = self.data_dir / "cookies.txt"
            cookie_path.unlink(missing_ok=True)
            self.state.update(status="idle", action="", message="已退出登录并清除界面会话凭据", started_at=None, ended_at=None, current="", progress={"total": 0, "done": 0, "success": 0, "failed": 0, "skipped": 0})
            self.logs = []


def create_app(data_dir=None, demo=False):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    console = Console(data_dir, demo)
    app.extensions["console"] = console

    @app.before_request
    def local_access():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return jsonify(error="只允许本机访问"), 403
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify(error="请求来源无效，请刷新本地页面"), 403
        if request.path.startswith("/api/") and not secrets.compare_digest(request.headers.get("X-Console-Token", ""), console.token):
            return jsonify(error="页面会话已更新，请刷新页面"), 403

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(413)
    def too_large(exc):
        return jsonify(error="请求内容过大"), 413

    @app.get("/")
    def index():
        return render_template("console.html", token=console.token)

    @app.get("/api/state")
    def state():
        try:
            after = max(0, int(request.args.get("after", 0)))
        except ValueError:
            raise ValueError("日志游标无效") from None
        return jsonify(console.snapshot(after))

    @app.post("/api/settings")
    def settings():
        console.save(request.get_json())
        return jsonify(ok=True)

    @app.post("/api/action")
    def action():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("操作格式无效")
        console.start(data.get("action"), data.get("keys"))
        return jsonify(ok=True), 202

    @app.post("/api/stop")
    def stop():
        console.stop()
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        console.logout()
        return jsonify(ok=True)

    atexit.register(console.stop)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="超星本地图形控制台")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="使用演示课程，不连接超星")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开默认浏览器")
    args = parser.parse_args()
    if sys.version_info < (3, 13):
        parser.error("请使用 Python 3.13 或更高版本")
    if not 1 <= args.port <= 65535:
        parser.error("端口范围为 1–65535")
    print(f"\n超星学习控制台：http://127.0.0.1:{args.port}" + (" （演示模式）" if args.demo else "") + "\n按 Ctrl+C 退出。\n")
    if args.open_browser:
        import webbrowser
        opener = threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{args.port}",))
        opener.daemon = True
        opener.start()
    create_app(demo=args.demo).run(host="127.0.0.1", port=args.port, threaded=True, debug=False, use_reloader=False)
