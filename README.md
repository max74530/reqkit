# ReqKit

> 零用金與申購單線上管理系統 — 申請、審核、統計、年度封存，一站搞定。

以姓名取代帳號密碼，讓同仁點一下就能填單；管理者多一層密碼，把關會計審核與人員權限。

---

## 功能特色

### 權控

|角色 | 進入方式 | 可用範圍 |
|------|----------|----------|
| **user** 一般使用者 | 輸入姓名，免密碼 | 零用金申請、申購單申請（僅自己的資料）|
| **admin** 管理員 | 姓名 + 密碼 | 全部功能 |

兩個身分看到的是**各自獨立的介面**，不是同一頁藏按鈕 — 後端 API 也會擋，繞不过前端。

### 三大模組

- **零用金申請端** — 日期、活動簡述、申請項目（可複選）、金額、請領人、備註
- **會計端審核** — 發票取得日期、金額確認、支付日期、支付方式、審核狀態
- **申購單申請** — 數量、金額、主管核准勾��、採購勾選、完成日期

### 管理功能

- **統計分析** — 每月申請項目統計、每個人申請統計、近12 個月趨勢
- **人員管理** — 新增／停用／改角色，附管理操作記錄
- **月份篩選** — 畫面預設只顯示當月，可切換其他年月
- **年度明細檔** — 每年封存一份快照，歷史資料隨時查
- **自動備份** — 每月最後一天自動備份 + 封存年度明細

---

## 技術棧

| 項目 | 採用 |
|------|------|
| 後端 | Python 3.9+ / Flask |
| 資料庫 | SQLite（單一檔案，零設定）|
| 前端 | 原生 HTML + Tailwind CSS（無建置步驟）|
| 部署 | systemd / 任意 Python 主機 |

沒有前端建置工具、沒有 ORM、沒有外部服務依賴。`git clone` 之後就能跑。

---

## 快速開始

### 需求

- Python 3.9 以上
- ��不依賴 pip 以外的系統套件

### 安裝

```bash
git clone <此倉庫網址>
cd reqkit

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 設定管理員密碼（必要）

首次啟動必須設定管理員密碼：

```bash
export REQKIT_ADMIN_PASSWORD='請改成你自己的密碼'
```

Windows PowerShell：

```powershell
$env:REQKIT_ADMIN_PASSWORD = "請改成你自己的密碼"
```

或複製 `.env.example` 並依內容設定環境變數。

> 密碼至少 4 碼。**請勿沿用範例值。**

### 啟動

```bash
python app.py
```

開啟瀏覽器進入：

```
http://127.0.0.1:2000
```

---

## 環境變數

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `REQKIT_ADMIN_PASSWORD` | *(無)* | **首次啟動必填**���管理員密碼 |
| `REQKIT_ADMIN_USER` | `admin` | 管理員帳號 |
| `REQKIT_ADMIN_NAME` | `Admin` | 管理員顯示名稱 |
| `REQKIT_DATA_DIR` | `./data` | 資料根目錄（資料庫 + 備份）|
| `REQKIT_DB_DIR` | `./data` | SQLite 檔案所在目錄 |
| `REQKIT_ARCHIVE_DIR` | `./data/archive` | 年度封存與備份檔目錄 |
| `REQKIT_SECRET_KEY` | 自動產生並保存 | Token 簽章金鑰 |
| `REQKIT_TOKEN_HOURS` | `12` | 登入有效���數 |
| `PORT` | `2000` | 服務埠號 |

---

## 目錄結構

```
reqkit/
├── app.py                 # Flask 主程式：API、權限、統計、人員管理
├── archive.py             # 年度封存與自動備份
├── requirements.txt
├── .env.example
├── static/
│   └── index.html         # 單頁前端（含兩套權限介面）
└── data/                  # 執行時自動產生（已 gitignore）
    ├── reqkit.db
    ├── .secret_key
    └── archive/
```

---

## 權限模型

| 動作 | user | admin |
|------|:----:|:-----:|
| 建立／編輯／刪除**自己的**零用金、申購單 | ✅ | ✅ |
| 查看**他人**的申請單 | ❌ | ✅ |
| 會計端審核（發票日、確認金額、支付日、方式、狀態） | ❌ | ✅ |
| 申購單「主管核准」勾選 | ❌ | ✅ |
| 申購單「已訂購」勾選與完成日期 | ❌ | ✅ |
| 管理申請項目類別 | ❌ | ✅ |
| 人員增減與權限調整 | ❌ | ✅ |
| 統計分析 | ❌ | ✅ |
| CSV 匯出、備份與還原 | ❌ | ✅ |
| 年度封存 | ❌ | ✅ |

預設管理員帳號受保護：不可刪除、不可停用、不可降級、不可改名。

---

## 備份與年度封存

### 手動

進入系統後按右上角 **備份還原**：

- **立即備份** — 產生 SQLite 線上安全快照 + JSON
- **執行月底作業** — 手動立即執行備份並封存本年度
- **年度明細檔** — 輸入年份封存，產生該年度 JSON 與年度資料表

### 自動（每月最後一天）

建立排程腳本：

```bash
#!/bin/bash
# monthly_job.sh
export REQKIT_DB_DIR=/path/to/data
export REQKIT_ARCHIVE_DIR=/path/to/data/archive
cd /path/to/reqkit
python3 -c "import archive,os; archive.auto_close_if_month_end(os.environ['REQKIT_DB_DIR']+'/reqkit.db')"
```

搭配 cron：

```cron
0 2 * * * /path/to/monthly_job.sh >> /path/to/monthly.log 2>&1
```

腳本會自行判斷當天是否為當月最後一天，不是就略過。

### 保留策略

自動備份檔案保留最近 24 份，舊檔自動清除。年度明細檔永久保留。

---

## 部署注意事項

1. **請勿直接暴露於公網** — 本系統採「姓名 + 密碼」輕量驗證，建議部署於內網或家���網路。
2. **對外服務請加反向代理與 HTTPS**（nginx / Caddy）。
3. **保護資料目錄** — `data/` 內含營運資料與 Token 簽章金鑰，權限設為僅擁有者可讀寫。
4. **定期備份** — 資料庫是單一檔案，複製即可備份（服務運行中請用系統的備份功能，確保一致性）。
5. **更改預設密碼** — 首次啟動後立即修改管理員密碼。

### systemd 範例

```ini
[Unit]
Description=ReqKit
After=network.target

[Service]
Type=simple
Environment=REQKIT_ADMIN_PASSWORD=你的密碼
Environment=REQKIT_DATA_DIR=/var/lib/reqkit
WorkingDirectory=/opt/reqkit
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 常見問題

**Q：忘記管理員密碼怎麼辦？**
停用服務後刪除 `data/reqkit.db` 會連資料一起消失。建議改用資料庫工具重設：

```bash
sqlite3 data/reqkit.db "UPDATE users SET role='user' WHERE username='admin'"
```

即可暫時降級為一般使用者登入，再重新指定一位管理員。

**Q：一般使用者第一次輸入姓名會怎樣？**
系統自動將其加入人員名單（權限為 `user`），並記入人員管理。

**Q：年度封存會刪除主資料嗎？**
不會。封存只是產生年度快照，主資料仍完整保留、可編輯。重新封存同一年度會覆寫該年度檔。

---

## 授權

MIT License — 詳見 [LICENSE](LICENSE)

---

## 免責聲明

本系統為內部作業管理工具，用於零用金與採購單據的申請與審核流程。
使用者須自行確保資料備份與權限設定符合所屬組織的資安規範。