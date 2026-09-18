import discord
from discord.ext import commands


HELP_SECTIONS = [
    ("📋 開場與出席", [
        ("上傳隊員圖片", "辨識名單後按「✅ 確認正確」開場；沒掉寶就按「📋 這場沒有掉落寶物」"),
        ("!startsession 名字1,名字2", "手動打字開場，不用圖片辨識"),
        ("!noloot", "補記錄出席（這場沒有掉落寶物）"),
        ("!sessioninfo", "查看目前場次的出席名單與寶物狀態"),
        ("!syncmembers", "有人事後才補登角色時，回頭補上舊記錄的帳號對應"),
    ]),
    ("💎 寶物處理", [
        ("!loot", "整合指令：選寶物 → 選擇賣出／免費領取／歸公會"),
        ("上傳寶物圖片", "辨識掉落內容，確認後記入目前場次"),
        ("!item 寶物名稱", "手動記錄一樣寶物（預設分潤）"),
        ("!items 名稱1,名稱2", "一次記錄多樣寶物"),
        ("!donate 寶物名稱 貢獻者", "記錄捐獻給公會的寶物"),
        ("!sell 編號 金額", "直接結算（不用選單）"),
        ("!giveto 編號 角色名稱", "直接登記免費領取（不用選單）"),
    ]),
    ("💰 分潤領取", [
        ("!claim 或 /claim", "領取自己的分潤"),
        ("!pending 或 /pending", "查看自己待領的金額"),
        ("!unclaimed", "查看這場還有誰沒領"),
        ("!forceclaim @某人", "管理員：代為標記某人已領取"),
        ("!guildfund", "查看公會基金總額"),
    ]),
    ("🧑 角色資料", [
        ("!profile", "登記自己的角色（名字＋職業＋位置）"),
        ("!profiles", "查看所有人的角色"),
        ("!myprofiles 或 /myprofiles", "查看自己名下的角色"),
        ("!delprofile 編號", "刪除自己的某隻角色"),
        ("!setavailability weekday=yes weekend=no", "設定可出席時段"),
        ("!myavailability", "查看自己的可出席設定"),
    ]),
    ("⚔️ 職業與位置", [
        ("!jobs", "查看職業樹"),
        ("!addjob 名稱 tier=1", "新增職業（tier=2 要加 parent=上一轉）"),
        ("!deljob 名稱", "刪除職業"),
        ("!positions", "查看戰鬥位置選項"),
        ("!addposition 名稱 / !delposition 名稱", "新增／刪除戰鬥位置"),
    ]),
    ("🔒 頻道限制", [
        ("!setthreadrules 指令1,指令2", "限制這個討論串只能用哪些指令"),
        ("!clearthreadrules / !threadrules", "解除／查看討論串限制"),
        ("!setforumrules 指令1,指令2", "設定整個論壇的預設限制"),
        ("!clearforumrules / !forumrules", "解除／查看論壇限制"),
    ]),
]


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help", aliases=["說明", "指令"])
    async def show_help(self, ctx, section: str = None):
        """顯示所有指令說明。用法：!help"""
        embed = discord.Embed(
            title="📖 指令總覽",
            description="公會出席與分潤管理機器人。最常用的是 `!loot`（處理寶物）跟 `!claim`（領錢）。",
            color=discord.Color.blurple(),
        )
        for title, items in HELP_SECTIONS:
            lines = "\n".join(f"`{cmd}`\n　{desc}" for cmd, desc in items)
            embed.add_field(name=title, value=lines, inline=False)
        embed.set_footer(text="資料都存在 Google 試算表，指令執行後會即時更新。")
        await ctx.send(embed=embed)


async def setup(bot):
    # 先移除 discord.py 內建的英文 help，才能換成自己的
    bot.remove_command("help")
    await bot.add_cog(Help(bot))
