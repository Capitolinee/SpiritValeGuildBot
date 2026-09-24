import asyncio
import io
import os
from datetime import datetime, timezone, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import audit

RULE_MANAGEMENT_COMMANDS = {
    "setthreadrules", "clearthreadrules", "threadrules",
    "setforumrules", "clearforumrules", "forumrules",
}


def _parse_commands_csv(commands_csv: str) -> list:
    """把「profile,myprofiles」或「/profile, !myprofiles」這種輸入整理成純指令名稱清單。"""
    return [c.strip().lstrip("!/") for c in commands_csv.split(",") if c.strip().lstrip("!/")]


class AccessControl(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.rules_cache: dict = {}

    async def cog_load(self):
        try:
            self.rules_cache = await asyncio.to_thread(self.store.get_all_channel_rules)
            print(
                f"✅ 已載入 {len(self.rules_cache)} 個頻道/討論串的「指令限制規則」"
                f"（用 /setthreadrules、/setforumrules 設定過的規則，0 個代表目前沒有設定過任何限制，一切正常）",
                flush=True,
            )
        except Exception as e:
            print(f"⚠️ 載入頻道規則失敗：{e}", flush=True)

        @self.bot.check
        async def restrict_by_forum(ctx):
            if ctx.command is None:
                return True
            if ctx.command.name in RULE_MANAGEMENT_COMMANDS:
                return True
            allowed = self._resolve_allowed(ctx)
            if allowed and ctx.command.name not in allowed:
                raise commands.CheckFailure(f"這裡只開放這些指令：{', '.join(allowed)}")
            return True

    def _resolve_allowed(self, ctx):
        channel = ctx.channel
        thread_key = str(channel.id)
        if thread_key in self.rules_cache:
            return self.rules_cache[thread_key]
        parent = getattr(channel, "parent", None)
        if parent is not None:
            parent_key = str(parent.id)
            if parent_key in self.rules_cache:
                return self.rules_cache[parent_key]
        return None

    @staticmethod
    def _forum_key(ctx) -> str:
        parent = getattr(ctx.channel, "parent", None)
        return str(parent.id) if parent is not None else str(ctx.channel.id)

    @commands.hybrid_command(name="setthreadrules", description="限制這個討論串只能用哪些指令")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(commands_csv="允許的指令，用逗號分隔，例如 profile,myprofiles")
    async def set_thread_rules(self, ctx, *, commands_csv: str):
        """限制這個討論串/頻道自己只能使用列出的指令。"""
        allowed = _parse_commands_csv(commands_csv)
        if not allowed:
            await ctx.send("⚠️ 至少要指定一個指令名稱。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        key = str(ctx.channel.id)
        async with self.store.lock:
            await asyncio.to_thread(self.store.set_channel_rules, key, allowed)
        self.rules_cache[key] = allowed
        audit.audit("設定討論串指令限制", who=ctx.author.display_name, detail=f"#{ctx.channel}｜{','.join(allowed)}")
        await ctx.send(f"✅ 已限制這個討論串只能使用：{', '.join(allowed)}", ephemeral=True)

    @commands.hybrid_command(name="clearthreadrules", description="解除這個討論串的指令限制")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def clear_thread_rules(self, ctx):
        """解除這個討論串/頻道自己的指令限制。"""
        await ctx.defer(ephemeral=True)
        key = str(ctx.channel.id)
        async with self.store.lock:
            await asyncio.to_thread(self.store.clear_channel_rules, key)
        self.rules_cache.pop(key, None)
        audit.audit("解除討論串指令限制", who=ctx.author.display_name, detail=f"#{ctx.channel}")
        await ctx.send("✅ 已解除這個討論串的專屬限制。", ephemeral=True)

    @commands.hybrid_command(name="threadrules", description="查看這裡目前套用的指令限制")
    async def show_thread_rules(self, ctx):
        """查看目前這個討論串/頻道實際套用的指令規則。"""
        own = self.rules_cache.get(str(ctx.channel.id))
        if own:
            await ctx.send(f"這個討論串有專屬限制，只能使用：{', '.join(own)}", ephemeral=True)
            return
        parent = getattr(ctx.channel, "parent", None)
        if parent is not None:
            parent_rule = self.rules_cache.get(str(parent.id))
            if parent_rule:
                await ctx.send(
                    f"這裡沒有專屬限制，但套用了上層論壇的規則，只能使用：{', '.join(parent_rule)}",
                    ephemeral=True,
                )
                return
        await ctx.send("這裡目前沒有任何指令限制，可以使用所有指令。", ephemeral=True)

    @commands.hybrid_command(name="setforumrules", description="設定整個論壇的預設指令限制")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(commands_csv="允許的指令，用逗號分隔，例如 item,sell,sessioninfo")
    async def set_forum_rules(self, ctx, *, commands_csv: str):
        """限制整個論壇的預設指令（沒有專屬設定的討論串都會套用）。"""
        allowed = _parse_commands_csv(commands_csv)
        if not allowed:
            await ctx.send("⚠️ 至少要指定一個指令名稱。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        key = self._forum_key(ctx)
        async with self.store.lock:
            await asyncio.to_thread(self.store.set_channel_rules, key, allowed)
        self.rules_cache[key] = allowed
        audit.audit("設定論壇指令限制", who=ctx.author.display_name, detail=f"{key}｜{','.join(allowed)}")
        await ctx.send(f"✅ 已設定這個論壇的預設規則：{', '.join(allowed)}", ephemeral=True)

    @commands.hybrid_command(name="clearforumrules", description="解除整個論壇的預設指令限制")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def clear_forum_rules(self, ctx):
        """解除整個論壇的預設指令限制。"""
        await ctx.defer(ephemeral=True)
        key = self._forum_key(ctx)
        async with self.store.lock:
            await asyncio.to_thread(self.store.clear_channel_rules, key)
        self.rules_cache.pop(key, None)
        audit.audit("解除論壇指令限制", who=ctx.author.display_name, detail=key)
        await ctx.send("✅ 已解除這個論壇的預設限制。", ephemeral=True)

    @commands.hybrid_command(name="forumrules", description="查看論壇的預設指令限制")
    async def show_forum_rules(self, ctx):
        """查看目前論壇的預設指令規則。"""
        allowed = self.rules_cache.get(self._forum_key(ctx))
        if not allowed:
            await ctx.send("這個論壇目前沒有預設規則。", ephemeral=True)
            return
        await ctx.send(f"這個論壇的預設規則：{', '.join(allowed)}", ephemeral=True)

    @commands.hybrid_command(name="viewlogs", description="讀取稽核／錯誤記錄（需管理權限）")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        log_type="audit＝稽核記錄，error＝錯誤記錄",
        days_ago="幾天前（0＝今天）",
        lines="顯示最後幾行",
    )
    async def view_logs(self, ctx, log_type: Literal["audit", "error"] = "audit",
                        days_ago: int = 0, lines: int = 30):
        """直接在 Discord 讀取稽核記錄，不用去主機或裝任何工具。"""
        await ctx.defer(ephemeral=True)
        tw_tz = timezone(timedelta(hours=8))
        target_date = (datetime.now(tw_tz) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        filename = f"{log_type}-{target_date}.txt"
        path = os.path.join(audit.LOG_DIR, filename)

        if not os.path.exists(path):
            await ctx.send(
                f"⚠️ 找不到 `{filename}`，這天可能沒有任何記錄，或路徑不對。\n目前 log 存放路徑：`{audit.LOG_DIR}`",
                ephemeral=True,
            )
            return

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            await ctx.send(f"`{filename}` 是空的。", ephemeral=True)
            return

        tail = "\n".join(content.splitlines()[-lines:])
        if len(tail) <= 1800:
            await ctx.send(f"**📄 {filename}（最後 {lines} 行）：**\n```{tail}```", ephemeral=True)
        else:
            file = discord.File(io.BytesIO(content.encode("utf-8")), filename=filename)
            await ctx.send(f"**📄 {filename}**（內容較長，整份用附件傳送）：", file=file, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AccessControl(bot))
