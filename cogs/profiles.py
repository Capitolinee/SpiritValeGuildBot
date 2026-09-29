import asyncio
import io
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.jobs import get_tier1_jobs, get_children_jobs
from helpers import resolve_display_name, sort_warning
import audit


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
                "⚠️ 目前還沒有設定任何職業，請先請管理員用 `/addjob` 新增職業。",
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

    created = result["status"] == "created"
    desc = f"名字：**{name}**\n職業：**{job}**"
    if position:
        desc += f"\n位置：**{position}**"
    if result["backfilled"]:
        desc += f"\n\n📋 找到你登記之前參加過的 {result['backfilled']} 筆出團記錄，已經補上，出席次數跟分潤都會算進來。"
    desc += sort_warning(result["sort"])
    embed = discord.Embed(
        title="✅ 已新增角色資料" if created else "✅ 已更新角色資料",
        description=desc,
        color=discord.Color.green(),
    )
    if image_url:
        embed.set_thumbnail(url=image_url)
    audit.audit(
        "新增角色" if created else "更新角色",
        who=display_name,
        detail=f"角色 {name}｜職業 {job}" + (f"｜位置 {position}" if position else "")
               + (f"｜補上場次記錄 {result['backfilled']} 筆" if result["backfilled"] else ""),
    )
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
    audit.error("互動發生錯誤", error, who=interaction.user.display_name)
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
            await interaction.response.send_message("這個按鈕是給發起的人用的，你可以自己打 `/profile` 喔。", ephemeral=True)
            return
        await interaction.response.send_modal(NameModal(self.store))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        await _report_view_error(interaction, error)


class Profiles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.hybrid_command(name="profile", description="登記或更新自己的角色（名字＋職業＋位置）")
    async def set_profile(self, ctx):
        """設定你自己的角色資料（名字＋職業＋位置）。"""
        # 用 / 打的話直接跳出輸入視窗，不用先按按鈕，頻道也不會留下任何訊息
        if ctx.interaction is not None:
            await ctx.interaction.response.send_modal(NameModal(self.store))
            return
        view = StartProfileView(self.store, ctx.author.id)
        await ctx.send(f"{ctx.author.mention} 點下面的按鈕開始設定你的角色資料：", view=view)

    @commands.hybrid_command(name="profiles", description="查看所有人登記的角色")
    async def list_profiles(self, ctx):
        """列出目前所有人設定的角色資料。"""
        await ctx.defer(ephemeral=True)
        chars = await asyncio.to_thread(self.store.get_characters)
        if not chars:
            await ctx.send("目前還沒有人設定角色資料，用 /profile 開始設定。", ephemeral=True)
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
            await ctx.send(f"**🧑‍🤝‍🧑 角色資料：**\n{text[i:i+1800]}", ephemeral=True)

    @commands.hybrid_command(name="myprofiles", description="查看自己名下的角色")
    async def my_profiles(self, ctx):
        """列出自己名下的所有角色（附編號，給 /delprofile 用）。"""
        await ctx.defer(ephemeral=True)
        chars = await asyncio.to_thread(self.store.get_user_characters, str(ctx.author.id))
        if not chars:
            await ctx.send("你還沒有設定任何角色，用 /profile 開始設定。", ephemeral=True)
            return
        lines = [f"[{i}] {c.get('角色名稱')}（{c.get('職業')}）" for i, c in enumerate(chars)]
        await ctx.send("**🧑 你目前的角色：**\n```" + "\n".join(lines) + "```", ephemeral=True)

    @commands.hybrid_command(name="delprofile", description="刪除自己名下的某隻角色")
    @app_commands.describe(index="角色編號（用 /myprofiles 查）")
    async def delete_profile(self, ctx, index: int):
        """刪除自己名下指定編號的角色。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            removed = await asyncio.to_thread(self.store.delete_character, str(ctx.author.id), index)
        if not removed:
            await ctx.send(f"⚠️ 編號 {index} 不存在，請先用 /myprofiles 確認編號。", ephemeral=True)
            return
        audit.audit(
            "刪除角色", who=ctx.author.display_name,
            detail=f"角色 {removed.get('角色名稱')}（{removed.get('職業')}）",
        )
        await ctx.send(f"🗑️ 已刪除角色：{removed.get('角色名稱')}（{removed.get('職業')}）"
                       + sort_warning(removed.get("sort")), ephemeral=True)

    @commands.hybrid_command(name="setavailability", description="設定自己平常可出席的時段")
    @app_commands.describe(
        weekday="平日可以出席嗎",
        weekend="假日可以出席嗎",
        note="其他時間備註，例如 平日8:00~9:00",
    )
    async def set_availability(self, ctx, weekday: Optional[bool] = None,
                               weekend: Optional[bool] = None, *, note: Optional[str] = None):
        """
        設定你平常可出席的時段（沒填的欄位維持原本設定不變）。
        用法：/setavailability weekday:True weekend:False
             /setavailability weekday:False weekend:False note:平日8:00~9:00
        """
        if weekday is None and weekend is None and note is None:
            await ctx.send("⚠️ 至少要填一個欄位（平日、假日、或備註），沒填的欄位不會被改動。", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            await asyncio.to_thread(
                self.store.update_availability, str(ctx.author.id), ctx.author.display_name,
                weekday, weekend, note,
            )
        audit.audit(
            "更新可出席時間", who=ctx.author.display_name,
            detail=f"平日={weekday}｜假日={weekend}｜備註={note}",
        )
        await ctx.send("✅ 已更新你的可出席時間設定。", ephemeral=True)

    @commands.hybrid_command(name="myavailability", description="查看自己的可出席時間設定")
    async def my_availability(self, ctx):
        """查看自己目前的可出席時間設定。"""
        await ctx.defer(ephemeral=True)
        stats = await asyncio.to_thread(self.store.get_account_stats, str(ctx.author.id))
        if not stats:
            await ctx.send("你還沒有任何角色資料，先用 /profile 設定一隻角色。", ephemeral=True)
            return
        weekday = "✅" if str(stats.get("平日可出席", "")).strip().upper() == "TRUE" else "❌"
        weekend = "✅" if str(stats.get("假日可出席", "")).strip().upper() == "TRUE" else "❌"
        note = stats.get("其他時間備註", "") or "（無）"
        await ctx.send(
            f"**🕒 你的可出席時間：**\n平日：{weekday}　假日：{weekend}\n其他時間備註：{note}",
            ephemeral=True,
        )

    @commands.hybrid_command(name="checkprofiles", description="管理員：檢查每隻角色的帳號 ID 是否都對得上伺服器成員")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def check_profiles(self, ctx):
        """
        把「角色資料」表裡每一組 Discord ID 都實際拿去問 Discord，
        列出對不上的角色，以及 Discord 真正的回應，方便找出「已離開的使用者」的原因。
        只讀取、不修改任何資料。
        """
        await ctx.defer(ephemeral=True)
        chars = await asyncio.to_thread(self.store.get_characters)
        by_id = {}
        for c in chars:
            by_id.setdefault(c.get("Discord ID", ""), []).append(c.get("角色名稱", "") or "（無名稱）")

        problems, cache_miss, ok = [], [], 0
        for raw_id, names in by_id.items():
            label = "、".join(names)
            sid = raw_id.strip()
            if not re.fullmatch(r"[0-9]{17,20}", sid):
                problems.append(f"❌ {label}\n　ID 格式不對：{raw_id!r}（{len(raw_id)} 個字元），ID 已經被改壞了")
                continue
            if ctx.guild.get_member(int(sid)):
                ok += 1
                continue
            try:
                await ctx.guild.fetch_member(int(sid))
                ok += 1
                cache_miss.append(label)  # 查得到，只是機器人的成員快取裡沒有
                continue
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                problems.append(f"⚠️ {label}：{sid}\n　查詢失敗（HTTP {e.status}）：{e.text}")
                continue
            # 伺服器裡找不到，再確認這個帳號在 Discord 上到底存不存在
            try:
                user = await self.bot.fetch_user(int(sid))
                reason = f"帳號存在（{user.name}），但 Discord 回報他不在這台伺服器"
            except discord.NotFound:
                reason = "Discord 上根本沒有這個帳號，ID 可能被改壞了"
            except discord.HTTPException as e:
                reason = f"查詢帳號失敗（HTTP {e.status}）"
            # 被試算表改壞的 ID 只保留前 15 位，後面全部變成 0（18 位數 → 最後 3 位是 0）
            if sid.endswith("0" * (len(sid) - 15)):
                reason += "\n　ID 後面幾位都是 0，這是被試算表改壞的典型樣子"
            problems.append(f"❌ {label}：{sid}\n　{reason}")

        lines = [
            f"伺服器：{ctx.guild.name}（{ctx.guild.id}）",
            f"共 {len(by_id)} 個帳號，{ok} 個正常，{len(problems)} 個有問題",
        ]
        if cache_miss:
            lines.append(f"（其中 {len(cache_miss)} 個機器人的成員快取裡沒有、但直接查得到：{'、'.join(cache_miss)}）")
        if problems:
            lines += ["", *problems, "", "要把某隻角色改到正確的人名下，用 /fixprofile。"]
        text = "\n".join(lines)
        if len(text) <= 1900:
            await ctx.send(f"```{text}```", ephemeral=True)
        else:
            await ctx.send("結果比較長，整份用附件傳送：",
                           file=discord.File(io.BytesIO(text.encode("utf-8")), filename="checkprofiles.txt"),
                           ephemeral=True)

    @commands.hybrid_command(name="fixprofile", description="管理員：把某隻角色改到正確的成員名下")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(character="角色名稱", member="這隻角色真正的主人")
    async def fix_profile(self, ctx, character: str, member: discord.Member):
        """把角色重新對應到正確的帳號，過去的出團記錄也會一起改過來。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.relink_character, character, str(member.id), member.display_name
            )
        if not result["ok"]:
            await ctx.send(f"⚠️ 找不到叫「{character}」的角色，可以先用 /profiles 確認名稱。", ephemeral=True)
            return
        audit.audit(
            "修正角色帳號", who=ctx.author.display_name,
            detail=(f"角色 {result['char_name']}｜改到 {member.display_name}（{member.id}）"
                    f"｜原本 {', '.join(result['old_ids']) or '（無）'}｜場次記錄 {result['session_rows']} 列"),
        )
        await ctx.send(
            f"✅ 「{result['char_name']}」已改到 {member.mention} 名下，"
            f"過去的出團記錄也一起改了 {result['session_rows']} 筆。" + sort_warning(result.get("sort")),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Profiles(bot))
