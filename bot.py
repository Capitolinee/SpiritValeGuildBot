import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import discord
from discord.ext import commands
from google import genai
from gspread.exceptions import APIError

from store import SheetsStore
import audit

# --- 背景 HTTP 伺服器（讓 Render Web Service 保持健康連線） ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

threading.Thread(target=run_health_check_server, daemon=True).start()

# --- 環境變數 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")

required_env = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "GOOGLE_SHEET_ID": GOOGLE_SHEET_ID,
    "GOOGLE_SERVICE_ACCOUNT_B64": GOOGLE_SERVICE_ACCOUNT_B64,
}
missing = [k for k, v in required_env.items() if not v]
if missing:
    raise ValueError(f"⚠️ 缺少環境變數：{', '.join(missing)}")

GEMINI_MODEL = "gemini-3.6-flash"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # 需要在 Discord Developer Portal 開啟「Server Members Intent」

bot = commands.Bot(command_prefix="!", intents=intents)

# 掛在 bot 上，所有 cog 用 self.bot.store / self.bot.gemini 共用同一份
bot.store = SheetsStore()
bot.gemini = genai.Client(api_key=GEMINI_API_KEY)
bot.gemini_model = GEMINI_MODEL

# 目前這一場（全域唯一，不分頻道）；記憶體狀態，重啟會遺失，見 sessions cog 說明
bot.active_session = None  # {"id": str, "members": [{"discord_id":..,"name":..,"display_name":..}], "next_item_index": int}

EXTENSIONS = [
    "cogs.jobs",
    "cogs.profiles",
    "cogs.sessions",
    "cogs.access_control",
    "cogs.help",
]


@bot.event
async def on_command_error(ctx, error):
    # ephemeral=True：用 / 打的指令，錯誤訊息只有打的人看得到；用 ! 打的會照常公開顯示
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ 這個指令需要「管理伺服器」權限才能使用。", ephemeral=True)
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"⚠️ {error}", ephemeral=True)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ 缺少必要參數：`{error.param.name}`，請確認指令用法（打 /help 查看）。", ephemeral=True)
        return
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("⚠️ 找不到這個成員，請用 @提及 的方式指定對象。", ephemeral=True)
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ 參數格式錯誤：{error}", ephemeral=True)
        return
    # 用 / 打的指令，錯誤會被包好幾層（HybridCommandError → CommandInvokeError → 真正的錯誤），
    # 一路拆到最裡面，才能判斷真正的原因（你截圖裡顯示 "Command ... raised an exception" 就是沒拆乾淨）
    original = error
    while getattr(original, "original", None) is not None:
        original = original.original
    audit.error(f"指令錯誤：{ctx.command}", original, who=ctx.author.display_name)

    if isinstance(original, APIError) and (original.code in (408, 429) or original.code >= 500):
        await ctx.send(
            "⚠️ Google 試算表暫時無法連線（已經自動重試約 30 秒仍然失敗），"
            "這是 Google 那邊的狀況，資料沒有遺失，請過幾分鐘再試一次。",
            ephemeral=True,
        )
        return
    await ctx.send(f"❌ 執行 `{ctx.command}` 時發生錯誤：{original}", ephemeral=True)


@bot.event
async def on_ready():
    audit.system(f"機器人上線：{bot.user.name}")

    # 設定機器人在成員清單上顯示的狀態（就是「正在玩 ⋯⋯」那一行）
    # 想換樣式的話改 ACTIVITY_TYPE / ACTIVITY_TEXT 這兩個環境變數就好，不用改程式碼：
    #   playing   → 正在玩 ⋯⋯
    #   watching  → 正在觀看 ⋯⋯
    #   listening → 正在聽 ⋯⋯
    #   competing → 正在參加 ⋯⋯
    activity_type = os.getenv("ACTIVITY_TYPE", "playing").lower()
    activity_text = os.getenv("ACTIVITY_TEXT", "/loot 管理公會分潤")
    activity_map = {
        "playing": discord.ActivityType.playing,
        "watching": discord.ActivityType.watching,
        "listening": discord.ActivityType.listening,
        "competing": discord.ActivityType.competing,
    }
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=activity_map.get(activity_type, discord.ActivityType.playing),
                name=activity_text,
            )
        )
        print(f"✅ 已設定狀態顯示：{activity_type} {activity_text}", flush=True)
    except Exception as e:
        print(f"⚠️ 設定狀態顯示失敗：{e}", flush=True)

    try:
        synced = await bot.tree.sync()
        print(
            f"✅ 已同步 {len(synced)} 個「/」開頭的斜線指令到 Discord"
            f"（例如 /claim、/pending 這種用 / 打的指令，同步後 Discord 才會認得）",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ 同步斜線指令失敗：{e}", flush=True)

    # access_control cog 會在自己的 cog_load 裡載入規則快取，這裡不用重複做


async def main():
    async with bot:
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
