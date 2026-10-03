import discord
from discord.ext import commands


HELP_SECTIONS = [
    ("📋 開場與出席", [
        ("上傳隊員圖片", "辨識名單後按「✅ 確認正確」開場；沒掉寶就按「📋 這場沒有掉落寶物」"),
        ("/startsession", "手動打字開場，不用圖片辨識"),
        ("/noloot", "補記錄出席（這場沒有掉落寶物）"),
        ("/sessioninfo", "查看目前場次的出席名單與寶物狀態"),
        ("/syncmembers", "有人事後才補登角色時，回頭補上舊記錄的帳號對應"),
    ]),
    ("💎 寶物處理", [
        ("/loot", "整合指令：選寶物 → 賣出分潤／免費領取／歸公會"),
        ("上傳寶物圖片", "辨識掉落內容，確認後記入目前場次"),
        ("/item", "記錄一樣寶物（可選類型：分潤／公會／自用）"),
        ("/items", "一次記錄多樣寶物"),
        ("/donate", "記錄捐獻給公會的寶物"),
        ("/sell", "結算售出金額，可以選發錢的人（不填參數就跳選單）"),
        ("/giveto", "登記成員免費領取（不填參數就跳選單）"),
    ]),
    ("💰 分潤領取", [
        ("/claim", "領取自己的分潤（先列出待領清單，選了才會領取）"),
        ("/pending", "查看自己待領的金額"),
        ("/unclaimed", "選一樣已賣出的寶物，看誰領了、誰還沒領"),
        ("/forceclaim", "管理員：代為標記某人已領取"),
        ("/guildfund", "查看公會基金總額"),
    ]),
    ("🧑 角色資料", [
        ("/profile", "登記自己的角色（名字＋職業＋位置）"),
        ("/profiles", "查看所有人的角色"),
        ("/myprofiles", "查看自己名下的角色"),
        ("/delprofile", "刪除自己的某隻角色"),
        ("/setavailability", "設定可出席時段"),
        ("/myavailability", "查看自己的可出席設定"),
    ]),
    ("⚔️ 職業與位置", [
        ("/jobs", "查看職業樹"),
        ("/addjob", "新增職業（第 2 轉以上要填上一轉）"),
        ("/deljob", "刪除職業"),
        ("/positions", "查看戰鬥位置選項"),
        ("/addposition　/delposition", "新增／刪除戰鬥位置"),
    ]),
    ("🏷️ 身分組按鈕（需管理權限）", [
        ("/roleadd", "設定一個身分組按鈕（成員按一下拿到、再按一下移除）"),
        ("/roleremove", "移除身分組按鈕，舊公告上的也會停用"),
        ("/rolelist", "查看目前設定的身分組按鈕"),
        ("/postrolepanel", "發一則身分組按鈕公告（常駐，重啟也能用）"),
    ]),
    ("🔒 管理", [
        ("/postpanel", "在頻道發一則附按鈕的公告：角色資料、寶物結算兩種（需管理權限）"),
        ("/ocrchannel", "設定哪些頻道的圖片要辨識，省 Gemini 額度（需管理權限）"),
        ("/viewlogs", "直接在 Discord 讀取稽核/錯誤記錄（需管理權限）"),
        ("/checkprofiles", "檢查每隻角色的帳號是否對得上伺服器成員（需管理權限）"),
        ("/fixprofile", "把對錯人的角色改到正確的成員名下（需管理權限）"),
        ("/deletesession", "刪除登記錯的記錄，一次刪一樣寶物，會先確認、自動備份與檢查（需管理權限）"),
        ("/repairformulas", "重寫所有統計公式、依 Discord ID 排序角色資料，出現 #REF! 時用（需管理權限）"),
        ("/setthreadrules　/clearthreadrules", "限制／解除這個討論串能用的指令（需管理權限）"),
        ("/setforumrules　/clearforumrules", "限制／解除整個論壇能用的指令（需管理權限）"),
        ("/threadrules　/forumrules", "查看目前的指令限制"),
    ]),
]


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="查看所有指令說明")
    async def show_help(self, ctx):
        """顯示所有指令說明。"""
        embed = discord.Embed(
            title="📖 指令總覽",
            description=(
                "公會出席與分潤管理機器人。最常用的是 `/loot`（處理寶物）跟 `/claim`（領錢）。\n"
                "指令一律用 `/` 打。開場跟寶物記錄的指令（startsession、noloot、item、items、"
                "donate、loot、sell、giveto）出團時要快速連續打，也可以用 `!` 開頭。"
            ),
            color=discord.Color.blurple(),
        )
        for title, items in HELP_SECTIONS:
            lines = "\n".join(f"`{cmd}`\n　{desc}" for cmd, desc in items)
            embed.add_field(name=title, value=lines, inline=False)
        embed.set_footer(text="資料都存在 Google 試算表，指令執行後會即時更新。")
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot):
    # 先移除 discord.py 內建的英文 help，才能換成自己的
    bot.remove_command("help")
    await bot.add_cog(Help(bot))
