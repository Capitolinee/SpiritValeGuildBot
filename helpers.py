"""跟 Discord API 互動的顯示用輔助函式（不是純資料儲存，所以獨立於 store.py）。"""
import asyncio
from datetime import datetime, timezone, timedelta

import discord

# 台灣時間（UTC+8）。Render 伺服器跑在 UTC，不轉的話記錄時間會比實際早 8 小時。
TW_TZ = timezone(timedelta(hours=8))


def now_str() -> str:
    """統一的時間格式：yyyy/mm/dd HH:MM:SS（台灣時間）。所有跟時間有關的紀錄都用這個。"""
    return datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S")


async def resolve_display_name(discord_id: str, fallback_name: str, guild: discord.Guild = None) -> str:
    """
    優先顯示 Discord 顯示名稱；這個 ID 在伺服器裡找不到時，顯示「角色名稱（⚠️ 帳號對不上）」。
    """
    if not discord_id:
        return fallback_name or "未知"
    if not guild:
        return fallback_name or f"（使用者 {discord_id}）"

    member = guild.get_member(int(discord_id))
    if member:
        return member.display_name

    try:
        member = await guild.fetch_member(int(discord_id))
        return member.display_name
    except discord.NotFound:
        # 不一定是真的離開了：也可能是用分身帳號登記、或 ID 被改壞。顯示角色名稱比一串 ID 好認，
        # 原因可以用 /checkprofiles 查，用 /fixprofile 修。
        return f"{fallback_name}（⚠️ 帳號對不上）" if fallback_name else f"（⚠️ 帳號對不上 {discord_id}）"
    except Exception:
        return fallback_name or f"（使用者 {discord_id}）"


def sort_warning(sort_result) -> str:
    """角色資料排序沒通過檢查（已自動還原）時，要附在回覆裡的提醒；排序正常或沒有排序就回空字串。"""
    if not sort_result or sort_result.get("ok"):
        return ""
    return (f"\n\n⚠️ 角色資料排序時檢查沒有通過，已經自動還原成排序前的樣子，資料沒有遺失。"
            f"\n原因：{sort_result.get('reason')}")


# ---------- 延後排序角色資料 ----------
# 登記、刪除角色之後不馬上排序，而是「預約」在 SORT_DELAY 秒後排；這段時間內又有人登記，就再往後延。
# 等大家都登記完、安靜下來才排一次：30 個人同時登記只排 1 次，不會撞到 Google 試算表的速度限制。
# 正在排序時又有人登記，不會中斷正在進行的排序（中斷在一半最危險），而是等這次排完再補排一次。

SORT_DELAY = 60  # 秒


def schedule_character_sort(bot):
    """預約排序角色資料。可以連續呼叫很多次，只會在最後一次呼叫的 SORT_DELAY 秒後排一次。"""
    loop = asyncio.get_running_loop()
    bot._sort_due = loop.time() + SORT_DELAY
    task = getattr(bot, "_sort_task", None)
    if task is None or task.done():
        bot._sort_task = loop.create_task(_character_sort_worker(bot))


async def _character_sort_worker(bot):
    import audit  # 放在這裡，避免 helpers 跟 audit 互相引用
    loop = asyncio.get_running_loop()
    while True:
        while (wait := bot._sort_due - loop.time()) > 0:
            await asyncio.sleep(wait)
        started = loop.time()
        try:
            async with bot.store.lock:
                result = await asyncio.to_thread(bot.store.sort_characters)
            if result.get("ok"):
                audit.system(f"延後排序角色資料完成（{result.get('rows', 0)} 列）")
            else:
                audit.system(f"⚠️ 延後排序角色資料沒有通過檢查，已經自動還原：{result.get('reason')}")
        except Exception as e:
            audit.error("延後排序角色資料失敗（資料有備份分頁，請看錯誤內容）", e)
        if bot._sort_due <= started:
            return  # 排序期間沒有人再登記，結束
        # 排序期間又有人登記或刪除，照新的時間再排一次
