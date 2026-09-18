import asyncio
import base64
import json
import mimetypes
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

from helpers import resolve_display_name, now_str
from store import SHEET_SESSIONS

PROMPT = """
你是一個遊戲紀錄助手。請判斷這張圖片的內容類型，並依照下列規則回傳「純 JSON」，
不要包含任何 Markdown 標記或額外說明文字：

1. 如果圖片是「隊員名單／成員列表」，回傳：
{"type": "member", "data": ["隊員名字1", "隊員名字2"]}

2. 如果圖片是「掉落寶物記錄」，回傳（只要名字，不用數量，同一樣寶物出現幾次就列幾次）：
{"type": "item", "data": ["寶物名稱1", "寶物名稱2"]}

3. 如果兩者都不是，或圖片內容無法辨識，回傳：
{"type": "unknown", "data": []}

請完整讀取圖片中的繁體中文與英文文字後再判斷與提取。
"""


def parse_gemini_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return json.loads(cleaned)


def new_session_id() -> str:
    return f"s{int(datetime.now(timezone.utc).timestamp())}"


async def build_session_members(store, guild, raw_names: list) -> list:
    """
    把辨識到的名字，對應到 Discord 帳號 + 顯示名稱，組成場次的出席名單（一次查完所有名字，不逐個查表）。
    同一個 Discord 帳號底下的不同角色各自算一份（帶兩隻角色出團就分兩份），
    所以這裡只對「完全相同的角色名字」去重複，不對 Discord 帳號去重複。
    """
    lookup = await asyncio.to_thread(store.find_users_by_character_names, raw_names)
    members = []
    seen_names = set()
    for raw_name in raw_names:
        uid, matched_name = lookup.get(raw_name, (None, None))
        char_name = matched_name or raw_name
        name_key = char_name.strip().lower()
        if name_key in seen_names:
            continue  # 同一個角色名字在名單裡重複列到，只算一次
        seen_names.add(name_key)
        display = await resolve_display_name(uid, char_name, guild)
        members.append({"discord_id": uid, "name": char_name, "display_name": display})
    return members


async def record_items(bot, item_names: list, item_type: str = "分潤", contributor: str = None,
                        force_no_session: bool = False, operator: str = ""):
    """
    把一批寶物名稱記錄進去。
    分潤類型需要目前有進行中的場次（bot.active_session）；公會/自用可以有場次也可以沒有（捐獻）。
    operator：誰觸發了這次記錄，寫進場次記錄表的「操作者」欄。
    回傳 (成功訊息, 是否有錯誤)。
    """
    store = bot.store
    session = None if force_no_session else bot.active_session
    session_id = session["id"] if session else None
    members = session["members"] if (session and item_type == "分潤") else []

    if item_type == "分潤" and not session:
        return None, "⚠️ 目前沒有進行中的場次，請先上傳隊員圖片，或改用 `!donate` 記錄捐獻的寶物。"

    now = now_str()
    recorded = []
    async with store.lock:
        for name in item_names:
            if item_type == "分潤":
                idx = session["next_item_index"]
                session["next_item_index"] += 1
            else:
                idx = None
            await asyncio.to_thread(
                store.append_item_rows, session_id, now, members, name, idx, item_type, contributor, operator
            )
            recorded.append(name)

    summary = "、".join(recorded)
    where = f"場次 `{session_id}`" if session_id else "捐獻清單"
    return f"✅ 已將以下寶物記錄進{where}（類型：{item_type}）：\n```{summary}```", None


class EditModal(discord.ui.Modal):
    """辨識結果修改視窗：每行一個名字。"""

    def __init__(self, view: "ConfirmView"):
        super().__init__(title="修改辨識結果")
        self.view_ref = view
        label = "每行一個隊員名字" if view.kind == "member" else "每行一個寶物名稱"
        self.text_input = discord.ui.TextInput(
            label=label, style=discord.TextStyle.paragraph,
            default="\n".join(view.payload), required=True, max_length=2000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        names = [line.strip() for line in self.text_input.value.splitlines() if line.strip()]
        if not names:
            await interaction.response.send_message("⚠️ 內容是空的，未進行任何記錄。", ephemeral=True)
            return
        self.view_ref.payload = names
        await interaction.response.defer()
        reply = await self.view_ref.save(interaction)
        await interaction.edit_original_response(content=reply, view=None)
        self.view_ref.stop()


class ConfirmView(discord.ui.View):
    def __init__(self, bot, kind: str, payload: list, author_id: int, guild):
        super().__init__(timeout=300)
        self.bot = bot
        self.kind = kind
        self.payload = payload
        self.author_id = author_id
        self.guild = guild
        self.message: discord.Message | None = None

        if kind == "member":
            no_loot_button = discord.ui.Button(
                label="📋 這場沒有掉落寶物", style=discord.ButtonStyle.secondary
            )
            no_loot_button.callback = self.no_loot_callback
            self.add_item(no_loot_button)

    async def no_loot_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有上傳圖片的人可以操作喔。", ephemeral=True)
            return
        await interaction.response.defer()

        store = self.bot.store
        members = await build_session_members(store, self.guild, self.payload)
        session_id = new_session_id()
        now = now_str()
        self.bot.active_session = {"id": session_id, "members": members, "next_item_index": 0}

        async with store.lock:
            await asyncio.to_thread(
                store.record_attendance, session_id, now, members, interaction.user.display_name
            )

        names = "、".join(m["display_name"] for m in members)
        content = (
            f"**✅ 已建立場次 `{session_id}`，出席：**\n```{names}```\n"
            f"（已標記這場沒有掉落寶物，出席已直接記錄。）"
        )
        await interaction.edit_original_response(content=content, view=None)
        self.stop()

    def preview_text(self) -> str:
        body = "、".join(self.payload) if self.payload else "（無）"
        label = "隊員名單" if self.kind == "member" else "寶物記錄"
        return f"**🔍 辨識為{label}：**\n```{body}```\n請確認是否正確？"

    async def save(self, interaction: discord.Interaction) -> str:
        store = self.bot.store
        if self.kind == "member":
            members = await build_session_members(store, self.guild, self.payload)
            self.bot.active_session = {
                "id": new_session_id(), "members": members, "next_item_index": 0,
            }
            names = "、".join(m["display_name"] for m in members)
            return (
                f"**✅ 已建立場次 `{self.bot.active_session['id']}`，出席：**\n```{names}```\n"
                f"接下來可以用 `!item 寶物名稱` 或上傳寶物圖片記錄掉落，賣掉後用 `!sell 編號 金額` 結算分潤。"
            )
        else:
            reply, err = await record_items(
                self.bot, self.payload, item_type="分潤", operator=interaction.user.display_name
            )
            return err or reply

    @discord.ui.button(label="✅ 確認正確", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有上傳圖片的人可以確認喔。", ephemeral=True)
            return
        await interaction.response.defer()
        reply = await self.save(interaction)
        await interaction.edit_original_response(content=reply, view=None)
        self.stop()

    @discord.ui.button(label="✏️ 修改後再存", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有上傳圖片的人可以修改喔。", ephemeral=True)
            return
        await interaction.response.send_modal(EditModal(self))

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content=self.message.content + "\n\n⏰ 已逾時未確認，未寫入記錄。", view=None)
            except Exception:
                pass


class SellAmountModal(discord.ui.Modal):
    """選好寶物後，跳出視窗輸入金額。"""

    def __init__(self, store, item: dict):
        super().__init__(title="輸入售出金額")
        self.store = store
        self.item = item
        self.amount_input = discord.ui.TextInput(
            label=f"「{item['name'][:30]}」賣多少錢？",
            placeholder="只填數字，例如 3000",
            required=True,
            max_length=12,
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.amount_input.value.strip().replace(",", "")
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message("⚠️ 金額必須是數字，請重新打一次 `!sell`。", ephemeral=True)
            return

        await interaction.response.defer()
        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.sell_item, self.item["session_id"], self.item["item_index"], amount,
                self.item["name"],
            )

        if not result["ok"]:
            msg = ("⚠️ 找不到這樣寶物，可能已經被別人處理掉了。"
                   if result["reason"] == "not_found" else "⚠️ 這樣寶物已經結算過了。")
            await interaction.edit_original_response(content=msg, view=None)
            return

        text = f"💰 「{result['item_name']}」已賣出 **{amount}**"
        if result["item_type"] == "分潤":
            text += (f"，共 {result['n_rows']} 人平分，每人 **{result['per_person']:.2f}**。"
                     f"隊員可以用 `!claim` 領取。")
        else:
            text += "，已計入公會基金。"
        await interaction.edit_original_response(content=text, view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"⚠️ 結算時發生錯誤：{error!r}", flush=True)
        try:
            await interaction.followup.send(f"❌ 結算時發生錯誤：{error}", ephemeral=True)
        except Exception:
            pass


class SellSelect(discord.ui.Select):
    def __init__(self, store, author_id: int, items: list):
        self.store = store
        self.author_id = author_id
        self.items = {}
        options = []
        for i, it in enumerate(items[:25]):
            key = str(i)
            self.items[key] = it
            when = it["when"][:16] if it["when"] else "（無日期）"
            label = f"{when}　{it['name']}"
            desc = f"類型：{it['item_type']}"
            if it["item_type"] == "分潤":
                desc += f"　{it['n_rows']} 人平分"
            options.append(discord.SelectOption(label=label[:100], value=key, description=desc[:100]))
        super().__init__(placeholder="選擇要結算的寶物", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的結算，你可以自己打 `!sell` 喔。", ephemeral=True)
            return
        item = self.items[self.values[0]]
        await interaction.response.send_modal(SellAmountModal(self.store, item))


class SellSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=180)
        self.add_item(SellSelect(store, author_id, items))


class GiveToReceiverModal(discord.ui.Modal):
    """選好寶物後，跳出視窗輸入是免費給誰。"""

    def __init__(self, store, item: dict):
        super().__init__(title="免費給誰？")
        self.store = store
        self.item = item
        self.receiver_input = discord.ui.TextInput(
            label=f"「{item['name'][:30]}」給誰？",
            placeholder="輸入登記過的角色名稱，例如 熊爺",
            required=True,
            max_length=50,
        )
        self.add_item(self.receiver_input)

    async def on_submit(self, interaction: discord.Interaction):
        receiver = self.receiver_input.value.strip()
        await interaction.response.defer()

        uid, matched_name = await asyncio.to_thread(
            self.store.find_user_by_character_name, receiver
        )
        if not matched_name:
            await interaction.followup.send(
                f"⚠️ 找不到角色「{receiver}」，請確認角色名稱有沒有打錯，"
                f"或這個人是不是還沒用 `!profile` 登記過。確認後重新打一次 `!giveto`。",
                ephemeral=True,
            )
            return
        display = await resolve_display_name(uid, matched_name, interaction.guild)

        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.give_item_to_member, self.item["session_id"], self.item["item_index"],
                receiver, self.item["name"], display,
            )

        if not result["ok"]:
            msg = ("⚠️ 找不到這樣寶物，可能已經被別人處理掉了。"
                   if result["reason"] == "not_found" else "⚠️ 這樣寶物已經結算過了。")
            await interaction.edit_original_response(content=msg, view=None)
            return

        await interaction.edit_original_response(
            content=f"🎁 「{result['item_name']}」已改成免費給 **{result['receiver']}**（類型：自用，不分潤）。",
            view=None,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"⚠️ 免費給人時發生錯誤：{error!r}", flush=True)
        try:
            await interaction.followup.send(f"❌ 執行時發生錯誤：{error}", ephemeral=True)
        except Exception:
            pass


class GiveToSelect(discord.ui.Select):
    def __init__(self, store, author_id: int, items: list):
        self.store = store
        self.author_id = author_id
        self.items = {}
        options = []
        for i, it in enumerate(items[:25]):
            key = str(i)
            self.items[key] = it
            when = it["when"][:16] if it["when"] else "（無日期）"
            label = f"{when}　{it['name']}"
            desc = f"類型：{it['item_type']}"
            if it["item_type"] == "分潤":
                desc += f"　原本 {it['n_rows']} 人平分"
            options.append(discord.SelectOption(label=label[:100], value=key, description=desc[:100]))
        super().__init__(placeholder="選擇要免費給人的寶物", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的操作，你可以自己打 `!giveto` 喔。", ephemeral=True)
            return
        item = self.items[self.values[0]]
        await interaction.response.send_modal(GiveToReceiverModal(self.store, item))


class GiveToSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=180)
        self.add_item(GiveToSelect(store, author_id, items))


class ClaimSelect(discord.ui.Select):
    def __init__(self, store, author_id: int, pending_sessions: list):
        options = [
            discord.SelectOption(label=f"{sid}（待領 {amt:.2f}）", value=sid)
            for sid, amt in pending_sessions[:24]
        ]
        options.append(discord.SelectOption(label="✅ 全部一起領取", value="__ALL__"))
        super().__init__(placeholder="選擇要領取哪一場", options=options, min_values=1, max_values=1)
        self.store = store
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的領取，你可以自己打 `!claim` 喔。", ephemeral=True)
            return
        chosen = self.values[0]
        uid = str(interaction.user.id)
        session_id = None if chosen == "__ALL__" else chosen

        await interaction.response.defer()
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.claim_for_user, uid, session_id)

        if not result["details"]:
            await interaction.edit_original_response(content="沒有可領取的分潤了（可能剛被領過）。", view=None)
            return
        detail_text = "\n".join(f"{sid}：{name} +{amt:.2f}" for sid, name, amt in result["details"])
        await interaction.edit_original_response(
            content=f"✅ 已領取，共 **{result['total']:.2f}**：\n```{detail_text}```", view=None
        )


class ClaimSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, pending_sessions: list):
        super().__init__(timeout=120)
        self.add_item(ClaimSelect(store, author_id, pending_sessions))


class Sessions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.attachments:
            return

        for attachment in message.attachments:
            if not any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                continue

            await message.channel.send("🔍 正在辨識圖片中的內容...")
            try:
                image_bytes = await attachment.read()
                mime_type = mimetypes.guess_type(attachment.filename)[0] or "image/png"
                image_b64 = base64.b64encode(image_bytes).decode("utf-8")

                result = self.bot.gemini.interactions.create(
                    model=self.bot.gemini_model,
                    input=[
                        {"type": "text", "text": PROMPT},
                        {"type": "image", "data": image_b64, "mime_type": mime_type},
                    ],
                )
                parsed = parse_gemini_json(result.output_text)
                kind = parsed.get("type", "unknown")
                # Gemini 有時候會把多個項目塞進同一個字串（例如 ["屠龍刀,精靈弓"]），
                # 這裡再拆一次，不完全信任它有正確分項。
                payload = []
                for raw in parsed.get("data", []):
                    if not isinstance(raw, str):
                        continue
                    for piece in raw.replace("\n", ",").replace("、", ",").split(","):
                        if piece.strip():
                            payload.append(piece.strip())

                if kind == "unknown" or not payload:
                    await message.channel.send("⚠️ 無法判斷這張圖片是隊員名單還是寶物記錄，或內容為空。")
                    continue

                view = ConfirmView(self.bot, kind, payload, message.author.id, message.guild)
                sent = await message.channel.send(view.preview_text(), view=view)
                view.message = sent

            except json.JSONDecodeError:
                await message.channel.send("❌ Gemini 回傳的內容不是有效的 JSON，辨識失敗。")
            except Exception as e:
                error_text = str(e)
                if "429" in error_text or "quota" in error_text.lower() or "RESOURCE_EXHAUSTED" in error_text:
                    match = re.search(r"retry in ([\d.]+)s", error_text)
                    if match:
                        seconds = int(float(match.group(1))) + 1
                        await message.channel.send(
                            f"⏳ 圖片辨識額度暫時用完了，請大約 **{seconds} 秒**後再重新上傳一次圖片。"
                        )
                    else:
                        await message.channel.send(
                            "⏳ 圖片辨識額度暫時用完了（免費額度是每分鐘限制次數），請稍等約 1 分鐘後再重新上傳一次圖片。"
                        )
                else:
                    await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

    @commands.command(name="startsession")
    async def start_session(self, ctx, *, names_text: str):
        """
        手動輸入隊員名單開場（不用上傳圖片、不耗圖片辨識額度）。
        名字用逗號、空白或換行分隔都可以。
        用法：!startsession 熊爺,柒柒,Open匠
             !startsession 熊爺 柒柒 Open匠
        開場後就跟上傳圖片辨識一樣，可以接著用 !item 記錄寶物、!sell 結算、!claim 領取。
        """
        raw_names = [
            n.strip()
            for n in names_text.replace("\n", ",").replace("、", ",").replace(" ", ",").split(",")
            if n.strip()
        ]
        if not raw_names:
            await ctx.send("⚠️ 至少要輸入一個隊員名字。用法：`!startsession 熊爺,柒柒,Open匠`")
            return

        members = await build_session_members(self.store, ctx.guild, raw_names)
        self.bot.active_session = {
            "id": new_session_id(), "members": members, "next_item_index": 0,
        }
        names = "、".join(m["display_name"] for m in members)
        unmatched = [m["name"] for m in members if not m["discord_id"]]

        reply = (
            f"**✅ 已建立場次 `{self.bot.active_session['id']}`，出席 {len(members)} 人：**\n```{names}```\n"
            f"接下來可以用 `!item 寶物名稱` 記錄掉落，賣掉後用 `!sell 編號 金額` 結算分潤；"
            f"這場沒有掉落的話用 `!noloot` 記錄出席。"
        )
        if unmatched:
            reply += (
                f"\n\n⚠️ 這些名字還沒對應到 Discord 帳號：{'、'.join(unmatched)}\n"
                f"他們要先用 `!profile` 登記角色，之後再打一次 `!syncmembers` 就會自動補上。"
            )
        await ctx.send(reply)

    @commands.command(name="noloot")
    async def no_loot(self, ctx):
        """
        如果目前進行中的場次確定沒有掉落寶物，用這個指令補記錄出席
        （適合用在已經用一般流程確認過名單、事後才確定沒有掉寶的情況；
        如果一開始就知道沒有掉寶，直接按隊員確認畫面上的「📋 這場沒有掉落寶物」按鈕更快）。
        """
        session = self.bot.active_session
        if not session:
            await ctx.send("目前沒有進行中的場次。")
            return
        now = now_str()
        async with self.store.lock:
            await asyncio.to_thread(
                self.store.record_attendance, session["id"], now, session["members"], ctx.author.display_name
            )
        await ctx.send(f"✅ 已補記錄場次 `{session['id']}` 的出席（沒有掉落寶物）。")

    @commands.command(name="item")
    async def add_item(self, ctx, *, text: str):
        """
        記錄寶物掉落，預設是「分潤」類型（需要有進行中的場次）。
        用法：!item 寶物名稱
             !item 寶物名稱 公會    → 明確指定類型（分潤/公會/自用）
        """
        parts = text.rsplit(maxsplit=1)
        item_type = "分潤"
        item_name = text
        if len(parts) == 2 and parts[1] in ("分潤", "公會", "自用"):
            item_name, item_type = parts

        reply, err = await record_items(
            self.bot, [item_name], item_type=item_type, operator=ctx.author.display_name
        )
        await ctx.send(err or reply)

    @commands.command(name="items")
    async def add_items(self, ctx, *, names_text: str):
        """
        一次記錄多樣寶物（都算「分潤」類型），名字用逗號、換行分隔。
        用法：!items 屠龍刀,精靈弓,神槍王
        每一樣都會各自拿到自己的編號，之後分別用 !sell 編號 金額 結算。
        """
        item_names = [
            n.strip()
            for n in names_text.replace("\n", ",").replace("、", ",").split(",")
            if n.strip()
        ]
        if not item_names:
            await ctx.send("⚠️ 至少要輸入一樣寶物名稱。用法：`!items 屠龍刀,精靈弓`")
            return

        reply, err = await record_items(
            self.bot, item_names, item_type="分潤", operator=ctx.author.display_name
        )
        await ctx.send(err or reply)

    @commands.command(name="donate")
    async def donate_item(self, ctx, item_name: str, *, contributor: str = None):
        """
        記錄捐獻的寶物（不屬於任何場次，不能分潤，只能是公會或自用）。
        用法：!donate 屠龍刀 牡羊
        """
        reply, err = await record_items(
            self.bot, [item_name], item_type="公會", contributor=contributor, force_no_session=True,
            operator=ctx.author.display_name,
        )
        await ctx.send(err or reply)

    @commands.command(name="sell")
    async def sell_item(self, ctx, *args):
        """
        結算寶物售出金額。
        用法：!sell                        → 跳出選單（日期＋寶物名稱），選完再輸入金額
             !sell 編號 金額            → 直接對目前進行中的場次結算
             !sell 場次ID 編號 金額     → 直接對指定場次結算
        """
        if not args:
            items = await asyncio.to_thread(self.store.list_unsold_items)
            if not items:
                await ctx.send("目前沒有任何還沒結算的寶物。")
                return
            view = SellSelectView(self.store, ctx.author.id, items)
            more = f"（只顯示最近 25 筆，共 {len(items)} 筆）" if len(items) > 25 else ""
            await ctx.send(f"請選擇要結算的寶物：{more}", view=view)
            return

        if len(args) == 2:
            if not self.bot.active_session:
                await ctx.send("⚠️ 目前沒有進行中的場次，請直接打 `!sell` 用選單，或用 `!sell 場次ID 編號 金額`。")
                return
            session_id = self.bot.active_session["id"]
            index_raw, amount_raw = args
        elif len(args) == 3:
            session_id, index_raw, amount_raw = args
        else:
            await ctx.send("⚠️ 用法：`!sell`（選單）、`!sell 編號 金額` 或 `!sell 場次ID 編號 金額`")
            return

        try:
            index = int(index_raw)
            amount = int(amount_raw)
        except ValueError:
            await ctx.send("⚠️ 編號跟金額都必須是數字。")
            return

        async with self.store.lock:
            result = await asyncio.to_thread(self.store.sell_item, session_id, index, amount)

        if not result["ok"]:
            if result["reason"] == "not_found":
                await ctx.send(f"⚠️ 找不到編號 {index}，請確認場次ID跟編號是否正確。")
            else:
                await ctx.send(f"⚠️ 編號 {index} 已經賣過了，不能重複結算。")
            return

        await ctx.send(
            f"💰 「{result['item_name']}」已賣出 **{amount}**"
            + (f"，共 {result['n_rows']} 人平分，每人 **{result['per_person']:.2f}**。隊員可以用 `!claim` 領取。"
               if result["item_type"] == "分潤" else "，已計入公會基金。")
        )

    @commands.command(name="giveto")
    async def give_to(self, ctx, *args):
        """
        原本要分潤的寶物，改成免費給某個成員（類型改「自用」、金額 0，不用分錢）。
        用法：!giveto                           → 跳出選單（日期＋寶物名稱），選完再輸入給誰
             !giveto 編號 成員名稱             → 直接對目前進行中的場次
             !giveto 場次ID 編號 成員名稱     → 直接對指定場次
        """
        if not args:
            items = await asyncio.to_thread(self.store.list_unsold_items)
            if not items:
                await ctx.send("目前沒有任何還沒結算的寶物。")
                return
            view = GiveToSelectView(self.store, ctx.author.id, items)
            more = f"（只顯示最近 25 筆，共 {len(items)} 筆）" if len(items) > 25 else ""
            await ctx.send(f"請選擇要免費給人的寶物：{more}", view=view)
            return

        if len(args) == 2:
            if not self.bot.active_session:
                await ctx.send("⚠️ 目前沒有進行中的場次，請直接打 `!giveto` 用選單，或用 `!giveto 場次ID 編號 成員名稱`。")
                return
            session_id = self.bot.active_session["id"]
            index_raw, receiver = args
        elif len(args) == 3:
            session_id, index_raw, receiver = args
        else:
            await ctx.send("⚠️ 用法：`!giveto`（選單）、`!giveto 編號 成員名稱` 或 `!giveto 場次ID 編號 成員名稱`")
            return

        try:
            index = int(index_raw)
        except ValueError:
            await ctx.send("⚠️ 編號必須是數字。")
            return

        uid, matched_name = await asyncio.to_thread(
            self.store.find_user_by_character_name, receiver
        )
        if not matched_name:
            await ctx.send(
                f"⚠️ 找不到角色「{receiver}」，請確認角色名稱有沒有打錯，"
                f"或這個人是不是還沒用 `!profile` 登記過。"
            )
            return
        display = await resolve_display_name(uid, matched_name, ctx.guild)

        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.give_item_to_member, session_id, index, receiver, None, display
            )

        if not result["ok"]:
            if result["reason"] == "not_found":
                await ctx.send(f"⚠️ 找不到編號 {index}，請用 `!sessioninfo` 確認編號。")
            else:
                await ctx.send(f"⚠️ 編號 {index} 已經結算過了，不能再改成免費給人。")
            return

        await ctx.send(
            f"🎁 「{result['item_name']}」已改成免費給 **{result['receiver']}**（類型：自用，不分潤）。"
        )

    @commands.command(name="sessioninfo")
    async def session_info(self, ctx):
        """查看目前進行中場次的出席名單與寶物狀態。"""
        session = self.bot.active_session
        if not session:
            await ctx.send("目前沒有進行中的場次。")
            return
        rows = await asyncio.to_thread(self.store.get_session_rows, session["id"])

        by_index = {}
        for r in rows:
            idx = r.get("寶物編號", "")
            by_index.setdefault(idx, []).append(r)

        names = "、".join(m["display_name"] for m in session["members"])
        lines = [f"場次 ID：{session['id']}", f"出席：{names}", "寶物："]
        if not by_index:
            lines.append("  （尚未記錄任何寶物）")
        for idx, group in by_index.items():
            first = group[0]
            if first.get("售出金額", "").strip():
                status = f"已賣 {first['售出金額']}"
                if first.get("均分$$", "").strip():
                    status += f"（每人 {first['均分$$']}）"
            else:
                status = "未賣出"
            lines.append(f"  [{idx}] {first.get('掉落')}（{first.get('類型')}）：{status}")

        await ctx.send("```" + "\n".join(lines) + "```")

    @commands.command(name="unclaimed")
    async def show_unclaimed(self, ctx, session_id: str = None):
        """查看場次裡還有誰沒領錢。不填場次ID時查目前進行中的場次。"""
        if not session_id:
            if not self.bot.active_session:
                await ctx.send("目前沒有進行中的場次，請指定場次ID：`!unclaimed 場次ID`。")
                return
            session_id = self.bot.active_session["id"]

        pending = await asyncio.to_thread(self.store.unclaimed_for_session, session_id)
        if not pending:
            await ctx.send("✅ 這個場次目前沒有人有待領款項。")
            return

        lines = []
        for key, amount in pending.items():
            if key.startswith("raw:"):
                display = f"{key[4:]}（未綁定 Discord 帳號，需人工處理）"
            else:
                display = await resolve_display_name(key, key, ctx.guild)
            lines.append(f"- {display}：{amount:.2f}")
        await ctx.send("**💸 尚未領款：**\n```" + "\n".join(lines) + "```")

    @commands.hybrid_command(name="claim")
    async def claim(self, ctx, session_id: str = None):
        """
        領取自己尚未領取的分潤。用 /claim 打的話只有你看得到。
        用法：!claim            → 只有一場待領時直接領取；多場時跳選單
             !claim 場次ID     → 直接領取指定場次
        """
        uid = str(ctx.author.id)

        if session_id:
            async with self.store.lock:
                result = await asyncio.to_thread(self.store.claim_for_user, uid, session_id)
            if not result["details"]:
                await ctx.send(f"場次 `{session_id}` 沒有可領取的分潤。", ephemeral=True)
                return
            detail_text = "\n".join(f"{sid}：{name} +{amt:.2f}" for sid, name, amt in result["details"])
            await ctx.send(f"✅ 已領取，共 **{result['total']:.2f}**：\n```{detail_text}```", ephemeral=True)
            return

        pending_sessions = await asyncio.to_thread(self.store.pending_sessions_for_user, uid)
        if not pending_sessions:
            await ctx.send("目前沒有可領取的分潤。", ephemeral=True)
            return

        if len(pending_sessions) == 1:
            async with self.store.lock:
                result = await asyncio.to_thread(self.store.claim_for_user, uid, pending_sessions[0][0])
            detail_text = "\n".join(f"{sid}：{name} +{amt:.2f}" for sid, name, amt in result["details"])
            await ctx.send(f"✅ 已領取，共 **{result['total']:.2f}**：\n```{detail_text}```", ephemeral=True)
            return

        lines = "\n".join(f"- {sid}：待領 {amt:.2f}" for sid, amt in pending_sessions)
        view = ClaimSelectView(self.store, ctx.author.id, pending_sessions)
        await ctx.send(f"你有多場待領分潤，請選擇要領取哪一場：\n```{lines}```", view=view, ephemeral=True)

    @commands.hybrid_command(name="pending")
    async def pending(self, ctx):
        """查看自己目前尚未領取的分潤總額與明細。用 /pending 打的話只有你看得到。"""
        result = await asyncio.to_thread(self.store.pending_for_user, str(ctx.author.id))
        if not result["details"]:
            await ctx.send("目前沒有待領取的分潤。", ephemeral=True)
            return
        text = "\n".join(f"{sid}：{name}（{amt:.2f}）" for sid, name, amt in result["details"])
        await ctx.send(f"**💰 待領取分潤，共 {result['total']:.2f}：**\n```{text}```", ephemeral=True)

    @commands.command(name="forceclaim")
    @commands.has_permissions(manage_guild=True)
    async def force_claim(self, ctx, member: discord.Member, session_id: str = None):
        """
        管理員專用：把某人尚未領取的分潤標記為已領（用在對方已經私下領過錢，
        但沒有自己打 !claim 的情況，避免系統一直停在「未領」）。
        用法：!forceclaim @某人            → 標記他所有場次的待領
             !forceclaim @某人 場次ID     → 只標記指定場次
        需要「管理伺服器」權限才能使用。
        """
        uid = str(member.id)
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.claim_for_user, uid, session_id)

        if not result["details"]:
            scope = f"場次 `{session_id}` " if session_id else ""
            await ctx.send(f"{member.display_name} 目前 {scope}沒有待領取的分潤。")
            return

        detail_text = "\n".join(f"{sid}：{name} +{amt:.2f}" for sid, name, amt in result["details"])
        await ctx.send(
            f"✅ 已由 {ctx.author.display_name} 代為標記 {member.display_name} 的分潤為已領，"
            f"共 **{result['total']:.2f}**：\n```{detail_text}```"
        )

    @force_claim.error
    async def force_claim_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⚠️ 這個指令需要「管理伺服器」權限才能使用。")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("⚠️ 找不到這個成員，請用 @提及 的方式指定對象。")

    @commands.command(name="syncmembers")
    async def sync_members(self, ctx):
        """
        把「場次記錄」裡沒有 Discord ID 的舊記錄，重新比對現在登記的角色資料，
        找得到就補上（適合在有人事後才補登 !profile 的情況下使用）。
        """
        async with self.store.lock:
            backfilled = await asyncio.to_thread(self.store.backfill_discord_ids)

        if not backfilled:
            await ctx.send("沒有發現需要補上 Discord ID 的記錄。")
            return

        # 順便把 DC名稱（E欄）也補上實際的 Discord 顯示名稱，這部分需要問 Discord API，
        # 所以在這裡（有 guild 物件可用）做，store.py 那邊只負責補 Discord ID。
        name_updates = []
        for row, uid, char_name in backfilled:
            display = await resolve_display_name(uid, char_name, ctx.guild)
            name_updates.append((row, 5, [display]))
        if name_updates:
            await asyncio.to_thread(self.store.batch_update_cells, SHEET_SESSIONS, name_updates)

        lines = "\n".join(f"[{row}] {name}" for row, _, name in backfilled)
        await ctx.send(f"✅ 已補上 {len(backfilled)} 筆記錄的 Discord ID：\n```{lines}```")

    @commands.command(name="guildfund")
    async def guild_fund(self, ctx):
        """查看公會基金總額。"""
        total = await asyncio.to_thread(self.store.guild_fund_total)
        await ctx.send(f"🏦 公會基金總額：**{total:.2f}**")


async def setup(bot):
    await bot.add_cog(Sessions(bot))
