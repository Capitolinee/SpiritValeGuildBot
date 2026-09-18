import asyncio
from discord.ext import commands

RULE_MANAGEMENT_COMMANDS = {
    "setthreadrules", "clearthreadrules", "threadrules",
    "setforumrules", "clearforumrules", "forumrules",
}


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
                f"（用 !setthreadrules、!setforumrules 設定過的規則，0 個代表目前沒有設定過任何限制，一切正常）",
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

    @commands.command(name="setthreadrules")
    async def set_thread_rules(self, ctx, *, commands_csv: str):
        """限制這個討論串/頻道自己只能使用列出的指令。用法：!setthreadrules myprofiles,profile"""
        key = str(ctx.channel.id)
        allowed = [c.strip().lstrip("!") for c in commands_csv.split(",") if c.strip()]
        if not allowed:
            await ctx.send("⚠️ 至少要指定一個指令名稱。")
            return
        async with self.store.lock:
            await asyncio.to_thread(self.store.set_channel_rules, key, allowed)
        self.rules_cache[key] = allowed
        await ctx.send(f"✅ 已限制這個討論串只能使用：{', '.join(allowed)}")

    @commands.command(name="clearthreadrules")
    async def clear_thread_rules(self, ctx):
        """解除這個討論串/頻道自己的指令限制。"""
        key = str(ctx.channel.id)
        async with self.store.lock:
            await asyncio.to_thread(self.store.clear_channel_rules, key)
        self.rules_cache.pop(key, None)
        await ctx.send("✅ 已解除這個討論串的專屬限制。")

    @commands.command(name="threadrules")
    async def show_thread_rules(self, ctx):
        """查看目前這個討論串/頻道實際套用的指令規則。"""
        channel = ctx.channel
        own = self.rules_cache.get(str(channel.id))
        if own:
            await ctx.send(f"這個討論串有專屬限制，只能使用：{', '.join(own)}")
            return
        parent = getattr(channel, "parent", None)
        if parent is not None:
            parent_rule = self.rules_cache.get(str(parent.id))
            if parent_rule:
                await ctx.send(f"這裡沒有專屬限制，但套用了上層論壇的規則，只能使用：{', '.join(parent_rule)}")
                return
        await ctx.send("這裡目前沒有任何指令限制，可以使用所有指令。")

    @commands.command(name="setforumrules")
    async def set_forum_rules(self, ctx, *, commands_csv: str):
        """限制整個論壇的預設指令（沒有專屬設定的討論串都會套用）。用法：!setforumrules item,sell,sessioninfo"""
        channel = ctx.channel
        parent = getattr(channel, "parent", None)
        key = str(parent.id) if parent is not None else str(channel.id)
        allowed = [c.strip().lstrip("!") for c in commands_csv.split(",") if c.strip()]
        if not allowed:
            await ctx.send("⚠️ 至少要指定一個指令名稱。")
            return
        async with self.store.lock:
            await asyncio.to_thread(self.store.set_channel_rules, key, allowed)
        self.rules_cache[key] = allowed
        await ctx.send(f"✅ 已設定這個論壇的預設規則：{', '.join(allowed)}")

    @commands.command(name="clearforumrules")
    async def clear_forum_rules(self, ctx):
        """解除整個論壇的預設指令限制。"""
        channel = ctx.channel
        parent = getattr(channel, "parent", None)
        key = str(parent.id) if parent is not None else str(channel.id)
        async with self.store.lock:
            await asyncio.to_thread(self.store.clear_channel_rules, key)
        self.rules_cache.pop(key, None)
        await ctx.send("✅ 已解除這個論壇的預設限制。")

    @commands.command(name="forumrules")
    async def show_forum_rules(self, ctx):
        """查看目前論壇的預設指令規則。"""
        channel = ctx.channel
        parent = getattr(channel, "parent", None)
        key = str(parent.id) if parent is not None else str(channel.id)
        allowed = self.rules_cache.get(key)
        if not allowed:
            await ctx.send("這個論壇目前沒有預設規則。")
            return
        await ctx.send(f"這個論壇的預設規則：{', '.join(allowed)}")


async def setup(bot):
    await bot.add_cog(AccessControl(bot))
