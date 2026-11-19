import os, re, sys, time, shlex, signal, shutil, sqlite3, atexit, threading, subprocess, zipfile, urllib.request, pty, fcntl, termios, struct
from contextlib import closing
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import psutil, requests
from flask import (Flask, render_template, request, redirect, url_for, session,
                   jsonify, Response, send_from_directory, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

SITE_NAME = os.environ.get("SITE_NAME", "BALESHWAR GAMING")

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR
DB_FILE = DATA_DIR / "hosting.db"
PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(MAX_CONTENT_LENGTH=200 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
                  PERMANENT_SESSION_LIFETIME=timedelta(days=365))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KINDS = {"bot": "Bot Hosting", "site": "Website Hosting"}
RT_NAME = {"static": "HTML Website", "python": "Python", "node": "Node.js"}
MAIN_CANDIDATES = ["app.py", "main.py", "bot.py", "run.py", "server.py", "index.js", "server.js", "app.js", "main.js", "bot.js"]
DEFAULT_SETTINGS = {
    "default_limit": "5", "maintenance": "0", "help_text": "Koi problem ho to helpline pe contact karo",
    "whatsapp": "919598263154", "yt": "", "ig": "", "telegram": "", "website": "",
}
RUNNING = {}
STARTING = set()
LOCK = threading.RLock()


# ============================ DATABASE ============================
def db():
    c = sqlite3.connect(DB_FILE, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def q(sql, args=(), one=False):
    with LOCK, closing(db()) as c:
        rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with LOCK, closing(db()) as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


def init_db():
    with LOCK, closing(db()) as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, name TEXT, pw TEXT,
            project_limit INTEGER, banned INTEGER DEFAULT 0, created TEXT, last_login TEXT, last_ip TEXT);
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, owner_id INTEGER,
            kind TEXT, runtime TEXT, public INTEGER, status TEXT DEFAULT 'stopped', main_file TEXT DEFAULT '',
            command TEXT DEFAULT '', port INTEGER, pid INTEGER, autostart INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0, created TEXT);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, rating INTEGER,
            text TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        """)
        c.commit()


init_db()


def meta_get(k):
    r = q("SELECT v FROM meta WHERE k=?", (k,), one=True)
    return r["v"] if r else None


def meta_set(k, v):
    x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, v, v))


# Secret key apne aap ban ke database me save hoti hai (env me kuch set nahi karna)
if not meta_get("secret"):
    meta_set("secret", os.urandom(32).hex())
app.secret_key = meta_get("secret")


def settings():
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT k,v FROM settings"):
        if r["k"] in DEFAULT_SETTINGS:
            s[r["k"]] = r["v"]
    return s


def now():
    return datetime.now().isoformat(timespec="seconds")


# ============================ HELPERS ============================
def pdir(p):
    return PROJECTS_DIR / str(p["id"])


def fdir(p):
    return pdir(p) / "files"


def logpath(p):
    return pdir(p) / "logs.txt"


def dir_size(path):
    t = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


def alive(pid):
    try:
        if not pid:
            return False
        pr = psutil.Process(pid)
        return pr.is_running() and pr.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_pid(pid):
    try:
        pr = psutil.Process(pid)
        for ch in pr.children(recursive=True):
            try: ch.kill()
            except Exception: pass
        pr.terminate()
        try: pr.wait(5)
        except psutil.TimeoutExpired: pr.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def live_status(p):
    """running / starting / stopped / live (static) / blocked"""
    if p["blocked"]:
        return "blocked"
    if p["runtime"] == "static":
        return "live"
    if p["id"] in STARTING:
        return "starting"
    return "running" if alive(p["pid"]) else "stopped"


def alloc_port():
    used = {r["port"] for r in q("SELECT port FROM projects")}
    for pt in range(9100, 9900):
        if pt not in used:
            return pt
    return 9900


def make_slug(name):
    base = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:30] or "app"
    slug = base
    while q("SELECT 1 FROM projects WHERE slug=?", (slug,), one=True):
        slug = f"{base}-{os.urandom(2).hex()}"
    return slug


def public_url(p):
    if not p["public"]:
        return ""
    pre = "/s/" if p["runtime"] == "static" else "/a/"
    return request.host_url.rstrip("/") + pre + p["slug"] + "/"


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]")


def tail(path, n=400):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 150000))
            raw = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return "Console ready. Neeche command likho ya Start dabao."
    txt = ANSI.sub("", raw).replace("\r\n", "\n")
    lines = [l.split("\r")[-1] for l in txt.split("\n")]
    return "\n".join(lines[-n:]) or "Console ready."


HIDDEN = {".deps_installed", ".npm_done", "node_modules", "__pycache__"}


def list_dir(p, rel):
    d = safe_path(p, rel)
    if not d.is_dir():
        return []
    out = []
    for e in sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
        if e.name in HIDDEN:
            continue
        out.append({"name": e.name, "type": "file" if e.is_file() else "dir", "size": e.stat().st_size if e.is_file() else 0})
    return out


def safe_path(p, rel):
    root = fdir(p).resolve()
    t = (root / str(rel).strip("/")).resolve()
    if t != root and root not in t.parents:
        abort(400)
    return t


def find_main(root):
    """app.py / main.py (ya js) khud dhundh leta hai - root me, phir folders ke andar."""
    root = Path(root)
    if not root.exists():
        return ""
    levels = [root] + [d for d in sorted(root.iterdir()) if d.is_dir() and d.name not in HIDDEN]
    for base in levels:
        for c in MAIN_CANDIDATES:
            if (base / c).is_file():
                return (base / c).relative_to(root).as_posix()
    for ext in (".py", ".js"):
        for f in sorted(root.glob("*" + ext)):
            return f.name
    return ""


def autodetect(pid):
    """Upload ke baad project ka type khud sahi kar deta hai."""
    p = q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p or p["blocked"]:
        return False
    root = fdir(p)
    mf = find_main(root)
    rt, pub, main = p["runtime"], p["public"], p["main_file"]
    if p["kind"] == "site":
        if (root / "index.html").exists() or not mf:
            rt, pub = "static", 1
        else:
            rt, pub = ("node" if mf.endswith(".js") else "python"), 1
    elif mf:
        rt = "node" if mf.endswith(".js") else "python"
    if rt != "static" and (not main or not (root / main).is_file()):
        main = mf
    changed = rt != p["runtime"]
    if changed and alive(p["pid"]):
        stop(p)
    x("UPDATE projects SET runtime=?, public=?, main_file=? WHERE id=?", (rt, pub, main if rt != "static" else "", pid))
    return changed


# ============================ PROCESS MGMT (pty = Termux jaisa console) ============================
def hostlog(p, msg):
    try:
        pdir(p).mkdir(parents=True, exist_ok=True)
        with open(logpath(p), "ab") as f:
            f.write(f"\n[HOST {datetime.now():%H:%M:%S}] {msg}\n".encode())
    except Exception:
        pass


def run_install(p, workdir, root, lg):
    for base in (workdir, root):
        if p["runtime"] == "python" and (base / "requirements.txt").exists():
            req, marker = base / "requirements.txt", base / ".deps_installed"
            cmd = [sys.executable, "-m", "pip", "install", "--no-input", "-r", "requirements.txt"]
        elif p["runtime"] == "node" and (base / "package.json").exists():
            req, marker = base / "package.json", base / ".npm_done"
            cmd = ["npm", "install", "--no-audit", "--no-fund"]
        else:
            continue
        if marker.exists() and marker.stat().st_mtime >= req.stat().st_mtime:
            return
        lg.write(f"\n[HOST] requirements mili - install ho raha hai...\n"); lg.flush()
        try:
            r = subprocess.run(cmd, stdout=lg, stderr=lg, cwd=str(base), timeout=170)
            marker.write_text("ok")
            lg.write("[HOST] install OK\n" if r.returncode == 0 else "[HOST] install me error aaya (upar dekho)\n")
        except Exception as e:
            lg.write(f"[HOST] install error: {e}\n")
        lg.flush()
        return
    # requirements nahi hai -> seedha chalao


def check_launch(p):
    if p["blocked"]:
        return False, "Admin ne ye project block kiya hai."
    if p["runtime"] == "static":
        return False, "Website ko start ki zarurat nahi, ye hamesha live hai"
    root = fdir(p)
    mf = p["main_file"] if p["main_file"] and (root / p["main_file"]).is_file() else find_main(root)
    if not mf:
        return False, "Main file (app.py / main.py) nahi mili. Pehle file ya zip upload karo."
    return True, mf


def kill_group(proc):
    try: os.killpg(proc.pid, signal.SIGTERM)
    except Exception: pass
    try: proc.wait(timeout=5)
    except Exception:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except Exception: pass


def pump(pid_, master, proc, logf):
    try:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            logf.write(data); logf.flush()
    finally:
        rc = proc.wait()
        try:
            logf.write(f"\n[HOST {datetime.now():%H:%M:%S}] ⏹ Process band hua (code {rc})\n".encode()); logf.close()
        except Exception:
            pass
        try: os.close(master)
        except OSError: pass
        e = RUNNING.get(pid_)
        if e and e["proc"] is proc:
            RUNNING.pop(pid_, None)


def launch(p, tag="START", cmd=None):
    root = fdir(p)
    if cmd is None:
        ok, mf = check_launch(p)
        if not ok:
            STARTING.discard(p["id"])
            return False, mf
        script = root / mf
        workdir = script.parent
        cmd = shlex.split(p["command"]) if p["command"] else (
            ["node", script.name] if p["runtime"] == "node" else [sys.executable, "-u", script.name])
    else:
        mf, workdir = p["main_file"], root
    STARTING.add(p["id"])
    try:
        pdir(p).mkdir(parents=True, exist_ok=True)
        lp = logpath(p)
        if lp.exists() and lp.stat().st_size > 3_000_000:
            lp.write_text("")
        if tag != "CMD":
            with open(lp, "a") as lg:
                run_install(p, workdir, root, lg)
        hostlog(p, f"{tag}: {' '.join(cmd) if tag != 'CMD' else '$ ' + cmd[-1]}   (PORT={p['port']})")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(pdir(p)), "PORT": str(p["port"]),
               "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8", "TERM": "xterm", "COLUMNS": "100", "LINES": "40"}
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))

        def pre():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # getpass / password prompt ke liye
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=slave, stdout=slave, stderr=slave, env=env,
                                preexec_fn=pre, close_fds=True)
        os.close(slave)
        logf = open(lp, "ab")
        RUNNING[p["id"]] = {"proc": proc, "master": master}
        threading.Thread(target=pump, args=(p["id"], master, proc, logf), daemon=True).start()
        x("UPDATE projects SET status='running', pid=?, main_file=? WHERE id=?", (proc.pid, mf or "", p["id"]))
        if tag != "CMD":
            def banner():
                time.sleep(4)
                if proc.poll() is None:
                    hostlog(p, f"✅ {'Bot' if p['kind'] == 'bot' else 'App'} successfully chal raha hai - LIVE (PID {proc.pid})")
            threading.Thread(target=banner, daemon=True).start()
        return True, proc.pid
    except Exception as e:
        hostlog(p, f"❌ Start nahi hua: {e}")
        return False, str(e)
    finally:
        STARTING.discard(p["id"])


def stop(p, quiet=False):
    e = RUNNING.pop(p["id"], None)
    if e:
        if not quiet:
            hostlog(p, "⏹ Stop kiya gaya")
        kill_group(e["proc"])
    elif p["pid"]:
        kill_pid(p["pid"])
    x("UPDATE projects SET status='stopped', pid=NULL, autostart=0 WHERE id=?", (p["id"],))


def shutdown_all():
    for e in list(RUNNING.values()):
        try: os.killpg(e["proc"].pid, signal.SIGTERM)
        except Exception: pass


atexit.register(shutdown_all)
CRASHES = {}


def monitor():
    time.sleep(10)
    while True:
        try:
            for p in q("SELECT * FROM projects WHERE runtime!='static' AND autostart=1 AND blocked=0"):
                if alive(p["pid"]) or p["id"] in STARTING:
                    continue
                hist = [t for t in CRASHES.get(p["id"], []) if time.time() - t < 600]
                if len(hist) >= 8:  # crash-loop protection
                    x("UPDATE projects SET autostart=0, status='stopped', pid=NULL WHERE id=?", (p["id"],))
                    hostlog(p, "❌ Bar bar crash ho raha hai, auto-restart band. Logs dekh ke fix karo aur Start dabao.")
                    continue
                hist.append(time.time()); CRASHES[p["id"]] = hist
                launch(p, tag="AUTO-RESTART")
            for p in q("SELECT * FROM projects WHERE runtime!='static' AND status='running' AND autostart=0"):
                if not alive(p["pid"]):
                    x("UPDATE projects SET status='stopped', pid=NULL WHERE id=?", (p["id"],))
        except Exception as e:
            print("[monitor]", e)
        time.sleep(20)


def keep_alive():
    ext = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    port = os.environ.get("PORT", "5007")
    while True:
        time.sleep(540)
        for u in (f"http://127.0.0.1:{port}/healthz", f"{ext}/healthz" if ext else None):
            if u:
                try: urllib.request.urlopen(u, timeout=10)
                except Exception: pass


def recovery():
    time.sleep(3)
    for p in q("SELECT * FROM projects WHERE runtime!='static'"):
        x("UPDATE projects SET pid=NULL, status='stopped' WHERE id=?", (p["id"],))
    for p in q("SELECT * FROM projects WHERE runtime!='static' AND autostart=1 AND blocked=0"):
        launch(q("SELECT * FROM projects WHERE id=?", (p["id"],), one=True), tag="RECOVERY")


for t in (monitor, keep_alive, recovery):
    threading.Thread(target=t, daemon=True).start()


# ============================ AUTH ============================

def cur_user():
    uid = session.get("uid")
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None


def is_admin():
    return bool(session.get("admin"))


def user_required(f):
    @wraps(f)
    def w(*a, **k):
        u = cur_user()
        if not u:
            session.pop("uid", None)
            if request.path.startswith("/api/"):
                return jsonify(success=False, error="Login expire ho gaya, dobara login karo"), 401
            return redirect(url_for("login"))
        if u["banned"]:
            session.clear()
            return render_template("auth.html", mode="login", error="Aapka account admin ne block kiya hai.")
        if settings()["maintenance"] == "1" and not is_admin():
            return render_template("maintenance.html")
        return f(*a, **k)
    return w


def access_required(f):
    """Project APIs: admin ya project ka owner (owner ki ban/maintenance checks ke saath)."""
    @wraps(f)
    def w(*a, **k):
        if is_admin():
            return f(*a, **k)
        return user_required(f)(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not is_admin():
            if request.path.startswith("/api/"):
                return jsonify(success=False, error="Admin only"), 403
            return redirect(url_for("admin_panel"))
        return f(*a, **k)
    return w


@app.context_processor
def inject():
    return {"site_name": SITE_NAME, "S": settings(), "me": cur_user() if "uid" in session else None,
            "admin": is_admin()}


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    if not request.path.startswith(("/s/", "/a/")):
        r.headers["X-Frame-Options"] = "SAMEORIGIN"
    return r


@app.route("/healthz")
@app.route("/api/ping")
def ping():
    return "pong"


@app.route("/")
def landing():
    revs = q("""SELECT r.*, u.name FROM reviews r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 12""")
    avg = q("SELECT AVG(rating) a, COUNT(*) c FROM reviews", one=True)
    return render_template("landing.html", reviews=revs, avg=round(avg["a"] or 0, 1), count=avg["c"],
                           n_users=q("SELECT COUNT(*) c FROM users", one=True)["c"],
                           n_proj=q("SELECT COUNT(*) c FROM projects", one=True)["c"])


def remember():
    session.permanent = True  # hamesha login rahe - jab tak khud logout na karo


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:40]
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        err = None
        if not name: err = "Naam likho"
        elif not EMAIL_RE.match(email): err = "Sahi Gmail likho"
        elif len(pw) < 6: err = "Password kam se kam 6 character ka rakho"
        elif email == (meta_get("admin_email") or ""): err = "Ye Gmail use nahi ho sakta"
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True): err = "Ye Gmail pehle se registered hai"
        if err:
            return render_template("auth.html", mode="signup", error=err, name=name, email=email)
        uid = x("INSERT INTO users(email,name,pw,project_limit,created,last_login,last_ip) VALUES(?,?,?,?,?,?,?)",
                (email, name, generate_password_hash(pw), int(settings()["default_limit"]), now(), now(), request.remote_addr or "?"))
        session.clear(); session["uid"] = uid; remember()
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="signup", error=None)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if not u:
            return render_template("auth.html", mode="login", error="Incorrect Gmail", email=email)
        if not check_password_hash(u["pw"], request.form.get("password", "")):
            return render_template("auth.html", mode="login", error="Incorrect Password", email=email)
        if u["banned"]:
            return render_template("auth.html", mode="login", error="Aapka account admin ne block kiya hai.", email=email)
        x("UPDATE users SET last_login=?, last_ip=? WHERE id=?", (now(), request.remote_addr or "?", u["id"]))
        session.clear(); session["uid"] = u["id"]
        if request.form.get("remember", "on") == "on":
            remember()
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="login", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


def admin_login_view():
    """Pehli baar jo Gmail+password daalo wahi hamesha ke liye admin. Baaki sab 'Incorrect'."""
    saved_email, saved_pw = meta_get("admin_email"), meta_get("admin_pw")
    first = not saved_email
    err = None
    em = request.form.get("email", "").strip().lower()
    if request.method == "POST":
        pw = request.form.get("password", "")
        if first:
            if not EMAIL_RE.match(em) or len(pw) < 6:
                err = "Sahi Gmail aur kam se kam 6 character ka password daalo"
            else:
                meta_set("admin_email", em); meta_set("admin_pw", generate_password_hash(pw))
                session["admin"] = True; remember()
                return redirect(url_for("admin_panel"))
        else:
            bad_e, bad_p = em != saved_email, not check_password_hash(saved_pw, pw)
            if not bad_e and not bad_p:
                session["admin"] = True
                if request.form.get("remember", "on") == "on":
                    remember()
                return redirect(url_for("admin_panel"))
            err = "Incorrect Gmail / Password" if (bad_e and bad_p) else ("Incorrect Gmail" if bad_e else "Incorrect Password")
    return render_template("auth.html", mode="admin", error=err, first=first, email=em)


@app.route("/admin-penel/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("landing"))


# ============================ USER DASHBOARD ============================
def type_label(p):
    return f"{KINDS.get(p['kind'], '?')} · {RT_NAME.get(p['runtime'], '?')}"


def project_rows(where="", args=()):
    rows = q(f"SELECT p.*, u.email owner_email, u.name owner_name FROM projects p LEFT JOIN users u ON u.id=p.owner_id {where} ORDER BY p.id DESC", args)
    out = []
    for p in rows:
        d = dict(p)
        d["live"] = live_status(p)
        d["url"] = public_url(p)
        d["size"] = dir_size(fdir(p)) if fdir(p).exists() else 0
        d["label"] = type_label(p)
        out.append(d)
    return out


@app.route("/dashboard")
@user_required
def dashboard():
    u = cur_user()
    ps = project_rows("WHERE p.owner_id=?", (u["id"],))
    mine = q("SELECT * FROM reviews WHERE user_id=?", (u["id"],), one=True)
    return render_template("dashboard.html", u=u, bots=[p for p in ps if p["kind"] == "bot"],
                           sites=[p for p in ps if p["kind"] == "site"], total=len(ps), review=mine)


@app.route("/api/stats")
def stats():
    if not (session.get("uid") or is_admin()):
        return jsonify(success=False), 401
    return jsonify(cpu=psutil.cpu_percent(interval=0.1), ram=psutil.virtual_memory().percent, disk=psutil.disk_usage("/").percent)


@app.route("/api/review", methods=["POST"])
@user_required
def post_review():
    d = request.get_json(silent=True) or {}
    try:
        r = max(1, min(5, int(d.get("rating", 5))))
    except (TypeError, ValueError):
        r = 5
    text = str(d.get("text", "")).strip()[:300]
    u = cur_user()
    x("INSERT INTO reviews(user_id,rating,text,created) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET rating=?, text=?, created=?",
      (u["id"], r, text, now(), r, text, now()))
    return jsonify(success=True)


# ============================ PROJECTS ============================
def get_proj(pid):
    """Owner ya admin hi access kar sakta hai."""
    p = q("SELECT * FROM projects WHERE id=?", (pid,), one=True)
    if not p:
        abort(404)
    if not is_admin():
        u = cur_user()
        if not u or u["id"] != p["owner_id"]:
            abort(403)
    return p


def err(msg, code=400):
    return jsonify(success=False, error=msg), code


@app.route("/api/projects", methods=["POST"])
@user_required
def create_project():
    u = cur_user()
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "")).strip()[:40]
    kind = d.get("kind") if d.get("kind") in KINDS else "bot"
    if not re.match(r"^[A-Za-z0-9 ._-]{2,40}$", name):
        return err("Naam 2-40 letters/numbers ka rakho")
    n = q("SELECT COUNT(*) c FROM projects WHERE owner_id=?", (u["id"],), one=True)["c"]
    if n >= u["project_limit"]:
        return err(f"Aapki limit {u['project_limit']} projects ki hai. Limit badhwane ke liye admin se contact karo.", 403)
    rt, pub = ("static", 1) if kind == "site" else ("python", 0)
    p_id = x("INSERT INTO projects(slug,name,owner_id,kind,runtime,public,port,created) VALUES(?,?,?,?,?,?,?,?)",
             (make_slug(name), name, u["id"], kind, rt, pub, alloc_port(), now()))
    fdir(q("SELECT * FROM projects WHERE id=?", (p_id,), one=True)).mkdir(parents=True, exist_ok=True)
    return jsonify(success=True, id=p_id)


@app.route("/project/<int:pid>")
def project_page(pid):
    if not (session.get("uid") or is_admin()):
        return redirect(url_for("login"))
    p = get_proj(pid)
    d = dict(p)
    d["live"] = live_status(p); d["url"] = public_url(p); d["label"] = type_label(p)
    owner = q("SELECT * FROM users WHERE id=?", (p["owner_id"],), one=True)
    return render_template("project.html", p=d, owner=owner)


def extract_zip(zpath, dest, strip_top=True):
    """Zip apne aap extract; agar sab kuch ek hi folder ke andar ho to wo folder hata deta hai."""
    dest = dest.resolve()
    out = []
    with zipfile.ZipFile(zpath) as z:
        infos = [m for m in z.infolist() if m.filename and not m.filename.startswith("__MACOSX") and "/__MACOSX" not in m.filename]
        names = [m.filename for m in infos if not m.filename.endswith("/")]
        tops = {n.split("/")[0] for n in names}
        strip = (tops.pop() + "/") if strip_top and len(tops) == 1 and all("/" in n for n in names) else ""
        for m in infos:
            n = m.filename
            if n.endswith("/"):
                continue
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            t = (dest / rel).resolve()
            if dest not in t.parents:
                raise ValueError("Zip me galat path mila")
            t.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(t, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(rel)
    return out


@app.route("/api/projects/<int:pid>/upload", methods=["POST"])
@access_required
def upload(pid):
    p = get_proj(pid)
    files = request.files.getlist("file")
    if not files:
        return err("File nahi mili")
    root = fdir(p); root.mkdir(parents=True, exist_ok=True)
    dest = safe_path(p, request.form.get("dir", ""))
    dest.mkdir(parents=True, exist_ok=True)
    done, zips = [], 0
    try:
        for f in files:
            fn = secure_filename(f.filename or "")
            if not fn:
                continue
            if fn.lower().endswith(".zip"):
                tmp = pdir(p) / ("up_" + fn); f.save(tmp)
                try:
                    done += extract_zip(tmp, dest, strip_top=(dest == root.resolve())); zips += 1
                except zipfile.BadZipFile:
                    return err("Ye zip file kharab hai")
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                f.save(dest / fn); done.append(fn)
    except Exception as e:
        return err(f"Upload fail: {e}", 500)
    changed = autodetect(pid)
    return jsonify(success=True, count=len(done), zips=zips, changed=changed)


@app.route("/api/projects/<int:pid>/settings", methods=["POST"])
@access_required
def p_settings(pid):
    p = get_proj(pid)
    d = request.get_json(silent=True) or {}
    mf = str(d.get("main_file", "")).strip()
    if mf and not safe_path(p, mf).is_file():
        return err("Ye main file upload me nahi mili")
    pub = 1 if d.get("public") else 0
    if p["runtime"] == "static":
        pub = 1
    x("UPDATE projects SET main_file=?, command=?, public=? WHERE id=?", (mf, str(d.get("command", "")).strip()[:200], pub, pid))
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/start", methods=["POST"])
@access_required
def p_start(pid):
    p = get_proj(pid)
    if alive(p["pid"]) or pid in STARTING:
        return err("Pehle se chal raha hai")
    ok, res = check_launch(p)
    if not ok:
        return err(res)
    x("UPDATE projects SET autostart=1 WHERE id=?", (pid,))
    STARTING.add(pid)  # install lamba ho sakta hai - background me chalega
    threading.Thread(target=launch, args=(p,), daemon=True).start()
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/stop", methods=["POST"])
@access_required
def p_stop(pid):
    stop(get_proj(pid))
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/input", methods=["POST"])
@access_required
def p_input(pid):
    """Console: chalte process ko input (password/Enter/Ctrl+C) ya band hone par shell command."""
    p = get_proj(pid)
    d = request.get_json(silent=True) or {}
    text = str(d.get("text", ""))[:2000]
    e = RUNNING.get(pid)
    if e and e["proc"].poll() is None:
        try:
            os.write(e["master"], b"\x03" if d.get("ctrl_c") else (text + "\n").encode())
        except OSError:
            return err("Console band ho gaya")
        return jsonify(success=True, mode="input")
    if d.get("ctrl_c") or not text.strip():
        return err("Process abhi chal nahi raha")
    if p["runtime"] == "static":
        return err("Website me console nahi hota")
    if p["blocked"]:
        return err("Project block hai")
    if pid in STARTING:
        return err("Start ho raha hai, thoda ruko")
    STARTING.add(pid)
    threading.Thread(target=launch, args=(p, "CMD", ["/bin/sh", "-c", text]), daemon=True).start()
    return jsonify(success=True, mode="command")


@app.route("/api/projects/<int:pid>/delete", methods=["POST"])
@access_required
def p_delete(pid):
    p = get_proj(pid)
    stop(p, quiet=True)
    x("DELETE FROM projects WHERE id=?", (pid,))
    shutil.rmtree(pdir(p), ignore_errors=True)
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/logs")
@access_required
def p_logs(pid):
    p = get_proj(pid)
    return jsonify(logs=tail(logpath(p)), status=live_status(p))


@app.route("/api/projects/<int:pid>/logs/clear", methods=["POST"])
@access_required
def p_logs_clear(pid):
    p = get_proj(pid)
    try: logpath(p).write_text("")
    except Exception: pass
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/files")
@access_required
def p_files(pid):
    return jsonify(files=list_dir(get_proj(pid), request.args.get("dir", "")))


@app.route("/api/projects/<int:pid>/files/delete", methods=["POST"])
@access_required
def p_file_delete(pid):
    p = get_proj(pid)
    t = safe_path(p, (request.get_json(silent=True) or {}).get("path", ""))
    if t == fdir(p).resolve() or not t.exists():
        return err("File nahi mili")
    shutil.rmtree(t) if t.is_dir() else t.unlink()
    autodetect(pid)
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/files/mkdir", methods=["POST"])
@access_required
def p_mkdir(pid):
    p = get_proj(pid)
    path = str((request.get_json(silent=True) or {}).get("path", "")).strip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return err("Folder ka naam sahi likho")
    safe_path(p, path).mkdir(parents=True, exist_ok=True)
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/files/save", methods=["POST"])
@access_required
def p_save(pid):
    p = get_proj(pid)
    d = request.get_json(silent=True) or {}
    path = str(d.get("path", "")).strip("/")
    if not path or path.endswith("/"):
        return err("File ka naam likho")
    t = safe_path(p, path)
    if t.is_dir():
        return err("Is naam ka folder pehle se hai")
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(str(d.get("content", "")), encoding="utf-8")
    changed = autodetect(pid)
    return jsonify(success=True, changed=changed)


@app.route("/api/projects/<int:pid>/files/rename", methods=["POST"])
@access_required
def p_rename(pid):
    p = get_proj(pid)
    d = request.get_json(silent=True) or {}
    src = safe_path(p, d.get("path", ""))
    new = secure_filename(str(d.get("new", "")))
    if not src.exists() or not new or src == fdir(p).resolve():
        return err("Naam sahi likho")
    dst = safe_path(p, (src.parent / new).relative_to(fdir(p).resolve()).as_posix())
    if dst.exists():
        return err("Is naam ki file pehle se hai")
    src.rename(dst)
    autodetect(pid)
    return jsonify(success=True)


@app.route("/api/projects/<int:pid>/files/view")
@access_required
def p_file_view(pid):
    p = get_proj(pid)
    t = safe_path(p, request.args.get("path", ""))
    if not t.is_file():
        return err("File nahi mili", 404)
    if t.stat().st_size > 400_000:
        return err("File bahut badi hai, edit nahi ho sakti")
    return jsonify(success=True, content=t.read_text(errors="replace"))


# ============================ PUBLIC HOSTING ============================
SANDBOX = "sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads"


def hosted_headers(resp):
    # Hosted code dusre users / admin panel ke session ko touch na kar sake
    resp.headers["Content-Security-Policy"] = SANDBOX
    return resp


@app.route("/s/<slug>/", defaults={"path": ""})
@app.route("/s/<slug>/<path:path>")
def serve_static(slug, path):
    p = q("SELECT * FROM projects WHERE slug=? AND runtime='static'", (slug,), one=True)
    if not p or p["blocked"]:
        return render_template("notfound.html"), 404
    root = fdir(p)
    t = safe_path(p, path)
    if t.is_dir() or not path:
        path = (path.rstrip("/") + "/index.html").lstrip("/")
    if not safe_path(p, path).is_file():
        return render_template("notfound.html"), 404
    return hosted_headers(send_from_directory(root, path))


HOP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "set-cookie", "te", "upgrade", "trailer"}


@app.route("/a/<slug>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route("/a/<slug>/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy(slug, path):
    p = q("SELECT * FROM projects WHERE slug=? AND public=1 AND runtime!='static'", (slug,), one=True)
    if not p or p["blocked"]:
        return render_template("notfound.html"), 404
    if not alive(p["pid"]):
        return render_template("notfound.html", msg="Ye app abhi band hai (stopped)."), 503
    hdr = {k: v for k, v in request.headers if k.lower() not in ("host", "cookie", "content-length", "connection", "accept-encoding")}
    try:
        r = requests.request(request.method, f"http://127.0.0.1:{p['port']}/{path}", params=request.args, headers=hdr,
                             data=request.get_data(), allow_redirects=False, timeout=60)
    except requests.RequestException:
        return render_template("notfound.html", msg="App ne abhi response nahi diya. Logs check karo (PORT env use karna zaruri hai)."), 502
    out = {}
    for k, v in r.headers.items():
        if k.lower() in HOP:
            continue
        if k.lower() == "location" and v.startswith("/"):
            v = f"/a/{slug}{v}"
        out[k] = v
    return hosted_headers(Response(r.content, r.status_code, out))


# ============================ ADMIN PANEL ============================
@app.route("/admin-penel", methods=["GET", "POST"])
def admin_panel():
    if not is_admin():
        return admin_login_view()
    users = []
    for u in q("SELECT * FROM users ORDER BY id DESC"):
        d = dict(u)
        d["n"] = q("SELECT COUNT(*) c FROM projects WHERE owner_id=?", (u["id"],), one=True)["c"]
        users.append(d)
    ps = project_rows()
    revs = q("SELECT r.*, u.name, u.email FROM reviews r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC")
    return render_template("admin.html", users=users, projects=ps, reviews=revs,
                           live=sum(1 for p in ps if p["live"] in ("running", "live")),
                           total_size=sum(p["size"] for p in ps))


@app.route("/api/admin/user/<int:uid>", methods=["POST"])
@admin_required
def admin_user(uid):
    d = request.get_json(silent=True) or {}
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        return err("User nahi mila", 404)
    act = d.get("action")
    if act == "limit":
        try: lim = max(0, min(1000, int(d.get("value"))))
        except (TypeError, ValueError): return err("Number daalo")
        x("UPDATE users SET project_limit=? WHERE id=?", (lim, uid))
    elif act in ("ban", "unban"):
        x("UPDATE users SET banned=? WHERE id=?", (1 if act == "ban" else 0, uid))
        if act == "ban":
            for p in q("SELECT * FROM projects WHERE owner_id=?", (uid,)):
                stop(p)
    elif act == "reset_pw":
        pw = str(d.get("value", ""))
        if len(pw) < 6: return err("Password kam se kam 6 character")
        x("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), uid))
    elif act == "delete":
        for p in q("SELECT * FROM projects WHERE owner_id=?", (uid,)):
            stop(p); shutil.rmtree(pdir(p), ignore_errors=True)
        x("DELETE FROM projects WHERE owner_id=?", (uid,))
        x("DELETE FROM reviews WHERE user_id=?", (uid,))
        x("DELETE FROM users WHERE id=?", (uid,))
    else:
        return err("Unknown action")
    return jsonify(success=True)


@app.route("/api/admin/project/<int:pid>/block", methods=["POST"])
@admin_required
def admin_block(pid):
    p = get_proj(pid)
    nb = 0 if p["blocked"] else 1
    if nb:
        stop(p)
    x("UPDATE projects SET blocked=? WHERE id=?", (nb, pid))
    return jsonify(success=True, blocked=nb)


@app.route("/api/admin/review/<int:rid>/delete", methods=["POST"])
@admin_required
def admin_review_del(rid):
    x("DELETE FROM reviews WHERE id=?", (rid,))
    return jsonify(success=True)


@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    d = request.get_json(silent=True) or {}
    for k in DEFAULT_SETTINGS:
        if k in d:
            v = str(d[k]).strip()[:300]
            if k in ("yt", "ig", "telegram", "website") and v and not v.startswith(("http://", "https://")):
                v = "https://" + v
            if k == "default_limit":
                v = str(max(0, min(1000, int(v or 5))))
            if k == "whatsapp":
                v = re.sub(r"\D", "", v)
            if k == "maintenance":
                v = "1" if v in ("1", "true", "True") else "0"
            x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, v, v))
    return jsonify(success=True)


@app.errorhandler(403)
def e403(e):
    return render_template("notfound.html", msg="Access denied"), 403


@app.errorhandler(404)
def e404(e):
    return render_template("notfound.html"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5007)), debug=False)
