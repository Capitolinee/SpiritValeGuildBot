"""
公告按鈕面板：管理員用 /postpanel 在指定頻道發一則附按鈕的公告，成員點按鈕就能使用常用功能，
不用記指令。按鈕點下去的畫面只有點的人自己看得到，頻道裡只會留著那一則公告。

放在哪個頻道完全由指令決定：在哪裡打 /postpanel 就發在哪裡；不要了直接刪掉那則訊息。

按鈕是「永久型」：每個按鈕有固定的 custom_id，機器人啟動時用 bot.add_view 重新接上，
所以機器人重啟、重新部署之後，舊公告上的按鈕照樣能按，不用重發。
"""
import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import audit
from cogs.profiles import NameModal
from cogs.sessions import ClaimSelectView, claim_line

DEFAULT_TITLE = "📋 公會常用功能"
DEFAULT_DESCRIPTION = (
    "點下面的按鈕就能使用，畫面只有你自己看得到。\n\n"
    "📝 **登記角色**：登記你的遊戲角色、職業、戰鬥位置（有多隻角色就每隻都點一次）\n"
    "🧑 **查看我的角色**：看自己登記了哪些角色\n"
    "🕒 **設定可出席時間**：設定平日、假日能不能出席\n"
    "💰 **領取分潤**：領取出團分到的錢"
)


def _yes_no(value) -> bool:
    return str(value or "").strip().upper() == "TRUE"


# ---------- 🕒 設定可出席時間：選單＋備註視窗 ----------

class AvailabilityNoteModal(discord.ui.Modal):
    """填寫「其他時間備註」，送出時連同上面選單選的平日／假日一起儲存。"""

    def __init__(self, view: "AvailabilityView"):
        super().__init__(title="其他時間備註")
        self.view_ref = view
        self.note_input = discord.ui.TextInput(
            label="其他時間備註（例如 平日8:00~9:00）", style=discord.TextStyle.paragraph,
            default=view.note or None, required=False, max_length=200,
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.note = self.note_input.value.strip()
        await self.view_ref.save(interaction, include_note=True)


class AvailabilitySelect(discord.ui.Select):
    def __init__(self, field: str, label: str, current: bool, row: int):
        self.field = field
        options = [
            discord.SelectOption(label=f"{label}：可以出席", value="yes", emoji="✅", default=current),
            discord.SelectOption(label=f"{label}：不行", value="no", emoji="❌", default=not current),
        ]
        super().__init__(options=options, min_values=1, max_values=1, row=row)

    async def callback(self, interaction: discord.Interaction):
        view: AvailabilityView = self.view
        setattr(view, self.field, self.values[0] == "yes")
        for opt in self.options:  # 讓選單保持顯示剛選的
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(content=view.status_text("選好後按「💾 儲存」"), view=view)


class AvailabilityView(discord.ui.View):
    def __init__(self, bot, user_id: int, weekday: bool, weekend: bool, note: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.weekday, self.weekend, self.note = weekday, weekend, note
        self.add_item(AvailabilitySelect("weekday", "平日", weekday, row=0))
        self.add_item(AvailabilitySelect("weekend", "假日", weekend, row=1))

    def status_text(self, hint: str) -> str:
        return (f"**🕒 可出席時間**\n平日：{'✅' if self.weekday else '❌'}　假日：{'✅' if self.weekend else '❌'}\n"
                f"其他時間備註：{self.note or '（無）'}\n\n{hint}")

    async def save(self, interaction: discord.Interaction, include_note: bool):
        await interaction.response.defer()
        store = self.bot.store
        async with store.lock:
            await asyncio.to_thread(
                store.update_availability, str(interaction.user.id), interaction.user.display_name,
                self.weekday, self.weekend, self.note if include_note else None,
            )
        audit.audit("更新可出席時間（按鈕）", who=interaction.user.display_name,
                    detail=f"平日={self.weekday}｜假日={self.weekend}"
                           + (f"｜備註={self.note}" if include_note else ""))
        await interaction.edit_original_response(content=self.status_text("✅ 已儲存。"), view=None)
        self.stop()

    @discord.ui.button(label="💾 儲存", style=discord.ButtonStyle.success, row=2)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.save(interaction, include_note=False)

    @discord.ui.button(label="📝 填寫備註並儲存", style=discord.ButtonStyle.secondary, row=2)
    async def note_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AvailabilityNoteModal(self))


# ---------- 公告上的四個按鈕（永久型） ----------

class PanelView(discord.ui.View):
    """
    timeout=None ＋ 每個按鈕固定 custom_id，機器人重啟後用 bot.add_view 重新接上就能繼續用。
    custom_id 一旦發出去就不能改，改了舊公告上的按鈕會失效。
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="登記角色", emoji="📝", style=discord.ButtonStyle.primary,
                       custom_id="guildpanel:profile")
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NameModal(self.bot.store))

    @discord.ui.button(label="查看我的角色", emoji="🧑", style=discord.ButtonStyle.secondary,
                       custom_id="guildpanel:myprofiles")
    async def my_profiles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        chars = await asyncio.to_thread(self.bot.store.get_user_characters, str(interaction.user.id))
        if not chars:
            await interaction.followup.send("你還沒有登記任何角色，點「📝 登記角色」開始登記。", ephemeral=True)
            return
        lines = [f"[{i}] {c.get('角色名稱')}（{c.get('職業')}）" + (f"｜{c.get('位置')}" if c.get("位置") else "")
                 for i, c in enumerate(chars)]
        await interaction.followup.send("**🧑 你目前的角色：**\n```" + "\n".join(lines) + "```", ephemeral=True)

    @discord.ui.button(label="設定可出席時間", emoji="🕒", style=discord.ButtonStyle.secondary,
                       custom_id="guildpanel:availability")
    async def availability(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        stats = await asyncio.to_thread(self.bot.store.get_account_stats, str(interaction.user.id))
        view = AvailabilityView(self.bot, interaction.user.id, _yes_no(stats.get("平日可出席")),
                                _yes_no(stats.get("假日可出席")), stats.get("其他時間備註", "") or "")
        await interaction.followup.send(view.status_text("用下面的選單修改，選好後按「💾 儲存」。"),
                                        view=view, ephemeral=True)

    @discord.ui.button(label="領取分潤", emoji="💰", style=discord.ButtonStyle.success,
                       custom_id="guildpanel:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        items = await asyncio.to_thread(self.bot.store.pending_items_for_user, str(interaction.user.id))
        if not items:
            await interaction.followup.send("目前沒有可領取的分潤。", ephemeral=True)
            return
        lines = "\n".join(claim_line(it) for it in items)
        await interaction.followup.send(
            f"你有 {len(items)} 筆待領，請選擇要領取哪一樣：\n```{lines}```",
            view=ClaimSelectView(self.bot.store, interaction.user.id, items), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        audit.error("公告按鈕發生錯誤", error, who=interaction.user.display_name)
        try:
            send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await send(f"❌ 執行時發生錯誤：{error}", ephemeral=True)
        except Exception:
            pass


class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # 機器人啟動時重新接上按鈕，之前發過的公告按鈕才能繼續用
        self.bot.add_view(PanelView(self.bot))

    @commands.hybrid_command(name="postpanel", description="管理員：在頻道發一則附按鈕的公告（登記角色、領分潤等）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="要發在哪個頻道（不填就發在目前這個頻道）",
        title="公告標題（不填就用預設）",
        description="公告內容（不填就用預設的按鈕說明）",
    )
    async def post_panel(self, ctx, channel: Optional[discord.TextChannel] = None,
                         title: Optional[str] = None, description: Optional[str] = None):
        """在指定頻道發一則附按鈕的公告。想放在其他頻道就去那邊再發一次；不要了直接刪掉那則訊息。"""
        await ctx.defer(ephemeral=True)
        target = channel or ctx.channel
        embed = discord.Embed(
            title=(title or DEFAULT_TITLE)[:256],
            description=(description.replace("\\n", "\n") if description else DEFAULT_DESCRIPTION)[:4000],
            color=discord.Color.blurple(),
        )
        try:
            msg = await target.send(embed=embed, view=PanelView(self.bot))
        except discord.Forbidden:
            await ctx.send(f"⚠️ 機器人在 {target.mention} 沒有「傳送訊息」或「嵌入連結」權限，請先開權限再試一次。",
                           ephemeral=True)
            return
        audit.audit("發佈按鈕公告", who=ctx.author.display_name, detail=f"#{target.name}｜{msg.jump_url}")
        await ctx.send(f"✅ 已在 {target.mention} 發佈公告：{msg.jump_url}\n"
                       f"想放在其他頻道就去那邊再打一次 `/postpanel`；不要了直接刪掉那則訊息就好。",
                       ephemeral=True)


async def setup(bot):
    await bot.add_cog(Panel(bot))
