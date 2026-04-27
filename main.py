import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
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
CHECKIN_COOLDOWN = 12 * 3600
TZ = timezone(timedelta(hours=8))  # GMT+8

# ══════════════════════════════════════════════════════════
# 資料結構：每個群組完全獨立
# data = {
#   "admins": [uid, ...],
#   "groups": {
#     "gid": {
#       "current_week": 1,
#       "current_month": 1,
#       "current_task": "...",
#       "weekly_checkin_limit": 7,
#       "task_history": [...],
#       "members": {
#         "uid": {
#           "name": "小明",
#           "scores": { "月": { "週": 分數 } },
#           "checkins": { "月-週": 次數 },
#           "checkin_ts": { "月-週_ts": timestamp }
#         }
#       }
#     }
#   }
# }
# ══════════════════════════════════════════════════════════

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"admins": [], "groups": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── 群組 ─────────────────────────────────────────────────
def get_group(data, gid):
    if gid not in data["groups"]:
        data["groups"][gid] = {
            "current_week": 1,
            "current_month": 1,
            "current_task": "（尚未設定任務）",
            "weekly_checkin_limit": 7,
            "task_history": [],
            "members": {}
        }
    g = data["groups"][gid]
    for key in ["task_history", "members"]:
        if key not in g:
            g[key] = {} if key == "members" else []
    return g

# ── 成員 ─────────────────────────────────────────────────
def ensure_member(g, uid):
    if uid not in g["members"]:
        g["members"][uid] = {
            "name": f"成員_{uid[-4:]}",
            "scores": {}, "checkins": {}, "checkin_ts": {}
        }
    m = g["members"][uid]
    for key in ["checkin_ts", "scores", "checkins"]:
        if key not in m:
            m[key] = {}
    return m

def find_member_by_name(g, name):
    # 完全符合優先
    for mid, m in g["members"].items():
        if name == m["name"]:
            return mid
    # 模糊
    for mid, m in g["members"].items():
        if name in m["name"]:
            return mid
    return None

def get_display_name(g, uid):
    """取得成員顯示名稱，優先用已登記的名稱"""
    m = g["members"].get(uid, {})
    name = m.get("name", f"成員_{uid[-4:]}")
    return name

# ── 打卡 ─────────────────────────────────────────────────
def checkin_key(month, week):
    return f"{month}-{week}"

def get_checkins(g, uid, month, week):
    return g["members"].get(uid, {}).get("checkins", {}).get(checkin_key(month, week), 0)

def get_last_ts(g, uid, month, week):
    key = checkin_key(month, week) + "_ts"
    return g["members"].get(uid, {}).get("checkin_ts", {}).get(key, 0)

def add_checkin(g, uid, month, week, now_ts):
    key = checkin_key(month, week)
    ts_key = key + "_ts"
    m = g["members"][uid]
    m["checkins"][key] = m["checkins"].get(key, 0) + 1
    m["checkin_ts"][ts_key] = now_ts

# ── 分數 ─────────────────────────────────────────────────
def get_monthly_score(g, uid, month):
    return sum(g["members"].get(uid, {}).get("scores", {}).get(str(month), {}).values())

def add_score(g, uid, month, week, pts):
    m, w = str(month), str(week)
    member = g["members"][uid]
    member.setdefault("scores", {}).setdefault(m, {})
    member["scores"][m][w] = member["scores"][m].get(w, 0) + pts

# ── 排行榜（完整，支援同分同名次）──────────────────────
def build_ranking_score(g, month):
    """月分數排行，回傳 [(rank, name, score), ...]，同分同名次"""
    raw = [(get_display_name(g, mid), get_monthly_score(g, mid, month))
           for mid in g["members"]]
    raw.sort(key=lambda x: -x[1])
    result = []
    rank = 1
    for i, (name, score) in enumerate(raw):
        if i > 0 and score < raw[i-1][1]:
            rank = i + 1
        result.append((rank, name, score))
    return result

def build_ranking_checkin(g, month, week):
    """週打卡次數排行，回傳 [(rank, name, count), ...]，同分同名次"""
    key = checkin_key(month, week)
    raw = [(get_display_name(g, mid), m.get("checkins", {}).get(key, 0))
           for mid, m in g["members"].items()]
    raw.sort(key=lambda x: -x[1])
    result = []
    rank = 1
    for i, (name, count) in enumerate(raw):
        if i > 0 and count < raw[i-1][1]:
            rank = i + 1
        result.append((rank, name, count))
    return result

def rank_medal(rank):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    return medals.get(rank, f"{rank}.")

# ── 週結算加分 ────────────────────────────────────────────
def calc_weekly_bonus(g, month, week):
    key = checkin_key(month, week)
    counts = {uid: m.get("checkins", {}).get(key, 0)
              for uid, m in g["members"].items()
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

# ── 歷史任務 ──────────────────────────────────────────────
def record_task_history(g, month, week, task):
    history = g.setdefault("task_history", [])
    for h in history:
        if h["month"] == month and h["week"] == week:
            h["task"] = task
            return
    history.append({"month": month, "week": week, "task": task})
    if len(history) > 20:
        g["task_history"] = history[-20:]

def format_task_history(g, is_admin):
    history = g.get("task_history", [])
    if not history:
        return "尚無歷史任務紀錄"
    cur_week = g["current_week"]
    cur_month = g["current_month"]
    past = [h for h in history
            if not (h["month"] == cur_month and h["week"] == cur_week)]
    past = past[-4:]
    if not past:
        return "目前只有本週任務，尚無過去紀錄"
    lines = ["📜 歷史任務（最近四週）\n"]
    for h in reversed(past):
        lines.append(f"第 {h['month']} 月第 {h['week']} 週：{h['task']}")
        if is_admin:
            key = checkin_key(h["month"], h["week"])
            ranking = sorted(
                [(get_display_name(g, uid),
                  m.get("checkins", {}).get(key, 0),
                  m.get("scores", {}).get(str(h["month"]), {}).get(str(h["week"]), 0))
                 for uid, m in g["members"].items()],
                key=lambda x: -x[1]
            )
            sub = [f"  • {name}：打卡 {cnt} 次 / 該週得分 {score} 分"
                   for name, cnt, score in ranking if cnt > 0]
            lines.extend(sub if sub else ["  （無人打卡）"])
        lines.append("")
    return "\n".join(lines).strip()

# ── 週末鼓勵推播 ──────────────────────────────────────────
def weekend_reminder():
    """每分鐘檢查一次，週六日 20:00 GMT+8 推播鼓勵訊息"""
    while True:
        now = datetime.now(TZ)
        # 週六=5, 週日=6
        if now.weekday() in (5, 6) and now.hour == 20 and now.minute == 0:
            try:
                data = load_data()
                for gid, g in data["groups"].items():
                    week = g["current_week"]
                    month = g["current_month"]
                    limit = g.get("weekly_checkin_limit", 7)
                    day_label = "週六" if now.weekday() == 5 else "週日"

                    # 統計本週還沒打滿的人
                    key = checkin_key(month, week)
                    not_done = []
                    for uid, m in g["members"].items():
                        cnt = m.get("checkins", {}).get(key, 0)
                        if cnt < limit:
                            not_done.append((get_display_name(g, uid), cnt))

                    if not_done:
                        names = "、".join([f"{n}（{c}次）" for n, c in not_done])
                        msg = (
                            f"📣 週末加油提醒！\n\n"
                            f"今天是{day_label}，本週還剩最後幾天！\n"
                            f"本週任務：{g['current_task']}\n\n"
                            f"以下成員還未打滿 {limit} 次：\n"
                            f"{names}\n\n"
                            f"最後衝刺！大家加油 💪"
                        )
                    else:
                        msg = (
                            f"📣 週末加油提醒！\n\n"
                            f"今天是{day_label}，大家這週都表現超棒！\n"
                            f"本週任務：{g['current_task']}\n"
                            f"繼續保持，週末加油 💪🎉"
                        )
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(
                            PushMessageRequest(
                                to=gid,
                                messages=[TextMessage(text=msg)]
                            )
                        )
            except Exception as e:
                print(f"週末推播錯誤：{e}")
            time.sleep(60)  # 避免同一分鐘重複發送
        time.sleep(30)

# ── Reply / Push ─────────────────────────────────────────
def reply_msg(event, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=text)]))

# ── Webhook ───────────────────────────────────────────────
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
        f"達標 — 今日打卡（12小時冷卻）\n"
        f"排行榜 — 本月分數排行\n"
        f"週排行 — 本週打卡次數排行\n"
        f"歷史任務 — 查看前四週任務\n"
        f"我的分數 — 查看分數與打卡狀態\n"
        f"本週任務 — 查看當前任務\n"
        f"說明 — 顯示所有指令\n\n"
        f"【管理員設定】\n"
        f"群主請輸入「/設管理員」\n"
        f"（只有尚無管理員時有效）")

@handler.add(MemberJoinedEvent)
def handle_member_join(event):
    data = load_data()
    gid = getattr(event.source, "group_id", "default")
    g = get_group(data, gid)
    for member in event.joined.members:
        ensure_member(g, member.user_id)
    save_data(data)
    reply_msg(event,
        f"歡迎新成員！👋\n"
        f"請先輸入「/我是 你的名字」登記名稱\n"
        f"本週任務：{g['current_task']}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    data = load_data()
    uid = event.source.user_id
    text = event.message.text.strip()
    gid = getattr(event.source, "group_id", "default")
    g = get_group(data, gid)
    ensure_member(g, uid)

    week = g["current_week"]
    month = g["current_month"]
    limit = g.get("weekly_checkin_limit", 7)
    is_admin = uid in data["admins"]
    now_ts = int(time.time())
    rep = None

    # /我是
    if text.startswith("/我是 "):
        new_name = text[4:].strip()
        if not new_name:
            rep = "請輸入名字，例如：/我是 小明"
        elif len(new_name) > 10:
            rep = "名字太長，請用 10 字以內"
        else:
            existing = find_member_by_name(g, new_name)
            if existing and existing != uid:
                rep = f"「{new_name}」已被其他成員使用，請換一個名字"
            else:
                old = g["members"][uid]["name"]
                g["members"][uid]["name"] = new_name
                if old.startswith("成員_"):
                    rep = (f"✅ 已登記名稱「{new_name}」！\n"
                           f"本週任務：{g['current_task']}\n"
                           f"完成後輸入「達標」打卡！")
                else:
                    rep = f"✅ 名稱已從「{old}」更新為「{new_name}」"

    # 達標
    elif text == "達標":
        name = get_display_name(g, uid)
        current = get_checkins(g, uid, month, week)
        last_ts = get_last_ts(g, uid, month, week)
        elapsed = now_ts - last_ts
        remaining = CHECKIN_COOLDOWN - elapsed

        if current >= limit:
            rep = f"{name}，本週已打卡 {current} 次，達上限（每週最多 {limit} 次）"
        elif last_ts > 0 and remaining > 0:
            hrs = remaining // 3600
            mins = (remaining % 3600) // 60
            rep = f"⏳ {name}，距離下次打卡還需等待 {hrs} 小時 {mins} 分鐘"
        else:
            add_checkin(g, uid, month, week, now_ts)
            rep = (f"✅ {name} 打卡成功！\n"
                   f"本週第 {current+1} 次（上限 {limit} 次）\n"
                   f"本月累計分數：{get_monthly_score(g, uid, month)} 分\n"
                   f"（下次最快 12 小時後可再打卡）")

    # 排行榜（月分數，完整列出）
    elif text in ["排行榜", "/排行"]:
        ranking = build_ranking_score(g, month)
        lines = [f"📊 第 {month} 月分數排行榜（共 {len(ranking)} 人）\n"]
        # 找出最後三名（倒數三個不同名次）
        last_ranks = sorted(set(r for r, _, _ in ranking), reverse=True)[:3]
        for rank, name, score in ranking:
            medal = rank_medal(rank)
            tag = " 🍽️" if rank in last_ranks and len(ranking) >= 4 else ""
            lines.append(f"{medal} {name}：{score} 分{tag}")
        if len(ranking) >= 4:
            lines.append("\n🍽️ 本月最後三名請第一名吃飯，一起加油！")
        rep = "\n".join(lines)

    # 週排行（打卡次數，完整列出）
    elif text in ["週排行", "/週排行"]:
        ranking = build_ranking_checkin(g, month, week)
        lines = [f"📅 第 {month} 月第 {week} 週打卡排行（共 {len(ranking)} 人）\n"]
        for rank, name, count in ranking:
            lines.append(f"{rank_medal(rank)} {name}：{count} 次")
        rep = "\n".join(lines)

    # 歷史任務
    elif text in ["歷史任務", "/歷史任務"]:
        rep = format_task_history(g, is_admin)

    # 我的分數
    elif text in ["我的分數", "/我的分數"]:
        name = get_display_name(g, uid)
        last_ts = get_last_ts(g, uid, month, week)
        elapsed = now_ts - last_ts
        remaining = CHECKIN_COOLDOWN - elapsed
        if last_ts > 0 and remaining > 0:
            hrs = remaining // 3600
            mins = (remaining % 3600) // 60
            cd_msg = f"下次打卡：{hrs} 小時 {mins} 分後"
        else:
            cd_msg = "現在可以打卡 ✅"
        rep = (f"👤 {name}\n"
               f"本月累計分數：{get_monthly_score(g, uid, month)} 分\n"
               f"本週打卡次數：{get_checkins(g, uid, month, week)} / {limit} 次\n"
               f"{cd_msg}")

    # 本週任務
    elif text in ["本週任務", "/本週任務"]:
        rep = f"📋 第 {week} 週任務：\n{g['current_task']}"

    # 說明
    elif text in ["說明", "/說明"]:
        rep = (
            f"📖 指令說明{'（你是管理員 ✅）' if is_admin else ''}\n\n"
            "【所有成員】\n"
            "/我是 [名字] — 登記或修改名稱\n"
            "達標 — 今日任務打卡（12小時冷卻）\n"
            "排行榜 — 本月分數排行（完整）\n"
            "週排行 — 本週打卡次數排行（完整）\n"
            "歷史任務 — 查看前四週任務\n"
            "我的分數 — 查看分數與打卡狀態\n"
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

    # /設管理員
    elif text == "/設管理員":
        if len(data["admins"]) == 0:
            data["admins"].append(uid)
            name = get_display_name(g, uid)
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
            target = find_member_by_name(g, text[6:].strip())
            if not target:
                rep = "找不到此成員，請確認對方已用「/我是」登記名稱"
            elif target in data["admins"]:
                rep = "對方已經是管理員了！"
            else:
                data["admins"].append(target)
                rep = f"✅ 已將「{get_display_name(g, target)}」設為管理員！"

    # /移除管理員
    elif text.startswith("/移除管理員 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            target = find_member_by_name(g, text[7:].strip())
            if not target:
                rep = "找不到此成員"
            elif target == uid:
                rep = "❌ 不能移除自己的管理員權限！"
            elif target not in data["admins"]:
                rep = "對方本來就不是管理員"
            else:
                data["admins"].remove(target)
                rep = f"✅ 已移除「{get_display_name(g, target)}」的管理員權限"

    # /任務
    elif text.startswith("/任務 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            task = text[4:].strip()
            if g["current_task"] != "（尚未設定任務）":
                record_task_history(g, month, week, g["current_task"])
            g["current_task"] = task
            record_task_history(g, month, week, task)
            rep = (f"✅ 第 {week} 週任務已設定：\n{task}\n\n"
                   f"完成後輸入「達標」打卡！\n"
                   f"（每週最多 {limit} 次，12小時冷卻）")

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
                target = find_member_by_name(g, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(g, target, month, week, pts)
                        rep = (f"🎁 已給予「{get_display_name(g, target)}」+{pts} 分！\n"
                               f"本月累計：{get_monthly_score(g, target, month)} 分")
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
                target = find_member_by_name(g, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(g, target, month, week, -pts)
                        rep = (f"📉 已扣除「{get_display_name(g, target)}」{pts} 分\n"
                               f"本月累計：{get_monthly_score(g, target, month)} 分")
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
            bonuses = calc_weekly_bonus(g, month, week)
            if not bonuses:
                rep = f"第 {week} 週尚無人打卡，無法結算。"
            else:
                lines = [f"🏆 第 {week} 週結算加分\n"]
                for uid_b, pts in bonuses.items():
                    add_score(g, uid_b, month, week, pts)
                    name = get_display_name(g, uid_b)
                    rank = "第一名 🥇 +2分" if pts == 2 else "第二名 🥈 +1分"
                    lines.append(f"{name}：{rank}")
                lines.append("\n加分已計入本月！")
                rep = "\n".join(lines)

    # /下一週
    elif text == "/下一週":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            if g["current_task"] != "（尚未設定任務）":
                record_task_history(g, month, week, g["current_task"])
            g["current_week"] += 1
            rep = (f"📅 已推進到第 {month} 月 第 {g['current_week']} 週！\n"
                   f"請用「/任務 內容」設定新任務。")

    # /下一月
    elif text == "/下一月":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            if g["current_task"] != "（尚未設定任務）":
                record_task_history(g, month, week, g["current_task"])
            old = g["current_month"]
            g["current_month"] += 1
            g["current_week"] = 1
            rep = (f"🗓️ 第 {old} 月結束！\n"
                   f"已進入第 {g['current_month']} 月第 1 週。\n"
                   f"請用「/任務 內容」設定新任務。")

    # /月結算
    elif text == "/月結算":
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            ranking = build_ranking_score(g, month)
            last_ranks = sorted(set(r for r, _, _ in ranking), reverse=True)[:3]
            first_name = ranking[0][1] if ranking else "第一名"
            lines = [f"🏆 第 {month} 月最終結算（共 {len(ranking)} 人）\n"]
            for rank, name, score in ranking:
                medal = rank_medal(rank)
                tag = " 🍽️" if rank in last_ranks and len(ranking) >= 4 else ""
                lines.append(f"{medal} {name}：{score} 分{tag}")
            if len(ranking) >= 4:
                lines.append(f"\n🍽️ 最後三名要請 {first_name} 吃飯喔！")
                lines.append("感謝大家這個月的努力，繼續加油！🎉")
            rep = "\n".join(lines)

    if rep:
        save_data(data)
        reply_msg(event, rep)

# ── 啟動週末提醒背景執行緒 ───────────────────────────────
reminder_thread = threading.Thread(target=weekend_reminder, daemon=True)
reminder_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
