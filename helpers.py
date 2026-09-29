"""跟 Discord API 互動的顯示用輔助函式（不是純資料儲存，所以獨立於 store.py）。"""
from datetime import datetime, timezone, timedelta

import discord

# 台灣時間（UTC+8）。Render 伺服器跑在 UTC，不轉的話記錄時間會比實際早 8 小時。
TW_TZ = timezone(timedelta(hours=8))


def now_str() -> str:
    """統一的時間格式：yyyy/mm/dd HH:MM:SS（台灣時間）。所有跟時間有關的紀錄都用這個。"""
    return datetime.now(TW_TZ).strftime("%Y/%m/%d %H:%M:%S")


async def resolve_display_name(discord_id: str, fallback_name: str, guild: discord.Guild = None) -> str:
    """
    優先顯示 Discord 顯示名稱；查不到（快取沒有、或真的已離開伺服器）就退回原始名字。
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
        return f"（已離開的使用者 {discord_id}）"
    except Exception:
        return fallback_name or f"（使用者 {discord_id}）"


def sort_warning(sort_result) -> str:
    """角色資料排序沒通過檢查（已自動還原）時，要附在回覆裡的提醒；排序正常或沒有排序就回空字串。"""
    if not sort_result or sort_result.get("ok"):
        return ""
    return (f"\n\n⚠️ 角色資料排序時檢查沒有通過，已經自動還原成排序前的樣子，資料沒有遺失。"
            f"\n原因：{sort_result.get('reason')}")
