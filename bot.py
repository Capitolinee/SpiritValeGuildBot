import os
import json
import base64
import mimetypes
import threading
import asyncio
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import discord
from discord.ext import commands
from google import genai
import requests

# --- 1. 背景 HTTP 伺服器（讓 Render Web Service 保持健康連線） ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return  # 關閉 HTTP log 保持主控台乾淨

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

# --- 2. 讀取環境變數 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")          # 格式："你的帳號/repo名稱"
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "data/records.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

required_env = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "GITHUB_REPO": GITHUB_REPO,
}
missing = [k for k, v in required_env.items() if not v]
if missing:
    raise ValueError(f"⚠️ 缺少環境變數：{', '.join(missing)}，請檢查 Render 的 Environment 設定！")

# --- 3. 初始化 Gemini Client & Discord Bot ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-3.6-flash"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # 需要在 Discord Developer Portal 開啟「Server Members Intent」，見部署說明
bot = commands.Bot(command_prefix="!", intents=intents)

# 同一時間只允許一個寫入動作，避免多筆訊息同時寫入 GitHub 造成 sha 衝突
github_lock = asyncio.Lock()

# 記錄每個頻道目前「進行中的場次」是哪一個（存在記憶體裡，機器人重啟會清空，
# 但場次本身的資料都在 GitHub 上，不會遺失，只是重啟後需要用新的隊員圖片重開一個場次）
ACTIVE_SESSIONS: dict = {}  # {channel_id(int): session_id(str)}


def new_session_id() -> str:
    return f"s{int(datetime.now(timezone.utc).timestamp())}"


def get_session(records: dict, session_id: str):
    for s in records.get("sessions", []):
        if s.get("id") == session_id:
            return s
    return None


def create_session(records: dict, channel_id: int, member_names: list) -> dict:
    """依這次確認的出席名單建立一個新場次，並嘗試對應到 Discord 帳號。"""
    now = datetime.now(timezone.utc).isoformat()
    members = []
    for raw_name in member_names:
        uid, matched_name = find_user_by_character_name(records, raw_name)
        members.append({
            "discord_user_id": uid,
            "name": matched_name or raw_name,
        })
    session = {
        "id": new_session_id(),
        "channel_id": str(channel_id),
        "created_at": now,
        "members": members,
        "items": [],
        "closed": False,
    }
    records.setdefault("sessions", []).append(session)
    return session

PROMPT = """
你是一個遊戲紀錄助手。請判斷這張圖片的內容類型，並依照下列規則回傳「純 JSON」，
不要包含任何 Markdown 標記（例如 ```json）或額外說明文字：

1. 如果圖片是「隊員名單／成員列表」，回傳：
{"type": "member", "data": ["隊員名字1", "隊員名字2"]}

2. 如果圖片是「掉落寶物記錄」，回傳：
{"type": "item", "data": [{"item": "寶物名稱", "amount": 1}]}

3. 如果兩者都不是，或圖片內容無法辨識，回傳：
{"type": "unknown", "data": []}

請完整讀取圖片中的繁體中文與英文文字後再判斷與提取。
"""


def _github_api_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def github_get_records():
    """從 GitHub 讀取現有的 JSON 記錄，回傳 (data_dict, sha)。檔案不存在時回傳空結構與 sha=None。"""
    resp = requests.get(
        _github_api_url(),
        headers=_github_headers(),
        params={"ref": GITHUB_BRANCH},
        timeout=15,
    )
    if resp.status_code == 404:
        return {"members": [], "items": [], "jobs": {}, "profiles": {}, "sessions": []}, None

    resp.raise_for_status()
    payload = resp.json()
    content = base64.b64decode(payload["content"]).decode("utf-8")
    sha = payload["sha"]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {"members": [], "items": [], "jobs": {}, "profiles": {}, "sessions": []}

    data.setdefault("members", [])
    data.setdefault("items", [])
    data.setdefault("jobs", {})       # {"職業名稱": {"tier": int, "parent": str|None, "image": str}}
    data.setdefault("profiles", {})   # {"discord_user_id": {"name":..., "job":..., "recorded_at":...}}
    data.setdefault("sessions", [])   # [{"id":..., "channel_id":..., "members":[...], "items":[...], "closed": bool}]
    return data, sha


def github_save_records(data: dict, sha, commit_message: str):
    """把更新後的 JSON 寫回 GitHub。"""
    new_content = json.dumps(data, ensure_ascii=False, indent=2)
    body = {
        "message": commit_message,
        "content": base64.b64encode(new_content.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha

    resp = requests.put(
        _github_api_url(),
        headers=_github_headers(),
        json=body,
        timeout=15,
    )
    resp.raise_for_status()


def parse_gemini_json(raw_text: str) -> dict:
    """清理並解析 Gemini 回傳的 JSON 字串。"""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


# 容易造成誤判的「形似字元」對照表：左邊會被視為跟右邊相同。
# 之後如果又發現新的形似字元組合，直接在這裡加一行就好。
CONFUSABLE_CHAR_MAP = {
    "ㄚ": "丫",   # 注音符號 ㄚ vs 中文字 丫
    "ㄧ": "一",   # 注音符號 ㄧ vs 中文字 一
    "ㄩ": "凵",   # 注音符號 ㄩ vs 部首 凵
    "O": "0",     # 英文字母 O vs 數字 0
    "l": "1",     # 英文小寫 l vs 數字 1
}


def normalize_name(name: str) -> str:
    """
    把名字正規化成用來「比對是否為同一人」的統一格式：
    - 轉小寫（英文大小寫視為相同）
    - 套用形似字元對照表
    - 去除頭尾空白
    注意：這只用來比對，實際存檔還是保留使用者原本輸入的寫法。
    """
    normalized = name.strip().lower()
    for confusable, canonical in CONFUSABLE_CHAR_MAP.items():
        normalized = normalized.replace(confusable.lower(), canonical.lower())
    return normalized


def find_user_by_character_name(records: dict, name: str):
    """
    依角色名字（正規化後）反查是哪個 Discord 使用者登記過這個角色。
    回傳 (discord_user_id, 登記時的原始名字) 或 (None, None)。
    """
    target_key = normalize_name(name)
    profiles = records.get("profiles", {})
    for user_id, entry in profiles.items():
        characters = [entry] if isinstance(entry, dict) else entry
        for c in characters:
            if normalize_name(c.get("name", "")) == target_key:
                return user_id, c.get("name")
    return None, None


async def resolve_member_display(m: dict, guild: discord.Guild = None) -> str:
    """
    列出隊員時的顯示文字：能對應到 Discord 帳號就顯示 Discord 顯示名稱，否則顯示原始角色名字。
    ID 一律是唯一、不會變動的識別碼；改暱稱不影響對應關係。
    """
    uid = m.get("discord_user_id")
    if not uid:
        return m.get("name", "未知")

    if not guild:
        return f"（使用者 {uid}）"

    member = guild.get_member(int(uid))
    if member:
        return member.display_name

    # 快取裡沒有，不代表這個人真的離開了，直接跟 Discord API 確認一次
    try:
        member = await guild.fetch_member(int(uid))
        return member.display_name
    except discord.NotFound:
        return f"（已離開的使用者 {uid}）"
    except Exception:
        return f"（使用者 {uid}）"


def upsert_member(records: dict, name: str, author: str, timestamp: str):
    """
    新增或更新一筆出席記錄。
    如果這個角色名字有對應到已登記的 Discord 帳號，出席次數會記在該帳號上
    （同一人換不同角色出席也會算同一人）；沒對應到的話就照角色名字去重（含形似字元正規化）。
    """
    user_id, _ = find_user_by_character_name(records, name)
    name_key = normalize_name(name)

    for m in records.setdefault("members", []):
        existing_uid = m.get("discord_user_id")
        if user_id:
            if existing_uid == user_id:
                m["count"] = m.get("count", 1) + 1
                m["recorded_at"] = timestamp
                m["recorded_by"] = author
                m["name"] = name  # 更新成這次辨識到的角色名字（該帳號最近使用的角色）
                return
        elif not existing_uid and normalize_name(m.get("name", "")) == name_key:
            m["count"] = m.get("count", 1) + 1
            m["recorded_at"] = timestamp
            m["recorded_by"] = author
            return

    records["members"].append({
        "name": name,
        "count": 1,
        "recorded_at": timestamp,
        "recorded_by": author,
        "discord_user_id": user_id,
    })


def rename_member(records: dict, index: int, new_name: str) -> str:
    """
    修改編號 index 的隊員名字。
    如果新名字正規化後跟另一筆既有記錄相同，會自動合併（次數相加），並回傳 'merged'；
    否則單純改名，回傳 'renamed'。
    """
    members = records.get("members", [])
    target = members[index]
    target_key = normalize_name(new_name)

    for i, m in enumerate(members):
        if i != index and normalize_name(m.get("name", "")) == target_key:
            m["count"] = m.get("count", 1) + target.get("count", 1)
            if target.get("recorded_at", "") > m.get("recorded_at", ""):
                m["recorded_at"] = target["recorded_at"]
                m["recorded_by"] = target.get("recorded_by")
            del members[index]
            return "merged"

    target["name"] = new_name
    return "renamed"


@bot.command(name="memberlist")
async def raw_member_list(ctx):
    """列出隊員記錄（每人只會有一筆，含編號，供修改／刪除使用）。"""
    try:
        records, _ = await asyncio.to_thread(github_get_records)
    except requests.HTTPError as e:
        await ctx.send(f"❌ 讀取記錄失敗：{e}")
        return

    members = records.get("members", [])
    if not members:
        await ctx.send("目前還沒有任何隊員記錄。")
        return

    lines = []
    for i, m in enumerate(members):
        display = await resolve_member_display(m, ctx.guild)
        lines.append(f"[{i}] {display}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）")
    text = "\n".join(lines)
    for i in range(0, len(text), 1800):
        await ctx.send(f"**📋 隊員記錄：**\n```{text[i:i+1800]}```")


@bot.command(name="itemlist")
async def raw_item_list(ctx):
    """列出寶物的原始記錄（含編號，供修改／刪除使用）。"""
    try:
        records, _ = await asyncio.to_thread(github_get_records)
    except requests.HTTPError as e:
        await ctx.send(f"❌ 讀取記錄失敗：{e}")
        return

    items = records.get("items", [])
    if not items:
        await ctx.send("目前還沒有任何寶物記錄。")
        return

    lines = [
        f"[{i}] {it.get('item', '未知')} x{it.get('amount', 1)}（{it.get('recorded_at', '')[:10]}）"
        for i, it in enumerate(items)
    ]
    text = "\n".join(lines)
    for i in range(0, len(text), 1800):
        await ctx.send(f"**📋 寶物原始記錄：**\n```{text[i:i+1800]}```")


@bot.command(name="editmember")
async def edit_member(ctx, index: int, *, new_name: str):
    """修改指定編號的隊員名字。若新名字與其他既有記錄重複，會自動合併次數。用法：!editmember 3 正確的名字"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        members = records.get("members", [])
        if index < 0 or index >= len(members):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !memberlist 確認編號。")
            return
        old_name = members[index].get("name")
        result = rename_member(records, index, new_name)
        await asyncio.to_thread(
            github_save_records, records, sha, f"修正隊員 [{index}]：{old_name} → {new_name}"
        )
    if result == "merged":
        await ctx.send(f"✅ 已將「{old_name}」合併進既有的「{new_name}」，次數已加總。")
    else:
        await ctx.send(f"✅ 已將 [{index}] 的「{old_name}」改為「{new_name}」")


@bot.command(name="delmember")
async def delete_member(ctx, index: int):
    """刪除指定編號的隊員記錄。用法：!delmember 3"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        members = records.get("members", [])
        if index < 0 or index >= len(members):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !memberlist 確認編號。")
            return
        removed = members.pop(index)
        await asyncio.to_thread(
            github_save_records, records, sha, f"刪除隊員 [{index}]：{removed.get('name')}"
        )
    await ctx.send(f"🗑️ 已刪除 [{index}]：{removed.get('name')}")


@bot.command(name="addmember")
async def add_member(ctx, *, name: str):
    """手動新增一筆隊員記錄（同名會自動疊加次數，不會重複建立）。用法：!addmember 隊員名字"""
    now = datetime.now(timezone.utc).isoformat()
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        upsert_member(records, name, str(ctx.author), now)
        await asyncio.to_thread(github_save_records, records, sha, f"手動新增隊員：{name}")
    await ctx.send(f"✅ 已記錄隊員：{name}")


@bot.command(name="edititem")
async def edit_item(ctx, index: int, new_amount: int, *, new_name: str):
    """修改指定編號的寶物名稱與數量。用法：!edititem 2 5 正確的寶物名稱"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        items = records.get("items", [])
        if index < 0 or index >= len(items):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !itemlist 確認編號。")
            return
        old = f"{items[index].get('item')} x{items[index].get('amount')}"
        items[index]["item"] = new_name
        items[index]["amount"] = new_amount
        await asyncio.to_thread(
            github_save_records, records, sha, f"修正寶物 [{index}]：{old} → {new_name} x{new_amount}"
        )
    await ctx.send(f"✅ 已將 [{index}] 改為「{new_name} x{new_amount}」")


@bot.command(name="delitem")
async def delete_item(ctx, index: int):
    """刪除指定編號的寶物記錄。用法：!delitem 2"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        items = records.get("items", [])
        if index < 0 or index >= len(items):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !itemlist 確認編號。")
            return
        removed = items.pop(index)
        await asyncio.to_thread(
            github_save_records, records, sha,
            f"刪除寶物 [{index}]：{removed.get('item')} x{removed.get('amount')}"
        )
    await ctx.send(f"🗑️ 已刪除 [{index}]：{removed.get('item')} x{removed.get('amount')}")


@bot.command(name="additem")
async def add_item(ctx, amount: int, *, name: str):
    """手動新增一筆寶物記錄。用法：!additem 3 寶物名稱"""
    now = datetime.now(timezone.utc).isoformat()
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        records.setdefault("items", []).append({
            "item": name,
            "amount": amount,
            "recorded_at": now,
            "recorded_by": str(ctx.author),
        })
        await asyncio.to_thread(github_save_records, records, sha, f"手動新增寶物：{name} x{amount}")
    await ctx.send(f"✅ 已新增寶物：{name} x{amount}")


@bot.command(name="members")
async def list_members(ctx):
    """查詢目前記錄的隊員名單。"""
    try:
        records, _ = await asyncio.to_thread(github_get_records)
    except requests.HTTPError as e:
        await ctx.send(f"❌ 讀取記錄失敗：{e}")
        return

    members = records.get("members", [])
    if not members:
        await ctx.send("目前還沒有任何隊員記錄。")
        return

    display_map = {}
    for m in members:
        display_map[id(m)] = await resolve_member_display(m, ctx.guild)

    lines = [
        f"- {display_map[id(m)]}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）"
        for m in sorted(members, key=lambda x: display_map[id(x)])
    ]
    text = "\n".join(lines)

    # Discord 單則訊息有長度限制，太長就分段送
    for i in range(0, len(text), 1800):
        await ctx.send(f"**👥 隊員名單（共 {len(members)} 人）：**\n```{text[i:i+1800]}```")


@bot.command(name="items")
async def list_items(ctx):
    """查詢目前記錄的寶物數量統計。"""
    try:
        records, _ = await asyncio.to_thread(github_get_records)
    except requests.HTTPError as e:
        await ctx.send(f"❌ 讀取記錄失敗：{e}")
        return

    items = records.get("items", [])
    if not items:
        await ctx.send("目前還沒有任何寶物記錄。")
        return

    totals = {}
    for it in items:
        name = it.get("item", "未知")
        amount = it.get("amount", 1) or 1
        totals[name] = totals.get(name, 0) + amount

    lines = [f"- {name} x{amount}" for name, amount in sorted(totals.items(), key=lambda x: -x[1])]
    text = "\n".join(lines)

    for i in range(0, len(text), 1800):
        await ctx.send(f"**💎 寶物總計（{len(totals)} 種）：**\n```{text[i:i+1800]}```")


class EditModal(discord.ui.Modal):
    """讓使用者在寫入 GitHub 前，手動修改辨識結果的彈出視窗。"""

    def __init__(self, view: "ConfirmView"):
        super().__init__(title="修改辨識結果")
        self.view_ref = view

        self.text_input = discord.ui.TextInput(
            label=view.edit_label(),
            style=discord.TextStyle.paragraph,
            default=view.to_editable_text(),
            required=True,
            max_length=2000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_payload = self.view_ref.parse_edited_text(self.text_input.value)
        except Exception:
            await interaction.response.send_message(
                "⚠️ 格式錯誤，請確認每行格式後再試一次（寶物記錄格式為：名稱,數量）。",
                ephemeral=True,
            )
            return

        if not new_payload:
            await interaction.response.send_message("⚠️ 內容是空的，未進行任何記錄。", ephemeral=True)
            return

        self.view_ref.payload = new_payload
        reply = await self.view_ref.save(str(interaction.user))
        await interaction.response.edit_message(content=reply, view=None)
        self.view_ref.stop()


class ConfirmView(discord.ui.View):
    """辨識完成後，讓使用者用按鈕確認正確或修改後再寫入 GitHub。"""

    def __init__(self, kind: str, payload: list, author_id: int, channel_id: int):
        super().__init__(timeout=300)  # 5 分鐘沒操作就失效
        self.kind = kind
        self.payload = payload
        self.author_id = author_id
        self.channel_id = channel_id
        self.message: discord.Message | None = None

    def preview_text(self) -> str:
        if self.kind == "member":
            body = "、".join(self.payload) if self.payload else "（無）"
            return f"**🔍 辨識為隊員名單：**\n```{body}```\n請確認是否正確？"
        lines = "\n".join(f"- {it.get('item')} x{it.get('amount', 1)}" for it in self.payload)
        return f"**🔍 辨識為寶物記錄：**\n```{lines or '（無）'}```\n請確認是否正確？"

    def edit_label(self) -> str:
        return "每行一個隊員名字" if self.kind == "member" else "格式：寶物名稱,數量（每行一筆）"

    def to_editable_text(self) -> str:
        if self.kind == "member":
            return "\n".join(self.payload)
        return "\n".join(f"{it.get('item')},{it.get('amount', 1)}" for it in self.payload)

    def parse_edited_text(self, text: str):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if self.kind == "member":
            return lines
        result = []
        for line in lines:
            parts = line.split(",")
            name = parts[0].strip()
            amount = 1
            if len(parts) > 1 and parts[1].strip().lstrip("-").isdigit():
                amount = int(parts[1].strip())
            result.append({"item": name, "amount": amount})
        return result

    async def save(self, author: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        async with github_lock:
            records, sha = await asyncio.to_thread(github_get_records)

            if self.kind == "member":
                for name in self.payload:
                    if name:
                        upsert_member(records, name, author, now)
                summary = "、".join(self.payload) if self.payload else "（無）"
                commit_msg = f"新增隊員：{summary}"
                reply = f"**✅ 已記錄隊員：**\n```{summary}```"

                # 依這次確認的出席名單開一個新場次，之後的寶物可以掛在這場底下結算分潤
                session = create_session(records, self.channel_id, self.payload)
                ACTIVE_SESSIONS[self.channel_id] = session["id"]
                commit_msg += f"，開啟場次 {session['id']}"
                reply += (
                    f"\n\n📌 已建立場次 `{session['id']}`，接下來可以用 `!item 寶物名稱` "
                    f"或上傳寶物圖片記錄掉落，賣掉後用 `!sell 金額 寶物名稱` 結算分潤。"
                )
            else:
                for it in self.payload:
                    records["items"].append({
                        "item": it.get("item"),
                        "amount": it.get("amount", 1),
                        "recorded_at": now,
                        "recorded_by": author,
                    })
                lines = "\n".join(f"- {it.get('item')} x{it.get('amount', 1)}" for it in self.payload) or "（無）"
                commit_msg = f"新增寶物記錄：{len(self.payload)} 筆"
                reply = f"**✅ 已記錄寶物：**\n```{lines}```"

                # 如果這個頻道有進行中的場次，同時把寶物掛進場次的清單裡
                active_id = ACTIVE_SESSIONS.get(self.channel_id)
                if active_id:
                    session = get_session(records, active_id)
                    if session and not session.get("closed"):
                        for it in self.payload:
                            session.setdefault("items", []).append({
                                "name": it.get("item"),
                                "recorded_at": now,
                                "sold": False,
                                "sale_amount": None,
                                "per_person": None,
                                "claims": {},
                            })
                        commit_msg += f"，掛入場次 {active_id}"
                        reply += f"\n\n📌 已加入場次 `{active_id}` 的寶物清單。"

            await asyncio.to_thread(github_save_records, records, sha, commit_msg)
        return reply

    @discord.ui.button(label="✅ 確認正確", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有上傳圖片的人可以確認喔。", ephemeral=True)
            return
        reply = await self.save(str(interaction.user))
        await interaction.response.edit_message(content=reply, view=None)
        self.stop()

    @discord.ui.button(label="✏️ 修改後再存", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有上傳圖片的人可以修改喔。", ephemeral=True)
            return
        await interaction.response.send_modal(EditModal(self))

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(
                    content=self.message.content + "\n\n⏰ 已逾時未確認，這筆資料未寫入記錄。",
                    view=None,
                )
            except Exception:
                pass


class ClearConfirmView(discord.ui.View):
    """清除資料前的二次確認按鈕。"""

    def __init__(self, target: str, author_id: int):
        super().__init__(timeout=60)
        self.target = target  # "members" / "items" / "all"
        self.author_id = author_id
        self.confirmed = False
        self.message: discord.Message | None = None

    @discord.ui.button(label="🗑️ 確定清除", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有發起清除的人可以確認喔。", ephemeral=True)
            return

        async with github_lock:
            records, sha = await asyncio.to_thread(github_get_records)
            if self.target == "members":
                records["members"] = []
                commit_msg = "清除所有隊員記錄"
                reply = "🗑️ 已清除所有隊員記錄。"
            elif self.target == "items":
                records["items"] = []
                commit_msg = "清除所有寶物記錄"
                reply = "🗑️ 已清除所有寶物記錄。"
            else:
                records["members"] = []
                records["items"] = []
                commit_msg = "清除所有記錄（隊員＋寶物）"
                reply = "🗑️ 已清除所有隊員與寶物記錄。"
            await asyncio.to_thread(github_save_records, records, sha, commit_msg)

        self.confirmed = True
        await interaction.response.edit_message(content=reply, view=None)
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有發起清除的人可以取消喔。", ephemeral=True)
            return
        await interaction.response.edit_message(content="已取消，資料沒有被清除。", view=None)
        self.stop()

    async def on_timeout(self):
        if self.message and not self.confirmed:
            try:
                await self.message.edit(content="⏰ 已逾時，未進行清除。", view=None)
            except Exception:
                pass


@bot.command(name="dedupemembers")
async def dedupe_members(ctx):
    """掃描目前所有隊員記錄，把正規化後名字相同（例如形似字元造成的重複）的記錄自動合併。"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        members = records.get("members", [])

        groups = {}
        for m in members:
            key = normalize_name(m.get("name", ""))
            groups.setdefault(key, []).append(m)

        new_members = []
        merge_log = []
        for group in groups.values():
            if len(group) == 1:
                new_members.append(group[0])
                continue
            # 合併：次數相加，名字採用最近一次記錄時的寫法
            group_sorted = sorted(group, key=lambda x: x.get("recorded_at", ""))
            base = dict(group_sorted[-1])
            base["count"] = sum(g.get("count", 1) for g in group)
            new_members.append(base)
            names_involved = "、".join(sorted({g.get("name", "") for g in group}))
            merge_log.append(f"{names_involved} → {base.get('name')}（共 {base['count']} 次）")

        if not merge_log:
            await ctx.send("沒有發現重複的隊員記錄，不需要合併。")
            return

        records["members"] = new_members
        await asyncio.to_thread(github_save_records, records, sha, "自動合併重複隊員記錄（正規化去重）")

    text = "\n".join(merge_log)
    await ctx.send(f"✅ 已自動合併以下重複記錄：\n```{text}```")


@bot.command(name="syncmembers")
async def sync_members(ctx):
    """
    把現有的隊員記錄，依照目前登記的角色資料（!profile）重新對應到 Discord 帳號，
    並把同一人底下的記錄合併次數。適合在補登角色資料後執行一次。
    """
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        members = records.get("members", [])

        merged = {}
        for m in members:
            uid = m.get("discord_user_id")
            if not uid:
                found_uid, _ = find_user_by_character_name(records, m.get("name", ""))
                uid = found_uid

            key = uid if uid else f"name:{normalize_name(m.get('name', ''))}"

            if key in merged:
                base = merged[key]
                base["count"] = base.get("count", 1) + m.get("count", 1)
                if m.get("recorded_at", "") > base.get("recorded_at", ""):
                    base["recorded_at"] = m["recorded_at"]
                    base["recorded_by"] = m.get("recorded_by")
                    base["name"] = m.get("name", base.get("name"))
                if uid:
                    base["discord_user_id"] = uid
            else:
                new_entry = dict(m)
                if uid:
                    new_entry["discord_user_id"] = uid
                merged[key] = new_entry

        records["members"] = list(merged.values())
        await asyncio.to_thread(github_save_records, records, sha, "依角色資料重新同步隊員記錄")

    await ctx.send(f"✅ 已同步完成，目前共 {len(records['members'])} 筆隊員記錄。")


@bot.command(name="item")
async def add_item_to_session(ctx, *, text: str):
    """
    把寶物手動加進場次。
    用法：
      !item 寶物名稱            → 加進目前頻道「預設場次」
      !item 場次ID 寶物名稱     → 加進指定場次（場次ID用 !sessions 查）
    """
    parts = text.split(maxsplit=1)
    session_id = None
    item_name = text

    if len(parts) == 2 and parts[0].startswith("s") and parts[0][1:].isdigit():
        session_id, item_name = parts

    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)

        if not session_id:
            session_id = ACTIVE_SESSIONS.get(ctx.channel.id)
            if not session_id:
                await ctx.send("⚠️ 目前這個頻道沒有預設場次，請先上傳隊員圖片，或用 `!item 場次ID 寶物名稱` 指定場次。")
                return

        session = get_session(records, session_id)
        if not session or session.get("closed"):
            await ctx.send(f"⚠️ 找不到場次 `{session_id}`，或該場次已結束。")
            return

        session.setdefault("items", []).append({
            "name": item_name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "sold": False,
            "sale_amount": None,
            "per_person": None,
            "claims": {},
        })
        await asyncio.to_thread(
            github_save_records, records, sha, f"場次 {session_id} 新增寶物：{item_name}"
        )
    await ctx.send(f"✅ 已將「{item_name}」加入場次 `{session_id}` 的寶物清單。")


@bot.command(name="sell")
async def sell_item(ctx, *args):
    """
    把場次裡指定編號的寶物標記為已賣出，並平均分配給該場次的出席隊員。
    用法：
      !sell 編號 金額            → 對目前頻道「預設場次」結算
      !sell 場次ID 編號 金額     → 對指定場次結算（場次ID用 !sessions 查）
    """
    if len(args) == 2:
        session_id = ACTIVE_SESSIONS.get(ctx.channel.id)
        index_raw, amount_raw = args
        if not session_id:
            await ctx.send(
                "⚠️ 目前這個頻道沒有預設場次，請改用 `!sell 場次ID 編號 金額`"
                "（場次ID用 `!sessions` 查）。"
            )
            return
    elif len(args) == 3:
        session_id, index_raw, amount_raw = args
    else:
        await ctx.send("⚠️ 用法：`!sell 編號 金額` 或 `!sell 場次ID 編號 金額`")
        return

    try:
        index = int(index_raw)
        amount = int(amount_raw)
    except ValueError:
        await ctx.send("⚠️ 編號跟金額都必須是數字，確認一下順序有沒有打反。")
        return

    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        session = get_session(records, session_id)
        if not session:
            await ctx.send(f"⚠️ 找不到場次 `{session_id}`，用 `!sessions` 確認場次ID。")
            return

        items = session.get("items", [])
        if index < 0 or index >= len(items):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 `!sessioninfo {session_id}` 確認編號。")
            return

        target_item = items[index]
        if target_item.get("sold"):
            await ctx.send(f"⚠️ 編號 {index}「{target_item.get('name')}」已經賣過了，不能重複結算。")
            return

        members = session.get("members", [])
        if not members:
            await ctx.send("⚠️ 這個場次沒有出席名單，無法分配。")
            return

        per_person = amount / len(members)
        target_item["sold"] = True
        target_item["sale_amount"] = amount
        target_item["per_person"] = per_person
        target_item["sold_at"] = datetime.now(timezone.utc).isoformat()
        target_item["claims"] = {
            (m.get("discord_user_id") or f"raw:{m.get('name')}"): False
            for m in members
        }
        item_name = target_item.get("name")

        await asyncio.to_thread(
            github_save_records, records, sha,
            f"場次 {session_id}：[{index}] {item_name} 賣出 {amount}，每人分 {per_person:.2f}"
        )

    member_list = "、".join(m.get("name") for m in members)
    await ctx.send(
        f"💰 場次 `{session_id}` [{index}]「{item_name}」已賣出 **{amount}**，共 {len(members)} 人平分，"
        f"每人 **{per_person:.2f}**。\n出席名單：{member_list}\n"
        f"隊員可以用 `!claim` 領取自己的份額。"
    )


@bot.command(name="sessions")
async def list_sessions(ctx):
    """列出這個頻道所有場次（含已結束的），方便找出還沒賣完寶物的舊場次。"""
    records, _ = await asyncio.to_thread(github_get_records)
    channel_sessions = [
        s for s in records.get("sessions", [])
        if s.get("channel_id") == str(ctx.channel.id)
    ]
    if not channel_sessions:
        await ctx.send("這個頻道還沒有任何場次記錄。")
        return

    active_id = ACTIVE_SESSIONS.get(ctx.channel.id)
    lines = []
    for s in sorted(channel_sessions, key=lambda x: x.get("created_at", "")):
        items = s.get("items", [])
        unsold = sum(1 for it in items if not it.get("sold"))
        created_date = s.get("created_at", "")[:16].replace("T", " ")
        if s["id"] == active_id:
            tag = "🟢 目前預設場次"
        elif s.get("closed"):
            tag = "🔴 已結束"
        else:
            tag = "⚪ 未結束（非預設，需指定場次ID操作）"
        lines.append(f"{s['id']}｜{created_date}｜{tag}｜未賣出寶物：{unsold} 筆")

    text = "\n".join(lines)
    await ctx.send(f"**📅 場次列表：**\n```{text}```\n對舊場次操作時，記得在指令加上場次ID，例如 `!sell {channel_sessions[0]['id']} 0 3000`。")


@bot.command(name="sessioninfo")
async def session_info(ctx, session_id: str = None):
    """查看場次資訊（出席名單、寶物清單、賣出狀態）。不填場次ID時查目前預設場次。"""
    records, _ = await asyncio.to_thread(github_get_records)
    if not session_id:
        session_id = ACTIVE_SESSIONS.get(ctx.channel.id)
        if not session_id:
            await ctx.send("目前這個頻道沒有預設場次，用 `!sessions` 查看有哪些場次，或指定場次ID：`!sessioninfo 場次ID`。")
            return
    session = get_session(records, session_id)
    if not session:
        await ctx.send(f"找不到場次 `{session_id}`，用 `!sessions` 確認場次ID。")
        return

    member_names = "、".join(m.get("name", "未知") for m in session.get("members", []))
    created_date = session.get("created_at", "")[:16].replace("T", " ")
    lines = [
        f"場次 ID：{session['id']}（{'已結束' if session.get('closed') else '進行中'}）",
        f"開場時間：{created_date}",
        f"出席：{member_names}",
        "寶物：",
    ]
    items = session.get("items", [])
    if not items:
        lines.append("  （尚未記錄任何寶物）")
    for i, it in enumerate(items):
        recorded_date = it.get("recorded_at", "")[:16].replace("T", " ")
        if it.get("sold"):
            sold_date = it.get("sold_at", "")[:16].replace("T", " ")
            status = f"已賣 {it['sale_amount']}（每人 {it['per_person']:.2f}，賣出時間：{sold_date}）"
        else:
            status = "未賣出"
        lines.append(f"  [{i}] {it.get('name')}（記錄時間：{recorded_date}）：{status}")

    await ctx.send("```" + "\n".join(lines) + "```")


@bot.command(name="closesession")
async def close_session_cmd(ctx, session_id: str = None):
    """結束一個場次（資料不會刪除，只是不再接受新寶物）。不填場次ID時結束目前預設場次。"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)

        if not session_id:
            session_id = ACTIVE_SESSIONS.get(ctx.channel.id)
            if not session_id:
                await ctx.send("目前這個頻道沒有預設場次，用 `!sessions` 查看場次ID，或指定 `!closesession 場次ID`。")
                return

        session = get_session(records, session_id)
        if not session:
            await ctx.send(f"⚠️ 找不到場次 `{session_id}`。")
            return

        session["closed"] = True
        if ACTIVE_SESSIONS.get(ctx.channel.id) == session_id:
            ACTIVE_SESSIONS.pop(ctx.channel.id, None)
        await asyncio.to_thread(github_save_records, records, sha, f"結束場次 {session_id}")
    await ctx.send(f"✅ 已結束場次 `{session_id}`。")


class ClaimSelect(discord.ui.Select):
    def __init__(self, author_id: int, pending_sessions: list):
        options = [
            discord.SelectOption(label=f"{sid}（待領 {amt:.2f}）", value=sid)
            for sid, amt in pending_sessions[:24]
        ]
        options.append(discord.SelectOption(label="✅ 全部一起領取", value="__ALL__"))
        super().__init__(placeholder="選擇要領取哪一場", options=options, min_values=1, max_values=1)
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的領取，你可以自己打 `!claim` 喔。", ephemeral=True)
            return

        chosen = self.values[0]
        uid = str(interaction.user.id)

        async with github_lock:
            records, sha = await asyncio.to_thread(github_get_records)
            sessions_to_check = (
                records.get("sessions", []) if chosen == "__ALL__" else [get_session(records, chosen)]
            )

            total = 0.0
            details = []
            for session in sessions_to_check:
                if not session:
                    continue
                for it in session.get("items", []):
                    claims = it.get("claims", {})
                    if claims.get(uid) is False:
                        claims[uid] = True
                        total += it.get("per_person", 0) or 0
                        details.append(f"{session['id']}：{it.get('name')} +{it.get('per_person', 0):.2f}")

            if not details:
                await interaction.response.edit_message(content="沒有可領取的分潤了（可能剛被領過）。", view=None)
                return

            await asyncio.to_thread(github_save_records, records, sha, f"{interaction.user} 領取分潤")

        detail_text = "\n".join(details)
        await interaction.response.edit_message(
            content=f"✅ 已領取，共 **{total:.2f}**：\n```{detail_text}```", view=None
        )


class ClaimSelectView(discord.ui.View):
    def __init__(self, author_id: int, pending_sessions: list):
        super().__init__(timeout=120)
        self.add_item(ClaimSelect(author_id, pending_sessions))


async def _do_claim_session(ctx, uid: str, session_id: str):
    """實際執行對單一場次的領取動作。"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        session = get_session(records, session_id)
        if not session:
            await ctx.send(f"⚠️ 找不到場次 `{session_id}`。")
            return

        total = 0.0
        details = []
        for it in session.get("items", []):
            claims = it.get("claims", {})
            if claims.get(uid) is False:
                claims[uid] = True
                total += it.get("per_person", 0) or 0
                details.append(f"{session['id']}：{it.get('name')} +{it.get('per_person', 0):.2f}")

        if not details:
            await ctx.send(f"場次 `{session_id}` 沒有可領取的分潤。")
            return

        await asyncio.to_thread(github_save_records, records, sha, f"{ctx.author} 領取分潤")

    detail_text = "\n".join(details)
    await ctx.send(f"✅ 已領取，共 **{total:.2f}**：\n```{detail_text}```")


@bot.command(name="claim")
async def claim_payout(ctx, session_id: str = None):
    """
    領取自己尚未領取的分潤。
    用法：
      !claim            → 只有一場待領時直接領取；有多場待領時跳出選單讓你挑
      !claim 場次ID     → 直接領取指定場次（場次ID用 !sessions 查）
    """
    uid = str(ctx.author.id)

    if session_id:
        await _do_claim_session(ctx, uid, session_id)
        return

    records, _ = await asyncio.to_thread(github_get_records)
    pending_sessions = []
    for session in records.get("sessions", []):
        amt = 0.0
        has_pending = False
        for it in session.get("items", []):
            if it.get("claims", {}).get(uid) is False:
                has_pending = True
                amt += it.get("per_person", 0) or 0
        if has_pending:
            pending_sessions.append((session["id"], amt))

    if not pending_sessions:
        await ctx.send("目前沒有可領取的分潤。")
        return

    if len(pending_sessions) == 1:
        await _do_claim_session(ctx, uid, pending_sessions[0][0])
        return

    lines = "\n".join(f"- {sid}：待領 {amt:.2f}" for sid, amt in pending_sessions)
    view = ClaimSelectView(ctx.author.id, pending_sessions)
    await ctx.send(f"你有多場待領分潤，請選擇要領取哪一場：\n```{lines}```", view=view)


@bot.command(name="pending")
async def show_pending(ctx):
    """查看自己目前尚未領取的分潤總額與明細。"""
    uid = str(ctx.author.id)
    records, _ = await asyncio.to_thread(github_get_records)
    total = 0.0
    details = []
    for session in records.get("sessions", []):
        for it in session.get("items", []):
            claims = it.get("claims", {})
            if claims.get(uid) is False:
                total += it.get("per_person", 0) or 0
                details.append(f"{session['id']}：{it.get('name')}（{it.get('per_person', 0):.2f}）")

    if not details:
        await ctx.send("目前沒有待領取的分潤。")
        return

    text = "\n".join(details)
    await ctx.send(f"**💰 待領取分潤，共 {total:.2f}：**\n```{text}```")


@bot.command(name="unclaimed")
async def show_unclaimed(ctx, session_id: str = None):
    """查看場次裡還有誰沒領錢。不填場次ID時查目前預設場次。"""
    records, _ = await asyncio.to_thread(github_get_records)
    if not session_id:
        session_id = ACTIVE_SESSIONS.get(ctx.channel.id)
        if not session_id:
            await ctx.send("目前這個頻道沒有預設場次，用 `!sessions` 查看場次ID，或指定 `!unclaimed 場次ID`。")
            return
    session = get_session(records, session_id)
    if not session:
        await ctx.send(f"找不到場次 `{session_id}`。")
        return

    pending = {}
    for it in session.get("items", []):
        if not it.get("sold"):
            continue
        for key, claimed in it.get("claims", {}).items():
            if not claimed:
                pending[key] = pending.get(key, 0) + (it.get("per_person") or 0)

    if not pending:
        await ctx.send("✅ 這個場次目前沒有人有待領款項（可能都領完了，或還沒有寶物賣出）。")
        return

    lines = []
    for key, amount in pending.items():
        if key.startswith("raw:"):
            display = f"{key[4:]}（未綁定 Discord 帳號，需人工處理）"
        else:
            member = ctx.guild.get_member(int(key)) if ctx.guild else None
            if not member and ctx.guild:
                try:
                    member = await ctx.guild.fetch_member(int(key))
                except Exception:
                    member = None
            display = member.display_name if member else f"（使用者 {key}）"
        lines.append(f"- {display}：{amount:.2f}")

    text = "\n".join(lines)
    await ctx.send(f"**💸 尚未領款：**\n```{text}```")


@bot.command(name="clearmembers")
async def clear_members(ctx):
    """清除所有隊員記錄（需二次確認）。"""
    view = ClearConfirmView("members", ctx.author.id)
    sent = await ctx.send("⚠️ 確定要清除**所有隊員記錄**嗎？此動作無法復原。", view=view)
    view.message = sent


@bot.command(name="clearitems")
async def clear_items(ctx):
    """清除所有寶物記錄（需二次確認）。"""
    view = ClearConfirmView("items", ctx.author.id)
    sent = await ctx.send("⚠️ 確定要清除**所有寶物記錄**嗎？此動作無法復原。", view=view)
    view.message = sent


@bot.command(name="clearall")
async def clear_all(ctx):
    """清除所有隊員與寶物記錄（需二次確認）。"""
    view = ClearConfirmView("all", ctx.author.id)
    sent = await ctx.send("⚠️ 確定要清除**所有隊員與寶物記錄**嗎？此動作無法復原。", view=view)
    view.message = sent


def get_job_info(jobs: dict, name: str) -> dict:
    """相容處理：舊版本可能把職業存成單純的圖片網址字串，這裡統一轉成 dict 格式。"""
    info = jobs.get(name, {})
    if isinstance(info, str):
        return {"tier": 1, "parent": None, "image": info}
    return info


def get_tier1_jobs(jobs: dict) -> list:
    return [n for n in jobs if get_job_info(jobs, n).get("tier", 1) == 1]


def get_children_jobs(jobs: dict, parent_name: str) -> list:
    return [n for n in jobs if get_job_info(jobs, n).get("parent") == parent_name]


@bot.command(name="addjob")
async def add_job(ctx, job_name: str, *, options: str = ""):
    """
    新增或更新職業，可設定轉職層級與上一轉職業。
    用法範例：
    !addjob 戰士 tier=1
    !addjob 聖騎士 tier=2 parent=戰士
    !addjob 聖騎士 tier=2 parent=戰士 image=https://.../paladin.png
    （tier 預設為 1，image 可留空之後再補）
    """
    opts = {}
    for token in options.split():
        if "=" in token:
            k, v = token.split("=", 1)
            opts[k.strip().lower()] = v.strip()

    try:
        tier = int(opts.get("tier", 1))
    except ValueError:
        await ctx.send("⚠️ tier 必須是數字，例如 tier=1、tier=2。")
        return

    parent = opts.get("parent")
    image_url = opts.get("image")

    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        jobs = records.setdefault("jobs", {})

        if tier > 1 and not parent:
            await ctx.send("⚠️ 第 2 轉以上的職業需要指定 `parent=上一轉職業`，例如：`!addjob 聖騎士 tier=2 parent=戰士`")
            return

        if parent and parent not in jobs:
            await ctx.send(f"⚠️ 找不到上一轉職業「{parent}」，請先用 !jobs 確認名稱，或先建立那個職業。")
            return

        existing = get_job_info(jobs, job_name)
        jobs[job_name] = {
            "tier": tier,
            "parent": parent,
            "image": image_url if image_url is not None else existing.get("image", ""),
        }
        await asyncio.to_thread(
            github_save_records, records, sha,
            f"設定職業：{job_name}（第{tier}轉{f'，承接自 {parent}' if parent else ''}）"
        )

    detail = f"第 {tier} 轉" + (f"，承接自「{parent}」" if parent else "")
    await ctx.send(f"✅ 已設定職業「{job_name}」（{detail}）。")


@bot.command(name="deljob")
async def del_job(ctx, job_name: str):
    """刪除一個職業設定。用法：!deljob 戰士"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        jobs = records.get("jobs", {})
        if job_name not in jobs:
            await ctx.send(f"⚠️ 找不到職業「{job_name}」，用 !jobs 確認目前有哪些職業。")
            return

        children = get_children_jobs(jobs, job_name)
        if children:
            await ctx.send(
                f"⚠️ 「{job_name}」還有下一轉職業（{', '.join(children)}）承接自它，"
                f"請先處理那些職業（改設定或刪除）後再刪除「{job_name}」。"
            )
            return

        del jobs[job_name]
        await asyncio.to_thread(github_save_records, records, sha, f"刪除職業設定：{job_name}")
    await ctx.send(f"🗑️ 已刪除職業「{job_name}」。")


@bot.command(name="jobs")
async def list_jobs(ctx):
    """列出目前設定的職業樹（依轉職層級分組）。"""
    records, _ = await asyncio.to_thread(github_get_records)
    jobs = records.get("jobs", {})
    if not jobs:
        await ctx.send("目前還沒有設定任何職業，用 `!addjob 職業名稱 tier=1` 新增第一個職業。")
        return

    by_tier = {}
    for name in jobs:
        info = get_job_info(jobs, name)
        by_tier.setdefault(info.get("tier", 1), []).append((name, info.get("parent")))

    lines = []
    for tier in sorted(by_tier):
        lines.append(f"【第 {tier} 轉】")
        for name, parent in sorted(by_tier[tier]):
            suffix = f"（承接自 {parent}）" if parent else ""
            lines.append(f"  - {name}{suffix}")
    text = "\n".join(lines)
    await ctx.send(f"**⚔️ 職業樹：**\n```{text}```")


class NameModal(discord.ui.Modal):
    """輸入名字用的彈出視窗，送出後接著跳出職業選單。"""

    def __init__(self):
        super().__init__(title="設定你的角色資料")
        self.name_input = discord.ui.TextInput(
            label="你的名字",
            placeholder="手動輸入你的角色名字",
            required=True,
            max_length=50,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        records, _ = await asyncio.to_thread(github_get_records)
        jobs = records.get("jobs", {})

        if not jobs:
            await interaction.response.send_message(
                "⚠️ 目前還沒有設定任何職業，請先請管理員用 `!addjob 職業名稱 tier=1` 新增職業。",
                ephemeral=True,
            )
            return

        view = JobSelectView(name, jobs)
        await interaction.response.send_message(
            f"名字：**{name}**\n請選擇你的職業：", view=view, ephemeral=True
        )


def get_user_characters(records: dict, user_id: str) -> list:
    """
    取得某個 Discord 使用者底下的角色清單（可能有多隻角色）。
    也相容舊格式（以前一個人只存一筆 dict，不是 list）。
    """
    profiles = records.setdefault("profiles", {})
    entry = profiles.get(user_id)
    if entry is None:
        entry = []
        profiles[user_id] = entry
    elif isinstance(entry, dict):
        entry = [entry]
        profiles[user_id] = entry
    return entry


async def finalize_profile(interaction: discord.Interaction, name: str, job: str, jobs: dict):
    """把最終選定的名字＋職業寫入 GitHub。同名角色會更新職業，不同名則新增一隻角色。"""
    info = get_job_info(jobs, job)
    image_url = info.get("image", "")
    now = datetime.now(timezone.utc).isoformat()
    user_id = str(interaction.user.id)
    target_key = normalize_name(name)

    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        characters = get_user_characters(records, user_id)

        updated = False
        for c in characters:
            if normalize_name(c.get("name", "")) == target_key:
                c["name"] = name
                c["job"] = job
                c["recorded_at"] = now
                updated = True
                break
        if not updated:
            characters.append({"name": name, "job": job, "recorded_at": now})

        await asyncio.to_thread(
            github_save_records, records, sha,
            f"{'更新' if updated else '新增'}角色資料：{name}（{job}）"
        )

    embed = discord.Embed(
        title="✅ 已更新角色資料" if updated else "✅ 已新增角色資料",
        description=f"名字：**{name}**\n職業：**{job}**",
        color=discord.Color.green(),
    )
    if image_url:
        embed.set_thumbnail(url=image_url)

    await interaction.response.edit_message(content=None, embed=embed, view=None)


class JobSelect(discord.ui.Select):
    """
    職業選單。current_job=None 時列出所有第一轉職業；
    否則列出 current_job 的下一轉選項，並附上「維持目前職業」選項。
    """

    def __init__(self, name: str, jobs: dict, current_job: str = None):
        self.name = name
        self.jobs = jobs
        self.current_job = current_job

        if current_job is None:
            candidates = get_tier1_jobs(jobs)
            placeholder = "選擇你的職業（第一轉）"
        else:
            candidates = get_children_jobs(jobs, current_job)
            placeholder = f"選擇「{current_job}」的下一轉（或維持不轉職）"

        options = [discord.SelectOption(label=c) for c in candidates[:24]]
        if current_job is not None:
            options.append(discord.SelectOption(label=f"維持「{current_job}」，不再轉職", value="__STOP__"))

        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]

        if chosen == "__STOP__":
            await finalize_profile(interaction, self.name, self.current_job, self.jobs)
            return

        children = get_children_jobs(self.jobs, chosen)
        if children:
            view = JobSelectView(self.name, self.jobs, current_job=chosen)
            await interaction.response.edit_message(
                content=f"名字：**{self.name}**\n已選擇：**{chosen}**\n請選擇下一轉職業，或維持目前職業：",
                view=view,
            )
        else:
            await finalize_profile(interaction, self.name, chosen, self.jobs)


class JobSelectView(discord.ui.View):
    def __init__(self, name: str, jobs: dict, current_job: str = None):
        super().__init__(timeout=120)
        self.add_item(JobSelect(name, jobs, current_job=current_job))


class StartProfileView(discord.ui.View):
    """!profile 指令送出的起始按鈕，按下才會跳出輸入名字的視窗。"""

    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    @discord.ui.button(label="📝 設定角色資料", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "這個按鈕是給發起的人用的，你可以自己打 `!profile` 喔。", ephemeral=True
            )
            return
        await interaction.response.send_modal(NameModal())


@bot.command(name="profile")
async def set_profile(ctx):
    """設定你自己的角色資料（名字＋職業）。"""
    view = StartProfileView(ctx.author.id)
    await ctx.send(f"{ctx.author.mention} 點下面的按鈕開始設定你的角色資料：", view=view)


@bot.command(name="profiles")
async def list_profiles(ctx):
    """列出目前所有人設定的角色資料（每人可能有多隻角色）。"""
    records, _ = await asyncio.to_thread(github_get_records)
    profiles = records.get("profiles", {})
    if not profiles:
        await ctx.send("目前還沒有人設定角色資料，用 `!profile` 開始設定。")
        return

    lines = []
    for user_id, entry in profiles.items():
        characters = [entry] if isinstance(entry, dict) else entry
        if not characters:
            continue
        char_text = "、".join(f"{c.get('name')}（{c.get('job')}）" for c in characters)
        lines.append(f"<@{user_id}>：{char_text}")

    text = "\n".join(lines)
    for i in range(0, len(text), 1800):
        await ctx.send(f"**🧑‍🤝‍🧑 角色資料：**\n{text[i:i+1800]}")


@bot.command(name="myprofiles")
async def my_profiles(ctx):
    """列出自己名下的所有角色（含編號，供 !delprofile 刪除使用）。"""
    records, _ = await asyncio.to_thread(github_get_records)
    characters = get_user_characters(records, str(ctx.author.id))
    if not characters:
        await ctx.send("你還沒有設定任何角色，用 `!profile` 開始設定。")
        return

    lines = [
        f"[{i}] {c.get('name')}（{c.get('job')}）"
        for i, c in enumerate(characters)
    ]
    await ctx.send("**🧑 你目前的角色：**\n```" + "\n".join(lines) + "```")


@bot.command(name="delprofile")
async def delete_profile(ctx, index: int):
    """刪除自己名下指定編號的角色。用法：!delprofile 0（編號用 !myprofiles 查）"""
    async with github_lock:
        records, sha = await asyncio.to_thread(github_get_records)
        characters = get_user_characters(records, str(ctx.author.id))
        if index < 0 or index >= len(characters):
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !myprofiles 確認編號。")
            return
        removed = characters.pop(index)
        await asyncio.to_thread(
            github_save_records, records, sha,
            f"刪除角色：{removed.get('name')}（{removed.get('job')}）"
        )
    await ctx.send(f"🗑️ 已刪除角色：{removed.get('name')}（{removed.get('job')}）")


@bot.event
async def on_command_error(ctx, error):
    """全域指令錯誤處理：讓錯誤直接顯示在 Discord，而不是只默默記錄在 Render log。"""
    if isinstance(error, commands.CommandNotFound):
        return  # 忽略打錯的指令名稱
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ 缺少必要參數：`{error.param.name}`，請確認指令用法。")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ 參數格式錯誤：{error}")
        return

    # 其他未預期的錯誤，把細節印到 Render log 方便除錯，同時回報到 Discord
    original = getattr(error, "original", error)
    print(f"⚠️ 指令錯誤（{ctx.command}）：{original!r}", flush=True)
    await ctx.send(f"❌ 執行 `{ctx.command}` 時發生錯誤：{original}")


@bot.event
async def on_ready():
    print(f"🤖 機器人已順利上線：{bot.user.name}", flush=True)
    try:
        for m in gemini_client.models.list():
            print(m.name, getattr(m, "supported_actions", None), flush=True)
    except Exception as e:
        print(f"⚠️ 無法列出模型：{e}", flush=True)


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.attachments:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                await message.channel.send("🔍 正在辨識圖片中的內容...")

                try:
                    image_bytes = await attachment.read()
                    mime_type = mimetypes.guess_type(attachment.filename)[0] or "image/png"
                    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

                    interaction = gemini_client.interactions.create(
                        model=GEMINI_MODEL,
                        input=[
                            {"type": "text", "text": PROMPT},
                            {"type": "image", "data": image_b64, "mime_type": mime_type},
                        ],
                    )

                    parsed = parse_gemini_json(interaction.output_text)
                    kind = parsed.get("type", "unknown")
                    payload = parsed.get("data", [])

                    if kind == "unknown" or not payload:
                        await message.channel.send("⚠️ 無法判斷這張圖片是隊員名單還是寶物記錄，或內容為空。")
                        continue

                    if kind == "member":
                        clean_payload = [n for n in payload if isinstance(n, str) and n.strip()]
                    elif kind == "item":
                        clean_payload = [
                            it for it in payload
                            if isinstance(it, dict) and it.get("item")
                        ]
                    else:
                        clean_payload = []

                    if not clean_payload:
                        await message.channel.send("⚠️ 辨識結果內容無效，未寫入記錄。")
                        continue

                    view = ConfirmView(kind, clean_payload, message.author.id, message.channel.id)
                    sent = await message.channel.send(view.preview_text(), view=view)
                    view.message = sent

                except json.JSONDecodeError:
                    await message.channel.send("❌ Gemini 回傳的內容不是有效的 JSON，辨識失敗。")
                except requests.HTTPError as e:
                    await message.channel.send(f"❌ 寫入 GitHub 失敗：{e}")
                except Exception as e:
                    await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
