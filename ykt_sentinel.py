#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长江雨课堂 · 课堂习题哨兵 (ykt_sentinel)

监听当前正在进行的雨课堂课程；老师推送随堂习题时：
  (1) 立即弹 macOS 通知（可选 Bark / Server酱 / 企业微信 推到手机）；
  (2) 调用 OpenAI 兼容大模型接口生成参考答案，并再次提醒。

设计边界：只提醒、只解读。不代答、不提交任何答案——是否作答、答什么，
由本人在手机上决定。

协议事实依据（公开研究，见 README「参考」一节）：
  - 2008lmf/yuketang-helper  docs/PROTOCOL.md（MIT）
  - Chiangjiang-RainClassroom-Auto（Apache-2.0，长江站点实测）
    即 github.com/skh2945932142/Fuck-Yangtze-RainClassroom（fork 自 Chiu-xaH）

用法：
  python3 ykt_sentinel.py login     # 微信扫码登录，写回 config.json
  python3 ykt_sentinel.py whoami    # 校验登录态
  python3 ykt_sentinel.py lessons   # 看当前正在上的课
  python3 ykt_sentinel.py run       # 挂机监控（默认）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote

# 系统 LibreSSL 下 urllib3 会发不相关的 OpenSSL 警告，按消息文本提前降噪
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import requests

# ---------------------------------------------------------------------------
# 常量与默认配置
# ---------------------------------------------------------------------------

SERVERS = {
    "changjiang": "https://changjiang.yuketang.cn",
    "yuketang": "https://www.yuketang.cn",
    "pro": "https://pro.yuketang.cn",
    "huanghe": "https://huanghe.yuketang.cn",
}

# 长江实测：接口只认 Cookie 里的 sessionid；把 sessionid/x-csrftoken 放请求头反而导致
# UNAUTHENTICATED（2026-11-16 实测，与长江站点开源实现一致）。浏览器 UA 仅课件拉取需要。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

PROBLEM_TYPES = {
    1: "单选题",
    2: "多选题",
    3: "投票题",
    4: "填空题",
    5: "主观题",
    6: "判断题",
}

DEFAULT_CONFIG = {
    "server": "changjiang",
    "base_url": "",            # 留空则按 server 取；也可直接写完整站点
    "sessionid": "",           # 浏览器 Cookie 里的 sessionid，或用 login 命令获取
    "csrftoken": "",           # 可选，Cookie 里的 csrftoken
    "course_filter": [],       # 只监听课程名包含其中之一的课；空 = 全部
    "scan_interval_seconds": 45,
    "poll_interval_seconds": 12,
    "notify_mac": True,
    "notify_sound": "Glass",
    "copy_answer_clipboard": True,   # 答案生成后自动复制到剪贴板（iPhone 同 Apple ID 可直接粘贴）
    "push": {
        "bark_url": "",        # 如 https://api.day.app/你的key
        "serverchan_sendkey": "",
        "wecom_webhook": "",   # 企业微信群机器人 webhook
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "vision_model": "",    # 图片题用；留空则图片题只提醒不解读
        "timeout_seconds": 45,
        "subjective_max_chars": 60,
    },
}


def default_config_path() -> Path:
    env = os.environ.get("YKT_SENTINEL_CONFIG")
    return Path(env) if env else Path(__file__).resolve().parent / "config.json"


class YktError(RuntimeError):
    """雨课堂接口返回了非预期结果。"""

    def __init__(self, message: str, code: int | str | None = None):
        super().__init__(message)
        self.code = code


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_hms()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path) -> dict:
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"config.json 读取失败（{exc}），请检查 JSON 格式。") from None
    return deep_merge(DEFAULT_CONFIG, raw)


def save_config(path: Path, cfg: dict) -> None:
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    try:  # 含登录凭证，收紧权限
        path.chmod(0o600)
    except OSError:
        pass


def base_url_of(cfg: dict) -> str:
    url = (cfg.get("base_url") or "").strip()
    if url:
        return url.rstrip("/")
    return SERVERS.get(cfg.get("server", "changjiang"), SERVERS["changjiang"])


# ---------------------------------------------------------------------------
# 雨课堂 HTTP 客户端
# ---------------------------------------------------------------------------

class YktClient:
    """一个站点 + 一份 Cookie 的只读会话（checkin 除外）。"""

    def __init__(self, base_url: str, sessionid: str, csrftoken: str = "", timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        if sessionid:
            # 只用 Cookie 形式的 sessionid（见 BROWSER_UA 上方的实测说明）
            self.s.headers["Cookie"] = f"sessionid={sessionid}"

    # -- 底层 ---------------------------------------------------------------

    def _get(self, path: str, *, params=None, bearer: str | None = None,
             browser_ua: bool = False, timeout: float | None = None) -> dict:
        headers = {}
        if bearer:
            headers["authorization"] = f"Bearer {bearer}"
        if browser_ua:
            headers["user-agent"] = BROWSER_UA
        resp = self.s.get(self.base + path, params=params, headers=headers,
                          timeout=timeout or self.timeout, allow_redirects=False)
        return self._decode(path, resp)

    def _post(self, path: str, body: dict, *, timeout: float | None = None):
        resp = self.s.post(self.base + path, json=body, timeout=timeout or self.timeout,
                           allow_redirects=False)
        return resp, self._decode(path, resp)

    @staticmethod
    def _decode(path: str, resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            raise YktError(f"HTTP {resp.status_code}：{path}", code=resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise YktError(f"响应不是 JSON：{path}", code="badjson") from None
        if isinstance(data, dict) and (
            data.get("code") == 50000 or data.get("msg") == "UNAUTHENTICATED"
        ):
            raise YktError("登录态失效（UNAUTHENTICATED），请更新 sessionid", code=50000)
        return data if isinstance(data, dict) else {}

    # -- 接口 ---------------------------------------------------------------

    def user_info(self) -> dict:
        data = self._get("/v/course_meta/user_info")
        profile = (data.get("data") or {}).get("user_profile") or {}
        if not profile:
            raise YktError("取不到用户资料，sessionid 可能已失效", code="noprofile")
        return {
            "uid": str(profile.get("user_id") or ""),
            "name": profile.get("name") or "",
            "school": profile.get("school") or "",
            "phone": profile.get("phone_number") or "",
        }

    def on_lesson(self) -> tuple[list[dict], list[dict]]:
        """返回 (正在上课的课堂列表, 即将开始/进行中的考试列表)。"""
        data = self._get("/api/v3/classroom/on-lesson-upcoming-exam")
        payload = data.get("data") or {}
        lessons = []
        for row in payload.get("onLessonClassrooms") or []:
            lessons.append({
                "lesson_id": str(row.get("lessonId") or ""),
                "course_name": row.get("courseName") or "未命名课程",
                "course_id": str(row.get("courseId") or ""),
            })
        return lessons, list(payload.get("upcomingExam") or [])

    def courses(self) -> list[dict]:
        data = self._get("/v/course_meta/learning_list/")
        rows = data.get("data") or []
        out = []
        for row in rows:
            out.append({
                "course_name": row.get("course_name") or "",
                "classroom_name": row.get("classroom_name") or "",
                "teacher": (row.get("teacher") or {}).get("name") or "",
            })
        return out

    def checkin(self, lesson_id: str) -> dict:
        """进入课堂，换取 Bearer 与 lessonToken。"""
        resp, data = self._post(
            "/api/v3/lesson/checkin",
            {"source": 21, "lessonId": str(lesson_id), "joinIfNotIn": True},
        )
        code = data.get("code")
        if code not in (0, None):
            raise YktError(f"进课堂失败：{data.get('msg') or code}", code=code)
        payload = data.get("data") or {}
        return {
            "bearer": resp.headers.get("set-auth") or "",
            "lesson_token": payload.get("lessonToken") or "",
            "identity_id": str(payload.get("identityId") or ""),
        }

    def scan_join(self, qr_url: str) -> dict:
        """动态二维码：把扫到的链接交给服务端换取 lessonId 并进班。"""
        data = self._post("/api/v3/app/scan", {"url": qr_url})[1]
        if data.get("code") not in (0, None):
            raise YktError(f"扫码失败：{data.get('msg') or data.get('code')}", code=data.get("code"))
        lesson_id = str((data.get("data") or {}).get("value") or "")
        if not lesson_id:
            raise YktError("扫码结果里没有 lessonId", code="noscan")
        return {"lesson_id": lesson_id, "checkin": self.checkin(lesson_id)}

    def presentation(self, presentation_id: str, bearer: str) -> dict:
        """取课件（含全部随堂题）。学生端题面可能带参考答案字段，有则直接用。"""
        data = self._get(
            "/api/v3/lesson/presentation/fetch",
            params={"presentation_id": presentation_id},
            bearer=bearer,
            browser_ua=True,
            timeout=15.0,
        )
        return data.get("data") or {}

    # -- 扫码登录 -------------------------------------------------------------

    def login_pre_info(self) -> dict:
        data = self._get("/api/v3/user/login/pre-info")
        return data.get("data") or {}

    def login_poll(self, token: str, timeout: float = 40.0) -> dict:
        """轮询扫码结果；code=50001 表示未扫码/超时。挂起最长约 30s。"""
        resp = self.s.post(self.base + "/api/v3/user/login", json={"token": token},
                           timeout=timeout)
        data = self._decode("/api/v3/user/login", resp)
        code = data.get("code")
        if code == 0:
            return {
                "sessionid": self.s.cookies.get("sessionid") or "",
                "csrftoken": self.s.cookies.get("csrftoken") or "",
                "profile": (data.get("data") or {}),
            }
        raise YktError(str(data.get("msg") or f"扫码登录未完成（code={code}）"), code=code)


# ---------------------------------------------------------------------------
# 题面解析与格式化
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text) -> str:
    if not text:
        return ""
    clean = unescape(_TAG_RE.sub(" ", str(text)))
    return _WS_RE.sub(" ", clean).strip()


def problem_key(problem: dict) -> str:
    for key in ("problemId", "problemid", "prob", "id"):
        if problem.get(key) is not None:
            return str(problem[key])
    return ""


def problem_type_of(problem: dict) -> int:
    try:
        return int(problem.get("problemType", problem.get("type", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def problem_body(problem: dict) -> str:
    return strip_html(problem.get("body") or problem.get("content") or "")


def problem_options(problem: dict) -> list[tuple[str, str]]:
    """把各种形态的 options 归一成 [(字母, 文本)]。"""
    out: list[tuple[str, str]] = []
    for i, opt in enumerate(problem.get("options") or []):
        if isinstance(opt, dict):
            key = str(opt.get("key") or opt.get("label") or chr(65 + i))
            val = opt.get("value")
            if isinstance(val, dict):
                val = val.get("value") or val.get("text") or ""
            val = val or opt.get("content") or opt.get("text") or ""
            out.append((key, strip_html(str(val))))
        else:
            out.append((chr(65 + i), strip_html(str(opt))))
    return out


def problem_image(problem: dict) -> str:
    return str(problem.get("img_url") or problem.get("img") or problem.get("_slide_img") or "")


def _is_placeholder_body(problem: dict) -> bool:
    """当堂出题/题面印在 PPT 页面图里的题：结构化题干是占位文本（实测："题目内容"/选项 A、B）。"""
    return problem_body(problem) in ("", "题目内容")


def problem_blanks(problem: dict) -> list:
    return problem.get("blanks") or []


def problem_answers(problem: dict) -> list:
    return problem.get("answers") or []


def describe_limit(limit) -> str:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return "不限时"
    return "不限时" if limit in (-1, 0) else f"限时{limit}秒"


def format_problem_text(problem: dict) -> str:
    """终端展示 / 推送用的题面纯文本。"""
    tname = PROBLEM_TYPES.get(problem_type_of(problem), "题目")
    lines = [f"【{tname}】{problem_body(problem) or '（题干为空或为图片）'}"]
    options = problem_options(problem)
    if options:
        lines.extend(f"  {key}. {val}" for key, val in options)
    if problem_type_of(problem) == 4:
        blanks = problem_blanks(problem)
        if blanks:
            lines.append(f"  （共 {len(blanks)} 空）")
    img = problem_image(problem)
    if img:
        lines.append(f"  （题目含图片：{img}）")
    return "\n".join(lines)


def build_prompt(problem: dict, subjective_max_chars: int = 60) -> str:
    t = problem_type_of(problem)
    tname = PROBLEM_TYPES.get(t, "题目")
    lines = [f"这是一道课堂{tname}。", "", "题目：", problem_body(problem) or "（题干为空或为图片）"]
    options = problem_options(problem)
    if options:
        lines.append("")
        lines.append("选项：")
        lines.extend(f"{key}. {val}" for key, val in options)
    blanks = problem_blanks(problem)
    if t == 4 and blanks:
        lines.append(f"（共 {len(blanks)} 个空）")
    img = problem_image(problem)
    if img:
        lines.append(f"（题目附带图片：{img}）")
    lines.append("")
    if t == 1:
        lines.append("要求：只输出正确选项的字母。")
    elif t == 2:
        lines.append("要求：只输出全部正确选项的字母，按字母顺序连写，不要分隔符。")
    elif t == 3:
        lines.append("要求：输出你认为最合适的一个选项字母。")
    elif t == 4:
        lines.append(f"要求：按空顺序输出答案，每空一行，每空不超过 20 字（共 {max(len(blanks), 1)} 空）。")
    elif t == 5:
        lines.append(f"要求：用不超过 {subjective_max_chars} 字简要作答。")
    elif t == 6:
        lines.append("要求：只输出「正确」或「错误」。")
    else:
        lines.append("要求：直接给出答案，尽量简短。")
    lines.append("答案单独一行。之后另起一行，用一句话（不超过 40 字）说明理由。")
    return "\n".join(lines)


def extract_answer_line(text: str, ptype: int) -> str:
    """从模型输出中提取第一行的答案本体。"""
    if not text:
        return ""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if ptype in (1, 2, 3):
        # 只取第一个词元里的字母，避免把选项正文里的英文单词当成答案
        token = re.split(r"[\s.、,，。:：;；!！?？\"'（）()\[\]]", first, 1)[0]
        letters = re.findall(r"[A-Fa-f]", token)
        head = "".join(dict.fromkeys(l.upper() for l in letters))
        if head:
            return head
        letters = re.findall(r"(?<![A-Za-z])([A-Fa-f])(?![A-Za-z])", text[:120])
        return "".join(dict.fromkeys(l.upper() for l in letters))
    if ptype == 6:
        return "正确" if ("正确" in first and "错误" not in first) else ("错误" if "错误" in first else first[:20])
    return first[:120]


# ---------------------------------------------------------------------------
# 大模型调用（OpenAI 兼容 chat/completions）
# ---------------------------------------------------------------------------

def _image_data_url(url: str) -> str:
    """图片转 base64 data URL（带签名的课件图最稳妥）；失败则原样返回交给模型自己拉。"""
    if url.startswith("data:"):
        return url
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and resp.content:
            mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            return f"data:{mime};base64,{base64.b64encode(resp.content).decode()}"
    except requests.RequestException as exc:
        log(f"题目图片下载失败：{exc}")
    return url


def call_llm(llm_cfg: dict, prompt: str, image_url: str = "") -> str:
    base = (llm_cfg.get("base_url") or "").rstrip("/")
    key = llm_cfg.get("api_key") or ""
    if not base or not key:
        raise YktError("LLM 未配置（config.json → llm.base_url / llm.api_key）", code="nollm")
    model = llm_cfg.get("model") or "deepseek-chat"
    use_vision = bool(image_url) and bool(llm_cfg.get("vision_model"))
    if use_vision:
        model = llm_cfg["vision_model"]
    content: object = prompt
    if use_vision:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _image_data_url(image_url)}},
        ]
    resp = requests.post(
        f"{base}/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
            "stream": False,
        },
        headers={"Authorization": f"Bearer {key}"},
        timeout=float(llm_cfg.get("timeout_seconds") or 45),
    )
    if resp.status_code >= 400:
        raise YktError(f"LLM HTTP {resp.status_code}：{resp.text[:200]}", code=resp.status_code)
    data = resp.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise YktError(f"LLM 响应格式异常：{json.dumps(data, ensure_ascii=False)[:200]}",
                       code="badllm") from None


def solve_problem(llm_cfg: dict, problem: dict) -> dict:
    """返回 {answer, detail, source}；课件自带参考答案时优先用。"""
    ptype = problem_type_of(problem)
    refs = problem_answers(problem)
    ref_text = ""
    if refs:
        try:
            ref_text = ", ".join(str(r) for r in refs)
        except TypeError:
            ref_text = str(refs)
    answer = detail = source = ""
    try:
        prompt = build_prompt(problem, int(llm_cfg.get("subjective_max_chars") or 60))
        if _is_placeholder_body(problem) and problem.get("_slide_img"):
            prompt = ("这是一页课堂PPT的截图，上面有一道随堂题，题干和选项都印在图里。"
                      "请仔细读图：先完整读出题干和所有选项，再用一两句话说明理由；"
                      "最后一行严格按「最终答案：X」格式输出（X 为选项字母或「正确」「错误」）。")
        raw = call_llm(llm_cfg, prompt, image_url=problem_image(problem))
        # 读图题要求模型在末行写「最终答案：X」，以理由推断后的结论为准，避免首行抢答与理由打架
        m = re.search(r"最终答案[：:]\s*\**\s*([A-Fa-f]+|正确|错误)", raw)
        answer = m.group(1).upper() if m else extract_answer_line(raw, ptype)
        detail = raw
        source = "AI"
    except (YktError, requests.RequestException) as exc:
        detail = f"AI 解答失败：{exc}"
    if ref_text:
        if answer and ref_text.replace(" ", "") not in answer.replace(" ", ""):
            answer = f"{answer}（课件参考答案：{ref_text}）"
        else:
            answer = answer or ref_text
        source = source or "课件"
    return {"answer": answer, "detail": detail, "source": source}


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

def _osascript_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _copy_to_clipboard(text: str) -> None:
    """macOS 剪贴板；配合 iPhone 通用剪贴板，答案可直接在手机上粘贴。"""
    if platform.system() != "Darwin" or not text:
        return
    try:
        subprocess.run(["pbcopy"], input=str(text).encode("utf-8"), timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("答案已复制到剪贴板")
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"复制到剪贴板失败：{exc}")


class Notifier:
    """macOS 通知 + 可选手机推送；终端始终打印。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.notify_mac = bool(cfg.get("notify_mac", True)) and platform.system() == "Darwin"
        self.sound = cfg.get("notify_sound") or ""
        self.push = cfg.get("push") or {}

    def send(self, title: str, body: str, sound: bool = False, push: bool = True) -> None:
        body = str(body)
        log(f"{title} | {body}")
        if self.notify_mac:
            self._mac(title, body, sound)
        if push:
            self._push(title, body)

    def _mac(self, title: str, body: str, sound: bool) -> None:
        script = f'display notification "{_osascript_escape(body[:220])}" with title "{_osascript_escape(title)}"'
        if sound and self.sound:
            script += f' sound name "{_osascript_escape(self.sound)}"'
        try:
            subprocess.run(["osascript", "-e", script], timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"macOS 通知发送失败：{exc}")

    def _push(self, title: str, body: str) -> None:
        errors = []
        bark = (self.push.get("bark_url") or "").rstrip("/")
        if bark:
            try:
                requests.get(f"{bark}/{quote(title, safe='')}/{quote(body[:180], safe='')}",
                             timeout=5)
            except requests.RequestException as exc:
                errors.append(f"bark: {exc}")
        key = self.push.get("serverchan_sendkey") or ""
        if key:
            try:
                requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title[:32], "desp": body[:900]}, timeout=5)
            except requests.RequestException as exc:
                errors.append(f"serverchan: {exc}")
        hook = self.push.get("wecom_webhook") or ""
        if hook:
            try:
                requests.post(hook, json={"msgtype": "text",
                                          "text": {"content": f"{title}\n{body}"}}, timeout=5)
            except requests.RequestException as exc:
                errors.append(f"wecom: {exc}")
        for err in errors:
            log(f"手机推送失败（{err}）")


# ---------------------------------------------------------------------------
# 课堂 WebSocket（websocket-client；事件分发）
# ---------------------------------------------------------------------------

class LessonSocket:
    """wss://<host>/wsapp/ 的最小封装：hello 认证、断线重连、事件回调。"""

    def __init__(self, base_url: str, hello: dict, on_event, on_dead):
        host = base_url.split("://", 1)[-1].rstrip("/")
        self.url = f"wss://{host}/wsapp/"
        self.hello = hello
        self.on_event = on_event
        self.on_dead = on_dead
        self.ws = None
        self.stop_event = threading.Event()
        self._send_lock = threading.Lock()

    def send(self, payload: dict) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:  # noqa: BLE001 - 连接层任何异常都不该带崩监听
            log(f"WS 发送失败：{exc!r}")
            return False

    def run(self) -> None:
        try:
            import websocket  # 延迟导入：login/whoami 等命令不需要它
        except ImportError:
            log("缺少 websocket-client 库：pip3 install --user websocket-client")
            self.on_dead()
            return

        retries = 0
        while not self.stop_event.is_set():
            app = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
            )
            self.ws = app
            try:
                app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                log(f"WS 连接异常：{exc!r}")
            self.ws = None
            if self.stop_event.is_set():
                break
            retries += 1
            if retries > 8:
                log("WS 连续重连失败，放弃本堂课")
                break
            backoff = min(2 ** retries, 30)
            log(f"{backoff}s 后重连 WS（第 {retries} 次）")
            self.stop_event.wait(backoff)
        self.on_dead()

    def stop(self) -> None:
        self.stop_event.set()
        ws = self.ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- 回调 ---------------------------------------------------------------

    def _on_open(self, ws) -> None:
        log(f"WS 已连接 {self.url}，发送 hello")
        self.send(self.hello)

    def _on_message(self, _ws, message: str) -> None:
        try:
            payload = json.loads(message)
        except ValueError:
            return
        op = str(payload.get("op") or "")
        if op == "hello" and payload.get("message") == "lesson finished":
            self.on_event("lessonfinished", payload)
            self.stop()
            return
        try:
            self.on_event(op, payload)
        except Exception as exc:  # noqa: BLE001 - 单个事件失败不能拖垮连接
            log(f"处理 {op or payload.get('op')} 事件失败：{exc!r}")

    def _on_error(self, _ws, error) -> None:
        log(f"WS 错误：{error!r}")


# ---------------------------------------------------------------------------
# 单堂课监听器
# ---------------------------------------------------------------------------

class LessonMonitor:
    """监听一堂正在进行的课：缓存课件题目、感知发题、提醒并解答。"""

    def __init__(self, client: YktClient, lesson: dict, notifier: Notifier, cfg: dict,
                 solver=None, socket_factory=None):
        self.client = client
        self.lesson = lesson
        self.lesson_id = lesson["lesson_id"]
        self.course = lesson["course_name"]
        self.notifier = notifier
        self.cfg = cfg
        self.solver = solver or (lambda problem: solve_problem(cfg.get("llm") or {}, problem))
        self.socket_factory = socket_factory or (
            lambda hello, on_event, on_dead: LessonSocket(base_url_of(cfg), hello, on_event, on_dead)
        )
        self.bearer = ""
        self.lesson_token = ""
        self.ws_userid = ""
        self.pres_ids: set[str] = set()
        self.problems: dict[str, dict] = {}
        self.alerted: set[str] = set()
        self.inflight: set[str] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.socket: LessonSocket | None = None
        self._finished = threading.Event()
        self.thread: threading.Thread | None = None

    # -- 生命周期 ------------------------------------------------------------

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self.run, name=f"ykt-{self.lesson_id}", daemon=True)
        self.thread.start()
        return self.thread

    def alive(self) -> bool:
        """进班失败的监听器线程会退出，扫描循环据此重建并重试进班（如动态二维码课
        程用户手机扫码后、服务端放行的场景）。"""
        return bool(self.thread and self.thread.is_alive())

    def run(self) -> None:
        try:
            sign = self.client.checkin(self.lesson_id)
        except YktError as exc:
            # 50000：session 失效，扫描循环会统一提醒；动态二维码课（DYNAMIC_QR_CHECK_IN_REFUSED）
            # 等用户手机扫码后，扫描循环会重建本监听器重试进班
            log(f"《{self.course}》进课堂失败：{exc}（下轮扫描重试；若为动态二维码课请先手机扫码）")
            self.stop_event.set()
            return
        except requests.RequestException as exc:
            log(f"《{self.course}》进课堂网络错误：{exc}")
            self.stop_event.set()
            return
        self.bearer = sign["bearer"]
        self.lesson_token = sign["lesson_token"]
        self.ws_userid = sign["identity_id"] or ""
        log(f"已进入课堂《{self.course}》lesson={self.lesson_id}"
            f"（Bearer {'OK' if self.bearer else '缺失'}）")
        hello = {
            "op": "hello",
            "userid": self.ws_userid,
            "role": "student",
            "auth": self.lesson_token,
            "lessonid": self.lesson_id,
        }
        self.socket = self.socket_factory(hello, self.handle_event, self._on_socket_dead)
        poller = threading.Thread(target=self._poll_loop, name=f"ykt-poll-{self.lesson_id}",
                                  daemon=True)
        poller.start()
        self.socket.run()
        self.stop_event.set()
        self._finished.set()

    def stop(self) -> None:
        self.stop_event.set()
        if self.socket:
            self.socket.stop()

    def _on_socket_dead(self) -> None:
        self.stop_event.set()

    # -- WS 事件 -------------------------------------------------------------

    def handle_event(self, op: str, payload: dict) -> None:
        if self.stop_event.is_set():
            return
        if op in ("hello", "showpresentation"):
            self._on_hello(payload)
        elif op == "unlockproblem":
            problem = payload.get("problem") or {}
            pid = str(problem.get("prob") or problem.get("problemid") or "")
            if pid:
                log(f"收到发题推送：{pid}（{describe_limit(problem.get('limit'))}）")
                self.handle_problem(pid, problem.get("limit"))
        elif op == "fetchtimeline":
            for pid in payload.get("unlockedproblem") or []:
                key = str(pid)
                if key in self.alerted or key in self.inflight:
                    continue
                if key in self.problems:
                    log(f"轮询发现已发布题目：{key}（推送可能丢失）")
                    self.handle_problem(key, None)
                else:
                    self._request_timeline()
                    break
        elif op in ("presentationupdated", "presentationcreated"):
            log(f"课件更新（{op}），重新拉取")
            self._request_timeline()
        elif op == "probleminfo":
            pid = str(payload.get("problemid") or "")
            if pid and pid not in self.alerted and pid in self.problems:
                log(f"题目 {pid} 状态：{describe_limit(payload.get('limit'))}")
        elif op == "lessonfinished":
            self._on_finished("下课信号")
        elif op in ("slide", "slidenav", "extendtime", "callpaused", "showfinished",
                    "locationcheckinext", "locationcheckinend"):
            pass
        else:
            log(f"WS 事件 {op or '?'}（未处理）")

    def _request_timeline(self) -> None:
        if self.socket:
            self.socket.send({"op": "fetchtimeline", "lessonid": str(self.lesson_id), "msgid": 1})

    def _on_hello(self, payload: dict) -> None:
        timeline = payload.get("timeline") or []
        pres_ids = {str(item.get("pres")) for item in timeline
                    if isinstance(item, dict) and item.get("type") == "slide" and item.get("pres")}
        if not pres_ids:
            return
        new_ids = pres_ids - self.pres_ids
        if not new_ids and self.problems:
            return
        self.pres_ids |= pres_ids
        threading.Thread(target=self._fetch_presentations, args=(new_ids,),
                         name=f"ykt-ppt-{self.lesson_id}", daemon=True).start()

    def _fetch_presentations(self, pres_ids: set[str]) -> None:
        found = 0
        for pres_id in sorted(pres_ids):
            if self.stop_event.is_set():
                return
            try:
                deck = self.client.presentation(pres_id, self.bearer)
            except (YktError, requests.RequestException) as exc:
                log(f"拉取课件 {pres_id} 失败：{exc}")
                continue
            slides = deck.get("slides") or []
            with self.lock:
                for slide in slides:
                    problem = slide.get("problem")
                    if isinstance(problem, dict):
                        pid = problem_key(problem)
                        if pid:
                            problem.setdefault("pres_id", pres_id)
                            slide_img = slide.get("cover") or slide.get("thumbnail") or ""
                            if slide_img:
                                problem.setdefault("_slide_img", str(slide_img))
                            self.problems[pid] = problem
                            found += 1
        total = len(self.problems)
        log(f"《{self.course}》课件含随堂题：本轮新增 {found}，累计 {total} 道")
        if found:
            self.notifier.send(
                f"📖 《{self.course}》",
                f"已进入课堂，课件共 {total} 道随堂题，发题时会立刻提醒你",
            )

    def _poll_loop(self) -> None:
        interval = int(self.cfg.get("poll_interval_seconds") or 12)
        while not self.stop_event.wait(max(interval, 5)):
            if self.socket and not self.socket.stop_event.is_set():
                self._request_timeline()

    # -- 发题处理 ------------------------------------------------------------

    def handle_problem(self, pid: str, limit) -> None:
        with self.lock:
            if pid in self.alerted or pid in self.inflight:
                return
            self.alerted.add(pid)   # 认领即去重，后续失败也不再重复提醒
            self.inflight.add(pid)
        threading.Thread(target=self._process_problem, args=(pid, limit),
                         name=f"ykt-q-{pid}", daemon=True).start()

    def _process_problem(self, pid: str, limit) -> None:
        try:
            problem = self.problems.get(pid)
            if problem is None:
                # 题面未缓存（临时出题/换课件），重拉一次
                self._fetch_presentations(set(self.pres_ids))
                problem = self.problems.get(pid)
            tname = PROBLEM_TYPES.get(problem_type_of(problem or {}), "题目")
            limit_text = describe_limit(limit)
            if problem is None:
                self.notifier.send(
                    f"🔔 《{self.course}》有新{tname}",
                    f"{limit_text}；题面暂未获取到（可能是当堂临时出题），请立刻打开手机作答",
                    sound=True,
                )
                return
            text = format_problem_text(problem)
            log(f"── 新题 {pid} ──────────────────\n{text}")
            self.notifier.send(
                f"🔔 《{self.course}》有新{tname}（{limit_text}）",
                (problem_body(problem) or "题目含图片，详情见终端")[:150],
                sound=True,
            )
            result = self.solver(problem)
            answer = result.get("answer") or ""
            detail = result.get("detail") or ""
            log(f"参考答案（{result.get('source') or 'AI'}）：{answer}\n{detail}")
            if answer:
                clipboard_note = ""
                if self.cfg.get("copy_answer_clipboard", True):
                    _copy_to_clipboard(answer)
                    clipboard_note = "（已复制到剪贴板，手机上可直接粘贴）"
                self.notifier.send(
                    f"✅ {tname}参考答案：{answer[:80]}",
                    ((detail.splitlines()[-1][:120] if detail else "") + clipboard_note)[:160],
                )
            else:
                self.notifier.send(
                    f"⚠️ {tname}AI 解答失败",
                    (detail or "请打开终端查看，或直接在手机上作答")[:150],
                )
        finally:
            with self.lock:
                self.inflight.discard(pid)

    def _on_finished(self, reason: str) -> None:
        if self._finished.is_set():
            return
        self._finished.set()
        self.stop_event.set()
        self.notifier.send(f"🎓 《{self.course}》", f"课堂结束（{reason}），哨兵退出本堂课")


# ---------------------------------------------------------------------------
# 哨兵主循环
# ---------------------------------------------------------------------------

class Sentinel:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.notifier = Notifier(self.cfg)
        self._notifier_key = self._notifier_signature(self.cfg)
        self.monitors: dict[str, LessonMonitor] = {}
        self.seen_exams: set[str] = set()
        self._last_auth_warn = 0.0
        self._stop = threading.Event()

    @staticmethod
    def _notifier_signature(cfg: dict) -> str:
        return json.dumps({
            "notify_mac": bool(cfg.get("notify_mac", True)),
            "notify_sound": cfg.get("notify_sound") or "",
            "push": cfg.get("push") or {},
        }, sort_keys=True, ensure_ascii=False)

    # -- 登录态 --------------------------------------------------------------

    def build_client(self, cfg: dict | None = None) -> YktClient:
        cfg = cfg or self.cfg
        client = YktClient(base_url_of(cfg), cfg.get("sessionid") or "",
                           cfg.get("csrftoken") or "")
        return client

    def warn_auth(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_auth_warn > 1800:
            self._last_auth_warn = now
            self.notifier.send(
                "🔑 雨课堂登录态失效",
                "请浏览器重新登录后，把新 sessionid 填入 config.json（正在监听的课堂不受影响）",
            )

    # -- 主循环 --------------------------------------------------------------

    def run(self) -> None:
        cfg = load_config(self.config_path)
        if not cfg.get("sessionid"):
            self.notifier.send("哨兵未配置", "请先 python3 ykt_sentinel.py login 或填写 config.json")
            return
        client = self.build_client(cfg)
        try:
            me = client.user_info()
        except (YktError, requests.RequestException) as exc:
            log(f"登录态校验失败：{exc}（仍会保持扫描，更新 config.json 后自动生效）")
            self.warn_auth(force=True)
            me = {"name": "?", "school": "?"}
        log(f"哨兵启动：{me.get('name')}（{me.get('school')}）@ {base_url_of(cfg)}")
        log("提示：只提醒、不代答；Ctrl+C 退出")

        while not self._stop.is_set():
            try:
                self._scan_once()
            except (YktError, requests.RequestException) as exc:
                if getattr(exc, "code", None) == 50000:
                    self.warn_auth()
                else:
                    log(f"扫描出错：{exc}")
            self._stop.wait(int(self.cfg.get("scan_interval_seconds") or 45))

    def _scan_once(self) -> None:
        self.cfg = load_config(self.config_path)   # 支持运行中热更新 sessionid
        sig = self._notifier_signature(self.cfg)
        if sig != self._notifier_key:
            self._notifier_key = sig
            self.notifier = Notifier(self.cfg)
        client = self.build_client(self.cfg)
        lessons, exams = client.on_lesson()
        filt = [s for s in (self.cfg.get("course_filter") or []) if s]
        for lesson in lessons:
            if filt and not any(name in lesson["course_name"] for name in filt):
                continue
            lid = lesson["lesson_id"]
            monitor = self.monitors.get(lid)
            if monitor and (monitor.stop_event.is_set() or not monitor.alive()):
                self.monitors.pop(lid, None)
                monitor = None
            if monitor is None:
                log(f"发现正在上课：《{lesson['course_name']}》lesson={lid}")
                monitor = LessonMonitor(client, lesson, self.notifier, self.cfg)
                self.monitors[lid] = monitor
                monitor.start()
        for exam in exams:
            key = str(exam.get("examId") or exam.get("id") or exam)
            if key not in self.seen_exams:
                self.seen_exams.add(key)
                summary = json.dumps(exam, ensure_ascii=False)
                self.notifier.send("📝 雨课堂考试提醒", summary[:200], sound=True)

    def stop(self) -> None:
        self._stop.set()
        for monitor in list(self.monitors.values()):
            monitor.stop()


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_login(args) -> int:
    cfg_path = args.config
    cfg = load_config(cfg_path)
    client = YktClient(base_url_of(cfg), cfg.get("sessionid") or "", cfg.get("csrftoken") or "")
    info = client.login_pre_info()
    token = info.get("token") or ""
    qr_content = info.get("qrContent") or ""
    if not token or not qr_content:
        log(f"取扫码票据失败：{info}")
        return 1
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_content)
        qr.print_ascii(invert=True)
    except ImportError:
        log("未安装 qrcode 库，无法在终端画二维码；请用微信打开下面的图片链接扫码：")
        log(info.get("qrImage") or qr_content)
    log("请用绑定雨课堂的微信扫码，并在手机上确认…")
    for attempt in range(3):
        try:
            result = client.login_poll(token)
        except (YktError, requests.RequestException) as exc:
            code = getattr(exc, "code", None)
            if code in (50001, None) or isinstance(exc, requests.RequestException):
                log(f"第 {attempt + 1}/3 次轮询未完成（{exc}），继续等待扫码…")
                continue
            log(f"扫码登录失败：{exc}")
            return 1
        cfg["sessionid"] = result["sessionid"]
        cfg["csrftoken"] = result["csrftoken"]
        save_config(cfg_path, cfg)
        log(f"登录成功，sessionid 已写回 {cfg_path}")
        return cmd_whoami(args)
    log("多次未扫码，退出。请重新执行 login。")
    return 1


def _make_client(cfg_path: Path) -> tuple[dict, YktClient]:
    cfg = load_config(cfg_path)
    if not cfg.get("sessionid"):
        raise SystemExit("config.json 里还没有 sessionid：先 python3 ykt_sentinel.py login，"
                         "或浏览器登录后把 Cookie 里的 sessionid 填进去。")
    return cfg, YktClient(base_url_of(cfg), cfg["sessionid"], cfg.get("csrftoken") or "")


def cmd_whoami(args) -> int:
    _, client = _make_client(args.config)
    me = client.user_info()
    log(f"当前用户：{me['name']}（{me['school']}）uid={me['uid']} phone={me['phone']}")
    return 0


def cmd_courses(args) -> int:
    _, client = _make_client(args.config)
    rows = client.courses()
    for row in rows:
        log(f"{row['course_name']} ｜ {row['classroom_name']} ｜ {row['teacher']}")
    if not rows:
        log("课程列表为空")
    return 0


def cmd_lessons(args) -> int:
    _, client = _make_client(args.config)
    lessons, exams = client.on_lesson()
    if not lessons:
        log("当前没有正在进行的课堂")
    for lesson in lessons:
        log(f"正在上课：《{lesson['course_name']}》lesson_id={lesson['lesson_id']}")
    for exam in exams:
        log(f"考试信息：{json.dumps(exam, ensure_ascii=False)[:200]}")
    return 0


def cmd_signin(args) -> int:
    """动态二维码课：用户把微信扫出来的链接粘进来，程序用同一账号接管监听。"""
    cfg, client = _make_client(args.config)
    if not args.url:
        raise SystemExit("用法：python3 ykt_sentinel.py signin --url '<微信扫课堂二维码得到的链接>'")
    result = client.scan_join(args.url)
    log(f"扫码进班成功：lesson={result['lesson_id']}（Bearer {'OK' if result['checkin']['bearer'] else '缺失'}）")
    return 0


def cmd_run(args) -> int:
    sentinel = Sentinel(args.config)
    try:
        sentinel.run()
    except KeyboardInterrupt:
        log("收到退出信号，正在停止…")
    finally:
        sentinel.stop()
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长江雨课堂课堂习题哨兵（提醒 + AI 参考答案，不代答）")
    parser.add_argument("--config", type=Path, default=default_config_path(),
                        help="配置文件路径（默认脚本同目录 config.json）")
    parser.add_argument("--url", default="", help="signin 用：微信扫课堂二维码得到的链接")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login", help="微信扫码登录并写回 config.json")
    sub.add_parser("whoami", help="校验登录态")
    sub.add_parser("courses", help="列出我的课程")
    sub.add_parser("lessons", help="查看正在进行的课堂")
    sub.add_parser("signin", help="动态二维码课：用微信扫出的链接进班")
    sub.add_parser("run", help="挂机监控（默认）")
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"
    return {"login": cmd_login, "whoami": cmd_whoami, "courses": cmd_courses,
            "lessons": cmd_lessons, "signin": cmd_signin, "run": cmd_run}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
