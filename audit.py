"""
稽核記錄（audit log）。

重點是「誰、什麼時候、對什麼資料做了什麼」，寫進 logs/ 資料夾底下的
每日 txt 檔案，關掉機器人也不會消失，方便事後查帳。

檔案：
  logs/audit-2026-09-18.txt    誰做了什麼（查帳用，人看得懂的格式）
  logs/error-2026-09-18.txt    錯誤的完整堆疊（除錯用）

兩個分開放，查帳時不會被一堆技術細節干擾。
"""
import os
import traceback
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
LOG_DIR = os.environ.get(
    "LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
)


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _timestamp() -> str:
    return datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S")


def _today() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def _write(filename: str, line: str):
    """寫一行進檔案，順便印到畫面上（本機執行時看得到即時狀況）。"""
    _ensure_dir()
    path = os.path.join(LOG_DIR, filename)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"⚠️ 寫入 log 失敗：{e}", flush=True)
    print(line, flush=True)


def audit(action: str, who: str = None, detail: str = None, where: str = None):
    """
    記錄一筆稽核事件。
    action：做了什麼（例如「結算寶物」「領取分潤」「新增角色」）
    who：誰做的（Discord 顯示名稱）
    detail：細節（例如「屠龍刀 賣出 3000，11 人平分，每人 272.73」）
    where：在哪個頻道
    """
    parts = [f"[{_timestamp()}]", action]
    if who:
        parts.append(f"｜操作者：{who}")
    if where:
        parts.append(f"｜頻道：{where}")
    if detail:
        parts.append(f"｜{detail}")
    _write(f"audit-{_today()}.txt", " ".join(parts))


def error(context: str, exc: Exception, who: str = None):
    """記錄錯誤，含完整堆疊，方便事後查是程式哪一行出問題。"""
    header = f"[{_timestamp()}] ❌ {context}"
    if who:
        header += f"｜操作者：{who}"
    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _write(f"error-{_today()}.txt", f"{header}\n{stack}{'-' * 60}")
    # 稽核檔也留一行摘要，這樣查帳時看得出「這個時間點有出過錯」
    _write(f"audit-{_today()}.txt", f"[{_timestamp()}] ❌ {context}｜{type(exc).__name__}: {exc}")


def system(message: str):
    """機器人啟動、關閉這類系統事件。"""
    _write(f"audit-{_today()}.txt", f"[{_timestamp()}] ⚙️ {message}")
