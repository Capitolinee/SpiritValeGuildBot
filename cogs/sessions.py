import asyncio
import base64
import json
import mimetypes
from datetime import datetime, timezone

import discord
from discord.ext import commands

from helpers import resolve_display_name

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
    """把辨識到的名字，對應到 Discord 帳號 + 顯示名稱，組成場次的出席名單。"""
    members = []
    for raw_name in raw_names:
        uid, matched_name = await asyncio.to_thread(store.find_user_by_character_name, raw_name)
        char_name = matched_name or raw_name
        display = await resolve_display_name(uid, char_name, guild)
        members.append({"discord_id": uid, "name": char_name, "display_name": display})
    return members


async def record_items(bot, item_names: list, item_type: str = "分潤", contributor: str = None,
                        force_no_session: bool = False):
    """
    把一批寶物名稱記錄進去。
    分潤類型需要目前有進行中的場次（bot.active_session）；公會/自用可以有場次也可以沒有（捐獻）。
    回傳 (成功訊息, 是否有錯誤)。
    """
    store = bot.store
    session = None if force_no_session else bot.active_session
    session_id = session["id"] if session else None
    members = session["members"] if (session and item_type == "分潤") else []

    if item_type == "分潤" and not session:
        return None, "⚠️ 目前沒有進行中的場次，請先上傳隊員圖片，或改用 `!donate` 記錄捐獻的寶物。"

    now = datetime.now(timezone.utc).isoformat()
    recorded = []
    async with store.lock:
        for name in item_names:
            if item_type == "分潤":
                idx = session["next_item_index"]
                session["next_item_index"] += 1
            else:
                idx = None
            await asyncio.to_thread(
                store.append_item_rows, session_id, now, members, name, idx, item_type, contributor
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
            reply, err = await record_items(self.bot, self.payload, item_type="分潤")
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
                payload = [n for n in parsed.get("data", []) if isinstance(n, str) and n.strip()]

                if kind == "unknown" or not payload:
                    await message.channel.send("⚠️ 無法判斷這張圖片是隊員名單還是寶物記錄，或內容為空。")
                    continue

                view = ConfirmView(self.bot, kind, payload, message.author.id, message.guild)
                sent = await message.channel.send(view.preview_text(), view=view)
                view.message = sent

            except json.JSONDecodeError:
                await message.channel.send("❌ Gemini 回傳的內容不是有效的 JSON，辨識失敗。")
            except Exception as e:
                await message.channel.send(f"❌ 辨識失敗，錯誤原因：{e}")

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

        reply, err = await record_items(self.bot, [item_name], item_type=item_type)
        await ctx.send(err or reply)

    @commands.command(name="donate")
    async def donate_item(self, ctx, item_name: str, *, contributor: str = None):
        """
        記錄捐獻的寶物（不屬於任何場次，不能分潤，只能是公會或自用）。
        用法：!donate 屠龍刀 牡羊
        """
        reply, err = await record_items(
            self.bot, [item_name], item_type="公會", contributor=contributor, force_no_session=True
        )
        await ctx.send(err or reply)

    @commands.command(name="sell")
    async def sell_item(self, ctx, *args):
        """
        把場次裡指定編號的寶物標記為已賣出。
        用法：!sell 編號 金額            → 對目前進行中的場次
             !sell 場次ID 編號 金額     → 對指定場次（捐獻物件沒有場次ID，用 0 或省略）
        """
        if len(args) == 2:
            if not self.bot.active_session:
                await ctx.send("⚠️ 目前沒有進行中的場次，請用 `!sell 場次ID 編號 金額`。")
                return
            session_id = self.bot.active_session["id"]
            index_raw, amount_raw = args
        elif len(args) == 3:
            session_id, index_raw, amount_raw = args
        else:
            await ctx.send("⚠️ 用法：`!sell 編號 金額` 或 `!sell 場次ID 編號 金額`")
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

    @commands.command(name="guildfund")
    async def guild_fund(self, ctx):
        """查看公會基金總額。"""
        total = await asyncio.to_thread(self.store.guild_fund_total)
        await ctx.send(f"🏦 公會基金總額：**{total:.2f}**")


async def setup(bot):
    await bot.add_cog(Sessions(bot))
