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

    # 統計每個名字被記錄的次數，並列出最近一次記錄時間
    summary = {}
    for m in members:
        name = m.get("name", "未知")
        summary.setdefault(name, {"count": 0, "last_seen": m.get("recorded_at", "")})
        summary[name]["count"] += 1
        if m.get("recorded_at", "") > summary[name]["last_seen"]:
            summary[name]["last_seen"] = m.get("recorded_at", "")

    lines = [
        f"- {name}（出現 {info['count']} 次，最近：{info['last_seen'][:10]}）"
        for name, info in sorted(summary.items())
    ]
    text = "\n".join(lines)

    # Discord 單則訊息有長度限制，太長就分段送
    for i in range(0, len(text), 1800):
        await ctx.send(f"**👥 隊員名單（共 {len(summary)} 人）：**\n```{text[i:i+1800]}```")


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

                    now = datetime.now(timezone.utc).isoformat()

                    async with github_lock:
                        records, sha = await asyncio.to_thread(github_get_records)

                        if kind == "member":
                            new_names = [n for n in payload if isinstance(n, str)]
                            for name in new_names:
                                records["members"].append({
                                    "name": name,
                                    "recorded_at": now,
                                    "recorded_by": str(message.author),
                                })
                            summary = "、".join(new_names) if new_names else "（無有效名字）"
                            commit_msg = f"新增隊員：{summary}"
                            reply = f"**✅ 已記錄隊員：**\n```{summary}```"

                        elif kind == "item":
                            valid_items = [
                                it for it in payload
                                if isinstance(it, dict) and "item" in it
                            ]
                            for it in valid_items:
                                records["items"].append({
                                    "item": it.get("item"),
                                    "amount": it.get("amount", 1),
                                    "recorded_at": now,
                                    "recorded_by": str(message.author),
                                })
                            summary_lines = "\n".join(
                                f"- {it.get('item')} x{it.get('amount', 1)}" for it in valid_items
                            ) or "（無有效寶物資料）"
                            commit_msg = f"新增寶物記錄：{len(valid_items)} 筆"
                            reply = f"**✅ 已記錄寶物：**\n```{summary_lines}```"

                        else:
                            await message.channel.send("⚠️ 未知的辨識類型，未寫入記錄。")
                            continue

                        await asyncio.to_thread(github_save_records, records, sha, commit_msg)

                    await message.channel.send(reply)

                except json.JSONDecodeError:
                    await message.channel.send("❌ Gemini 回傳的內容不是有效的 JSON，辨識失敗。")
                except requests.HTTPError as e:
                    await message.channel.send(f"❌ 寫入 GitHub 失敗：{e}")
                except Exception as e:
                    await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
