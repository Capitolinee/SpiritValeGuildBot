"""跟 Discord API 互動的顯示用輔助函式（不是純資料儲存，所以獨立於 store.py）。"""
import discord


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
