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
        return {"members": [], "items": []}, None

    resp.raise_for_status()
    payload = resp.json()
    content = base64.b64decode(payload["content"]).decode("utf-8")
    sha = payload["sha"]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        data = {"members": [], "items": []}

    data.setdefault("members", [])
    data.setdefault("items", [])
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


def upsert_member(records: dict, name: str, author: str, timestamp: str):
    """新增或更新一位隊員（依名字去重）。同名已存在時只更新次數與最近時間。"""
    for m in records.setdefault("members", []):
        if m.get("name") == name:
            m["count"] = m.get("count", 1) + 1
            m["recorded_at"] = timestamp
            m["recorded_by"] = author
            return
    records["members"].append({
        "name": name,
        "count": 1,
        "recorded_at": timestamp,
        "recorded_by": author,
    })


def rename_member(records: dict, index: int, new_name: str) -> str:
    """
    修改編號 index 的隊員名字。
    如果新名字跟另一筆既有記錄重複，會自動合併（次數相加），並回傳 'merged'；
    否則單純改名，回傳 'renamed'。
    """
    members = records.get("members", [])
    target = members[index]

    for i, m in enumerate(members):
        if i != index and m.get("name") == new_name:
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
        f"[{i}] {m.get('name', '未知')}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）"
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
        f"- {m.get('name', '未知')}（出現 {m.get('count', 1)} 次，最近：{m.get('recorded_at', '')[:10]}）"
        for m in sorted(members, key=lambda x: x.get("name", ""))
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
