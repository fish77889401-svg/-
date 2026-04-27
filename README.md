# 任務統計 Line Bot

## 成員指令
| 輸入 | 功能 |
|------|------|
| 達標 | 本週任務打卡 (+10分) |
| 排行榜 | 查看本月排名 |
| 我的分數 | 查看自己分數 |
| 本週任務 | 查看當前任務內容 |
| 說明 | 顯示所有指令 |

## 管理員指令
| 輸入 | 功能 |
|------|------|
| /設管理員 | 設定自己為管理員（第一次使用）|
| /任務 [內容] | 設定本週任務 |
| /獎勵 [名稱] [分數] | 給予成員額外加分 |
| /下一週 | 推進到下一週 |
| /下一月 | 月份結算並進入下一月 |
| /月結算 | 顯示本月完整結算 |

## 部署步驟（Render）

1. 將這個資料夾上傳到 GitHub（新建 repository）
2. 到 render.com → New → Web Service
3. 連結你的 GitHub repository
4. 設定如下：
   - Environment: Python
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn main:app`
5. 部署完成後取得網址（例如 https://linebot-xxx.onrender.com）
6. 將網址填入 LINE Official Account Manager 的 Webhook 網址：
   https://linebot-xxx.onrender.com/callback
7. 開啟 Webhook

## 注意事項
- Bot 加入群組後，第一個傳訊息的人會自動被記錄為成員
- 群主需先輸入「/設管理員」來啟用管理員權限
- 分數資料儲存在 data.json，Render 免費版重啟後資料會消失（可升級或改用資料庫）
