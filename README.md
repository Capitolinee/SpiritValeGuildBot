# 檔案結構

```
bot.py                  # 進入點：啟動健康檢查伺服器、載入 cogs、啟動機器人
store.py                # Google Sheets 儲存層，所有 gspread 操作都在這裡
helpers.py              # Discord 顯示名稱解析（跟 Sheets 無關，獨立出來）
cogs/
  jobs.py               # !addjob !deljob !jobs
  profiles.py           # !profile !profiles !myprofiles !delprofile
  sessions.py           # 圖片辨識、!item !donate !sell !sessioninfo !claim !pending !unclaimed !guildfund
  access_control.py     # !setthreadrules 等討論串/論壇指令限制
requirements.txt
```

# 環境變數（跟之前一樣）

- `GEMINI_API_KEY`
- `DISCORD_TOKEN`
- `GOOGLE_SHEET_ID`（試算表網址 `/d/` 和 `/edit` 中間那段）
- `GOOGLE_SERVICE_ACCOUNT_B64`（服務帳號 JSON 金鑰的 base64）

不再需要 `GITHUB_*` 系列環境變數。

# 部署到 Render

Start Command 改成：
```
python bot.py
```
（不是 `python bot.py` 裡面直接 `bot.run(...)`，這次入口是 `asyncio.run(main())`，行為一樣，指令不變。）

其餘（Server Members Intent、applications.commands OAuth2 scope）都跟之前教過的一樣，沒有變化。

# 跟舊版（GitHub JSON）的行為差異

1. **場次不再是持久化物件**，只存在 `bot.active_session` 記憶體變數（全域唯一，不分頻道）。機器人重啟後「目前進行中的場次」會變成沒有，需要重新上傳隊員圖片才能繼續記錄寶物——但**已經寫進表格的資料不會遺失**。
2. **寶物的類型（分潤/公會/自用）在記錄當下就要決定**，不再是賣掉時才選：
   - `!item 寶物名稱`：預設「分潤」，需要有進行中的場次。
   - `!item 寶物名稱 公會` / `!item 寶物名稱 自用`：指定類型，此時只會寫一列（不分人）。
   - `!donate 寶物名稱 貢獻者`：捐獻的寶物，不屬於任何場次，固定是「公會」類型。
3. 拿掉了舊版的 `!dedupemembers`、`!syncmembers`、全域寶物清單（`!additem`/`!itemlist`/`!items`），這些概念在新表格設計下不再需要。
4. `!clearmembers`/`!clearitems`/`!clearall` 這幾個「清空」指令也先拿掉了，Google Sheets 沒有實作對應的安全清空方式；需要的話之後再加。
5. 新增 `!guildfund` 查看公會基金總額。

# 已知限制

- 如果一場活動全程沒有任何寶物被記錄成「分潤」類型，出席者的「出席次數」不會增加（因為出席次數是從場次記錄的分潤列反推的）。如果你們常常有「純練習、沒有掉寶」的場次也想算出席，這裡需要再討論怎麼設計。
- Google Sheets API 有呼叫頻率限制（預設每分鐘約 60 次寫入），正常公會使用量不會碰到，但如果短時間內大量操作（例如一次貼很多筆資料）可能會被限流，屆時 gspread 會拋出例外，目前程式沒有做重試機制。
