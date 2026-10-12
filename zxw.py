"""智学网成绩监控 —— 单文件，零依赖。

用法：
    python zxw.py

测试提醒：
    python zxw.py --test

首次运行会提示输入 Cookie 并保存到本地；收到 HTTP 400 时会提醒重新输入。
启动后会问一次轮询间隔（分钟），然后一直跑。有新成绩或成绩变化时打印。
按 Ctrl+C 退出。

测试用：python zxw.py --rounds 1   （只跑一轮就退出）
"""

import base64
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# ==========================================================================
# Cookie
# ==========================================================================
COOKIE = ""
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zxw_cookie.txt")

BASE = "https://www.zhixue.com"
UA = "Mozilla/5.0 (Windows NT 6.1; rv:2.0.1) Gecko/20100101 Firefox/4.0.1"

DEFAULT_INTERVAL_MINUTES = 10
LOOKBACK_YEARS = 2          # 往前看几个学年
PAGE_SIZE = 20              # 每学年最多看几场考试
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zxw_state.json")

# 颜色
GREEN, YELLOW, RED, GRAY, BOLD, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
)
USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def c(text, color):
    return color + text + RESET if USE_COLOR else text


def parse_cookie(raw):
    """规范化浏览器复制的 Cookie 请求头，并验证每个 cookie 项。"""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw or "\r" in raw or "\n" in raw:
        raise ValueError("Cookie 不能为空，也不能包含换行。")

    parts = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(f"无法解析 Cookie 项：{item}")
        parts.append(f"{name}={value.strip()}")
    if not parts:
        raise ValueError("没有找到有效的 Cookie 项。")
    return "; ".join(parts)


def save_cookie(cookie):
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie + "\n")


def prompt_cookie(reason=None):
    global COOKIE

    if reason:
        print(c(reason, RED))
    while True:
        try:
            raw = input("请粘贴新的 Cookie（可包含 Cookie: 前缀）：")
        except EOFError as exc:
            raise EOFError("无法读取 Cookie 输入。") from exc
        try:
            cookie = parse_cookie(raw)
        except ValueError as exc:
            print(c(f"  Cookie 格式无效：{exc}", YELLOW))
            continue
        save_cookie(cookie)
        COOKIE = cookie
        print(c(f"Cookie 已保存到 {COOKIE_FILE}", GREEN))
        return cookie


def initialize_cookie():
    global COOKIE

    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            saved_cookie = f.read()
    except FileNotFoundError:
        saved_cookie = ""

    if saved_cookie.strip():
        try:
            COOKIE = parse_cookie(saved_cookie)
            return
        except ValueError as exc:
            print(c(f"本地 Cookie 文件内容无效：{exc}", YELLOW))
    prompt_cookie("未找到可用的本地 Cookie，请输入 Cookie。" if not saved_cookie.strip() else
                 "请重新输入有效的 Cookie。")


# ==========================================================================
# 网络
# ==========================================================================
def http_get(path, params=None, headers=None, timeout=20):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    all_headers = {
        "User-Agent": UA,
        "Cookie": COOKIE,
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(url, headers=all_headers)
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            exc.close()
            prompt_cookie(
                f"{now()} 收到 HTTP 400，Cookie 可能已过期。"
                "请重新登录网站并输入新的 Cookie；输入后将自动重试请求。"
            )
            all_headers["Cookie"] = COOKIE
            req = urllib.request.Request(url, headers=all_headers)


def get_xtoken():
    guid = str(uuid.uuid4())
    ts = str(int(time.time() * 1000))
    token = hashlib.md5((guid + ts + "iflytek!@#123student").encode()).hexdigest()
    data = http_get(
        "/container/app/token/getToken",
        headers={
            "authbizcode": "0001",
            "authguid": guid,
            "authtimestamp": ts,
            "authtoken": token,
        },
    )
    return data.get("result", "")


def get_exams(xtoken):
    """返回最近几个学年的考试列表 [(examId, examName)]。"""
    years = http_get(
        "/zhixuebao/base/common/academicYear", headers={"XToken": xtoken}
    ).get("result") or []

    exams = []
    seen = set()
    for year in years[:LOOKBACK_YEARS]:
        try:
            data = http_get(
                "/zhixuebao/report/exam/getUserExamList",
                params={
                    "pageIndex": 1,
                    "pageSize": PAGE_SIZE,
                    "startSchoolYear": year.get("beginTime", ""),
                    "endSchoolYear": year.get("endTime", ""),
                },
                headers={"XToken": xtoken},
            )
        except Exception:
            continue
        result = data.get("result") or {}
        for item in result.get("examList", []) or []:
            exam_id = item.get("examId")
            if exam_id and exam_id not in seen:
                seen.add(exam_id)
                exams.append((exam_id, item.get("examName", "")))
    return exams


def get_scores(xtoken, exam_id):
    """返回 [(科目, 分数, 满分, 是否总分)]。"""
    data = http_get(
        "/zhixuebao/report/exam/getReportMain",
        params={"examId": exam_id},
        headers={"XToken": xtoken},
    )
    result = data.get("result")
    if not isinstance(result, dict):
        return []

    rows = []
    for paper in result.get("paperList", []) or []:
        rows.append((
            paper.get("subjectName", "?"),
            float(paper.get("userScore") or 0),
            float(paper.get("standardScore") or 0),
            False,
        ))
    total = result.get("totalScore")
    if isinstance(total, dict) and total.get("userScore") is not None:
        rows.append((
            total.get("subjectName") or "总分",
            float(total.get("userScore") or 0),
            float(total.get("standardScore") or 0),
            True,
        ))
    return rows


def fingerprint(rows):
    text = "|".join(f"{n}={s:g}/{m:g}" for n, s, m, _ in sorted(rows))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ==========================================================================
# 本地状态
# ==========================================================================
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"exams": {}}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(c(f"  保存状态失败：{exc}", YELLOW))


# ==========================================================================
# 输出
# ==========================================================================
def now():
    return datetime.now().strftime("%H:%M:%S")


def print_scores(exam_name, rows, tag):
    print()
    print(c("=" * 58, GRAY))
    print(c(f"  [{tag}] {exam_name}", GREEN if tag == "新成绩" else YELLOW))
    print(c("=" * 58, GRAY))
    for name, score, standard, is_total in rows:
        line = f"{name}: {score:g}" + (f"/{standard:g}" if standard else "")
        if is_total:
            print("  " + c(line, BOLD))
        else:
            print("  " + line)


def subject_names(rows):
    return {
        row[0]
        for row in (rows or [])
        if isinstance(row, (list, tuple))
        and len(row) >= 4
        and row[0]
        and not row[3]
    }


def alert_new_subjects(exam_name, subjects):
    if os.name != "nt":
        raise RuntimeError("新科目提醒仅支持 Windows。")

    import ctypes
    import winsound

    message = f"考试：{exam_name}\n新发现科目：\n" + "\n".join(
        f"  - {name}" for name in subjects
    )
    try:
        winsound.PlaySound(
            "SystemExclamation",
            winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_LOOP,
        )
    except RuntimeError as exc:
        print(c(f"  播放提醒铃声失败：{exc}", RED))

    try:
        result = ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "发现新科目！",
            0x00000030 | 0x00001000 | 0x00040000,
        )
        if result == 0:
            raise ctypes.WinError()
    finally:
        winsound.PlaySound(None, 0)


# ==========================================================================
# 主流程
# ==========================================================================
def ask_interval():
    while True:
        try:
            raw = input(f"轮询间隔（分钟，直接回车 = {DEFAULT_INTERVAL_MINUTES}）：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return DEFAULT_INTERVAL_MINUTES
        if not raw:
            return DEFAULT_INTERVAL_MINUTES
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        print("  请输入大于 0 的数字。")


def choose_exam(exams):
    if not exams:
        print(c("  没有找到可监控的考试。", YELLOW))
        return None

    print("\n可监控的考试：")
    for index, (_, exam_name) in enumerate(exams, 1):
        print(f"  {index}. {exam_name}")

    while True:
        try:
            raw = input("请输入要监控的考试序号：").strip()
        except EOFError:
            print()
            return None
        try:
            index = int(raw)
            if 1 <= index <= len(exams):
                return exams[index - 1]
        except ValueError:
            pass
        print(f"  请输入 1 到 {len(exams)} 之间的序号。")


def poll_once(state, first_run, selected_exam):
    xtoken = get_xtoken()
    exam_id, exam_name = selected_exam
    known = state.setdefault("exams", {})
    new_items = []

    try:
        rows = get_scores(xtoken, exam_id)
    except Exception as exc:
        print(c(f"  读取「{exam_name}」失败：{exc}", YELLOW))
        return
    if not rows:
        print(f"{c(now(), GRAY)} 「{exam_name}」暂无成绩")
        return

    fp = fingerprint(rows)
    old = known.get(exam_id)
    added_subjects = []

    if old is None:
        known[exam_id] = {"name": exam_name, "fp": fp, "rows": rows}
        new_items.append((exam_name, rows, "新成绩"))
    elif old.get("fp") != fp:
        added_subjects = sorted(subject_names(rows) - subject_names(old.get("rows", [])))
        known[exam_id] = {"name": exam_name, "fp": fp, "rows": rows}
        new_items.append((exam_name, rows, "成绩更新"))

    if first_run:
        print(c(f"  已记录「{exam_name}」作为基线，之后只提示该考试的新变化", GRAY))
        return

    if new_items:
        for exam_name, rows, tag in new_items:
            print_scores(exam_name, rows, tag)
        if added_subjects:
            print(c(f"\n  !!! 检测到新科目：{'、'.join(added_subjects)} !!!", RED + BOLD))
            alert_new_subjects(exam_name, added_subjects)
    else:
        print(f"{c(now(), GRAY)} 「{exam_name}」无更新")


def main():
    if "--test" in sys.argv:
        print("正在测试新科目提醒；关闭弹窗即可结束测试。")
        alert_new_subjects("提醒测试", ["语文（测试）", "数学（测试）"])
        return 0

    try:
        initialize_cookie()
    except (EOFError, OSError) as exc:
        print(c(f"无法读取或保存 Cookie：{exc}", RED))
        return 1

    print(c("=" * 58, GRAY))
    print(c("  智学网成绩监控", BOLD))
    print(c("=" * 58, GRAY))

    interval_minutes = ask_interval()
    interval_seconds = max(10.0, interval_minutes * 60)
    print(c(f"每 {interval_minutes:g} 分钟检查一次，按 Ctrl+C 退出", GRAY))

    try:
        exams = get_exams(get_xtoken())
    except urllib.error.HTTPError as exc:
        print(c(f"{now()} 请求失败：HTTP {exc.code}", RED))
        return 1
    except urllib.error.URLError as exc:
        print(c(f"{now()} 网络错误：{exc.reason}", RED))
        return 1
    except Exception as exc:
        print(c(f"{now()} 出错：{type(exc).__name__}: {exc}", RED))
        return 1

    selected_exam = choose_exam(exams)
    if selected_exam is None:
        return 1
    print(c(f"正在监控：{selected_exam[1]}", GREEN))

    # 可选：只跑固定轮数（方便测试）。python zxw_check.py --rounds 1
    max_rounds = 0
    if "--rounds" in sys.argv:
        try:
            max_rounds = int(sys.argv[sys.argv.index("--rounds") + 1])
        except (IndexError, ValueError):
            max_rounds = 0

    state = load_state()

    round_no = 0
    while True:
        round_no += 1
        first_run = selected_exam[0] not in state.get("exams", {})
        try:
            poll_once(state, first_run, selected_exam)
            save_state(state)
        except EOFError as exc:
            print(c(f"Cookie 更新未完成：{exc}", RED))
            return 1
        except urllib.error.HTTPError as exc:
            print(c(f"{now()} 请求失败：HTTP {exc.code}", RED))
        except urllib.error.URLError as exc:
            print(c(f"{now()} 网络错误：{exc.reason}", RED))
        except Exception as exc:
            print(c(f"{now()} 出错：{type(exc).__name__}: {exc}", RED))

        if max_rounds and round_no >= max_rounds:
            return 0

        print(c(f"{now()} 下次检查在 {interval_minutes:g} 分钟后", GRAY))
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print()
            print(c("已退出。", YELLOW))
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(c("已退出。", YELLOW))
