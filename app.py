"""AKFA Qo'qon Premium: sayt + buyurtma API + Telegram bot (bitta ilova)."""
from html import escape as _esc
import os, re, json, hmac, hashlib, sqlite3, time, threading
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, send_from_directory

def esc(s):
    return _esc(str(s), quote=False)


app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OFFICE_CHAT_ID = os.environ.get("OFFICE_CHAT_ID", "")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:24]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(DATA_DIR, "akfa.db")

# Buyurtmani qabul qiladigan menejerlar (tugmalarda shu ismlar chiqadi). Ismni o'zgartirish yoki qo'shish mumkin.
MANAGERS = ["Ayubxon", "Bekzodjon", "Behzodbek", "Muhammadjon", "Umarxon", "Xolidjon"]
OFFICE_PHONE = "+998 72 200 00 00"

# Eslatmalar: ish vaqti Toshkent vaqti bilan (UTC+5). Render'da WORK_START / WORK_END o'zgaruvchilari bilan o'zgartiriladi.
TZ = timezone(timedelta(hours=5))
WORK_START = int(os.environ.get("WORK_START", "8"))
WORK_END = int(os.environ.get("WORK_END", "18"))
REMIND_AT = [5, 15, 30]  # qabul qilinmagan buyurtma uchun eslatmalar (daqiqa)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # 30 daqiqadan keyin shaxsiy xabar boradigan chat


def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, step TEXT, username TEXT)")
    _c.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'new', taken_by TEXT, created INTEGER)")
    try:
        _c.execute("ALTER TABLE orders ADD COLUMN mode TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        _c.execute("ALTER TABLE orders ADD COLUMN body TEXT")
    except sqlite3.OperationalError:
        pass
    for _col in ("rem_count INTEGER DEFAULT 0", "rem_ids TEXT", "office_msg_id INTEGER", "dm_sent INTEGER DEFAULT 0"):
        try:
            _c.execute(f"ALTER TABLE orders ADD COLUMN {_col}")
        except sqlite3.OperationalError:
            pass
    _c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")


def tg(method, **kw):
    try:
        return requests.post(f"{API}/{method}", json=kw, timeout=15).json()
    except Exception as e:
        app.logger.error("tg %s: %s", method, e)
        return {}


def ask_mode(cid):
    tg("sendMessage", chat_id=cid, text="Buyurtmani qanday olasiz?", reply_markup={"inline_keyboard": [
        [{"text": "🚚 Yetkazib berish", "callback_data": "mode:delivery"}],
        [{"text": "🏬 O'zim olib ketaman", "callback_data": "mode:pickup"}]]})


def show_catalog(cid, text="Katalogni ochish uchun pastdagi tugmani bosing. Keyingi safar chap pastdagi «Katalog» tugmasi ham bor:"):
    tg("sendMessage", chat_id=cid, text=text,
       reply_markup={"inline_keyboard": [[{"text": "📋 Katalogni ochish", "web_app": {"url": SITE_URL}}]]})


def on_message(m):
    chat, uid = m["chat"], m["from"]["id"]
    cid, text = chat["id"], (m.get("text") or "").strip()
    if text.split("@")[0] == "/id":  # guruh ID sini bilish uchun
        tg("sendMessage", chat_id=cid, text=f"Chat ID: {cid}")
        return
    if text.split("@")[0] == "/ochiq" and str(cid) == str(OFFICE_CHAT_ID):
        send_open_list(cid)
        return
    if chat["type"] != "private":
        return
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    uname = m["from"].get("username") or ""
    if text.startswith("/start") and u and u["phone"]:
        show_catalog(cid)
        return
    if not u or text.startswith("/start"):
        c.execute("INSERT INTO users(id,step,username) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET step=?,username=?",
                  (uid, "name", uname, "name", uname))
        c.commit()
        tg("sendMessage", chat_id=cid, text="Assalomu alaykum! AKFA Qo'qon Premium botiga xush kelibsiz.\n\nIsmingiz yoki tashkilot nomingizni yozing:")
        return
    if u["step"] == "name":
        if not text:
            tg("sendMessage", chat_id=cid, text="Ismingizni matn ko'rinishida yozing:")
            return
        c.execute("UPDATE users SET name=?,step='phone' WHERE id=?", (text[:100], uid))
        c.commit()
        tg("sendMessage", chat_id=cid, text="Endi telefon raqamingizni yuboring (pastdagi tugmani bosing):", reply_markup={
            "keyboard": [[{"text": "📱 Raqamni yuborish", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True})
        return
    if u["step"] == "phone":
        phone = None
        if m.get("contact") and m["contact"].get("user_id") in (uid, None):
            phone = m["contact"]["phone_number"]
        elif len(re.sub(r"\D", "", text)) >= 9:
            phone = text
        if not phone:
            tg("sendMessage", chat_id=cid, text="Raqam qabul qilinmadi. Pastdagi tugmani bosing yoki raqamni yozing.")
            return
        d = re.sub(r"\D", "", phone)
        phone = "+" + ("998" + d if len(d) == 9 else d)
        c.execute("UPDATE users SET phone=?,step='done' WHERE id=?", (phone, uid))
        c.commit()
        tg("sendMessage", chat_id=cid, text="✅ Rahmat, ro'yxatdan o'tdingiz.", reply_markup={"remove_keyboard": True})
        show_catalog(cid)
        return
    show_catalog(cid)


def on_callback(q):
    data, cid = q.get("data", ""), q["message"]["chat"]["id"]
    if data.startswith("mode:"):
        mode = data.split(":")[1]
        tg("answerCallbackQuery", callback_query_id=q["id"])
        label = "Yetkazib berish" if mode == "delivery" else "O'zi olib ketish"
        tg("sendMessage", chat_id=cid, text=f"{label} tanlandi. Katalogni oching, tovarlarni savatga yig'ing va buyurtmani yuboring:",
           reply_markup={"inline_keyboard": [[{"text": "📋 Katalogni ochish", "web_app": {"url": f"{SITE_URL}/?mode={mode}"}}]]})
    elif data.startswith("take:"):
        parts = data.split(":", 2)
        if len(parts) < 3 or parts[2] not in MANAGERS:
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Bu tugma eskirgan", show_alert=True)
            return
        oid, name = int(parts[1]), parts[2]
        c = db()
        cur = c.execute("UPDATE orders SET status='taken',taken_by=? WHERE id=? AND status='new'", (name, oid))
        c.commit()
        if cur.rowcount == 1:
            tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Buyurtma {name}ga biriktirildi")
            tg("editMessageReplyMarkup", chat_id=cid, message_id=q["message"]["message_id"], reply_markup={"inline_keyboard": []})
            tg("sendMessage", chat_id=cid, reply_to_message_id=q["message"]["message_id"], text=f"✅ Buyurtma #{oid} ni {name} qabul qildi.")
            delete_reminders(oid)
            o = c.execute("SELECT user_id, mode FROM orders WHERE id=?", (oid,)).fetchone()
            if o:
                tail = {"delivery": "U tez orada siz bilan bog'lanadi.",
                        "pickup": "Yukingiz tayyorlab qo'yiladi, tayyor bo'lganda xabar beradi."}.get(o["mode"], "U tez orada siz bilan bog'lanadi.")
                tg("sendMessage", chat_id=o["user_id"], text=f"👤 Buyurtmangiz #{oid} ni menejer {name} qabul qildi.\n{tail}")
        else:
            row = c.execute("SELECT taken_by FROM orders WHERE id=?", (oid,)).fetchone()
            who = (row["taken_by"] if row else "") or "boshqa menejer"
            tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Bu buyurtmani {who} olgan", show_alert=True)


# ---------------- Eski bot: faqat yangi botga yo'naltiradi ----------------
OLD_BOT_TOKEN = os.environ.get("OLD_BOT_TOKEN", "")
NEW_BOT_USERNAME = os.environ.get("NEW_BOT_USERNAME", "").lstrip("@")
OLD_SECRET = hashlib.sha256(OLD_BOT_TOKEN.encode()).hexdigest()[:24]


def tg_old(method, **kw):
    try:
        return requests.post(f"https://api.telegram.org/bot{OLD_BOT_TOKEN}/{method}", json=kw, timeout=15).json()
    except Exception as e:
        app.logger.error("tg_old %s: %s", method, e)
        return {}


@app.post(f"/old-webhook/{OLD_SECRET}")
def old_webhook():
    if not (OLD_BOT_TOKEN and NEW_BOT_USERNAME):
        return "ok"
    u = request.get_json(force=True, silent=True) or {}
    try:
        m = u.get("message")
        if m and m["chat"]["type"] == "private":
            tg_old("sendMessage", chat_id=m["chat"]["id"],
                   text="👋 Bu eski bot. Buyurtma berish uchun AKFA Qo'qon Rasmiy Diller yangi botiga o'ting:",
                   reply_markup={"inline_keyboard": [[{"text": "➡️ Yangi botga o'tish", "url": f"https://t.me/{NEW_BOT_USERNAME}"}]]})
    except Exception as e:
        app.logger.exception(e)
    return "ok"


@app.post(f"/webhook/{SECRET}")
def webhook():
    u = request.get_json(force=True, silent=True) or {}
    try:
        if "message" in u:
            on_message(u["message"])
        elif "callback_query" in u:
            on_callback(u["callback_query"])
    except Exception as e:
        app.logger.exception(e)
    return "ok"


@app.get("/setup")
def setup():  # brauzerda bir marta ochiladi (har yangilashdan keyin ham ochish mumkin)
    r1 = tg("setWebhook", url=f"{SITE_URL}/webhook/{SECRET}", allowed_updates=["message", "callback_query"])
    r2 = tg("setMyCommands", commands=[{"command": "start", "description": "Buyurtma berish"}], scope={"type": "all_private_chats"})
    r3 = tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Katalog", "web_app": {"url": SITE_URL}})
    r4 = tg("setMyCommands", commands=[{"command": "ochiq", "description": "Qabul qilinmagan buyurtmalar"}],
            scope={"type": "chat", "chat_id": int(OFFICE_CHAT_ID)}) if OFFICE_CHAT_ID else {}
    old = {}
    if OLD_BOT_TOKEN and NEW_BOT_USERNAME:  # eski botni yangi botga yo'naltirish
        old = {"webhook": tg_old("setWebhook", url=f"{SITE_URL}/old-webhook/{OLD_SECRET}", allowed_updates=["message"]),
               "tugma": tg_old("setChatMenuButton", menu_button={"type": "default"}),
               "menyu": tg_old("setMyCommands", commands=[{"command": "start", "description": "Yangi botga o'tish"}])}
    return jsonify(webhook=r1, menyu=r2, tugma=r3, guruh_menyu=r4, eski_bot=old)


def verify_init(init_data):
    try:
        p = dict(parse_qsl(init_data, keep_blank_values=True))
        h = p.pop("hash", "")
        dcs = "\n".join(f"{k}={p[k]}" for k in sorted(p))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        ok = hmac.compare_digest(hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest(), h)
        if ok and time.time() - int(p.get("auth_date", 0)) < 172800:
            return json.loads(p["user"])
    except Exception:
        pass
    return None


def roman(n):
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"),
                 (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out += s
            n -= v
    return out


def body_lines(items, note):
    groups = {}
    for s, col, n, qty, inote in items:
        groups.setdefault(f"{s} · {col}" if col else str(s), []).append((n, qty, inote))
    lines = []
    for g, (key, its) in enumerate(groups.items(), 1):
        lines += ["", f"<b>{roman(g)}. {esc(key[:120])}</b>"]
        for k, (n, qty, inote) in enumerate(its, 1):
            try:
                qty = max(1, min(int(qty), 1000000))
            except Exception:
                qty = 1
            lines.append(f"{k}) {esc(str(n)[:150])} - {qty} ta" + (f" (izoh: {esc(str(inote)[:150])})" if inote else ""))
    if note:
        lines += ["", f"💬 Izoh: {esc(note)}"]
    return lines


def send_lines(chat_id, lines, markup=None):
    chunks, cur_txt = [], ""
    for ln in lines:
        if len(cur_txt) + len(ln) > 3500:
            chunks.append(cur_txt)
            cur_txt = ""
        cur_txt += ln + "\n"
    chunks.append(cur_txt)
    ok, last_id = True, None
    for k, ch in enumerate(chunks):
        kw = {"chat_id": chat_id, "text": ch, "parse_mode": "HTML"}
        if markup and k == len(chunks) - 1:
            kw["reply_markup"] = markup
        res = tg("sendMessage", **kw)
        ok = res.get("ok", False) and ok
        last_id = (res.get("result") or {}).get("message_id", last_id)
    return ok, last_id


def manager_keyboard(oid):
    btns = [{"text": m, "callback_data": f"take:{oid}:{m}"} for m in MANAGERS]
    return {"inline_keyboard": [btns[i:i + 3] for i in range(0, len(btns), 3)]}


@app.post("/api/order")
def order():
    d = request.get_json(silent=True) or {}
    tu = verify_init(d.get("initData", ""))
    if not tu:
        return jsonify(ok=False, error="auth"), 401
    items = [i for i in (d.get("items") or []) if isinstance(i, list) and len(i) >= 5][:300]
    if not items:
        return jsonify(ok=False, error="empty"), 400
    mode = d.get("mode") if d.get("mode") in ("delivery", "pickup") else ""
    note = str(d.get("note") or "")[:500]
    if not mode:
        return jsonify(ok=False, error="mode"), 400
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (tu["id"],)).fetchone()
    if not (u and u["phone"]):
        return jsonify(ok=False, error="register"), 403
    cur = c.execute("INSERT INTO orders(user_id,created,mode,body) VALUES(?,?,?,?)",
                    (tu["id"], int(time.time()), mode, json.dumps({"items": items, "note": note}, ensure_ascii=False)))
    c.commit()
    oid = cur.lastrowid
    name = (u and u["name"]) or tu.get("first_name", "")
    phone = (u and u["phone"]) or "-"
    un = f"@{tu['username']}" if tu.get("username") else ""
    head = [f"🆕 Buyurtma #{oid}", esc(f"👤 {name} {un}".strip()), esc(f"📞 {phone}")]
    head.append({"delivery": "🚚 YETKAZIB BERISH: menejer mijoz bilan bog'lansin",
                 "pickup": "🏬 O'ZI OLIB KETADI: yukni rasxod qilib saqlab qo'ying"}.get(mode, "Usul tanlanmagan"))
    if not (u and u["phone"]):
        head.append("⚠️ Botda to'liq ro'yxatdan o'tmagan")
    body = body_lines(items, note)
    sent, office_mid = send_lines(OFFICE_CHAT_ID, head + body, manager_keyboard(oid))
    if not sent:
        c.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
        c.commit()
        return jsonify(ok=False, error="office"), 502
    c.execute("UPDATE orders SET office_msg_id=? WHERE id=?", (office_mid, oid))
    c.commit()
    chead = [f"📋 Buyurtmangiz #{oid} yuborildi. Tekshirib oling!"]
    chead.append({"delivery": "🚚 Yetkazib berish", "pickup": "🏬 O'zi olib ketaman"}.get(mode, ""))
    send_lines(tu["id"], [x for x in chead if x] + body + ["", f"Xato yoki o'zgartirish bo'lsa, menejerlarimiz bilan bog'laning: {OFFICE_PHONE}"])
    return jsonify(ok=True, id=oid)


@app.post("/api/me")
def me():
    d = request.get_json(silent=True) or {}
    tu = verify_init(d.get("initData", ""))
    if not tu:
        return jsonify(ok=False, error="auth"), 401
    u = db().execute("SELECT name, phone FROM users WHERE id=?", (tu["id"],)).fetchone()
    return jsonify(ok=True, registered=bool(u and u["phone"]), name=(u["name"] if u else ""))


@app.post("/api/my-orders")
def my_orders():
    d = request.get_json(silent=True) or {}
    tu = verify_init(d.get("initData", ""))
    if not tu:
        return jsonify(ok=False, error="auth"), 401
    rows = db().execute("SELECT id, created, mode, status, taken_by, body FROM orders "
                        "WHERE user_id=? AND body IS NOT NULL AND status!='failed' ORDER BY id DESC LIMIT 100", (tu["id"],)).fetchall()
    out = []
    for r in rows:
        try:
            b = json.loads(r["body"])
        except Exception:
            continue
        out.append({"id": r["id"], "created": r["created"], "mode": r["mode"],
                    "taken_by": (r["taken_by"] or "") if r["status"] == "taken" else "",
                    "items": b.get("items", []), "note": b.get("note", "")})
    return jsonify(ok=True, orders=out)


@app.get("/")
def index():
    r = send_from_directory(app.root_path, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/health")
def health():
    return "ok"


# ---------------- Eslatmalar ----------------
def wait_text(minutes):
    m = int(minutes)
    if m < 60:
        return f"{m} daqiqa"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h} soat" + (f" {mm} daqiqa" if mm else "")
    return f"{h // 24} kun"


def untaken(c):
    return c.execute("SELECT o.id, o.created, o.mode, o.rem_count, o.office_msg_id, o.dm_sent, u.name "
                     "FROM orders o LEFT JOIN users u ON u.id=o.user_id "
                     "WHERE o.status='new' AND o.body IS NOT NULL ORDER BY o.id").fetchall()


def delete_reminders(oid):
    c = db()
    r = c.execute("SELECT rem_ids FROM orders WHERE id=?", (oid,)).fetchone()
    try:
        ids = json.loads(r["rem_ids"] or "[]") if r else []
    except Exception:
        ids = []
    for mid in ids:
        tg("deleteMessage", chat_id=OFFICE_CHAT_ID, message_id=mid)
    c.execute("UPDATE orders SET rem_ids=NULL WHERE id=?", (oid,))
    c.commit()


def send_open_list(cid):
    rows, ts = untaken(db()), time.time()
    if not rows:
        tg("sendMessage", chat_id=cid, text="✅ Qabul qilinmagan buyurtma yo'q.")
        return
    lines = [f"📋 Qabul qilinmagan buyurtmalar ({len(rows)} ta):"]
    for r in rows:
        mode = {"delivery": "Yetkazib berish", "pickup": "O'zi olib ketadi"}.get(r["mode"], "")
        lines.append(f"#{r['id']} · {wait_text((ts - r['created']) / 60)} · {mode} · {r['name'] or '-'}")
    send_lines(cid, [esc(x) for x in lines])


def reminder_tick(now=None):
    now = now or datetime.now(TZ)
    if not (WORK_START <= now.hour < WORK_END):
        return  # ish vaqtidan tashqarida eslatma yuborilmaydi
    ts = now.timestamp()
    work_open = now.replace(hour=WORK_START, minute=0, second=0, microsecond=0).timestamp()
    c = db()
    today = now.strftime("%Y-%m-%d")
    d = c.execute("SELECT v FROM kv WHERE k='digest'").fetchone()
    if not d or d["v"] != today:  # har kuni ish boshlanganda, bir marta
        c.execute("INSERT INTO kv(k,v) VALUES('digest',?) ON CONFLICT(k) DO UPDATE SET v=?", (today, today))
        c.commit()
        old = [r for r in untaken(c) if r["created"] < work_open]
        if old:
            lines = [f"☀️ Ish boshlandi. Qabul qilinmagan buyurtmalar ({len(old)} ta):"]
            lines += [f"#{r['id']} · {wait_text((ts - r['created']) / 60)} kutyapti" for r in old]
            send_lines(OFFICE_CHAT_ID, [esc(x) for x in lines])
    for r in untaken(c):
        if r["created"] < work_open:
            continue  # kechagi va tungi buyurtmalar ertalabki ro'yxatda
        wait, n = (ts - r["created"]) / 60, (r["rem_count"] or 0)
        if n >= len(REMIND_AT) or wait < REMIND_AT[n]:
            continue
        cur = c.execute("UPDATE orders SET rem_count=? WHERE id=? AND status='new' AND rem_count=?", (n + 1, r["id"], n))
        c.commit()
        if cur.rowcount != 1:
            continue
        kw = {"chat_id": OFFICE_CHAT_ID, "text": f"⏰ Buyurtma #{r['id']} hali qabul qilinmadi ({int(wait)} daqiqa). Kim oladi?"}
        if r["office_msg_id"]:
            kw["reply_to_message_id"] = r["office_msg_id"]
            kw["allow_sending_without_reply"] = True
        mid = (tg("sendMessage", **kw).get("result") or {}).get("message_id")
        if mid:
            row = c.execute("SELECT rem_ids, status FROM orders WHERE id=?", (r["id"],)).fetchone()
            ids = json.loads(row["rem_ids"] or "[]") + [mid]
            c.execute("UPDATE orders SET rem_ids=? WHERE id=?", (json.dumps(ids), r["id"]))
            c.commit()
            if row["status"] != "new":  # shu orada qabul qilingan bo'lsa, eslatmani darrov o'chiramiz
                delete_reminders(r["id"])
        if n + 1 == len(REMIND_AT) and ADMIN_CHAT_ID and not r["dm_sent"]:
            c.execute("UPDATE orders SET dm_sent=1 WHERE id=?", (r["id"],))
            c.commit()
            tg("sendMessage", chat_id=ADMIN_CHAT_ID, text=f"⚠️ Buyurtma #{r['id']} {int(wait)} daqiqadan beri qabul qilinmadi.")


def _reminder_loop():
    while True:
        time.sleep(60)
        try:
            reminder_tick()
        except Exception as e:
            app.logger.exception(e)


if os.environ.get("REMINDERS", "1") == "1":
    threading.Thread(target=_reminder_loop, daemon=True).start()
