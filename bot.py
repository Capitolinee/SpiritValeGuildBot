import os
import io
import base64
import mimetypes
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord.ext import commands
from google import genai

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

# 啟動背景執行緒跑 HTTP Server
threading.Thread(target=run_health_check_server, daemon=True).start()

# --- 2. 讀取環境變數 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not GEMINI_API_KEY or not DISCORD_TOKEN:
    raise ValueError("⚠️ 找不到 GEMINI_API_KEY 或 DISCORD_TOKEN，請檢查 Environment 變數設定！")

# --- 3. 初始化 Gemini Client & Discord Bot ---
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 使用的模型名稱（Gemini 2.5 系列已對新用戶關閉，改用 3.6 Flash）
GEMINI_MODEL = "gemini-3.6-flash"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

PROMPT = """
你是一個遊戲掉落紀錄助手。請讀取這張圖片中的文字（包含繁體中文與英文），
只提取寶物名稱與數量。
請直接回傳 JSON 格式，格式範例如下：
[{"item": "寶物名稱", "amount": 1}]
如果圖片中沒有寶物資訊，請回傳空陣列 []。不要包含任何 Markdown 標記或額外說明。
"""


@bot.event
async def on_ready():
    print(f"🤖 機器人已順利上線：{bot.user.name}")
    # 除錯用：列出目前金鑰可用的模型（正式穩定後可以刪掉這段）
    try:
        for m in gemini_client.models.list():
            print(m.name, getattr(m, "supported_actions", None))
    except Exception as e:
        print(f"⚠️ 無法列出模型：{e}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.attachments:
        for attachment in message.attachments:
            if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                await message.channel.send("🔍 正在辨識圖片中的掉落寶物...")

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

                    result_text = interaction.output_text.strip()
                    await message.channel.send(f"**辨識結果：**\n```{result_text}```")

                except Exception as e:
                    await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
