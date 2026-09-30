import asyncio
import io
import os
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional

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

        # 圖片辨識頻道：放在 bot 上，圖片辨識那邊直接讀，不用每張圖都去讀試算表
        self.bot.ocr_channels = set()
        try:
            self.bot.ocr_channels = await asyncio.to_thread(self.store.get_ocr_channels)
            state = (f"只辨識 {len(self.bot.ocr_channels)} 個指定頻道的圖片" if self.bot.ocr_channels
                     else "還沒有設定辨識頻道，所有頻道的圖片都會辨識")
            print(f"✅ 圖片辨識：{state}", flush=True)
        except Exception as e:
            print(f"⚠️ 載入圖片辨識頻道失敗（先維持所有頻道都辨識）：{e}", flush=True)

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

    @commands.hybrid_command(name="ocrchannel", description="設定哪些頻道的圖片要辨識（省 Gemini 額度，需管理權限）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        action="開啟＝這個頻道的圖片要辨識，關閉＝不辨識，查看＝列出目前設定",
        channel="要設定的頻道（不填就是目前這個頻道；論壇的話底下每篇貼文都算）",
    )
    async def ocr_channel(self, ctx, action: Literal["開啟", "關閉", "查看"],
                          channel: Optional[discord.abc.GuildChannel] = None):
        """
        設定哪些頻道的圖片會送去 Gemini 辨識。
        完全沒設定任何頻道時，所有頻道都辨識；設定了至少一個之後，只辨識那些頻道。
        """
        await ctx.defer(ephemeral=True)
        target = channel or ctx.channel
        # 在論壇貼文（討論串）裡打的話，設定的是整個論壇
        if channel is None and getattr(target, "parent", None) is not None:
            target = target.parent

        if action == "查看":
            if not self.bot.ocr_channels:
                await ctx.send("目前**沒有設定**辨識頻道，所以**所有頻道**的圖片都會辨識。\n"
                               "用 `/ocrchannel action:開啟` 在出團頻道開啟之後，就只會辨識那些頻道。", ephemeral=True)
                return
            names = []
            for cid in sorted(self.bot.ocr_channels):
                ch = self.bot.get_channel(int(cid)) if cid.isdigit() else None
                names.append(ch.mention if ch else f"（找不到的頻道 {cid}）")
            await ctx.send("**📸 只有這些頻道的圖片會辨識：**\n" + "\n".join(names), ephemeral=True)
            return

        cid = str(target.id)
        async with self.store.lock:
            if action == "開啟":
                changed = await asyncio.to_thread(self.store.add_ocr_channel, cid)
                if changed:
                    self.bot.ocr_channels.add(cid)
            else:
                changed = await asyncio.to_thread(self.store.remove_ocr_channel, cid)
                if changed:
                    self.bot.ocr_channels.discard(cid)
        if changed:
            audit.audit(f"圖片辨識頻道{action}", who=ctx.author.display_name, detail=f"#{target.name}")

        if action == "開啟":
            msg = (f"✅ {target.mention} 的圖片會辨識。" if changed else f"{target.mention} 本來就有開啟辨識。")
            msg += f"\n現在只有 {len(self.bot.ocr_channels)} 個頻道的圖片會辨識，其他頻道的圖片機器人會忽略。"
        else:
            msg = (f"✅ {target.mention} 的圖片不再辨識。" if changed else f"{target.mention} 本來就沒有開啟辨識。")
            if not self.bot.ocr_channels:
                msg += "\n⚠️ 現在一個辨識頻道都沒有了，所以會變回**所有頻道**的圖片都辨識。"
        await ctx.send(msg, ephemeral=True)

    @commands.hybrid_command(name="repairformulas", description="重寫所有統計公式並依 Discord ID 排序角色資料，修好 #REF!（需管理權限）")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def repair_formulas(self, ctx):
        """把三張表的公式欄重寫成最新版本，並排序角色資料（有備份、前後比對、出錯自動還原）。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.repair_formulas)
        sort = result["角色資料"]
        others = {k: v for k, v in result.items() if k != "角色資料"}
        audit.audit("重寫統計公式＋排序角色資料", who=ctx.author.display_name,
                    detail=(f"角色資料 {'排序完成 ' + str(sort.get('rows', 0)) + ' 列' if sort.get('ok') else '排序失敗已還原：' + str(sort.get('reason'))}｜"
                            + "｜".join(f"{k} {v['rows']} 列" + (f"（有錯誤：{v['broken'][:10]}）" if v["broken"] else "")
                                       for k, v in others.items())))

        lines = []
        if sort.get("ok"):
            lines.append(f"✅ 角色資料：已依 Discord ID 排序，並重寫公式（{sort.get('rows', 0)} 列），前後資料比對一致。")
        else:
            lines.append(f"⚠️ 角色資料：排序時檢查沒有通過，已經自動還原成排序前的樣子，資料沒有遺失。\n　原因：{sort.get('reason')}")
        for label, key in [("場次記錄", "場次記錄"), ("帳號基本資料", "帳號基本資料")]:
            v = others[key]
            broken = sorted(set(v["broken"] + (others["場次記錄Q"]["broken"] if key == "場次記錄" else [])))
            if broken:
                lines.append(f"⚠️ {label}：公式已重寫（{v['rows']} 列），但這幾列算出錯誤，請檢查：第 {'、'.join(map(str, broken[:10]))} 列")
            else:
                lines.append(f"✅ {label}：公式已重寫（{v['rows']} 列），沒有計算錯誤。")
        await ctx.send("\n".join(lines), ephemeral=True)

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
