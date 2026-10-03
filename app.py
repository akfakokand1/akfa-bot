"""AKFA Qo'qon Premium: sayt + buyurtma API + Telegram bot (bitta ilova)."""
import os, re, json, hmac, hashlib, sqlite3, time
from urllib.parse import parse_qsl
import requests
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OFFICE_CHAT_ID = os.environ.get("OFFICE_CHAT_ID", "")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:24]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(DATA_DIR, "akfa.db")


def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    return c


with db() as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, step TEXT, username TEXT)")
    _c.execute("CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'new', taken_by TEXT, created INTEGER)")


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


def on_message(m):
    chat, uid = m["chat"], m["from"]["id"]
    cid, text = chat["id"], (m.get("text") or "").strip()
    if text.split("@")[0] == "/id":  # guruh ID sini bilish uchun
        tg("sendMessage", chat_id=cid, text=f"Chat ID: {cid}")
        return
    if chat["type"] != "private":
        return
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    uname = m["from"].get("username") or ""
    if text.startswith("/start") and u and u["phone"]:
        ask_mode(cid)
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
        ask_mode(cid)
        return
    ask_mode(cid)


def on_callback(q):
    data, cid = q.get("data", ""), q["message"]["chat"]["id"]
    if data.startswith("mode:"):
        mode = data.split(":")[1]
        tg("answerCallbackQuery", callback_query_id=q["id"])
        label = "Yetkazib berish" if mode == "delivery" else "O'zi olib ketish"
        tg("sendMessage", chat_id=cid, text=f"{label} tanlandi. Katalogni oching, tovarlarni savatga yig'ing va buyurtmani yuboring:",
           reply_markup={"inline_keyboard": [[{"text": "📋 Katalogni ochish", "web_app": {"url": f"{SITE_URL}/?mode={mode}"}}]]})
    elif data.startswith("take:"):
        oid = int(data.split(":")[1])
        f = q["from"]
        who = (f.get("first_name", "") + " " + f.get("last_name", "")).strip() + (f" (@{f['username']})" if f.get("username") else "")
        c = db()
        cur = c.execute("UPDATE orders SET status='taken',taken_by=? WHERE id=? AND status='new'", (who, oid))
        c.commit()
        if cur.rowcount == 1:
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Buyurtma sizga biriktirildi")
            tg("editMessageReplyMarkup", chat_id=cid, message_id=q["message"]["message_id"], reply_markup={"inline_keyboard": []})
            tg("sendMessage", chat_id=cid, reply_to_message_id=q["message"]["message_id"], text=f"✅ Buyurtma #{oid} ni {who} qabul qildi.")
        else:
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Bu buyurtmani boshqa menejer olgan", show_alert=True)


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
def setup():  # brauzerda bir marta ochiladi
    return jsonify(tg("setWebhook", url=f"{SITE_URL}/webhook/{SECRET}", allowed_updates=["message", "callback_query"]))


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
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (tu["id"],)).fetchone()
    cur = c.execute("INSERT INTO orders(user_id,created) VALUES(?,?)", (tu["id"], int(time.time())))
    c.commit()
    oid = cur.lastrowid
    name = (u and u["name"]) or tu.get("first_name", "")
    phone = (u and u["phone"]) or "-"
    un = f"@{tu['username']}" if tu.get("username") else ""
    head = [f"🆕 Buyurtma #{oid}", f"👤 {name} {un}".strip(), f"📞 {phone}"]
    head.append({"delivery": "🚚 YETKAZIB BERISH: menejer mijoz bilan bog'lansin",
                 "pickup": "🏬 O'ZI OLIB KETADI: yukni rasxod qilib saqlab qo'ying"}.get(mode, "Usul tanlanmagan"))
    if not (u and u["phone"]):
        head.append("⚠️ Botda to'liq ro'yxatdan o'tmagan")
    lines, last = list(head), None
    for s, col, n, qty, inote in items:
        key = f"{s} · {col}" if col else str(s)
        if key != last:
            lines += ["", f"▪️ {key[:120]}"]
            last = key
        try:
            qty = max(1, min(int(qty), 1000000))
        except Exception:
            qty = 1
        lines.append(f"• {str(n)[:150]} — {qty} ta" + (f" (izoh: {str(inote)[:150]})" if inote else ""))
    if note:
        lines += ["", f"💬 Izoh: {note}"]
    chunks, cur_txt = [], ""
    for ln in lines:
        if len(cur_txt) + len(ln) > 3500:
            chunks.append(cur_txt)
            cur_txt = ""
        cur_txt += ln + "\n"
    chunks.append(cur_txt)
    sent = True
    for k, ch in enumerate(chunks):
        kw = {"chat_id": OFFICE_CHAT_ID, "text": ch}
        if k == len(chunks) - 1:
            kw["reply_markup"] = {"inline_keyboard": [[{"text": "✅ Men olaman", "callback_data": f"take:{oid}"}]]}
        sent = sent and tg("sendMessage", **kw).get("ok", False)
    if not sent:
        c.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
        c.commit()
        return jsonify(ok=False, error="office"), 502
    tg("sendMessage", chat_id=tu["id"], text=f"✅ Buyurtmangiz #{oid} qabul qilindi. " + (
        "Menejerimiz tez orada siz bilan bog'lanadi." if mode == "delivery" else "Yukingiz tayyorlab qo'yiladi, menejerimiz xabar beradi."))
    return jsonify(ok=True, id=oid)


@app.get("/")
def index():
    r = send_from_directory(app.root_path, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/health")
def health():
    return "ok"
