import os
import json
import time
import threading
import random
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
from pymongo import MongoClient

app = Flask(__name__)

CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "667b16a4820dd8e65d4caa00b80210f9")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "mx7Oz6AD9+iCpY4RoQ6nFPE795eETLgxRfi6vdZFGa6ymsqKc6EvTkaqeX7kTg1PIsy2c0Wvmzabtb0weS7Je+5kijz/bqAJUxLgGC97+HZ4lBhSgzc2HLu/BtQdWzcCoiDwtXzWuYN/8Zt42qnyxgdB04t89/1O/w1cDnyilFU=")
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://linebotuser:linebot1234@cluster0.skhhqmy.mongodb.net/?appName=Cluster0")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
TZ = timezone(timedelta(hours=8))
CHECKIN_COOLDOWN = 12 * 3600

# ── MongoDB（延遲初始化，解決 fork-safe 問題）────────────
_mongo_client = None

def get_col():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI)
    return _mongo_client["linebot"]["data"]

def load_data():
    try:
        doc = get_col().find_one({"_id": "main"})
        if doc:
            doc.pop("_id", None)
            return doc
    except Exception as e:
        print(f"load_data error: {e}")
    return {"groups": {}}

def save_data(data):
    try:
        get_col().replace_one({"_id": "main"}, {"_id": "main", **data}, upsert=True)
    except Exception as e:
        print(f"save_data error: {e}")

# ── 群組（管理員改為各群組獨立）─────────────────────────
def get_group(data, gid):
    if gid not in data["groups"]:
        data["groups"][gid] = {
            "current_week": 1,
            "current_month": 1,
            "current_task": "（尚未設定任務）",
            "weekly_checkin_limit": 7,
            "task_history": [],
            "members": {},
            "admins": []          # ← 每個群組有自己的管理員清單
        }
    g = data["groups"][gid]
    for key, default in [("task_history", []), ("members", {}), ("admins", [])]:
        if key not in g:
            g[key] = default
    return g

# ── 成員 ─────────────────────────────────────────────────
def ensure_member(g, uid):
    if uid not in g["members"]:
        g["members"][uid] = {
            "name": f"成員_{uid[-4:]}",
            "scores": {}, "checkins": {}, "checkin_ts": {}
        }
    m = g["members"][uid]
    for key in ["checkin_ts", "scores", "checkins", "shares"]:
        if key not in m:
            m[key] = {}
    return m

def find_member_by_name(g, name):
    for mid, m in g["members"].items():
        if name == m["name"]:
            return mid
    for mid, m in g["members"].items():
        if name in m["name"]:
            return mid
    return None

def get_display_name(g, uid):
    return g["members"].get(uid, {}).get("name", f"成員_{uid[-4:]}")

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

def set_checkin_count(g, uid, month, week, count):
    """直接設定打卡次數（補打卡用）"""
    key = checkin_key(month, week)
    g["members"][uid]["checkins"][key] = count

# ── 分享次數 ──────────────────────────────────────────────
def share_key(month):
    return f"share_{month}"

def get_shares(g, uid, month):
    return g["members"].get(uid, {}).get("shares", {}).get(share_key(month), 0)

def add_share(g, uid, month):
    key = share_key(month)
    g["members"][uid].setdefault("shares", {})
    g["members"][uid]["shares"][key] = g["members"][uid]["shares"].get(key, 0) + 1

# ── 分數 ─────────────────────────────────────────────────
def get_monthly_score(g, uid, month):
    return sum(g["members"].get(uid, {}).get("scores", {}).get(str(month), {}).values())

def add_score(g, uid, month, week, pts):
    m, w = str(month), str(week)
    member = g["members"][uid]
    member.setdefault("scores", {}).setdefault(m, {})
    member["scores"][m][w] = member["scores"][m].get(w, 0) + pts

# ── 排行榜 ────────────────────────────────────────────────
def build_ranking_score(g, month):
    raw = [(get_display_name(g, mid), get_monthly_score(g, mid, month))
           for mid in g["members"]]
    raw.sort(key=lambda x: -x[1])
    result, rank = [], 1
    for i, (name, score) in enumerate(raw):
        if i > 0 and score < raw[i-1][1]:
            rank = i + 1
        result.append((rank, name, score))
    return result

def build_ranking_checkin(g, month, week):
    key = checkin_key(month, week)
    raw = [(get_display_name(g, mid), m.get("checkins", {}).get(key, 0))
           for mid, m in g["members"].items()]
    raw.sort(key=lambda x: -x[1])
    result, rank = [], 1
    for i, (name, count) in enumerate(raw):
        if i > 0 and count < raw[i-1][1]:
            rank = i + 1
        result.append((rank, name, count))
    return result

def rank_medal(rank):
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")

# ── 週結算 ────────────────────────────────────────────────
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
    cur_week, cur_month = g["current_week"], g["current_month"]
    past = [h for h in history
            if not (h["month"] == cur_month and h["week"] == cur_week)][-4:]
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
                key=lambda x: -x[1])
            sub = [f"  • {name}：打卡 {cnt} 次 / 該週得分 {score} 分"
                   for name, cnt, score in ranking if cnt > 0]
            lines.extend(sub if sub else ["  （無人打卡）"])
        lines.append("")
    return "\n".join(lines).strip()

# ── 週末推播 ──────────────────────────────────────────────
def weekend_reminder():
    sent_today = set()
    while True:
        now = datetime.now(TZ)
        today_key = now.strftime("%Y-%m-%d")
        if now.weekday() in (5, 6) and now.hour == 20 and now.minute == 0 and today_key not in sent_today:
            sent_today.add(today_key)
            try:
                data = load_data()
                day_label = "週六" if now.weekday() == 5 else "週日"
                for gid, g in data["groups"].items():
                    week, month = g["current_week"], g["current_month"]
                    limit = g.get("weekly_checkin_limit", 7)
                    key = checkin_key(month, week)
                    not_done = [(get_display_name(g, uid), m.get("checkins", {}).get(key, 0))
                                for uid, m in g["members"].items()
                                if m.get("checkins", {}).get(key, 0) < limit]
                    if not_done:
                        names = "\n".join([f"  • {n}（已打 {c} 次）" for n, c in not_done])
                        msg = (f"📣 週末加油提醒！\n\n今天是{day_label}，本週最後衝刺！\n"
                               f"本週任務：{g['current_task']}\n\n"
                               f"以下成員還未打滿 {limit} 次：\n{names}\n\n"
                               f"加油！最後兩天把事情完成 💪")
                    else:
                        msg = (f"📣 週末加油提醒！\n\n今天是{day_label}，大家這週都超棒！🎉\n"
                               f"本週任務：{g['current_task']}\n繼續保持，週末加油 💪")
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(
                            PushMessageRequest(to=gid, messages=[TextMessage(text=msg)]))
            except Exception as e:
                print(f"週末推播錯誤：{e}")
        time.sleep(30)

# ── Reply ─────────────────────────────────────────────────
def reply_msg(event, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=event.reply_token,
                                messages=[TextMessage(text=text)]))

# ── 用戶群組對應 API ─────────────────────────────────────
@app.route("/splitbill/user-group", methods=["GET"])
def sb_user_group():
    from flask import jsonify, request as req
    uid = req.args.get("uid", "")
    data = load_data()
    # 從所有群組找這個 user 在哪個群組
    for gid, g in data["groups"].items():
        if uid in g.get("members", {}):
            return jsonify({"gid": gid})
    return jsonify({"gid": "default"})

# ── CORS 設定 ────────────────────────────────────────────
@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    allowed = ["https://fish77889401-svg.github.io", "https://liff.line.me"]
    if any(origin.startswith(a) for a in allowed) or not origin:
        response.headers["Access-Control-Allow-Origin"] = origin or "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/splitbill/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    from flask import Response
    resp = Response()
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

# ── 分帳 API ──────────────────────────────────────────────
@app.route("/splitbill/members", methods=["GET"])
def sb_members():
    from flask import jsonify, request as req
    gid = req.args.get("group", "default")
    data = load_data()
    g = data["groups"].get(gid, {})
    members = [m["name"] for m in g.get("members", {}).values()
               if not m["name"].startswith("成員_")]
    return jsonify({"members": members})

@app.route("/splitbill/books", methods=["GET"])
def sb_books():
    from flask import jsonify, request as req
    gid = req.args.get("group", "default")
    data = load_data()
    g = get_group(data, gid)
    books = g.get("splitbooks", [])
    save_data(data)
    return jsonify({"books": books})

@app.route("/splitbill/book", methods=["POST"])
def sb_create_book():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    g.setdefault("splitbooks", []).insert(0, body["book"])
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/book/close", methods=["POST"])
def sb_close_book():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            b["closed"] = True
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/book/reopen", methods=["POST"])
def sb_reopen_book():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            b["closed"] = False
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/expense", methods=["POST"])
def sb_add_expense():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            b.setdefault("expenses", []).append(body["expense"])
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/book/addmember", methods=["POST"])
def sb_add_member():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            b["members"] = body["members"]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/expense/edit", methods=["POST"])
def sb_edit_expense():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            for i, e in enumerate(b.get("expenses", [])):
                if e["id"] == body["expense"]["id"]:
                    b["expenses"][i] = body["expense"]
    save_data(data)
    return jsonify({"ok": True})

@app.route("/splitbill/expense/delete", methods=["POST"])
def sb_delete_expense():
    from flask import jsonify, request as req
    body = req.json
    gid = body.get("groupId", "default")
    data = load_data()
    g = get_group(data, gid)
    for b in g.get("splitbooks", []):
        if b["id"] == body["bookId"]:
            b["expenses"] = [e for e in b.get("expenses",[]) if e["id"] != body["expId"]]
    save_data(data)
    return jsonify({"ok": True})

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
        f"（只有此群尚無管理員時有效）")

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
    is_admin = uid in g["admins"]   # ← 各群組獨立管理員
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

    # 我要分享
    elif text == "我要分享":
        name = get_display_name(g, uid)
        add_share(g, uid, month)
        total = get_shares(g, uid, month)
        cheers = [
            f"哇！{name}同學主動分享了！副班長超感動 🥹✨\n你的分享讓大家更有動力，繼續保持！",
            f"太棒了！{name}同學又來分享囉 🎉\n副班長幫你記好了，你是大家的榜樣！",
            f"{name}同學分享達人出現！📢\n每一次分享都是在為班級加油，謝謝你！",
            f"副班長收到！{name}同學的分享已記錄 ✅\n你的行動力真的讓人佩服 💪",
            f"🌟 {name}同學！你的分享點亮了今天！\n副班長替大家謝謝你，繼續加油哦！",
            f"{name}同學太勤快了吧！📣\n分享精神滿分，副班長幫你蓋章認證 🏅",
            f"哎呀！{name}同學又來啦 😄🎊\n副班長最喜歡看到你分享了，大家都在看喔！",
            f"📢 {name}同學的分享已送達全班！\n你每次的分享都是最好的示範，繼續衝！",
            f"{name}同學你好棒喔！🌈\n每次看到你分享副班長都超開心的！",
            f"哇塞！{name}同學今天也來分享了！🎯\n這種積極的態度，副班長給你100分！",
            f"✨ {name}同學！你的到來讓今天更精彩！\n謝謝你願意把好的東西帶給大家！",
            f"{name}同學出現了！🙌\n你的分享就是班級最好的養分，繼續加油！",
            f"副班長最喜歡{name}同學這樣積極的態度了！😍\n你真的是大家的好榜樣！",
            f"哇！{name}同學！你的行動力讓副班長甘拜下風 🫡\n感謝你為班級貢獻！",
            f"🎊 {name}同學！分享的力量超強大的！\n你每次的付出副班長都有看到，辛苦了！",
            f"{name}同學，你知道嗎？你的分享讓班級更有凝聚力 🤝\n副班長替大家謝謝你！",
            f"哈囉！{name}同學！今天也這麼勤奮呢 😊\n你的正能量感染了副班長，謝謝你！",
            f"🌻 {name}同學！你就像陽光一樣！\n每次分享都讓班級更溫暖，繼續保持！",
            f"{name}同學的分享超有價值的！💎\n副班長幫你記錄好了，你超棒的！",
            f"哇喔！{name}同學！你的積極度讓副班長印象深刻 🔥\n大家都在向你學習喔！",
            f"🎵 {name}同學，你來啦！\n你的分享讓今天的氣氛超好的，副班長愛你！",
            f"{name}同學！副班長要頒給你「最佳分享獎」🏆\n謝謝你每次都這麼認真！",
            f"哇！{name}同學好厲害！✨\n每次看到你分享，副班長就覺得這個班好有活力！",
            f"🌟 {name}同學！你的行動力讓副班長超崇拜的！\n繼續帶領大家向前衝！",
            f"{name}同學！你知道你有多棒嗎？🥰\n每次的分享都讓班級進步了一點點，謝謝你！",
            f"副班長要給{name}同學一個大大的讚！👍\n你的分享精神值得大家學習！",
            f"🎉 哇！{name}同學！你今天也來了！\n你的出現讓班級更完整，副班長超開心！",
            f"{name}同學！你的每一次分享都是在為自己投資 💰\n副班長幫你記錄，繼續加油！",
            f"哎唷！{name}同學你真的超認真的啦 😄\n副班長幫你蓋上「努力章」，繼續保持！",
            f"🌈 {name}同學！謝謝你今天的分享！\n你讓班級的每一天都更有意義，超棒的！",
            f"{name}同學！副班長偷偷告訴你，你是班上最積極的人之一喔 🤫✨\n繼續加油！",
            f"哇！{name}同學又出動了！🚀\n你的分享能量滿滿，副班長幫你加油！",
            f"🎯 {name}同學！每次分享都是一次成長！\n副班長為你的努力感到驕傲！",
            f"{name}同學！你的分享讓副班長充滿動力 ⚡\n謝謝你，大家都在支持你！",
            f"哇塞！{name}同學！你的行動力超強的！🦸\n副班長幫你記好了，繼續衝！",
            f"🌺 {name}同學！你每次分享都像在種下一顆種子！\n謝謝你讓班級開出美麗的花！",
            f"{name}同學！副班長超喜歡看到你分享的！😆\n你是班級裡的小太陽，繼續發光！",
            f"哇！{name}同學！你的熱情讓副班長都想跟著你一起衝！🔥\n謝謝你的付出！",
            f"🎊 {name}同學！你做到了！\n每一次分享都是你進步的證明，副班長替你驕傲！",
            f"{name}同學！你知道嗎，你的分享是班級的寶藏 💫\n謝謝你，繼續閃閃發光！",
            f"哎呀！{name}同學你真的超可以的啦！😍\n副班長替大家謝謝你的每一次付出！",
            f"🏅 {name}同學！副班長要頒給你本日最佳積極獎！\n謝謝你讓班級充滿正能量！",
            f"{name}同學！你的分享精神感動了副班長 🥺\n你是班級裡最閃亮的星星之一！",
            f"哇！{name}同學！你每次的行動都讓副班長超感動的 😭✨\n謝謝你，繼續加油！",
            f"🌙 {name}同學！不管什麼時候分享，副班長都在這裡幫你記錄！\n你超棒的，繼續！",
            f"{name}同學！副班長要為你鼓掌 👏👏👏\n你的積極態度是大家最好的示範！",
            f"哇喔！{name}同學！你又來啦！🎈\n每次看到你，副班長就知道今天也會很充實！",
            f"🦋 {name}同學！你的每一次分享都讓班級蛻變得更好！\n謝謝你，超級無敵棒！",
            f"{name}同學！副班長想說，有你在真的很幸運 🍀\n謝謝你每次都這麼認真分享！",
            f"哇！{name}同學！你的能量值滿格！⚡⚡⚡\n副班長幫你記錄下這美好的一刻！",
            f"🎁 {name}同學！你的分享就是送給大家最好的禮物！\n副班長替全班謝謝你！",
        ]
        msg = random.choice(cheers)
        rep = msg

    # 達標／達成／完成
    elif text in ["達標", "達成", "完成"]:
        name = get_display_name(g, uid)
        current = get_checkins(g, uid, month, week)
        last_ts = get_last_ts(g, uid, month, week)
        remaining = CHECKIN_COOLDOWN - (now_ts - last_ts)
        if current >= limit:
            rep = f"{name}，本週已打卡 {current} 次，達上限（每週最多 {limit} 次）"
        elif last_ts > 0 and remaining > 0:
            hrs, mins = remaining // 3600, (remaining % 3600) // 60
            rep = f"⏳ {name}，距離下次打卡還需等待 {hrs} 小時 {mins} 分鐘"
        else:
            add_checkin(g, uid, month, week, now_ts)
            rep = (f"✅ {name} 打卡成功！\n"
                   f"本週第 {current+1} 次（上限 {limit} 次）\n"
                   f"本月累計分數：{get_monthly_score(g, uid, month)} 分\n"
                   f"（下次最快 12 小時後可再打卡）")

    # 排行榜
    elif text in ["排行榜", "/排行"]:
        ranking = build_ranking_score(g, month)
        last_ranks = sorted(set(r for r, _, _ in ranking), reverse=True)[:3]
        lines = [f"📊 第 {month} 月分數排行榜（共 {len(ranking)} 人）\n"]
        for rank, name, score in ranking:
            # 找回 uid 取分享次數
            uid_for = next((mid for mid, m in g["members"].items()
                           if get_display_name(g, mid) == name), None)
            shares = get_shares(g, uid_for, month) if uid_for else 0
            share_str = f" 📢{shares}" if shares > 0 else ""
            tag = " 🍽️" if rank in last_ranks and len(ranking) >= 4 else ""
            lines.append(f"{rank_medal(rank)} {name}：{score} 分{share_str}{tag}")
        if len(ranking) >= 4:
            lines.append("\n🍽️ 最後三名本月要請第一名吃飯，一起加油！")
        lines.append("\n📢 = 本月分享次數")
        rep = "\n".join(lines)

    # 週排行
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
        remaining = CHECKIN_COOLDOWN - (now_ts - last_ts)
        if last_ts > 0 and remaining > 0:
            hrs, mins = remaining // 3600, (remaining % 3600) // 60
            cd_msg = f"下次打卡：{hrs} 小時 {mins} 分後"
        else:
            cd_msg = "現在可以打卡 ✅"
        shares = get_shares(g, uid, month)
        rep = (f"👤 {name}\n"
               f"本月累計分數：{get_monthly_score(g, uid, month)} 分\n"
               f"本週打卡次數：{get_checkins(g, uid, month, week)} / {limit} 次\n"
               f"本月分享次數：{shares} 次 📢\n"
               f"{cd_msg}")

    # 分帳
    elif text in ["分帳", "/分帳"]:
        gid_param = gid if gid != "default" else "default"
        liff_url = f"https://liff.line.me/2010052774-AyGqRPDF?liff.state=%3Fgid%3D{gid_param}"
        rep = f"💰 副班長分帳\n\n點下方連結開啟：\n{liff_url}\n\n建立帳本、記錄支出、自動結算誰欠誰多少錢！"

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
            "我要分享 — 記錄本月分享次數\n"
            "分帳 — 開啟分帳功能\n"
            "排行榜 — 本月分數排行（含分享次數）\n"
            "週排行 — 本週打卡次數排行（完整）\n"
            "歷史任務 — 查看前四週任務\n"
            "我的分數 — 查看分數與打卡狀態\n"
            "本週任務 — 查看當前任務\n\n"
            "【管理員專用】\n"
            "/任務 [內容] — 設定本週任務\n"
            "/週上限 [次數] — 設定每週打卡上限\n"
            "/補打卡 [名字] [次數] — 補登打卡次數（可多行）\n"
            "/減打卡 [名字] [次數] — 扣除打卡次數（可多行）\n"
            "/獎勵 [名字] [分數] — 額外加分（可多行）\n"
            "/扣分 [名字] [分數] — 扣分（可多行）\n"
            "/週結算 — 結算本週排名加分\n"
            "/下一週 — 推進到下一週\n"
            "/下一月 — 進入下一個月\n"
            "/月結算 — 顯示本月最終結算\n"
            "/加管理員 [名字] — 新增管理員\n"
            "/移除管理員 [名字] — 移除管理員"
        )

    # /設管理員（此群組無管理員時才能用）
    elif text == "/設管理員":
        if len(g["admins"]) == 0:
            g["admins"].append(uid)
            name = get_display_name(g, uid)
            rep = (f"✅ {name} 已成為此群組的管理員！\n\n"
                   f"可用指令：\n"
                   f"/任務 [內容] — 設定本週任務\n"
                   f"/週上限 [次數] — 每週打卡上限（預設7次）\n"
                   f"/補打卡 [名字] [次數] — 補登打卡次數（可多行）\n"
                   f"/減打卡 [名字] [次數] — 扣除打卡次數（可多行）\n"
                   f"/週結算 — 結算週排名加分\n"
                   f"/下一週 / /下一月 — 推進時間\n"
                   f"/加管理員 [名字] — 新增其他管理員")
        else:
            rep = "❌ 此群組已有管理員！請由現有管理員使用「/加管理員 名字」新增。"

    # /加管理員
    elif text.startswith("/加管理員 "):
        if not is_admin:
            rep = "❌ 管理員專用指令"
        else:
            target = find_member_by_name(g, text[6:].strip())
            if not target:
                rep = "找不到此成員，請確認對方已用「/我是」登記名稱"
            elif target in g["admins"]:
                rep = "對方已經是此群組的管理員了！"
            else:
                g["admins"].append(target)
                rep = f"✅ 已將「{get_display_name(g, target)}」設為此群組的管理員！"

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
            elif target not in g["admins"]:
                rep = "對方本來就不是管理員"
            else:
                g["admins"].remove(target)
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

    # /補打卡（支援多行批次）
    elif "/補打卡 " in text and is_admin:
        lines_in = text.strip().splitlines()
        results = []
        errors = []
        for line in lines_in:
            line = line.strip()
            if not line.startswith("/補打卡 "):
                continue
            parts = line.split()
            if len(parts) >= 3:
                target = find_member_by_name(g, parts[1])
                try:
                    add_n = int(parts[2])
                    if target:
                        current = get_checkins(g, target, month, week)
                        new_count = min(current + add_n, limit)
                        set_checkin_count(g, target, month, week, new_count)
                        name = get_display_name(g, target)
                        results.append(f"✅ {name} 補打卡 +{add_n} 次（本週 {new_count}/{limit} 次）")
                    else:
                        errors.append(f"❌ 找不到「{parts[1]}」")
                except ValueError:
                    errors.append(f"❌ 格式錯誤：{line}")
            else:
                errors.append(f"❌ 格式錯誤：{line}")
        if results or errors:
            rep = "📋 補打卡結果：\n\n" + "\n".join(results + errors)
        else:
            rep = "格式：/補打卡 名字 次數（可多行）"

    elif text.startswith("/補打卡 ") and not is_admin:
        rep = "❌ 管理員專用指令"

    # /減打卡（支援多行批次）
    elif "/減打卡 " in text and is_admin:
        lines_in = text.strip().splitlines()
        results = []
        errors = []
        for line in lines_in:
            line = line.strip()
            if not line.startswith("/減打卡 "):
                continue
            parts = line.split()
            if len(parts) >= 3:
                target = find_member_by_name(g, parts[1])
                try:
                    sub_n = int(parts[2])
                    if target:
                        current = get_checkins(g, target, month, week)
                        new_count = max(current - sub_n, 0)
                        set_checkin_count(g, target, month, week, new_count)
                        name = get_display_name(g, target)
                        results.append(f"📉 {name} 減打卡 -{sub_n} 次（本週 {new_count}/{limit} 次）")
                    else:
                        errors.append(f"❌ 找不到「{parts[1]}」")
                except ValueError:
                    errors.append(f"❌ 格式錯誤：{line}")
            else:
                errors.append(f"❌ 格式錯誤：{line}")
        if results or errors:
            rep = "📋 減打卡結果：\n\n" + "\n".join(results + errors)
        else:
            rep = "格式：/減打卡 名字 次數（可多行）"

    elif text.startswith("/減打卡 ") and not is_admin:
        rep = "❌ 管理員專用指令"

    # /獎勵（支援多行批次）
    elif "/獎勵 " in text and is_admin:
        lines_in = text.strip().splitlines()
        results = []
        errors = []
        for line in lines_in:
            line = line.strip()
            if not line.startswith("/獎勵 "):
                continue
            parts = line.split()
            if len(parts) >= 3:
                target = find_member_by_name(g, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(g, target, month, week, pts)
                        results.append(f"✅ {get_display_name(g, target)} +{pts} 分")
                    else:
                        errors.append(f"❌ 找不到「{parts[1]}」")
                except ValueError:
                    errors.append(f"❌ 格式錯誤：{line}")
            else:
                errors.append(f"❌ 格式錯誤：{line}")
        if results or errors:
            rep = "🎁 批次獎勵結果：\n\n" + "\n".join(results + errors)
        else:
            rep = "格式：/獎勵 名字 分數（可多行）"

    elif text.startswith("/獎勵 ") and not is_admin:
        rep = "❌ 管理員專用指令"

    # /扣分（支援多行批次）
    elif "/扣分 " in text and is_admin:
        lines_in = text.strip().splitlines()
        results = []
        errors = []
        for line in lines_in:
            line = line.strip()
            if not line.startswith("/扣分 "):
                continue
            parts = line.split()
            if len(parts) >= 3:
                target = find_member_by_name(g, parts[1])
                try:
                    pts = int(parts[2])
                    if target:
                        add_score(g, target, month, week, -pts)
                        results.append(f"📉 {get_display_name(g, target)} -{pts} 分")
                    else:
                        errors.append(f"❌ 找不到「{parts[1]}」")
                except ValueError:
                    errors.append(f"❌ 格式錯誤：{line}")
            else:
                errors.append(f"❌ 格式錯誤：{line}")
        if results or errors:
            rep = "批次扣分結果：\n\n" + "\n".join(results + errors)
        else:
            rep = "格式：/扣分 名字 分數（可多行）"

    elif text.startswith("/扣分 ") and not is_admin:
        rep = "❌ 管理員專用指令"

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
                    rank_label = "第一名 🥇 +2分" if pts == 2 else "第二名 🥈 +1分"
                    lines.append(f"{get_display_name(g, uid_b)}：{rank_label}")
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
                uid_for = next((mid for mid, m in g["members"].items()
                               if get_display_name(g, mid) == name), None)
                shares = get_shares(g, uid_for, month) if uid_for else 0
                share_str = f" 📢{shares}" if shares > 0 else ""
                tag = " 🍽️" if rank in last_ranks and len(ranking) >= 4 else ""
                lines.append(f"{rank_medal(rank)} {name}：{score} 分{share_str}{tag}")
            if len(ranking) >= 4:
                lines.append(f"\n🍽️ 最後三名要請 {first_name} 吃飯喔！")
                lines.append("感謝大家這個月的努力，繼續加油！🎉")
            lines.append("\n📢 = 本月分享次數")
            rep = "\n".join(lines)

    if rep:
        save_data(data)
        reply_msg(event, rep)

# ── 週末推播背景執行緒 ────────────────────────────────────
threading.Thread(target=weekend_reminder, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
