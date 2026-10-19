"""Isolated adapter for the existing API. JSON in via stdin, events via stdout."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import threading
import time

OUTPUT_LOCK = threading.Lock()


def emit(kind, **values):
    with OUTPUT_LOCK:
        print(json.dumps({"type": kind, **values}, ensure_ascii=False), flush=True)


def course_key(course):
    return ":".join(str(course.get(k, "")) for k in ("courseId", "clazzId", "cpi"))


def demo(payload):
    action = payload["action"]
    time.sleep(0.4)
    if action == "login":
        emit("authenticated")
    if action in ("login", "courses"):
        courses = []
        for i, (title, teacher, desc) in enumerate([("大学英语 · 综合教程", "张老师", "通识教育 / 2026 秋季"), ("Python 程序设计", "李老师", "专业基础 / 2026 秋季"), ("中国近现代史纲要", "王老师", "通识教育 / 2026 秋季"), ("高等数学（上）", "陈老师", "专业基础 / 2026 秋季")], 1):
            course = {"courseId": str(100000 + i), "clazzId": str(i), "cpi": "demo", "title": title, "teacher": teacher, "desc": desc}
            course["key"] = course_key(course)
            courses.append(course)
        emit("courses", courses=courses)
        emit("log", level="INFO", message="已载入 4 门演示课程。所有数据均为模拟，不会连接超星。")
    if action in ("chapters", "run"):
        points = [{"id": str(i), "title": f"第 {i} 节 · 演示章节", "jobCount": 2, "has_finished": i <= 2, "need_unlock": i == 6} for i in range(1, 7)]
        for course in payload["courses"]:
            emit("chapters", key=course["key"], points=points)
        if action == "run":
            total = 6 * len(payload["courses"])
            progress = {"total": total, "done": 0, "success": 0, "failed": 0, "skipped": 0}
            for course in payload["courses"]:
                for point in points:
                    emit("progress", progress=progress.copy(), current=course["title"] + " / " + point["title"])
                    time.sleep(0.8)
                    progress["done"] += 1
                    progress["success"] += 1
                    emit("log", level="INFO", message=f"[演示] 完成 {course['title']} / {point['title']}")
                    emit("progress", progress=progress.copy(), current=point["title"])
    emit("result", status="completed", message="演示操作已完成，未连接超星")


def run(payload):
    if payload.get("demo"):
        demo(payload)
        return

    # Import lazily so the web UI itself remains accessible on dependency errors.
    os.environ["CHAOXING_GUI_WORKER"] = "1"
    import requests
    from api.logger import logger
    logger.remove()
    logger.add(lambda msg: emit("log", level=msg.record["level"].name, message=msg.record["message"]), level="INFO", format="{message}", colorize=False, diagnose=False)
    from api.base import Account, Chaoxing
    from api.answer import Tiku
    from api.config import GlobalConst

    original_request = requests.Session.request

    def timed_request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (10, 40))
        return original_request(self, method, url, **kwargs)

    requests.Session.request = timed_request
    data_dir = Path(payload["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = data_dir / "cookies.txt"
    common = payload["settings"]["common"]
    action = payload["action"]
    if action == "login" and common["use_cookies"] and not cookie_file.exists():
        source = Path(__file__).resolve().parent / "cookies.txt"
        if not source.exists() or not source.read_text().strip():
            raise ValueError("未找到可用 cookies.txt，请先使用账号密码登录或准备 Cookie 文件")
        cookie_file.write_bytes(source.read_bytes())
    GlobalConst.COOKIES_PATH = str(cookie_file)

    tiku_conf = payload["settings"]["tiku"]
    if action != "run" or not tiku_conf["provider"]:
        # Use the current factory: Tiku is now an abstract class. An explicit
        # empty provider avoids falling back to the CLI's config.ini.
        tiku = Tiku.get_tiku_from_config({"provider": ""})
    else:
        tiku = Tiku.get_tiku_from_config(tiku_conf)
        tiku.init_tiku()
        if tiku_conf["provider"] in ("AI", "SiliconFlow") and tiku_conf["check_llm_connection"] == "true" and not tiku.check_llm_connection():
            raise ValueError("大模型连接检查失败，请检查题库设置后重试")

    cx = Chaoxing(account=Account(common["username"], common["password"]), tiku=tiku, query_delay=tiku_conf["delay"])
    # Only an explicit login may use the password; later actions use this UI's cookie.
    if action != "login":
        cx.account = Account("", "")
        if not cookie_file.exists():
            raise ValueError("登录会话不存在，请重新登录")
    login = cx.login(login_with_cookies=common["use_cookies"] if action == "login" else True)
    if not login.get("status"):
        emit("authenticated", authenticated=False)
        raise ValueError("登录失败：" + str(login.get("msg", "请检查账号或 Cookie")))
    emit("authenticated")

    if action in ("login", "courses"):
        courses = cx.get_course_list()
        unique = {}
        for course in courses:
            course["key"] = course_key(course)
            unique[course["key"]] = course
        emit("courses", courses=list(unique.values()))
        emit("result", status="completed", message=f"已读取 {len(unique)} 门课程")
        return

    plans = []
    for course in payload["courses"]:
        points = cx.get_course_point(course["courseId"], course["clazzId"], course["cpi"])["points"]
        emit("chapters", key=course["key"], points=points)
        plans.extend((course, point) for point in points)
    if action == "chapters":
        emit("result", status="completed", message=f"已读取 {len(plans)} 个章节")
        return
    if not plans:
        emit("log", level="WARNING", message="所选课程没有返回章节，请检查课程是否开放或平台接口是否有变化。")
        emit("result", status="partial", message="未读取到章节，本轮没有处理任务")
        return

    from main import ChapterResult, process_chapter
    progress = {"total": len(plans), "done": 0, "success": 0, "failed": 0, "skipped": 0}
    emit("progress", progress=progress.copy(), current="准备处理章节")

    def study(item):
        course, point = item
        title = course["title"] + " / " + point["title"]
        for attempt in range(3):
            result = process_chapter(cx, course, point, common["speed"])
            if result == ChapterResult.SUCCESS:
                return "success", title
            if result == ChapterResult.NOT_OPEN and common["notopen_action"] == "continue":
                logger.warning(f"章节未开放，已跳过：{title}")
                return "skipped", title
            if attempt < 2:
                logger.warning(f"章节暂未完成，正在重试 {attempt + 1}/2：{title}")
                time.sleep(2)
        logger.error(f"重试后仍未完成：{title}")
        return "failed", title

    with ThreadPoolExecutor(max_workers=common["jobs"]) as pool:
        futures = {pool.submit(study, item): item for item in plans}
        for future in as_completed(futures):
            try:
                outcome, title = future.result()
            except Exception as exc:
                course, point = futures[future]
                outcome, title = "failed", course["title"] + " / " + point["title"]
                logger.error(f"章节处理异常：{title}：{exc}")
            progress["done"] += 1
            progress[outcome] += 1
            logger.info(f"章节结果：{outcome} · {title}")
            emit("progress", progress=progress.copy(), current=title)
    status = "partial" if progress["failed"] or progress["skipped"] else "completed"
    message = f"本轮处理结束：成功 {progress['success']}，跳过 {progress['skipped']}，失败 {progress['failed']}"
    notice_conf = payload["settings"]["notification"]
    if notice_conf["provider"]:
        try:
            from api.notification import Notification
            notice = Notification()
            notice.config_set(notice_conf)
            notice = notice.get_notification_from_config()
            notice.init_notification()
            notice.send("chaoxing：" + message)
        except Exception as exc:
            logger.warning(f"任务处理已结束，但通知发送失败：{exc}")
    emit("result", status=status, message=message)


if __name__ == "__main__":
    try:
        run(json.loads(sys.stdin.readline()))
    except Exception as exc:
        emit("log", level="ERROR", message=f"{type(exc).__name__}: {exc}")
        emit("result", status="error", message="操作失败，请查看日志并检查配置")
