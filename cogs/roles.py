"""
身分組按鈕：管理員設定好「哪個按鈕對應哪個身分組」，發一則公告，成員按一下拿到、再按一下移除，
可以同時拿好幾個。畫面只有按的人自己看得到。

按鈕是 discord.py 的「動態按鈕」：每個按鈕的 ID 裡直接帶著身分組編號（rolebtn:身分組ID），
機器人啟動時只要登記「這一類按鈕」一次，不管是哪個身分組、什麼時候發的公告、重啟幾次都接得上，
之後新增的身分組也不用重新登記。

安全：
  - 有管理員、管理伺服器、踢人、封鎖這類危險權限的身分組，不能設成按鈕（不然誰按誰就有管理權限）
  - 從設定裡移除的身分組，舊公告上的按鈕按下去也不會再發
  - 機器人沒有權限、或身分組排在機器人上面時，會清楚告訴使用者，不會默默失敗
"""
import asyncio
import re
import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import audit

MAX_BUTTONS = 25   # Discord 一則訊息最多 25 個按鈕

# 這些權限只要有一個，就不能設成「誰按誰拿」的身分組
DANGEROUS_PERMISSIONS = {
    "administrator": "管理員", "manage_guild": "管理伺服器", "manage_roles": "管理身分組",
    "manage_channels": "管理頻道", "manage_messages": "管理訊息", "manage_webhooks": "管理 Webhook",
    "kick_members": "踢出成員", "ban_members": "封鎖成員", "moderate_members": "禁言成員",
    "mention_everyone": "提及 @everyone", "manage_nicknames": "管理暱稱", "manage_expressions": "管理表情符號",
}


def is_valid_emoji(text: str) -> bool:
    """
    是不是 Discord 按鈕能用的表情符號：自訂表情符號（<:名稱:編號>），或一般的表情符號字元。
    discord.py 的 PartialEmoji.from_str 不會拒絕任何文字，所以要自己判斷；中文、英文字母這類文字一律不收。
    """
    if re.fullmatch(r"<a?:\w{2,32}:\d{15,20}>", text):
        return True
    if not text or len(text) > 16:
        return False
    has_symbol = False
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "So" or 0x1F000 <= ord(ch) <= 0x1FAFF:
            has_symbol = True                      # 表情符號本體
        elif ch in "\u200d\ufe0f\u20e3" or cat in ("Mn", "Me", "Sk") or ch in "0123456789#*":
            continue                               # 組合用的字元（膚色、變體、數字鍵帽 1️⃣）
        else:
            return False                           # 一般文字
    return has_symbol or "\u20e3" in text


def role_problem(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """這個身分組能不能設成按鈕／現在發不發得出去。可以就回傳 None，不行就回傳原因。"""
    if role.is_default():
        return "不能設定 @everyone。"
    if role.managed:
        return f"「{role.name}」是機器人或整合服務自動管理的身分組，不能手動發。"
    danger = [label for perm, label in DANGEROUS_PERMISSIONS.items() if getattr(role.permissions, perm, False)]
    if danger:
        return (f"「{role.name}」有這些權限：{'、'.join(danger)}。設成按鈕的話，任何人按一下就有這些權限，"
                f"為了安全不允許。請另外建一個沒有這些權限的身分組。")
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return "機器人沒有「管理身分組」權限，請到伺服器設定 → 身分組，幫機器人的身分組打開這個權限。"
    if role >= me.top_role:
        return (f"「{role.name}」排在機器人的身分組上面（或一樣高），Discord 不允許機器人發這個身分組。"
                f"請到伺服器設定 → 身分組，把機器人的身分組拖到「{role.name}」上方。")
    return None


class RoleToggleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolebtn:(?P<role_id>[0-9]{15,20})"):
    """按一下拿到身分組、再按一下移除。按鈕的 ID 裡帶著身分組編號，重啟後也接得上。"""

    def __init__(self, role_id: int, label: Optional[str] = None, emoji=None):
        super().__init__(discord.ui.Button(
            label=(label or "身分組")[:80], emoji=emoji or None,
            style=discord.ButtonStyle.secondary, custom_id=f"rolebtn:{role_id}",
        ))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["role_id"]), label=item.label, emoji=item.emoji)

    async def callback(self, interaction: discord.Interaction):
        # 動態按鈕出錯時 discord.py 只會默默寫進記錄，使用者只會看到「此互動失敗」，所以每種失敗都自己接住
        try:
            await self._toggle(interaction)
        except Exception as e:
            audit.error("身分組按鈕發生錯誤", e, who=interaction.user.display_name)
            msg = f"❌ 發生錯誤，請稍後再試或通知管理員：{e}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except Exception:
                pass

    async def _toggle(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        allowed = {b["role_id"] for b in getattr(interaction.client, "role_buttons", [])}
        if str(self.role_id) not in allowed:
            await interaction.response.send_message("這個身分組按鈕已經停用了，請找管理員確認。", ephemeral=True)
            return
        role = guild.get_role(self.role_id) if guild else None
        if role is None:
            await interaction.response.send_message("這個身分組已經不存在了，請找管理員更新公告。", ephemeral=True)
            return
        problem = role_problem(guild, role)
        if problem:
            await interaction.response.send_message(f"⚠️ 現在沒辦法發這個身分組：{problem}", ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="身分組按鈕：成員自己移除")
                text, action = f"已移除 {role.mention}。想再拿回來，再按一次就好。", "移除"
            else:
                await member.add_roles(role, reason="身分組按鈕：成員自己領取")
                text, action = f"✅ 已經給你 {role.mention}。想移除的話，再按一次就好。", "領取"
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Discord 拒絕了這個動作，通常是機器人的身分組排得不夠高，請通知管理員。", ephemeral=True)
            return
        audit.audit(f"身分組按鈕：{action}", who=member.display_name, detail=role.name)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    async def cog_load(self):
        self.bot.role_buttons = []
        try:
            self.bot.role_buttons = await asyncio.to_thread(self.store.get_role_buttons)
        except Exception as e:
            print(f"⚠️ 載入身分組按鈕設定失敗：{e}", flush=True)
        # 登記「這一類按鈕」，之前發過的公告、之後新增的身分組都接得上
        self.bot.add_dynamic_items(RoleToggleButton)

    async def _reload(self):
        self.bot.role_buttons = await asyncio.to_thread(self.store.get_role_buttons)

    @commands.hybrid_command(name="roleadd", description="管理員：設定一個身分組按鈕（按一下拿到、再按一下移除）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="要發的身分組", emoji="按鈕上的表情符號（選填）", group="分組名稱，不同分組可以發成不同公告（不填就是「預設」）")
    async def role_add(self, ctx, role: discord.Role, emoji: Optional[str] = None, group: Optional[str] = None):
        await ctx.defer(ephemeral=True)
        group = (group or "預設").strip()
        problem = role_problem(ctx.guild, role)
        if problem:
            await ctx.send(f"⚠️ 不能設定：{problem}", ephemeral=True)
            return
        emoji = (emoji or "").strip()
        if emoji and not is_valid_emoji(emoji):
            await ctx.send("⚠️ 表情符號格式不對，請直接貼上一個表情符號（例如 🛡️），或伺服器的自訂表情符號。", ephemeral=True)
            return
        same_group = [b for b in self.bot.role_buttons if b["group"] == group and b["role_id"] != str(role.id)]
        if len(same_group) >= MAX_BUTTONS:
            await ctx.send(f"⚠️ 「{group}」已經有 {MAX_BUTTONS} 個按鈕了（Discord 一則公告的上限），請換一個分組。", ephemeral=True)
            return
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.set_role_button, group, str(role.id), emoji, role.name)
            await self._reload()
        audit.audit("設定身分組按鈕", who=ctx.author.display_name, detail=f"{group}｜{role.name}｜{emoji or '（無）'}")
        await ctx.send(f"✅ 已{'新增' if result == 'created' else '更新'}「{group}」分組的按鈕：{emoji} {role.name}\n"
                       f"已經發出去的公告不會自動多這個按鈕，用 `/postrolepanel` 重發一次。", ephemeral=True)

    @commands.hybrid_command(name="roleremove", description="管理員：移除一個身分組按鈕（舊公告上的按鈕也會停用）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(role="要移除的身分組", group="分組名稱（不填就是「預設」）")
    async def role_remove(self, ctx, role: discord.Role, group: Optional[str] = None):
        await ctx.defer(ephemeral=True)
        group = (group or "預設").strip()
        async with self.store.lock:
            removed = await asyncio.to_thread(self.store.remove_role_button, group, str(role.id))
            await self._reload()
        if not removed:
            await ctx.send(f"「{group}」分組裡沒有 {role.name} 這個按鈕。", ephemeral=True)
            return
        audit.audit("移除身分組按鈕", who=ctx.author.display_name, detail=f"{group}｜{role.name}")
        await ctx.send(f"✅ 已移除「{group}」分組的 {role.name}。舊公告上這個按鈕按下去也不會再發了，"
                       f"想讓畫面整齊的話用 `/postrolepanel` 重發一次。", ephemeral=True)

    @commands.hybrid_command(name="rolelist", description="管理員：查看目前設定的身分組按鈕")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def role_list(self, ctx):
        await ctx.defer(ephemeral=True)
        await self._reload()
        if not self.bot.role_buttons:
            await ctx.send("目前沒有設定任何身分組按鈕，用 `/roleadd` 新增。", ephemeral=True)
            return
        groups = {}
        for b in self.bot.role_buttons:
            role = ctx.guild.get_role(int(b["role_id"]))
            name = role.name if role else f"（已不存在的身分組 {b['name'] or b['role_id']}）"
            warn = f"　⚠️ {role_problem(ctx.guild, role)}" if role and role_problem(ctx.guild, role) else ""
            groups.setdefault(b["group"], []).append(f"{b['emoji']} {name}{warn}".strip())
        text = "\n\n".join(f"**{g}**\n" + "\n".join(f"・{x}" for x in items) for g, items in groups.items())
        await ctx.send(f"**🏷️ 身分組按鈕設定：**\n{text}", ephemeral=True)

    @commands.hybrid_command(name="postrolepanel", description="管理員：發一則身分組按鈕公告（常駐，重啟也能用）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(group="要發哪一個分組（不填就是「預設」）", channel="發在哪個頻道（不填就是目前這個頻道）",
                           title="公告標題（選填）", description="公告內容（選填，換行打 \\n）")
    async def post_role_panel(self, ctx, group: Optional[str] = None, channel: Optional[discord.TextChannel] = None,
                              title: Optional[str] = None, description: Optional[str] = None):
        await ctx.defer(ephemeral=True)
        group = (group or "預設").strip()
        target = channel or ctx.channel
        await self._reload()
        buttons, skipped = [], []
        for b in self.bot.role_buttons:
            if b["group"] != group:
                continue
            role = ctx.guild.get_role(int(b["role_id"]))
            problem = "身分組已經不存在" if role is None else role_problem(ctx.guild, role)
            if problem:
                skipped.append(f"{b['name'] or b['role_id']}：{problem}")
                continue
            buttons.append(RoleToggleButton(role.id, label=role.name, emoji=b["emoji"] or None))
        if not buttons:
            msg = f"「{group}」分組沒有可以發的身分組按鈕，先用 `/roleadd` 設定。"
            if skipped:
                msg += "\n以下這些被略過：\n" + "\n".join(skipped)
            await ctx.send(msg, ephemeral=True)
            return
        view = discord.ui.View(timeout=None)
        for item in buttons[:MAX_BUTTONS]:
            view.add_item(item)
        embed = discord.Embed(
            title=(title or "🏷️ 選擇你的身分組")[:256],
            description=(description.replace("\\n", "\n") if description else
                         "按下面的按鈕拿到身分組，再按一次就會移除，可以同時選好幾個。\n按了之後的訊息只有你自己看得到。")[:4000],
            color=discord.Color.blurple(),
        )
        try:
            msg = await target.send(embed=embed, view=view)
        except discord.Forbidden:
            await ctx.send(f"⚠️ 機器人在 {target.mention} 沒有「傳送訊息」或「嵌入連結」權限。", ephemeral=True)
            return
        except discord.HTTPException as e:
            await ctx.send(f"⚠️ Discord 拒絕了這則公告，最常見的原因是某個表情符號機器人用不了"
                           f"（例如別的伺服器的自訂表情符號）：{e}", ephemeral=True)
            return
        audit.audit("發佈身分組按鈕公告", who=ctx.author.display_name, detail=f"{group}｜#{target.name}｜{len(buttons)} 個按鈕")
        reply = f"✅ 已在 {target.mention} 發佈「{group}」身分組公告（{len(buttons)} 個按鈕）：{msg.jump_url}"
        if skipped:
            reply += "\n\n⚠️ 以下這些沒有放上去：\n" + "\n".join(skipped)
        await ctx.send(reply, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Roles(bot))
