# 遊戲紀錄機器人指令說明

這支機器人可以自動辨識遊戲截圖（隊員名單、掉落寶物），並記錄出席、寶物、分潤、角色資料等資訊，所有資料都存在 GitHub 上的 JSON 檔案，不會因為機器人重啟而遺失。

---

## 📷 圖片辨識（不用打指令）

直接把截圖上傳到頻道，機器人會自動判斷圖片是「隊員名單」還是「寶物掉落」，辨識完會顯示確認畫面：

- **✅ 確認正確**：直接照辨識結果寫入記錄
- **✏️ 修改後再存**：跳出可編輯的文字框，改完送出才寫入
  - 隊員格式：每行一個名字
  - 寶物格式：每行 `名稱,數量`（例如 `神秘藥水,3`）

只有上傳圖片的人可以按這些按鈕，5 分鐘沒人操作會自動失效。

**上傳隊員名單並確認後，會自動開啟一個「場次」**，之後記錄的寶物會掛在這個場次底下，供之後分潤結算使用。

---

## 👥 隊員記錄

| 指令 | 說明 | 範例 |
|---|---|---|
| `!members` | 查詢目前累計的隊員名單（含出席次數） | `!members` |
| `!memberlist` | 列出隊員記錄，含編號（供修改/刪除用） | `!memberlist` |
| `!editmember 編號 新名字` | 修改指定編號的隊員名字；若新名字跟其他記錄重複會自動合併次數 | `!editmember 3 小丫頭` |
| `!delmember 編號` | 刪除指定編號的隊員記錄 | `!delmember 3` |
| `!addmember 名字` | 手動新增一筆隊員記錄（同名會疊加次數，不會重複建立） | `!addmember 戰士A` |
| `!dedupemembers` | 掃描並自動合併「正規化後名字相同」的重複記錄（例如形似字元造成的重複） | `!dedupemembers` |
| `!syncmembers` | 依照目前登記的角色資料，把舊記錄重新對應到 Discord 帳號並合併次數 | `!syncmembers` |

> 隊員名字會自動比對 `!profile` 登記過的角色，能對應到的話會直接顯示 Discord 顯示名稱；換角色出席也會被視為同一人。

---

## 💎 寶物記錄（全域，不綁定場次）

| 指令 | 說明 | 範例 |
|---|---|---|
| `!items` | 查詢寶物累計總數（同名寶物自動加總） | `!items` |
| `!itemlist` | 列出寶物原始記錄，含編號 | `!itemlist` |
| `!additem 數量 名稱` | 手動新增一筆寶物記錄 | `!additem 3 神秘藥水` |
| `!edititem 編號 新數量 新名稱` | 修改指定編號的寶物 | `!edititem 2 5 屠龍刀` |
| `!delitem 編號` | 刪除指定編號的寶物記錄 | `!delitem 2` |

---

## 🎯 場次與分潤

一場「隊員出席」對應一個「場次」，場次裡的寶物賣掉後可以平均分給出席的隊員。

| 指令 | 說明 | 範例 |
|---|---|---|
| `!item 寶物名稱` | 把寶物加進目前頻道的預設場次 | `!item 屠龍刀` |
| `!item 場次ID 寶物名稱` | 把寶物加進指定場次（不是目前預設場次時使用） | `!item s1758012345 神秘藥水` |
| `!sessions` | 列出這個頻道所有場次（含已結束、含還沒賣完的舊場次） | `!sessions` |
| `!sessioninfo` | 查看目前預設場次的出席名單、寶物清單、賣出狀態 | `!sessioninfo` |
| `!sessioninfo 場次ID` | 查看指定場次的詳細資訊 | `!sessioninfo s1758012345` |
| `!sell 編號 金額` | 把預設場次裡指定編號的寶物標記賣出，平分給出席隊員 | `!sell 0 3000` |
| `!sell 場次ID 編號 金額` | 對指定場次的寶物結算 | `!sell s1758012345 0 3000` |
| `!closesession` | 結束目前預設場次（不影響已有資料，只是不再接受新寶物） | `!closesession` |
| `!closesession 場次ID` | 結束指定場次 | `!closesession s1758012345` |
| `!unclaimed` | 查看預設場次裡還有誰沒領錢 | `!unclaimed` |
| `!unclaimed 場次ID` | 查看指定場次還有誰沒領錢 | `!unclaimed s1758012345` |
| `!claim` / `/claim` | 領取自己的分潤：只有一場待領時直接領取，多場待領時跳選單讓你挑 | `!claim` |
| `!claim 場次ID` | 直接領取指定場次的分潤 | `!claim s1758012345` |
| `!pending` / `/pending` | 查看自己目前所有待領分潤的明細與總額 | `!pending` |

> `/claim`、`/pending` 用斜線指令打的話，回覆只有自己看得到；用 `!` 打的話回覆公開顯示。
>
> 同一個頻道可以同時存在多個場次，不會互相覆蓋——新場次不會影響舊場次還沒賣完的寶物，只是需要在指令後面加上場次 ID 才能操作非預設的場次。

---

## 🧑 角色資料（姓名 + 職業）

每個 Discord 帳號可以登記**多隻角色**（不同名字、不同職業）。

| 指令 | 說明 | 範例 |
|---|---|---|
| `!profile` | 開始設定/新增自己的角色資料（按鈕 → 輸入名字 → 選職業，可逐轉選到底） | `!profile` |
| `!profiles` | 列出所有人登記的角色資料 | `!profiles` |
| `!myprofiles` / `/myprofiles` | 列出自己名下所有角色，含編號 | `!myprofiles` |
| `!delprofile 編號` | 刪除自己名下指定編號的角色 | `!delprofile 0` |

---

## ⚔️ 職業設定（管理員用）

職業可以設定「轉職層級」與「承接自哪個職業」，形成一整棵職業樹。

| 指令 | 說明 | 範例 |
|---|---|---|
| `!addjob 職業名稱 tier=層級` | 新增/更新職業，tier 預設為 1 | `!addjob 戰士 tier=1` |
| `!addjob 職業名稱 tier=層級 parent=上一轉職業` | 新增第 2 轉以上的職業，需指定承接自哪個職業 | `!addjob 聖騎士 tier=2 parent=騎士` |
| `!addjob 職業名稱 tier=層級 parent=... image=圖片網址` | 額外附上職業圖片 | `!addjob 聖騎士 tier=2 parent=騎士 image=https://.../paladin.png` |
| `!deljob 職業名稱` | 刪除職業（底下還有更高轉職業掛著時會擋下來） | `!deljob 戰士` |
| `!jobs` | 顯示目前整棵職業樹（依轉職層級分組） | `!jobs` |

---

## 🗑️ 清除資料（需二次確認）

| 指令 | 說明 |
|---|---|
| `!clearmembers` | 清除所有隊員記錄 |
| `!clearitems` | 清除所有寶物記錄 |
| `!clearall` | 隊員與寶物記錄全部清空 |

打了指令後會跳出「🗑️ 確定清除」/「取消」兩個按鈕，只有下指令的人能操作，60 秒沒人按會自動失效。

---

## 🧵 討論串／論壇指令限制

可以針對特定討論串或整個論壇，限制只能使用哪些指令。規則管理指令本身不受限制。

| 指令 | 說明 | 範例 |
|---|---|---|
| `!setthreadrules 指令1,指令2,...` | 限制「這個討論串自己」只能用列出的指令（優先權最高） | `!setthreadrules profile,myprofiles` |
| `!clearthreadrules` | 解除這個討論串的專屬限制 | `!clearthreadrules` |
| `!threadrules` | 查看這個討論串目前實際套用的規則（含繼承自論壇的） | `!threadrules` |
| `!setforumrules 指令1,指令2,...` | 設定整個論壇的預設規則，底下沒專屬設定的討論串都會套用 | `!setforumrules item,sell,sessioninfo` |
| `!clearforumrules` | 解除整個論壇的預設限制 | `!clearforumrules` |
| `!forumrules` | 查看整個論壇的預設規則 | `!forumrules` |

> 沒有設定過規則的頻道／討論串完全不受影響，可以使用所有指令。

**範例情境**：三個討論串分別限制不同用途
```
# 第一個討論串：只能查詢自己的角色資料
!setthreadrules profile,myprofiles

# 第二個討論串：只能記錄隊員與寶物、結算分潤
!setthreadrules item,sell,addmember,members,memberlist,additem,items,itemlist

# 第三個討論串：開放查詢類指令
!setthreadrules jobs,sessions,sessioninfo,unclaimed,claim,pending
```

---

## 部署需求備忘

- **Server Members Intent**：需要在 Discord Developer Portal 開啟，否則機器人無法正確解析 Discord 顯示名稱。
- **applications.commands 權限**：`/claim`、`/pending`、`/myprofiles` 這些斜線指令需要邀請連結含這個 OAuth2 scope，機器人啟動時會自動同步。
- **環境變數**：`GEMINI_API_KEY`、`DISCORD_TOKEN`、`GITHUB_TOKEN`、`GITHUB_REPO`（格式 `帳號/repo名稱`）、`GITHUB_FILE_PATH`（預設 `data/records.json`）、`GITHUB_BRANCH`（預設 `main`）。
