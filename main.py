import os
import json
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent, TextMessageContent,
    JoinEvent, MemberJoinedEvent
)

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "667b16a4820dd8e65d4caa00b80210f9")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "mx7Oz6AD9+iCpY4RoQ6nFPE795eETLgxRfi6vdZFGa6ymsqKc6EvTkaqeX7kTg1PIsy2c0Wvmzabtb0weS7Je+5kijz/bqAJUxLgGC97+HZ4lBhSgzc2HLu/BtQdWzcCoiDwtXzWuYN/8Zt42qnyxgdB04t89/1O/w1cDnyilFU=")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"members": {}, "admins": [], "groups": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_group(data, gid):
    if gid not in data["groups"]:
        data["groups"][gid] = {
            "current_week": 1, "current_month": 1,
            "current_task": "（尚未設定任務）", "weekly_checkin_limit": 7
        }
    return data["groups"][gid]

def checkin_key(month, week):
    return f"{month}-{week}"

def get_checkins(data, uid, month, week):
    return data["members"].get(uid, {}).get("checkins", {}).get(checkin_key(month, week), 0)

def add_checkin(data, uid, month, week):
    key = checkin_key(month, week)
    data["members"][uid].setdefault("checkins", {})[key] = \
        data["members"][uid]["checkins"].get(key, 0) + 1

def get_monthly_score(data, uid, month):
    return sum(data["members"].get(uid, {}).get("scores", {}).get(str(month), {}).values())

def add_score(data, uid, month, week, pts):
    m, w = str(month), str(week)
    data["members"][uid].setdefault("scores", {}).setdefault(m, {})
    data["members"][uid]["scores"][m][w] = data["members"][uid]["scores"][m].get(w, 0) + pts

def find_member_by_name(data, name):
    for mid, m in data["members"].items():
        if name == m["name"]:
            return mid
    for mid, m in data["members"].items():
        if name in m["name"]:
            return mid
    return None

def calc_weekly_bonus(data, month, week):
    key = checkin_key(month, week)
    counts = {uid: m.get("checkins", {}).get(key, 0)
              for uid, m in data["members"].items()
              if m.get("checkins", {}).get(key, 0) > 0}
    if not counts:
        return {}
    sorted_vals = sorted(set(counts.values()), reverse=True)
    first = sorted_vals[0]
    second = sorted_vals[1] if len(sorted_vals) > 1 else None
    bonuses = {}
    for uid, c in counts.items():
        if c == first:
            bonuses[uid] = 2
        elif second and c == second:
            bonuses[uid] = 1
    return bonuses

def reply_msg(event, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=text)]))

def ensure_member(data, uid):
    if uid not in data["members"]:
        data["members"][uid] = {"name": f"成員_{uid[-4:]}", "scores": {}, "checkins": {}}

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(JoinEvent)
def handle_join(event):
    data = load_data()
    gid = getattr(event.source, "group_id", "default")
    g = get_group(data, gid)
    save_data(data)
    reply_msg(event,
        f"大家好！我是任務統計機器人 🤖\n\n"
        f"目前是第 {g['current_month']} 月 第 {g['current_week']} 週\n"
        f"本週任務：{g['current_task']}\n\n"
        f"【成員指令】\n"
        f"/我是 [名字] — 登記你的名稱（請先做！）\n"
        f"達標 — 今日打卡\n"
        f"排行榜 — 本月分數排行\n"
        f"週排行 — 本週打卡次數排行\n"
        f"我的分數 — 查看分數與打卡數\n"
        f"本週任務 — 查看當前任務\n"
        f"說明 — 顯示所有指令\n\n"
        f"【管理員設定】\n"
        f"群主請輸入「/設管理員」（只有尚無管理員時有效）")

@handler.add(MemberJoinedEvent)
def handle_member_join(event):
    data = load_data()
    gid = getattr(event.source, "group_id", "default")
    g = get_group(data, gid)
    for member in event.joined.members:
        ensure_member(data, member.user_id)
    save_data(data)
    reply_msg(event, f"歡迎新成員！👋\n請先輸入「/我是 你的名字」登記名稱\n本週任務：{g['current_task']}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    data = load_data()
    uid = event.source.user_id
    text = event.message.text.strip()
    gid = getattr(event.source, "group_id", "default")
    g = get_group(data, gid)
    ensure_member(data, uid)

    week = g["current_week"]
    month = g["current_month"]
    limit = g.get("weekly_checkin_limit", 7)
    is_admin = uid in data["admins"]
    rep = None

    # /我是
    if text.startswith("/我是 "):
        new_name = text[4:].strip()
        if not new_name:
            rep = "請輸入名字，例如：/我是 小明"
        elif len(new_name) > 10:
            rep = "名字太長，請用 10 字以內"
        else:
            existing = find_member_by_name(data, new_name)
            if existing and existing != uid:
                rep = f"「{new_name}」已被其他成員使用，請換一個名字"
            else:
                old = data["members"][uid]["name"]
                data["members"][uid]["name"] = new_name
                if old.startswith("成員_"):
                    rep = f"✅ 已登記名稱「{new_name}」！\n本週任務：{g['current_task']}\n完成後輸入「達標」打卡！"
                else:
                    rep = f"✅ 名稱已從「{old}」更新為「{new_name}」"

    # 達標
    elif text == "達標":
        name = data["members"][uid]["name"]
        current = get_checkins(data, uid, month, week)
        if current >= limit:
            rep = f"{name}，本週已打卡 {current} 次，達上限（每週最多 {limit} 次）"
        else:
            add_checkin(data, uid, month, week)
            rep = (f"✅ {name} 打卡成功！\n"
                   f"本週第 {current+1} 次（上限 {limit} 次）\n"
                   f"本月累計分數：{get_monthly_score(data, uid, month)} 分")

    # 排行榜（月分數）
    elif text in ["排行榜", "/排行"]:
        ranking = sorted(
            [(m["name"], get_monthly_score(data, mid, month)) for mid, m in data["members"].items()],
            key=lambda x: -x[1])
        medals = ["🥇","🥈","🥉"]
        lines = [f"📊 第 {month} 月分數排行榜\n"]
        for i, (name, score) in enumerate(ranking):
            lines.append(f"{medals[i] if i<3 else str(i+1)+'.'} {name}：{score} 分")
        rep = "\n".join(lines)

    # 週排行（打卡次數）
    elif text in ["週排行", "/週排行"]:
        key = checkin_key(month, week)
        ranking = sorted(
            [(m["name"], m.get("checkins",{}).get(key,0)) for mid, m in data["members"].items()],
            key=lambda x: -x[1])
        medals = ["🥇","🥈","🥉"]
        lines = [f"📅 第 {month} 月第 {week} 週打卡排行\n"]
        for i, (name, count) in enumerate(ranking):
            lines.append(f"{medals[i] if i<3 else str(i+1)+'.'} {name}：{count} 次")
        rep = "\n".join(lines)

    # 我的分數
    elif text in ["我的分數", "/我的分數"]:
        name = data["members"][uid]["name"]
        rep = (f"👤 {name}\n"
               f"本月累計分數：{get_monthly_score(data, uid, month)} 分\n"
               f"本週打卡次數：{get_checkins(data, uid, month, week)} / {limit} 次")

    # 本週任務
    elif text in ["本週任務", "/本週任務"]:
        rep = f"📋 第 {week} 週任務：\n{g['current_task']}"

    # 說明
    elif text in ["說明", "/說明"]:
        rep = (
            f"📖 指令說明{'（你是管理員 ✅）' if is_admin else ''}\n\n"
            "【所有成員】\n"
            "/我是 [名字] — 登記或修改名稱\n"
            "達標 — 今日任務打卡\n"
            "排行榜 — 本月分數排行\n"
            "週排行 — 本週打卡次數排行\n"
            "我的分數 — 查看分數與打卡數\n"
            "本週任務 — 查看當前任務\n\n"
            "【管理員專用】\n"
            "/任務 [內容] — 設定本週任務\n"
            "/週上限 [次數] — 設定每週打卡上限\n"
            "/獎勵 [名字] [分數] — 額外加分\n"
            "/扣分 [名字] [分數] — 扣分\n"
            "/週結算 — 結算本週排名加分\n"
            "/下一週 — 推進到下一週\n"
            "/下一月 — 進入下一個月\n"
            "/月結算 — 顯示本月最終結算\n"
            "/加管理員 [名字] — 新增管理員\n"
            "/移除管理員 [名字] — 移除管理員"
        )

    # /設管理員（無管理員時才能用）
    elif text == "/設管理員":
        if len(data["admins"]) == 0:
            data["admins"].append(uid)
            name = data["members"][uid]["name"]
            rep = (f"✅ {name} 已成為第一位管理員！\n\n"
                   f"可用指令：\n"
                   f"/任務 [內容] — 設定本週任務\n"
                   f"/週上限 [次數] — 設定每週打卡上限（預設7次）\n"
                   f"/週結算 — 結算週排名加分\n"
                   f"/下一週 / /下一月 — 推進時間\n"
                   f"/加管理員 [名字] — 新增其他管理員")
        else:
            rep = "❌ 管理員已存在！請由現有管理員使用「/加管理員 名字」新增。"

    # /加管理員
    elif text.startswith("/加管理員 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            target = find_member_by_name(data, text[6:].strip())
            if not target:
                rep = f"找不到此成員，請確認對方已用「/我是」登記名稱"
            elif target in data["admins"]:
                rep = "對方已經是管理員了！"
            else:
                data["admins"].append(target)
                rep = f"✅ 已將「{data['members'][target]['name']}」設為管理員！"

    # /移除管理員
    elif text.startswith("/移除管理員 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            target = find_member_by_name(data, text[7:].strip())
            if not target:
                rep = "找不到此成員"
            elif target == uid:
                rep = "❌ 不能移除自己的管理員權限！"
            elif target not in data["admins"]:
                rep = "對方本來就不是管理員"
            else:
                data["admins"].remove(target)
                rep = f"✅ 已移除「{data['members'][target]['name']}」的管理員權限"

    # /任務
    elif text.startswith("/任務 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            task = text[4:].strip()
            g["current_task"] = task
            rep = f"✅ 第 {week} 週任務已設定：\n{task}\n\n完成後輸入「達標」打卡！（每週最多 {limit} 次）"

    # /週上限
    elif text.startswith("/週上限 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            try:
                n = int(text[5:].strip())
                if 1 <= n <= 31:
                    g["weekly_checkin_limit"] = n
                    rep = f"✅ 每週打卡上限已設為 {n} 次"
                else:
                    rep = "請輸入 1~31 之間的數字"
            except ValueError:
                rep = "格式錯誤，請用：/週上限 7"

    # /獎勵
    elif text.startswith("/獎勵 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            parts = text.split()
            if len(parts) >= 3:
                target = find_member_by_name(data, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(data, target, month, week, pts)
                        rep = f"🎁 已給予「{data['members'][target]['name']}」+{pts} 分！\n本月累計：{get_monthly_score(data, target, month)} 分"
                    else:
                        rep = f"找不到「{parts[1]}」"
                except ValueError:
                    rep = "格式：/獎勵 名字 分數"
            else:
                rep = "格式：/獎勵 名字 分數"

    # /扣分
    elif text.startswith("/扣分 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            parts = text.split()
            if len(parts) >= 3:
                target = find_member_by_name(data, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(data, target, month, week, -pts)
                        rep = f"📉 已扣除「{data['members'][target]['name']}」{pts} 分\n本月累計：{get_monthly_score(data, target, month)} 分"
                    else:
                        rep = f"找不到「{parts[1]}」"
                except ValueError:
                    rep = "格式：/扣分 名字 分數"
            else:
                rep = "格式：/扣分 名字 分數"

    # /週結算
    elif text == "/週結算":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            bonuses = calc_weekly_bonus(data, month, week)
            if not bonuses:
                rep = f"第 {week} 週尚無人打卡，無法結算。"
            else:
                lines = [f"🏆 第 {week} 週結算加分\n"]
                for uid_b, pts in bonuses.items():
                    add_score(data, uid_b, month, week, pts)
                    name = data["members"][uid_b]["name"]
                    rank = "第一名 🥇 +2分" if pts == 2 else "第二名 🥈 +1分"
                    lines.append(f"{name}：{rank}")
                lines.append("\n加分已計入本月！")
                rep = "\n".join(lines)

    # /下一週
    elif text == "/下一週":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            g["current_week"] += 1
            rep = f"📅 已推進到第 {month} 月 第 {g['current_week']} 週！\n請用「/任務 內容」設定新任務。"

    # /下一月
    elif text == "/下一月":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            old = g["current_month"]
            g["current_month"] += 1
            g["current_week"] = 1
            rep = f"🗓️ 第 {old} 月結束！已進入第 {g['current_month']} 月第 1 週。\n請用「/任務 內容」設定新任務。"

    # /月結算
    elif text == "/月結算":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            ranking = sorted(
                [(m["name"], get_monthly_score(data, mid, month)) for mid, m in data["members"].items()],
                key=lambda x: -x[1])
            medals = ["🥇","🥈","🥉"]
            lines = [f"🏆 第 {month} 月最終結算\n"]
            for i, (name, score) in enumerate(ranking):
                lines.append(f"{medals[i] if i<3 else str(i+1)+'.'} {name}：{score} 分")
            lines.append("\n恭喜所有人辛苦了！🎉")
            rep = "\n".join(lines)

    if rep:
        save_data(data)
        reply_msg(event, rep)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
