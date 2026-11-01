"""𝐖𝐑𝐎𝐗 𝐒𝐌𝐌 - Telegram Mini App (bot ile aynı panel.db veritabanını kullanır).
Kurulum: pip install flask requests   |   Çalıştır: python wrox_smm_site.py   |   Adres: http://localhost:8080
İlk açılışta admin şifresi terminale yazılır (kullanıcı adı: admin). Dosyayı kimseyle paylaşma."""
import os, time, secrets, sqlite3, threading, requests, hmac, hashlib, json
from urllib.parse import parse_qsl
from functools import wraps
from flask import (Flask, request, session, redirect, g, flash, abort, render_template, has_app_context,
                   get_flashed_messages, jsonify)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

BRAND = "𝐖𝐑𝐎𝐗 𝐒𝐌𝐌"
API_URL = os.environ.get("API_URL", "https://zsosyal.com.tr/api/v2")
API_KEY = os.environ.get("ZSOSYAL_KEY", "zs_8942458321_1_f7a2f1f9c5b87d33792f2318ce2ceeb3a51893a2")
TG_TOKEN = os.environ.get("BOT_TOKEN", "9076042998:AAH7YSJK2XyoVnxnvXmeocSTkOOulqbvKjQ")
TG_CHAT = os.environ.get("ADMIN_ID", "8942458321")
DBF = os.environ.get("DB_PATH", "panel.db")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8942458321"))
ROLE = "CASE WHEN id=%d THEN 2 WHEN is_admin=1 THEN 1 ELSE 0 END role" % ADMIN_ID
DEF = {"markup": "1.5", "cur": "₺", "bonus_enabled": "0", "bonus_amount": "0.1", "ref_enabled": "1", "ref_reward": "0.5",
       "ref_mode": "join", "maintenance": "0", "star_value": "0.02", "contact": "itaatediceniz", "stock_enabled": "1",
       "min_dep": "10", "pay": "Ödeme bilgilerini Admin > Ayarlar bölümünden yazabilirsin (IBAN, Papara, kripto vb.)."}
app = Flask(__name__)
SK = ".wrox_secret"
if not os.path.exists(SK):
    open(SK, "w").write(secrets.token_hex(32))
app.secret_key = open(SK).read()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
_tl = threading.local()


def newc():
    c = sqlite3.connect(DBF, timeout=10, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def D():
    if has_app_context():
        if "db" not in g:
            g.db = newc()
        return g.db
    if not hasattr(_tl, "c"):
        _tl.c = newc()
    return _tl.c


@app.teardown_appcontext
def _close(_):
    c = g.pop("db", None)
    if c:
        c.close()


def q(sql, a=(), one=False):
    r = D().execute(sql, a).fetchall()
    return (r[0] if r else None) if one else r


def ex(sql, a=()):
    return D().execute(sql, a)


def S(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), True)
    return r["value"] if r else DEF.get(k, "")


def setS(k, v):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def now():
    return int(time.time())


def init():
    c = newc()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0, banned INTEGER DEFAULT 0,
      joined INTEGER, last_bonus INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service INTEGER, service_name TEXT,
      link TEXT, qty INTEGER, price REAL, provider_order INTEGER, status TEXT DEFAULT 'Pending', refunded INTEGER DEFAULT 0,
      refill_id INTEGER, created INTEGER);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount REAL, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0,
      expires INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created INTEGER);
    CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, ts INTEGER, PRIMARY KEY(code, user_id));
    CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, status TEXT DEFAULT 'open', created INTEGER);
    CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, status TEXT DEFAULT 'pending', created INTEGER);
    CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, name TEXT, category TEXT, rate REAL, mn INTEGER, mx INTEGER,
      hidden INTEGER DEFAULT 0, gone INTEGER DEFAULT 0);""")
    for t, cols in {"users": {"name": "TEXT", "ip": "TEXT", "ips": "TEXT", "referred_by": "INTEGER", "ref_paid": "INTEGER DEFAULT 0",
                              "ref_earned": "REAL DEFAULT 0", "last_seen": "INTEGER"},
                    "tickets": {"subject": "TEXT", "body": "TEXT", "reply": "TEXT"},
                    "deposits": {"note": "TEXT", "stars": "INTEGER DEFAULT 0"}}.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({t})")}
        for col, typ in cols.items():
            if col not in have:
                c.execute(f"ALTER TABLE {t} ADD COLUMN {col} {typ}")
    c.close()


def tg_user(init):
    """Telegram Mini App initData doğrulaması (bot token ile HMAC). Geçersizse None."""
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash", "")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        sec = hmac.new(b"WebAppData", TG_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(sec, chk.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if now() - int(d.get("auth_date", 0)) > 3600:
            return None
        u = json.loads(d["user"])
        u["start"] = d.get("start_param", "")
        return u
    except Exception:
        return None


# ---------------- sağlayıcı
def papi(**p):
    try:
        return requests.post(API_URL, data={"key": API_KEY, **p}, timeout=20).json()
    except Exception as e:
        return {"error": f"API hatası: {e}", "_net": True}


_pb = {"v": None, "t": 0.0, "alert": 0.0}


def pbal(force=False):
    if not force and _pb["v"] is not None and time.time() - _pb["t"] < 30:
        return _pb["v"]
    try:
        _pb.update(v=float(papi(action="balance")["balance"]), t=time.time())
    except Exception:
        pass
    return _pb["v"]


def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception:
        pass


def sync():
    r = papi(action="services")
    if not isinstance(r, list):
        return 0
    ids = []
    for s in r:
        ids.append(int(s["service"]))
        ex("INSERT INTO services(id,name,category,rate,mn,mx,gone) VALUES(?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET "
           "name=excluded.name,category=excluded.category,rate=excluded.rate,mn=excluded.mn,mx=excluded.mx,gone=0",
           (int(s["service"]), s["name"], s["category"], float(s["rate"]), int(s["min"]), int(s["max"])))
    ex("UPDATE services SET gone=1 WHERE id NOT IN (%s)" % ",".join("?" * len(ids)), ids) if ids else None
    return len(ids)


def price(rate):
    return rate * float(S("markup"))


def stock(sv, pb):
    if pb is None or S("stock_enabled") != "1":
        return None
    return max(0, min(sv["mx"], int(pb / sv["rate"] * 1000))) if sv["rate"] > 0 else sv["mx"]


def check_empty():
    pb = pbal(True)
    c = q("SELECT MIN(mn/1000.0*rate) m FROM services WHERE gone=0", one=True)
    if pb is not None and (pb <= 0.01 or (c and c["m"] and pb < c["m"])) and time.time() - _pb["alert"] > 3600:
        _pb["alert"] = time.time()
        tg("⚠️ API'deki bakiyeniz bitti. Lütfen bakiye yatırın.")


def refresh(oid):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if not o or not o["provider_order"]:
        return
    r = papi(action="status", order=o["provider_order"])
    if "status" not in r:
        return
    st, ref = r["status"], 0.0
    ex("UPDATE orders SET status=? WHERE id=?", (st, oid))
    if st in ("Canceled", "Cancelled"):
        ref = o["price"]
    elif st == "Partial":
        try:
            ref = min(o["price"], o["price"] * int(r.get("remains", 0)) / o["qty"])
        except Exception:
            ref = 0
    if ref > 0 and ex("UPDATE orders SET refunded=1 WHERE id=? AND refunded=0", (oid,)).rowcount == 1:
        ex("UPDATE users SET balance=balance+? WHERE id=?", (ref, o["user_id"]))


def create_order(uid, sid, link, qty):
    sv = q("SELECT * FROM services WHERE id=? AND hidden=0 AND gone=0", (sid,), True)
    if not sv:
        return "Hizmet bulunamadı."
    if not sv["mn"] <= qty <= sv["mx"]:
        return f"Miktar {sv['mn']} - {sv['mx']} arasında olmalı."
    if not link.lower().startswith(("http://", "https://")):
        return "Link http:// veya https:// ile başlamalı."
    pb = pbal(True)
    if S("stock_enabled") == "1" and pb is not None and qty / 1000 * sv["rate"] > pb:
        check_empty()
        return "Bu hizmet için şu an stok yok. Lütfen daha sonra tekrar dene."
    cost = round(qty / 1000 * price(sv["rate"]), 4)
    ex("BEGIN IMMEDIATE")
    try:
        if ex("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (cost, uid, cost)).rowcount == 0:
            ex("ROLLBACK")
            return "Yetersiz bakiye. Lütfen bakiye yükle."
        oid = ex("INSERT INTO orders(user_id,service,service_name,link,qty,price,status,created) VALUES(?,?,?,?,?,?,'Creating',?)",
                 (uid, sid, sv["name"], link, qty, cost, now())).lastrowid
        ex("COMMIT")
    except Exception:
        ex("ROLLBACK")
        raise
    r = papi(action="add", service=sid, link=link, quantity=qty)
    if "order" in r:
        ex("UPDATE orders SET provider_order=?, status='Pending' WHERE id=?", (r["order"], oid))
        _pb["t"] = 0
        return None
    if r.get("_net"):
        ex("UPDATE orders SET status='Unknown' WHERE id=?", (oid,))
        tg(f"⚠️ Sipariş #{oid} sonucu belirsiz (ağ hatası). Sağlayıcıda kontrol et, yoksa iade et.")
        return "Sipariş doğrulanıyor, bakiyen korunuyor. Yönetici kontrol edecek."
    ex("DELETE FROM orders WHERE id=?", (oid,))
    ex("UPDATE users SET balance=balance+? WHERE id=?", (cost, uid))
    if any(w in str(r.get("error", "")).lower() for w in ("fund", "balance")):
        check_empty()
        return "Şu an bu hizmet için stok yok, bakiyen iade edildi."
    return "Sipariş başarısız, bakiyen iade edildi: " + str(r.get("error", ""))


def watcher():
    n = 0
    while True:
        time.sleep(600)
        try:
            for o in q("SELECT id FROM orders WHERE provider_order IS NOT NULL AND status NOT IN ('Completed','Canceled','Cancelled','Partial') "
                       "AND created>? ORDER BY id DESC LIMIT 40", (now() - 7 * 86400,)):
                refresh(o["id"])
            check_empty()
            n += 1
            if n % 6 == 0:
                sync()
        except Exception as e:
            print("watcher:", e)


# ---------------- oturum / güvenlik
def me():
    if "u" not in g:
        g.u = q(f"SELECT *, {ROLE} FROM users WHERE id=?", (session.get("uid"),), True) if session.get("uid") else None
    return g.u


def login_req(f):
    @wraps(f)
    def w(*a, **k):
        u = me()
        if not u or u["banned"]:
            session.clear()
            return redirect("/")
        return f(*a, **k)
    return w


def admin_req(level=1):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = me()
            if not u or u["role"] < level:
                abort(403)
            return f(*a, **k)
        return w
    return deco


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def guard():
    if request.method == "POST" and request.path != "/tg/auth" and request.form.get("_c") != session.get("csrf"):
        abort(400)
    u = me()
    if not u and request.path not in ("/", "/tg/auth"):
        return redirect("/")
    if S("maintenance") == "1" and not (u and u["role"] >= 1) and request.path != "/tg/auth":
        return render_template("maint.html"), 503


@app.context_processor
def ctx():
    return dict(brand=BRAND, u=me(), csrf=csrf, S=S, cur=S("cur"), pcount=(q("SELECT COUNT(*) c FROM deposits WHERE status='pending'", one=True)["c"] if me() and me()["role"] else 0))


@app.template_filter("m")
def money(v):
    return f"{(v or 0):,.2f} {S('cur')}"


@app.template_filter("dt")
def dt(t):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(t or 0))


TR = {"Pending": "Beklemede", "In progress": "İşleniyor", "Processing": "İşleniyor", "Completed": "Tamamlandı", "Partial": "Kısmi",
      "Canceled": "İptal", "Cancelled": "İptal", "Creating": "Oluşturuluyor", "Unknown": "Doğrulanıyor"}
app.jinja_env.filters["tr"] = lambda s: TR.get(s, s)


@app.errorhandler(403)
def e403(_):
    return render_template("msg.html", t="Yetkin yok", m="Bu sayfa için yetkin yok."), 403


@app.errorhandler(404)
def e404(_):
    return render_template("msg.html", t="Bulunamadı", m="Aradığın sayfa yok."), 404


@app.errorhandler(400)
def e400(_):
    return render_template("msg.html", t="Geçersiz istek", m="Oturum süresi dolmuş olabilir, sayfayı yenileyip tekrar dene."), 400


# ---------------- sayfalar
@app.route("/")
def home():
    if not me():
        return render_template("gate.html")
    st = q("SELECT (SELECT COUNT(*) FROM users) u,(SELECT COUNT(*) FROM orders) o,(SELECT COUNT(*) FROM services WHERE gone=0 AND hidden=0) s", one=True)
    top = q("SELECT * FROM services WHERE gone=0 AND hidden=0 ORDER BY rate LIMIT 6")
    return render_template("home.html", st=st, top=top, price=price)


@app.route("/hizmetler")
def services():
    return render_template("services.html", sv=q("SELECT * FROM services WHERE gone=0 AND hidden=0 ORDER BY category,name"), price=price, pb=pbal(), stock=stock)


@app.route("/tg/auth", methods=["POST"])
def tg_auth():
    t = tg_user(request.form.get("init", ""))
    if not t:
        return jsonify(ok=False, msg="Doğrulama başarısız. Siteyi Telegram botundan aç."), 403
    uid = int(t["id"])
    ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    name = (t.get("first_name", "") + " " + t.get("last_name", "")).strip()
    new = ex("INSERT OR IGNORE INTO users(id,username,joined) VALUES(?,?,?)", (uid, t.get("username", ""), now())).rowcount == 1
    old = q("SELECT ips, banned FROM users WHERE id=?", (uid,), True)
    if old["banned"]:
        return jsonify(ok=False, msg="Hesabın engellendi."), 403
    ips = [x for x in (old["ips"] or "").split(",") if x and x != ip][-4:] + [ip]
    ex("UPDATE users SET username=?, name=?, ip=?, ips=?, last_seen=? WHERE id=?", (t.get("username", ""), name, ip, ",".join(ips), now(), uid))
    st = t.get("start", "")
    if new and S("ref_enabled") == "1" and st.startswith("ref_") and st[4:].isdigit() and int(st[4:]) != uid:
        if q("SELECT 1 FROM users WHERE id=? AND banned=0", (int(st[4:]),), True):
            ex("UPDATE users SET referred_by=? WHERE id=? AND referred_by IS NULL", (int(st[4:]), uid))
    if S("ref_mode") == "join":
        pay_ref(uid)
    session.clear()
    session["uid"] = uid
    return jsonify(ok=True)


def pay_ref(uid):
    if S("ref_enabled") != "1":
        return
    r = q("SELECT referred_by FROM users WHERE id=?", (uid,), True)
    if not r or not r["referred_by"] or ex("UPDATE users SET ref_paid=1 WHERE id=? AND ref_paid=0", (uid,)).rowcount == 0:
        return
    rw = float(S("ref_reward"))
    ex("UPDATE users SET balance=balance+?, ref_earned=ref_earned+? WHERE id=? AND banned=0", (rw, rw, r["referred_by"]))


@app.route("/panel")
@login_req
def panel():
    u = me()
    st = q("SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM orders WHERE user_id=?", (u["id"],), True)
    return render_template("panel.html", st=st, last=q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (u["id"],)))


@app.route("/yeni", methods=["GET", "POST"])
@login_req
def new():
    if request.method == "POST":
        try:
            err = create_order(me()["id"], int(request.form["service"]), request.form["link"].strip(), int(request.form["qty"]))
        except ValueError:
            err = "Geçersiz değer."
        flash(err or "Siparişin alındı!")
        return redirect("/siparisler" if not err else "/yeni")
    pb = pbal()
    sv = [dict(id=s["id"], name=s["name"], cat=s["category"], p=round(price(s["rate"]), 4), mn=s["mn"], mx=s["mx"], st=stock(s, pb))
          for s in q("SELECT * FROM services WHERE gone=0 AND hidden=0 ORDER BY category,name")]
    return render_template("new.html", sv=sv)


@app.route("/siparisler")
@login_req
def orders():
    return render_template("orders.html", o=q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 200", (me()["id"],)))


@app.route("/siparis/<int:oid>/yenile", methods=["POST"])
@login_req
def o_refresh(oid):
    o = q("SELECT user_id FROM orders WHERE id=?", (oid,), True)
    if o and (o["user_id"] == me()["id"] or me()["role"]):
        refresh(oid)
    return redirect(request.referrer or "/siparisler")


@app.route("/bakiye", methods=["GET", "POST"])
@login_req
def funds():
    u = me()
    if request.method == "POST":
        try:
            a = float(request.form["amount"].replace(",", "."))
        except ValueError:
            a = 0
        if a < float(S("min_dep")) or q("SELECT COUNT(*) c FROM deposits WHERE user_id=? AND status='pending'", (u["id"],), True)["c"] >= 3:
            flash(f"Tutar en az {S('min_dep')} olmalı (en fazla 3 bekleyen talep).")
        else:
            i = ex("INSERT INTO deposits(user_id,amount,note,created) VALUES(?,?,?,?)", (u["id"], a, request.form.get("note", "")[:300], now())).lastrowid
            tg(f"💳 Yeni yükleme talebi #{i}: {u['username']} - {a} {S('cur')}")
            flash("Talebin alındı. Ödemeyi yaptıktan sonra onaylanınca bakiyen eklenir.")
        return redirect("/bakiye")
    return render_template("funds.html", dep=q("SELECT * FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 20", (u["id"],)))


@app.route("/bonus", methods=["POST"])
@login_req
def bonus():
    t, a = now(), float(S("bonus_amount"))
    if S("bonus_enabled") != "1":
        flash("Günlük bonus şu an kapalı.")
    elif ex("UPDATE users SET last_bonus=?, balance=balance+? WHERE id=? AND last_bonus<=?", (t, a, me()["id"], t - 86400)).rowcount:
        flash(f"+{a:.2f} {S('cur')} bonus eklendi!")
    else:
        flash("Bonusu bugün zaten aldın, yarın tekrar gel.")
    return redirect("/panel")


@app.route("/promo", methods=["POST"])
@login_req
def promo():
    c, uid = request.form["code"].strip().upper(), me()["id"]
    p = q("SELECT * FROM promos WHERE code=?", (c,), True)
    ex("BEGIN IMMEDIATE")
    try:
        if not p or not p["active"]:
            msg = "Geçersiz kod."
        elif q("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (c, uid), True):
            msg = "Bu kodu zaten kullandın."
        elif ex("UPDATE promos SET used=used+1 WHERE code=? AND (max_uses=0 OR used<max_uses)", (c,)).rowcount == 0:
            msg = "Kodun limiti dolmuş."
        else:
            ex("INSERT INTO promo_uses(code,user_id,ts) VALUES(?,?,?)", (c, uid, now()))
            ex("UPDATE users SET balance=balance+? WHERE id=?", (p["amount"], uid))
            msg = f"Kod kullanıldı! +{p['amount']:.2f} {S('cur')}"
        ex("COMMIT")
    except Exception:
        ex("ROLLBACK")
        msg = "Bir hata oluştu."
    flash(msg)
    return redirect("/panel")


@app.route("/referans")
@login_req
def referral():
    return render_template("ref.html", n=q("SELECT COUNT(*) c FROM users WHERE referred_by=?", (me()["id"],), True)["c"])


@app.route("/destek", methods=["GET", "POST"])
@login_req
def support():
    u = me()
    if request.method == "POST":
        sbj, body = request.form["subject"].strip()[:100], request.form["body"].strip()[:2000]
        if sbj and body:
            i = ex("INSERT INTO tickets(user_id,text,subject,body,created) VALUES(?,?,?,?,?)", (u["id"], sbj + "\n" + body, sbj, body, now())).lastrowid
            tg(f"🆘 Yeni destek talebi #{i} ({u['username']}): {sbj}")
            flash("Talebin iletildi.")
        return redirect("/destek")
    return render_template("support.html", t=q("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", (u["id"],)))


@app.route("/profil")
@login_req
def profile():
    return render_template("profile.html")


# ---------------- admin
@app.route("/admin")
@admin_req()
def adm():
    st = q("SELECT (SELECT COUNT(*) FROM users) u,(SELECT COALESCE(SUM(balance),0) FROM users) b,(SELECT COUNT(*) FROM orders) o,"
           "(SELECT COALESCE(SUM(price),0) FROM orders WHERE refunded=0) r,(SELECT COUNT(*) FROM deposits WHERE status='pending') d,"
           "(SELECT COUNT(*) FROM tickets WHERE status='open') t,(SELECT COUNT(*) FROM orders WHERE status IN ('Unknown','Creating')) k", one=True)
    pb = pbal(True)
    need = st["b"] / float(S("markup"))
    return render_template("admin.html", st=st, pb=pb, need=need)


@app.route("/admin/kullanicilar")
@admin_req()
def a_users():
    s = request.args.get("q", "").strip()
    return render_template("a_users.html", us=q(f"SELECT *, {ROLE} FROM users WHERE username LIKE ? OR name LIKE ? OR CAST(id AS TEXT)=? OR ip LIKE ? ORDER BY joined DESC LIMIT 200", (f"%{s}%", f"%{s}%", s, f"%{s}%")), s=s)


@app.route("/admin/kullanici/<int:uid>", methods=["POST"])
@admin_req()
def a_user(uid):
    t, a = q(f"SELECT *, {ROLE} FROM users WHERE id=?", (uid,), True), request.form["a"]
    if not t:
        abort(404)
    sup = me()["role"] == 2
    if a == "ban" and t["role"] < me()["role"]:
        ex("UPDATE users SET banned=1-banned WHERE id=?", (uid,))
    elif a in ("add", "sub") and sup:
        try:
            v = float(request.form["v"].replace(",", "."))
            ex("UPDATE users SET balance=MAX(0,balance+?) WHERE id=?", (v if a == "add" else -v, uid))
        except ValueError:
            flash("Geçersiz tutar.")
    elif a == "admin" and sup and t["role"] < 2:
        ex("UPDATE users SET is_admin=1-is_admin WHERE id=?", (uid,))
    flash("Kaydedildi.")
    return redirect(request.referrer or "/admin/kullanicilar")


@app.route("/admin/siparisler")
@admin_req()
def a_orders():
    return render_template("a_orders.html", o=q("SELECT o.*,u.username FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 200"))


@app.route("/admin/iade/<int:oid>", methods=["POST"])
@admin_req(2)
def a_refund(oid):
    o = q("SELECT * FROM orders WHERE id=?", (oid,), True)
    if o and ex("UPDATE orders SET refunded=1 WHERE id=? AND refunded=0", (oid,)).rowcount == 1:
        ex("UPDATE users SET balance=balance+? WHERE id=?", (o["price"], o["user_id"]))
        flash("İade yapıldı.")
    else:
        flash("İade yapılamaz.")
    return redirect("/admin/siparisler")


@app.route("/admin/talepler", methods=["GET", "POST"])
@admin_req(2)
def a_dep():
    if request.method == "POST":
        i, ok = int(request.form["id"]), request.form["a"] == "ok"
        d = q("SELECT * FROM deposits WHERE id=?", (i,), True)
        if d and ex("UPDATE deposits SET status=? WHERE id=? AND status='pending'", ("approved" if ok else "rejected", i)).rowcount:
            if ok:
                ex("UPDATE users SET balance=balance+? WHERE id=?", (d["amount"], d["user_id"]))
                if S("ref_mode") == "deposit":
                    pay_ref(d["user_id"])
            flash("İşlendi.")
        return redirect("/admin/talepler")
    return render_template("a_dep.html", d=q("SELECT d.*,u.username FROM deposits d JOIN users u ON u.id=d.user_id ORDER BY (d.status='pending') DESC, d.id DESC LIMIT 200"))


@app.route("/admin/hizmetler", methods=["GET", "POST"])
@admin_req(2)
def a_serv():
    if request.method == "POST":
        if request.form["a"] == "sync":
            flash(f"{sync()} hizmet senkronlandı.")
        else:
            ex("UPDATE services SET hidden=1-hidden WHERE id=?", (int(request.form["id"]),))
        return redirect("/admin/hizmetler")
    return render_template("a_serv.html", sv=q("SELECT * FROM services WHERE gone=0 ORDER BY category,name"), price=price)


@app.route("/admin/promo", methods=["GET", "POST"])
@admin_req(2)
def a_promo():
    if request.method == "POST":
        a = request.form["a"]
        try:
            if a == "new":
                ex("INSERT INTO promos(code,amount,max_uses) VALUES(?,?,?)", (request.form["code"].strip().upper(), float(request.form["amount"]), int(request.form["max"] or 0)))
            elif a == "tg":
                ex("UPDATE promos SET active=1-active WHERE code=?", (request.form["code"],))
            else:
                ex("DELETE FROM promos WHERE code=?", (request.form["code"],))
        except Exception:
            flash("Geçersiz bilgi ya da kod zaten var.")
        return redirect("/admin/promo")
    return render_template("a_promo.html", p=q("SELECT * FROM promos"))


@app.route("/admin/destek", methods=["GET", "POST"])
@admin_req()
def a_sup():
    if request.method == "POST":
        i = int(request.form["id"])
        if request.form["a"] == "close":
            ex("UPDATE tickets SET status='closed' WHERE id=?", (i,))
        elif request.form.get("reply", "").strip():
            ex("UPDATE tickets SET reply=?, status='answered' WHERE id=?", (request.form["reply"].strip()[:2000], i))
        return redirect("/admin/destek")
    return render_template("a_sup.html", t=q("SELECT t.*,u.username FROM tickets t JOIN users u ON u.id=t.user_id ORDER BY (t.status='open') DESC, t.id DESC LIMIT 100"))


@app.route("/admin/ayarlar", methods=["GET", "POST"])
@admin_req(2)
def a_set():
    if request.method == "POST":
        for k in DEF:
            if k in request.form:
                setS(k, request.form[k].strip())
            elif k in ("bonus_enabled", "ref_enabled", "maintenance", "stock_enabled"):
                setS(k, "0")
        flash("Ayarlar kaydedildi.")
        return redirect("/admin/ayarlar")
    return render_template("a_set.html")


@app.route("/api/v2", methods=["POST"])
def api_v2():
    return jsonify({"error": "Bu panelde API kapalı."}), 404


# ---------------- şablonlar
CSS = """:root{--bg:#0b0b14;--c:#14142a;--c2:#1c1c3a;--t:#eceaff;--m:#9a98c4;--p:#7c3aed;--p2:#06b6d4;--g:#22c55e;--r:#ef4444;--b:#2a2a52}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1000px 500px at 80% -10%,#2b1b66 0,transparent 60%),var(--bg);color:var(--t);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--p2);text-decoration:none}header{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:12px;padding:12px 16px;background:#0b0b14ee;backdrop-filter:blur(10px);border-bottom:1px solid var(--b)}
.logo{font-weight:800;font-size:19px;color:var(--t);letter-spacing:.5px;background:linear-gradient(90deg,#fff,#b9a5ff);-webkit-background-clip:text;color:transparent}
#bg{background:var(--c2);border:1px solid var(--b);color:var(--t);width:42px;height:42px;border-radius:12px;font-size:22px;cursor:pointer}.sp{flex:1}.pill{background:var(--c2);border:1px solid var(--b);padding:6px 12px;border-radius:99px;font-size:13px}
#ov{position:fixed;inset:0;background:#000a;opacity:0;pointer-events:none;transition:.25s;z-index:40}#ov.o{opacity:1;pointer-events:auto}
#dr{position:fixed;top:0;left:0;bottom:0;width:280px;max-width:84%;background:var(--c);border-right:1px solid var(--b);transform:translateX(-102%);transition:.28s;z-index:50;padding:18px 14px;overflow:auto}#dr.o{transform:none}
#dr a,#dr button.l{display:block;width:100%;text-align:left;padding:11px 12px;border-radius:10px;color:var(--t);background:none;border:0;font:inherit;cursor:pointer}#dr a:hover,#dr button.l:hover{background:var(--c2)}#dr h4{margin:16px 12px 4px;color:var(--m);font-size:12px;text-transform:uppercase;letter-spacing:1px}
main{max-width:980px;margin:0 auto;padding:18px 14px 60px}.card{background:linear-gradient(180deg,var(--c),#101026);border:1px solid var(--b);border-radius:18px;padding:18px;margin-bottom:14px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.stat{background:var(--c2);border-radius:14px;padding:14px}.stat b{display:block;font-size:22px}.stat span{color:var(--m);font-size:13px}
h1,h2{margin:.2em 0 .6em}.btn{display:inline-block;background:linear-gradient(90deg,var(--p),#4f46e5);color:#fff;border:0;border-radius:12px;padding:11px 18px;font:inherit;font-weight:600;cursor:pointer}.btn.s{padding:6px 12px;font-size:13px;border-radius:9px}.btn.g{background:var(--g)}.btn.r{background:var(--r)}.btn.o{background:var(--c2);border:1px solid var(--b)}
input,select,textarea{width:100%;background:#0e0e22;border:1px solid var(--b);color:var(--t);border-radius:12px;padding:11px 12px;font:inherit;margin:4px 0 12px}label{font-size:13px;color:var(--m)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:9px 8px;border-bottom:1px solid var(--b);text-align:left;vertical-align:top}th{color:var(--m);font-weight:600;font-size:12px;text-transform:uppercase}.tw{overflow-x:auto}
.flash{background:#1d3b2a;border:1px solid #2f6b46;padding:11px 14px;border-radius:12px;margin-bottom:12px}.warn{background:#3b1d1d;border:1px solid #7a2e2e}.hero{text-align:center;padding:38px 10px}.hero h1{font-size:34px;line-height:1.15}
.tag{padding:2px 9px;border-radius:99px;font-size:12px;background:var(--c2)}.ok{color:var(--g)}.bad{color:var(--r)}footer{text-align:center;color:var(--m);padding:24px;font-size:13px}form.i{display:inline}
@media(min-width:760px){.hero h1{font-size:46px}}"""

BASE = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://telegram.org/js/telegram-web-app.js"></script><title>{% block t %}{% endblock %} • {{brand}}</title><meta name="application-name" content="{{brand}}">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%237c3aed'/%3E%3Ctext x='32' y='45' font-size='36' text-anchor='middle' fill='white' font-family='Arial' font-weight='700'%3EW%3C/text%3E%3C/svg%3E">
<style>""" + CSS + """</style></head><body>
<header><button id="bg" aria-label="Menü">☰</button><a class="logo" href="/">{{brand}}</a><span class="sp"></span>
{% if u %}<span class="pill">💎 {{u.balance|m}}</span>{% endif %}</header>
<div id="ov"></div><nav id="dr"><div class="logo" style="padding:6px 12px 12px;font-size:22px">{{brand}}</div>
<a href="/">🏠 Ana Sayfa</a><a href="/hizmetler">📋 Hizmetler</a>
{% if u %}<h4>Hesabım</h4><a href="/panel">📊 Panel</a><a href="/yeni">🛍 Yeni Sipariş</a><a href="/siparisler">📦 Siparişlerim</a><a href="/bakiye">💳 Bakiye Yükle</a>
{% if S('ref_enabled')=='1' %}<a href="/referans">👥 Referans</a>{% endif %}<a href="/destek">💬 Destek</a><a href="/profil">⚙️ Profil</a>
{% if u.role %}<h4>{{brand}} Admin</h4><a href="/admin">🛠 Genel Bakış</a><a href="/admin/kullanicilar">👤 Kullanıcılar</a><a href="/admin/siparisler">🧾 Siparişler</a><a href="/admin/destek">🆘 Destek</a>
{% if u.role==2 %}<a href="/admin/talepler">💳 Yükleme Talepleri{% if pcount %} ({{pcount}}){% endif %}</a><a href="/admin/hizmetler">🔁 Hizmetler</a><a href="/admin/promo">🎟 Promo Kodlar</a><a href="/admin/ayarlar">⚙️ Ayarlar</a>{% endif %}{% endif %}
{% endif %}</nav>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{m}}</div>{% endfor %}{% block c %}{% endblock %}</main>
<footer>© {{brand}} • Tüm hakları saklıdır</footer>
<script>var d=document.getElementById('dr'),o=document.getElementById('ov');function t(x){d.classList.toggle('o',x);o.classList.toggle('o',x)}
document.getElementById('bg').onclick=function(){t(!d.classList.contains('o'))};o.onclick=function(){t(false)};d.querySelectorAll('a').forEach(function(a){a.onclick=function(){t(false)}});
document.onkeydown=function(e){if(e.key=='Escape')t(false)};try{var W=Telegram.WebApp;W.ready();W.expand();W.setHeaderColor('#0b0b14');W.setBackgroundColor('#0b0b14')}catch(e){}</script></body></html>"""

TPL = {
"base.html": BASE,
"gate.html": """{% extends 'base.html' %}{% block t %}Giriş{% endblock %}{% block c %}<div class="card hero"><h1>{{brand}}</h1><p id="m">Telegram hesabın doğrulanıyor...</p></div>
<script>var w=window.Telegram&&Telegram.WebApp,m=document.getElementById('m');if(w&&w.initData){fetch('/tg/auth',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'init='+encodeURIComponent(w.initData)}).then(function(r){return r.json()}).then(function(j){if(j.ok)location.replace('/panel');else m.textContent=j.msg||'Giriş başarısız.'}).catch(function(){m.textContent='Bağlantı hatası, tekrar dene.'})}else{m.textContent='Bu site sadece Telegram botumuz üzerinden açılır. Botu aç ve alttaki 𝐖𝐑𝐎𝐗 𝐒𝐌𝐌 butonuna bas.'}</script>{% endblock %}""",
"msg.html": """{% extends 'base.html' %}{% block t %}{{t}}{% endblock %}{% block c %}<div class="card hero"><h1>{{t}}</h1><p>{{m}}</p><a class="btn" href="/">Ana Sayfa</a></div>{% endblock %}""",
"maint.html": """{% extends 'base.html' %}{% block t %}Bakım{% endblock %}{% block c %}<div class="card hero"><h1>🚧 Bakımdayız</h1><p>{{brand}} kısa süre sonra yeniden hizmette.</p></div>{% endblock %}""",
"home.html": """{% extends 'base.html' %}{% block t %}Ana Sayfa{% endblock %}{% block c %}
<div class="card hero"><h1>{{brand}}</h1><p>Instagram, TikTok, Telegram, YouTube ve daha fazlası için hızlı, güvenilir ve uygun fiyatlı sosyal medya hizmetleri.</p>
<p><a class="btn" href="/yeni">🛍 Sipariş Ver</a></p></div>
<div class="grid"><div class="stat"><b>{{st.u}}</b><span>Üye</span></div><div class="stat"><b>{{st.o}}</b><span>Sipariş</span></div><div class="stat"><b>{{st.s}}</b><span>Hizmet</span></div><div class="stat"><b>7/24</b><span>Otomatik teslimat</span></div></div>
<div class="card" style="margin-top:14px"><h2>Nasıl çalışır?</h2><p>1) Kayıt ol • 2) Bakiye yükle • 3) Hizmeti seç, linki ve miktarı gir • 4) Siparişini panelden takip et.</p></div>
{% if top %}<div class="card"><h2>Popüler hizmetler</h2><div class="tw"><table>{% for s in top %}<tr><td>{{s.name}}</td><td>{{ price(s.rate)|m }} / 1000</td></tr>{% endfor %}</table></div><p><a href="/hizmetler">Tüm hizmetler →</a></p></div>{% endif %}
<div class="card"><h2>Sık sorulanlar</h2><p><b>Teslimat ne kadar sürer?</b> Çoğu sipariş dakikalar içinde başlar.<br><b>İptal olursa?</b> Ücret otomatik bakiyene iade edilir.<br><b>Nasıl bakiye yüklerim?</b> Bakiye Yükle sayfasından talep oluşturursun.</p></div>{% endblock %}""",
"services.html": """{% extends 'base.html' %}{% block t %}Hizmetler{% endblock %}{% block c %}<div class="card"><h1>📋 Hizmetler</h1>
<input id="f" placeholder="Hizmet ara..." oninput="var v=this.value.toLowerCase();document.querySelectorAll('#tb tr').forEach(function(r){r.style.display=r.innerText.toLowerCase().indexOf(v)<0?'none':''})">
<div class="tw"><table><thead><tr><th>ID</th><th>Hizmet</th><th>1000 adet</th><th>Min-Max</th><th>Stok</th></tr></thead><tbody id="tb">
{% for s in sv %}{% set k=stock(s,pb) %}<tr><td>{{s.id}}</td><td>{{s.name}}<br><span class="tag">{{s.category}}</span></td><td>{{ price(s.rate)|m }}</td><td>{{s.mn}} - {{s.mx}}</td><td>{% if k is none %}-{% elif k<s.mn %}<span class="bad">Yok</span>{% else %}{{k}}{% endif %}</td></tr>{% endfor %}</tbody></table></div></div>{% endblock %}""",
"auth.html": """{% extends 'base.html' %}{% block t %}{{'Kayıt Ol' if reg else 'Giriş Yap'}}{% endblock %}{% block c %}<div class="card" style="max-width:420px;margin:20px auto"><h1 class="logo" style="font-size:26px">{{brand}}</h1><h2>{{'Kayıt Ol' if reg else 'Giriş Yap'}}</h2>
<form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><label>Kullanıcı adı</label><input name="username" required autocomplete="username"><label>Şifre</label><input name="password" type="password" required autocomplete="{{'new-password' if reg else 'current-password'}}">
<button class="btn" style="width:100%">{{'Kayıt Ol' if reg else 'Giriş Yap'}}</button></form><p style="text-align:center">{% if reg %}Hesabın var mı? <a href="/giris">Giriş yap</a>{% else %}Hesabın yok mu? <a href="/kayit">Kayıt ol</a>{% endif %}</p></div>{% endblock %}""",
"panel.html": """{% extends 'base.html' %}{% block t %}Panel{% endblock %}{% block c %}<h1>Merhaba, {{u.username}} 👋</h1>
<div class="grid"><div class="stat"><b>{{u.balance|m}}</b><span>Bakiye</span></div><div class="stat"><b>{{st.c}}</b><span>Sipariş</span></div><div class="stat"><b>{{st.s|m}}</b><span>Toplam harcama</span></div></div>
<div style="margin:14px 0"><a class="btn" href="/yeni">🛍 Yeni Sipariş</a> <a class="btn o" href="/bakiye">💳 Bakiye Yükle</a></div>
<div class="grid">{% if S('bonus_enabled')=='1' %}<div class="card"><h2>🎁 Günlük Bonus</h2><p>Her gün {{S('bonus_amount')}} {{cur}} kazan.</p><form method="post" action="/bonus"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn g">Bonusu Al</button></form></div>{% endif %}
<div class="card"><h2>🎟 Promo Kod</h2><form method="post" action="/promo"><input type="hidden" name="_c" value="{{csrf()}}"><input name="code" placeholder="KODUNU YAZ" required><button class="btn">Kullan</button></form></div></div>
<div class="card"><h2>Son siparişler</h2><div class="tw"><table>{% for o in last %}<tr><td>#{{o.id}}</td><td>{{o.service_name}}</td><td>{{o.qty}}</td><td><span class="tag">{{o.status|tr}}</span></td></tr>{% else %}<tr><td>Henüz sipariş yok.</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"new.html": """{% extends 'base.html' %}{% block t %}Yeni Sipariş{% endblock %}{% block c %}<div class="card"><h1>🛍 Yeni Sipariş</h1>
<form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><label>Kategori</label><select id="cat"></select><label>Hizmet</label><select name="service" id="sv" required></select>
<div id="inf" class="stat" style="margin-bottom:12px"></div><label>Link</label><input name="link" placeholder="https://..." required><label>Miktar</label><input name="qty" id="qty" type="number" required>
<p>Toplam: <b id="tot">-</b></p><button class="btn">Siparişi Onayla</button></form></div>
<script>var S={{ sv|tojson }},cat=document.getElementById('cat'),sv=document.getElementById('sv'),q=document.getElementById('qty'),cs=[];
S.forEach(function(s){if(cs.indexOf(s.cat)<0)cs.push(s.cat)});cs.forEach(function(c){cat.add(new Option(c,c))});
function fill(){sv.innerHTML='';S.filter(function(s){return s.cat==cat.value}).forEach(function(s){sv.add(new Option(s.name+(s.st!==null&&s.st<s.mn?' (Stok yok)':''),s.id))});upd()}
function cur(){return S.find(function(s){return s.id==sv.value})}
function upd(){var s=cur();if(!s)return;document.getElementById('inf').innerHTML='1000 adet: <b>'+s.p.toFixed(2)+' {{cur}}</b> • Min '+s.mn+' • Max '+s.mx+(s.st!==null?' • Stok: <b>'+(s.st<s.mn?'Yok':s.st)+'</b>':'');q.min=s.mn;q.max=s.st!==null?Math.min(s.mx,s.st):s.mx;
document.getElementById('tot').textContent=q.value?(q.value/1000*s.p).toFixed(2)+' {{cur}}':'-'}
cat.onchange=fill;sv.onchange=upd;q.oninput=upd;if(cs.length)fill()</script>{% endblock %}""",
"orders.html": """{% extends 'base.html' %}{% block t %}Siparişlerim{% endblock %}{% block c %}<div class="card"><h1>📦 Siparişlerim</h1><div class="tw"><table><tr><th>#</th><th>Hizmet</th><th>Miktar</th><th>Tutar</th><th>Durum</th><th></th></tr>
{% for o in o %}<tr><td>{{o.id}}</td><td>{{o.service_name}}<br><small>{{o.link}}</small></td><td>{{o.qty}}</td><td>{{o.price|m}}</td><td><span class="tag">{{o.status|tr}}</span>{% if o.refunded %}<br><small class="ok">İade edildi</small>{% endif %}</td>
<td><form class="i" method="post" action="/siparis/{{o.id}}/yenile"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn s o">🔄</button></form></td></tr>{% else %}<tr><td colspan="6">Henüz siparişin yok.</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"funds.html": """{% extends 'base.html' %}{% block t %}Bakiye Yükle{% endblock %}{% block c %}<div class="card"><h1>💳 Bakiye Yükle</h1><p style="white-space:pre-line">{{S('pay')}}</p>
<form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><label>Tutar ({{cur}}) - en az {{S('min_dep')}}</label><input name="amount" required><label>Açıklama / dekont no</label><input name="note"><button class="btn">Talep Oluştur</button></form></div>
{% if S('contact') %}<div class="card"><h2>⭐ Telegram Yıldız ile yükle</h2><p>Yıldız göndermek için yöneticiye yaz, kullanıcı adını belirt: <b>{{u.username}}</b></p>
<a class="btn" href="https://t.me/{{S('contact')}}?text=Bakiye%20y%C3%BCkleme%20-%20kullan%C4%B1c%C4%B1:%20{{u.username}}">💬 @{{S('contact')}}</a></div>{% endif %}
<div class="card"><h2>Taleplerim</h2><div class="tw"><table>{% for x in dep %}<tr><td>#{{x.id}}</td><td>{{x.amount|m}}</td><td>{{x.status}}</td><td>{{x.created|dt}}</td></tr>{% else %}<tr><td>Talep yok.</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"ref.html": """{% extends 'base.html' %}{% block t %}Referans{% endblock %}{% block c %}<div class="card"><h1>👥 Referans</h1><p>Her davet için <b>{{S('ref_reward')}} {{cur}}</b> kazan ({{'davet edilen kayıt olunca' if S('ref_mode')=='join' else 'davet edilen ilk yüklemeyi yapınca'}}).</p>
<input readonly value="{{request.host_url}}" onclick="this.select()"><div class="grid"><div class="stat"><b>{{n}}</b><span>Davet</span></div><div class="stat"><b>{{u.ref_earned|m}}</b><span>Kazanç</span></div></div></div>{% endblock %}""",
"support.html": """{% extends 'base.html' %}{% block t %}Destek{% endblock %}{% block c %}<div class="card"><h1>💬 Destek</h1><form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><label>Konu</label><input name="subject" required><label>Mesaj</label><textarea name="body" rows="4" required></textarea><button class="btn">Gönder</button></form></div>
{% for x in t %}<div class="card"><b>#{{x.id}} {{x.subject}}</b> <span class="tag">{{x.status}}</span><p style="white-space:pre-line">{{x.body}}</p>{% if x.reply %}<div class="stat"><b>Yanıt</b><span style="white-space:pre-line">{{x.reply}}</span></div>{% endif %}</div>{% endfor %}{% endblock %}""",
"profile.html": """{% extends 'base.html' %}{% block t %}Profil{% endblock %}{% block c %}<div class="card"><h1>⚙️ Profil</h1><p>ID: <b>{{u.id}}</b><br>Ad: <b>{{u.name or '-'}}</b><br>Tag: <b>@{{u.username or '-'}}</b><br>Bakiye: <b>{{u.balance|m}}</b></p><p>Hesabın Telegram hesabına bağlıdır, bot ile aynı bakiyeyi kullanır.</p></div>{% endblock %}""",
"admin.html": """{% extends 'base.html' %}{% block t %}Admin{% endblock %}{% block c %}<h1>🛠 {{brand}} Admin</h1>
{% if pb is none %}<div class="flash warn">Sağlayıcı bakiyesi alınamadı. API anahtarını kontrol et.</div>{% elif pb<=0.01 %}<div class="flash warn">⚠️ API'deki bakiyeniz bitti. Lütfen bakiye yatırın.</div>{% elif pb<need %}<div class="flash warn">Sağlayıcıya yaklaşık {{ (need-pb)|m }} yüklemelisin.</div>{% endif %}
<div class="grid"><div class="stat"><b>{{ 'bilinmiyor' if pb is none else pb|m }}</b><span>Sağlayıcı bakiyesi</span></div><div class="stat"><b>{{st.u}}</b><span>Üye</span></div><div class="stat"><b>{{st.o}}</b><span>Sipariş</span></div><div class="stat"><b>{{st.r|m}}</b><span>Ciro</span></div>
<div class="stat"><b>{{st.b|m}}</b><span>Kullanıcı bakiyeleri</span></div><div class="stat"><b>{{st.d}}</b><span>Bekleyen yükleme</span></div><div class="stat"><b>{{st.t}}</b><span>Açık destek</span></div><div class="stat"><b>{{st.k}}</b><span>Doğrulanacak sipariş</span></div></div>{% endblock %}""",
"a_users.html": """{% extends 'base.html' %}{% block t %}Kullanıcılar{% endblock %}{% block c %}<div class="card"><h1>👤 Kullanıcılar</h1><form><input name="q" value="{{s}}" placeholder="Kullanıcı ara"></form><div class="tw"><table><tr><th>ID</th><th>Ad / Tag / IP</th><th>Bakiye</th><th>İşlem</th></tr>
{% for x in us %}<tr><td>{{x.id}}</td><td>{{x.name or '-'}}<br><small>@{{x.username or '-'}} • {{x.ip or '-'}}</small><br>{% if x.role==2 %}👑{% elif x.role %}🛡{% endif %}{% if x.banned %}<span class="bad">engelli</span>{% endif %}</td><td>{{x.balance|m}}</td><td>
<form class="i" method="post" action="/admin/kullanici/{{x.id}}"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn s r" name="a" value="ban">{{'Engeli kaldır' if x.banned else 'Engelle'}}</button></form>
{% if u.role==2 %}<form class="i" method="post" action="/admin/kullanici/{{x.id}}"><input type="hidden" name="_c" value="{{csrf()}}"><input name="v" placeholder="tutar" style="width:80px;display:inline;padding:6px;margin:0"><button class="btn s g" name="a" value="add">+</button><button class="btn s r" name="a" value="sub">-</button>
{% if x.role<2 %}<button class="btn s o" name="a" value="admin">{{'Admin al' if x.role else 'Admin yap'}}</button>{% endif %}</form>{% endif %}</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"a_orders.html": """{% extends 'base.html' %}{% block t %}Siparişler{% endblock %}{% block c %}<div class="card"><h1>🧾 Siparişler</h1><div class="tw"><table><tr><th>#</th><th>Kullanıcı</th><th>Hizmet</th><th>Tutar</th><th>Durum</th><th></th></tr>
{% for x in o %}<tr><td>{{x.id}}</td><td>{{x.username}}</td><td>{{x.service_name}}<br><small>{{x.link}} • {{x.qty}}</small></td><td>{{x.price|m}}</td><td><span class="tag">{{x.status|tr}}</span>{% if x.refunded %}<br><small class="ok">iade</small>{% endif %}</td>
<td><form class="i" method="post" action="/siparis/{{x.id}}/yenile"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn s o">🔄</button></form>
{% if u.role==2 and not x.refunded %}<form class="i" method="post" action="/admin/iade/{{x.id}}"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn s r">İade</button></form>{% endif %}</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"a_dep.html": """{% extends 'base.html' %}{% block t %}Yükleme Talepleri{% endblock %}{% block c %}<div class="card"><h1>💳 Yükleme Talepleri</h1><div class="tw"><table>{% for x in d %}<tr><td>#{{x.id}}</td><td>{{x.username}}</td><td>{{x.amount|m}}<br><small>{{x.note}}</small></td><td>{{x.created|dt}}</td><td>
{% if x.status=='pending' %}<form class="i" method="post"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="id" value="{{x.id}}"><button class="btn s g" name="a" value="ok">Onayla</button> <button class="btn s r" name="a" value="no">Reddet</button></form>{% else %}{{x.status}}{% endif %}</td></tr>{% else %}<tr><td>Talep yok.</td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"a_serv.html": """{% extends 'base.html' %}{% block t %}Hizmetler{% endblock %}{% block c %}<div class="card"><h1>🔁 Hizmetler</h1><form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><button class="btn" name="a" value="sync">Sağlayıcıdan Senkronla</button></form></div>
<div class="card"><div class="tw"><table><tr><th>ID</th><th>Hizmet</th><th>Maliyet</th><th>Satış</th><th></th></tr>{% for s in sv %}<tr><td>{{s.id}}</td><td>{{s.name}}</td><td>{{s.rate|m}}</td><td>{{ price(s.rate)|m }}</td><td><form class="i" method="post"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="id" value="{{s.id}}"><button class="btn s {{'g' if s.hidden else 'o'}}" name="a" value="tg">{{'Göster' if s.hidden else 'Gizle'}}</button></form></td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"a_promo.html": """{% extends 'base.html' %}{% block t %}Promo Kodlar{% endblock %}{% block c %}<div class="card"><h1>🎟 Promo Kodlar</h1><form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="a" value="new"><label>Kod</label><input name="code" required><label>Miktar</label><input name="amount" required><label>Kullanım limiti (0 = sınırsız)</label><input name="max" value="0"><button class="btn">Oluştur</button></form></div>
<div class="card"><div class="tw"><table>{% for x in p %}<tr><td>{{x.code}}</td><td>{{x.amount|m}}</td><td>{{x.used}}/{{x.max_uses or '∞'}}</td><td><form class="i" method="post"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="code" value="{{x.code}}"><button class="btn s o" name="a" value="tg">{{'Kapat' if x.active else 'Aç'}}</button> <button class="btn s r" name="a" value="del">Sil</button></form></td></tr>{% endfor %}</table></div></div>{% endblock %}""",
"a_sup.html": """{% extends 'base.html' %}{% block t %}Destek{% endblock %}{% block c %}<h1>🆘 Destek Talepleri</h1>{% for x in t %}<div class="card"><b>#{{x.id}} {{x.username}} - {{x.subject}}</b> <span class="tag">{{x.status}}</span><p style="white-space:pre-line">{{x.body}}</p>{% if x.reply %}<div class="stat"><span style="white-space:pre-line">{{x.reply}}</span></div>{% endif %}
{% if x.status!='closed' %}<form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="id" value="{{x.id}}"><textarea name="reply" rows="2" placeholder="Yanıt yaz"></textarea><button class="btn s" name="a" value="reply">Yanıtla</button> <button class="btn s r" name="a" value="close">Kapat</button></form>{% endif %}</div>{% endfor %}{% endblock %}""",
"a_set.html": """{% extends 'base.html' %}{% block t %}Ayarlar{% endblock %}{% block c %}<div class="card"><h1>⚙️ Ayarlar</h1><form method="post"><input type="hidden" name="_c" value="{{csrf()}}">
<label>Fiyat çarpanı (markup)</label><input name="markup" value="{{S('markup')}}"><label>Para birimi sembolü</label><input name="cur" value="{{S('cur')}}"><label>1 Telegram Yıldızı kaç bakiye</label><input name="star_value" value="{{S('star_value')}}">
<label>Minimum yükleme</label><input name="min_dep" value="{{S('min_dep')}}"><label>Yönetici Telegram kullanıcı adı (@ olmadan)</label><input name="contact" value="{{S('contact')}}">
<label>Ödeme bilgileri (IBAN, Papara, kripto...)</label><textarea name="pay" rows="4">{{S('pay')}}</textarea>
<label>Günlük bonus miktarı</label><input name="bonus_amount" value="{{S('bonus_amount')}}"><label>Referans ödülü</label><input name="ref_reward" value="{{S('ref_reward')}}"><label>Referans ödülü zamanı</label><select name="ref_mode"><option value="join" {{'selected' if S('ref_mode')=='join'}}>Kayıt olunca</option><option value="deposit" {{'selected' if S('ref_mode')=='deposit'}}>İlk yüklemede</option></select>
{% for k,l in [('bonus_enabled','Günlük bonus açık'),('ref_enabled','Referans sistemi açık'),('stock_enabled','Stok kontrolü (sağlayıcı bakiyesine göre)'),('maintenance','Bakım modu')] %}<label style="display:block;margin:6px 0"><input type="checkbox" name="{{k}}" value="1" {{'checked' if S(k)=='1'}} style="width:auto;margin:0 8px 0 0"> {{l}}</label>{% endfor %}
<br><button class="btn">Kaydet</button></form></div>{% endblock %}""",
}
app.jinja_loader = DictLoader(TPL)

init()
with app.app_context():
    if not q("SELECT 1 FROM services LIMIT 1", one=True):
        sync()

if __name__ == "__main__":
    threading.Thread(target=watcher, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), threaded=True)
