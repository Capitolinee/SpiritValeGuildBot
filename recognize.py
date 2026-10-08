"""
圖片辨識：依序試好幾個 Gemini 模型，主要的失敗了就自動換備用的。

順序（可以用 Railway 的環境變數 GEMINI_MODELS 改，逗號分隔）：
  gemini-3.6-flash → gemini-3.5-flash-lite → gemini-3.1-flash-lite → gemini-3.8-flash
每個模型的免費額度是分開算的，主要模型失敗了，換下一個不會扣到同一份額度。

失敗的請求也會扣每日額度（Google AI Studio 的用量圖看得到），所以已經知道會失敗的模型會「先跳過一段時間」：
  503 伺服器忙碌 → 2 分鐘；429 每分鐘次數 → 1 分鐘（或 Google 說的秒數）；429 免費容量不足 → 5 分鐘；
  429 每日額度用完 → 到太平洋時間午夜（台灣 15:00／16:00）；404 模型不存在 → 6 小時；500／502／504 → 1 分鐘
不換模型、直接停下來的：401／403（金鑰有問題，換模型也一樣）、主要模型就回 400（圖片本身有問題）。

使用者只會看到「成功」或「失敗、原因、該怎麼辦」；用了哪個模型、每個模型為什麼失敗，寫進記錄。
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")      # 夏令／冬令自動切換
except Exception:                                    # 沒有時區資料的環境：用冬令時間，最多晚一小時恢復
    PACIFIC = timezone(timedelta(hours=-8))

TW = timezone(timedelta(hours=8))
DEFAULT_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]


def models_from_env(value: str = None) -> list:
    """GEMINI_MODELS 環境變數：逗號分隔，空白會被忽略；沒設定就用預設順序。"""
    models = [m.strip() for m in (value or "").split(",") if m.strip()]
    return list(dict.fromkeys(models)) or list(DEFAULT_MODELS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def next_quota_reset(now: datetime = None) -> datetime:
    """Google 每日額度的重置時間：太平洋時間的下一個午夜。"""
    p = (now or utcnow()).astimezone(PACIFIC)
    d = (p + timedelta(days=1)).date()
    return datetime(d.year, d.month, d.day, tzinfo=PACIFIC)


def tw_hm(dt: datetime) -> str:
    return dt.astimezone(TW).strftime("%H:%M")


def classify(e: Exception) -> dict:
    """
    把 Gemini 的錯誤分類。回傳：
      kind：分類；reason：寫進記錄的中文原因；cooldown：先跳過幾秒（"day" 是到重置為止，None 是不跳過）；
      stop：要不要直接停下來、不換模型
    """
    text = str(e)
    code = getattr(e, "code", None)
    if not isinstance(code, int):
        m = re.match(r"\s*(\d{3})\b", text)
        code = int(m.group(1)) if m else None
    if code == 429:
        if re.search(r"PerDay|per ?day", text, re.I):
            return {"kind": "day", "reason": "今天的額度用完了", "cooldown": "day", "stop": False}
        if re.search(r"PerMinute|per ?minute", text, re.I):
            m = re.search(r"retry in ([\d.]+)s", text)
            wait = max(60, int(float(m.group(1))) + 1) if m else 60
            return {"kind": "minute", "reason": "一分鐘內用太多次", "cooldown": wait, "stop": False}
        return {"kind": "capacity", "reason": "Google 免費容量暫時不足", "cooldown": 300, "stop": False}
    if code == 503:
        return {"kind": "busy", "reason": "Google 伺服器忙碌（503）", "cooldown": 120, "stop": False}
    if code in (500, 502, 504):
        return {"kind": "server", "reason": f"Google 內部錯誤或逾時（{code}）", "cooldown": 60, "stop": False}
    if code == 404:
        return {"kind": "not_found", "reason": "模型名稱不存在或已停用（404）", "cooldown": 6 * 3600, "stop": False}
    if code in (401, 403):
        return {"kind": "auth", "reason": f"API 金鑰無效或沒有權限（{code}）", "cooldown": None, "stop": True}
    if code == 400:
        return {"kind": "bad_request", "reason": "圖片無法處理（400）", "cooldown": None, "stop": False}
    if code is None:
        return {"kind": "network", "reason": f"連不到 Google（{type(e).__name__}）", "cooldown": 30, "stop": False}
    return {"kind": "other", "reason": f"其他錯誤（{code}）", "cooldown": 60, "stop": False}


class RecognitionFailed(Exception):
    """所有模型都失敗了（或遇到不該換模型的錯誤）。user_message 給使用者看；log_detail 寫進記錄。"""

    def __init__(self, user_message: str, log_detail: str):
        super().__init__(user_message)
        self.user_message, self.log_detail = user_message, log_detail


class Recognizer:
    def __init__(self, client, models: list):
        self.client, self.models = client, list(models)
        self.skip_until = {}          # 模型 → 這個時間之前先跳過（UTC）

    def _available(self, now: datetime) -> list:
        return [m for m in self.models if self.skip_until.get(m, now) <= now]

    def _cool(self, model: str, cooldown, now: datetime):
        if cooldown == "day":
            self.skip_until[model] = next_quota_reset(now)
        elif cooldown:
            self.skip_until[model] = now + timedelta(seconds=cooldown)

    async def recognize(self, image_b64: str, mime_type: str, prompt: str, parse, now: datetime = None):
        """回傳 (parse 之後的結果, 用了哪個模型, 前面失敗的 [(模型, 原因)])；全部失敗就丟 RecognitionFailed。"""
        now = now or utcnow()
        tried, kinds = [], []
        candidates = self._available(now)
        if not candidates:
            soonest = min(self.skip_until.values())
            raise RecognitionFailed(
                f"⏳ 圖片辨識暫時無法使用（Google 那邊剛剛一直失敗），請在台灣時間 **{tw_hm(soonest)}** 之後再上傳。\n"
                f"不急的話，也可以改用 `/startsession` 手動開場、`/item` 手動記錄寶物。",
                "所有模型都在暫停中：" + "、".join(f"{m} 到 {tw_hm(t)}" for m, t in self.skip_until.items()))
        for i, model in enumerate(candidates):
            try:
                result = await asyncio.to_thread(
                    self.client.interactions.create, model=model,
                    input=[{"type": "text", "text": prompt}, {"type": "image", "data": image_b64, "mime_type": mime_type}])
                return parse(result.output_text), model, tried
            except json.JSONDecodeError:
                tried.append((model, "回傳的內容格式不正確"))
                kinds.append("format")
                continue
            except Exception as e:
                c = classify(e)
                tried.append((model, f"{c['reason']}：{str(e)[:200]}"))
                kinds.append(c["kind"])
                self._cool(model, c["cooldown"], now)
                if c["stop"]:
                    raise RecognitionFailed("❌ 圖片辨識的設定有問題（Google 的 API 金鑰無效或沒有權限），請通知管理員檢查。",
                                            _detail(tried))
                if c["kind"] == "bad_request" and i == 0:
                    raise RecognitionFailed("⚠️ 這張圖片沒辦法辨識（格式不支援或檔案有問題），請重新截一張圖再上傳。",
                                            _detail(tried))
        raise RecognitionFailed(_user_message(kinds, self.skip_until, now), _detail(tried))


def _detail(tried: list) -> str:
    return "｜".join(f"{m}：{r}" for m, r in tried)


def _user_message(kinds: list, skip_until: dict, now: datetime) -> str:
    fallback = "\n這段時間可以改用 `/startsession` 手動開場、`/item` 手動記錄寶物。"
    if kinds and all(k == "day" for k in kinds):
        return f"⛔ 今天的圖片辨識額度都用完了，台灣時間 **{tw_hm(next_quota_reset(now))}** 重置。" + fallback
    if kinds and all(k in ("busy", "capacity", "server", "minute", "network") for k in kinds):
        times = [t for t in skip_until.values() if t > now]
        when = f"，大約台灣時間 **{tw_hm(min(times))}** 之後再上傳" if times else "，請過幾分鐘再上傳"
        return f"⏳ Google 的圖片辨識現在很忙，暫時無法使用{when}。" + fallback
    if kinds and all(k == "format" for k in kinds):
        return "⚠️ 這張圖片辨識不出內容，請確認截到的是隊員名單或寶物畫面，再重新上傳。"
    return "❌ 這次圖片辨識失敗了，請過幾分鐘再上傳。" + fallback
