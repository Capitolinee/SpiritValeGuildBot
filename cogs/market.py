"""
交易區：成員自己掛賣、以物易物。

流程：
  1. 文字頻道裡有一則常駐公告（/postmarket），按「📸 我要賣東西」
  2. 選大類 → 細分類 → 交易方式（出售／交換／都可以）→ 填名稱、價格／想換什麼、備註、上傳圖片
  3. 機器人在交易區論壇（/setmarket 設定）開一篇貼文，掛上「大類、交易方式、出售中」標籤
  4. 其他人按「🙋 我有興趣」：出售的話機器人在貼文裡 @ 賣家；交換的話先填想拿什麼換（可附圖）
     議價公開在貼文裡進行
  5. 賣家按「⚙️ 賣家管理」（只有賣家跟管理員能用，選單只有自己看得到）：修改、已成交、下架
  6. 已成交：賣家選買家、成交方式、金額／換得物品 → 機器人在貼文裡請買家確認 → 買家按確認才算成交
     成交後標籤換成「已售出」、貼文鎖定，記進試算表「交易區」分頁

卡片跟成交確認的按鈕是「動態按鈕」：ID 裡帶著貼文編號，重新部署後舊貼文的按鈕照樣能用。
動態按鈕出錯時 discord.py 不會告訴使用者，所以每個按鈕都自己接住錯誤、回一句話。
"""
import asyncio
import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import audit
from helpers import TW_TZ

STATUS_TAGS = ("出售中", "議價中", "已售出")
TRADE_TAGS = ("出售", "交換", "贈送", "收購", "長期")
TRADE_TYPES = ("出售", "交換", "都可以", "贈送", "長期供貨", "收購", "長期收購")
LONG = ("長期供貨", "長期收購")    # 成交後不鎖定，每筆成交扣數量，數量歸零自動暫停
BUY = ("收購", "長期收購")         # 收購貼文：發文的人是買家，「賣家」欄記的是發文的人
ACTIVE = ("出售中", "議價中")      # 可以按我有興趣、可以成交
PAUSED = "暫停"                    # 長期的商品暫停（缺貨或賣家暫停），貼文還在、不鎖定
OPEN = ACTIVE + (PAUSED,)          # 還掛在論壇上的
LONG_CHECK_DAYS = 10               # 長期的商品每幾天提醒一次「還在供貨（收購）嗎？」
LONG_GRACE_DAYS = 3                # 提醒之後幾天沒回應就下架
MAX_IMAGE_BYTES = 10 * 1024 * 1024
LISTING_DAYS = 30          # 掛賣幾天後到期
REMIND_DAYS_BEFORE = 5     # 到期前幾天提醒賣家（第 25 天）
ACTIVE_GRACE_DAYS = 7      # 到期時，貼文裡這幾天內還有人（不是機器人）回覆的話，代表還在談，先不下架
TIME_FMT = "%Y/%m/%d %H:%M:%S"
NONE_MENTIONS = discord.AllowedMentions.none()


# ---------------- 小工具 ----------------

def parse_price(text: str) -> Optional[int]:
    """「5,000,000」「500萬」「1.5億」都可以；看不懂或小於等於 0 就回傳 None。"""
    t = (text or "").replace(",", "").replace("，", "").replace(" ", "").strip()
    if not t:
        return None
    unit = 1
    if t.endswith("萬"):
        unit, t = 10_000, t[:-1]
    elif t.endswith("億"):
        unit, t = 100_000_000, t[:-1]
    try:
        value = int(round(float(t) * unit))
    except ValueError:
        return None
    return value if value > 0 else None


def fmt_price(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return ""


def listing_title(l: dict) -> str:
    trade, price = l.get("交易方式"), fmt_price(l.get("開價"))
    done = {"贈送": "【已送出】", "收購": "【已收購】"}.get(trade, "【已售出】")
    prefix = {"已售出": done, "已下架": "【已下架】", PAUSED: "【暫停】"}.get(l.get("狀態"), "")
    if trade in BUY:
        prefix += "【收購】"
    tail = {"出售": price, "交換": "可交換", "都可以": f"{price}｜可交換" if price else "可交換",
            "贈送": "免費贈送",
            "長期供貨": f"長期供貨｜單價 {price}",
            "收購": f"預算 {price}" if price else "價格可議",
            "長期收購": f"長期收購｜單價 {price}" if price else "長期收購｜價格可議"}.get(trade, price)
    return f"{prefix}【{l.get('分類')}】{l.get('物品名稱')}｜{tail}"[:100]


TRADE_EMOJI = {"出售": "💰", "交換": "🔄", "都可以": "💰🔄", "贈送": "🎁", "長期供貨": "🔁",
               "收購": "🛒", "長期收購": "🛒🔁"}
STATUS_TEXT = {"出售中": "🟢 出售中", "議價中": "🤝 議價中", "已售出": "✅ 已售出", "已下架": "🗑️ 已下架",
               PAUSED: "⏸️ 暫停供貨"}
BUY_STATUS_TEXT = {"出售中": "🟢 收購中", "議價中": "🙋 有人有貨", "已售出": "✅ 已收購", "已下架": "🗑️ 已下架",
                   PAUSED: "⏸️ 暫停收購"}
METHOD_TEXT = {"金錢": "💰 金錢", "交換": "🔄 交換", "兩者都有": "🔄💰 交換＋補差價", "贈送": "🎁 贈送"}
# 贈品的狀態用比較貼切的說法（論壇標籤名稱還是同一組）
GIFT_STATUS_TEXT = {"出售中": "🟢 等人索取", "議價中": "🙋 有人想要", "已售出": "✅ 已送出", "已下架": "🗑️ 已下架"}


def now_tw() -> datetime:
    """交易區所有「現在幾點」都從這裡拿（台灣時間），時間來源統一，測試時也能快轉。"""
    return datetime.now(TW_TZ)


def parse_time(stored: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stored or "", TIME_FMT).replace(tzinfo=TW_TZ)
    except ValueError:
        return None


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(TW_TZ).strftime(TIME_FMT)


def expires_at(l: dict) -> Optional[datetime]:
    """到期時間；舊的商品沒有記到期時間，就用掛賣時間＋30 天。"""
    exp = parse_time(l.get("到期時間"))
    if exp is None:
        listed = parse_time(l.get("掛賣時間"))
        exp = listed + timedelta(days=LISTING_DAYS) if listed else None
    return exp


def when_text(stored: str, ts: Optional[int]) -> str:
    """2026/10/02 21:30（3 小時前）：前面是固定的台灣時間，括號裡是 Discord 的相對時間，會自己更新。"""
    text = (stored or "")[:16]
    return f"{text}（<t:{ts}:R>）" if ts else text


def to_ts(stored: str) -> Optional[int]:
    try:
        return int(datetime.strptime(stored or "", "%Y/%m/%d %H:%M:%S").replace(tzinfo=TW_TZ).timestamp())
    except ValueError:
        return None


def listing_embed(l: dict) -> discord.Embed:
    status = l.get("狀態", "")
    color = {"出售中": discord.Color.green(), "議價中": discord.Color.orange(),
             "已售出": discord.Color.dark_grey(), "已下架": discord.Color.dark_grey()}.get(status, discord.Color.blurple())
    trade = l.get("交易方式", "")
    lines = [
        f"📂 **分類**：{l.get('分類')}（{l.get('大類')}）",
        f"{TRADE_EMOJI.get(trade, '💱')} **交易方式**：{trade}",
    ]
    if l.get("開價"):
        label = "單價" if trade in LONG else "預算" if trade == "收購" else "開價"
        lines.append(f"🏷️ **{label}**：{fmt_price(l['開價'])}")
    elif trade in BUY:
        lines.append("🏷️ **預算**：價格可議")
    if trade in LONG or trade == "收購":
        qty = l.get("數量") or "0"
        lines.append(f"📦 **{'庫存' if trade == '長期供貨' else '還要收'}**：{qty} 個")
    if trade in LONG:
        lines.append(f"✅ **已成交**：{l.get('成交次數') or 0} 筆")
    if l.get("想換"):
        lines.append(f"🎯 **想換**：{l['想換']}")
    if l.get("備註"):
        lines.append(f"📝 **備註**：{l['備註']}")
    lines.append(f"👤 **{'買家' if trade in BUY else '賣家'}**：<@{l.get('賣家ID')}>")
    lines.append(f"🕒 **{'發文時間' if trade in BUY else '掛賣時間'}**：{when_text(l.get('掛賣時間'), to_ts(l.get('掛賣時間')))}")
    texts = GIFT_STATUS_TEXT if trade == "贈送" else BUY_STATUS_TEXT if trade in BUY else STATUS_TEXT
    lines.append(f"📌 **狀態**：{texts.get(status, status)}")
    exp = expires_at(l)
    if status in OPEN and exp is not None:
        label = "下次確認" if trade in LONG else "到期時間"
        lines.append(f"⏳ **{label}**：{when_text(fmt_time(exp), int(exp.timestamp()))}")
    if status == "已售出":
        lines.append("")
        lines.append(f"🙋 **{'收到的人' if trade == '贈送' else '賣給他的人' if trade in BUY else '買家'}**：<@{l.get('買家ID')}>")
        lines.append(f"🤝 **成交方式**：{METHOD_TEXT.get(l.get('成交方式'), l.get('成交方式'))}")
        if l.get("成交價"):
            lines.append(f"💵 **成交價**：{fmt_price(l['成交價'])}")
        if l.get("換得物品"):
            lines.append(f"🎁 **換得物品**：{l['換得物品']}")
        lines.append(f"✅ **{'送出時間' if trade == '贈送' else '成交時間'}**：{when_text(l.get('成交時間'), to_ts(l.get('成交時間')))}")
    embed = discord.Embed(title=l.get("物品名稱", "")[:256], description="\n".join(lines)[:4000], color=color)
    if l.get("圖片檔名"):
        embed.set_image(url=f"attachment://{l['圖片檔名']}")
    return embed


TRADE_CODE = {"出售": "s", "交換": "x", "都可以": "b", "贈送": "g",      # 寫進「我有興趣」按鈕 ID 的交易方式代碼
              "長期供貨": "l", "收購": "p", "長期收購": "q"}


def card_view(post_id: int, trade: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(InterestButton(post_id, TRADE_CODE.get(trade, "s")))
    view.add_item(ManageButton(post_id, "買家管理" if trade in BUY else "賣家管理"))
    return view


def interested_ids(l: dict) -> list:
    return [x for x in (l.get("有興趣的人") or "").split() if x.isdigit()]


async def reply_error(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except Exception:
        pass


async def guarded(interaction: discord.Interaction, coro, where: str):
    """動態按鈕出錯時 discord.py 只會默默寫進記錄，使用者會看到「此互動失敗」，所以自己接住。"""
    try:
        await coro
    except Exception as e:
        audit.error(f"交易區：{where}發生錯誤", e, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 發生錯誤，請稍後再試或通知管理員：{e}")


def check_image(att) -> Optional[str]:
    if att is None:
        return None
    if not (att.content_type or "").startswith("image/"):
        return "上傳的檔案不是圖片，請上傳 png、jpg 之類的圖片。"
    if (att.size or 0) > MAX_IMAGE_BYTES:
        return "圖片太大了（超過 10MB），請換一張小一點的。"
    return None


async def image_file(att):
    """把上傳的圖片轉成要發出去的檔案。檔名改成英文，中文檔名在卡片裡顯示圖片常會出問題。"""
    ext = os.path.splitext(att.filename or "")[1].lower()
    ext = ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext) else ".png"
    name = f"item{ext}"
    return await att.to_file(filename=name), name


# ---------------- 貼文的讀取與更新 ----------------

async def get_thread(bot, post_id: int):
    ch = bot.get_channel(int(post_id))
    return ch if ch is not None else await bot.fetch_channel(int(post_id))


def tag_key(name: str) -> str:
    """
    比對論壇標籤名稱用：只留文字跟數字（中文算文字），表情符號、符號、空白都忽略。
    這樣標籤名稱被改成「🟢 出售中」「出售中🟢」也一樣認得出是「出售中」。
    """
    return "".join(ch for ch in (name or "") if unicodedata.category(ch)[0] in "LN").casefold()


def tags_for(forum, l: dict) -> list:
    names = [l.get("大類")]
    trade = l.get("交易方式")
    if trade in ("出售", "都可以", "長期供貨"):
        names.append("出售")
    if trade in ("交換", "都可以"):
        names.append("交換")
    if trade == "贈送":
        names.append("贈送")
    if trade in BUY:
        names.append("收購")
    if trade in LONG:
        names.append("長期")
    status = l.get("狀態")
    if status in STATUS_TAGS:
        names.append(status)
    by_key = {tag_key(t.name): t for t in forum.available_tags}
    return [by_key[tag_key(n)] for n in names if tag_key(n) in by_key][:5]


def tags_missing(forum, names) -> list:
    """論壇裡找不到的標籤名稱（一樣忽略表情符號和空白）。"""
    have = {tag_key(t.name) for t in forum.available_tags}
    return [n for n in names if tag_key(n) not in have]


async def refresh_post(bot, l: dict):
    """照試算表裡的最新狀態，更新貼文標題、標籤、卡片；結束的商品會鎖定並收起來。"""
    thread = await get_thread(bot, int(l["貼文ID"]))
    ended = l.get("狀態") not in OPEN       # 暫停的長期商品還掛著，不鎖定
    if getattr(thread, "archived", False):
        await thread.edit(archived=False)
    starter = await thread.fetch_message(int(l["貼文ID"]))
    await starter.edit(embed=listing_embed(l), view=None if ended else card_view(int(l["貼文ID"]), l.get("交易方式")))
    forum = thread.parent or await bot.get_cog("Market").get_forum()
    tags = tags_for(forum, l)
    await thread.edit(name=listing_title(l), **({"applied_tags": tags} if tags else {}),
                      **({"archived": True, "locked": True} if ended else {"locked": False}))


# ---------------- 掛賣：選大類、細分類、交易方式 → 填內容 ----------------

class ListingModal(discord.ui.Modal):
    def __init__(self, bot, group: str, category: str, trade: str):
        super().__init__(title=f"{'收購' if trade in BUY else '掛賣'}：{category}"[:45])
        self.bot, self.group, self.category, self.trade = bot, group, category, trade
        self.name_input = discord.ui.TextInput(placeholder="例如：死靈之弓 +5", max_length=40)
        self.add_item(discord.ui.Label(text="想買的物品" if trade in BUY else "物品名稱", component=self.name_input))
        self.price_input = self.wants_input = self.qty_input = None
        if trade in LONG or trade == "收購":
            self.price_input = discord.ui.TextInput(placeholder="例如 50000、5萬", max_length=20,
                                                    required=(trade == "長期供貨"))
            self.add_item(discord.ui.Label(
                text="單價" if trade in LONG else "預算", component=self.price_input,
                description="不填就是價格可議" if trade in BUY else "一個多少錢"))
            self.qty_input = discord.ui.TextInput(placeholder="例如 30", max_length=6, default="1" if trade == "收購" else None)
            self.add_item(discord.ui.Label(text={"長期供貨": "庫存（有幾個）", "收購": "要幾個",
                                                 "長期收購": "總共要收幾個"}[trade], component=self.qty_input))
        if trade in ("出售", "都可以"):
            self.price_input = discord.ui.TextInput(placeholder="例如 5000000、500萬", max_length=20,
                                                    required=(trade == "出售"))
            self.add_item(discord.ui.Label(text="價格", component=self.price_input,
                                           description="都可以的話可以不填，只填想換什麼" if trade == "都可以" else None))
        if trade in ("交換", "都可以"):
            self.wants_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=200,
                                                    placeholder="例如：想換武器類，或 +3 以上的頭盔",
                                                    required=(trade == "交換"))
            self.add_item(discord.ui.Label(text="想換什麼", component=self.wants_input))
        self.note_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=300, required=False,
                                               placeholder="選填，例如：附魔、耐久、可以面交的時間")
        self.add_item(discord.ui.Label(text="備註", component=self.note_input))
        self.image_input = discord.ui.FileUpload(required=False, max_values=1)
        self.add_item(discord.ui.Label(text="圖片（選填）", component=self.image_input,
                                       description="看不到上傳的地方的話，請把 Discord 更新到最新版"))

    async def on_submit(self, interaction: discord.Interaction):
        price = None
        if self.price_input is not None and self.price_input.value.strip():
            price = parse_price(self.price_input.value)
            if price is None:
                await interaction.response.send_message("⚠️ 價格看不懂，請填數字，例如 5000000 或 500萬。", ephemeral=True)
                return
        wants = self.wants_input.value.strip() if self.wants_input is not None else ""
        qty = ""
        if self.qty_input is not None:
            qty = self.qty_input.value.strip()
            if not qty.isdigit() or int(qty) <= 0:
                await interaction.response.send_message("⚠️ 數量請填大於 0 的整數。", ephemeral=True)
                return
        if self.trade == "都可以" and price is None and not wants:
            await interaction.response.send_message("⚠️ 價格跟想換什麼至少要填一個。", ephemeral=True)
            return
        att = (self.image_input.values or [None])[0]
        problem = check_image(att)
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog("Market")
        forum = await cog.get_forum()
        if forum is None:
            await interaction.followup.send("⚠️ 管理員還沒設定交易區論壇（/setmarket）。", ephemeral=True)
            return
        listing = {
            "掛賣時間": fmt_time(now_tw()), "物品名稱": self.name_input.value.strip(), "分類": self.category,
            "大類": self.group, "交易方式": self.trade, "開價": price or "", "想換": wants,
            "備註": self.note_input.value.strip(), "賣家": interaction.user.display_name,
            "賣家ID": str(interaction.user.id), "狀態": "出售中", "有興趣的人": "", "待確認": "",
            "到期時間": fmt_time(now_tw() + timedelta(days=LONG_CHECK_DAYS if self.trade in LONG else LISTING_DAYS)),
            "已提醒": "", "數量": qty, "成交次數": "0",
        }
        tags = tags_for(forum, listing)
        if not tags and getattr(forum.flags, "require_tag", False):
            missing = tags_missing(forum, [self.group, "出售中"])
            audit.audit("交易區：掛賣失敗，論壇標籤對不上", who=interaction.user.display_name,
                        detail=f"找不到的標籤：{'、'.join(missing)}")
            await interaction.followup.send(
                "⚠️ 交易區論壇的標籤對不上，沒辦法開貼文（論壇要求貼文一定要有標籤）。\n"
                "請通知管理員打 `/setmarket` 檢查標籤，你填的內容沒有送出，等修好之後再掛一次。", ephemeral=True)
            return
        file = None
        if att is not None:
            file, listing["圖片檔名"] = await image_file(att)
        created = await forum.create_thread(
            name=listing_title(listing), embed=listing_embed(listing), applied_tags=tags,
            auto_archive_duration=10080, allowed_mentions=NONE_MENTIONS, **({"file": file} if file else {}))
        thread, starter = created.thread, created.message
        await starter.edit(view=card_view(thread.id, self.trade))
        listing.update({"貼文ID": str(thread.id), "貼文連結": thread.jump_url})
        async with self.bot.store.lock:
            await asyncio.to_thread(self.bot.store.market_add, listing)
        audit.audit("交易區：掛賣", who=interaction.user.display_name,
                    detail=f"{listing['物品名稱']}｜{self.category}｜{self.trade}｜{fmt_price(price) or wants}")
        await interaction.followup.send(f"✅ {'收購貼文已經發出' if self.trade in BUY else '已經上架'}：{thread.jump_url}",
                                        ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：掛賣發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 掛賣失敗，請稍後再試或通知管理員：{error}")


class SellWizardView(discord.ui.View):
    """只有自己看得到的掛賣／收購精靈：大類 → 細分類 → 交易方式（或收購方式）→ 下一步。"""

    def __init__(self, bot, user_id: int, categories: list, buy: bool = False):
        super().__init__(timeout=600)
        self.bot, self.user_id, self.categories, self.buy = bot, user_id, categories, buy
        self.group = self.category = self.trade = None
        groups = list(dict.fromkeys(g for _, g in categories))[:25]
        self.group_select = discord.ui.Select(placeholder="① 選擇大類", row=0,
                                              options=[discord.SelectOption(label=g, value=g) for g in groups])
        self.group_select.callback = self.on_group
        self.category_select = discord.ui.Select(placeholder="② 先選大類", row=1, disabled=True,
                                                 options=[discord.SelectOption(label="（先選大類）", value="-")])
        self.category_select.callback = self.on_category
        if buy:
            trade_options = [
                discord.SelectOption(label="收購", value="收購", emoji="🛒", description="這次要買，收到就結束"),
                discord.SelectOption(label="長期收購", value="長期收購", emoji="🔁", description="一直在收，收滿數量才停")]
        else:
            trade_options = [
                discord.SelectOption(label="出售", value="出售", emoji="💰", description="只收錢"),
                discord.SelectOption(label="交換", value="交換", emoji="🔄", description="只接受以物易物"),
                discord.SelectOption(label="都可以", value="都可以", emoji="🤝", description="收錢或交換都可以"),
                discord.SelectOption(label="贈送", value="贈送", emoji="🎁", description="免費送出，不收任何代價"),
                discord.SelectOption(label="長期供貨", value="長期供貨", emoji="🔁", description="一直有貨，填單價跟庫存")]
        self.trade_select = discord.ui.Select(placeholder="③ 選擇收購方式" if buy else "③ 選擇交易方式", row=2,
                                              options=trade_options)
        self.trade_select.callback = self.on_trade
        for item in (self.group_select, self.category_select, self.trade_select):
            self.add_item(item)

    def _status(self) -> str:
        title = "🛒 收購" if self.buy else "📸 掛賣商品"
        return (f"**{title}**\n大類：{self.group or '—'}　細分類：{self.category or '—'}　"
                f"{'收購方式' if self.buy else '交易方式'}：{self.trade or '—'}\n"
                f"三個都選好之後，按「下一步」填寫{'想買的東西、預算' if self.buy else '名稱、價格'}跟上傳圖片。")

    @staticmethod
    def _keep(select, value):
        for o in select.options:
            o.default = (o.value == value)

    async def on_group(self, interaction: discord.Interaction):
        self.group = self.group_select.values[0]
        self._keep(self.group_select, self.group)
        cats = [c for c, g in self.categories if g == self.group][:25]
        self.category = cats[0] if len(cats) == 1 else None    # 只有一個細分類就自動選好
        self.category_select.options = [discord.SelectOption(label=c, value=c, default=(c == self.category)) for c in cats]
        self.category_select.disabled = False
        self.category_select.placeholder = "② 選擇細分類"
        await interaction.response.edit_message(content=self._status(), view=self)

    async def on_category(self, interaction: discord.Interaction):
        self.category = self.category_select.values[0]
        self._keep(self.category_select, self.category)
        await interaction.response.edit_message(content=self._status(), view=self)

    async def on_trade(self, interaction: discord.Interaction):
        self.trade = self.trade_select.values[0]
        self._keep(self.trade_select, self.trade)
        await interaction.response.edit_message(content=self._status(), view=self)

    @discord.ui.button(label="下一步：填寫內容", emoji="✏️", style=discord.ButtonStyle.primary, row=3)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (self.group and self.category and self.trade):
            await interaction.response.send_message("⚠️ 大類、細分類、交易（收購）方式三個都要先選好。", ephemeral=True)
            return
        await interaction.response.send_modal(ListingModal(self.bot, self.group, self.category, self.trade))


# ---------------- 交易區公告（文字頻道裡的常駐按鈕） ----------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).casefold()


def price_report(deals: list, listings: list, keyword: str) -> str:
    """
    用關鍵字找物品名稱或細分類。成交的部分讀「成交記錄」（取消的不算），用錢成交的照「單價」統計，
    一次買 5 個的不會把平均拉高；另外列出目前在賣、目前在收的。
    """
    key = _norm(keyword)
    match = lambda r: key and (key in _norm(r.get("物品名稱")) or key == _norm(r.get("分類")))
    sold = sorted([d for d in deals if match(d) and d.get("備註") != "已取消"],
                  key=lambda r: r.get("成交時間", ""), reverse=True)
    selling = [r for r in listings if match(r) and r.get("狀態") in ACTIVE and r.get("交易方式") not in BUY]
    buying = [r for r in listings if match(r) and r.get("狀態") in ACTIVE and r.get("交易方式") in BUY]
    if not sold and not selling and not buying:
        return f"📈 找不到「{keyword}」的成交記錄、在賣或在收的。可以試試只打一部分，例如「死靈」。"

    def unit_of(d):
        u = str(d.get("單價") or "")
        if u.isdigit():
            return int(u)
        p, q = str(d.get("成交價") or ""), str(d.get("數量") or "1")
        return int(p) // max(1, int(q)) if p.isdigit() and q.isdigit() else None

    lines = [f"**📈 「{keyword}」的行情**"]
    money = [u for u in (unit_of(d) for d in sold if d.get("成交方式") == "金錢") if u]
    if money:
        lines.append(f"\n💰 **用錢成交：{len(money)} 筆（以單價計算）**\n平均 {fmt_price(sum(money) // len(money))}｜"
                     f"最低 {fmt_price(min(money))}｜最高 {fmt_price(max(money))}")
    else:
        lines.append("\n💰 還沒有用錢成交的記錄")
    if sold:
        lines.append("\n**最近的成交：**")
        for d in sold[:10]:
            date = (d.get("成交時間") or "")[5:10]
            qty = int(d.get("數量") or 1) if str(d.get("數量") or "1").isdigit() else 1
            method = d.get("成交方式")
            if method == "金錢":
                deal = f"💰 {fmt_price(d.get('成交價'))}" + (f"（{qty} 個，單價 {fmt_price(unit_of(d))}）" if qty > 1 else "")
            elif method == "贈送":
                deal = "🎁 送出"
            else:
                deal = f"🔄 換到 {d.get('換得物品')}" + (f"＋補 {fmt_price(d['成交價'])}" if d.get("成交價") else "")
            tag = "🛒" if d.get("交易方式") in BUY else ""
            lines.append(f"・{date}　{tag}{d.get('物品名稱')}　{deal}")
        if len(sold) > 10:
            lines.append(f"…還有 {len(sold) - 10} 筆更早的")
    if selling:
        lines.append("\n**目前在賣：**")
        for r in selling[:6]:
            if r.get("交易方式") == "長期供貨":
                ask = f"🔁 單價 {fmt_price(r['開價'])}，庫存 {r.get('數量') or 0}"
            else:
                ask = f"開價 {fmt_price(r['開價'])}" if r.get("開價") else ("免費贈送" if r.get("交易方式") == "贈送" else "可交換")
            lines.append(f"・{r.get('物品名稱')}　{ask}　{r.get('貼文連結', '')}")
    if buying:
        lines.append("\n**目前在收：**")
        for r in buying[:6]:
            budget = f"預算 {fmt_price(r['開價'])}" if r.get("開價") else "價格可議"
            lines.append(f"・{r.get('物品名稱')}　{budget}，要 {r.get('數量') or 1} 個　{r.get('貼文連結', '')}")
    return "\n".join(lines)[:1900]


class PriceSearchModal(discord.ui.Modal):
    def __init__(self, bot):
        super().__init__(title="查行情")
        self.bot = bot
        self.keyword = discord.ui.TextInput(placeholder="例如：死靈、屠龍刀、弓", max_length=40)
        self.add_item(discord.ui.Label(text="物品名稱（打一部分就可以）", component=self.keyword))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        deals = await asyncio.to_thread(self.bot.store.deals_all)
        listings = await asyncio.to_thread(self.bot.store.market_all)
        await interaction.followup.send(price_report(deals, listings, self.keyword.value.strip()), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：查行情發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 查詢失敗：{error}")


class MarketPanelView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="我要賣東西", emoji="📸", style=discord.ButtonStyle.success, custom_id="mkt:panel:sell")
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("Market")
        if cog.forum_id is None:
            await interaction.response.send_message("⚠️ 管理員還沒設定交易區論壇（/setmarket）。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        categories = await asyncio.to_thread(self.bot.store.get_market_categories)
        view = SellWizardView(self.bot, interaction.user.id, categories)
        await interaction.followup.send(view._status(), view=view, ephemeral=True)

    @discord.ui.button(label="我想買", emoji="🛒", style=discord.ButtonStyle.primary, custom_id="mkt:panel:buy")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("Market")
        if cog.forum_id is None:
            await interaction.response.send_message("⚠️ 管理員還沒設定交易區論壇（/setmarket）。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        categories = await asyncio.to_thread(self.bot.store.get_market_categories)
        view = SellWizardView(self.bot, interaction.user.id, categories, buy=True)
        await interaction.followup.send(view._status(), view=view, ephemeral=True)

    @discord.ui.button(label="我的商品", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="mkt:panel:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.market_by_seller, str(interaction.user.id))
        active = [r for r in rows if r.get("狀態") in OPEN]
        ended = [r for r in rows if r.get("狀態") not in OPEN][-5:]
        if not rows:
            await interaction.followup.send("你還沒有掛賣過任何商品。", ephemeral=True)
            return
        lines = ["**📦 你的商品**"]
        lines += [f"・{r['狀態']}　{r['物品名稱']}　{r.get('貼文連結', '')}" for r in active] or ["（目前沒有掛賣中的商品）"]
        if ended:
            lines += ["", "**最近結束的**"] + [f"・{r['狀態']}　{r['物品名稱']}　{r.get('貼文連結', '')}" for r in reversed(ended)]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    @discord.ui.button(label="查行情", emoji="📈", style=discord.ButtonStyle.secondary, custom_id="mkt:panel:price")
    async def price(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PriceSearchModal(self.bot))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        audit.error("交易區公告按鈕發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 執行時發生錯誤：{error}")


# ---------------- 我有興趣 ----------------

async def express_interest(bot, interaction: discord.Interaction, post_id: int, offer: str = "", att=None):
    """把這個人加進「有興趣的人」，在貼文裡 @ 賣家；第一個有興趣的人出現時，狀態從出售中改成議價中。"""
    store = bot.store
    async with store.lock:
        l = await asyncio.to_thread(store.market_get, post_id)
        if l is not None and l.get("狀態") == PAUSED:
            await reply_error(interaction, "這件目前暫停中（缺貨或暫停收購），等恢復之後再來。")
            return
        if l is None or l.get("狀態") not in ACTIVE:
            await reply_error(interaction, "這件商品已經結束了。")
            return
        uid = str(interaction.user.id)
        if l.get("賣家ID") == uid:
            await reply_error(interaction, "這是你自己發的貼文喔。")
            return
        ids = interested_ids(l)
        if uid in ids and not offer and l.get("交易方式") not in LONG:   # 長期的可以一再回購，不擋
            await reply_error(interaction, "你已經表示過了，直接在貼文裡聊就好。")
            return
        fields = {"有興趣的人": " ".join(ids + ([uid] if uid not in ids else []))}
        if l.get("狀態") == "出售中":
            fields["狀態"] = "議價中"
        l = await asyncio.to_thread(store.market_update, post_id, fields)
    thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else await get_thread(bot, post_id)
    seller = f"<@{l['賣家ID']}>"
    mentions = discord.AllowedMentions(users=[discord.Object(int(l["賣家ID"])), interaction.user])
    if offer:
        text = f"🔄 {interaction.user.mention} 想拿東西交換「{l['物品名稱']}」：\n> {offer}\n{seller} 可以直接在這裡討論。"
        if att is not None:
            file, _ = await image_file(att)
            await thread.send(text, file=file, allowed_mentions=mentions)
        else:
            await thread.send(text, allowed_mentions=mentions)
    elif l.get("交易方式") in BUY:
        await thread.send(f"🙋 {interaction.user.mention} 有「{l['物品名稱']}」可以賣你！\n{seller} 可以直接在這裡議價，"
                          f"談好之後按「⚙️ 買家管理」→「✅ 已成交」。", allowed_mentions=mentions)
    elif l.get("交易方式") == "贈送":
        await thread.send(f"🙋 {interaction.user.mention} 想要「{l['物品名稱']}」！\n{seller} 決定好要送給誰之後，"
                          f"按「⚙️ 賣家管理」→「✅ 已送出」。", allowed_mentions=mentions)
    else:
        await thread.send(f"🙋 {interaction.user.mention} 想買「{l['物品名稱']}」！\n{seller} 可以直接在這裡議價。",
                          allowed_mentions=mentions)
    if "狀態" in fields:
        await refresh_post(bot, l)
    audit.audit("交易區：有興趣", who=interaction.user.display_name,
                detail=f"{l['物品名稱']}｜{offer or ('有貨' if l.get('交易方式') in BUY else '想買')}")
    await reply_error(interaction, f"✅ 已經通知{'買家' if l.get('交易方式') in BUY else '賣家'}了，接下來直接在貼文裡議價吧。")


class OfferModal(discord.ui.Modal):
    def __init__(self, bot, post_id: int):
        super().__init__(title="我想拿東西交換")
        self.bot, self.post_id = bot, post_id
        self.offer_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=300,
                                                placeholder="例如：精靈弓 +5，再補 50 萬")
        self.add_item(discord.ui.Label(text="你想拿什麼交換？", component=self.offer_input))
        self.image_input = discord.ui.FileUpload(required=False, max_values=1)
        self.add_item(discord.ui.Label(text="你的物品圖片（選填）", component=self.image_input))

    async def on_submit(self, interaction: discord.Interaction):
        att = (self.image_input.values or [None])[0]
        problem = check_image(att)
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await express_interest(self.bot, interaction, self.post_id, offer=self.offer_input.value.strip(), att=att)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：提出交換發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 發生錯誤，請稍後再試或通知管理員：{error}")


class BuyOrSwapView(discord.ui.View):
    """「都可以」的商品：先問要出價還是拿東西換。"""

    def __init__(self, bot, post_id: int):
        super().__init__(timeout=180)
        self.bot, self.post_id = bot, post_id

    @discord.ui.button(label="我想用錢買", emoji="💰", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, express_interest(self.bot, interaction, self.post_id), "表示想買")

    @discord.ui.button(label="我想拿東西換", emoji="🔄", style=discord.ButtonStyle.primary)
    async def swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OfferModal(self.bot, self.post_id))


class InterestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mkt:i:(?P<pid>[0-9]+):(?P<t>[sxbglpq])"):
    """
    交易方式直接寫在按鈕 ID 裡（s 出售、x 交換、b 都可以、g 贈送、l 長期供貨、p 收購、q 長期收購）：交換的話按下去要馬上跳出輸入視窗，
    輸入視窗必須是第一個回應、要在 3 秒內，不能先去查試算表。檢查（是不是自己的、結束了沒）在送出時才做。
    """

    def __init__(self, post_id: int, trade_code: str = "s"):
        label = {"g": "我想要", "p": "我有這個", "q": "我有這個"}.get(trade_code, "我有興趣")
        super().__init__(discord.ui.Button(label=label, emoji="🙋",
                                           style=discord.ButtonStyle.success, custom_id=f"mkt:i:{post_id}:{trade_code}"))
        self.post_id, self.trade_code = post_id, trade_code

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), match["t"])

    async def callback(self, interaction: discord.Interaction):
        await guarded(interaction, self._run(interaction), "我有興趣")

    async def _run(self, interaction: discord.Interaction):
        bot = interaction.client
        if self.trade_code == "x":
            await interaction.response.send_modal(OfferModal(bot, self.post_id))
        elif self.trade_code == "b":
            await interaction.response.send_message("這件商品可以用錢買，也可以拿東西換，你想要哪一種？",
                                                    view=BuyOrSwapView(bot, self.post_id), ephemeral=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await express_interest(bot, interaction, self.post_id)


# ---------------- 賣家管理：修改、已成交、下架 ----------------

def can_manage(interaction: discord.Interaction, l: dict) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return l.get("賣家ID") == str(interaction.user.id) or bool(perms and perms.manage_guild)


class EditModal(discord.ui.Modal):
    def __init__(self, bot, l: dict):
        super().__init__(title="修改商品")
        self.bot, self.post_id, self.trade = bot, int(l["貼文ID"]), l.get("交易方式")
        self.name_input = discord.ui.TextInput(default=l.get("物品名稱"), max_length=40)
        self.add_item(discord.ui.Label(text="物品名稱", component=self.name_input))
        self.price_input = self.wants_input = None
        if self.trade in ("出售", "都可以"):
            self.price_input = discord.ui.TextInput(default=l.get("開價") or None, max_length=20,
                                                    required=(self.trade == "出售"))
            self.add_item(discord.ui.Label(text="價格", component=self.price_input))
        if self.trade in ("交換", "都可以"):
            self.wants_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, default=l.get("想換") or None,
                                                    max_length=200, required=(self.trade == "交換"))
            self.add_item(discord.ui.Label(text="想換什麼", component=self.wants_input))
        self.note_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, default=l.get("備註") or None,
                                               max_length=300, required=False)
        self.add_item(discord.ui.Label(text="備註", component=self.note_input))

    async def on_submit(self, interaction: discord.Interaction):
        fields = {"物品名稱": self.name_input.value.strip(), "備註": self.note_input.value.strip()}
        if self.price_input is not None:
            text = self.price_input.value.strip()
            price = parse_price(text) if text else None
            if text and price is None:
                await interaction.response.send_message("⚠️ 價格看不懂，請填數字，例如 5000000 或 500萬。", ephemeral=True)
                return
            fields["開價"] = price or ""
        if self.wants_input is not None:
            fields["想換"] = self.wants_input.value.strip()
        if self.trade == "都可以" and not fields.get("開價") and not fields.get("想換"):
            await interaction.response.send_message("⚠️ 價格跟想換什麼至少要填一個。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.store.lock:
            l = await asyncio.to_thread(self.bot.store.market_update, self.post_id, fields)
        await refresh_post(self.bot, l)
        audit.audit("交易區：修改商品", who=interaction.user.display_name, detail=f"{l['物品名稱']}｜{fields}")
        await interaction.followup.send("✅ 已經更新商品內容。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：修改商品發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 修改失敗：{error}")


async def send_deal_confirmation(bot, interaction: discord.Interaction, post_id: int, buyer, method: str,
                                 price="", item="", qty: int = 1, unit=""):
    """
    記下待確認的成交內容（附一次性驗證碼），在貼文裡請「對方」確認。呼叫前要先 defer。
    對方：掛賣的貼文是買家；收購的貼文是賣給他的人。pending 裡的 buyer_id 一律指「要按確認的那個人」。
    """
    nonce = secrets.token_hex(3)
    pending = {"buyer_id": str(buyer.id), "buyer": buyer.display_name, "method": method,
               "price": price, "item": item, "qty": qty, "unit": unit, "nonce": nonce}
    async with bot.store.lock:
        l = await asyncio.to_thread(bot.store.market_get, post_id)
        if l is None or l.get("狀態") not in ACTIVE:
            await interaction.followup.send("這件商品已經結束了。", ephemeral=True)
            return
        if l.get("交易方式") in LONG and qty > int(l.get("數量") or 0):
            await interaction.followup.send(f"⚠️ 數量不夠：現在只有 {l.get('數量') or 0} 個。", ephemeral=True)
            return
        await asyncio.to_thread(bot.store.market_update, post_id, {"待確認": json.dumps(pending, ensure_ascii=False)})
    thread = await get_thread(bot, post_id)
    view = discord.ui.View(timeout=None)
    view.add_item(ConfirmDealButton(post_id, nonce))
    view.add_item(RejectDealButton(post_id, nonce))
    if method == "贈送":
        text = (f"🎁 <@{l['賣家ID']}> 要把「{l['物品名稱']}」送給 {buyer.mention}！\n"
                f"{buyer.mention} 收到之後請按「確認成交」（只有你能按）。")
    else:
        deal = []
        if l.get("交易方式") in LONG:
            deal.append(f"**{qty}** 個 × 單價 {fmt_price(unit)} ＝ 共 **{fmt_price(price)}**")
        elif price:
            deal.append(f"{'成交價' if method == '金錢' else '補差價'} **{fmt_price(price)}**")
        if item:
            deal.append(f"換得 **{item}**")
        if l.get("交易方式") in BUY:
            text = (f"🤝 <@{l['賣家ID']}> 確認向 {buyer.mention} 收購「{l['物品名稱']}」：{'，'.join(deal)}\n"
                    f"{buyer.mention} 請確認這筆交易（只有你能按）。")
        else:
            text = (f"🤝 <@{l['賣家ID']}> 確認把「{l['物品名稱']}」交易給 {buyer.mention}：{'，'.join(deal)}\n"
                    f"{buyer.mention} 請確認這筆交易（只有你能按）。")
    await thread.send(text, view=view, allowed_mentions=discord.AllowedMentions(users=[buyer]))
    audit.audit("交易區：送出成交確認", who=interaction.user.display_name,
                detail=f"{l['物品名稱']}｜對象 {buyer.display_name}｜{method}｜{price or ''} {item}")
    await interaction.followup.send(f"✅ 已經請 {buyer.display_name} 在貼文裡確認，他按下確認才算完成。", ephemeral=True)


def deal_summary(l: dict) -> str:
    parts = [f"對象 <@{l.get('買家ID')}>", METHOD_TEXT.get(l.get("成交方式"), l.get("成交方式") or "")]
    if l.get("成交價"):
        parts.append(f"金額 {fmt_price(l['成交價'])}")
    if l.get("換得物品"):
        parts.append(f"換得 {l['換得物品']}")
    return "，".join(p for p in parts if p)


async def post_admin_note(bot, post_id: int, text: str):
    """在已鎖定、收起來的貼文裡公開留一則說明（先打開，refresh_post 會再依狀態鎖回去）。"""
    thread = await get_thread(bot, post_id)
    # 先明確打開、解鎖再留言，不依賴「有管理權限的機器人能不能在鎖定的貼文發言」這種細節；
    # 留完言之後 refresh_post 會依照狀態再鎖回去
    if getattr(thread, "archived", False) or getattr(thread, "locked", False):
        await thread.edit(archived=False, locked=False)
    await thread.send(text, allowed_mentions=NONE_MENTIONS)


async def apply_deal_fix(bot, interaction: discord.Interaction, post_id: int, buyer, method: str, price="", item=""):
    """管理員修正已成交的內容：直接寫入，並在貼文裡公開說明改了什麼（原本 → 現在）。"""
    async with bot.store.lock:
        before = await asyncio.to_thread(bot.store.market_get, post_id)
        if before is None or before.get("狀態") != "已售出":
            await interaction.followup.send("只有已成交的商品可以修正。", ephemeral=True)
            return
        old = deal_summary(before)
        l = await asyncio.to_thread(bot.store.market_update, post_id, {
            "買家": buyer.display_name, "買家ID": str(buyer.id), "成交方式": method, "成交價": price, "換得物品": item})
        side = ("賣家", "賣家ID") if l.get("交易方式") in BUY else ("買家", "買家ID")   # 收購貼文：對方是賣家
        await asyncio.to_thread(bot.store.deal_update_last, post_id, {
            side[0]: buyer.display_name, side[1]: str(buyer.id), "成交方式": method, "成交價": price,
            "單價": price if method == "金錢" else "", "換得物品": item})
    await post_admin_note(bot, post_id, f"🛠️ 管理員 {interaction.user.display_name} 修正了成交內容：\n"
                                        f"原本：{old}\n現在：{deal_summary(l)}")
    await refresh_post(bot, l)
    audit.audit("交易區：管理員修正成交", who=interaction.user.display_name,
                detail=f"{l['物品名稱']}｜原本 {old}｜現在 {deal_summary(l)}")
    await interaction.followup.send("✅ 已經修正成交內容，並在貼文裡留下說明。", ephemeral=True)


async def cancel_deal(bot, interaction: discord.Interaction, post_id: int):
    """管理員取消已成交的交易：清掉成交資料、重新上架（到期時間重新算 30 天），在貼文裡公開說明。"""
    async with bot.store.lock:
        before = await asyncio.to_thread(bot.store.market_get, post_id)
        if before is None or before.get("狀態") != "已售出":
            await interaction.followup.send("只有已成交的商品可以取消成交。", ephemeral=True)
            return
        old = deal_summary(before)
        l = await asyncio.to_thread(bot.store.market_update, post_id, {
            "狀態": "議價中" if interested_ids(before) else "出售中",
            "成交時間": "", "買家": "", "買家ID": "", "成交方式": "", "成交價": "", "換得物品": "", "待確認": "",
            "到期時間": fmt_time(now_tw() + timedelta(days=LISTING_DAYS)), "已提醒": ""})
        await asyncio.to_thread(bot.store.deal_update_last, post_id, {"備註": "已取消"})
    await post_admin_note(bot, post_id, f"↩️ 管理員 {interaction.user.display_name} 取消了這筆成交（原本：{old}），"
                                        f"商品重新上架，可以繼續交易。")
    await refresh_post(bot, l)
    audit.audit("交易區：管理員取消成交", who=interaction.user.display_name, detail=f"{l['物品名稱']}｜原本 {old}")
    await interaction.followup.send("✅ 已經取消成交，商品重新上架，並在貼文裡留下說明。", ephemeral=True)


class FixDealView(discord.ui.View):
    """/fixdeal 的選單（只有管理員看得到）：修改成交內容、取消成交並重新上架。"""

    def __init__(self, bot, l: dict):
        super().__init__(timeout=600)
        self.bot, self.l = bot, l

    @discord.ui.button(label="修改成交內容", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**✏️ 修正：{self.l['物品名稱']}**\n已經帶入原本的內容，只改要改的地方，再按下一步。",
            view=DealSetupView(self.bot, self.l, fix=True), ephemeral=True)

    @discord.ui.button(label="取消成交，重新上架", emoji="↩️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=120)
        confirm = discord.ui.Button(label="確定取消成交", style=discord.ButtonStyle.danger)

        async def do_cancel(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=True)
            await guarded(inter, cancel_deal(self.bot, inter, int(self.l["貼文ID"])), "取消成交")
        confirm.callback = do_cancel
        view.add_item(confirm)
        await interaction.response.send_message(
            f"確定要取消「{self.l['物品名稱']}」的成交嗎？試算表的成交資料會清掉、商品重新上架，貼文裡會公開留下說明。",
            view=view, ephemeral=True)


class DealModal(discord.ui.Modal):
    def __init__(self, bot, post_id: int, buyer: discord.abc.User, method: str, fix: bool = False, current: dict = None,
                 listing: dict = None):
        super().__init__(title="修正成交內容" if fix else "成交內容")
        self.bot, self.post_id, self.buyer, self.method, self.fix = bot, post_id, buyer, method, fix
        current = current or {}
        self.listing = listing or {}
        self.price_input = self.item_input = self.qty_input = self.unit_input = None
        if self.listing.get("交易方式") in LONG and not fix:     # 長期的：數量 × 單價
            left = self.listing.get("數量") or "0"
            self.qty_input = discord.ui.TextInput(placeholder="例如 5", max_length=6)
            self.add_item(discord.ui.Label(text="數量", component=self.qty_input, description=f"目前還有 {left} 個"))
            self.unit_input = discord.ui.TextInput(default=self.listing.get("開價") or None, max_length=20,
                                                   placeholder="例如 50000、5萬")
            self.add_item(discord.ui.Label(text="單價", component=self.unit_input))
            return
        if method in ("金錢", "兩者都有"):
            self.price_input = discord.ui.TextInput(placeholder="例如 4500000、450萬", max_length=20,
                                                    default=current.get("成交價") or None)
            self.add_item(discord.ui.Label(text="成交價" if method == "金錢" else "補差價的金額", component=self.price_input))
        if method in ("交換", "兩者都有"):
            self.item_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=200,
                                                   placeholder="例如：精靈弓 +5", default=current.get("換得物品") or None)
            self.add_item(discord.ui.Label(text="換到的物品", component=self.item_input))

    async def on_submit(self, interaction: discord.Interaction):
        if self.qty_input is not None:
            qty = self.qty_input.value.strip()
            unit = parse_price(self.unit_input.value)
            if not qty.isdigit() or int(qty) <= 0:
                await interaction.response.send_message("⚠️ 數量請填大於 0 的整數。", ephemeral=True)
                return
            if unit is None:
                await interaction.response.send_message("⚠️ 單價看不懂，請填數字，例如 50000 或 5萬。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            await send_deal_confirmation(self.bot, interaction, self.post_id, self.buyer, self.method,
                                         price=int(qty) * unit, qty=int(qty), unit=unit)
            return
        price = ""
        if self.price_input is not None:
            price = parse_price(self.price_input.value)
            if price is None:
                await interaction.response.send_message("⚠️ 金額看不懂，請填數字，例如 4500000 或 450萬。", ephemeral=True)
                return
        item = self.item_input.value.strip() if self.item_input is not None else ""
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.fix:
            await apply_deal_fix(self.bot, interaction, self.post_id, self.buyer, self.method, price, item)
        else:
            await send_deal_confirmation(self.bot, interaction, self.post_id, self.buyer, self.method, price, item)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：成交內容發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 發生錯誤：{error}")


class DealSetupView(discord.ui.View):
    """賣家選成交對象（Discord 內建的成員選擇器，可以搜尋）跟成交方式。"""

    def __init__(self, bot, l: dict, fix: bool = False):
        super().__init__(timeout=600)
        self.bot, self.l, self.buyer, self.fix = bot, l, None, fix
        trade = l.get("交易方式")
        methods = {"出售": ["金錢"], "交換": ["交換", "兩者都有"], "都可以": ["金錢", "交換", "兩者都有"],
                   "贈送": ["贈送"], "長期供貨": ["金錢"], "收購": ["金錢", "交換", "兩者都有"],
                   "長期收購": ["金錢"]}.get(trade, ["金錢"])
        if trade == "贈送":
            self.next_step.label = "送出：請對方確認"
        self.method = methods[0] if len(methods) == 1 else None
        self.user_select = discord.ui.UserSelect(
            placeholder="選擇要送給誰" if trade == "贈送" else "選擇賣給你的人" if trade in BUY else "選擇成交對象", row=0)
        self.user_select.callback = self.on_user
        self.add_item(self.user_select)
        if len(methods) > 1:
            labels = {"金錢": "💰 金錢", "交換": "🔄 交換", "兩者都有": "🔄💰 交換＋補差價"}
            self.method_select = discord.ui.Select(placeholder="選擇成交方式", row=1,
                                                   options=[discord.SelectOption(label=labels[m], value=m) for m in methods])
            self.method_select.callback = self.on_method
            self.add_item(self.method_select)
        if fix:   # 修正模式：帶入原本的買家、成交方式，只改要改的地方就好
            self.next_step.label = "套用修正" if trade == "贈送" else "下一步：修正成交內容"
            if l.get("買家ID", "").isdigit():
                bid = int(l["買家ID"])
                self.buyer = type("Buyer", (), {"id": bid, "display_name": l.get("買家", ""), "mention": f"<@{bid}>"})()
                self.user_select.default_values = [discord.Object(bid)]
            if l.get("成交方式") in methods:
                self.method = l["成交方式"]
                if len(methods) > 1:
                    for o in self.method_select.options:
                        o.default = (o.value == self.method)

    async def on_user(self, interaction: discord.Interaction):
        user = self.user_select.values[0]
        if str(user.id) == self.l.get("賣家ID") or getattr(user, "bot", False):
            await interaction.response.send_message("⚠️ 成交對象不能是自己或機器人。", ephemeral=True)
            return
        self.buyer = user
        await interaction.response.defer()

    async def on_method(self, interaction: discord.Interaction):
        self.method = self.method_select.values[0]
        for o in self.method_select.options:
            o.default = (o.value == self.method)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="下一步：填成交內容", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.buyer is None or self.method is None:
            await interaction.response.send_message("⚠️ 成交對象跟成交方式都要先選好。", ephemeral=True)
            return
        if self.method == "贈送":   # 贈品沒有金額跟換得的物品，直接請對方確認（修正模式則直接套用）
            await interaction.response.defer(ephemeral=True, thinking=True)
            if self.fix:
                await apply_deal_fix(self.bot, interaction, int(self.l["貼文ID"]), self.buyer, "贈送")
            else:
                await send_deal_confirmation(self.bot, interaction, int(self.l["貼文ID"]), self.buyer, "贈送")
            return
        await interaction.response.send_modal(DealModal(self.bot, int(self.l["貼文ID"]), self.buyer, self.method,
                                                        fix=self.fix, current=self.l if self.fix else None, listing=self.l))


class StockModal(discord.ui.Modal):
    """長期供貨補貨／長期收購調整數量：直接填新的數量。數量大於 0 而且是暫停中，就自動恢復。"""

    def __init__(self, bot, l: dict):
        super().__init__(title="補貨" if l.get("交易方式") == "長期供貨" else "調整還要收的數量")
        self.bot, self.post_id = bot, int(l["貼文ID"])
        self.qty = discord.ui.TextInput(default=l.get("數量") or "0", max_length=6)
        self.add_item(discord.ui.Label(text="新的庫存" if l.get("交易方式") == "長期供貨" else "還要收幾個",
                                       component=self.qty, description=f"現在是 {l.get('數量') or 0} 個"))

    async def on_submit(self, interaction: discord.Interaction):
        v = self.qty.value.strip()
        if not v.isdigit():
            await interaction.response.send_message("⚠️ 請填 0 以上的整數。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.bot.store.lock:
            l = await asyncio.to_thread(self.bot.store.market_get, self.post_id)
            if l is None or l.get("狀態") not in OPEN:
                await interaction.followup.send("這件商品已經結束了。", ephemeral=True)
                return
            fields = {"數量": v}
            if int(v) == 0:
                fields["狀態"] = PAUSED
            elif l.get("狀態") == PAUSED:
                fields["狀態"] = "議價中" if interested_ids(l) else "出售中"
            l = await asyncio.to_thread(self.bot.store.market_update, self.post_id, fields)
        await refresh_post(self.bot, l)
        audit.audit("交易區：調整數量", who=interaction.user.display_name, detail=f"{l['物品名稱']}｜{v}")
        extra = "，已經自動暫停" if int(v) == 0 else ("，已經恢復" if fields.get("狀態") in ACTIVE else "")
        await interaction.followup.send(f"✅ 數量改成 {v} 個{extra}。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：調整數量發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 調整失敗：{error}")


class ManageView(discord.ui.View):
    def __init__(self, bot, l: dict):
        super().__init__(timeout=600)
        self.bot, self.l = bot, l
        trade = l.get("交易方式")
        if trade == "贈送":
            self.deal.label, self.deal.emoji = "已送出", "🎁"
        if trade not in LONG:                       # 補貨、暫停只有長期的才有
            self.remove_item(self.stock)
            self.remove_item(self.pause)
        else:
            if trade == "長期收購":
                self.stock.label = "調整數量"
            if l.get("狀態") == PAUSED:
                self.pause.label, self.pause.emoji = "恢復", "▶️"
                self.remove_item(self.deal)        # 暫停中不能成交

    @discord.ui.button(label="修改", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditModal(self.bot, self.l))

    @discord.ui.button(label="已成交", emoji="✅", style=discord.ButtonStyle.success)
    async def deal(self, interaction: discord.Interaction, button: discord.ui.Button):
        names = [f"<@{i}>" for i in interested_ids(self.l)]
        if self.l.get("交易方式") == "贈送":
            hint = f"想要的人：{'、'.join(names)}\n" if names else ""
            await interaction.response.send_message(
                f"**🎁 送出：{self.l['物品名稱']}**\n{hint}選好要送給誰，按「送出」。對方確認收到之後才算完成。",
                view=DealSetupView(self.bot, self.l), ephemeral=True, allowed_mentions=NONE_MENTIONS)
            return
        hint = f"有興趣的人：{'、'.join(names)}\n" if names else ""
        await interaction.response.send_message(
            f"**✅ 成交：{self.l['物品名稱']}**\n{hint}選好成交對象和成交方式，再按「下一步」。對方確認之後才算成交。",
            view=DealSetupView(self.bot, self.l), ephemeral=True, allowed_mentions=NONE_MENTIONS)

    @discord.ui.button(label="補貨", emoji="📦", style=discord.ButtonStyle.secondary)
    async def stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StockModal(self.bot, self.l))

    @discord.ui.button(label="暫停", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pid = int(self.l["貼文ID"])
        async with self.bot.store.lock:
            l = await asyncio.to_thread(self.bot.store.market_get, pid)
            if l is None or l.get("狀態") not in OPEN:
                await interaction.followup.send("這件商品已經結束了。", ephemeral=True)
                return
            if l.get("狀態") == PAUSED:
                if int(l.get("數量") or 0) <= 0:
                    await interaction.followup.send("數量是 0，請先按「補貨／調整數量」。", ephemeral=True)
                    return
                new = "議價中" if interested_ids(l) else "出售中"
            else:
                new = PAUSED
            l = await asyncio.to_thread(self.bot.store.market_update, pid, {"狀態": new, "待確認": ""})
        await refresh_post(self.bot, l)
        audit.audit("交易區：暫停" if new == PAUSED else "交易區：恢復", who=interaction.user.display_name, detail=l["物品名稱"])
        await interaction.followup.send("⏸️ 已經暫停，別人暫時不能按我有興趣。" if new == PAUSED else "▶️ 已經恢復。",
                                        ephemeral=True)

    @discord.ui.button(label="下架", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=120)
        confirm = discord.ui.Button(label="確定下架", style=discord.ButtonStyle.danger)

        async def do_remove(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=True)
            async with self.bot.store.lock:
                l = await asyncio.to_thread(self.bot.store.market_get, int(self.l["貼文ID"]))
                if l is None or l.get("狀態") not in OPEN:
                    await inter.followup.send("這件商品已經結束了。", ephemeral=True)
                    return
                l = await asyncio.to_thread(self.bot.store.market_update, int(self.l["貼文ID"]),
                                            {"狀態": "已下架", "待確認": ""})
            await refresh_post(self.bot, l)
            audit.audit("交易區：下架", who=inter.user.display_name, detail=l["物品名稱"])
            await inter.followup.send("🗑️ 已經下架，貼文已鎖定。", ephemeral=True)
        confirm.callback = do_remove
        view.add_item(confirm)
        await interaction.response.send_message(f"確定要下架「{self.l['物品名稱']}」嗎？下架後貼文會鎖定。",
                                                view=view, ephemeral=True)


class ManageButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mkt:m:(?P<pid>[0-9]+)"):
    def __init__(self, post_id: int, label: str = "賣家管理"):
        super().__init__(discord.ui.Button(label=label, emoji="⚙️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mkt:m:{post_id}"))
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), getattr(item, "label", None) or "賣家管理")

    async def callback(self, interaction: discord.Interaction):
        await guarded(interaction, self._run(interaction), "賣家管理")

    async def _run(self, interaction: discord.Interaction):
        bot = interaction.client
        await interaction.response.defer(ephemeral=True, thinking=True)
        l = await asyncio.to_thread(bot.store.market_get, self.post_id)
        if l is None:
            await reply_error(interaction, "找不到這件商品的資料。")
            return
        if not can_manage(interaction, l):
            await reply_error(interaction, "只有發文的人可以管理這篇。")
            return
        if l.get("狀態") not in OPEN:
            await reply_error(interaction, "這件商品已經結束了。")
            return
        what = "收購" if l.get("交易方式") in BUY else "商品"
        await interaction.followup.send(f"**⚙️ 管理你的{what}：{l['物品名稱']}**", view=ManageView(bot, l), ephemeral=True)


# ---------------- 買家確認成交 ----------------

def deal_row(l: dict, pending: dict) -> dict:
    """一筆成交寫進「成交記錄」的內容。收購貼文：發文的人是買家，按確認的人是賣家。"""
    poster = (l.get("賣家", ""), l.get("賣家ID", ""))
    other = (pending["buyer"], pending["buyer_id"])
    seller, buyer = (other, poster) if l.get("交易方式") in BUY else (poster, other)
    qty = int(pending.get("qty") or 1)
    price = pending.get("price") or ""
    unit = pending.get("unit") or (int(price) // qty if str(price).isdigit() and pending.get("method") == "金錢" else "")
    return {"成交時間": fmt_time(now_tw()), "貼文ID": l["貼文ID"], "物品名稱": l.get("物品名稱"), "分類": l.get("分類"),
            "大類": l.get("大類"), "交易方式": l.get("交易方式"), "賣家": seller[0], "賣家ID": seller[1],
            "買家": buyer[0], "買家ID": buyer[1], "成交方式": pending.get("method"), "數量": qty, "單價": unit,
            "成交價": price, "換得物品": pending.get("item") or "", "貼文連結": l.get("貼文連結", ""), "備註": ""}


class _DealMixin:
    """確認成交／有誤共用的檢查。不能直接繼承 DynamicItem：discord.py 規定每個動態按鈕類別都要有自己的 template，
    共用的父類別如果繼承它，載入時就會出錯、整支機器人啟動失敗。所以寫成混入，讓兩個按鈕各自繼承。"""

    async def _load(self, interaction: discord.Interaction):
        bot = interaction.client
        l = await asyncio.to_thread(bot.store.market_get, self.post_id)
        pending = json.loads(l["待確認"]) if l and l.get("待確認") else None
        if not pending or pending.get("nonce") != self.nonce or l.get("狀態") not in ACTIVE:
            await reply_error(interaction, "這個確認已經失效了（可能賣家重新送出、或交易已經結束）。")
            return None, None
        if str(interaction.user.id) != pending["buyer_id"]:
            await reply_error(interaction, "只有被指定的交易對象可以按這個按鈕。")
            return None, None
        return l, pending


class ConfirmDealButton(_DealMixin, discord.ui.DynamicItem[discord.ui.Button],
                        template=r"mkt:c:(?P<pid>[0-9]+):(?P<nonce>[0-9a-f]{6})"):
    def __init__(self, post_id: int, nonce: str):
        super().__init__(discord.ui.Button(label="確認成交", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id=f"mkt:c:{post_id}:{nonce}"))
        self.post_id, self.nonce = post_id, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await guarded(interaction, self._run(interaction), "確認成交")

    async def _run(self, interaction: discord.Interaction):
        bot = interaction.client
        async with bot.store.lock:
            l, pending = await self._load(interaction)
            if l is None:
                return
            # 先確定成交記錄分頁存在（第一次會把以前的成交搬過去），再把這筆標成已售出，才不會被搬到、變成兩筆
            await asyncio.to_thread(bot.store._deal_sheet)
            row = deal_row(l, pending)
            if l.get("交易方式") in LONG:
                left = int(l.get("數量") or 0) - int(pending.get("qty") or 1)
                if left < 0:
                    await reply_error(interaction, f"⚠️ 數量不夠了（現在只剩 {l.get('數量') or 0} 個），請對方重新送出。")
                    return
                count = int(l.get("成交次數") or 0) + 1
                l = await asyncio.to_thread(bot.store.market_update, self.post_id, {
                    "數量": left, "成交次數": count, "待確認": "",
                    "狀態": PAUSED if left == 0 else l.get("狀態")})
            else:
                l = await asyncio.to_thread(bot.store.market_update, self.post_id, {
                    "狀態": "已售出", "成交時間": row["成交時間"], "買家": pending["buyer"], "買家ID": pending["buyer_id"],
                    "成交方式": pending["method"], "成交價": pending["price"], "換得物品": pending["item"], "待確認": "",
                })
            await asyncio.to_thread(bot.store.deal_add, row)
        await interaction.message.edit(content=f"{interaction.message.content}\n\n✅ **{interaction.user.display_name} 已確認成交**",
                                       view=None, allowed_mentions=NONE_MENTIONS)
        if l.get("交易方式") in LONG:
            thread = await get_thread(bot, self.post_id)
            what = "庫存" if l.get("交易方式") == "長期供貨" else "還要收"
            note = (f"✅ 第 {l['成交次數']} 筆成交：<@{row['買家ID']}> 向 <@{row['賣家ID']}> 買了 {row['數量']} 個，"
                    f"共 {fmt_price(row['成交價'])}。{what}剩 {l['數量']} 個。")
            if l.get("狀態") == PAUSED:
                note += "\n⏸️ 數量用完了，已經自動暫停；按「⚙️ 管理」→「補貨／調整數量」就能恢復。"
            await thread.send(note, allowed_mentions=NONE_MENTIONS)
        await refresh_post(bot, l)
        audit.audit("交易區：成交", who=interaction.user.display_name,
                    detail=f"{row['物品名稱']}｜賣家 {row['賣家']}｜買家 {row['買家']}｜{row['成交方式']}｜"
                           f"{row['數量']} 個｜{row['成交價']} {row['換得物品']}")


class RejectDealButton(_DealMixin, discord.ui.DynamicItem[discord.ui.Button],
                       template=r"mkt:x:(?P<pid>[0-9]+):(?P<nonce>[0-9a-f]{6})"):
    def __init__(self, post_id: int, nonce: str):
        super().__init__(discord.ui.Button(label="有誤", emoji="❌", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mkt:x:{post_id}:{nonce}"))
        self.post_id, self.nonce = post_id, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), match["nonce"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await guarded(interaction, self._run(interaction), "成交有誤")

    async def _run(self, interaction: discord.Interaction):
        bot = interaction.client
        async with bot.store.lock:
            l, pending = await self._load(interaction)
            if l is None:
                return
            await asyncio.to_thread(bot.store.market_update, self.post_id, {"待確認": ""})
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n❌ **{interaction.user.display_name} 表示內容有誤**，"
                    f"<@{l['賣家ID']}> 請再確認一次，重新從「⚙️ 賣家管理」送出。",
            view=None, allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(l["賣家ID"]))]))
        audit.audit("交易區：買家表示成交有誤", who=interaction.user.display_name, detail=l["物品名稱"])


# ---------------- 到期提醒的按鈕：續期、讓它下架 ----------------

async def take_down(bot, post_id: int, reason: str = "") -> Optional[dict]:
    """把商品下架並鎖定貼文；已經結束的回傳 None。reason 有的話先在貼文裡說明原因（鎖定前）。"""
    async with bot.store.lock:
        l = await asyncio.to_thread(bot.store.market_get, post_id)
        if l is None or l.get("狀態") not in OPEN:
            return None
        l = await asyncio.to_thread(bot.store.market_update, post_id, {"狀態": "已下架", "待確認": ""})
    if reason:
        thread = await get_thread(bot, post_id)
        await thread.send(reason, allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(l["賣家ID"]))]))
    await refresh_post(bot, l)
    return l


class _SellerOnlyMixin:
    """續期／讓它下架：只有賣家跟管理員能按。共用的檢查寫成混入（DynamicItem 不能有共用的父類別）。"""

    async def _load(self, interaction: discord.Interaction):
        l = await asyncio.to_thread(interaction.client.store.market_get, self.post_id)
        if l is None:
            await reply_error(interaction, "找不到這件商品的資料。")
            return None
        if not can_manage(interaction, l):
            await reply_error(interaction, "只有賣家可以操作這個按鈕。")
            return None
        if l.get("狀態") not in OPEN:
            await reply_error(interaction, "這件商品已經結束了，想繼續賣的話請重新掛賣一次。")
            return None
        return l


class RenewButton(_SellerOnlyMixin, discord.ui.DynamicItem[discord.ui.Button], template=r"mkt:rn:(?P<pid>[0-9]+)"):
    def __init__(self, post_id: int, label: str = f"續期 {LISTING_DAYS} 天"):
        super().__init__(discord.ui.Button(label=label, emoji="🔄", style=discord.ButtonStyle.success,
                                           custom_id=f"mkt:rn:{post_id}"))
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]), getattr(item, "label", None) or f"續期 {LISTING_DAYS} 天")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await guarded(interaction, self._run(interaction), "續期")

    async def _run(self, interaction: discord.Interaction):
        bot = interaction.client
        if await self._load(interaction) is None:
            return
        l0 = await asyncio.to_thread(bot.store.market_get, self.post_id)
        new_exp = now_tw() + timedelta(days=LONG_CHECK_DAYS if l0.get("交易方式") in LONG else LISTING_DAYS)
        async with bot.store.lock:
            l = await asyncio.to_thread(bot.store.market_update, self.post_id, {"到期時間": fmt_time(new_exp), "已提醒": ""})
        await interaction.message.edit(
            content=f"{interaction.message.content}\n\n🔄 **已確認**，下次確認時間：{fmt_time(new_exp)[:16]}"
            if l0.get("交易方式") in LONG else
            f"{interaction.message.content}\n\n🔄 **已續期**，新的到期時間：{fmt_time(new_exp)[:16]}",
            view=None, allowed_mentions=NONE_MENTIONS)
        await refresh_post(bot, l)
        audit.audit("交易區：續期", who=interaction.user.display_name, detail=f"{l['物品名稱']}｜到 {fmt_time(new_exp)}")


class ExpireNowButton(_SellerOnlyMixin, discord.ui.DynamicItem[discord.ui.Button], template=r"mkt:ex:(?P<pid>[0-9]+)"):
    def __init__(self, post_id: int):
        super().__init__(discord.ui.Button(label="讓它下架", emoji="🗑️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mkt:ex:{post_id}"))
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await guarded(interaction, self._run(interaction), "讓它下架")

    async def _run(self, interaction: discord.Interaction):
        if await self._load(interaction) is None:
            return
        await interaction.message.edit(content=f"{interaction.message.content}\n\n🗑️ **賣家選擇下架**",
                                       view=None, allowed_mentions=NONE_MENTIONS)
        l = await take_down(interaction.client, self.post_id)
        if l:
            audit.audit("交易區：到期前自己下架", who=interaction.user.display_name, detail=l["物品名稱"])


async def recent_human_activity(thread, since: datetime) -> bool:
    """貼文裡從 since 之後，有沒有人（不是機器人）回覆過。機器人自己的訊息（例如到期提醒）不算。"""
    async for m in thread.history(limit=50):
        if m.created_at < since:
            break
        if not getattr(m.author, "bot", False):
            return True
    return False


# ---------------- 指令 ----------------

class Market(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.forum_id = None

    async def cog_load(self):
        self.bot.add_view(MarketPanelView(self.bot))
        self.bot.add_dynamic_items(InterestButton, ManageButton, ConfirmDealButton, RejectDealButton,
                                   RenewButton, ExpireNowButton)
        self.expiry_loop.start()
        try:
            self.forum_id = await asyncio.to_thread(self.store.load_market_forum)
            if self.forum_id is not None:
                await asyncio.to_thread(self.store._deal_sheet)    # 啟動時先把成交記錄分頁建好（含搬以前的成交）
        except Exception as e:
            print(f"⚠️ 讀取交易區論壇設定失敗：{e}", flush=True)

    async def cog_unload(self):
        self.expiry_loop.cancel()

    @tasks.loop(hours=1)
    async def expiry_loop(self):
        try:
            await self.check_expiry()
        except Exception as e:
            audit.error("交易區：檢查到期商品失敗", e)

    @expiry_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def check_expiry(self, now: Optional[datetime] = None) -> dict:
        """
        一般商品：到期前 5 天提醒賣家（附續期／讓它下架按鈕），到期了就自動下架並通知賣家。
                  到期時如果貼文裡最近 7 天還有人回覆（不是機器人），代表還在談，先不下架，下次檢查再看。
        長期供貨／長期收購（包含暫停中的）：每 10 天提醒一次「還在供貨（收購）嗎？」，3 天沒回應就下架。
        """
        now = now or now_tw()
        _, _, rows = await asyncio.to_thread(self.store._market_rows)
        done = {"提醒": [], "下架": [], "還在談先保留": []}
        for l in rows:
            if l.get("狀態") not in OPEN:
                continue
            exp = expires_at(l)
            if exp is None:
                continue
            pid = int(l["貼文ID"])
            try:
                if l.get("交易方式") in LONG:
                    await self._check_long(l, pid, exp, now, done)
                    continue
                if l.get("狀態") not in ACTIVE:
                    continue
                if now >= exp:
                    thread = await get_thread(self.bot, pid)
                    if await recent_human_activity(thread, now - timedelta(days=ACTIVE_GRACE_DAYS)):
                        done["還在談先保留"].append(l["物品名稱"])
                        continue
                    took = await take_down(self.bot, pid, reason=(
                        f"⏰ <@{l['賣家ID']}> 你的「{l['物品名稱']}」已經掛賣超過 {LISTING_DAYS} 天，系統自動下架了。\n"
                        f"想繼續賣的話，可以到交易櫃檯重新掛賣一次。"))
                    if took:
                        done["下架"].append(l["物品名稱"])
                        audit.audit("交易區：到期自動下架", who="系統", detail=l["物品名稱"])
                elif now >= exp - timedelta(days=REMIND_DAYS_BEFORE) and not l.get("已提醒"):
                    thread = await get_thread(self.bot, pid)
                    view = discord.ui.View(timeout=None)
                    view.add_item(RenewButton(pid))
                    view.add_item(ExpireNowButton(pid))
                    left = max(1, (exp - now).days + (1 if (exp - now).seconds else 0))
                    if getattr(thread, "archived", False):
                        await thread.edit(archived=False)
                    await thread.send(
                        f"⏰ <@{l['賣家ID']}> 你的「{l['物品名稱']}」再 {left} 天（{fmt_time(exp)[:16]}）就會自動下架。\n"
                        f"如果還想繼續賣，按「續期」從今天重新算 {LISTING_DAYS} 天。",
                        view=view, allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(l["賣家ID"]))]))
                    async with self.store.lock:
                        await asyncio.to_thread(self.store.market_update, pid, {"已提醒": fmt_time(now)})
                    done["提醒"].append(l["物品名稱"])
            except Exception as e:
                audit.error(f"交易區：處理到期商品「{l.get('物品名稱')}」失敗", e)
        return done

    async def _check_long(self, l: dict, pid: int, next_check: datetime, now: datetime, done: dict):
        """長期的商品：到了確認時間就提醒；提醒後 3 天沒按「繼續」就下架。"""
        reminded = parse_time(l.get("已提醒"))
        supply = l.get("交易方式") == "長期供貨"
        if reminded is not None:
            if now >= reminded + timedelta(days=LONG_GRACE_DAYS):
                took = await take_down(self.bot, pid, reason=(
                    f"⏰ <@{l['賣家ID']}> 「{l['物品名稱']}」{LONG_GRACE_DAYS} 天沒有回應確認，系統自動下架了。\n"
                    f"還要繼續{'供貨' if supply else '收購'}的話，請重新發一篇。"))
                if took:
                    done["下架"].append(l["物品名稱"])
                    audit.audit("交易區：長期商品沒回應，自動下架", who="系統", detail=l["物品名稱"])
            return
        if now < next_check:
            return
        thread = await get_thread(self.bot, pid)
        view = discord.ui.View(timeout=None)
        view.add_item(RenewButton(pid, "繼續供貨" if supply else "繼續收購"))
        view.add_item(ExpireNowButton(pid))
        if getattr(thread, "archived", False):
            await thread.edit(archived=False)
        await thread.send(
            f"⏰ <@{l['賣家ID']}> 「{l['物品名稱']}」還在{'供貨' if supply else '收購'}嗎？\n"
            f"按「繼續{'供貨' if supply else '收購'}」就會再掛 {LONG_CHECK_DAYS} 天；"
            f"{LONG_GRACE_DAYS} 天內沒有回應會自動下架。",
            view=view, allowed_mentions=discord.AllowedMentions(users=[discord.Object(int(l["賣家ID"]))]))
        async with self.store.lock:
            await asyncio.to_thread(self.store.market_update, pid, {"已提醒": fmt_time(now)})
        done["提醒"].append(l["物品名稱"])

    async def get_forum(self):
        if self.forum_id is None:
            return None
        ch = self.bot.get_channel(self.forum_id)
        return ch if ch is not None else await self.bot.fetch_channel(self.forum_id)

    @commands.hybrid_command(name="setmarket", description="管理員：設定交易區論壇（會檢查並補上需要的標籤）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(forum="用來放掛賣貼文的論壇頻道")
    async def set_market(self, ctx, forum: discord.ForumChannel):
        await ctx.defer(ephemeral=True)
        perms = forum.permissions_for(ctx.guild.me)
        need = {"send_messages": "建立貼文", "send_messages_in_threads": "在討論串中傳送訊息",
                "manage_threads": "管理討論串", "attach_files": "附加檔案", "embed_links": "嵌入連結"}
        missing_perms = [label for p, label in need.items() if not getattr(perms, p, False)]
        categories = await asyncio.to_thread(self.store.get_market_categories)
        groups = list(dict.fromkeys(g for _, g in categories))
        wanted = list(STATUS_TAGS) + list(TRADE_TAGS) + groups
        have = {t.name for t in forum.available_tags}
        missing_tags = tags_missing(forum, wanted)
        created, problems = [], []
        if missing_tags:
            if len(have) + len(missing_tags) > 20:
                problems.append(f"論壇最多 20 個標籤，現在有 {len(have)} 個、還要再加 {len(missing_tags)} 個，"
                                f"請先刪掉用不到的標籤，或在試算表「交易分類」減少大類。")
            elif perms.manage_channels:
                for name in missing_tags:
                    await forum.create_tag(name=name)
                    created.append(name)
            else:
                problems.append(f"缺少這些標籤：{'、'.join(missing_tags)}\n"
                                f"　（可以手動在論壇設定裡新增，或暫時給機器人「管理頻道」權限，讓它自動建立）")
        if missing_perms:
            problems.append(f"機器人在 {forum.mention} 缺少權限：{'、'.join(missing_perms)}")
        if problems:
            await ctx.send(f"⚠️ {forum.mention} 還不能用來掛賣：\n・" + "\n・".join(problems), ephemeral=True)
            return
        self.forum_id = forum.id
        await asyncio.to_thread(self.store.save_market_forum, forum.id)
        audit.audit("設定交易區論壇", who=ctx.author.display_name, detail=f"#{forum.name}")
        msg = f"✅ 交易區論壇設定為 {forum.mention}。"
        if created:
            msg += f"\n已經自動建立標籤：{'、'.join(created)}"
        msg += "\n接下來用 `/postmarket` 在文字頻道發一則「我要賣東西」的公告。"
        await ctx.send(msg, ephemeral=True)

    @commands.hybrid_command(name="fixdeal", description="管理員：修正已成交的交易（在那篇貼文裡打）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    async def fix_deal(self, ctx):
        """在已成交的商品貼文裡打：修改成交內容，或取消成交並重新上架。都會在貼文裡公開留下說明。"""
        await ctx.defer(ephemeral=True)
        l = await asyncio.to_thread(self.store.market_get, ctx.channel.id)
        if l is None:
            await ctx.send("⚠️ 請在交易區**那篇已成交的商品貼文裡**打這個指令。", ephemeral=True)
            return
        if l.get("交易方式") in LONG:
            await ctx.send("長期供貨／長期收購的成交有很多筆，請直接到試算表「成交記錄」分頁修改那一筆；"
                           "數量用貼文裡的「⚙️ 管理」→「補貨／調整數量」調整。", ephemeral=True)
            return
        if l.get("狀態") != "已售出":
            await ctx.send(f"這件商品目前是「{l.get('狀態')}」，不是已成交的。還在賣的商品請用貼文裡的「⚙️ 賣家管理」。",
                           ephemeral=True)
            return
        await ctx.send(f"**🛠️ 修正成交：{l['物品名稱']}**\n目前：{deal_summary(l)}",
                       view=FixDealView(self.bot, l), ephemeral=True, allowed_mentions=NONE_MENTIONS)

    @commands.hybrid_command(name="postmarket", description="管理員：發一則交易區公告（我要賣東西、我的商品）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="發在哪個頻道（不填就是目前這個頻道）")
    async def post_market(self, ctx, channel: Optional[discord.TextChannel] = None):
        await ctx.defer(ephemeral=True)
        if self.forum_id is None:
            await ctx.send("⚠️ 請先用 `/setmarket` 設定交易區論壇。", ephemeral=True)
            return
        target = channel or ctx.channel
        embed = discord.Embed(
            title="🛒 交易區",
            description=(f"想賣東西或以物易物，按「📸 我要賣東西」，商品會刊登在 <#{self.forum_id}>。\n\n"
                         "📸 **我要賣東西**：選分類、交易方式，填價格或想換什麼，可以附上圖片；也可以長期供貨\n"
                         "🛒 **我想買**：找不到人在賣的話，發一篇收購，有貨的人會來找你；也可以長期收購\n"
                         "📦 **我的商品**：看自己掛賣中的商品\n"
                         "📈 **查行情**：查某件物品以前賣多少、現在有誰在賣\n\n"
                         "想買的話，到論壇裡找到商品，按「🙋 我有興趣」，就會通知賣家。"),
            color=discord.Color.green())
        try:
            msg = await target.send(embed=embed, view=MarketPanelView(self.bot))
        except discord.Forbidden:
            await ctx.send(f"⚠️ 機器人在 {target.mention} 沒有「傳送訊息」或「嵌入連結」權限。", ephemeral=True)
            return
        audit.audit("發佈交易區公告", who=ctx.author.display_name, detail=f"#{target.name}")
        await ctx.send(f"✅ 已在 {target.mention} 發佈交易區公告：{msg.jump_url}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Market(bot))
