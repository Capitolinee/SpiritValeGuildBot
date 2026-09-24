import asyncio
from typing import Optional

from discord import app_commands
from discord.ext import commands

import audit


def get_tier1_jobs(jobs: dict) -> list:
    return [n for n, info in jobs.items() if info.get("tier", 1) == 1]


def get_children_jobs(jobs: dict, parent_name: str) -> list:
    return [n for n, info in jobs.items() if info.get("parent") == parent_name]


class Jobs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.hybrid_command(name="addjob", description="新增或更新職業")
    @app_commands.describe(
        job_name="職業名稱",
        tier="轉職層級（第幾轉），預設 1",
        parent="上一轉職業（第 2 轉以上必填）",
        image="職業圖片網址（選填）",
    )
    async def add_job(self, ctx, job_name: str, tier: int = 1,
                      parent: Optional[str] = None, image: Optional[str] = None):
        """
        新增或更新職業。
        用法：/addjob job_name:戰士
             /addjob job_name:聖騎士 tier:2 parent:戰士
             !addjob 聖騎士 2 戰士 https://.../paladin.png
        """
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            jobs = await asyncio.to_thread(self.store.get_jobs)
            if tier > 1 and not parent:
                await ctx.send("⚠️ 第 2 轉以上的職業需要指定上一轉職業（parent）。", ephemeral=True)
                return
            if parent and parent not in jobs:
                await ctx.send(f"⚠️ 找不到上一轉職業「{parent}」，請先用 /jobs 確認名稱。", ephemeral=True)
                return
            await asyncio.to_thread(self.store.upsert_job, job_name, tier, parent, image)

        detail = f"第 {tier} 轉" + (f"，承接自「{parent}」" if parent else "")
        audit.audit("設定職業", who=ctx.author.display_name, detail=f"{job_name}｜{detail}")
        await ctx.send(f"✅ 已設定職業「{job_name}」（{detail}）。", ephemeral=True)

    @commands.hybrid_command(name="deljob", description="刪除一個職業")
    @app_commands.describe(job_name="要刪除的職業名稱")
    async def del_job(self, ctx, job_name: str):
        """刪除一個職業設定。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            jobs = await asyncio.to_thread(self.store.get_jobs)
            if job_name not in jobs:
                await ctx.send(f"⚠️ 找不到職業「{job_name}」。", ephemeral=True)
                return
            children = get_children_jobs(jobs, job_name)
            if children:
                await ctx.send(
                    f"⚠️ 「{job_name}」還有下一轉職業（{', '.join(children)}）承接自它，請先處理那些職業。",
                    ephemeral=True,
                )
                return
            await asyncio.to_thread(self.store.delete_job, job_name)
        audit.audit("刪除職業", who=ctx.author.display_name, detail=job_name)
        await ctx.send(f"🗑️ 已刪除職業「{job_name}」。", ephemeral=True)

    @commands.hybrid_command(name="jobs", description="查看職業樹")
    async def list_jobs(self, ctx):
        """列出目前設定的職業樹（依轉職層級分組）。"""
        await ctx.defer(ephemeral=True)
        jobs = await asyncio.to_thread(self.store.get_jobs)
        if not jobs:
            await ctx.send("目前還沒有設定任何職業，用 /addjob 新增第一個職業。", ephemeral=True)
            return

        by_tier = {}
        for name, info in jobs.items():
            by_tier.setdefault(info.get("tier", 1), []).append((name, info.get("parent")))

        lines = []
        for tier in sorted(by_tier):
            lines.append(f"【第 {tier} 轉】")
            for name, parent in sorted(by_tier[tier]):
                suffix = f"（承接自 {parent}）" if parent else ""
                lines.append(f"  - {name}{suffix}")
        await ctx.send("**⚔️ 職業樹：**\n```" + "\n".join(lines) + "```", ephemeral=True)

    @commands.hybrid_command(name="addposition", description="新增戰鬥位置選項")
    @app_commands.describe(name="位置名稱，例如 輔助")
    async def add_position(self, ctx, *, name: str):
        """新增一個戰鬥位置選項（/profile 選位置時會出現）。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.add_position, name)
        if result == "exists":
            await ctx.send(f"⚠️ 「{name}」已經存在，不用重複新增。", ephemeral=True)
            return
        audit.audit("新增位置", who=ctx.author.display_name, detail=name)
        await ctx.send(f"✅ 已新增位置「{name}」。", ephemeral=True)

    @commands.hybrid_command(name="delposition", description="刪除戰鬥位置選項")
    @app_commands.describe(name="要刪除的位置名稱")
    async def del_position(self, ctx, *, name: str):
        """刪除一個戰鬥位置選項。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            removed = await asyncio.to_thread(self.store.delete_position, name)
        if not removed:
            await ctx.send(f"⚠️ 找不到位置「{name}」，用 /positions 確認目前有哪些。", ephemeral=True)
            return
        audit.audit("刪除位置", who=ctx.author.display_name, detail=name)
        await ctx.send(f"🗑️ 已刪除位置「{name}」。", ephemeral=True)

    @commands.hybrid_command(name="positions", description="查看戰鬥位置選項")
    async def list_positions(self, ctx):
        """列出目前設定的所有戰鬥位置選項。"""
        await ctx.defer(ephemeral=True)
        positions = await asyncio.to_thread(self.store.get_positions)
        if not positions:
            await ctx.send("目前還沒有設定任何位置，用 /addposition 新增。", ephemeral=True)
            return
        await ctx.send("**🎯 目前的位置選項：**\n```" + "、".join(positions) + "```", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Jobs(bot))
