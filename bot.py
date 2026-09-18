import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import discord
from discord.ext import commands
from google import genai

from store import SheetsStore

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
]


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"⚠️ {error}")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ 缺少必要參數：`{error.param.name}`，請確認指令用法。")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ 參數格式錯誤：{error}")
        return
    original = getattr(error, "original", error)
    print(f"⚠️ 指令錯誤（{ctx.command}）：{original!r}", flush=True)
    await ctx.send(f"❌ 執行 `{ctx.command}` 時發生錯誤：{original}")


@bot.event
async def on_ready():
    print(f"🤖 機器人已順利上線：{bot.user.name}", flush=True)
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
