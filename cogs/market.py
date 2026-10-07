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
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import audit
from helpers import now_str, TW_TZ

STATUS_TAGS = ("出售中", "議價中", "已售出")
TRADE_TAGS = ("出售", "交換", "贈送")
TRADE_TYPES = ("出售", "交換", "都可以", "贈送")
ACTIVE = ("出售中", "議價中")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
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
    prefix = {"已售出": "【已送出】" if trade == "贈送" else "【已售出】", "已下架": "【已下架】"}.get(l.get("狀態"), "")
    tail = {"出售": price, "交換": "可交換", "都可以": f"{price}｜可交換" if price else "可交換",
            "贈送": "免費贈送"}.get(trade, price)
    return f"{prefix}【{l.get('分類')}】{l.get('物品名稱')}｜{tail}"[:100]


TRADE_EMOJI = {"出售": "💰", "交換": "🔄", "都可以": "💰🔄", "贈送": "🎁"}
STATUS_TEXT = {"出售中": "🟢 出售中", "議價中": "🤝 議價中", "已售出": "✅ 已售出", "已下架": "🗑️ 已下架"}
METHOD_TEXT = {"金錢": "💰 金錢", "交換": "🔄 交換", "兩者都有": "🔄💰 交換＋補差價", "贈送": "🎁 贈送"}
# 贈品的狀態用比較貼切的說法（論壇標籤名稱還是同一組）
GIFT_STATUS_TEXT = {"出售中": "🟢 等人索取", "議價中": "🙋 有人想要", "已售出": "✅ 已送出", "已下架": "🗑️ 已下架"}


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
        lines.append(f"🏷️ **開價**：{fmt_price(l['開價'])}")
    if l.get("想換"):
        lines.append(f"🎯 **想換**：{l['想換']}")
    if l.get("備註"):
        lines.append(f"📝 **備註**：{l['備註']}")
    lines.append(f"👤 **賣家**：<@{l.get('賣家ID')}>")
    lines.append(f"🕒 **掛賣時間**：{when_text(l.get('掛賣時間'), to_ts(l.get('掛賣時間')))}")
    lines.append(f"📌 **狀態**：{(GIFT_STATUS_TEXT if trade == '贈送' else STATUS_TEXT).get(status, status)}")
    if status == "已售出":
        lines.append("")
        lines.append(f"🙋 **{'收到的人' if trade == '贈送' else '買家'}**：<@{l.get('買家ID')}>")
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


TRADE_CODE = {"出售": "s", "交換": "x", "都可以": "b", "贈送": "g"}   # 寫進「我有興趣」按鈕 ID 的交易方式代碼


def card_view(post_id: int, trade: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(InterestButton(post_id, TRADE_CODE.get(trade, "s")))
    view.add_item(ManageButton(post_id))
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


def tags_for(forum, l: dict) -> list:
    names = [l.get("大類")]
    trade = l.get("交易方式")
    if trade in ("出售", "都可以"):
        names.append("出售")
    if trade in ("交換", "都可以"):
        names.append("交換")
    if trade == "贈送":
        names.append("贈送")
    status = l.get("狀態")
    if status in STATUS_TAGS:
        names.append(status)
    by_name = {t.name: t for t in forum.available_tags}
    return [by_name[n] for n in names if n in by_name][:5]


async def refresh_post(bot, l: dict):
    """照試算表裡的最新狀態，更新貼文標題、標籤、卡片；結束的商品會鎖定並收起來。"""
    thread = await get_thread(bot, int(l["貼文ID"]))
    ended = l.get("狀態") not in ACTIVE
    if getattr(thread, "archived", False):
        await thread.edit(archived=False)
    starter = await thread.fetch_message(int(l["貼文ID"]))
    await starter.edit(embed=listing_embed(l), view=None if ended else card_view(int(l["貼文ID"]), l.get("交易方式")))
    forum = thread.parent or await bot.get_cog("Market").get_forum()
    await thread.edit(name=listing_title(l), applied_tags=tags_for(forum, l),
                      **({"archived": True, "locked": True} if ended else {}))


# ---------------- 掛賣：選大類、細分類、交易方式 → 填內容 ----------------

class ListingModal(discord.ui.Modal):
    def __init__(self, bot, group: str, category: str, trade: str):
        super().__init__(title=f"掛賣：{category}"[:45])
        self.bot, self.group, self.category, self.trade = bot, group, category, trade
        self.name_input = discord.ui.TextInput(placeholder="例如：死靈之弓 +5", max_length=40)
        self.add_item(discord.ui.Label(text="物品名稱", component=self.name_input))
        self.price_input = self.wants_input = None
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
            "掛賣時間": now_str(), "物品名稱": self.name_input.value.strip(), "分類": self.category,
            "大類": self.group, "交易方式": self.trade, "開價": price or "", "想換": wants,
            "備註": self.note_input.value.strip(), "賣家": interaction.user.display_name,
            "賣家ID": str(interaction.user.id), "狀態": "出售中", "有興趣的人": "", "待確認": "",
        }
        file = None
        if att is not None:
            file, listing["圖片檔名"] = await image_file(att)
        created = await forum.create_thread(
            name=listing_title(listing), embed=listing_embed(listing), applied_tags=tags_for(forum, listing),
            auto_archive_duration=10080, allowed_mentions=NONE_MENTIONS, **({"file": file} if file else {}))
        thread, starter = created.thread, created.message
        await starter.edit(view=card_view(thread.id, self.trade))
        listing.update({"貼文ID": str(thread.id), "貼文連結": thread.jump_url})
        async with self.bot.store.lock:
            await asyncio.to_thread(self.bot.store.market_add, listing)
        audit.audit("交易區：掛賣", who=interaction.user.display_name,
                    detail=f"{listing['物品名稱']}｜{self.category}｜{self.trade}｜{fmt_price(price) or wants}")
        await interaction.followup.send(f"✅ 已經上架：{thread.jump_url}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：掛賣發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 掛賣失敗，請稍後再試或通知管理員：{error}")


class SellWizardView(discord.ui.View):
    """只有自己看得到的掛賣精靈：大類 → 細分類 → 交易方式 → 下一步。"""

    def __init__(self, bot, user_id: int, categories: list):
        super().__init__(timeout=600)
        self.bot, self.user_id, self.categories = bot, user_id, categories
        self.group = self.category = self.trade = None
        groups = list(dict.fromkeys(g for _, g in categories))[:25]
        self.group_select = discord.ui.Select(placeholder="① 選擇大類", row=0,
                                              options=[discord.SelectOption(label=g, value=g) for g in groups])
        self.group_select.callback = self.on_group
        self.category_select = discord.ui.Select(placeholder="② 先選大類", row=1, disabled=True,
                                                 options=[discord.SelectOption(label="（先選大類）", value="-")])
        self.category_select.callback = self.on_category
        self.trade_select = discord.ui.Select(placeholder="③ 選擇交易方式", row=2, options=[
            discord.SelectOption(label="出售", value="出售", emoji="💰", description="只收錢"),
            discord.SelectOption(label="交換", value="交換", emoji="🔄", description="只接受以物易物"),
            discord.SelectOption(label="都可以", value="都可以", emoji="🤝", description="收錢或交換都可以"),
            discord.SelectOption(label="贈送", value="贈送", emoji="🎁", description="免費送出，不收任何代價")])
        self.trade_select.callback = self.on_trade
        for item in (self.group_select, self.category_select, self.trade_select):
            self.add_item(item)

    def _status(self) -> str:
        return (f"**📸 掛賣商品**\n大類：{self.group or '—'}　細分類：{self.category or '—'}　交易方式：{self.trade or '—'}\n"
                f"三個都選好之後，按「下一步」填寫名稱、價格跟上傳圖片。")

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
            await interaction.response.send_message("⚠️ 大類、細分類、交易方式三個都要先選好。", ephemeral=True)
            return
        await interaction.response.send_modal(ListingModal(self.bot, self.group, self.category, self.trade))


# ---------------- 交易區公告（文字頻道裡的常駐按鈕） ----------------

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

    @discord.ui.button(label="我的商品", emoji="📦", style=discord.ButtonStyle.secondary, custom_id="mkt:panel:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await asyncio.to_thread(self.bot.store.market_by_seller, str(interaction.user.id))
        active = [r for r in rows if r.get("狀態") in ACTIVE]
        ended = [r for r in rows if r.get("狀態") not in ACTIVE][-5:]
        if not rows:
            await interaction.followup.send("你還沒有掛賣過任何商品。", ephemeral=True)
            return
        lines = ["**📦 你的商品**"]
        lines += [f"・{r['狀態']}　{r['物品名稱']}　{r.get('貼文連結', '')}" for r in active] or ["（目前沒有掛賣中的商品）"]
        if ended:
            lines += ["", "**最近結束的**"] + [f"・{r['狀態']}　{r['物品名稱']}　{r.get('貼文連結', '')}" for r in reversed(ended)]
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        audit.error("交易區公告按鈕發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 執行時發生錯誤：{error}")


# ---------------- 我有興趣 ----------------

async def express_interest(bot, interaction: discord.Interaction, post_id: int, offer: str = "", att=None):
    """把這個人加進「有興趣的人」，在貼文裡 @ 賣家；第一個有興趣的人出現時，狀態從出售中改成議價中。"""
    store = bot.store
    async with store.lock:
        l = await asyncio.to_thread(store.market_get, post_id)
        if l is None or l.get("狀態") not in ACTIVE:
            await reply_error(interaction, "這件商品已經結束了。")
            return
        uid = str(interaction.user.id)
        if l.get("賣家ID") == uid:
            await reply_error(interaction, "這是你自己掛的商品喔。")
            return
        ids = interested_ids(l)
        if uid in ids and not offer:
            await reply_error(interaction, "你已經表示過有興趣了，直接在貼文裡跟賣家聊就好。")
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
    elif l.get("交易方式") == "贈送":
        await thread.send(f"🙋 {interaction.user.mention} 想要「{l['物品名稱']}」！\n{seller} 決定好要送給誰之後，"
                          f"按「⚙️ 賣家管理」→「✅ 已送出」。", allowed_mentions=mentions)
    else:
        await thread.send(f"🙋 {interaction.user.mention} 想買「{l['物品名稱']}」！\n{seller} 可以直接在這裡議價。",
                          allowed_mentions=mentions)
    if "狀態" in fields:
        await refresh_post(bot, l)
    audit.audit("交易區：有興趣", who=interaction.user.display_name, detail=f"{l['物品名稱']}｜{offer or '想買'}")
    await reply_error(interaction, "✅ 已經通知賣家了，接下來直接在貼文裡議價吧。")


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


class InterestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mkt:i:(?P<pid>[0-9]+):(?P<t>[sxbg])"):
    """
    交易方式直接寫在按鈕 ID 裡（s 出售、x 交換、b 都可以、g 贈送）：交換的話按下去要馬上跳出輸入視窗，
    輸入視窗必須是第一個回應、要在 3 秒內，不能先去查試算表。檢查（是不是自己的、結束了沒）在送出時才做。
    """

    def __init__(self, post_id: int, trade_code: str = "s"):
        super().__init__(discord.ui.Button(label="我想要" if trade_code == "g" else "我有興趣", emoji="🙋",
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
                                 price="", item=""):
    """記下待確認的成交內容（附一次性驗證碼），在貼文裡請買家／收到的人確認。呼叫前要先 defer。"""
    nonce = secrets.token_hex(3)
    pending = {"buyer_id": str(buyer.id), "buyer": buyer.display_name, "method": method,
               "price": price, "item": item, "nonce": nonce}
    async with bot.store.lock:
        l = await asyncio.to_thread(bot.store.market_get, post_id)
        if l is None or l.get("狀態") not in ACTIVE:
            await interaction.followup.send("這件商品已經結束了。", ephemeral=True)
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
        if price:
            deal.append(f"{'成交價' if method == '金錢' else '補差價'} **{fmt_price(price)}**")
        if item:
            deal.append(f"換得 **{item}**")
        text = (f"🤝 <@{l['賣家ID']}> 確認把「{l['物品名稱']}」交易給 {buyer.mention}：{'，'.join(deal)}\n"
                f"{buyer.mention} 請確認這筆交易（只有你能按）。")
    await thread.send(text, view=view, allowed_mentions=discord.AllowedMentions(users=[buyer]))
    audit.audit("交易區：送出成交確認", who=interaction.user.display_name,
                detail=f"{l['物品名稱']}｜對象 {buyer.display_name}｜{method}｜{price or ''} {item}")
    await interaction.followup.send(f"✅ 已經請 {buyer.display_name} 在貼文裡確認，他按下確認才算完成。", ephemeral=True)


class DealModal(discord.ui.Modal):
    def __init__(self, bot, post_id: int, buyer: discord.abc.User, method: str):
        super().__init__(title="成交內容")
        self.bot, self.post_id, self.buyer, self.method = bot, post_id, buyer, method
        self.price_input = self.item_input = None
        if method in ("金錢", "兩者都有"):
            self.price_input = discord.ui.TextInput(placeholder="例如 4500000、450萬", max_length=20)
            self.add_item(discord.ui.Label(text="成交價" if method == "金錢" else "補差價的金額", component=self.price_input))
        if method in ("交換", "兩者都有"):
            self.item_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=200,
                                                   placeholder="例如：精靈弓 +5")
            self.add_item(discord.ui.Label(text="換到的物品", component=self.item_input))

    async def on_submit(self, interaction: discord.Interaction):
        price = ""
        if self.price_input is not None:
            price = parse_price(self.price_input.value)
            if price is None:
                await interaction.response.send_message("⚠️ 金額看不懂，請填數字，例如 4500000 或 450萬。", ephemeral=True)
                return
        item = self.item_input.value.strip() if self.item_input is not None else ""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await send_deal_confirmation(self.bot, interaction, self.post_id, self.buyer, self.method, price, item)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("交易區：成交內容發生錯誤", error, who=interaction.user.display_name)
        await reply_error(interaction, f"❌ 發生錯誤：{error}")


class DealSetupView(discord.ui.View):
    """賣家選成交對象（Discord 內建的成員選擇器，可以搜尋）跟成交方式。"""

    def __init__(self, bot, l: dict):
        super().__init__(timeout=600)
        self.bot, self.l, self.buyer = bot, l, None
        trade = l.get("交易方式")
        methods = {"出售": ["金錢"], "交換": ["交換", "兩者都有"], "都可以": ["金錢", "交換", "兩者都有"],
                   "贈送": ["贈送"]}.get(trade, ["金錢"])
        if trade == "贈送":
            self.next_step.label = "送出：請對方確認"
        self.method = methods[0] if len(methods) == 1 else None
        self.user_select = discord.ui.UserSelect(placeholder="選擇要送給誰" if trade == "贈送" else "選擇成交對象", row=0)
        self.user_select.callback = self.on_user
        self.add_item(self.user_select)
        if len(methods) > 1:
            labels = {"金錢": "💰 金錢", "交換": "🔄 交換", "兩者都有": "🔄💰 交換＋補差價"}
            self.method_select = discord.ui.Select(placeholder="選擇成交方式", row=1,
                                                   options=[discord.SelectOption(label=labels[m], value=m) for m in methods])
            self.method_select.callback = self.on_method
            self.add_item(self.method_select)

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
        if self.method == "贈送":   # 贈品沒有金額跟換得的物品，直接請對方確認
            await interaction.response.defer(ephemeral=True, thinking=True)
            await send_deal_confirmation(self.bot, interaction, int(self.l["貼文ID"]), self.buyer, "贈送")
            return
        await interaction.response.send_modal(DealModal(self.bot, int(self.l["貼文ID"]), self.buyer, self.method))


class ManageView(discord.ui.View):
    def __init__(self, bot, l: dict):
        super().__init__(timeout=600)
        self.bot, self.l = bot, l
        if l.get("交易方式") == "贈送":
            self.deal.label, self.deal.emoji = "已送出", "🎁"

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

    @discord.ui.button(label="下架", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=120)
        confirm = discord.ui.Button(label="確定下架", style=discord.ButtonStyle.danger)

        async def do_remove(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=True)
            async with self.bot.store.lock:
                l = await asyncio.to_thread(self.bot.store.market_get, int(self.l["貼文ID"]))
                if l is None or l.get("狀態") not in ACTIVE:
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
    def __init__(self, post_id: int):
        super().__init__(discord.ui.Button(label="賣家管理", emoji="⚙️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mkt:m:{post_id}"))
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]))

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
            await reply_error(interaction, "只有賣家可以管理這件商品。")
            return
        if l.get("狀態") not in ACTIVE:
            await reply_error(interaction, "這件商品已經結束了。")
            return
        await interaction.followup.send(f"**⚙️ 管理你的商品：{l['物品名稱']}**", view=ManageView(bot, l), ephemeral=True)


# ---------------- 買家確認成交 ----------------

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
            await reply_error(interaction, "只有被指定的買家可以按這個按鈕。")
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
            l = await asyncio.to_thread(bot.store.market_update, self.post_id, {
                "狀態": "已售出", "成交時間": now_str(), "買家": pending["buyer"], "買家ID": pending["buyer_id"],
                "成交方式": pending["method"], "成交價": pending["price"], "換得物品": pending["item"], "待確認": "",
            })
        await interaction.message.edit(content=f"{interaction.message.content}\n\n✅ **{interaction.user.display_name} 已確認成交**",
                                       view=None, allowed_mentions=NONE_MENTIONS)
        await refresh_post(bot, l)
        audit.audit("交易區：成交", who=interaction.user.display_name,
                    detail=f"{l['物品名稱']}｜賣家 {l['賣家']}｜買家 {l['買家']}｜{l['成交方式']}｜{l['成交價']} {l['換得物品']}")


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


# ---------------- 指令 ----------------

class Market(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.forum_id = None

    async def cog_load(self):
        self.bot.add_view(MarketPanelView(self.bot))
        self.bot.add_dynamic_items(InterestButton, ManageButton, ConfirmDealButton, RejectDealButton)
        try:
            self.forum_id = await asyncio.to_thread(self.store.load_market_forum)
        except Exception as e:
            print(f"⚠️ 讀取交易區論壇設定失敗：{e}", flush=True)

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
        missing_tags = [n for n in wanted if n not in have]
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
                         "📸 **我要賣東西**：選分類、交易方式，填價格或想換什麼，可以附上圖片\n"
                         "📦 **我的商品**：看自己掛賣中的商品\n\n"
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
