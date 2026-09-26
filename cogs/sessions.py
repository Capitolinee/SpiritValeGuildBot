import asyncio
import base64
import json
import mimetypes
import re
from datetime import datetime, timezone

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from helpers import resolve_display_name, now_str
from store import SHEET_SESSIONS
import audit

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


async def finish_menu(interaction: discord.Interaction, text: str):
    """
    選單操作完成後的收尾：
    - 選單是私密的（用 / 叫出來、只有操作者看得到）→ 把私密選單收掉，結果公告到頻道讓大家看到
    - 選單本來就是公開的（用 ! 叫出來）→ 直接把選單訊息改成結果
    """
    msg = interaction.message
    if msg is not None and msg.flags.ephemeral:
        try:
            await interaction.delete_original_response()
        except Exception:
            await interaction.edit_original_response(content="✅ 已完成，結果已公告在頻道。", view=None)
        await interaction.channel.send(f"{text}\n-# 由 {interaction.user.display_name} 操作")
    else:
        await interaction.edit_original_response(content=text, view=None)


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
        return None, "⚠️ 目前沒有進行中的場次，請先上傳隊員圖片，或改用 `/donate` 記錄捐獻的寶物。"

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
    audit.audit(
        "記錄寶物", who=operator or "（未知）",
        detail=f"場次 {session_id or '（捐獻）'}｜類型 {item_type}｜{summary}",
    )
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
        audit.audit(
            "開場（無掉落）", who=interaction.user.display_name,
            detail=f"場次 {session_id}｜出席 {len(members)} 人：{names}",
        )
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
            audit.audit(
                "開場（圖片辨識）", who=interaction.user.display_name,
                detail=f"場次 {self.bot.active_session['id']}｜出席 {len(members)} 人：{names}",
            )
            return (
                f"**✅ 已建立場次 `{self.bot.active_session['id']}`，出席：**\n```{names}```\n"
                f"接下來可以用 `/item` 或上傳寶物圖片記錄掉落，之後用 `/loot` 處理寶物。"
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
            await interaction.response.send_message("⚠️ 金額必須是數字，請重新打一次 `/sell`。", ephemeral=True)
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

        audit.audit(
            "結算寶物", who=interaction.user.display_name,
            detail=(f"{result['item_name']}｜類型 {result['item_type']}｜售出 {amount}"
                    + (f"｜{result['n_rows']} 人平分，每人 {result['per_person']:.0f}"
                       if result["item_type"] == "分潤" else "｜進公會基金")),
        )
        text = f"💰 「{result['item_name']}」已賣出 **{amount}**"
        if result["item_type"] == "分潤":
            text += (f"，共 {result['n_rows']} 人平分，每人 **{result['per_person']:.0f}**。"
                     f"隊員可以用 `/claim` 領取。")
        else:
            text += "，已計入公會基金。"
        await finish_menu(interaction, text)

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
            await interaction.response.send_message("這是別人發起的結算，你可以自己打 `/sell` 喔。", ephemeral=True)
            return
        item = self.items[self.values[0]]
        await interaction.response.send_modal(SellAmountModal(self.store, item))


class SellSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=180)
        self.add_item(SellSelect(store, author_id, items))


class GiveToReceiverModal(discord.ui.Modal):
    """選好寶物後，跳出視窗輸入領取者的角色名稱。"""

    def __init__(self, store, item: dict):
        super().__init__(title="登記免費領取")
        self.store = store
        self.item = item
        self.receiver_input = discord.ui.TextInput(
            label=f"「{item['name'][:30]}」的領取者",
            placeholder="輸入登記過的角色名稱",
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
                f"或這個人是不是還沒用 `/profile` 登記過。確認後重新打一次 `/giveto`。",
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

        audit.audit(
            "登記免費領取", who=interaction.user.display_name,
            detail=f"{result['item_name']}｜領取者 {result['receiver']}（{display}）",
        )
        await finish_menu(
            interaction,
            f"🎁 「{result['item_name']}」已登記為成員免費領取（類型：自用，不分潤，已記錄領取時間）。",
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"⚠️ 登記免費領取時發生錯誤：{error!r}", flush=True)
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
        super().__init__(placeholder="選擇要登記免費領取的寶物", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的操作，你可以自己打 `/giveto` 喔。", ephemeral=True)
            return
        item = self.items[self.values[0]]
        await interaction.response.send_modal(GiveToReceiverModal(self.store, item))


class GiveToSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=180)
        self.add_item(GiveToSelect(store, author_id, items))


class LootActionView(discord.ui.View):
    """
    選好一樣寶物之後，決定要對它做什麼：賣出分潤／免費給成員／改成公會收藏。
    重複用既有的 SellAmountModal、GiveToReceiverModal，行為跟 !sell、!giveto 完全一致。
    """

    def __init__(self, store, author_id: int, item: dict):
        super().__init__(timeout=180)
        self.store = store
        self.author_id = author_id
        self.item = item

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "這是別人發起的操作，你可以自己打 `/loot` 喔。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="💰 賣出分潤", style=discord.ButtonStyle.success)
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(SellAmountModal(self.store, self.item))
        self.stop()

    @discord.ui.button(label="🎁 成員免費領取", style=discord.ButtonStyle.primary)
    async def give(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.send_modal(GiveToReceiverModal(self.store, self.item))
        self.stop()

    @discord.ui.button(label="🏦 歸公會（留著之後處理）", style=discord.ButtonStyle.secondary)
    async def to_guild(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_owner(interaction):
            return
        await interaction.response.defer()
        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.change_item_type, self.item["session_id"], self.item["item_index"],
                "公會", self.item["name"],
            )
        if not result["ok"]:
            msg = ("⚠️ 找不到這樣寶物，可能已經被別人處理掉了。"
                   if result["reason"] == "not_found" else "⚠️ 這樣寶物已經結算過了。")
            await interaction.edit_original_response(content=msg, view=None)
            return
        audit.audit(
            "改為公會收藏", who=interaction.user.display_name,
            detail=f"{result['item_name']}",
        )
        await finish_menu(
            interaction,
            (f"🏦 「{result['item_name']}」已改成公會收藏（類型：公會）。"
             f"之後要賣掉換錢進基金，再用 `/loot` 選它結算就好。"),
        )
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"⚠️ 寶物操作時發生錯誤：{error!r}", flush=True)
        try:
            await interaction.followup.send(f"❌ 執行時發生錯誤：{error}", ephemeral=True)
        except Exception:
            pass


class LootSelect(discord.ui.Select):
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
        super().__init__(placeholder="選擇一樣寶物", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的操作，你可以自己打 `/loot` 喔。", ephemeral=True)
            return
        item = self.items[self.values[0]]
        when = item["when"][:16] if item["when"] else "（無日期）"
        view = LootActionView(self.store, self.author_id, item)
        await interaction.response.edit_message(
            content=(f"**{item['name']}**（{when}　類型：{item['item_type']}）\n"
                     f"要對這樣寶物做什麼？"),
            view=view,
        )


class LootSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=180)
        self.add_item(LootSelect(store, author_id, items))


class ClaimSelect(discord.ui.Select):
    def __init__(self, store, author_id: int, items: list):
        self.store = store
        self.author_id = author_id
        self.items = {}
        options = []
        for it in items[:24]:
            key = str(it["row"])
            self.items[key] = it
            session_label = it["session_id"] or "捐獻寶物"
            label = f"{it['item_name']}（{session_label}，+{it['amount']:.0f}）"
            options.append(discord.SelectOption(label=label[:100], value=key))
        options.append(discord.SelectOption(label="✅ 全部一起領取", value="__ALL__"))
        super().__init__(placeholder="選擇要領取哪一樣", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這是別人發起的領取，你可以自己打 `/claim` 喔。", ephemeral=True)
            return
        chosen = self.values[0]
        uid = str(interaction.user.id)
        await interaction.response.defer()

        if chosen == "__ALL__":
            async with self.store.lock:
                result = await asyncio.to_thread(self.store.claim_for_user, uid, None)
            if not result["details"]:
                await interaction.edit_original_response(content="沒有可領取的分潤了（可能剛被領過）。", view=None)
                return
            detail_text = "\n".join(f"{sid or '捐獻寶物'}：{name} +{amt:.0f}" for sid, name, amt in result["details"])
            audit.audit(
                "領取分潤", who=interaction.user.display_name,
                detail=f"共 {result['total']:.2f}｜{len(result['details'])} 筆｜" + "；".join(
                    f"{sid} {name} {amt:.2f}" for sid, name, amt in result["details"]),
            )
            await interaction.edit_original_response(
                content=f"✅ 已領取，共 **{result['total']:.0f}**：\n```{detail_text}```", view=None
            )
            return

        item = self.items[chosen]
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.claim_item_row, uid, item["row"])

        if not result["ok"]:
            await interaction.edit_original_response(content="這筆待領已經被處理掉了（可能剛被領過）。", view=None)
            return
        audit.audit(
            "領取分潤", who=interaction.user.display_name,
            detail=f"{result['session_id'] or '捐獻寶物'} {result['item_name']} {result['amount']:.2f}",
        )
        await interaction.edit_original_response(
            content=f"✅ 已領取「{result['item_name']}」：+**{result['amount']:.0f}**", view=None
        )


class ClaimSelectView(discord.ui.View):
    def __init__(self, store, author_id: int, items: list):
        super().__init__(timeout=120)
        self.add_item(ClaimSelect(store, author_id, items))


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

        # 開啟自動翻譯的頻道，不要拿去做隊員/寶物圖片辨識，
        # 否則公告裡的圖片會白白吃掉辨識額度。
        translate_cog = self.bot.get_cog("Translate")
        if translate_cog and str(message.channel.id) in getattr(translate_cog, "channels", set()):
            return

        for attachment in message.attachments:
            if not any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                continue

            await message.channel.send("🔍 正在辨識圖片中的內容...")
            try:
                image_bytes = await attachment.read()
                mime_type = mimetypes.guess_type(attachment.filename)[0] or "image/png"
                image_b64 = base64.b64encode(image_bytes).decode("utf-8")

                # Gemini 讀一張圖要好幾秒，一定要丟到背景執行緒去跑。
                # 直接呼叫的話整支機器人會凍住，這幾秒內別人打的 / 指令來不及在 3 秒內回應 Discord，
                # 就會被當成「沒有反應」直接丟掉（! 指令只是晚點處理，所以看起來正常）。
                result = await asyncio.to_thread(
                    self.bot.gemini.interactions.create,
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

    @commands.hybrid_command(name="startsession", description="手動輸入隊員名單開場（不用截圖）")
    @app_commands.describe(names="隊員角色名稱，用逗號或空白分隔，例如 熊爺,柒柒,Open匠")
    async def start_session(self, ctx, *, names: str):
        """手動輸入隊員名單開場（不用上傳圖片、不耗圖片辨識額度）。"""
        raw_names = [
            n.strip()
            for n in names.replace("\n", ",").replace("、", ",").replace(" ", ",").split(",")
            if n.strip()
        ]
        if not raw_names:
            await ctx.send("⚠️ 至少要輸入一個隊員名字，例如 `熊爺,柒柒,Open匠`。", ephemeral=True)
            return

        await ctx.defer()
        members = await build_session_members(self.store, ctx.guild, raw_names)
        self.bot.active_session = {
            "id": new_session_id(), "members": members, "next_item_index": 0,
        }
        names_text = "、".join(m["display_name"] for m in members)
        unmatched = [m["name"] for m in members if not m["discord_id"]]

        audit.audit(
            "開場（手動輸入）", who=ctx.author.display_name,
            detail=f"場次 {self.bot.active_session['id']}｜出席 {len(members)} 人：{names_text}",
        )
        reply = (
            f"**✅ 已建立場次 `{self.bot.active_session['id']}`，出席 {len(members)} 人：**\n```{names_text}```\n"
            f"接下來用 `/item` 記錄掉落、`/loot` 處理寶物；這場沒有掉落的話用 `/noloot` 記錄出席。"
        )
        if unmatched:
            reply += (
                f"\n\n⚠️ 這些名字還沒對應到 Discord 帳號：{'、'.join(unmatched)}\n"
                f"他們要先用 `/profile` 登記角色，之後再用 `/syncmembers` 就會自動補上。"
            )
        await ctx.send(reply)

    @commands.hybrid_command(name="noloot", description="這場沒有掉落寶物，直接記錄出席")
    async def no_loot(self, ctx):
        """已經開場、事後才確定這場沒有掉寶時，補記錄出席。"""
        session = self.bot.active_session
        if not session:
            await ctx.send("目前沒有進行中的場次。", ephemeral=True)
            return
        await ctx.defer()
        async with self.store.lock:
            await asyncio.to_thread(
                self.store.record_attendance, session["id"], now_str(), session["members"],
                ctx.author.display_name,
            )
        audit.audit(
            "補記出席（無掉落）", who=ctx.author.display_name,
            detail=f"場次 {session['id']}｜{len(session['members'])} 人",
        )
        await ctx.send(f"✅ 已補記錄場次 `{session['id']}` 的出席（沒有掉落寶物）。")

    @commands.hybrid_command(name="item", description="記錄一樣寶物掉落")
    @app_commands.describe(name="寶物名稱", item_type="類型（預設分潤）")
    async def add_item(self, ctx, name: str, item_type: Literal["分潤", "公會", "自用"] = "分潤"):
        """記錄一樣寶物掉落，預設是「分潤」類型（需要有進行中的場次）。"""
        await ctx.defer()
        reply, err = await record_items(
            self.bot, [name], item_type=item_type, operator=ctx.author.display_name
        )
        await ctx.send(err or reply)

    @commands.hybrid_command(name="items", description="一次記錄多樣寶物（都算分潤）")
    @app_commands.describe(names="寶物名稱，用逗號分隔，例如 屠龍刀,精靈弓")
    async def add_items(self, ctx, *, names: str):
        """一次記錄多樣寶物（都算「分潤」類型），每一樣都會各自拿到自己的編號。"""
        item_names = [
            n.strip()
            for n in names.replace("\n", ",").replace("、", ",").split(",")
            if n.strip()
        ]
        if not item_names:
            await ctx.send("⚠️ 至少要輸入一樣寶物名稱，例如 `屠龍刀,精靈弓`。", ephemeral=True)
            return
        await ctx.defer()
        reply, err = await record_items(
            self.bot, item_names, item_type="分潤", operator=ctx.author.display_name
        )
        await ctx.send(err or reply)

    @commands.hybrid_command(name="donate", description="記錄捐獻給公會的寶物")
    @app_commands.describe(item_name="寶物名稱", contributor="貢獻者（選填）")
    async def donate_item(self, ctx, item_name: str, *, contributor: Optional[str] = None):
        """記錄捐獻的寶物（不屬於任何場次，歸公會）。"""
        await ctx.defer()
        reply, err = await record_items(
            self.bot, [item_name], item_type="公會", contributor=contributor, force_no_session=True,
            operator=ctx.author.display_name,
        )
        await ctx.send(err or reply)

    async def _send_item_picker(self, ctx, view_cls, prompt: str):
        """列出還沒結算的寶物讓操作者選（用 / 叫出來時選單只有操作者看得到）。"""
        await ctx.defer(ephemeral=True)
        items = await asyncio.to_thread(self.store.list_unsold_items)
        if not items:
            await ctx.send("目前沒有任何還沒結算的寶物。", ephemeral=True)
            return
        view = view_cls(self.store, ctx.author.id, items)
        more = f"（只顯示最近 25 筆，共 {len(items)} 筆）" if len(items) > 25 else ""
        await ctx.send(f"{prompt}{more}", view=view, ephemeral=True)

    @commands.hybrid_command(name="loot", description="選一樣寶物：賣出分潤／免費領取／歸公會")
    async def loot(self, ctx):
        """以寶物為出發點的整合指令：先選寶物，再決定要賣出、免費給成員、還是歸公會。"""
        await self._send_item_picker(ctx, LootSelectView, "請選擇要處理的寶物：")

    @commands.hybrid_command(name="sell", description="結算寶物售出金額（不填參數就跳選單）")
    @app_commands.describe(
        index="寶物編號（不填就跳選單）",
        amount="售出金額",
        session_id="場次ID（不填就是目前進行中的場次）",
    )
    async def sell_item(self, ctx, index: Optional[int] = None, amount: Optional[int] = None,
                        session_id: Optional[str] = None):
        """結算寶物售出金額。不填參數會跳出選單。"""
        if index is None and amount is None:
            await self._send_item_picker(ctx, SellSelectView, "請選擇要結算的寶物：")
            return
        if index is None or amount is None:
            await ctx.send("⚠️ 編號跟金額要一起填，或兩個都不填改用選單。", ephemeral=True)
            return
        if session_id is None:
            if not self.bot.active_session:
                await ctx.send("⚠️ 目前沒有進行中的場次，請指定場次ID，或直接用 `/sell` 跳選單。", ephemeral=True)
                return
            session_id = self.bot.active_session["id"]

        await ctx.defer()
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.sell_item, session_id, index, amount)

        if not result["ok"]:
            if result["reason"] == "not_found":
                await ctx.send(f"⚠️ 找不到編號 {index}，請確認場次ID跟編號是否正確。")
            else:
                await ctx.send(f"⚠️ 編號 {index} 已經賣過了，不能重複結算。")
            return

        audit.audit(
            "結算寶物", who=ctx.author.display_name,
            detail=(f"{result['item_name']}｜類型 {result['item_type']}｜售出 {amount}"
                    + (f"｜{result['n_rows']} 人平分，每人 {result['per_person']:.2f}"
                       if result["item_type"] == "分潤" else "｜進公會基金")),
        )
        await ctx.send(
            f"💰 「{result['item_name']}」已賣出 **{amount}**"
            + (f"，共 {result['n_rows']} 人平分，每人 **{result['per_person']:.0f}**。隊員可以用 `/claim` 領取。"
               if result["item_type"] == "分潤" else "，已計入公會基金。")
        )

    @commands.hybrid_command(name="giveto", description="登記寶物由成員免費領取（不填參數就跳選單）")
    @app_commands.describe(
        index="寶物編號（不填就跳選單）",
        receiver="領取者的角色名稱",
        session_id="場次ID（不填就是目前進行中的場次）",
    )
    async def give_to(self, ctx, index: Optional[int] = None, receiver: Optional[str] = None,
                      session_id: Optional[str] = None):
        """原本要分潤的寶物，改成由某個成員免費領取（類型改「自用」、金額 0）。"""
        if index is None and receiver is None:
            await self._send_item_picker(ctx, GiveToSelectView, "請選擇要登記免費領取的寶物：")
            return
        if index is None or receiver is None:
            await ctx.send("⚠️ 編號跟角色名稱要一起填，或兩個都不填改用選單。", ephemeral=True)
            return
        if session_id is None:
            if not self.bot.active_session:
                await ctx.send("⚠️ 目前沒有進行中的場次，請指定場次ID，或直接用 `/giveto` 跳選單。", ephemeral=True)
                return
            session_id = self.bot.active_session["id"]

        await ctx.defer()
        uid, matched_name = await asyncio.to_thread(self.store.find_user_by_character_name, receiver)
        if not matched_name:
            await ctx.send(
                f"⚠️ 找不到角色「{receiver}」，請確認角色名稱有沒有打錯，"
                f"或這個人是不是還沒用 `/profile` 登記過。"
            )
            return
        display = await resolve_display_name(uid, matched_name, ctx.guild)

        async with self.store.lock:
            result = await asyncio.to_thread(
                self.store.give_item_to_member, session_id, index, receiver, None, display
            )

        if not result["ok"]:
            if result["reason"] == "not_found":
                await ctx.send(f"⚠️ 找不到編號 {index}，請用 `/sessioninfo` 確認編號。")
            else:
                await ctx.send(f"⚠️ 編號 {index} 已經結算過了，不能再登記免費領取。")
            return

        audit.audit(
            "登記免費領取", who=ctx.author.display_name,
            detail=f"{result['item_name']}｜領取者 {result['receiver']}（{display}）",
        )
        await ctx.send(
            f"🎁 「{result['item_name']}」已登記為成員免費領取（類型：自用，不分潤，已記錄領取時間）。"
        )

    @commands.hybrid_command(name="sessioninfo", description="查看目前場次的出席名單與寶物狀態")
    async def session_info(self, ctx):
        """查看目前進行中場次的出席名單與寶物狀態。"""
        session = self.bot.active_session
        if not session:
            await ctx.send("目前沒有進行中的場次。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        rows = await asyncio.to_thread(self.store.get_session_rows, session["id"])

        by_index = {}
        for r in rows:
            if r.get("類型") == "出席":
                continue
            by_index.setdefault(r.get("寶物編號", ""), []).append(r)

        names = "、".join(m["display_name"] for m in session["members"])
        lines = [f"場次 ID：{session['id']}", f"出席：{names}", "寶物："]
        if not by_index:
            lines.append("  （尚未記錄任何寶物）")
        for idx, group in by_index.items():
            first = group[0]
            if first.get("售出金額", "").strip():
                status = f"已賣 {first['售出金額']}"
                if first.get("均分$$", "").strip():
                    status += f"（每人 {float(first['均分$$']):.0f}）"
            else:
                status = "未賣出"
            lines.append(f"  [{idx}] {first.get('掉落')}（{first.get('類型')}）：{status}")

        await ctx.send("```" + "\n".join(lines) + "```", ephemeral=True)

    @commands.hybrid_command(name="unclaimed", description="查看場次裡還有誰沒領錢")
    @app_commands.describe(session_id="場次ID（不填就是目前進行中的場次）")
    async def show_unclaimed(self, ctx, session_id: Optional[str] = None):
        """查看場次裡還有誰沒領錢。"""
        if not session_id:
            if not self.bot.active_session:
                await ctx.send("目前沒有進行中的場次，請指定場次ID。", ephemeral=True)
                return
            session_id = self.bot.active_session["id"]

        await ctx.defer(ephemeral=True)
        pending = await asyncio.to_thread(self.store.unclaimed_for_session, session_id)
        if not pending:
            await ctx.send("✅ 這個場次目前沒有人有待領款項。", ephemeral=True)
            return

        lines = []
        for key, amount in pending.items():
            if key.startswith("raw:"):
                display = f"{key[4:]}（未綁定 Discord 帳號，需人工處理）"
            else:
                display = await resolve_display_name(key, key, ctx.guild)
            lines.append(f"- {display}：{amount:.0f}")
        await ctx.send("**💸 尚未領款：**\n```" + "\n".join(lines) + "```", ephemeral=True)

    @commands.hybrid_command(name="claim", description="領取自己的分潤")
    @app_commands.describe(session_id="只領指定場次的全部（不填就一樣一樣選）")
    async def claim(self, ctx, session_id: Optional[str] = None):
        """領取自己尚未領取的分潤。只有一筆直接領，多筆會跳選單一樣一樣選。"""
        await ctx.defer(ephemeral=True)
        uid = str(ctx.author.id)

        if session_id:
            async with self.store.lock:
                result = await asyncio.to_thread(self.store.claim_for_user, uid, session_id)
            if not result["details"]:
                await ctx.send(f"場次 `{session_id}` 沒有可領取的分潤。", ephemeral=True)
                return
            detail_text = "\n".join(f"{sid}：{name} +{amt:.0f}" for sid, name, amt in result["details"])
            audit.audit(
                "領取分潤", who=ctx.author.display_name,
                detail=f"共 {result['total']:.2f}｜{len(result['details'])} 筆｜" + "；".join(
                    f"{sid} {name} {amt:.2f}" for sid, name, amt in result["details"]),
            )
            await ctx.send(f"✅ 已領取，共 **{result['total']:.0f}**：\n```{detail_text}```", ephemeral=True)
            return

        items = await asyncio.to_thread(self.store.pending_items_for_user, uid)
        if not items:
            await ctx.send("目前沒有可領取的分潤。", ephemeral=True)
            return

        if len(items) == 1:
            async with self.store.lock:
                result = await asyncio.to_thread(self.store.claim_item_row, uid, items[0]["row"])
            if not result["ok"]:
                await ctx.send("這筆待領已經被處理掉了（可能剛被領過）。", ephemeral=True)
                return
            audit.audit(
                "領取分潤", who=ctx.author.display_name,
                detail=f"{result['session_id'] or '捐獻寶物'} {result['item_name']} {result['amount']:.2f}",
            )
            await ctx.send(f"✅ 已領取「{result['item_name']}」：+**{result['amount']:.0f}**", ephemeral=True)
            return

        lines = "\n".join(
            f"- {it['item_name']}（{it['session_id'] or '捐獻寶物'}）：+{it['amount']:.0f}" for it in items
        )
        view = ClaimSelectView(self.store, ctx.author.id, items)
        await ctx.send(f"你有 {len(items)} 筆待領，請選擇要領取哪一樣：\n```{lines}```", view=view, ephemeral=True)

    @commands.hybrid_command(name="pending", description="查看自己待領的分潤")
    async def pending(self, ctx):
        """查看自己目前尚未領取的分潤總額與明細。"""
        await ctx.defer(ephemeral=True)
        result = await asyncio.to_thread(self.store.pending_for_user, str(ctx.author.id))
        if not result["details"]:
            await ctx.send("目前沒有待領取的分潤。", ephemeral=True)
            return
        text = "\n".join(f"{sid or '捐獻寶物'}：{name}（{amt:.0f}）" for sid, name, amt in result["details"])
        await ctx.send(f"**💰 待領取分潤，共 {result['total']:.0f}：**\n```{text}```", ephemeral=True)

    @commands.hybrid_command(name="forceclaim", description="管理員：代為標記某人的分潤已領")
    @commands.has_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="要標記的成員", session_id="只標記指定場次（選填）")
    async def force_claim(self, ctx, member: discord.Member, session_id: Optional[str] = None):
        """管理員專用：對方已經私下領過錢、但沒有自己領取時，代為標記已領。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            result = await asyncio.to_thread(self.store.claim_for_user, str(member.id), session_id)

        if not result["details"]:
            scope = f"場次 `{session_id}` " if session_id else ""
            await ctx.send(f"{member.display_name} 目前 {scope}沒有待領取的分潤。", ephemeral=True)
            return

        detail_text = "\n".join(f"{sid}：{name} +{amt:.0f}" for sid, name, amt in result["details"])
        audit.audit(
            "管理員代為標記已領", who=ctx.author.display_name,
            detail=f"對象 {member.display_name}｜共 {result['total']:.2f}｜" + "；".join(
                f"{sid} {name} {amt:.2f}" for sid, name, amt in result["details"]),
        )
        await ctx.send(
            f"✅ 已代為標記 {member.display_name} 的分潤為已領，"
            f"共 **{result['total']:.0f}**：\n```{detail_text}```",
            ephemeral=True,
        )

    @commands.hybrid_command(name="syncmembers", description="有人補登角色後，回頭補上舊記錄的帳號對應")
    async def sync_members(self, ctx):
        """把「場次記錄」裡沒有 Discord ID 的舊記錄，重新比對角色資料補上。"""
        await ctx.defer(ephemeral=True)
        async with self.store.lock:
            backfilled = await asyncio.to_thread(self.store.backfill_discord_ids)

        if not backfilled:
            await ctx.send("沒有發現需要補上 Discord ID 的記錄。", ephemeral=True)
            return

        name_updates = []
        for row, uid, char_name in backfilled:
            display = await resolve_display_name(uid, char_name, ctx.guild)
            name_updates.append((row, 5, [display]))
        async with self.store.lock:
            await asyncio.to_thread(self.store.batch_update_cells, SHEET_SESSIONS, name_updates)

        lines = "\n".join(f"[{row}] {name}" for row, _, name in backfilled)
        audit.audit(
            "回溯補上帳號對應", who=ctx.author.display_name,
            detail=f"{len(backfilled)} 筆｜" + "；".join(f"第{row}列 {name}" for row, _, name in backfilled),
        )
        await ctx.send(f"✅ 已補上 {len(backfilled)} 筆記錄的 Discord ID：\n```{lines}```", ephemeral=True)

    @commands.hybrid_command(name="guildfund", description="查看公會基金總額")
    async def guild_fund(self, ctx):
        """查看公會基金總額。"""
        await ctx.defer(ephemeral=True)
        total = await asyncio.to_thread(self.store.guild_fund_total)
        await ctx.send(f"🏦 公會基金總額：**{total:.0f}**", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Sessions(bot))
