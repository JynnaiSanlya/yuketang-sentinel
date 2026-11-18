# -*- coding: utf-8 -*-
"""ykt_sentinel 离线单元测试：模拟雨课堂协议响应与 LLM，不联网。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("ykt_sentinel", ROOT / "ykt_sentinel.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return bool(pred())


PRESENTATION = {
    "slides": [
        {"id": 1},
        {"id": 2, "problem": {
            "problemId": "p1", "problemType": 1,
            "body": "<p>中国的首都是哪里？</p>",
            "options": [{"key": "A", "value": "上海"},
                        {"key": "B", "value": "<b>北京</b>"}],
        }},
        {"id": 3, "problem": {
            "problemid": "p2", "problemType": 4,
            "content": "1+1=？",
            "blanks": [{"type": 1}, {"type": 1}],
        }},
    ],
}


class FakeClient(mod.YktClient):
    def __init__(self):
        super().__init__("https://changjiang.yuketang.cn", "sess", timeout=1)
        self.presentation_calls: list[str] = []

    def _get(self, path, *, params=None, bearer=None, browser_ua=False, timeout=None):
        if path == "/v/course_meta/user_info":
            return {"code": 0, "data": {"user_profile": {
                "user_id": 42, "name": "张三", "school": "测试大学", "phone_number": "138x"}}}
        if path == "/api/v3/classroom/on-lesson-upcoming-exam":
            return {"code": 0, "data": {
                "onLessonClassrooms": [{"lessonId": 123, "courseName": "测试课程", "courseId": 9}],
                "upcomingExam": [{"examId": "e1", "title": "期中"}]}}
        if path == "/api/v3/lesson/presentation/fetch":
            self.presentation_calls.append(str(params.get("presentation_id")))
            return {"code": 0, "data": PRESENTATION}
        if path == "/v/course_meta/learning_list/":
            return {"code": 0, "data": [
                {"course_name": "测试课程", "classroom_name": "2026秋",
                 "teacher": {"name": "李四"}}]}
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, path, body, *, timeout=None):
        class Resp:
            headers = {"set-auth": "JWT"}
        return Resp(), {"code": 0, "data": {"lessonToken": "LT", "identityId": "ID1"}}


class FakeNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def send(self, title, body, sound=False, push=True):
        self.messages.append((title, str(body)))


class FakeSocket:
    def __init__(self):
        self.stop_event = threading.Event()
        self.sent: list[dict] = []

    def send(self, payload):
        self.sent.append(payload)
        return True

    def stop(self):
        self.stop_event.set()

    def run(self):
        pass


def make_monitor(solver=None, client=None):
    client = client or FakeClient()
    notifier = FakeNotifier()
    cfg = {"poll_interval_seconds": 5, "llm": {"base_url": "", "api_key": "", "model": ""}}
    monitor = mod.LessonMonitor(
        client, {"lesson_id": "123", "course_name": "测试课程"},
        notifier, cfg, solver=solver, socket_factory=lambda hello, on_event, on_dead: FakeSocket())
    return client, notifier, monitor


class TestFormatting(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(mod.strip_html("<p>中国&nbsp;北京</p>"), "中国 北京")

    def test_problem_options_shapes(self):
        p1 = {"options": ["上海", "北京"]}
        self.assertEqual(mod.problem_options(p1), [("A", "上海"), ("B", "北京")])
        p2 = {"options": [{"key": "A", "value": "x"},
                          {"key": "B", "value": "<b>y</b>"}]}
        self.assertEqual(mod.problem_options(p2), [("A", "x"), ("B", "y")])

    def test_problem_key_fallback(self):
        self.assertEqual(mod.problem_key({"problemid": 7}), "7")
        self.assertEqual(mod.problem_key({"prob": "abc"}), "abc")

    def test_describe_limit(self):
        self.assertEqual(mod.describe_limit(90), "限时90秒")
        self.assertEqual(mod.describe_limit("60"), "限时60秒")
        self.assertEqual(mod.describe_limit(-1), "不限时")
        self.assertEqual(mod.describe_limit(None), "不限时")

    def test_format_problem_text(self):
        problem = PRESENTATION["slides"][1]["problem"]
        text = mod.format_problem_text(problem)
        self.assertIn("【单选题】", text)
        self.assertIn("中国的首都是哪里", text)
        self.assertIn("B. 北京", text)

    def test_format_blank_and_image(self):
        problem = {"problemType": 4, "body": "填空", "blanks": [{}, {}],
                   "img_url": "http://x/y.png"}
        text = mod.format_problem_text(problem)
        self.assertIn("共 2 空", text)
        self.assertIn("http://x/y.png", text)

    def test_build_prompt(self):
        problem = PRESENTATION["slides"][1]["problem"]
        prompt = mod.build_prompt(problem)
        self.assertIn("单选题", prompt)
        self.assertIn("A. 上海", prompt)
        self.assertIn("只输出正确选项的字母", prompt)

    def test_extract_answer_line(self):
        self.assertEqual(mod.extract_answer_line("B", 1), "B")
        self.assertEqual(mod.extract_answer_line("B. 北京", 1), "B")
        self.assertEqual(mod.extract_answer_line("ABD", 2), "ABD")
        self.assertEqual(mod.extract_answer_line("答案是 C", 1), "C")
        self.assertEqual(mod.extract_answer_line("b", 1), "B")
        self.assertEqual(mod.extract_answer_line("北京\n理由：首都", 4), "北京")
        self.assertEqual(mod.extract_answer_line("正确", 6), "正确")


class TestSolve(unittest.TestCase):
    def test_uses_reference_answer_when_llm_missing(self):
        problem = dict(PRESENTATION["slides"][1]["problem"], answers=["C"])
        result = mod.solve_problem({"base_url": "", "api_key": ""}, problem)
        self.assertEqual(result["answer"], "C")
        self.assertEqual(result["source"], "课件")

    def test_llm_answer_with_reference(self):
        orig = mod.call_llm
        try:
            mod.call_llm = lambda cfg, prompt, image_url="": "B\n理由：北京是首都。"
            problem = dict(PRESENTATION["slides"][1]["problem"], answers=["B"])
            result = mod.solve_problem({"base_url": "x", "api_key": "k", "model": "m"}, problem)
            self.assertTrue(result["answer"].startswith("B"))
            self.assertIn("北京是首都", result["detail"])
        finally:
            mod.call_llm = orig

    def test_llm_failure_without_reference(self):
        result = mod.solve_problem({"base_url": "", "api_key": ""},
                                   PRESENTATION["slides"][1]["problem"])
        self.assertEqual(result["answer"], "")
        self.assertIn("AI 解答失败", result["detail"])


class TestClient(unittest.TestCase):
    def test_user_info_and_on_lesson(self):
        client = FakeClient()
        me = client.user_info()
        self.assertEqual(me["name"], "张三")
        lessons, exams = client.on_lesson()
        self.assertEqual(lessons[0]["lesson_id"], "123")
        self.assertEqual(lessons[0]["course_name"], "测试课程")
        self.assertEqual(exams[0]["examId"], "e1")

    def test_unauthenticated_raises(self):
        class Resp:
            status_code = 200
            def json(self):
                return {"code": 50000, "msg": "UNAUTHENTICATED", "data": ""}
        with self.assertRaises(mod.YktError) as ctx:
            mod.YktClient._decode("/x", Resp())
        self.assertEqual(ctx.exception.code, 50000)

    def test_checkin(self):
        client = FakeClient()
        sign = client.checkin("123")
        self.assertEqual(sign["bearer"], "JWT")
        self.assertEqual(sign["lesson_token"], "LT")
        self.assertEqual(sign["identity_id"], "ID1")


class TestMonitor(unittest.TestCase):
    def test_hello_caches_problems(self):
        client, notifier, monitor = make_monitor()
        monitor.handle_event("hello", {"op": "hello", "timeline": [
            {"type": "slide", "pres": "pres1"},
            {"type": "slide", "pres": "pres1"},
        ]})
        self.assertTrue(wait_until(lambda: set(monitor.problems) == {"p1", "p2"}))
        self.assertTrue(any("📖" in t for t, _ in notifier.messages))
        monitor.handle_event("hello", {"op": "hello", "timeline": [
            {"type": "slide", "pres": "pres1"}]})
        time.sleep(0.3)  # 无新课件不应重复拉取
        self.assertEqual(client.presentation_calls, ["pres1"])

    def test_unlock_problem_alerts_once(self):
        calls: list[str] = []

        def solver(problem):
            calls.append(mod.problem_key(problem))
            return {"answer": "B", "detail": "B\n理由：北京是首都。", "source": "AI"}

        client, notifier, monitor = make_monitor(solver=solver)
        monitor.handle_event("hello", {"op": "hello", "timeline": [
            {"type": "slide", "pres": "pres1"}]})
        self.assertTrue(wait_until(lambda: len(monitor.problems) == 2))

        monitor.handle_event("unlockproblem", {"problem": {"prob": "p1", "limit": 60}})
        self.assertTrue(wait_until(lambda: len(notifier.messages) >= 3))
        titles = [t for t, _ in notifier.messages]
        self.assertTrue(any("有新单选题" in t for t in titles), titles)
        answer_titles = [t for t in titles if "参考答案" in t]
        self.assertTrue(any("B" in t for t in answer_titles), titles)
        self.assertEqual(calls, ["p1"])

        # 推送 + 轮询重复到达只提醒一次
        monitor.handle_event("fetchtimeline", {"unlockedproblem": ["p1"]})
        monitor.handle_event("unlockproblem", {"problem": {"prob": "p1", "limit": 60}})
        time.sleep(0.4)
        self.assertEqual(len(notifier.messages), 3)
        self.assertEqual(calls, ["p1"])

    def test_fetchtimeline_backup_for_uncached_push(self):
        client, notifier, monitor = make_monitor(
            solver=lambda problem: {"answer": "11", "detail": "11", "source": "AI"})
        monitor.handle_event("hello", {"op": "hello", "timeline": [
            {"type": "slide", "pres": "pres1"}]})
        self.assertTrue(wait_until(lambda: len(monitor.problems) == 2))
        base = len(notifier.messages)
        monitor.handle_event("fetchtimeline", {"unlockedproblem": ["p2"]})
        self.assertTrue(wait_until(lambda: len(notifier.messages) >= base + 2))
        self.assertTrue(any("有新填空题" in t for t, _ in notifier.messages))

    def test_unknown_problem_still_alerts(self):
        client, notifier, monitor = make_monitor()
        monitor.handle_event("hello", {"op": "hello", "timeline": [
            {"type": "slide", "pres": "pres1"}]})
        self.assertTrue(wait_until(lambda: len(monitor.problems) == 2))
        monitor.handle_event("unlockproblem", {"problem": {"prob": "pX", "limit": 30}})
        self.assertTrue(wait_until(
            lambda: any("题面暂未获取到" in b for _, b in notifier.messages)))

    def test_lesson_finished(self):
        client, notifier, monitor = make_monitor()
        monitor.handle_event("lessonfinished", {"event": {"code": "LESSON_FINISH"}})
        self.assertTrue(wait_until(
            lambda: any("课堂结束" in b for _, b in notifier.messages)))
        self.assertTrue(monitor.stop_event.is_set())


class TestSentinelScan(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _write_cfg(self, **extra):
        import json
        cfg = {"sessionid": "s", "scan_interval_seconds": 45, "course_filter": []}
        cfg.update(extra)
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_scan_starts_monitor_and_exam_alert(self):
        self._write_cfg()
        client = FakeClient()
        notifier = FakeNotifier()
        sentinel = mod.Sentinel(self.config_path)
        sentinel.build_client = lambda cfg=None: client
        sentinel.notifier = notifier
        started = []

        class StubMonitor:
            def __init__(self, client, lesson, notifier, cfg):
                self.lesson = lesson
                self.stop_event = threading.Event()
                started.append(self)

            def alive(self):
                return True

            def start(self):
                return threading.Thread(target=lambda: None, daemon=True)

        orig_monitor = mod.LessonMonitor
        mod.LessonMonitor = StubMonitor
        try:
            sentinel._scan_once()
        finally:
            mod.LessonMonitor = orig_monitor
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].lesson["lesson_id"], "123")
        self.assertTrue(any("考试提醒" in t for t, _ in notifier.messages))
        # 第二轮扫描：课堂已在监听、考试已提醒过，不应重复
        sentinel._scan_once()
        self.assertEqual(len(started), 1)
        self.assertEqual(sum(1 for t, _ in notifier.messages if "考试提醒" in t), 1)

    def test_course_filter(self):
        self._write_cfg(course_filter=["高等数学"])
        client = FakeClient()
        sentinel = mod.Sentinel(self.config_path)
        sentinel.build_client = lambda cfg=None: client
        sentinel.notifier = FakeNotifier()
        started = []

        class StubMonitor:
            def __init__(self, client, lesson, notifier, cfg):
                self.lesson = lesson
                self.stop_event = threading.Event()
                started.append(self)

            def alive(self):
                return False

            def start(self):
                return threading.Thread(target=lambda: None, daemon=True)

        orig_monitor = mod.LessonMonitor
        mod.LessonMonitor = StubMonitor
        try:
            sentinel._scan_once()
        finally:
            mod.LessonMonitor = orig_monitor
        self.assertEqual(len(started), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
