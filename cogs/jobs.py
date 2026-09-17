import asyncio
from discord.ext import commands


def get_tier1_jobs(jobs: dict) -> list:
    return [n for n, info in jobs.items() if info.get("tier", 1) == 1]


def get_children_jobs(jobs: dict, parent_name: str) -> list:
    return [n for n, info in jobs.items() if info.get("parent") == parent_name]


class Jobs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.command(name="addjob")
    async def add_job(self, ctx, job_name: str, *, options: str = ""):
        """
        新增或更新職業，可設定轉職層級與上一轉職業。
        用法：!addjob 戰士 tier=1
             !addjob 聖騎士 tier=2 parent=戰士
             !addjob 聖騎士 tier=2 parent=戰士 image=https://.../paladin.png
        """
        opts = {}
        for token in options.split():
            if "=" in token:
                k, v = token.split("=", 1)
                opts[k.strip().lower()] = v.strip()

        try:
            tier = int(opts.get("tier", 1))
        except ValueError:
            await ctx.send("⚠️ tier 必須是數字，例如 tier=1、tier=2。")
            return

        parent = opts.get("parent")
        image_url = opts.get("image")

        async with self.store.lock:
            jobs = await asyncio.to_thread(self.store.get_jobs)
            if tier > 1 and not parent:
                await ctx.send("⚠️ 第 2 轉以上的職業需要指定 `parent=上一轉職業`。")
                return
            if parent and parent not in jobs:
                await ctx.send(f"⚠️ 找不到上一轉職業「{parent}」，請先用 !jobs 確認名稱。")
                return
            await asyncio.to_thread(self.store.upsert_job, job_name, tier, parent, image_url)

        detail = f"第 {tier} 轉" + (f"，承接自「{parent}」" if parent else "")
        await ctx.send(f"✅ 已設定職業「{job_name}」（{detail}）。")

    @commands.command(name="deljob")
    async def del_job(self, ctx, job_name: str):
        """刪除一個職業設定。用法：!deljob 戰士"""
        async with self.store.lock:
            jobs = await asyncio.to_thread(self.store.get_jobs)
            if job_name not in jobs:
                await ctx.send(f"⚠️ 找不到職業「{job_name}」。")
                return
            children = get_children_jobs(jobs, job_name)
            if children:
                await ctx.send(f"⚠️ 「{job_name}」還有下一轉職業（{', '.join(children)}）承接自它，請先處理那些職業。")
                return
            await asyncio.to_thread(self.store.delete_job, job_name)
        await ctx.send(f"🗑️ 已刪除職業「{job_name}」。")

    @commands.command(name="jobs")
    async def list_jobs(self, ctx):
        """列出目前設定的職業樹（依轉職層級分組）。"""
        jobs = await asyncio.to_thread(self.store.get_jobs)
        if not jobs:
            await ctx.send("目前還沒有設定任何職業，用 `!addjob 職業名稱 tier=1` 新增第一個職業。")
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
        await ctx.send("**⚔️ 職業樹：**\n```" + "\n".join(lines) + "```")


async def setup(bot):
    await bot.add_cog(Jobs(bot))
