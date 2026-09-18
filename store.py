"""
Google Sheets 儲存層。
所有跟 gspread 的直接互動都集中在這裡，cogs 不直接碰 gspread。

工作表結構（跟 xlsx 範本 v10 一致，欄位順序不能亂動，公式欄位機器人永遠不寫）：

角色資料：   A DiscordID(隱藏) B顯示名稱 C角色名稱 D職業 E位置
             F出席次數(公式) G分潤總額(公式) H色碼(公式)
場次記錄：   A場次ID(隱藏) B日期時間 C塔團 D DiscordID(隱藏) E DC名稱 F掉落 G寶物編號(隱藏)
             H類型 I來源/貢獻者 J售出金額 K均分$$ L已領 M領取時間 N同場首筆(公式) O色碼(公式)
職業管理：   A職業名稱 B轉職層級 C承接自 D圖片網址
帳號基本資料：A DiscordID(隱藏) B顯示名稱 C平日可出席 D假日可出席 E其他時間備註
             F出席次數(公式) G分潤總額(公式) H已領總額(公式) I待領總額(公式)
"""
import os
import base64
import json
import asyncio
from datetime import datetime, timezone

import gspread
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


def _col_letter(n: int) -> str:
    """1 -> A, 2 -> B ... 只用在 row=1 的情況，所以可以簡單用 rowcol_to_a1 再去掉最後的 '1'。"""
    return gspread.utils.rowcol_to_a1(1, n)[:-1]


def _char_attendance_formula(row: int) -> str:
    return (f"=SUMIFS('{SHEET_SESSIONS}'!$N:$N,'{SHEET_SESSIONS}'!$D:$D,A{row},"
            f"'{SHEET_SESSIONS}'!$C:$C,C{row})")


def _char_earnings_formula(row: int) -> str:
    return (f"=SUMIFS('{SHEET_SESSIONS}'!$K:$K,'{SHEET_SESSIONS}'!$D:$D,A{row},"
            f"'{SHEET_SESSIONS}'!$C:$C,C{row},'{SHEET_SESSIONS}'!$H:$H,\"分潤\")")


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
    """這欄用成長式範圍（$A$2:A{row}），本來就隨列數自動擴大，不需要整欄參照。"""
    return f'=IF($D{row}="","",IF(COUNTIFS($A$2:A{row},A{row},$D$2:D{row},D{row})=1,1,0))'


class SheetsStore:
    """所有 Google Sheets 讀寫都透過這個類別，方法都是同步的（gspread 本身是同步函式庫），
    cogs 呼叫時要自己包 asyncio.to_thread。"""

    def __init__(self):
        b64 = os.environ["GOOGLE_SERVICE_ACCOUNT_B64"]
        creds_dict = json.loads(base64.b64decode(b64))
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        self.client = gspread.authorize(creds)
        self.sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.lock = asyncio.Lock()
        self._spreadsheet = None

    def _ss(self):
        if self._spreadsheet is None:
            self._spreadsheet = self.client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def ws(self, sheet_name: str):
        return self._ss().worksheet(sheet_name)

    # ---------- 通用 row 操作 ----------

    def get_rows(self, sheet_name: str) -> list:
        """回傳這張表所有非空白列，每筆是 dict（含 _row 實際列號），跳過完全空白的列。"""
        values = self.ws(sheet_name).get_all_values()
        if not values:
            return []
        headers = values[0]
        rows = []
        for i, row in enumerate(values[1:], start=2):
            if not any(cell.strip() for cell in row):
                continue
            d = {headers[j]: (row[j] if j < len(row) else "") for j in range(len(headers))}
            d["_row"] = i
            rows.append(d)
        return rows

    def find_first_empty_row(self, sheet_name: str, key_col_index: int = 1) -> int:
        """
        找到第一個「這個欄位是空的」列號，用來決定新資料要寫在哪一列。
        直接照這欄實際存在多少資料來判斷，不設任何上限，資料多長都找得到正確位置。
        """
        col_values = self.ws(sheet_name).col_values(key_col_index)
        for r in range(2, len(col_values) + 1):
            if not col_values[r - 1].strip():
                return r
        return len(col_values) + 1

    def write_row(self, sheet_name: str, row_number: int, values: list, start_col: int = 1):
        """把 values 依序寫進指定列，從 start_col 開始（1-indexed）。"""
        start = f"{_col_letter(start_col)}{row_number}"
        end = f"{_col_letter(start_col + len(values) - 1)}{row_number}"
        self.ws(sheet_name).update(f"{start}:{end}", [values])

    def append_rows_batch(self, sheet_name: str, values_list: list, start_col: int = 1, key_col_index: int = 1,
                           extra_formulas: list = None):
        """
        一次性把多列資料寫入（從第一個空白列開始，連續往下填），用「一次」API 呼叫寫完。
        extra_formulas: [(col_index, formula_fn), ...]，formula_fn(row_number) 回傳這一列該欄位的公式字串，
        確保機器人新增的每一列都自帶自己需要的公式，資料量再大也不會漏算。
        """
        if not values_list:
            return None
        start_row = self.find_first_empty_row(sheet_name, key_col_index=key_col_index)
        end_row = start_row + len(values_list) - 1
        start_letter = _col_letter(start_col)
        end_letter = _col_letter(start_col + len(values_list[0]) - 1)
        self.ws(sheet_name).update(f"{start_letter}{start_row}:{end_letter}{end_row}", values_list)

        if extra_formulas:
            updates = []
            for r in range(start_row, end_row + 1):
                for col, formula_fn in extra_formulas:
                    updates.append((r, col, [formula_fn(r)]))
            self.batch_update_cells(sheet_name, updates)

        return start_row

    def batch_update_cells(self, sheet_name: str, updates: list):
        """
        一次性更新多個（可能不連續的）位置。updates 是 [(row, start_col, values), ...]，
        用 gspread 的 batch_update 一次送出，避免每一列各打一次 API。
        """
        if not updates:
            return
        data = []
        for row, start_col, values in updates:
            start_letter = _col_letter(start_col)
            end_letter = _col_letter(start_col + len(values) - 1)
            data.append({"range": f"{start_letter}{row}:{end_letter}{row}", "values": [values]})
        self.ws(sheet_name).batch_update(data)

    def update_cell(self, sheet_name: str, row: int, col: int, value):
        self.ws(sheet_name).update_cell(row, col, value)

    def delete_row(self, sheet_name: str, row_number: int):
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

    def upsert_character(self, discord_id: str, display_name: str, char_name: str, job: str, position: str = "") -> str:
        """新增或更新一隻角色。同名角色（正規化後）視為同一隻，更新職業/位置；否則新增一列。"""
        key = normalize_name(char_name)
        rows = self.get_characters()
        for r in rows:
            if r.get("Discord ID", "").strip() == str(discord_id) and normalize_name(r.get("角色名稱", "")) == key:
                self.write_row(SHEET_CHARACTERS, r["_row"],
                                [str(discord_id), display_name, char_name, job, position])
                self.ensure_account_row(discord_id, display_name)
                return "updated"
        row_num = self._first_empty_row_from(rows)
        self.write_row(SHEET_CHARACTERS, row_num, [str(discord_id), display_name, char_name, job, position])
        self.write_row(
            SHEET_CHARACTERS, row_num,
            [_char_attendance_formula(row_num), _char_earnings_formula(row_num)],
            start_col=6,
        )
        self.ensure_account_row(discord_id, display_name)
        return "created"

    def delete_character(self, discord_id: str, index: int):
        """index 是這個帳號角色清單裡的第幾個（0-based，跟 !myprofiles 顯示的編號一致）。"""
        chars = self.get_user_characters(discord_id)
        if index < 0 or index >= len(chars):
            return None
        target = chars[index]
        self.delete_row(SHEET_CHARACTERS, target["_row"])
        return target

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
            start_col=6,
        )

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

    def get_session_rows(self, session_id: str) -> list:
        return [r for r in self.get_rows(SHEET_SESSIONS) if r.get("場次ID", "").strip() == session_id]

    def next_item_index(self, session_id: str) -> int:
        indices = [
            int(r["寶物編號"]) for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip().isdigit()
        ]
        return (max(indices) + 1) if indices else 0

    def record_attendance(self, session_id: str, when_iso: str, members: list, operator: str = ""):
        """
        單純記錄出席，不綁定任何寶物。確認隊員名單的當下就寫入這筆，
        這樣就算這一場全程沒有掉寶，出席次數也還是會被正確算到。
        operator：誰觸發了這次記錄（寫進 P 欄「操作者」，方便事後追查是誰記錄的）。
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
                (16, lambda r: operator),
            ],
        )

    def append_item_rows(self, session_id, when_iso, members, item_name, item_index, item_type, contributor,
                          operator: str = ""):
        """
        members: list of {"discord_id": str|None, "name": str} — 分潤類型才需要多列。
        分潤：每個 member 各一列；公會/自用：只需要一列（塔團/DiscordID 留空）。
        這裡會把這次要新增的所有列一次性打包成一個 API 呼叫寫入，不會一列一列分開打。
        operator：誰觸發了這次記錄（寫進 P 欄「操作者」，方便事後追查是誰記錄的）。
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
                (16, lambda r: operator),
            ],
        )

    def sell_item(self, session_id: str, item_index: int, amount: int) -> dict:
        """把指定場次+編號的寶物填上售出金額，分潤類型會平分給每一列。回傳結果摘要。"""
        target_rows = [
            r for r in self.get_session_rows(session_id)
            if r.get("寶物編號", "").strip() == str(item_index)
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

        self.batch_update_cells(SHEET_SESSIONS, updates)

        return {
            "ok": True, "item_name": item_name, "item_type": item_type,
            "amount": amount, "per_person": per_person, "n_rows": len(target_rows),
        }

    def claim_for_user(self, discord_id: str, session_id: str = None) -> dict:
        """把這個使用者所有（或指定場次）尚未領取的分潤列標記已領。回傳明細。"""
        now = now_str()
        total = 0.0
        details = []
        updates = []
        for r in self.get_rows(SHEET_SESSIONS):
            if r.get("類型") != "分潤":
                continue
            if r.get("Discord ID", "").strip() != str(discord_id):
                continue
            if session_id and r.get("場次ID", "").strip() != session_id:
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

        if updates:
            self.batch_update_cells(SHEET_SESSIONS, updates)
        return {"total": total, "details": details}

    def pending_for_user(self, discord_id: str) -> dict:
        total = 0.0
        details = []
        for r in self.get_rows(SHEET_SESSIONS):
            if r.get("類型") != "分潤":
                continue
            if r.get("Discord ID", "").strip() != str(discord_id):
                continue
            already = str(r.get("已領", "")).strip().upper() == "TRUE"
            sale = r.get("售出金額", "").strip()
            per_person = r.get("均分$$", "").strip()
            if already or not sale or not per_person:
                continue
            amt = float(per_person)
            total += amt
            details.append((r.get("場次ID", ""), r.get("掉落", ""), amt))
        return {"total": total, "details": details}

    def pending_sessions_for_user(self, discord_id: str) -> list:
        """回傳這個人有待領分潤的場次清單 [(session_id, amount), ...]。"""
        by_session = {}
        for session_id, item_name, amt in self.pending_for_user(discord_id)["details"]:
            by_session[session_id] = by_session.get(session_id, 0) + amt
        return list(by_session.items())

    def unclaimed_for_session(self, session_id: str) -> dict:
        """回傳這個場次裡，誰還沒領錢 {key: amount}，key 是 discord_id 或 "raw:名字"。"""
        pending = {}
        for r in self.get_session_rows(session_id):
            if r.get("類型") != "分潤":
                continue
            sale = r.get("售出金額", "").strip()
            if not sale:
                continue
            already = str(r.get("已領", "")).strip().upper() == "TRUE"
            if already:
                continue
            key = r.get("Discord ID", "").strip() or f"raw:{r.get('塔團', '')}"
            per_person = float(r.get("均分$$", "0") or 0)
            pending[key] = pending.get(key, 0) + per_person
        return pending

    def guild_fund_total(self) -> float:
        total = 0.0
        for r in self.get_rows(SHEET_SESSIONS):
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
            ws.update("A1:B1", [["頻道/討論串ID", "允許指令(逗號分隔)"]])
            return ws

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
                ws.update(f"A{i}:B{i}", [[key, ",".join(allowed)]])
                return
        row_num = len(values) + 1
        ws.update(f"A{row_num}:B{row_num}", [[key, ",".join(allowed)]])

    def clear_channel_rules(self, key: str):
        ws = self._rules_sheet()
        values = ws.get_all_values()
        for i, row in enumerate(values[1:], start=2):
            if row and row[0].strip() == key:
                ws.delete_rows(i)
                return
