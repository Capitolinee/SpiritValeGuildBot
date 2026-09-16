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
bot = commands.Bot(command_prefix="!", intents=intents)

# 同一時間只允許一個寫入動作，避免多筆訊息同時寫入 GitHub 造成 sha 衝突
github_lock = asyncio.Lock()

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
        return {"members": [], "items": [], "jobs": {}, "profiles": {}}, None

    resp.raise_for_status()
    payload = resp.json()
    content = base64.b64decode(payload["content"]).decode("utf-8")
    sha = payload["sha"]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {"members": [], "items": [], "jobs": {}, "profiles": {}}

    data.setdefault("members", [])
    data.setdefault("items", [])
    data.setdefault("jobs", {})       # {"職業名稱": {"tier": int, "parent": str|None, "image": str}}
    data.setdefault("profiles", {})   # {"discord_user_id": {"name":..., "job":..., "recorded_at":...}}
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


def get_member_display(m: dict, guild: discord.Guild = None) -> str:
    """列出隊員時的顯示文字：能對應到 Discord 帳號就顯示 Discord 顯示名稱，否則顯示原始角色名字。"""
    uid = m.get("discord_user_id")
    if uid:
        member = guild.get_member(int(uid)) if guild else None
        if member:
            return member.display_name
        return f"（已離開的使用者 {uid}）"
    return m.get("name", "未知")


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

    lines = [
        f"[{i}] {get_member_display(m, ctx.guild)}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）"
        for i, m in enumerate(members)
    ]
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

    lines = [
        f"- {get_member_display(m, ctx.guild)}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）"
        for m in sorted(members, key=lambda x: get_member_display(x, ctx.guild))
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

    def __init__(self, kind: str, payload: list, author_id: int):
        super().__init__(timeout=300)  # 5 分鐘沒操作就失效
        self.kind = kind
        self.payload = payload
        self.author_id = author_id
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

                    view = ConfirmView(kind, clean_payload, message.author.id)
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
