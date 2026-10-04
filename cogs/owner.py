"""
機器人擁有者專用：查看機器人在哪些伺服器、讓機器人離開某個伺服器。

兩道保護：
  1. 這兩個指令只會出現在「跟機器人的私訊」裡，任何伺服器的指令清單上都看不到
  2. 只有 Discord Developer Portal 上這個應用程式的擁有者能用，其他人就算叫出來也會被擋

用法：點機器人的頭像 → 傳送訊息，在私訊裡打 /servers 或 /leaveserver。
"""
import discord
from discord import app_commands
from discord.ext import commands

import audit

NOT_OWNER = "這個指令只有機器人的擁有者能用。"


def dm_only(func):
    """只在跟機器人的私訊裡出現（伺服器的指令清單上看不到）。"""
    func = app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)(func)
    return app_commands.allowed_installs(guilds=True, users=False)(func)


def guild_line(g: discord.Guild) -> str:
    return f"{g.name}（ID {g.id}｜{g.member_count or '?'} 人）"


class LeaveConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, guild: discord.Guild):
        super().__init__(timeout=120)
        self.owner_id, self.guild = owner_id, guild

    @discord.ui.button(label="🚪 確認離開", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_OWNER, ephemeral=True)
            return
        name, gid = self.guild.name, self.guild.id
        try:
            await self.guild.leave()
        except discord.HTTPException as e:
            await interaction.response.edit_message(content=f"⚠️ 離開失敗：{e}", view=None)
            return
        audit.audit("機器人離開伺服器", who=interaction.user.display_name, detail=f"{name}（{gid}）")
        await interaction.response.edit_message(content=f"✅ 機器人已經離開「{name}」。", view=None)
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="已取消，機器人沒有離開任何伺服器。", view=None)
        self.stop()


class LeaveSelect(discord.ui.Select):
    def __init__(self, owner_id: int, guilds: list):
        self.owner_id = owner_id
        self.guilds = {str(g.id): g for g in guilds[:25]}
        options = [discord.SelectOption(label=g.name[:100], value=str(g.id),
                                        description=f"ID {g.id}｜{g.member_count or '?'} 人"[:100])
                   for g in guilds[:25]]
        super().__init__(placeholder="選擇要讓機器人離開的伺服器", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_OWNER, ephemeral=True)
            return
        g = self.guilds[self.values[0]]
        text = f"**確定要讓機器人離開這個伺服器嗎？**\n{guild_line(g)}\n"
        # 你自己也在這個伺服器的話，多半就是你們公會自己的伺服器，特別提醒一次
        if g.get_member(self.owner_id) is not None:
            text += ("\n⚠️ **你自己也在這個伺服器裡**，這很可能就是你們公會自己的伺服器。"
                     "離開之後所有指令、公告按鈕都會停止運作，要重新邀請才會回來。\n")
        text += "\n離開之後，那邊的人就不能再使用這支機器人。"
        await interaction.response.edit_message(content=text, view=LeaveConfirmView(self.owner_id, g))


class LeaveView(discord.ui.View):
    def __init__(self, owner_id: int, guilds: list):
        super().__init__(timeout=180)
        self.add_item(LeaveSelect(owner_id, guilds))


class Owner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _check_owner(self, ctx) -> bool:
        if await self.bot.is_owner(ctx.author):
            return True
        await ctx.send(NOT_OWNER, ephemeral=True)
        return False

    @commands.hybrid_command(name="servers", description="擁有者專用：查看機器人在哪些伺服器（只在私訊裡能用）")
    @dm_only
    async def servers(self, ctx):
        if not await self._check_owner(ctx):
            return
        guilds = sorted(self.bot.guilds, key=lambda g: g.name)
        if not guilds:
            await ctx.send("機器人目前沒有在任何伺服器裡。", ephemeral=True)
            return
        lines = []
        for g in guilds:
            mine = "　← 你也在這裡" if g.get_member(ctx.author.id) is not None else ""
            lines.append(f"・{guild_line(g)}{mine}")
        await ctx.send(f"**🌐 機器人目前在 {len(guilds)} 個伺服器：**\n" + "\n".join(lines)
                       + "\n\n要讓機器人離開某一個，打 `/leaveserver`。", ephemeral=True)

    @commands.hybrid_command(name="leaveserver", description="擁有者專用：讓機器人離開某個伺服器（只在私訊裡能用）")
    @dm_only
    async def leave_server(self, ctx):
        if not await self._check_owner(ctx):
            return
        guilds = sorted(self.bot.guilds, key=lambda g: g.name)
        if not guilds:
            await ctx.send("機器人目前沒有在任何伺服器裡。", ephemeral=True)
            return
        more = f"（只列出 25 個，共 {len(guilds)} 個）" if len(guilds) > 25 else ""
        await ctx.send(f"請選擇要讓機器人離開的伺服器：{more}", view=LeaveView(ctx.author.id, guilds), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Owner(bot))
