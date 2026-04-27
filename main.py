import os
import json
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent, MemberJoinedEvent
)
from datetime import datetime

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "667b16a4820dd8e65d4caa00b80210f9")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "mx7Oz6AD9+iCpY4RoQ6nFPE795eETLgxRfi6vdZFGa6ymsqKc6EvTkaqeX7kTg1PIsy2c0Wvmzabtb0weS7Je+5kijz/bqAJUxLgGC97+HZ4lBhSgzc2HLu/BtQdWzcCoiDwtXzWuYN/8Zt42qnyxgdB04t89/1O/w1cDnyilFU=")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ── 資料儲存（儲存在 data.json）──────────────────────────
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "members": {},       # user_id -> { name, scores: {月: 週: 分} }
        "current_week": 1,
        "current_month": 1,
        "current_task": "（尚未設定任務）",
        "admins": []         # 群主/管理員 user_id 清單
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_score(data, uid):
    month = str(data["current_month"])
    member = data["members"].get(uid, {})
    scores = member.get("scores", {}).get(month, {})
    return sum(scores.values())

def add_score(data, uid, pts, week=None):
    month = str(data["current_month"])
    week_key = str(week or data["current_week"])
    if uid not in data["members"]:
        return
    if month not in data["members"][uid]["scores"]:
        data["members"][uid]["scores"][month] = {}
    prev = data["members"][uid]["scores"][month].get(week_key, 0)
    data["members"][uid]["scores"][month][week_key] = prev + pts

# ── Webhook 進入點 ────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ── Bot 加入群組 ──────────────────────────────────────────
@handler.add(JoinEvent)
def handle_join(event):
    data = load_data()
    month = data["current_month"]
    week = data["current_week"]
    task = data["current_task"]
    msg = (
        f"大家好！我是任務統計機器人 🤖\n\n"
        f"目前是第 {month} 月 第 {week} 週\n"
        f"本週任務：{task}\n\n"
        f"完成任務請回覆：達標\n"
        f"查看排行請輸入：排行榜\n\n"
        f"群主指令：\n"
        f"  /任務 [內容] — 設定本週任務\n"
        f"  /獎勵 @成員 [分數] — 給予額外加分\n"
        f"  /下一週 — 推進到下一週\n"
        f"  /下一月 — 推進到下一個月\n"
        f"  /設管理員 — 設定自己為管理員"
    )
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )

# ── 新成員加入 ────────────────────────────────────────────
@handler.add(MemberJoinedEvent)
def handle_member_join(event):
    data = load_data()
    for member in event.joined.members:
        uid = member.profile.user_id if hasattr(member, 'profile') else member.user_id
        display_name = member.display_name if hasattr(member, 'display_name') else "新成員"
        if uid not in data["members"]:
            data["members"][uid] = {"name": display_name, "scores": {}}
    save_data(data)

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        names = []
        for member in event.joined.members:
            n = member.display_name if hasattr(member, 'display_name') else "新成員"
            names.append(n)
        name_str = "、".join(names)
        task = data["current_task"]
        api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(
                    text=f"歡迎 {name_str} 加入！👋\n本週任務：{task}\n完成後請輸入「達標」即可記錄分數！"
                )]
            )
        )

# ── 訊息處理 ──────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    data = load_data()
    uid = event.source.user_id
    text = event.message.text.strip()
    group_id = getattr(event.source, "group_id", None)

    # 自動記錄傳訊者
    if uid not in data["members"]:
        data["members"][uid] = {"name": "成員", "scores": {}}

    # 嘗試取得名稱（群組內無法主動取得，先用 uid 後幾碼）
    if data["members"][uid]["name"] == "成員":
        data["members"][uid]["name"] = f"成員_{uid[-4:]}"

    is_admin = uid in data["admins"]
    reply = None

    # ── 成員指令 ──────────────────────────────────────────
    if text == "達標":
        week_key = str(data["current_week"])
        month_key = str(data["current_month"])
        already = data["members"][uid]["scores"].get(month_key, {}).get(week_key, 0)
        if already > 0:
            reply = f"你本週已經記錄過達標了！\n目前本月累計：{get_score(data, uid)} 分"
        else:
            add_score(data, uid, 10)
            save_data(data)
            reply = f"✅ 達標記錄成功！本週 +10 分\n本月累計：{get_score(data, uid)} 分"

    elif text == "排行榜" or text == "/排行":
        month = data["current_month"]
        ranking = []
        for mid, m in data["members"].items():
            s = get_score(data, mid)
            ranking.append((m["name"], s))
        ranking.sort(key=lambda x: -x[1])
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"📊 第 {month} 月排行榜\n"]
        for i, (name, score) in enumerate(ranking):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {name}：{score} 分")
        reply = "\n".join(lines)

    elif text == "我的分數" or text == "/我的分數":
        score = get_score(data, uid)
        reply = f"你本月累計：{score} 分（第 {data['current_month']} 月 第 {data['current_week']} 週）"

    elif text == "本週任務" or text == "/任務":
        reply = f"📋 本週任務（第{data['current_week']}週）：\n{data['current_task']}"

    # ── 管理員指令 ────────────────────────────────────────
    elif text == "/設管理員":
        if uid not in data["admins"]:
            data["admins"].append(uid)
            save_data(data)
        reply = "✅ 已設定為管理員！你現在可以使用群主指令。"

    elif text.startswith("/任務 ") and is_admin:
        task = text[4:].strip()
        data["current_task"] = task
        save_data(data)
        reply = f"✅ 本週任務已更新：\n{task}\n\n請大家完成後輸入「達標」登記！"

    elif text.startswith("/獎勵") and is_admin:
        # 格式：/獎勵 名稱 分數
        parts = text.split()
        if len(parts) >= 3:
            target_name = parts[1].replace("@", "")
            try:
                pts = int(parts[2])
                target_uid = None
                for mid, m in data["members"].items():
                    if target_name in m["name"]:
                        target_uid = mid
                        break
                if target_uid:
                    add_score(data, target_uid, pts)
                    save_data(data)
                    reply = f"🎁 已給予 {data['members'][target_uid]['name']} 獎勵 +{pts} 分！"
                else:
                    reply = f"找不到「{target_name}」，請確認名稱是否正確。"
            except ValueError:
                reply = "格式錯誤，請用：/獎勵 名稱 分數"
        else:
            reply = "格式：/獎勵 名稱 分數"

    elif text == "/下一週" and is_admin:
        data["current_week"] += 1
        save_data(data)
        reply = f"📅 已推進到第 {data['current_month']} 月 第 {data['current_week']} 週！\n請記得用 /任務 設定新的本週任務。"

    elif text == "/下一月" and is_admin:
        data["current_month"] += 1
        data["current_week"] = 1
        save_data(data)
        reply = f"🗓️ 已進入第 {data['current_month']} 月！\n第 {data['current_month']-1} 月結算完成。\n請用 /任務 設定新任務。"

    elif text == "/月結算" and is_admin:
        month = data["current_month"]
        ranking = []
        for mid, m in data["members"].items():
            s = get_score(data, mid)
            ranking.append((m["name"], s))
        ranking.sort(key=lambda x: -x[1])
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 第 {month} 月最終結算\n"]
        for i, (name, score) in enumerate(ranking):
            medal = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{medal} {name}：{score} 分")
        lines.append("\n恭喜所有人辛苦了！🎉")
        reply = "\n".join(lines)

    elif text == "/說明" or text == "說明":
        reply = (
            "📖 指令說明\n\n"
            "【所有成員】\n"
            "達標 — 本週任務打卡 (+10分)\n"
            "排行榜 — 查看本月排名\n"
            "我的分數 — 查看自己分數\n"
            "本週任務 — 查看當前任務\n\n"
            "【管理員】\n"
            "/任務 [內容] — 設定本週任務\n"
            "/獎勵 [名稱] [分數] — 額外加分\n"
            "/下一週 — 推進週次\n"
            "/下一月 — 月份結算並推進\n"
            "/月結算 — 顯示本月結算\n"
            "/設管理員 — 設定自己為管理員"
        )

    if reply:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply)]
                )
            )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
