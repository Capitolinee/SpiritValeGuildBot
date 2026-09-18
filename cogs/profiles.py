import asyncio
import discord
from discord.ext import commands

from cogs.jobs import get_tier1_jobs, get_children_jobs
from helpers import resolve_display_name


class NameModal(discord.ui.Modal):
    """輸入角色名字，送出後接著跳出職業選單。"""

    def __init__(self, store):
        super().__init__(title="設定你的角色資料")
        self.store = store
        self.name_input = discord.ui.TextInput(
            label="你的角色名字", placeholder="手動輸入", required=True, max_length=50
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        jobs = await asyncio.to_thread(self.store.get_jobs)
        if not jobs:
            await interaction.followup.send(
                "⚠️ 目前還沒有設定任何職業，請先請管理員用 `!addjob 職業名稱 tier=1` 新增職業。",
                ephemeral=True,
            )
            return
        view = JobSelectView(self.store, name, jobs)
        await interaction.followup.send(f"名字：**{name}**\n請選擇你的職業：", view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await _report_view_error(interaction, error)


async def finalize_profile(store, interaction: discord.Interaction, name: str, job: str, jobs: dict, position: str = ""):
    info = jobs.get(job, {})
    image_url = info.get("image", "")
    user_id = str(interaction.user.id)
    display_name = interaction.user.display_name

    async with store.lock:
        result = await asyncio.to_thread(store.upsert_character, user_id, display_name, name, job, position)

    desc = f"名字：**{name}**\n職業：**{job}**"
    if position:
        desc += f"\n位置：**{position}**"
    embed = discord.Embed(
        title="✅ 已更新角色資料" if result == "updated" else "✅ 已新增角色資料",
        description=desc,
        color=discord.Color.green(),
    )
    if image_url:
        embed.set_thumbnail(url=image_url)
    await interaction.edit_original_response(content=None, embed=embed, view=None)


class PositionSelect(discord.ui.Select):
    """選職業之後接著選戰鬥位置，選完才真正寫入。位置清單從「職業管理」表動態讀取。"""

    def __init__(self, store, name: str, job: str, jobs: dict, positions: list):
        self.store = store
        self.name = name
        self.job = job
        self.jobs = jobs
        options = [discord.SelectOption(label=p) for p in positions[:24]]
        options.append(discord.SelectOption(label="不設定位置", value="__SKIP__"))
        super().__init__(placeholder="選擇這隻角色的戰鬥位置", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        position = "" if self.values[0] == "__SKIP__" else self.values[0]
        await interaction.response.defer()
        await finalize_profile(self.store, interaction, self.name, self.job, self.jobs, position)


async def _report_view_error(interaction: discord.Interaction, error: Exception):
    """按鈕/選單背後發生例外時，把錯誤顯示給使用者看，而不是默默卡住沒反應。"""
    print(f"⚠️ 互動發生錯誤：{error!r}", flush=True)
    message = f"❌ 執行時發生錯誤：{error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as e:
        print(f"⚠️ 連錯誤訊息都送不出去：{e!r}", flush=True)


class PositionSelectView(discord.ui.View):
    def __init__(self, store, name: str, job: str, jobs: dict, positions: list):
        super().__init__(timeout=120)
        self.add_item(PositionSelect(store, name, job, jobs, positions))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_view_error(interaction, error)


async def show_position_step(store, interaction: discord.Interaction, name: str, job: str, jobs: dict):
    """
    選完職業後的下一步：有設定位置清單就跳出選單，完全沒設定過的話直接跳過，
    當作不設定位置寫入，避免因為管理員還沒建立位置清單就卡住整個流程。
    """
    positions = await asyncio.to_thread(store.get_positions)
    if not positions:
        await interaction.response.defer()
        await finalize_profile(store, interaction, name, job, jobs, "")
        return
    view = PositionSelectView(store, name, job, jobs, positions)
    await interaction.response.edit_message(
        content=f"名字：**{name}**\n職業：**{job}**\n請選擇戰鬥位置：",
        view=view,
    )


class JobSelect(discord.ui.Select):
    def __init__(self, store, name: str, jobs: dict, current_job: str = None):
        self.store = store
        self.name = name
        self.jobs = jobs
        self.current_job = current_job

        if current_job is None:
            candidates = get_tier1_jobs(jobs)
            placeholder = "選擇你的職業（第一轉）"
        else:
            candidates = get_children_jobs(jobs, current_job)
            placeholder = f"選擇「{current_job}」的下一轉（或維持不轉職）"

        options = [discord.SelectOption(label=c) for c in candidates[:24]]
        if current_job is not None:
            options.append(discord.SelectOption(label=f"維持「{current_job}」，不再轉職", value="__STOP__"))

        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        if chosen == "__STOP__":
            await show_position_step(self.store, interaction, self.name, self.current_job, self.jobs)
            return

        children = get_children_jobs(self.jobs, chosen)
        if children:
            view = JobSelectView(self.store, self.name, self.jobs, current_job=chosen)
            await interaction.response.edit_message(
                content=f"名字：**{self.name}**\n已選擇：**{chosen}**\n請選擇下一轉職業，或維持目前職業：",
                view=view,
            )
        else:
            await show_position_step(self.store, interaction, self.name, chosen, self.jobs)


class JobSelectView(discord.ui.View):
    def __init__(self, store, name: str, jobs: dict, current_job: str = None):
        super().__init__(timeout=120)
        self.add_item(JobSelect(store, name, jobs, current_job=current_job))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_view_error(interaction, error)


class StartProfileView(discord.ui.View):
    def __init__(self, store, author_id: int):
        super().__init__(timeout=120)
        self.store = store
        self.author_id = author_id

    @discord.ui.button(label="📝 設定角色資料", style=discord.ButtonStyle.primary)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這個按鈕是給發起的人用的，你可以自己打 `!profile` 喔。", ephemeral=True)
            return
        await interaction.response.send_modal(NameModal(self.store))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_view_error(interaction, error)


class Profiles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.command(name="profile")
    async def set_profile(self, ctx):
        """設定你自己的角色資料（名字＋職業）。"""
        view = StartProfileView(self.store, ctx.author.id)
        await ctx.send(f"{ctx.author.mention} 點下面的按鈕開始設定你的角色資料：", view=view)

    @commands.command(name="profiles")
    async def list_profiles(self, ctx):
        """列出目前所有人設定的角色資料。"""
        chars = await asyncio.to_thread(self.store.get_characters)
        if not chars:
            await ctx.send("目前還沒有人設定角色資料，用 `!profile` 開始設定。")
            return

        by_user = {}
        for c in chars:
            uid = c.get("Discord ID", "").strip()
            by_user.setdefault(uid, []).append(c)

        lines = []
        for uid, cs in by_user.items():
            char_text = "、".join(f"{c.get('角色名稱')}（{c.get('職業')}）" for c in cs)
            lines.append(f"<@{uid}>：{char_text}")
        text = "\n".join(lines)
        for i in range(0, len(text), 1800):
            await ctx.send(f"**🧑‍🤝‍🧑 角色資料：**\n{text[i:i+1800]}")

    @commands.hybrid_command(name="myprofiles")
    async def my_profiles(self, ctx):
        """列出自己名下的所有角色。用 /myprofiles 打的話只有你看得到。"""
        chars = await asyncio.to_thread(self.store.get_user_characters, str(ctx.author.id))
        if not chars:
            await ctx.send("你還沒有設定任何角色，用 `!profile` 開始設定。", ephemeral=True)
            return
        lines = [f"[{i}] {c.get('角色名稱')}（{c.get('職業')}）" for i, c in enumerate(chars)]
        await ctx.send("**🧑 你目前的角色：**\n```" + "\n".join(lines) + "```", ephemeral=True)

    @commands.command(name="delprofile")
    async def delete_profile(self, ctx, index: int):
        """刪除自己名下指定編號的角色。用法：!delprofile 0（編號用 !myprofiles 查）"""
        async with self.store.lock:
            removed = await asyncio.to_thread(self.store.delete_character, str(ctx.author.id), index)
        if not removed:
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 !myprofiles 確認編號。")
            return
        await ctx.send(f"🗑️ 已刪除角色：{removed.get('角色名稱')}（{removed.get('職業')}）")

    @commands.command(name="setavailability")
    async def set_availability(self, ctx, *, options: str = ""):
        """
        設定你平常可出席的時段（平日/假日可以都設，也可以都不設，改用備註手動說明）。
        用法：!setavailability weekday=yes weekend=no
             !setavailability weekday=no weekend=no note=平日8:00~9:00
        """
        opts = {}
        for token in options.split():
            if "=" in token:
                k, v = token.split("=", 1)
                opts[k.strip().lower()] = v.strip()

        def to_bool(s):
            return s.lower() in ("yes", "true", "y", "1", "是")

        weekday = to_bool(opts["weekday"]) if "weekday" in opts else None
        weekend = to_bool(opts["weekend"]) if "weekend" in opts else None
        note = opts.get("note")

        if weekday is None and weekend is None and note is None:
            await ctx.send("⚠️ 用法：`!setavailability weekday=yes weekend=no note=平日8:00~9:00`（三個參數都可省略，不填的欄位不會被改動）")
            return

        async with self.store.lock:
            await asyncio.to_thread(
                self.store.update_availability, str(ctx.author.id), ctx.author.display_name,
                weekday, weekend, note,
            )
        await ctx.send("✅ 已更新你的可出席時間設定。")

    @commands.hybrid_command(name="myavailability")
    async def my_availability(self, ctx):
        """查看自己目前的可出席時間設定。用 /myavailability 打的話只有你看得到。"""
        stats = await asyncio.to_thread(self.store.get_account_stats, str(ctx.author.id))
        if not stats:
            await ctx.send("你還沒有任何角色資料，先用 `!profile` 設定一隻角色。", ephemeral=True)
            return
        weekday = "✅" if str(stats.get("平日可出席", "")).strip().upper() == "TRUE" else "❌"
        weekend = "✅" if str(stats.get("假日可出席", "")).strip().upper() == "TRUE" else "❌"
        note = stats.get("其他時間備註", "") or "（無）"
        await ctx.send(
            f"**🕒 你的可出席時間：**\n平日：{weekday}　假日：{weekend}\n其他時間備註：{note}",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Profiles(bot))
