"""
爬塔揪團：臨時揪團（現在就打）、預約揪團（指定時間）。

頻道：
  - 揪團看板（/setparty board）：一場一則卡片，狀態變動就改那則卡片，結束後留著當紀錄
  - 揪團通知（/setparty notify）：臨時揪團 @everyone、叫候補、提醒、關閉通知，都發在這裡，附卡片連結

流程：
  - 發起：選種類、伺服器、帶哪隻角色 → 填集合地點（預約的再填時間）→ 看板出現卡片
  - 加入：沒登記角色就給登記按鈕；多隻角色選一隻；跟已加入的別場時間（前後 30 分鐘內）衝突就擋；
          滿 12 人排候補（依順序）
  - 隊伍有空位：tag 候補第 1 位，5 分鐘內按「我要遞補」才進來；沒反應或不要就移出候補、tag 下一位
    （開打前自動叫；開打後要發起人按「補人」）
  - 發起人：開打、移除隊友（被移除的不能再加入這場）、修改伺服器／地點／時間、取消、延長、補人、結束
  - 沒開打自動關閉：臨時從發起算 10 分鐘，預約從預約時間算 10 分鐘；剩 2 分鐘提醒發起人，可以延長 10 分鐘
  - 預約：開打前 15 分鐘 @ 隊伍，到時間 @ 發起人
  - 開打後：隊員可以中途退出（不自動叫候補），開打 3 小時後自動結束

進行中的揪團存在試算表「揪團」分頁（重新部署後接得回來），結束就刪掉。
所有按鈕都是動態按鈕（ID 裡帶揪團編號），重新部署後舊卡片的按鈕照樣能用；出錯時一定會回一句話。
"""
import asyncio
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import audit
from helpers import TW_TZ

MAX_MEMBERS = 12
OFFER_MINUTES = 5            # 候補被 tag 之後幾分鐘內要回應
CLOSE_MINUTES = 10           # 沒開打幾分鐘後自動關閉
WARN_BEFORE_MINUTES = 2      # 自動關閉前幾分鐘提醒發起人
REMIND_BEFORE_MINUTES = 15   # 預約：開打前幾分鐘 @ 隊伍
AUTO_END_HOURS = 3           # 開打後幾小時自動結束
CONFLICT_MINUTES = 30        # 開打時間前後幾分鐘內算「同一個時間」，不能同時加入
ACTIVE = ("招募中", "已開打")
TIME_FMT = "%Y/%m/%d %H:%M:%S"
NONE_MENTIONS = discord.AllowedMentions.none()


def now_tw() -> datetime:
    """揪團所有「現在幾點」都從這裡拿（台灣時間），測試時可以快轉。"""
    return datetime.now(TW_TZ)


def fmt(dt: datetime) -> str:
    return dt.astimezone(TW_TZ).strftime(TIME_FMT)


def parse(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s or "", TIME_FMT).replace(tzinfo=TW_TZ)
    except ValueError:
        return None


def parse_when(text: str, now: datetime) -> Optional[datetime]:
    """
    預約時間：「21:00」（今天，已經過了就當明天）、「10/12 21:00」、「2026/10/12 21:00」都可以，全形冒號也行。
    看不懂、已經過了、或超過 30 天後，都回傳 None。
    """
    t = (text or "").strip().replace("：", ":").replace("／", "/")
    m = re.fullmatch(r"(?:(?:(\d{4})/)?(\d{1,2})/(\d{1,2})\s+)?(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    year, month, day, hh, mm = m.groups()
    try:
        if month:
            dt = datetime(int(year or now.year), int(month), int(day), int(hh), int(mm), tzinfo=TW_TZ)
            if not year and dt < now - timedelta(days=1):
                dt = dt.replace(year=dt.year + 1)      # 寫 1/5 而現在是 12 月，就是明年
        else:
            dt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
    except ValueError:
        return None
    if dt <= now + timedelta(minutes=1) or dt > now + timedelta(days=30):
        return None
    return dt


def ts(dt: datetime) -> int:
    return int(dt.timestamp())


def when_text(dt: datetime) -> str:
    return f"{fmt(dt)[:16]}（<t:{ts(dt)}:R>）"


def active_members(p: dict) -> list:
    return [m for m in p["members"] if not m.get("quit")]


def free_seats(p: dict) -> int:
    return MAX_MEMBERS - len(active_members(p)) - (1 if p.get("offer") else 0)


def person_line(i: int, m: dict) -> str:
    job = "｜".join(x for x in (m.get("job"), m.get("pos")) if x)
    quit_mark = "🚪 " if m.get("quit") else ""
    tail = "（中途退出）" if m.get("quit") else ""
    return f"{i}. {quit_mark}{m['char']}　{job}　<@{m['uid']}>{tail}"


def involved(p: dict, uid: str) -> bool:
    """這個人在這一團有佔位置嗎（隊員、候補、正在被叫遞補）。中途退出的不算。"""
    return (any(m["uid"] == uid and not m.get("quit") for m in p["members"])
            or any(w["uid"] == uid for w in p["waitlist"])
            or (p.get("offer") or {}).get("uid") == uid)


def everyone_in(p: dict) -> list:
    ids = [m["uid"] for m in active_members(p)] + [w["uid"] for w in p["waitlist"]]
    if p.get("offer"):
        ids.append(p["offer"]["uid"])
    return list(dict.fromkeys(ids))


def mention_list(ids) -> str:
    return " ".join(f"<@{i}>" for i in ids)


def users(ids) -> discord.AllowedMentions:
    return discord.AllowedMentions(users=[discord.Object(int(i)) for i in ids], everyone=False, roles=False)


def card_embed(p: dict) -> discord.Embed:
    kind = "🔥 臨時" if p["kind"] == "臨時" else "📅 預約"
    status = p["status"]
    badge = {"已開打": "　⚔️ 已開打", "已結束": "　🏁 已結束", "已關閉": "　⏹️ 已關閉（沒有開打）",
             "已取消": "　❌ 已取消"}.get(status, "")
    start = parse(p["start"])
    lines = [f"👤 **發起人**：<@{p['leader_id']}>", f"📍 **集合**：{p['server']}　{p['place']}"]
    if status == "招募中":
        if p["kind"] == "臨時":
            lines.append(f"🕒 **開打**：現在（<t:{ts(parse(p['deadline']))}:R>沒開打會自動關閉）")
        else:
            lines.append(f"🕒 **開打**：{when_text(start)}")
    elif status == "已開打":
        lines.append(f"⚔️ **開打時間**：{fmt(parse(p['started_at']))[:16]}")
    else:
        lines.append(f"🕒 **時間**：{fmt(start)[:16]}")
    if p.get("note"):
        lines.append(f"📝 **備註**：{p['note']}")
    lines.append("")
    lines.append(f"**隊伍（{len(active_members(p))}/{MAX_MEMBERS}）**")
    lines += [person_line(i, m) for i, m in enumerate(p["members"], start=1)] or ["（還沒有人）"]
    if p.get("offer"):
        o = p["offer"]
        lines.append(f"⏳ 保留中：{o['char']}　<@{o['uid']}>（等他回應遞補）")
    if p["waitlist"] and status in ACTIVE:
        lines.append("")
        lines.append(f"**候補（{len(p['waitlist'])}）**")
        shown = p["waitlist"][:15]
        lines += [f"{i}. {w['char']}　{'｜'.join(x for x in (w.get('job'), w.get('pos')) if x)}　<@{w['uid']}>"
                  for i, w in enumerate(shown, start=1)]
        if len(p["waitlist"]) > 15:
            lines.append(f"…還有 {len(p['waitlist']) - 15} 位")
    color = {"招募中": discord.Color.green(), "已開打": discord.Color.orange()}.get(status, discord.Color.dark_grey())
    return discord.Embed(title=f"🗼 {p.get('activity', '爬塔')}招募｜{kind}{badge}",
                         description="\n".join(lines)[:4000], color=color)


def card_view(p: dict) -> Optional[discord.ui.View]:
    if p["status"] not in ACTIVE:
        return None
    view = discord.ui.View(timeout=None)
    if p["status"] == "招募中":
        view.add_item(JoinButton(p["id"]))
        view.add_item(LeaveButton(p["id"]))
    else:
        view.add_item(QuitButton(p["id"]))
    view.add_item(LeaderButton(p["id"]))
    return view


async def reply(interaction: discord.Interaction, text: str, view=None):
    kw = {"ephemeral": True, "allowed_mentions": NONE_MENTIONS}
    if view is not None:
        kw["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, **kw)
        else:
            await interaction.response.send_message(text, **kw)
    except Exception:
        pass


async def guarded(interaction: discord.Interaction, coro, where: str):
    """動態按鈕出錯時 discord.py 只會默默寫進記錄、使用者看到「此互動失敗」，所以自己接住。"""
    try:
        await coro
    except Exception as e:
        audit.error(f"揪團：{where}發生錯誤", e, who=interaction.user.display_name)
        await reply(interaction, f"❌ 發生錯誤，請稍後再試或通知管理員：{e}")


def cog_of(interaction) -> "Party":
    return interaction.client.get_cog("Party")


# ---------------- 卡片上的按鈕 ----------------

class _PartyButton:
    """共用：找出這一團。動態按鈕不能有共用的父類別（discord.py 規定每個都要有自己的 template），所以寫成混入。"""

    async def _party(self, interaction: discord.Interaction) -> Optional[dict]:
        p = cog_of(interaction).parties.get(self.pid)
        if p is None or p["status"] not in ACTIVE:
            await reply(interaction, "這個揪團已經結束了。")
            return None
        return p


class JoinButton(_PartyButton, discord.ui.DynamicItem[discord.ui.Button], template=r"pty:j:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(discord.ui.Button(label="加入", emoji="✋", style=discord.ButtonStyle.success, custom_id=f"pty:j:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).start_join(interaction, self.pid), "加入")


class LeaveButton(_PartyButton, discord.ui.DynamicItem[discord.ui.Button], template=r"pty:l:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(discord.ui.Button(label="退出", emoji="🚪", style=discord.ButtonStyle.secondary, custom_id=f"pty:l:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).leave(interaction, self.pid), "退出")


class QuitButton(_PartyButton, discord.ui.DynamicItem[discord.ui.Button], template=r"pty:q:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(discord.ui.Button(label="中途退出", emoji="🚪", style=discord.ButtonStyle.secondary, custom_id=f"pty:q:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).quit_midway(interaction, self.pid), "中途退出")


class LeaderButton(_PartyButton, discord.ui.DynamicItem[discord.ui.Button], template=r"pty:m:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(discord.ui.Button(label="發起人管理", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id=f"pty:m:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await guarded(interaction, self._run(interaction), "發起人管理")

    async def _run(self, interaction: discord.Interaction):
        p = await self._party(interaction)
        if p is None:
            return
        if not can_lead(interaction, p):
            await reply(interaction, "只有發起人可以管理這個揪團。")
            return
        await interaction.response.send_message(f"**⚙️ 管理你的揪團**（{p['server']}　{p['place']}）",
                                                view=LeaderView(interaction.client, p["id"], p["status"]), ephemeral=True)


def can_lead(interaction: discord.Interaction, p: dict) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return str(interaction.user.id) == p["leader_id"] or bool(perms and perms.manage_guild)


class OfferAcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pty:oa:(?P<pid>[0-9a-f]{8}):(?P<n>[0-9a-f]{6})"):
    def __init__(self, pid: str, nonce: str):
        super().__init__(discord.ui.Button(label="我要遞補", emoji="✅", style=discord.ButtonStyle.success,
                                           custom_id=f"pty:oa:{pid}:{nonce}"))
        self.pid, self.nonce = pid, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"], match["n"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).answer_offer(interaction, self.pid, self.nonce, True), "遞補")


class OfferDeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pty:od:(?P<pid>[0-9a-f]{8}):(?P<n>[0-9a-f]{6})"):
    def __init__(self, pid: str, nonce: str):
        super().__init__(discord.ui.Button(label="不用了", emoji="❌", style=discord.ButtonStyle.secondary,
                                           custom_id=f"pty:od:{pid}:{nonce}"))
        self.pid, self.nonce = pid, nonce

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"], match["n"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).answer_offer(interaction, self.pid, self.nonce, False), "放棄遞補")


class ExtendButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pty:x:(?P<pid>[0-9a-f]{8})"):
    def __init__(self, pid: str):
        super().__init__(discord.ui.Button(label=f"延長 {CLOSE_MINUTES} 分鐘", emoji="⏰", style=discord.ButtonStyle.primary,
                                           custom_id=f"pty:x:{pid}"))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["pid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, cog_of(interaction).extend(interaction, self.pid), "延長")


# ---------------- 只有自己看得到的畫面 ----------------

class RegisterView(discord.ui.View):
    """沒登記角色的人按加入時看到的：直接在這裡登記，不用離開。"""

    def __init__(self, store):
        super().__init__(timeout=600)
        self.store = store

    @discord.ui.button(label="登記角色", emoji="📝", style=discord.ButtonStyle.primary)
    async def register(self, interaction: discord.Interaction, button: discord.ui.Button):
        from cogs.profiles import NameModal
        await interaction.response.send_modal(NameModal(self.store))


class CharPickView(discord.ui.View):
    """有好幾隻角色：選這次帶哪一隻。"""

    def __init__(self, bot, pid: str, chars: list):
        super().__init__(timeout=300)
        self.bot, self.pid, self.chars = bot, pid, {c["角色名稱"][:100]: c for c in chars[:25]}
        sel = discord.ui.Select(placeholder="這次帶哪一隻角色？", options=[
            discord.SelectOption(label=f"{c['角色名稱']}（{c.get('職業') or '未設定職業'}"
                                       f"{'｜' + c['位置'] if c.get('位置') else ''}）"[:100], value=c["角色名稱"][:100])
            for c in chars[:25]])
        sel.callback = self.on_pick
        self.sel = sel
        self.add_item(sel)

    async def on_pick(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        c = self.chars[self.sel.values[0]]
        await guarded(interaction, self.bot.get_cog("Party").do_join(interaction, self.pid, c), "加入")


class CreateModal(discord.ui.Modal):
    def __init__(self, bot, kind: str, server: str, char: dict):
        super().__init__(title=f"發起{kind}揪團")
        self.bot, self.kind, self.server, self.char = bot, kind, server, char
        self.place = discord.ui.TextInput(placeholder="例如：王座、145 傳點", max_length=40)
        self.add_item(discord.ui.Label(text="集合地點", component=self.place))
        self.when = None
        if kind == "預約":
            self.when = discord.ui.TextInput(placeholder="例如：21:00、10/12 21:00", max_length=20)
            self.add_item(discord.ui.Label(text="開打時間", component=self.when,
                                           description="只寫時間的話是今天，已經過了就當明天"))
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=200, required=False,
                                         placeholder="選填，例如：等級 80 以上、需要坦")
        self.add_item(discord.ui.Label(text="備註", component=self.note))

    async def on_submit(self, interaction: discord.Interaction):
        start = now_tw()
        if self.when is not None:
            start = parse_when(self.when.value, now_tw())
            if start is None:
                await interaction.response.send_message(
                    "⚠️ 開打時間看不懂，或已經過了、超過 30 天。請填像「21:00」或「10/12 21:00」這樣的時間。", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.get_cog("Party").create(interaction, self.kind, self.server, self.place.value.strip(),
                                               self.note.value.strip(), start, self.char)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("揪團：發起發生錯誤", error, who=interaction.user.display_name)
        await reply(interaction, f"❌ 發起失敗，請稍後再試或通知管理員：{error}")


class CreateWizard(discord.ui.View):
    def __init__(self, bot, servers: list, chars: list):
        super().__init__(timeout=600)
        self.bot = bot
        self.kind = self.server = None
        self.chars = {c["角色名稱"][:100]: c for c in chars[:25]}
        self.char = chars[0] if len(chars) == 1 else None
        self.kind_sel = discord.ui.Select(placeholder="① 種類", row=0, options=[
            discord.SelectOption(label="臨時（現在就打）", value="臨時", emoji="🔥"),
            discord.SelectOption(label="預約（指定時間）", value="預約", emoji="📅")])
        self.server_sel = discord.ui.Select(placeholder="② 伺服器", row=1,
                                            options=[discord.SelectOption(label=s[:100], value=s[:100]) for s in servers[:25]])
        self.char_sel = discord.ui.Select(placeholder="③ 帶哪隻角色", row=2, options=[
            discord.SelectOption(label=f"{c['角色名稱']}（{c.get('職業') or '未設定職業'}）"[:100], value=c["角色名稱"][:100],
                                 default=(len(chars) == 1)) for c in chars[:25]])
        for sel, attr in ((self.kind_sel, "kind"), (self.server_sel, "server"), (self.char_sel, "char")):
            sel.callback = self._make_cb(sel, attr)
            self.add_item(sel)

    def _make_cb(self, sel, attr):
        async def cb(interaction: discord.Interaction):
            v = sel.values[0]
            setattr(self, attr, self.chars[v] if attr == "char" else v)
            for o in sel.options:
                o.default = (o.value == v)
            await interaction.response.edit_message(view=self)
        return cb

    @discord.ui.button(label="下一步：填集合地點", emoji="✏️", style=discord.ButtonStyle.primary, row=3)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (self.kind and self.server and self.char):
            await interaction.response.send_message("⚠️ 種類、伺服器、角色三個都要先選好。", ephemeral=True)
            return
        await interaction.response.send_modal(CreateModal(self.bot, self.kind, self.server, self.char))


class EditModal(discord.ui.Modal):
    def __init__(self, bot, p: dict, server: str):
        super().__init__(title="修改揪團資訊")
        self.bot, self.pid, self.server = bot, p["id"], server
        self.place = discord.ui.TextInput(default=p["place"], max_length=40)
        self.add_item(discord.ui.Label(text="集合地點", component=self.place))
        self.when = None
        if p["kind"] == "預約":
            self.when = discord.ui.TextInput(default=fmt(parse(p["start"]))[5:16], max_length=20)
            self.add_item(discord.ui.Label(text="開打時間", component=self.when))
        self.note = discord.ui.TextInput(style=discord.TextStyle.paragraph, default=p.get("note") or None,
                                         max_length=200, required=False)
        self.add_item(discord.ui.Label(text="備註", component=self.note))

    async def on_submit(self, interaction: discord.Interaction):
        start = None
        if self.when is not None:
            start = parse_when(self.when.value, now_tw())
            if start is None:
                await interaction.response.send_message("⚠️ 開打時間看不懂，或已經過了。", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.bot.get_cog("Party").edit_info(interaction, self.pid, self.server, self.place.value.strip(),
                                                  self.note.value.strip(), start)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        audit.error("揪團：修改資訊發生錯誤", error, who=interaction.user.display_name)
        await reply(interaction, f"❌ 修改失敗：{error}")


class EditWizard(discord.ui.View):
    def __init__(self, bot, p: dict, servers: list):
        super().__init__(timeout=600)
        self.bot, self.p, self.server = bot, p, p["server"]
        self.sel = discord.ui.Select(placeholder="伺服器", options=[
            discord.SelectOption(label=s[:100], value=s[:100], default=(s == p["server"])) for s in servers[:25]])
        self.sel.callback = self.on_server
        self.add_item(self.sel)

    async def on_server(self, interaction: discord.Interaction):
        self.server = self.sel.values[0]
        for o in self.sel.options:
            o.default = (o.value == self.server)
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="下一步：地點、時間、備註", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditModal(self.bot, self.p, self.server))


class RemoveView(discord.ui.View):
    def __init__(self, bot, p: dict):
        super().__init__(timeout=300)
        self.bot, self.pid = bot, p["id"]
        opts = []
        for m in p["members"]:
            if m["uid"] == p["leader_id"]:
                continue
            opts.append(discord.SelectOption(label=f"{m['char']}{'（中途退出）' if m.get('quit') else ''}"[:100],
                                             value=f"m:{m['uid']}", description="隊伍"))
        for w in p["waitlist"]:
            opts.append(discord.SelectOption(label=w["char"][:100], value=f"w:{w['uid']}", description="候補"))
        self.sel = discord.ui.Select(placeholder="要移除誰？", options=opts[:25])
        self.sel.callback = self.on_pick
        self.add_item(self.sel)

    async def on_pick(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        where, uid = self.sel.values[0].split(":", 1)
        await guarded(interaction, self.bot.get_cog("Party").remove_person(interaction, self.pid, uid, where), "移除隊友")


class LeaderView(discord.ui.View):
    def __init__(self, bot, pid: str, status: str):
        super().__init__(timeout=600)
        self.bot, self.pid = bot, pid
        keep = ("開打", "移除隊友", "修改資訊", "延長", "取消揪團") if status == "招募中" else ("補人", "移除隊友", "結束")
        for item in list(self.children):
            if not any(k in (item.label or "") for k in keep):
                self.remove_item(item)

    def _p(self):
        return self.bot.get_cog("Party").parties.get(self.pid)

    async def _run(self, interaction, coro, where):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await guarded(interaction, coro, where)

    @discord.ui.button(label="開打", emoji="⚔️", style=discord.ButtonStyle.success)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, self.bot.get_cog("Party").start_party(interaction, self.pid), "開打")

    @discord.ui.button(label="補人", emoji="➕", style=discord.ButtonStyle.success)
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, self.bot.get_cog("Party").request_fill(interaction, self.pid), "補人")

    @discord.ui.button(label="移除隊友", emoji="➖", style=discord.ButtonStyle.secondary)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        p = self._p()
        if p is None or p["status"] not in ACTIVE:
            await reply(interaction, "這個揪團已經結束了。")
            return
        if len(p["members"]) <= 1 and not p["waitlist"]:
            await reply(interaction, "目前沒有可以移除的人。")
            return
        await interaction.response.send_message("選擇要移除的人（被移除的人不能再加入這一場）：",
                                                view=RemoveView(self.bot, p), ephemeral=True)

    @discord.ui.button(label="修改資訊", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = self._p()
        servers = await asyncio.to_thread(self.bot.store.get_party_servers)
        await interaction.followup.send("修改伺服器，再按下一步改地點、時間、備註：",
                                        view=EditWizard(self.bot, p, servers), ephemeral=True)

    @discord.ui.button(label="延長 10 分鐘", emoji="⏰", style=discord.ButtonStyle.secondary)
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, self.bot.get_cog("Party").extend(interaction, self.pid), "延長")

    @discord.ui.button(label="結束", emoji="🏁", style=discord.ButtonStyle.secondary)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, self.bot.get_cog("Party").finish(self.pid, "已結束", by=interaction), "結束")

    @discord.ui.button(label="取消揪團", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run(interaction, self.bot.get_cog("Party").finish(self.pid, "已取消", by=interaction), "取消揪團")


class PartyPanelView(discord.ui.View):
    """揪團公告上的按鈕（永久型）。"""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="發起揪團", emoji="📣", style=discord.ButtonStyle.success, custom_id="pty:panel:new")
    async def new(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog("Party")
        if cog.board_id is None or cog.notify_id is None:
            await interaction.followup.send("⚠️ 管理員還沒設定揪團看板和通知頻道（/setparty）。", ephemeral=True)
            return
        chars = await asyncio.to_thread(self.bot.store.get_user_characters, str(interaction.user.id))
        if not chars:
            await interaction.followup.send("你還沒有登記角色，登記之後才能發起揪團。", view=RegisterView(self.bot.store),
                                            ephemeral=True)
            return
        servers = await asyncio.to_thread(self.bot.store.get_party_servers)
        await interaction.followup.send("**📣 發起揪團**\n選好種類、伺服器、角色，再按下一步。",
                                        view=CreateWizard(self.bot, servers, chars), ephemeral=True)

    @discord.ui.button(label="我的揪團", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="pty:panel:mine")
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("Party")
        uid = str(interaction.user.id)
        mine = [p for p in cog.parties.values() if p["status"] in ACTIVE and (involved(p, uid) or p["leader_id"] == uid)]
        if not mine:
            await interaction.response.send_message("你目前沒有參加任何揪團。", ephemeral=True)
            return
        lines = ["**📋 你的揪團**"]
        for p in sorted(mine, key=lambda x: x["start"]):
            role = ("發起人" if p["leader_id"] == uid else "隊員" if any(m["uid"] == uid for m in active_members(p))
                    else "被叫遞補中" if (p.get("offer") or {}).get("uid") == uid
                    else f"候補第 {[w['uid'] for w in p['waitlist']].index(uid) + 1} 位")
            lines.append(f"・{p['status']}　{fmt(parse(p['start']))[5:16]}　{p['server']} {p['place']}　（{role}）　{cog.card_url(p)}")
        await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        audit.error("揪團公告按鈕發生錯誤", error, who=interaction.user.display_name)
        await reply(interaction, f"❌ 執行時發生錯誤：{error}")


# ---------------- 主體 ----------------

class Party(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.parties = {}          # 進行中的揪團，記憶體為主，每次變動寫回試算表
        self.lock = asyncio.Lock()
        self.board_id = self.notify_id = None

    async def cog_load(self):
        self.bot.add_view(PartyPanelView(self.bot))
        self.bot.add_dynamic_items(JoinButton, LeaveButton, QuitButton, LeaderButton,
                                   OfferAcceptButton, OfferDeclineButton, ExtendButton)
        try:
            self.board_id, self.notify_id = await asyncio.to_thread(self.store.load_party_channels)
            for p in await asyncio.to_thread(self.store.party_load_all):
                if p.get("status") in ACTIVE:
                    self.parties[p["id"]] = p
        except Exception as e:
            print(f"⚠️ 讀取揪團資料失敗：{e}", flush=True)
        self.timer.start()

    async def cog_unload(self):
        self.timer.cancel()

    # ---- 頻道、卡片、通知 ----

    async def _channel(self, cid):
        ch = self.bot.get_channel(int(cid))
        return ch if ch is not None else await self.bot.fetch_channel(int(cid))

    def card_url(self, p: dict) -> str:
        c = p.get("card") or {}
        return f"https://discord.com/channels/{c.get('guild', '@me')}/{c.get('channel')}/{c.get('message')}"

    async def notify(self, text: str, mention_ids=(), everyone=False, view=None):
        ch = await self._channel(self.notify_id)
        allowed = discord.AllowedMentions(everyone=everyone, roles=False,
                                          users=[discord.Object(int(i)) for i in mention_ids])
        kw = {"allowed_mentions": allowed}
        if view is not None:
            kw["view"] = view
        return await ch.send(text, **kw)

    async def refresh_card(self, p: dict):
        c = p["card"]
        msg = await (await self._channel(c["channel"])).fetch_message(int(c["message"]))
        await msg.edit(embed=card_embed(p), view=card_view(p))

    async def save(self, p: dict):
        async with self.store.lock:
            if p["status"] in ACTIVE:
                await asyncio.to_thread(self.store.party_save, p)
            else:
                await asyncio.to_thread(self.store.party_delete, p["id"])

    def conflict_with(self, uid: str, start: datetime, exclude: str = None) -> Optional[dict]:
        """這個人在別場有沒有佔位置，而且開打時間前後 30 分鐘內。"""
        for q in self.parties.values():
            if q["id"] == exclude or q["status"] not in ACTIVE:
                continue
            if not (involved(q, uid) or q["leader_id"] == uid):
                continue
            if abs((parse(q["start"]) - start).total_seconds()) < CONFLICT_MINUTES * 60:
                return q
        return None

    # ---- 發起 ----

    async def create(self, interaction, kind, server, place, note, start, char):
        uid = str(interaction.user.id)
        clash = self.conflict_with(uid, start)
        if clash:
            await reply(interaction, f"⚠️ 你已經在「{fmt(parse(clash['start']))[5:16]} {clash['server']} {clash['place']}」"
                                     f"這一團，時間太接近，不能同時發起：{self.card_url(clash)}")
            return
        pid = secrets.token_hex(4)
        while pid in self.parties:
            pid = secrets.token_hex(4)
        deadline = (start if kind == "臨時" else start) + timedelta(minutes=CLOSE_MINUTES)
        p = {"id": pid, "kind": kind, "activity": "爬塔", "server": server, "place": place, "note": note,
             "leader_id": uid, "start": fmt(start), "deadline": fmt(deadline), "status": "招募中",
             "members": [{"uid": uid, "char": char["角色名稱"], "job": char.get("職業", ""), "pos": char.get("位置", "")}],
             "waitlist": [], "offer": None, "banned": [], "warned": False, "reminded": False, "pinged": False,
             "fill": False, "created": fmt(now_tw())}
        board = await self._channel(self.board_id)
        msg = await board.send(embed=card_embed(p), view=card_view(p), allowed_mentions=NONE_MENTIONS)
        p["card"] = {"guild": getattr(getattr(board, "guild", None), "id", "@me"), "channel": board.id, "message": msg.id}
        async with self.lock:
            self.parties[pid] = p
            await self.save(p)
        head = f"🗼 {'🔥 臨時' if kind == '臨時' else '📅 預約'}揪團爬塔：{server}　{place}，發起人 <@{uid}>"
        if kind == "臨時":
            await self.notify(f"@everyone {head}，現在就打！\n👉 {self.card_url(p)}", everyone=True)
        else:
            await self.notify(f"{head}\n🕒 {when_text(start)}\n👉 {self.card_url(p)}")
        audit.audit(f"揪團：發起{kind}揪團", who=interaction.user.display_name, detail=f"{server} {place}｜{fmt(start)}")
        await reply(interaction, f"✅ 揪團已經發起：{self.card_url(p)}")

    # ---- 加入、退出 ----

    async def start_join(self, interaction, pid):
        p = self.parties.get(pid)
        uid = str(interaction.user.id)
        if p is None or p["status"] != "招募中":
            await reply(interaction, "這個揪團已經不能加入了。")
            return
        if p["leader_id"] == uid or involved(p, uid):
            await reply(interaction, "你已經在這一團了。")
            return
        if uid in p["banned"]:
            await reply(interaction, "你已經被發起人移出這一場，不能再加入。")
            return
        chars = await asyncio.to_thread(self.store.get_user_characters, uid)
        if not chars:
            await reply(interaction, "你還沒有登記角色，登記之後才能加入揪團。登記完再按一次「加入」。",
                        view=RegisterView(self.store))
            return
        clash = self.conflict_with(uid, parse(p["start"]), exclude=pid)
        if clash:
            await reply(interaction, f"⚠️ 你已經在「{fmt(parse(clash['start']))[5:16]} {clash['server']} {clash['place']}」"
                                     f"這一團，時間跟這場太接近，要先退出那場才能加入：{self.card_url(clash)}")
            return
        if len(chars) == 1:
            await self.do_join(interaction, pid, chars[0])
        else:
            await reply(interaction, "你有好幾隻角色，這次要帶哪一隻？", view=CharPickView(self.bot, pid, chars))

    async def do_join(self, interaction, pid, char):
        uid = str(interaction.user.id)
        async with self.lock:
            p = self.parties.get(pid)
            if p is None or p["status"] != "招募中":
                await reply(interaction, "這個揪團已經不能加入了。")
                return
            if p["leader_id"] == uid or involved(p, uid):
                await reply(interaction, "你已經在這一團了。")
                return
            entry = {"uid": uid, "char": char["角色名稱"], "job": char.get("職業", ""), "pos": char.get("位置", "")}
            if not p["waitlist"] and free_seats(p) > 0:
                p["members"].append(entry)
                text = f"✅ 已經加入（{len(active_members(p))}/{MAX_MEMBERS}）。"
            else:
                p["waitlist"].append(entry)
                text = f"隊伍滿了，你排在候補第 {len(p['waitlist'])} 位。有空位時機器人會 tag 你，5 分鐘內按「我要遞補」就能加入。"
            await self.save(p)
        await self.refresh_card(p)
        audit.audit("揪團：加入", who=interaction.user.display_name, detail=f"{p['server']} {p['place']}｜{entry['char']}")
        await reply(interaction, text)

    async def leave(self, interaction, pid):
        uid = str(interaction.user.id)
        async with self.lock:
            p = self.parties.get(pid)
            if p is None or p["status"] != "招募中":
                await reply(interaction, "這個揪團已經不能退出了。")
                return
            if p["leader_id"] == uid:
                await reply(interaction, "發起人不能退出，要取消的話請按「⚙️ 發起人管理」→「取消揪團」。")
                return
            if (p.get("offer") or {}).get("uid") == uid:
                await self._close_offer(p, f"🚪 <@{uid}> 退出了，不遞補。")
            elif any(m["uid"] == uid for m in p["members"]):
                p["members"] = [m for m in p["members"] if m["uid"] != uid]
            elif any(w["uid"] == uid for w in p["waitlist"]):
                p["waitlist"] = [w for w in p["waitlist"] if w["uid"] != uid]
            else:
                await reply(interaction, "你不在這一團。")
                return
            await self.save(p)
        await self.refresh_card(p)
        audit.audit("揪團：退出", who=interaction.user.display_name, detail=f"{p['server']} {p['place']}")
        await reply(interaction, "✅ 已經退出。")
        await self.maybe_offer(pid)

    async def quit_midway(self, interaction, pid):
        uid = str(interaction.user.id)
        async with self.lock:
            p = self.parties.get(pid)
            if p is None or p["status"] != "已開打":
                await reply(interaction, "這個揪團不在進行中。")
                return
            if p["leader_id"] == uid:
                await reply(interaction, "發起人不能中途退出，要結束的話請按「⚙️ 發起人管理」→「結束」。")
                return
            m = next((m for m in p["members"] if m["uid"] == uid and not m.get("quit")), None)
            if m is None:
                await reply(interaction, "你不在這一團的隊伍裡。")
                return
            m["quit"] = True
            await self.save(p)
        await self.refresh_card(p)
        await self.notify(f"🚪 <@{p['leader_id']}> {m['char']}（<@{uid}>）中途退出了。需要補人的話，按「⚙️ 發起人管理」→「補人」。"
                          f"\n👉 {self.card_url(p)}", mention_ids=[p["leader_id"]])
        audit.audit("揪團：中途退出", who=interaction.user.display_name, detail=f"{p['server']} {p['place']}")
        await reply(interaction, "✅ 已經標示為中途退出，也通知發起人了。")

    # ---- 候補遞補 ----

    async def maybe_offer(self, pid):
        """有空位、有候補、現在沒有人正在被叫 → tag 候補第 1 位。開打前自動叫；開打後要發起人按過「補人」。"""
        async with self.lock:
            p = self.parties.get(pid)
            if p is None or p["status"] not in ACTIVE or p.get("offer") or not p["waitlist"] or free_seats(p) <= 0:
                return
            if p["status"] == "已開打" and not p.get("fill"):
                return
            w = p["waitlist"].pop(0)
            nonce = secrets.token_hex(3)
            p["offer"] = {**w, "nonce": nonce, "expires": fmt(now_tw() + timedelta(minutes=OFFER_MINUTES))}
            await self.save(p)
        view = discord.ui.View(timeout=None)
        view.add_item(OfferAcceptButton(pid, nonce))
        view.add_item(OfferDeclineButton(pid, nonce))
        msg = await self.notify(f"🙋 <@{w['uid']}> {p['server']} {p['place']} 的揪團有空位了！"
                                f"{OFFER_MINUTES} 分鐘內按「我要遞補」就能加入（只有你按得動）。\n👉 {self.card_url(p)}",
                                mention_ids=[w["uid"]], view=view)
        async with self.lock:
            if p.get("offer") and p["offer"]["nonce"] == nonce:
                p["offer"]["msg"] = msg.id
                await self.save(p)
        await self.refresh_card(p)

    async def _close_offer(self, p: dict, text: str):
        """結束目前的遞補邀請：把邀請訊息的按鈕拿掉並加上說明。呼叫的地方要持有 self.lock。"""
        o = p.get("offer")
        p["offer"] = None
        if o and o.get("msg"):
            try:
                msg = await (await self._channel(self.notify_id)).fetch_message(int(o["msg"]))
                await msg.edit(content=f"{msg.content}\n\n{text}", view=None, allowed_mentions=NONE_MENTIONS)
            except discord.HTTPException:
                pass

    async def answer_offer(self, interaction, pid, nonce, accept: bool):
        uid = str(interaction.user.id)
        async with self.lock:
            p = self.parties.get(pid)
            o = (p or {}).get("offer")
            if p is None or not o or o["nonce"] != nonce:
                await reply(interaction, "這個遞補邀請已經失效了。")
                return
            if o["uid"] != uid:
                await reply(interaction, "只有被 tag 的人可以按這個按鈕。")
                return
            if accept:
                p["members"].append({k: o[k] for k in ("uid", "char", "job", "pos")})
                p["fill"] = False
                await self._close_offer(p, f"✅ {o['char']} 已經遞補進隊伍。")
            else:
                await self._close_offer(p, f"❌ {o['char']} 不遞補，換下一位。")
            await self.save(p)
        await self.refresh_card(p)
        audit.audit("揪團：遞補" if accept else "揪團：放棄遞補", who=interaction.user.display_name,
                    detail=f"{p['server']} {p['place']}")
        await reply(interaction, "✅ 已經遞補進隊伍了！" if accept else "好的，已經把位置讓給下一位。")
        await self.maybe_offer(pid)

    # ---- 發起人 ----

    async def _lead(self, interaction, pid, need=ACTIVE):
        p = self.parties.get(pid)
        if p is None or p["status"] not in need:
            await reply(interaction, "這個揪團現在不能做這件事。")
            return None
        if not can_lead(interaction, p):
            await reply(interaction, "只有發起人可以管理這個揪團。")
            return None
        return p

    async def start_party(self, interaction, pid):
        async with self.lock:
            p = await self._lead(interaction, pid, ("招募中",))
            if p is None:
                return
            p["status"] = "已開打"
            p["started_at"] = fmt(now_tw())
            await self.save(p)
        await self.refresh_card(p)
        audit.audit("揪團：開打", who=interaction.user.display_name, detail=f"{p['server']} {p['place']}｜{len(active_members(p))} 人")
        await reply(interaction, "⚔️ 開打了！之後隊員可以按「中途退出」，需要補人時按「⚙️ 發起人管理」→「補人」。")

    async def request_fill(self, interaction, pid):
        async with self.lock:
            p = await self._lead(interaction, pid, ("已開打",))
            if p is None:
                return
            if not p["waitlist"]:
                await reply(interaction, "候補沒有人了。")
                return
            if p.get("offer"):
                await reply(interaction, "正在等候補的人回應，請稍等。")
                return
            if free_seats(p) <= 0:
                await reply(interaction, "隊伍已經滿了，要先移除中途退出的人才有位置。")
                return
            p["fill"] = True
            await self.save(p)
        await reply(interaction, "✅ 開始叫候補，5 分鐘沒回應會自動換下一位。")
        await self.maybe_offer(pid)

    async def remove_person(self, interaction, pid, uid, where):
        async with self.lock:
            p = await self._lead(interaction, pid)
            if p is None:
                return
            if uid == p["leader_id"]:
                await reply(interaction, "不能移除發起人。")
                return
            if where == "m":
                target = next((m for m in p["members"] if m["uid"] == uid), None)
                p["members"] = [m for m in p["members"] if m["uid"] != uid]
            else:
                target = next((w for w in p["waitlist"] if w["uid"] == uid), None)
                p["waitlist"] = [w for w in p["waitlist"] if w["uid"] != uid]
            if target is None:
                await reply(interaction, "這個人已經不在這一團了。")
                return
            if uid not in p["banned"]:
                p["banned"].append(uid)
            await self.save(p)
        await self.refresh_card(p)
        await self.notify(f"➖ <@{uid}> 你被發起人移出了 {p['server']} {p['place']} 的揪團。\n👉 {self.card_url(p)}",
                          mention_ids=[uid])
        audit.audit("揪團：移除隊友", who=interaction.user.display_name, detail=f"{p['server']} {p['place']}｜{target['char']}")
        await reply(interaction, f"✅ 已經移除 {target['char']}。")
        if p["status"] == "招募中":
            await self.maybe_offer(pid)

    async def edit_info(self, interaction, pid, server, place, note, start):
        async with self.lock:
            p = await self._lead(interaction, pid)
            if p is None:
                return
            changes = []
            if server != p["server"] or place != p["place"]:
                changes.append(f"集合改成 {server}　{place}")
            p["server"], p["place"], p["note"] = server, place, note
            if start is not None and p["status"] == "招募中" and fmt(start) != p["start"]:
                p["start"] = fmt(start)
                p["deadline"] = fmt(start + timedelta(minutes=CLOSE_MINUTES))
                p["warned"] = p["reminded"] = p["pinged"] = False
                changes.append(f"開打時間改成 {when_text(start)}")
            await self.save(p)
        await self.refresh_card(p)
        others = [i for i in everyone_in(p) if i != str(interaction.user.id)]
        if changes and others:
            await self.notify(f"✏️ {mention_list(others)}\n揪團資訊有更新：{'；'.join(changes)}\n👉 {self.card_url(p)}",
                              mention_ids=others)
        audit.audit("揪團：修改資訊", who=interaction.user.display_name, detail="；".join(changes) or "備註")
        await reply(interaction, "✅ 已經更新揪團資訊。")

    async def extend(self, interaction, pid):
        async with self.lock:
            p = await self._lead(interaction, pid, ("招募中",))
            if p is None:
                return
            base = max(parse(p["deadline"]), now_tw())
            p["deadline"] = fmt(base + timedelta(minutes=CLOSE_MINUTES))
            p["warned"] = False
            await self.save(p)
        await self.refresh_card(p)
        audit.audit("揪團：延長", who=interaction.user.display_name, detail=p["deadline"])
        await reply(interaction, f"✅ 已經延長，{fmt(parse(p['deadline']))[11:16]} 前沒開打才會自動關閉。")

    async def finish(self, pid, status: str, by=None):
        """結束一團：已結束（打完）、已關閉（沒開打自動關閉）、已取消（發起人取消）。"""
        async with self.lock:
            p = self.parties.get(pid)
            if p is None or p["status"] not in ACTIVE:
                if by is not None:
                    await reply(by, "這個揪團已經結束了。")
                return
            if by is not None and not can_lead(by, p):
                await reply(by, "只有發起人可以管理這個揪團。")
                return
            if p.get("offer"):
                await self._close_offer(p, "這個揪團已經結束了。")
            p["status"] = status
            notify_ids = everyone_in(p)
            await self.save(p)
            self.parties.pop(pid, None)
        await self.refresh_card(p)
        others = [i for i in notify_ids if by is None or i != str(by.user.id)]
        if status == "已關閉" and others:
            await self.notify(f"⏹️ {mention_list(others)}\n{p['server']} {p['place']} 的揪團沒有開打，已經自動關閉。"
                              f"\n👉 {self.card_url(p)}", mention_ids=others)
        elif status == "已取消" and others:
            await self.notify(f"❌ {mention_list(others)}\n{p['server']} {p['place']} 的揪團被發起人取消了。"
                              f"\n👉 {self.card_url(p)}", mention_ids=others)
        audit.audit(f"揪團：{status}", who=by.user.display_name if by else "系統", detail=f"{p['server']} {p['place']}")
        if by is not None:
            await reply(by, {"已結束": "🏁 揪團已經結束。", "已取消": "❌ 揪團已經取消，也通知大家了。"}.get(status, "完成"))

    # ---- 計時 ----

    @tasks.loop(seconds=30)
    async def timer(self):
        try:
            await self.tick()
        except Exception as e:
            audit.error("揪團：計時檢查失敗", e)

    @timer.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    async def tick(self, now: Optional[datetime] = None):
        """每 30 秒：候補沒回應換下一位、自動關閉提醒與關閉、預約提醒、開打後自動結束。"""
        now = now or now_tw()
        for pid in list(self.parties):
            p = self.parties.get(pid)
            if p is None:
                continue
            try:
                o = p.get("offer")
                if o and now >= parse(o["expires"]):
                    async with self.lock:
                        if p.get("offer") is o:
                            await self._close_offer(p, f"⌛ {o['char']} {OFFER_MINUTES} 分鐘沒有回應，移出候補，換下一位。")
                            await self.save(p)
                    await self.refresh_card(p)
                    audit.audit("揪團：候補沒回應，移出候補", who="系統", detail=o["char"])
                    await self.maybe_offer(pid)
                if p["status"] == "招募中":
                    start, deadline = parse(p["start"]), parse(p["deadline"])
                    if p["kind"] == "預約" and not p["reminded"] and start - timedelta(minutes=REMIND_BEFORE_MINUTES) <= now < start:
                        ids = everyone_in(p)
                        p["reminded"] = True
                        await self.save(p)
                        await self.notify(f"⏰ {mention_list(ids)}\n{p['server']} {p['place']} 的揪團 "
                                          f"{REMIND_BEFORE_MINUTES} 分鐘後（{fmt(start)[11:16]}）開打，準備集合！\n👉 {self.card_url(p)}",
                                          mention_ids=ids)
                    if p["kind"] == "預約" and not p["pinged"] and now >= start:
                        p["pinged"] = True
                        await self.save(p)
                        await self.notify(f"🔔 <@{p['leader_id']}> 預約的時間到了，人到齊就按「⚙️ 發起人管理」→「開打」。"
                                          f"\n👉 {self.card_url(p)}", mention_ids=[p["leader_id"]])
                    if now >= deadline:
                        await self.finish(pid, "已關閉")
                        continue
                    if not p["warned"] and now >= deadline - timedelta(minutes=WARN_BEFORE_MINUTES):
                        p["warned"] = True
                        await self.save(p)
                        view = discord.ui.View(timeout=None)
                        view.add_item(ExtendButton(pid))
                        await self.notify(f"⏰ <@{p['leader_id']}> {p['server']} {p['place']} 的揪團再 {WARN_BEFORE_MINUTES} 分鐘"
                                          f"就會自動關閉，還在湊人的話可以延長。\n👉 {self.card_url(p)}",
                                          mention_ids=[p["leader_id"]], view=view)
                elif p["status"] == "已開打" and now >= parse(p["started_at"]) + timedelta(hours=AUTO_END_HOURS):
                    await self.finish(pid, "已結束")
            except Exception as e:
                audit.error(f"揪團：處理 {p.get('server')} {p.get('place')} 的計時失敗", e)

    # ---- 指令 ----

    @commands.hybrid_command(name="setparty", description="管理員：設定揪團看板（放卡片）和揪團通知（@ 人）兩個頻道")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(board="揪團看板：放每一場的卡片", notify="揪團通知：@everyone、叫候補、提醒都發在這裡")
    async def set_party(self, ctx, board: discord.TextChannel, notify: discord.TextChannel):
        await ctx.defer(ephemeral=True)
        problems = []
        bp, np_ = board.permissions_for(ctx.guild.me), notify.permissions_for(ctx.guild.me)
        for ch, perms, need in ((board, bp, {"send_messages": "傳送訊息", "embed_links": "嵌入連結",
                                             "read_message_history": "讀取訊息歷史"}),
                                (notify, np_, {"send_messages": "傳送訊息", "read_message_history": "讀取訊息歷史"})):
            miss = [label for k, label in need.items() if not getattr(perms, k, False)]
            if miss:
                problems.append(f"{ch.mention} 缺少：{'、'.join(miss)}")
        if problems:
            await ctx.send("⚠️ 機器人的權限不夠：\n・" + "\n・".join(problems), ephemeral=True)
            return
        self.board_id, self.notify_id = board.id, notify.id
        await asyncio.to_thread(self.store.save_party_channels, board.id, notify.id)
        await asyncio.to_thread(self.store.get_party_servers)     # 第一次用就把伺服器清單分頁建好
        msg = f"✅ 揪團看板：{board.mention}，揪團通知：{notify.mention}。"
        if not getattr(np_, "mention_everyone", False):
            msg += ("\n⚠️ 機器人在 " + notify.mention + " 沒有「提及 @everyone、@here 和所有身分組」權限，"
                    "臨時揪團的 @everyone 會顯示文字但**不會真的通知到人**。")
        msg += "\n伺服器的下拉選單在試算表「揪團伺服器」分頁，可以直接改。接下來用 `/postparty` 發揪團公告。"
        audit.audit("設定揪團頻道", who=ctx.author.display_name, detail=f"看板 #{board.name}｜通知 #{notify.name}")
        await ctx.send(msg, ephemeral=True)

    @commands.hybrid_command(name="postparty", description="管理員：發一則揪團公告（發起揪團、我的揪團）")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="發在哪個頻道（不填就是目前這個頻道）")
    async def post_party(self, ctx, channel: Optional[discord.TextChannel] = None):
        await ctx.defer(ephemeral=True)
        if self.board_id is None:
            await ctx.send("⚠️ 請先用 `/setparty` 設定揪團看板和通知頻道。", ephemeral=True)
            return
        target = channel or ctx.channel
        embed = discord.Embed(
            title="🗼 爬塔揪團",
            description=(f"想找人一起爬塔，按「📣 發起揪團」，招募卡片會出現在 <#{self.board_id}>，"
                         f"通知發在 <#{self.notify_id}>。\n\n"
                         "📣 **發起揪團**：臨時（現在就打）或預約（指定時間）\n"
                         "📋 **我的揪團**：看自己參加了哪些團\n\n"
                         "想加入的話，到看板找招募中的卡片按「✋ 加入」。"),
            color=discord.Color.green())
        try:
            msg = await target.send(embed=embed, view=PartyPanelView(self.bot))
        except discord.Forbidden:
            await ctx.send(f"⚠️ 機器人在 {target.mention} 沒有「傳送訊息」或「嵌入連結」權限。", ephemeral=True)
            return
        audit.audit("發佈揪團公告", who=ctx.author.display_name, detail=f"#{target.name}")
        await ctx.send(f"✅ 已在 {target.mention} 發佈揪團公告：{msg.jump_url}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Party(bot))
