"""
Google Sheets 儲存層。
所有跟 gspread 的直接互動都集中在這裡，cogs 不直接碰 gspread。

工作表結構（欄位順序不能亂動，公式欄位機器人永遠不寫）：

角色資料：   A DiscordID(隱藏) B顯示名稱 C角色名稱 D職業 E位置
             F出席次數(公式) G分潤總額(公式) H色碼(公式)（依 Discord ID 排序）
場次記錄：   A場次ID(隱藏) B日期時間 C塔團 D DiscordID(隱藏) E DC名稱 F掉落 G寶物編號(隱藏)
             H類型 I來源/貢獻者 J售出金額 K均分$$ L已領 M領取時間 N同場首筆(公式)
             O色碼(公式) P發錢的人 Q同場角色首筆(公式)
職業管理：   A職業名稱 B轉職層級 C承接自 D圖片網址 E位置名稱(跟職業各自獨立管理)
帳號基本資料：A DiscordID(隱藏) B顯示名稱 C平日可出席 D假日可出席 E其他時間備註
             F出席次數(公式) G分潤總額(公式) H已領總額(公式) I待領總額(公式)

以下三張機器人第一次用到時會自動建立，不用手動建：
系統設定：   A頻道/討論串ID B允許指令(逗號分隔)（/setthreadrules、/setforumrules 的設定）
辨識頻道：   A頻道ID（/ocrchannel 開啟的頻道；完全沒設定時所有頻道的圖片都辨識）
系統狀態：   A項目 B內容（進行中的場次，機器人重啟後接回來用，請不要手動修改）
"""
import os
import re
import base64
import json
import asyncio
import time
from collections import Counter
from datetime import datetime, timezone

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from helpers import now_str

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_CHARACTERS = "角色資料"
SHEET_SESSIONS = "場次記錄"
SHEET_JOBS = "職業管理"
SHEET_ACCOUNTS = "帳號基本資料"

# 容易造成誤判的「形似字元」對照表：左邊會被視為跟右邊相同。
CONFUSABLE_CHAR_MAP = {
    "ㄚ": "丫", "ㄧ": "一", "ㄩ": "凵", "O": "0", "l": "1",
}


def normalize_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    for confusable, canonical in CONFUSABLE_CHAR_MAP.items():
        normalized = normalized.replace(confusable.lower(), canonical.lower())
    return normalized


def _sanitize(value):
    """
    防止公式注入（formula injection）：使用者輸入的文字如果開頭是
    = + - @ 這幾個會被 Google Sheets 當成公式起始符號的字元，
    用 USER_ENTERED 模式寫入時會被誤判成公式去執行（例如有人把角色名稱
    設成 =IMPORTXML(...) 之類的，可能造成資料外洩或試算表被搞亂）。
    這裡在前面加一個單引號，強制 Google Sheets 當成純文字處理，
    不影響其他一般文字/數字/布林值的寫入行為。
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    # 純數字的字串（Discord 帳號 ID、頻道 ID 都是 17～19 位數）也要當文字存。
    # 不然 Google 會把它當成數字，而試算表的數字只精確到 15 位，後面幾位會被改成 0，
    # ID 一改就對不上人。前面的單引號只是告訴 Google「這是文字」，儲存格裡不會顯示出來。
    # 真正的數字（int/float，例如金額）不受影響，照樣當數字存。
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return "'" + value
    return value


def _col_letter(n: int) -> str:
    """1 -> A, 2 -> B ... 只用在 row=1 的情況，所以可以簡單用 rowcol_to_a1 再去掉最後的 '1'。"""
    return gspread.utils.rowcol_to_a1(1, n)[:-1]


def _char_attendance_formula(row: int) -> str:
    # 用 Q 欄（角色層級的同場首筆），每隻角色各自累積出席次數
    return (f"=SUMIFS('{SHEET_SESSIONS}'!$Q:$Q,'{SHEET_SESSIONS}'!$D:$D,A{row},"
            f"'{SHEET_SESSIONS}'!$C:$C,C{row})")


def _char_earnings_formula(row: int) -> str:
    return (f"=SUMIFS('{SHEET_SESSIONS}'!$K:$K,'{SHEET_SESSIONS}'!$D:$D,A{row},"
            f"'{SHEET_SESSIONS}'!$C:$C,C{row},'{SHEET_SESSIONS}'!$H:$H,\"分潤\")")


def _char_color_formula(row: int) -> str:
    """
    角色資料 H 欄「色碼(輔助)」：跟上一列是同一個 Discord ID 就沿用同一個顏色，不同就換下一色（6 色循環）。
    配合依 Discord ID 排序，同一個人的角色會排在一起、顯示同一個顏色。
    「上一列」用 INDEX(範圍, 範圍列數) 取範圍的最後一格，刪列時範圍會自動縮小，不會變成 #REF!。
    """
    prev = row - 1

    def above(col: str) -> str:
        return f"INDEX(${col}$1:${col}{prev},ROWS(${col}$1:${col}{prev}))"

    return (f'=IF($A{row}="","",IF($A{row}={above("A")},{above("H")},'
            f'MOD(N({above("H")})+1,6)))')


def _account_attendance_formula(row: int) -> str:
    return f"=SUMIFS('{SHEET_SESSIONS}'!$N:$N,'{SHEET_SESSIONS}'!$D:$D,A{row})"


def _account_earnings_formula(row: int) -> str:
    return f"=SUMIFS('{SHEET_SESSIONS}'!$K:$K,'{SHEET_SESSIONS}'!$D:$D,A{row},'{SHEET_SESSIONS}'!$H:$H,\"分潤\")"


def _account_claimed_formula(row: int) -> str:
    return (f"=SUMIFS('{SHEET_SESSIONS}'!$K:$K,'{SHEET_SESSIONS}'!$D:$D,A{row},"
            f"'{SHEET_SESSIONS}'!$H:$H,\"分潤\",'{SHEET_SESSIONS}'!$L:$L,TRUE())")


def _account_pending_formula(row: int) -> str:
    return f"=G{row}-H{row}"


def _session_first_occurrence_formula(row: int) -> str:
    """
    帳號層級的「同場首筆」：同一場次＋同一個 Discord 帳號只算一次，
    給「帳號基本資料」的出席次數用（一個人一場只算出席一次，不管帶幾隻角色）。
    """
    return f'=IF($D{row}="","",IF(COUNTIFS($A$2:A{row},A{row},$D$2:D{row},D{row})=1,1,0))'


def _session_first_occurrence_by_char_formula(row: int) -> str:
    """
    角色層級的「同場首筆」：同一場次＋同一個角色名字只算一次，
    給「角色資料」的出席次數用（每隻角色各自累積自己的出席次數）。
    """
    return f'=IF($C{row}="","",IF(COUNTIFS($A$2:A{row},A{row},$C$2:C{row},C{row})=1,1,0))'


def _session_color_formula(row: int) -> str:
    """
    色碼(輔助)：同一場次+同一樣寶物（用場次ID+寶物編號當分組鍵）自動上同一個顏色，
    捐獻的寶物場次ID是空的，用「donation-列號」當獨立分組鍵，不會跟別的捐獻混在一起。
    這是鏈式公式（要回頭看前一列），跟前一列比對分組鍵是否相同，相同就沿用同一個顏色，
    不同就往下一個顏色輪替（MOD ...+1,6 是 6 色循環）。純視覺效果，不影響任何金額/出席次數計算。

    「前一列」不能直接寫成 O51 這種單一儲存格：第 51 列被刪掉，公式就會變成 #REF!。
    改用 INDEX($O$1:$O51, ROWS($O$1:$O51))，也就是「第 1 列到前一列這個範圍的最後一格」。
    範圍裡的列被刪掉時，試算表會自動把範圍縮小而不是報錯，所以永遠指到正上方那一列。
    """
    prev = row - 1

    def above(col: str) -> str:
        return f"INDEX(${col}$1:${col}{prev},ROWS(${col}$1:${col}{prev}))"

    return (
        f'=IF($F{row}="","",IF(IF($A{row}="","donation-"&ROW(),$A{row}&"|"&$G{row})'
        f'=IF({above("A")}="","donation-"&(ROW()-1),{above("A")}&"|"&{above("G")}),'
        f'{above("O")},MOD(N({above("O")})+1,6)))'
    )


def looks_corrupted_id(value) -> bool:
    """
    判斷一個 Discord ID 是不是被試算表改壞了（空白不算）。
    Discord ID 是 17～20 位數字；被試算表當成數字存的話，只會保留前 15 位、後面全部變成 0
    （例如 536582078557323265 → 536582078557323000），或是變成 5.36582E+17 這種格式。
    """
    text = str(value or "").strip()
    if not text:
        return False
    if not re.fullmatch(r"[0-9]{17,20}", text):
        return True
    return text.endswith("0" * (len(text) - 15))


def _to_number(value) -> float:
    """把金額欄的內容轉成數字（處理 70,000,000 這種有千分位逗號的顯示格式）；轉不了就當 0。"""
    try:
        return float(str(value or "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _date_part(value) -> str:
    """場次記錄「日期時間」欄只取日期部分，例如 2026/09/26 15:39:53 → 2026/09/26。"""
    text = str(value or "").strip()
    return text.split()[0] if text else ""


class DataIntegrityError(Exception):
    """排序或寫入公式之後，檢查發現資料不一致或公式出錯；或是欄位順序不對，為了安全停止寫入。"""


# 機器人寫入這三張表時是「照固定位置」寫（例如第 F 欄就是掉落）。如果有人插入或移動過欄位，
# 就會把資料寫進錯的欄，而且不會有任何錯誤。所以寫入前先確認這些欄的標題在正確的位置。
# 只檢查「程式本來就照標題名稱讀取」的欄：這些標題不對的話機器人早就不能用，拿來當基準不會誤判；
# 中間只要有任何一欄被插入或移動，至少會有一欄對不上。
_EXPECTED_LAYOUT = {
    # A～L 之間只要有任何一欄被插入或移動，A、D、F～H、J～L 至少會有一欄對不上；P 用來抓 M～P 之間的位移
    "場次記錄": {1: ("場次ID",), 4: ("Discord ID",), 6: ("掉落",), 7: ("寶物編號",), 8: ("類型",),
                 10: ("售出金額",), 11: ("均分$$",), 12: ("已領",), 16: ("發錢的人", "操作者"),
                 17: ("同場角色首筆*",)},   # 結尾的 * 代表只比對開頭，「同場角色首筆(輔助)」也算對
    "角色資料": {1: ("Discord ID",), 3: ("角色名稱",), 4: ("職業",), 5: ("位置",)},
    "帳號基本資料": {1: ("Discord ID",), 3: ("平日可出席",), 4: ("假日可出席",), 5: ("其他時間備註",)},
}
_LAYOUT_CACHE_SECONDS = 300


class _RetryHTTPClient(gspread.HTTPClient):
    """
    Google Sheets 偶爾會回 503（服務暫時無法使用）、500、429（呼叫太頻繁）、408（逾時）
    這類「過一下就好」的暫時性錯誤。這裡遇到時自動等 2、4、8、16 秒逐次重試（最多約 30 秒），
    Google 恢復就繼續執行，使用者不用手動重打指令；真的持續故障就放棄、把錯誤回報出來。

    不用 gspread 內建的 BackOffHTTPClient，是因為它的次數上限判斷有 bug
    （先把等待秒數壓在上限內、再檢查有沒有超過上限，永遠成立），
    Google 長時間故障時會無限重試、一直握著寫入鎖，把其他所有人的指令也卡死。
    """
    _RETRY_WAITS = (2, 4, 8, 16)

    @staticmethod
    def _is_transient(err: APIError) -> bool:
        code = err.code
        return code in (408, 429) or code >= 500

    def request(self, *args, **kwargs):
        for wait in self._RETRY_WAITS:
            try:
                return super().request(*args, **kwargs)
            except APIError as err:
                if not self._is_transient(err):
                    raise
                time.sleep(wait)
        return super().request(*args, **kwargs)  # 最後一次，再失敗就把錯誤丟出去


class SheetsStore:
    """所有 Google Sheets 讀寫都透過這個類別，方法都是同步的（gspread 本身是同步函式庫），
    cogs 呼叫時要自己包 asyncio.to_thread。"""

    def __init__(self):
        b64 = os.environ["GOOGLE_SERVICE_ACCOUNT_B64"]
        creds_dict = json.loads(base64.b64decode(b64))
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.client = gspread.authorize(creds, http_client=_RetryHTTPClient)
        self.sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.lock = asyncio.Lock()
        self._spreadsheet = None

    def _ss(self):
        if self._spreadsheet is None:
            self._spreadsheet = self.client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def ws(self, sheet_name: str):
        return self._ss().worksheet(sheet_name)

    # ---------- 欄位順序檢查（寫入前） ----------

    def check_layout(self, sheet_name: str):
        """
        確認這張表的欄位順序跟機器人預期的一樣，不一樣就丟出 DataIntegrityError、什麼都不寫。
        通過的結果暫存 5 分鐘，不會每次寫入都多讀一次；沒通過不暫存，修好之後馬上就能用。
        """
        expected = _EXPECTED_LAYOUT.get(sheet_name)
        if not expected:
            return
        cache = self.__dict__.setdefault("_layout_ok", {})
        if time.monotonic() - cache.get(sheet_name, -1e9) < _LAYOUT_CACHE_SECONDS:
            return
        headers = self.ws(sheet_name).row_values(1)
        problems = []
        for col, names in expected.items():
            actual = headers[col - 1].strip() if col <= len(headers) else ""
            if not any(actual.startswith(n[:-1]) if n.endswith("*") else actual == n for n in names):
                problems.append(f"第 {_col_letter(col)} 欄應該是「{names[0].rstrip('*')}」，現在是「{actual or '空白'}」")
        if problems:
            raise DataIntegrityError(
                f"「{sheet_name}」的欄位順序跟機器人預期的不一樣，為了避免把資料寫進錯的欄，已經停止寫入，"
                f"資料沒有被改動：\n" + "\n".join(problems)
                + "\n請把欄位移回原本的位置（或刪掉中間插入的欄）之後再試一次。"
            )
        cache[sheet_name] = time.monotonic()

    # ---------- 通用 row 操作 ----------

    def get_rows(self, sheet_name: str, key_col_index: int = 1) -> list:
        """
        回傳這張表所有「真的有資料」的列，每筆是 dict（含 _row 實際列號）。
        判斷「有沒有資料」只看 key_col_index 這一欄，不是看整列任何欄位有沒有內容——
        因為公式欄本來就會算出 0、打勾格本來就有格式，這些「非真正資料」的內容
        不該被誤判成「這列已經有資料」，否則會一路把整段預先格式化的空白列都當成占用中。
        場次記錄表因為捐獻列的場次ID（A欄）本來就允許留空，呼叫時要傳 key_col_index=2
        （日期時間欄），改用這欄判斷才不會漏掉捐獻列。
        """
        values = self.ws(sheet_name).get_all_values()
        if not values:
            return []
        headers = values[0]
        rows = []
        for i, row in enumerate(values[1:], start=2):
            key_cell = row[key_col_index - 1] if len(row) >= key_col_index else ""
            if not key_cell.strip():
                continue
            d = {headers[j]: (row[j] if j < len(row) else "") for j in range(len(headers))}
            d["_row"] = i
            rows.append(d)
        return rows

    def next_append_row(self, sheet_name: str, key_col_index: int = 1) -> int:
        """
        回傳「最後一筆資料的下一列」，批次寫入一律從這裡開始往下寫。
        不能去填中間的空白列：批次是一次寫一整塊連續的列（例如一場 12 人就寫 12 列），
        如果有人把中間幾列清空，從空白處開始寫會直接蓋掉下面原本的資料。
        """
        return len(self.ws(sheet_name).col_values(key_col_index)) + 1

    def _ensure_row_capacity(self, sheet_name: str, needed_row: int):
        """
        確保這張表的實際格線列數夠寫到 needed_row，不夠就自動用 API 幫它加列
        （多留 100 列緩衝，避免之後每加一筆資料就要呼叫一次擴充列數的 API）。
        """
        ws = self.ws(sheet_name)
        if needed_row > ws.row_count:
            ws.add_rows(needed_row - ws.row_count + 100)

    def write_row(self, sheet_name: str, row_number: int, values: list, start_col: int = 1, raw: bool = False):
        """
        把 values 依序寫進指定列，從 start_col 開始（1-indexed）。
        raw=False（預設）：自動防止公式注入，使用者輸入的文字不會被誤判成公式。
        raw=True：只在寫入「機器人自己產生的公式字串」時使用，跳過防注入處理。
        """
        self.check_layout(sheet_name)
        self._ensure_row_capacity(sheet_name, row_number)
        if not raw:
            values = [_sanitize(v) for v in values]
        start = f"{_col_letter(start_col)}{row_number}"
        end = f"{_col_letter(start_col + len(values) - 1)}{row_number}"
        self.ws(sheet_name).update(f"{start}:{end}", [values], value_input_option="USER_ENTERED")

    def append_rows_batch(self, sheet_name: str, values_list: list, start_col: int = 1, key_col_index: int = 1,
                           extra_formulas: list = None):
        """
        一次性把多列資料寫入（從第一個空白列開始，連續往下填），用「一次」API 呼叫寫完。
        extra_formulas: [(col_index, formula_fn), ...]，formula_fn(row_number) 回傳這一列該欄位的公式字串，
        確保機器人新增的每一列都自帶自己需要的公式，資料量再大也不會漏算。
        values_list 一律視為使用者輸入，自動防止公式注入；extra_formulas 是機器人自己產生的公式，不受影響。
        """
        self.check_layout(sheet_name)
        if not values_list:
            return None
        start_row = self.next_append_row(sheet_name, key_col_index=key_col_index)
        end_row = start_row + len(values_list) - 1
        self._ensure_row_capacity(sheet_name, end_row)
        sanitized = [[_sanitize(v) for v in row] for row in values_list]
        start_letter = _col_letter(start_col)
        end_letter = _col_letter(start_col + len(values_list[0]) - 1)
        self.ws(sheet_name).update(f"{start_letter}{start_row}:{end_letter}{end_row}", sanitized, value_input_option="USER_ENTERED")

        if extra_formulas:
            updates = []
            for r in range(start_row, end_row + 1):
                for col, formula_fn in extra_formulas:
                    updates.append((r, col, [formula_fn(r)]))
            self.batch_update_cells(sheet_name, updates, raw=True)

        return start_row

    def batch_update_cells(self, sheet_name: str, updates: list, raw: bool = False):
        """
        一次性更新多個（可能不連續的）位置。updates 是 [(row, start_col, values), ...]，
        用 gspread 的 batch_update 一次送出，避免每一列各打一次 API。
        raw=False（預設）：自動防止公式注入。raw=True：寫入機器人自己產生的公式時使用。
        """
        self.check_layout(sheet_name)
        if not updates:
            return
        self._ensure_row_capacity(sheet_name, max(row for row, _, _ in updates))
        data = []
        for row, start_col, values in updates:
            if not raw:
                values = [_sanitize(v) for v in values]
            start_letter = _col_letter(start_col)
            end_letter = _col_letter(start_col + len(values) - 1)
            data.append({"range": f"{start_letter}{row}:{end_letter}{row}", "values": [values]})
        self.ws(sheet_name).batch_update(data, value_input_option="USER_ENTERED")

    def update_cell(self, sheet_name: str, row: int, col: int, value):
        self.check_layout(sheet_name)
        letter = _col_letter(col)
        self.ws(sheet_name).update(f"{letter}{row}", [[_sanitize(value)]], value_input_option="USER_ENTERED")

    def delete_row(self, sheet_name: str, row_number: int):
        self.check_layout(sheet_name)
        self.ws(sheet_name).delete_rows(row_number)

    @staticmethod
    def _first_empty_row_from(rows: list) -> int:
        """
        從已經讀到的 rows（get_rows 的結果，每筆帶 _row）直接算出第一個空白列，
        不用再另外打一次 API 去問。仍然會正確找回被刪除留下的空缺列，不設任何上限。
        """
        if not rows:
            return 2
        occupied = {r["_row"] for r in rows}
        max_existing = max(occupied)
        for r in range(2, max_existing + 1):
            if r not in occupied:
                return r
        return max_existing + 1

    # ---------- 職業 ----------

    def get_jobs(self) -> dict:
        jobs = {}
        for r in self.get_rows(SHEET_JOBS):
            name = r.get("職業名稱", "").strip()
            if not name:
                continue
            try:
                tier = int(r.get("轉職層級") or 1)
            except ValueError:
                tier = 1
            parent = r.get("承接自", "").strip() or None
            image = r.get("圖片網址", "").strip()
            jobs[name] = {"tier": tier, "parent": parent, "image": image}
        return jobs

    def upsert_job(self, name: str, tier: int, parent: str, image: str) -> str:
        rows = self.get_rows(SHEET_JOBS)
        for r in rows:
            if r.get("職業名稱", "").strip() == name:
                final_image = image if image is not None else r.get("圖片網址", "")
                self.write_row(SHEET_JOBS, r["_row"], [name, tier, parent or "", final_image])
                return "updated"
        row_num = self._first_empty_row_from(rows)
        self.write_row(SHEET_JOBS, row_num, [name, tier, parent or "", image or ""])
        return "created"

    def delete_job(self, name: str):
        rows = self.get_rows(SHEET_JOBS)
        for r in rows:
            if r.get("職業名稱", "").strip() == name:
                self.delete_row(SHEET_JOBS, r["_row"])
                return True
        return False

    # ---------- 戰鬥位置清單（存在「職業管理」表的 E 欄，跟職業本身的 A-D 欄各自獨立） ----------
    # 用 E 欄存放，不能直接刪整列（會誤刪同一列的職業資料），刪除時只清空儲存格內容，
    # 中間留空缺沒關係，get_positions 只回傳非空白的值。

    POSITION_COL = 5  # E 欄

    def get_positions(self) -> list:
        col_values = self.ws(SHEET_JOBS).col_values(self.POSITION_COL)
        return [v.strip() for v in col_values[1:] if v.strip()]

    # ---------- 發錢的人清單（存在「職業管理」表的 F 欄，直接在試算表裡手動增刪） ----------
    # 賣出寶物（/sell、/loot、公告上的「賣掉寶物」）時，會從這裡讀出選單讓人選「這樣寶物由誰發錢」，
    # 選到的名字寫進「場次記錄」的 P 欄。F1 是標題，從 F2 往下一格填一個名字。

    PAYMASTER_COL = 6  # F 欄

    def get_paymasters_cached(self, seconds: int = 60) -> list:
        """跟 get_paymasters 一樣，但結果暫存一段時間。按鈕／選單要在 3 秒內回應，用這個比較保險。"""
        cached = self.__dict__.get("_paymasters_cache")
        if cached and time.monotonic() - cached[0] < seconds:
            return list(cached[1])
        names = self.get_paymasters()
        self._paymasters_cache = (time.monotonic(), names)
        return list(names)

    def get_paymasters(self) -> list:
        col_values = self.ws(SHEET_JOBS).col_values(self.PAYMASTER_COL)
        names = []
        for v in col_values[1:]:
            v = v.strip()
            if v and v not in names:  # 去掉空白格跟重複的名字
                names.append(v)
        return names

    def add_position(self, name: str) -> str:
        """新增一個位置名稱。回傳 'added' 或 'exists'（已存在就不重複加）。"""
        if name in self.get_positions():
            return "exists"
        col_values = self.ws(SHEET_JOBS).col_values(self.POSITION_COL)
        for i in range(2, len(col_values) + 1):
            if not col_values[i - 1].strip():
                self.update_cell(SHEET_JOBS, i, self.POSITION_COL, name)
                return "added"
        self.update_cell(SHEET_JOBS, len(col_values) + 1, self.POSITION_COL, name)
        return "added"

    def delete_position(self, name: str) -> bool:
        """刪除一個位置名稱（只清空那一格，不刪整列，避免動到同一列的職業資料）。"""
        col_values = self.ws(SHEET_JOBS).col_values(self.POSITION_COL)
        for i, v in enumerate(col_values[1:], start=2):
            if v.strip() == name:
                self.update_cell(SHEET_JOBS, i, self.POSITION_COL, "")
                return True
        return False

    # ---------- 角色資料 / 帳號基本資料 ----------

    def get_characters(self) -> list:
        return self.get_rows(SHEET_CHARACTERS)

    def get_user_characters(self, discord_id: str) -> list:
        return [r for r in self.get_characters() if r.get("Discord ID", "").strip() == str(discord_id)]

    def find_user_by_character_name(self, name: str):
        """依角色名字（正規化後）反查 Discord ID。回傳 (discord_id, 原始角色名字) 或 (None, None)。"""
        key = normalize_name(name)
        for r in self.get_characters():
            if normalize_name(r.get("角色名稱", "")) == key:
                return r.get("Discord ID", "").strip() or None, r.get("角色名稱", "")
        return None, None

    def backfill_discord_ids(self) -> list:
        """
        掃描「場次記錄」裡 Discord ID 是空白、但塔團有名字的列，重新比對現在登記的
        角色資料，找得到就補上 Discord ID（適合在有人「事後才補登 !profile」時使用）。
        回傳補上的清單 [(row_number, discord_id, 角色名稱), ...]，方便呼叫端接著去補
        DC名稱（那個需要問 Discord API，不在這個純資料層處理）。
        """
        characters = self.get_characters()

        def find_uid(name: str):
            key = normalize_name(name)
            for r in characters:
                if normalize_name(r.get("角色名稱", "")) == key:
                    return r.get("Discord ID", "").strip() or None
            return None

        updates = []
        backfilled = []
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if r.get("Discord ID", "").strip():
                continue  # 已經有了，不用補
            char_name = r.get("塔團", "").strip()
            if not char_name:
                continue  # 公會/自用/捐獻列本來就沒有塔團，不用補
            uid = find_uid(char_name)
            if uid:
                updates.append((r["_row"], 4, [uid]))
                backfilled.append((r["_row"], uid, char_name))

        if updates:
            self.batch_update_cells(SHEET_SESSIONS, updates)
        return backfilled

    def find_users_by_character_names(self, names: list) -> dict:
        """
        一次性查詢多個角色名字各自對應到的 Discord ID，只讀一次「角色資料」表，
        避免像 find_user_by_character_name 那樣每個名字各自讀一次整張表。
        回傳 {原始名字: (discord_id, 登記時的角色名字)}。
        """
        characters = self.get_characters()
        result = {}
        for name in names:
            key = normalize_name(name)
            match = (None, None)
            for r in characters:
                if normalize_name(r.get("角色名稱", "")) == key:
                    match = (r.get("Discord ID", "").strip() or None, r.get("角色名稱", ""))
                    break
            result[name] = match
        return result

    def upsert_character(self, discord_id: str, display_name: str, char_name: str, job: str, position: str = "") -> dict:
        """
        新增或更新一隻角色。同名角色（正規化後）視為同一隻，更新職業/位置；否則新增一列。
        新建角色時，會把場次記錄裡這隻角色還沒對應到帳號的舊記錄補上。排序由呼叫的地方預約（延後排序）。
        回傳 {"status": "created" 或 "updated", "backfilled": 補上了幾筆場次記錄}。
        """
        key = normalize_name(char_name)
        rows = self.get_characters()
        for r in rows:
            if r.get("Discord ID", "").strip() == str(discord_id) and normalize_name(r.get("角色名稱", "")) == key:
                self.write_row(SHEET_CHARACTERS, r["_row"],
                                [str(discord_id), display_name, char_name, job, position])
                self.ensure_account_row(discord_id, display_name)
                return {"status": "updated", "backfilled": 0, "sort": None}  # ID 沒變、位置不動，不用排序
        row_num = self._first_empty_row_from(rows)
        self.write_row(SHEET_CHARACTERS, row_num, [str(discord_id), display_name, char_name, job, position])
        # F 出席、G 分潤、H 色碼 三欄公式一起寫（H 欄以前是排序時才補，改成延後排序後，
        # 新角色在排序前也要有顏色，所以建立時就寫好）
        self.write_row(
            SHEET_CHARACTERS, row_num,
            [_char_attendance_formula(row_num), _char_earnings_formula(row_num), _char_color_formula(row_num)],
            start_col=6, raw=True,
        )
        self.ensure_account_row(discord_id, display_name)
        backfilled = self.backfill_sessions_for_character(char_name, discord_id, display_name)
        # 不在這裡馬上排序：很多人同時登記時，每一隻都排一次會撞到 Google 的速度限制。
        # 由呼叫的地方用 schedule_character_sort 預約，等一陣子沒人登記了再排一次。
        return {"status": "created", "backfilled": backfilled, "sort": None}

    def backfill_sessions_for_character(self, char_name: str, discord_id: str, display_name: str) -> int:
        """
        新登記角色時，把場次記錄裡「塔團是這個角色、但 Discord ID 還是空白」的舊記錄補上帳號，
        這樣他在登記之前參加的團，出席次數、分潤、待領金額都會算進來，也能用 /claim 領。
        已經掛在別人 ID 下的列不動（那種情況用 /fixprofile 處理）。
        """
        key = normalize_name(char_name)
        updates = [
            (r["_row"], 4, [str(discord_id), display_name])  # D=Discord ID、E=DC名稱
            for r in self.get_rows(SHEET_SESSIONS, key_col_index=2)
            if not r.get("Discord ID", "").strip() and normalize_name(r.get("塔團", "")) == key
        ]
        if updates:
            self.batch_update_cells(SHEET_SESSIONS, updates)
        return len(updates)

    # 角色資料 F、G、H 三欄是公式（出席次數、分潤總額、色碼），其他欄都是資料
    _CHAR_FORMULA_COLS = {5, 6, 7}   # 0 起算：F=5、G=6、H=7

    def sort_characters(self) -> dict:
        """
        角色資料依 Discord ID 排序（同一個人的角色再依角色名稱），同一個人的角色會排在一起，
        排完重寫 F、G、H 三欄公式。排序在 Google 的伺服器上完成，機器人只送請求。

        排序會搬動所有資料，所以整個過程有保護：
          1. 先把整張「角色資料」複製一份當備份
          2. 排序前記下每一列的資料，排完再讀一次比對，確認每一列都還在、內容一字不差
          3. 公式重寫後讀回計算結果，確認沒有 #REF!、#ERROR! 之類的錯誤
          4. 任何一步出問題，就從備份原封不動貼回去，回到排序前的樣子
          5. 全部通過才刪掉備份
        排序範圍涵蓋到這張表的最後一欄，右邊如果有自己加的欄位也會跟著整列移動。

        回傳 {"ok": True, "rows": 幾列} 或 {"ok": False, "reason": 原因}（此時已經還原）。
        真的連還原都失敗才會丟出例外，並告知備份分頁的名稱，資料都還在那一頁。
        """
        self.check_layout(SHEET_CHARACTERS)
        ss = self._ss()
        ws = self.ws(SHEET_CHARACTERS)
        last_row = len(ws.col_values(1))  # A 欄＝Discord ID，最後一筆資料在哪一列
        if last_row < 2:
            return {"ok": True, "rows": 0}
        width = ws.col_count
        n_rows = last_row - 1
        data_range = f"A2:{_col_letter(width)}{last_row}"

        def data_snapshot() -> Counter:
            """每一列的資料（不含 F、G、H 公式欄），當成一包來比，不管順序。"""
            rows = ws.get(data_range, maintain_size=True)
            return Counter(
                tuple(v for i, v in enumerate(row) if i not in self._CHAR_FORMULA_COLS)
                for row in rows
            )

        before = data_snapshot()
        backup = ss.duplicate_sheet(
            ws.id, new_sheet_name=f"角色資料_排序前備份_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        try:
            if n_rows >= 2:
                ws.sort((1, "asc"), (3, "asc"), range=data_range)
                if data_snapshot() != before:
                    raise DataIntegrityError("排序前後的資料不一致（有列不見、重複，或內容被改動）")

            formulas = [
                [_char_attendance_formula(r), _char_earnings_formula(r), _char_color_formula(r)]
                for r in range(2, last_row + 1)
            ]
            ws.update(f"F2:H{last_row}", formulas, value_input_option="USER_ENTERED")
            results = ws.get(f"F2:H{last_row}", maintain_size=True)
            broken = [i + 2 for i, row in enumerate(results) if any(str(v).startswith("#") for v in row)]
            if broken:
                raise DataIntegrityError(f"公式計算出錯（第 {'、'.join(map(str, broken[:10]))} 列）")
        except Exception as e:
            reason = str(e) if isinstance(e, DataIntegrityError) else f"{type(e).__name__}: {e}"
            self._restore_from_backup(ss, backup, ws, last_row, width, before, data_snapshot)
            return {"ok": False, "reason": reason}

        ss.del_worksheet(backup)
        return {"ok": True, "rows": n_rows}

    def _restore_from_backup(self, ss, backup, ws, last_row, width, before, data_snapshot, action="排序"):
        """
        把備份分頁的內容（值、公式、格式）原封不動貼回原本的分頁，並確認還原後資料跟操作前一致。
        還原成功就刪掉備份；還原失敗就保留備份分頁並丟出例外，讓管理員知道資料在哪裡。
        action 只用在錯誤訊息裡（排序、刪除場次）。
        """
        grid = lambda sheet_id: {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": last_row,
                                 "startColumnIndex": 0, "endColumnIndex": width}
        try:
            # 刪過列的話表格會變短，先確認列數夠把備份整塊貼回去
            if ws.row_count < last_row:
                ws.add_rows(last_row - ws.row_count)
            ss.batch_update({"requests": [{"copyPaste": {
                "source": grid(backup.id), "destination": grid(ws.id),
                "pasteType": "PASTE_NORMAL", "pasteOrientation": "NORMAL",
            }}]})
            if data_snapshot() != before:
                raise DataIntegrityError("還原後的資料仍然不一致")
        except Exception as e:
            raise DataIntegrityError(
                f"{action}出錯，自動還原也失敗了（{e}）。{action}前的完整資料保留在「{backup.title}」這個分頁，"
                f"請不要刪掉它，可以從那裡複製回來，或用試算表的「檔案 → 版本記錄」還原。"
            )
        ss.del_worksheet(backup)

    def delete_character(self, discord_id: str, index: int):
        """index 是這個帳號角色清單裡的第幾個（0-based，跟 !myprofiles 顯示的編號一致）。"""
        chars = self.get_user_characters(discord_id)
        if index < 0 or index >= len(chars):
            return None
        target = chars[index]
        self.delete_row(SHEET_CHARACTERS, target["_row"])
        return {**target, "sort": None}  # 排序由呼叫的地方預約

    def delete_character_by_name(self, discord_id: str, char_name: str):
        """
        用「角色名稱」刪除自己名下的某隻角色（給公告按鈕用）。
        不用編號，是因為角色資料會自動排序，查到編號之後、按下刪除之前如果剛好有人登記新角色，
        順序可能改變，用編號就會刪到別隻；用名字刪，刪的一定是選的那一隻。
        只會刪這個帳號名下的角色，別人同名的角色不會動到。找不到回傳 None。
        """
        key = normalize_name(char_name)
        matches = [c for c in self.get_user_characters(discord_id)
                   if normalize_name(c.get("角色名稱", "")) == key]
        if len(matches) != 1:
            return None
        target = matches[0]
        self.delete_row(SHEET_CHARACTERS, target["_row"])
        return {**target, "sort": None}  # 排序由呼叫的地方預約

    def ensure_account_row(self, discord_id: str, display_name: str):
        """確保帳號基本資料表裡有這個 Discord ID 的列，沒有就新增一列（可出席時間留空）。"""
        rows = self.get_rows(SHEET_ACCOUNTS)
        for r in rows:
            if r.get("Discord ID", "").strip() == str(discord_id):
                return
        row_num = self._first_empty_row_from(rows)
        self.write_row(SHEET_ACCOUNTS, row_num, [str(discord_id), display_name, "", "", ""])
        self.write_row(
            SHEET_ACCOUNTS, row_num,
            [
                _account_attendance_formula(row_num),
                _account_earnings_formula(row_num),
                _account_claimed_formula(row_num),
                _account_pending_formula(row_num),
            ],
            start_col=6, raw=True,
        )

    def find_corrupted_ids(self) -> dict:
        """
        找出「場次記錄」D 欄、「帳號基本資料」A 欄裡看起來被改壞的 Discord ID（給 /checkprofiles 用）。
        角色資料那邊由 /checkprofiles 自己逐一問 Discord，這裡只檢查另外兩張表。
        回傳 {"場次記錄": [(列號, 角色, ID), ...], "帳號基本資料": [(列號, 顯示名稱, ID), ...]}
        """
        sessions = [(r["_row"], r.get("塔團", "").strip() or "（無角色名）", r.get("Discord ID", "").strip())
                    for r in self.get_rows(SHEET_SESSIONS, key_col_index=2)
                    if looks_corrupted_id(r.get("Discord ID", ""))]
        accounts = [(r["_row"], r.get("顯示名稱", "").strip() or "（無名稱）", r.get("Discord ID", "").strip())
                    for r in self.get_rows(SHEET_ACCOUNTS)
                    if looks_corrupted_id(r.get("Discord ID", ""))]
        return {"場次記錄": sessions, "帳號基本資料": accounts}

    def relink_character(self, char_name: str, new_id: str, display_name: str) -> dict:
        """
        把某隻角色重新對應到正確的 Discord 帳號（給管理員修正對錯人的角色用）。
        除了「角色資料」那一列，也會把「場次記錄」裡這隻角色過去的出團記錄一起改過來，
        這樣舊的出席次數、分潤、待領金額才會算到正確的人頭上。
        """
        key = normalize_name(char_name)
        targets = [r for r in self.get_characters() if normalize_name(r.get("角色名稱", "")) == key]
        if not targets:
            return {"ok": False, "reason": "not_found"}

        old_ids = {r.get("Discord ID", "").strip() for r in targets}
        # A=Discord ID、B=顯示名稱（C 以後的角色名稱、職業、位置不動）
        for r in targets:
            self.write_row(SHEET_CHARACTERS, r["_row"], [str(new_id), display_name], start_col=1)
        self.ensure_account_row(new_id, display_name)

        # 場次記錄：這隻角色名下、原本掛在舊 ID、根本沒 ID、或 ID 已經被改壞的列，改掛到新 ID
        updates = []
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if normalize_name(r.get("塔團", "")) != key:
                continue
            current = r.get("Discord ID", "").strip()
            if current in old_ids | {""} or looks_corrupted_id(current):
                updates.append((r["_row"], 4, [str(new_id), display_name]))  # D=Discord ID、E=DC名稱
        if updates:
            self.batch_update_cells(SHEET_SESSIONS, updates)
        sort = self.sort_characters()  # ID 改了，要重新排到新主人的其他角色旁邊

        return {
            "ok": True,
            "sort": sort,
            "char_name": targets[0].get("角色名稱", char_name),
            "old_ids": sorted(i for i in old_ids if i),
            "character_rows": len(targets),
            "session_rows": len(updates),
        }

    def repair_formulas(self) -> dict:
        """
        把三張表所有現有資料列的公式欄，一次重新寫成最新版本，順便把角色資料依 Discord ID 排序。
        用途：修好已經出現 #REF! 的列，並把舊版公式換成「刪除列也不會壞」的新寫法。

        角色資料：走 sort_characters（有備份、前後比對、出錯自動還原）
        場次記錄、帳號基本資料：只寫公式欄、不搬動資料，寫完讀回檢查有沒有計算錯誤
        """
        for name in (SHEET_SESSIONS, SHEET_ACCOUNTS):   # 角色資料由 sort_characters 自己檢查
            self.check_layout(name)
        sort = self.sort_characters()

        def write_block(sheet, first_col, last_col, last_row, makers):
            if last_row < 2:
                return {"rows": 0, "broken": []}
            rng = f"{first_col}2:{last_col}{last_row}"
            rows = [[make(r) for make in makers] for r in range(2, last_row + 1)]
            ws = self.ws(sheet)
            ws.update(rng, rows, value_input_option="USER_ENTERED")
            results = ws.get(rng, maintain_size=True)
            broken = [i + 2 for i, row in enumerate(results) if any(str(v).startswith("#") for v in row)]
            return {"rows": len(rows), "broken": broken}

        last_session = len(self.ws(SHEET_SESSIONS).col_values(2))   # B 欄＝日期時間
        last_acct = len(self.ws(SHEET_ACCOUNTS).col_values(1))      # A 欄＝Discord ID
        return {
            "角色資料": sort,
            # 場次記錄：N、O 相鄰一起寫；P 是「發錢的人」資料欄不能動，所以 Q 另外寫
            "場次記錄": write_block(SHEET_SESSIONS, "N", "O", last_session,
                                    [_session_first_occurrence_formula, _session_color_formula]),
            "場次記錄Q": write_block(SHEET_SESSIONS, "Q", "Q", last_session,
                                     [_session_first_occurrence_by_char_formula]),
            "帳號基本資料": write_block(SHEET_ACCOUNTS, "F", "I", last_acct,
                                      [_account_attendance_formula, _account_earnings_formula,
                                       _account_claimed_formula, _account_pending_formula]),
        }

    def get_account_stats(self, discord_id: str) -> dict:
        for r in self.get_rows(SHEET_ACCOUNTS):
            if r.get("Discord ID", "").strip() == str(discord_id):
                return r
        return {}

    def update_availability(self, discord_id: str, display_name: str,
                             weekday: bool = None, weekend: bool = None, note: str = None):
        """更新平日/假日可出席、其他時間備註。傳 None 代表這個欄位保持原值不變。"""
        self.ensure_account_row(discord_id, display_name)
        for r in self.get_rows(SHEET_ACCOUNTS):
            if r.get("Discord ID", "").strip() == str(discord_id):
                cur_weekday = str(r.get("平日可出席", "")).strip().upper() == "TRUE"
                cur_weekend = str(r.get("假日可出席", "")).strip().upper() == "TRUE"
                cur_note = r.get("其他時間備註", "")
                new_weekday = cur_weekday if weekday is None else weekday
                new_weekend = cur_weekend if weekend is None else weekend
                new_note = cur_note if note is None else note
                self.write_row(SHEET_ACCOUNTS, r["_row"], [new_weekday, new_weekend, new_note], start_col=3)
                return

    # ---------- 場次記錄 ----------

    # ---------- 進行中的場次（存在「系統狀態」分頁，機器人重啟後接回來） ----------
    # 進行中的場次原本只存在機器人的記憶體裡，Railway 每次部署都會重啟，出團到一半就會不見。
    # 每次開場、記錄寶物之後存一份，啟動時讀回來。

    def _state_sheet(self):
        try:
            return self.ws("系統狀態")
        except gspread.WorksheetNotFound:
            ws = self._ss().add_worksheet(title="系統狀態", rows=10, cols=2)
            ws.update("A1:B1", [["項目", "內容（機器人自動維護，請不要手動修改）"]], value_input_option="RAW")
            return ws

    def save_active_session(self, session):
        """存下進行中的場次；None 代表目前沒有進行中的場次。用 RAW 寫入，內容原封不動當文字存。"""
        value = json.dumps(session, ensure_ascii=False) if session else ""
        self._state_sheet().update("A2:B2", [["進行中場次", value]], value_input_option="RAW")

    def load_active_session(self):
        """
        讀回進行中的場次，沒有或讀不懂就回傳 None。
        寶物編號會再跟場次記錄核對一次，取比較大的那個：就算某次存檔失敗，
        接回來之後也不會讓兩樣寶物用到同一個編號（同編號在結算時會被當成同一樣）。
        """
        raw = (self._state_sheet().get("B2") or [[""]])[0]
        raw = raw[0].strip() if raw else ""
        if not raw:
            return None
        session = json.loads(raw)
        if not isinstance(session, dict) or not session.get("id") or not isinstance(session.get("members"), list):
            return None
        session["next_item_index"] = max(int(session.get("next_item_index", 0)),
                                         self.next_item_index(session["id"]))
        return session

    def get_session_rows(self, session_id: str) -> list:
        return [r for r in self.get_rows(SHEET_SESSIONS, key_col_index=2) if r.get("場次ID", "").strip() == session_id]

    def next_item_index(self, session_id: str) -> int:
        indices = [
            int(r["寶物編號"]) for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip().isdigit()
        ]
        return (max(indices) + 1) if indices else 0

    def record_attendance(self, session_id: str, when_iso: str, members: list):
        """
        單純記錄出席，不綁定任何寶物。確認隊員名單的當下就寫入這筆，
        這樣就算這一場全程沒有掉寶，出席次數也還是會被正確算到。
        P 欄「發錢的人」留空（沒有寶物，不需要發錢）。
        """
        rows_data = []
        for m in members:
            rows_data.append([
                session_id, when_iso, m.get("name", ""), m.get("discord_id") or "",
                m.get("display_name", m.get("name", "")), "", "", "出席", "", "", "", "", "",
            ])
        self.append_rows_batch(
            SHEET_SESSIONS, rows_data, start_col=1, key_col_index=2,
            extra_formulas=[
                (14, _session_first_occurrence_formula),
                (15, _session_color_formula),
                (16, lambda r: ""),
                (17, _session_first_occurrence_by_char_formula),
            ],
        )

    def append_item_rows(self, session_id, when_iso, members, item_name, item_index, item_type, contributor,
                          paymaster: str = ""):
        """
        members: list of {"discord_id": str|None, "name": str} — 分潤類型才需要多列。
        分潤：每個 member 各一列；公會/自用：只需要一列（塔團/DiscordID 留空）。
        這裡會把這次要新增的所有列一次性打包成一個 API 呼叫寫入，不會一列一列分開打。
        paymaster：這批寶物由誰發錢，寫進 P 欄「發錢的人」（只有上傳寶物截圖的流程會選，其他情況留空）。
        """
        rows_data = []
        if item_type == "分潤":
            for m in members:
                rows_data.append([
                    session_id or "", when_iso, m.get("name", ""), m.get("discord_id") or "",
                    m.get("display_name", m.get("name", "")), item_name, item_index,
                    item_type, contributor or "", "", "", "", "",
                ])
        else:
            rows_data.append([
                session_id or "", when_iso, "", "", "",
                item_name, item_index if item_index is not None else "",
                item_type, contributor or "", "", "", "", "",
            ])
        self.append_rows_batch(
            SHEET_SESSIONS, rows_data, start_col=1, key_col_index=2,
            extra_formulas=[
                (14, _session_first_occurrence_formula),
                (15, _session_color_formula),
                (16, lambda r: _sanitize(paymaster)),
                (17, _session_first_occurrence_by_char_formula),
            ],
        )

    def sell_item(self, session_id: str, item_index: int, amount: int, item_name: str = None,
                  paymaster: str = "") -> dict:
        """
        把指定場次+編號的寶物填上售出金額，分潤類型會平分給每一列。回傳結果摘要。
        paymaster：這樣寶物由誰發錢，有填的話寫進這樣寶物每一列的 P 欄「發錢的人」；沒填就不動 P 欄。
        item_name：捐獻的寶物場次ID跟編號都是空白，光靠編號會比對到同一批所有捐獻，
        所以選單那條路會額外傳名稱來精確定位。
        售出金額必須大於 0：填 0 會變成「已售出、每人分 0」而且之後不能再賣；填負數會讓大家的待領變成負的。
        """
        if not isinstance(amount, (int, float)) or amount <= 0:
            return {"ok": False, "reason": "invalid_amount"}
        target_rows = [
            r for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip() == str(item_index)
            and (item_name is None or r.get("掉落", "").strip() == item_name)
        ]
        if not target_rows:
            return {"ok": False, "reason": "not_found"}
        if any(r.get("售出金額", "").strip() for r in target_rows):
            return {"ok": False, "reason": "already_sold"}

        item_type = target_rows[0].get("類型", "")
        item_name = target_rows[0].get("掉落", "")

        if item_type == "分潤":
            per_person = amount / len(target_rows)
            updates = [(r["_row"], 10, [amount, round(per_person, 2), False, ""]) for r in target_rows]
        else:
            per_person = amount
            updates = [(r["_row"], 10, [amount, "", "", ""]) for r in target_rows]

        if paymaster:
            updates += [(r["_row"], 16, [paymaster]) for r in target_rows]   # P 欄＝發錢的人
        self.batch_update_cells(SHEET_SESSIONS, updates)   # 售出金額跟發錢的人同一次寫入

        return {
            "ok": True, "item_name": item_name, "item_type": item_type,
            "amount": amount, "per_person": per_person, "n_rows": len(target_rows),
            "paymaster": paymaster,
        }

    def list_unsold_items(self, session_id: str = None) -> list:
        """
        列出還沒結算（售出金額空白）的寶物，給 !sell 的選單用。
        不指定 session_id 就列出所有場次的，包含捐獻（場次ID 空白）的寶物。
        回傳 [{"session_id","item_index","name","when","item_type","n_rows"}, ...]，
        照日期時間新到舊排序。
        """
        rows = self.get_rows(SHEET_SESSIONS, key_col_index=2)
        if session_id:
            rows = [r for r in rows if r.get("場次ID", "").strip() == session_id]

        groups = {}
        for r in rows:
            if r.get("類型") == "出席":
                continue  # 純出席列沒有寶物
            if not r.get("掉落", "").strip():
                continue
            if r.get("售出金額", "").strip():
                continue  # 已經結算過了
            key = (r.get("場次ID", "").strip(), r.get("寶物編號", "").strip(), r.get("掉落", "").strip())
            if key not in groups:
                groups[key] = {
                    "session_id": key[0],
                    "item_index": key[1],
                    "name": key[2],
                    "when": r.get("日期時間", ""),
                    "item_type": r.get("類型", ""),
                    "n_rows": 0,
                }
            groups[key]["n_rows"] += 1

        return sorted(groups.values(), key=lambda x: x["when"], reverse=True)

    def give_item_to_member(self, session_id: str, item_index: int, receiver: str,
                             item_name: str = None, receiver_display: str = None) -> dict:
        """
        把原本要分潤的寶物改成免費給某個成員（類型改「自用」、金額 0）。
        receiver 必須是「角色資料」裡登記過的角色名稱，比對不到會擋下來並回傳
        reason="unknown_receiver"，讓呼叫端提示重打。
        保留的那一列會把塔團/DiscordID/DC名稱填成收下的人，其他分潤列刪掉，
        避免每個人都掛著一筆 0 元待領。
        """
        uid, matched_name = self.find_user_by_character_name(receiver)
        if not matched_name:
            return {"ok": False, "reason": "unknown_receiver", "receiver": receiver}

        target_rows = [
            r for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip() == str(item_index)
            and (item_name is None or r.get("掉落", "").strip() == item_name)
        ]
        if not target_rows:
            return {"ok": False, "reason": "not_found"}
        if any(r.get("售出金額", "").strip() for r in target_rows):
            return {"ok": False, "reason": "already_sold"}

        item_name = target_rows[0].get("掉落", "")
        keep = target_rows[0]
        display = receiver_display or matched_name

        # C=塔團 D=DiscordID E=DC名稱 → 填成收下的人
        self.write_row(SHEET_SESSIONS, keep["_row"], [matched_name, uid or "", display], start_col=3)
        # H=類型 I=來源/貢獻者（誰拿走的已經記在塔團/DC名稱欄，這裡只記處理方式）
        self.write_row(SHEET_SESSIONS, keep["_row"], ["自用", "成員免費領取"], start_col=8)
        # J=售出金額 K=均分$$ L=已領 M=領取時間
        # 東西當下就交出去了，所以已領打勾、領取時間記成現在
        self.write_row(SHEET_SESSIONS, keep["_row"], [0, "", True, now_str()], start_col=10)

        # 其他多餘的分潤列整列刪掉（從後面往前刪，避免刪除後列號位移）
        for r in sorted(target_rows[1:], key=lambda x: x["_row"], reverse=True):
            self.delete_row(SHEET_SESSIONS, r["_row"])

        return {
            "ok": True, "item_name": item_name, "receiver": matched_name,
            "discord_id": uid, "removed_rows": len(target_rows) - 1,
        }

    def change_item_type(self, session_id: str, item_index: int, new_type: str,
                          item_name: str = None) -> dict:
        """
        把還沒結算的寶物改成別的類型（例如原本要分潤、改成歸公會之後再處理）。
        改成「公會」或「自用」時只留一列（那兩種類型本來就不分人），多餘的分潤列會刪掉。
        """
        target_rows = [
            r for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip() == str(item_index)
            and (item_name is None or r.get("掉落", "").strip() == item_name)
        ]
        if not target_rows:
            return {"ok": False, "reason": "not_found"}
        if any(r.get("售出金額", "").strip() for r in target_rows):
            return {"ok": False, "reason": "already_sold"}

        found_name = target_rows[0].get("掉落", "")
        keep = target_rows[0]

        if new_type in ("公會", "自用"):
            # C=塔團 D=DiscordID E=DC名稱 清空（這兩種類型不掛在特定人身上）
            self.write_row(SHEET_SESSIONS, keep["_row"], ["", "", ""], start_col=3)
            self.write_row(SHEET_SESSIONS, keep["_row"], [new_type], start_col=8)
            for r in sorted(target_rows[1:], key=lambda x: x["_row"], reverse=True):
                self.delete_row(SHEET_SESSIONS, r["_row"])
        else:
            for r in target_rows:
                self.write_row(SHEET_SESSIONS, r["_row"], [new_type], start_col=8)

        return {"ok": True, "item_name": found_name, "new_type": new_type}

    def claim_for_user(self, discord_id: str, session_id: str = None) -> dict:
        """
        把這個使用者尚未領取的分潤列標記已領。回傳明細。
        session_id=None 代表不限場次（全部領取）；傳入字串就只領那一場——
        注意捐獻的寶物場次ID 是空字串，所以這裡要用 is not None 判斷，
        不能直接用 if session_id，否則空字串會被當成「沒指定」而整包全領。
        """
        now = now_str()
        total = 0.0
        details = []
        items = []
        updates = []
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if r.get("類型") != "分潤":
                continue
            if r.get("Discord ID", "").strip() != str(discord_id):
                continue
            if session_id is not None and r.get("場次ID", "").strip() != session_id:
                continue
            already = str(r.get("已領", "")).strip().upper() == "TRUE"
            sale = r.get("售出金額", "").strip()
            per_person = r.get("均分$$", "").strip()
            if already or not sale or not per_person:
                continue
            updates.append((r["_row"], 12, [True, now]))
            amt = float(per_person)
            total += amt
            details.append((r.get("場次ID", ""), r.get("掉落", ""), amt))
            items.append({"session_id": r.get("場次ID", ""), "date": _date_part(r.get("日期時間", "")),
                          "item_name": r.get("掉落", ""), "amount": amt,
                          "paymaster": r.get("發錢的人", "").strip()})

        if updates:
            self.batch_update_cells(SHEET_SESSIONS, updates)
        return {"total": total, "details": details, "items": items}

    def pending_items_for_user(self, discord_id: str) -> list:
        """
        回傳這個人每一筆待領（以「每一樣寶物」為單位，不是以場次為單位）。
        [{"row": 列號, "session_id":..., "date": 日期, "item_name":..., "amount":..., "paymaster": 發錢的人}, ...]
        row 是這一列在「場次記錄」表的實際列號，用來精確指定要領哪一筆。
        """
        items = []
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if r.get("類型") != "分潤":
                continue
            if r.get("Discord ID", "").strip() != str(discord_id):
                continue
            already = str(r.get("已領", "")).strip().upper() == "TRUE"
            sale = r.get("售出金額", "").strip()
            per_person = r.get("均分$$", "").strip()
            if already or not sale or not per_person:
                continue
            items.append({
                "row": r["_row"],
                "session_id": r.get("場次ID", ""),
                "item_index": r.get("寶物編號", "").strip(),
                "character": r.get("塔團", "").strip(),
                "date": _date_part(r.get("日期時間", "")),
                "item_name": r.get("掉落", ""),
                "amount": float(per_person),
                "paymaster": r.get("發錢的人", "").strip(),
            })
        return items

    def claim_item_row(self, discord_id: str, row: int, expected: dict = None) -> dict:
        """
        領取單一一筆待領。

        row 是打開清單當下這一筆在第幾列。但清單打開之後、按下領取之前，如果有人刪除了上面的記錄，
        下面的列會往上補，列號就不再是原本那一筆。所以 expected 帶著「場次ID＋寶物編號＋寶物名稱＋角色」，
        先確認那一列還是同一筆；不是的話就用這些資訊重新找。
        （要加上角色：同一個帳號帶兩隻角色出同一場，同一樣寶物會有兩筆，只看寶物分不出來。）
        找不到、或找到不只一筆，就不領（回傳 reason="moved"），請使用者重新打開清單。
        """
        def identity(r):
            return (r.get("場次ID", "").strip(), r.get("寶物編號", "").strip(), r.get("掉落", "").strip(),
                    r.get("塔團", "").strip())

        def is_mine_pending(r):
            return (r.get("類型") == "分潤" and r.get("Discord ID", "").strip() == str(discord_id)
                    and str(r.get("已領", "")).strip().upper() != "TRUE"
                    and r.get("售出金額", "").strip() and r.get("均分$$", "").strip())

        all_rows = self.get_rows(SHEET_SESSIONS, key_col_index=2)
        r = next((x for x in all_rows if x["_row"] == row), None)
        if expected is not None:
            want = (str(expected.get("session_id", "")).strip(), str(expected.get("item_index", "")).strip(),
                    str(expected.get("item_name", "")).strip(), str(expected.get("character", "")).strip())
            if r is None or identity(r) != want:
                # 列號已經不是原本那一筆了（上面有記錄被刪除），用場次＋寶物重新找
                candidates = [x for x in all_rows if identity(x) == want and is_mine_pending(x)]
                if len(candidates) != 1:
                    return {"ok": False, "reason": "moved"}
                r = candidates[0]
                row = r["_row"]
        if r is None or r.get("類型") != "分潤" or r.get("Discord ID", "").strip() != str(discord_id):
            return {"ok": False, "reason": "not_found"}
        already = str(r.get("已領", "")).strip().upper() == "TRUE"
        sale = r.get("售出金額", "").strip()
        per_person = r.get("均分$$", "").strip()
        if already or not sale or not per_person:
            return {"ok": False, "reason": "already_claimed"}

        now = now_str()
        self.batch_update_cells(SHEET_SESSIONS, [(row, 12, [True, now])])
        return {
            "ok": True, "session_id": r.get("場次ID", ""), "date": _date_part(r.get("日期時間", "")),
            "item_name": r.get("掉落", ""), "amount": float(per_person),
            "paymaster": r.get("發錢的人", "").strip(),
        }

    # 場次記錄 N、O、Q 三欄是公式（同場首筆、色碼、同場角色首筆），其他欄（含 P 發錢的人）都是資料
    _SESSION_FORMULA_COLS = {13, 14, 16}   # 0 起算：N=13、O=14、Q=16

    @staticmethod
    def _record_key(r: dict):
        """
        這一列屬於哪一筆可刪除的記錄：(場次ID, 種類, 寶物編號, 寶物名稱)；不屬於任何一筆就回傳 None。
        /deletesession 的選單跟刪除都用這個函式認資料，保證選單列得出來的，刪除時一定找得到同一批列。
        """
        sid = str(r.get("場次ID", "")).strip()
        if not sid:
            return None   # 捐獻沒有場次ID，不用這個方式刪
        if str(r.get("類型", "")).strip() == "出席":
            return (sid, "attendance", "", "出席記錄")
        name = str(r.get("掉落", "")).strip()
        if not name:
            return None
        return (sid, "item", str(r.get("寶物編號", "")).strip(), name)

    def list_records(self, session_id: str = None) -> list:
        """
        列出場次記錄裡可以刪除的項目，給 /deletesession 的選單用。最新的排最前面。
        每一樣寶物一個項目（場次ID＋寶物編號＋掉落）；只有出席、沒有寶物的場次，出席列另外一個項目。
        捐獻（沒有場次ID）不列出。session_id 有給的話只列那一場。

        [{"key", "session_id", "date", "kind": "item" 或 "attendance", "item_index", "item_name",
          "rows": 幾列, "people": [誰], "claimed": 已領幾人, "unclaimed": 未領幾人, "sold": 是否已賣出,
          "last_item": 刪掉之後這場是不是就沒有任何記錄了（出席也會一起消失）}, ...]
        """
        entries, per_session = {}, {}
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            rk = self._record_key(r)
            if rk is None or (session_id is not None and rk[0] != session_id):
                continue
            sid, kind, idx, name = rk
            key = f"{sid}|{kind}|{idx}|{name}"
            e = entries.setdefault(key, {
                "key": key, "session_id": sid, "date": _date_part(r.get("日期時間", "")), "kind": kind,
                "item_index": idx, "item_name": name, "rows": 0, "people": [], "claimed": 0, "unclaimed": 0,
                "sold": False, "last_row": 0,
            })
            e["rows"] += 1
            e["last_row"] = max(e["last_row"], r["_row"])
            person = r.get("DC名稱", "").strip() or r.get("塔團", "").strip()
            if person:
                e["people"].append(person)
            if str(r.get("售出金額", "")).strip():
                e["sold"] = True
            if r.get("類型") == "分潤":
                if str(r.get("已領", "")).strip().upper() == "TRUE":
                    e["claimed"] += 1
                else:
                    e["unclaimed"] += 1
            per_session.setdefault(sid, set()).add(key)

        result = sorted(entries.values(), key=lambda x: x["last_row"], reverse=True)
        seen = {}
        for e in result:
            e["last_item"] = len(per_session[e["session_id"]]) == 1
            # 同一場有兩樣同名的寶物（例如掉了兩張死靈卡），第二個起加上（2）（3）區分
            label_key = (e["session_id"], e["item_name"])
            seen[label_key] = seen.get(label_key, 0) + 1
        counts = dict(seen)
        order = {}
        for e in reversed(result):   # 從最舊的開始編號，比較直覺
            label_key = (e["session_id"], e["item_name"])
            order[label_key] = order.get(label_key, 0) + 1
            e["dup_no"] = order[label_key] if counts[label_key] > 1 else 0
            e.pop("last_row", None)
        return result

    def delete_record(self, session_id: str, kind: str, item_index: str, item_name: str,
                      expected_rows: int) -> dict:
        """
        刪除一筆記錄：一樣寶物（場次ID＋寶物編號＋掉落）的所有列，或一場的出席列。
        其他寶物、其他場次都不會動到。刪除後後面的記錄自動往上補，不留空白（跟手動「刪除列」一樣）。

        整個過程有保護：
          1. 先核對列數跟確認畫面上看到的一樣，不一樣代表中間有人改過資料，直接取消
          2. 刪除前把整張「場次記錄」複製一份當備份
          3. 所有要刪的列包在同一個請求裡送出，要嘛全部刪掉、要嘛全部不動
          4. 刪完比對：其他每一列都還在、內容一字不差，而且要刪的列一列都不剩
          5. 重寫 N、O、Q 公式，確認沒有 #REF! 之類的錯誤
          6. 任何一步出錯就從備份原封不動貼回去；全部通過才刪掉備份

        回傳 {"ok": True, "deleted": 刪了幾列, "rows": [被刪掉的每一列內容]}
        或 {"ok": False, "reason": 原因}（此時資料沒有被改動，或已經還原）。
        """
        if not session_id:
            return {"ok": False, "reason": "沒有指定場次（捐獻的記錄不能用這個方式刪）"}
        try:
            self.check_layout(SHEET_SESSIONS)
        except DataIntegrityError as e:
            return {"ok": False, "reason": str(e)}

        wanted = (session_id, kind, str(item_index).strip(), str(item_name).strip())

        def target_rows() -> set:
            """用跟選單一模一樣的方式認資料，回傳這筆記錄現在在哪幾列。"""
            return {r["_row"] for r in self.get_rows(SHEET_SESSIONS, key_col_index=2)
                    if self._record_key(r) == wanted}

        ss = self._ss()
        ws = self.ws(SHEET_SESSIONS)
        last_row = len(ws.col_values(2))   # B 欄＝日期時間，每一列都有
        if last_row < 2:
            return {"ok": False, "reason": "場次記錄是空的"}
        width = ws.col_count
        headers = ws.row_values(1)
        data_range = f"A2:{_col_letter(width)}{last_row}"

        def data_rows():
            """
            [(列號, 整列內容), ...]，只算日期時間（B 欄）有內容的列。
            不能用「整列有沒有任何一格有內容」判斷：已領欄如果整欄插入了核取方塊，
            資料下面的空白列也會有一個沒打勾的方塊（讀出來是 FALSE）。刪掉幾列之後這些空白列會往上移、
            跑進比對範圍，被誤當成資料，比對就會失敗。機器人寫的每一列都有日期時間，預先放的方塊、公式不會有。
            """
            rows = ws.get(data_range, maintain_size=True)
            return [(i + 2, row) for i, row in enumerate(rows) if str(row[1]).strip()]

        def data_key(row):
            return tuple(v for i, v in enumerate(row) if i not in self._SESSION_FORMULA_COLS)

        def data_snapshot() -> Counter:
            return Counter(data_key(row) for _, row in data_rows())

        before_rows = data_rows()
        target_nums = target_rows()
        targets = [(n, row) for n, row in before_rows if n in target_nums]
        if not targets:
            # 列出這一場目前有哪些記錄，方便判斷是被刪了、還是名稱／編號對不上
            present = sorted({f"{k[3]}（編號 {k[2] or '-'}）" for k in
                              (self._record_key(r) for r in self.get_rows(SHEET_SESSIONS, key_col_index=2))
                              if k and k[0] == session_id})
            where = "、".join(present) if present else "（這一場已經沒有任何記錄）"
            return {"ok": False, "reason": (f"找不到這筆記錄（{session_id} {item_name}，編號 {item_index or '-'}），"
                                            f"可能已經被刪除了。這一場目前有：{where}")}
        if len(targets) != expected_rows:
            return {"ok": False, "reason": (f"這筆記錄在確認期間有變動（確認時是 {expected_rows} 列，現在是 "
                                            f"{len(targets)} 列），為了安全沒有刪除，請重新操作一次")}
        before = Counter(data_key(row) for _, row in before_rows)
        expected_after = before - Counter(data_key(row) for _, row in targets)

        # 連續的列合併成一段，從最下面那段開始刪，前面的列號才不會因為刪除而位移
        target_nums = sorted(n for n, _ in targets)
        spans = []
        for n in target_nums:
            if spans and n == spans[-1][1] + 1:
                spans[-1][1] = n
            else:
                spans.append([n, n])
        requests = [{"deleteDimension": {"range": {
            "sheetId": ws.id, "dimension": "ROWS", "startIndex": a - 1, "endIndex": b}}}
            for a, b in reversed(spans)]

        backup = ss.duplicate_sheet(
            ws.id, new_sheet_name=f"場次記錄_刪除前備份_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        )
        try:
            ss.batch_update({"requests": requests})
            after = data_snapshot()
            if after != expected_after:
                raise DataIntegrityError("刪除後的資料不一致（刪到別場的列，或有列不見、被改動）")
            if target_rows():
                raise DataIntegrityError("刪除後還有殘留的列")

            new_last = len(ws.col_values(2))
            if new_last >= 2:
                for rng, makers in (
                    (f"N2:O{new_last}", [_session_first_occurrence_formula, _session_color_formula]),
                    (f"Q2:Q{new_last}", [_session_first_occurrence_by_char_formula]),
                ):
                    ws.update(rng, [[m(r) for m in makers] for r in range(2, new_last + 1)],
                              value_input_option="USER_ENTERED")
                    results = ws.get(rng, maintain_size=True)
                    broken = [i + 2 for i, row in enumerate(results) if any(str(v).startswith("#") for v in row)]
                    if broken:
                        raise DataIntegrityError(f"刪除後公式計算出錯（第 {'、'.join(map(str, broken[:10]))} 列）")
        except Exception as e:
            reason = str(e) if isinstance(e, DataIntegrityError) else f"{type(e).__name__}: {e}"
            self._restore_from_backup(ss, backup, ws, last_row, width, before, data_snapshot, action="刪除記錄")
            return {"ok": False, "reason": reason}

        ss.del_worksheet(backup)
        deleted = [dict(zip(headers, row)) for _, row in targets]
        return {"ok": True, "deleted": len(targets), "rows": deleted}

    def sold_items_claim_status(self, session_id: str = None) -> list:
        """
        列出已經賣出（有售出金額）的分潤寶物，每一樣附上誰領了、誰還沒領。給 /unclaimed 用。
        session_id 有給的話只列那一場。最新的排最前面（依寶物在表格裡的位置，越下面越新）。

        回傳：[{"key": 識別字串, "session_id", "date", "item_name", "sale",
                "claimed": [(名字, 金額), ...], "unclaimed": [(名字, 金額), ...]}, ...]
        名字：有綁定帳號的用 DC名稱；沒綁定的在後面加「(未綁定)」。
        同一個帳號帶兩隻角色，金額合併成一筆。
        """
        items = {}
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if r.get("類型") != "分潤" or not str(r.get("售出金額", "")).strip():
                continue
            sid = r.get("場次ID", "").strip()
            if session_id is not None and sid != session_id:
                continue
            key = f"{sid}|{r.get('寶物編號', '').strip()}|{r.get('掉落', '').strip()}"
            item = items.setdefault(key, {
                "key": key, "session_id": sid, "date": _date_part(r.get("日期時間", "")),
                "item_name": r.get("掉落", "").strip(), "sale": _to_number(r.get("售出金額")),
                "last_row": 0, "people": {},
            })
            item["last_row"] = max(item["last_row"], r["_row"])

            uid = r.get("Discord ID", "").strip()
            dc_name = r.get("DC名稱", "").strip() or r.get("塔團", "").strip()
            person_key = uid or f"raw:{r.get('塔團', '').strip()}"
            name = dc_name if uid else f"{dc_name}(未綁定)"
            claimed = str(r.get("已領", "")).strip().upper() == "TRUE"
            slot = item["people"].setdefault((person_key, claimed), [name, 0.0])
            slot[1] += _to_number(r.get("均分$$"))

        result = []
        for item in sorted(items.values(), key=lambda x: x["last_row"], reverse=True):
            people = item.pop("people")
            item.pop("last_row")
            item["claimed"] = [tuple(v) for (pk, c), v in people.items() if c]
            item["unclaimed"] = [tuple(v) for (pk, c), v in people.items() if not c]
            result.append(item)
        return result

    def guild_fund_total(self) -> float:
        total = 0.0
        for r in self.get_rows(SHEET_SESSIONS, key_col_index=2):
            if r.get("類型") == "公會" and r.get("售出金額", "").strip():
                total += float(r["售出金額"])
        return total

    # ---------- 頻道/論壇指令規則（另外存一個獨立分頁不划算，先放在職業管理表旁邊的做法不理想，
    # 改成存在一個叫「系統設定」的分頁；如果沒有這個分頁，第一次使用時自動建立） ----------

    def _rules_sheet(self):
        try:
            return self.ws("系統設定")
        except gspread.WorksheetNotFound:
            ss = self._ss()
            ws = ss.add_worksheet(title="系統設定", rows=200, cols=2)
            ws.update("A1:B1", [["頻道/討論串ID", "允許指令(逗號分隔)"]], value_input_option="USER_ENTERED")
            return ws

    # ---------- 身分組按鈕（存在「身分組按鈕」分頁，第一次用到時自動建立） ----------
    # 一列一個按鈕：A 分組、B 身分組ID、C 表情符號、D 身分組名稱（只是方便人看，機器人以 ID 為準）。

    def _role_sheet(self):
        try:
            return self.ws("身分組按鈕")
        except gspread.WorksheetNotFound:
            ws = self._ss().add_worksheet(title="身分組按鈕", rows=200, cols=4)
            ws.update("A1:D1", [["分組", "身分組ID", "表情符號", "身分組名稱（參考用）"]], value_input_option="RAW")
            return ws

    def get_role_buttons(self) -> list:
        """[{"group", "role_id", "emoji", "name"}, ...]，照試算表裡的順序。"""
        out = []
        for row in self._role_sheet().get_all_values()[1:]:
            row = row + [""] * (4 - len(row))
            if row[1].strip():
                out.append({"group": row[0].strip() or "預設", "role_id": row[1].strip(),
                            "emoji": row[2].strip(), "name": row[3].strip()})
        return out

    def set_role_button(self, group: str, role_id: str, emoji: str, name: str) -> str:
        """新增或更新一個身分組按鈕（同一分組裡同一個身分組只會有一列）。回傳 "created" 或 "updated"。"""
        ws = self._role_sheet()
        values = ws.get_all_values()
        row = [_sanitize(group), _sanitize(role_id), _sanitize(emoji), _sanitize(name)]
        for i, r in enumerate(values[1:], start=2):
            r = r + [""] * (4 - len(r))
            if (r[0].strip() or "預設") == group and r[1].strip() == role_id:
                ws.update(f"A{i}:D{i}", [row], value_input_option="USER_ENTERED")
                return "updated"
        n = len(values) + 1
        ws.update(f"A{n}:D{n}", [row], value_input_option="USER_ENTERED")
        return "created"

    def remove_role_button(self, group: str, role_id: str) -> bool:
        ws = self._role_sheet()
        for i, r in enumerate(ws.get_all_values()[1:], start=2):
            r = r + [""] * (4 - len(r))
            if (r[0].strip() or "預設") == group and r[1].strip() == role_id:
                ws.delete_rows(i)
                return True
        return False

    # ---------- 圖片辨識頻道（存在「辨識頻道」分頁，第一次用到時自動建立） ----------
    # 一個頻道ID一列。完全沒有設定任何頻道時，所有頻道的圖片都辨識（維持原本的行為）；
    # 設定了至少一個之後，只有這些頻道（以及它們底下的討論串／論壇貼文）的圖片才會辨識。

    def _ocr_sheet(self):
        try:
            return self.ws("辨識頻道")
        except gspread.WorksheetNotFound:
            ws = self._ss().add_worksheet(title="辨識頻道", rows=200, cols=1)
            ws.update("A1", [["頻道ID（這些頻道的圖片才會辨識）"]], value_input_option="USER_ENTERED")
            return ws

    def get_ocr_channels(self) -> set:
        values = self._ocr_sheet().col_values(1)
        return {v.strip() for v in values[1:] if v.strip()}

    def add_ocr_channel(self, channel_id: str) -> bool:
        """加入辨識頻道；已經在清單裡就不重複加，回傳 False。"""
        ws = self._ocr_sheet()
        values = ws.col_values(1)
        if channel_id in {v.strip() for v in values[1:]}:
            return False
        ws.update(f"A{len(values) + 1}", [[_sanitize(channel_id)]], value_input_option="USER_ENTERED")
        return True

    def remove_ocr_channel(self, channel_id: str) -> bool:
        """移出辨識頻道；本來就不在清單裡回傳 False。"""
        ws = self._ocr_sheet()
        for i, v in enumerate(ws.col_values(1)[1:], start=2):
            if v.strip() == channel_id:
                ws.delete_rows(i)
                return True
        return False

    def get_all_channel_rules(self) -> dict:
        ws = self._rules_sheet()
        values = ws.get_all_values()
        rules = {}
        for row in values[1:]:
            if len(row) >= 2 and row[0].strip():
                rules[row[0].strip()] = [c.strip() for c in row[1].split(",") if c.strip()]
        return rules

    def set_channel_rules(self, key: str, allowed: list):
        ws = self._rules_sheet()
        values = ws.get_all_values()
        for i, row in enumerate(values[1:], start=2):
            if row and row[0].strip() == key:
                ws.update(f"A{i}:B{i}", [[_sanitize(key), ",".join(allowed)]], value_input_option="USER_ENTERED")
                return
        row_num = len(values) + 1
        ws.update(f"A{row_num}:B{row_num}", [[_sanitize(key), ",".join(allowed)]], value_input_option="USER_ENTERED")

    def clear_channel_rules(self, key: str):
        ws = self._rules_sheet()
        values = ws.get_all_values()
        for i, row in enumerate(values[1:], start=2):
            if row and row[0].strip() == key:
                ws.delete_rows(i)
                return
