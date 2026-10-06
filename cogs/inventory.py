"""
待售物品清單：一則常駐的訊息，列出試算表裡還沒賣掉的寶物（跟 /sell 選單列出來的完全一致）。

即時更新：任何寫入場次記錄的動作（記錄寶物、賣出、免費給人、刪除記錄、捐獻⋯⋯）完成後都會通知這裡，
等 3 秒（把連續的多次異動合併成一次）再更新清單。不是定時檢查，沒有異動就不會去動。
內容跟上次一樣的話也不會去改訊息（例如有人領錢，待售清單其實沒變）。

清單只改同一則訊息，不會重發；發在哪則訊息存在試算表的「系統狀態」分頁，重新部署後照樣找得到。
直接在試算表手動修改的話機器人不會知道，按清單上的「🔄 重新整理」就好。
"""
import asyncio
import io
from collections import Counter
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import audit
from helpers import now_str

TITLE = "📦 待售物品"
REFRESH_DELAY = 3          # 秒：連續的異動合併成一次更新
MAX_PANEL_CHARS = 3800     # 清單上最多放多少字（Discord 單則上限 4096，留一點空間給說明）


def summarize(items: list) -> list:
    """同名的寶物合併計數：[("槍手卡", 2), ("死靈卡", 1), ...]，照第一次出現的順序（新的在前）。"""
    counts = Counter(it["name"] for it in items)
    order = list(dict.fromkeys(it["name"] for it in items))
    return [(name, counts[name]) for name in order]


def panel_body(items: list) -> str:
    """清單內容（不含更新時間）：只顯示物品名稱，有重複的寫上數量。"""
    if not items:
        return "目前沒有待售的物品。"
    lines = [f"・{name}" + (f" ×{n}" if n > 1 else "") for name, n in summarize(items)]
    body, shown = [], 0
    for line in lines:
        if sum(len(x) + 1 for x in body) + len(line) > MAX_PANEL_CHARS:
            break
        body.append(line)
        shown += 1
    text = f"共 {len(items)} 件\n\n" + "\n".join(body)
    if shown < len(lines):
        text += f"\n…還有 {len(lines) - shown} 種，按「📋 查看完整清單」"
    return text


def panel_embed(items: list) -> discord.Embed:
    embed = discord.Embed(title=TITLE, description=panel_body(items), color=discord.Color.gold())
    embed.set_footer(text=f"最後更新：{now_str()}")
    return embed


class InventoryView(discord.ui.View):
    """清單上的兩個按鈕（永久型，custom_id 發出去之後就不能改）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="查看完整清單", emoji="📋", style=discord.ButtonStyle.secondary,
                       custom_id="inventory:full")
    async def full_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = await asyncio.to_thread(self.bot.store.list_unsold_items)
        if not items:
            await interaction.followup.send("目前沒有待售的物品。", ephemeral=True)
            return
        lines = [f"・{it['name']}　{(it['when'] or '')[:10]}　{it['session_id'] or '捐獻'}　{it['item_type']}"
                 for it in items]
        text = f"**📋 待售物品完整清單（共 {len(items)} 件）**\n" + "\n".join(lines)
        if len(text) <= 1900:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.followup.send(
                f"**📋 待售物品完整清單（共 {len(items)} 件）**，比較長，整份用附件傳送：",
                file=discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="待售物品.txt"),
                ephemeral=True)

    @discord.ui.button(label="重新整理", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="inventory:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog("Inventory")
        await cog.refresh(force=True)
        await interaction.followup.send("✅ 已經更新成最新的狀態。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        audit.error("待售清單按鈕發生錯誤", error, who=interaction.user.display_name)
        try:
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send(f"❌ 執行時發生錯誤：{error}", ephemeral=True)
        except Exception:
            pass


class Inventory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.panels = []            # [{"channel": 頻道ID, "message": 訊息ID}, ...]
        self._pending = None        # 等待中的更新（3 秒內的異動合併成一次）
        self._last_body = None      # 上次的清單內容，沒變就不改訊息

    async def cog_load(self):
        self.bot.add_view(InventoryView(self.bot))
        loop = asyncio.get_running_loop()
        # 寫入是在背景執行緒做的，切回主執行緒再排更新
        self.store.add_change_listener(lambda: loop.call_soon_threadsafe(self.schedule_refresh))
        try:
            self.panels = await asyncio.to_thread(self.store.load_inventory_panels)
        except Exception as e:
            print(f"⚠️ 讀取待售清單位置失敗：{e}", flush=True)

    def schedule_refresh(self):
        """場次記錄有異動時呼叫。3 秒內的多次異動只會更新一次。"""
        if not self.panels:
            return
        if self._pending is None or self._pending.done():
            self._pending = asyncio.get_running_loop().create_task(self._delayed_refresh())

    async def _delayed_refresh(self):
        await asyncio.sleep(REFRESH_DELAY)
        try:
            await self.refresh()
        except Exception as e:
            audit.error("更新待售清單失敗", e)

    async def refresh(self, force: bool = False):
        """讀最新的待售物品，更新每一則清單。內容沒變就不改（force=True 時一定改，順便更新時間）。"""
        if not self.panels:
            return
        items = await asyncio.to_thread(self.store.list_unsold_items)
        body = panel_body(items)
        if body == self._last_body and not force:
            return
        embed = panel_embed(items)
        alive = []
        for p in self.panels:
            channel = self.bot.get_channel(int(p["channel"]))
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(int(p["channel"]))
                msg = await channel.fetch_message(int(p["message"]))
                await msg.edit(embed=embed)
                alive.append(p)
            except discord.NotFound:
                continue                          # 訊息或頻道被刪掉了，之後不再更新它
            except discord.HTTPException as e:
                audit.error("更新待售清單時 Discord 回傳錯誤", e)
                alive.append(p)                   # 暫時的問題，下次再試
        self._last_body = body
        if alive != self.panels:
            self.panels = alive
            await asyncio.to_thread(self.store.save_inventory_panels, self.panels)

    @commands.hybrid_command(name="postinventory", description="管理員：發一則常駐的待售物品清單（有異動自動更新）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="發在哪個頻道（不填就是目前這個頻道）")
    async def post_inventory(self, ctx, channel: Optional[discord.TextChannel] = None):
        await ctx.defer(ephemeral=True)
        target = channel or ctx.channel
        items = await asyncio.to_thread(self.store.list_unsold_items)
        try:
            msg = await target.send(embed=panel_embed(items), view=InventoryView(self.bot))
        except discord.Forbidden:
            await ctx.send(f"⚠️ 機器人在 {target.mention} 沒有「傳送訊息」或「嵌入連結」權限。", ephemeral=True)
            return
        # 同一個頻道只保留一則：舊的那則刪掉，以後只更新新的
        old = [p for p in self.panels if int(p["channel"]) == target.id]
        for p in old:
            try:
                await (await target.fetch_message(int(p["message"]))).delete()
            except discord.HTTPException:
                pass
        self.panels = [p for p in self.panels if int(p["channel"]) != target.id] + \
                      [{"channel": target.id, "message": msg.id}]
        self._last_body = panel_body(items)
        await asyncio.to_thread(self.store.save_inventory_panels, self.panels)
        audit.audit("發佈待售物品清單", who=ctx.author.display_name, detail=f"#{target.name}")
        note = "（這個頻道原本的那則已經刪掉了）" if old else ""
        await ctx.send(f"✅ 已在 {target.mention} 發佈待售物品清單{note}，之後有異動會自動更新：{msg.jump_url}",
                       ephemeral=True)


async def setup(bot):
    await bot.add_cog(Inventory(bot))
