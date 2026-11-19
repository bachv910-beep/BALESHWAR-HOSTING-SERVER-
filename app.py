import os
import re
import json
import signal
import subprocess
import shutil
import sys
import zipfile
import hashlib
import hmac
import psutil
import threading
import time
import urllib.request
import atexit
from pathlib import Path
from functools import wraps
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

SITE_NAME_DEFAULT = os.environ.get("SITE_NAME", "BALESHWAR GAMING")

app = Flask(__name__, template_folder="templates")
# Render sits behind a proxy -> get real client IP / https scheme
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---- Stable secret key (set SECRET_KEY in Render env) ----
app.secret_key = os.environ.get(
    "SECRET_KEY",
    hashlib.sha256((SITE_NAME_DEFAULT + "-fallback-key").encode()).hexdigest(),
)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

BASE_DIR = Path(__file__).parent

# ---- Persistent data dir (on Render: mount a Disk and set DATA_DIR=/var/data) ----
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR

DATA_FILE = DATA_DIR / "data.json"
SERVERS_DIR = DATA_DIR / "servers"
SERVERS_DIR.mkdir(parents=True, exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "BALESHWAR")
RUNNING_PROCESSES = {}
DATA_LOCK = threading.RLock()
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


# ==================== DATA ====================
def default_data():
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#8b5cf6",
            "auto_restart_interval": 30,
        },
    }


def load_data():
    with DATA_LOCK:
        if DATA_FILE.exists():
            try:
                d = json.loads(DATA_FILE.read_text())
                d.setdefault("servers", {})
                d.setdefault("users", {})
                d.setdefault("settings", {})
                return d
            except Exception:
                pass
        return default_data()


def save_data(data):
    with DATA_LOCK:
        try:
            tmp = DATA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            os.replace(tmp, DATA_FILE)  # atomic write -> no corrupted data.json
        except Exception as e:
            print(f"[save_data error] {e}")


def site_name():
    # Site name always comes from env/default so old data can never override it
    return SITE_NAME_DEFAULT


def initials(name):
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "BG"


def get_theme_color():
    return load_data().get("settings", {}).get("theme_color", "#8b5cf6")


@app.context_processor
def inject_globals():
    n = site_name()
    return {"theme_color": get_theme_color(), "site_name": n, "initials": initials(n)}


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ==================== PROCESS HELPERS ====================
def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def kill_process(pid):
    try:
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            try:
                c.terminate()
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    if runtime == "static":
        return [sys.executable, "-m", "http.server", "8080"]
    return [sys.executable, "-u", main_file]


def install_requirements(name, extract_dir, log_path):
    """Install requirements.txt of the hosted project once (per upload)."""
    req = extract_dir / "requirements.txt"
    marker = extract_dir / ".deps_installed"
    if not req.exists():
        return
    if marker.exists() and marker.stat().st_mtime >= req.stat().st_mtime:
        return
    with open(log_path, "a") as lf:
        lf.write(f"\n[{datetime.now().isoformat()}] Installing requirements.txt ...\n")
        lf.flush()
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-input", "-r", str(req)],
                stdout=lf, stderr=lf, timeout=100, cwd=str(extract_dir))
            if r.returncode == 0:
                marker.write_text("ok")
                lf.write("[deps] installed OK\n")
            else:
                lf.write("[deps] install FAILED (see above)\n")
        except subprocess.TimeoutExpired:
            lf.write("[deps] install timed out, starting anyway\n")
        except Exception as e:
            lf.write(f"[deps] error: {e}\n")


def launch_server(name, cfg, tag="START"):
    """Start the hosted project. Returns (ok, error_or_pid)."""
    mf = cfg.get("main_file") or "main.py"
    ed = SERVERS_DIR / name / "extracted"
    if not (ed / mf).exists():
        return False, f"{mf} not found. Upload files first."
    lp = SERVERS_DIR / name / "logs.txt"
    lp.parent.mkdir(parents=True, exist_ok=True)
    install_requirements(name, ed, lp)
    cmd = cfg.get("main_command", "").split() or get_run_command(cfg.get("runtime", "python"), mf)
    env = os.environ.copy()
    env["PORT"] = str(cfg.get("port", 8080))
    env["PYTHONUNBUFFERED"] = "1"
    try:
        lf = open(lp, "a")
        lf.write(f"\n[{datetime.now().isoformat()}] {tag}: {' '.join(cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, cwd=str(ed), stdout=lf, stderr=lf,
                                env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": lf}
        return True, proc.pid
    except Exception as e:
        return False, str(e)


# ==================== STARTUP RECOVERY ====================
def startup_recovery():
    """After a Render restart/deploy: bring back every project that was running."""
    def worker():
        try:
            data = load_data()
            for name, cfg in list(data.get("servers", {}).items()):
                if cfg.get("status") == "running" or cfg.get("autostart"):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    if cfg.get("autostart"):
                        ok, res = launch_server(name, cfg, tag="RECOVERY-START")
                        if ok:
                            cfg["status"] = "running"
                            cfg["pid"] = res
            save_data(data)
            print(f"[startup] recovery done. data_dir={DATA_DIR}")
        except Exception as e:
            print(f"[startup] error: {e}")
    threading.Thread(target=worker, daemon=True).start()


# ==================== GRACEFUL SHUTDOWN ====================
def shutdown_all():
    print("[shutdown] stopping child processes...")
    for name in list(RUNNING_PROCESSES.keys()):
        entry = RUNNING_PROCESSES.get(name)
        if not entry:
            continue
        try:
            os.killpg(os.getpgid(entry["proc"].pid), signal.SIGTERM)
        except Exception:
            pass
        try:
            entry["log_file"].close()
        except Exception:
            pass


atexit.register(shutdown_all)


# ==================== LOGIN REQUIRED ====================
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("username"):
            if request.path.startswith(("/api/", "/server/")) and request.method != "GET":
                return jsonify({"success": False, "error": "Session expired, login again"}), 401
            return redirect(url_for("login"))
        s = load_data().get("settings", {})
        if s.get("maintenance"):
            return render_template("maintenance.html",
                                   message=s.get("maintenance_msg", "Under maintenance"))
        return f(*a, **kw)
    return wrapper


# ==================== HEALTH / KEEP ALIVE ====================
@app.route("/api/ping")
@app.route("/healthz")
def ping():
    return "pong", 200


def render_keep_alive():
    external_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    local_port = os.environ.get("PORT", "5007")
    while True:
        time.sleep(540)  # < 15 min so free Render instance does not sleep
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{local_port}/api/ping", timeout=10)
        except Exception:
            pass
        if external_url:
            try:
                req = urllib.request.Request(f"{external_url}/api/ping",
                                             headers={"User-Agent": "KeepAlive/1.0"})
                urllib.request.urlopen(req, timeout=15)
            except Exception:
                pass


threading.Thread(target=render_keep_alive, daemon=True).start()


# ==================== AUTO RESTART MONITOR ====================
def auto_restart_monitor():
    """Every few seconds: if a project that should be running has crashed, restart it."""
    time.sleep(15)
    while True:
        try:
            data = load_data()
            interval = max(10, int(data.get("settings", {}).get("auto_restart_interval", 30)))
            changed = False
            for name, cfg in list(data.get("servers", {}).items()):
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    changed = True
                    if cfg.get("autostart"):
                        ok, res = launch_server(name, cfg, tag="AUTO-RESTART")
                        if ok:
                            cfg["status"] = "running"
                            cfg["pid"] = res
            if changed:
                save_data(data)
            time.sleep(interval)
        except Exception as e:
            print(f"[monitor] {e}")
            time.sleep(30)


threading.Thread(target=auto_restart_monitor, daemon=True).start()


# ==================== LOGIN (with brute-force protection) ====================
FAILED = {}  # ip -> [count, first_time]
MAX_TRIES, WINDOW = 8, 600


def too_many(ip):
    e = FAILED.get(ip)
    if not e:
        return False
    if time.time() - e[1] > WINDOW:
        FAILED.pop(ip, None)
        return False
    return e[0] >= MAX_TRIES


@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many(ip):
            return render_template("login.html", error="Too many attempts. Try again in 10 minutes."), 429
        pw = request.form.get("password", "").strip()
        if not hmac.compare_digest(pw.encode(), NORMAL_PASSWORD.encode()):
            e = FAILED.setdefault(ip, [0, time.time()])
            e[0] += 1
            return render_template("login.html", error="Invalid access key")
        FAILED.pop(ip, None)
        data = load_data()
        if "admin" not in data["users"]:
            data["users"]["admin"] = {"joined": datetime.now().isoformat()}
            save_data(data)
        session["username"] = "admin"
        session.permanent = True
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==================== EMERGENCY MAINTENANCE KILL (needs login key) ====================
@app.route("/killmaintenance")
def kill_maintenance():
    key = request.args.get("key", "")
    if not hmac.compare_digest(key.encode(), NORMAL_PASSWORD.encode()):
        return "Use /killmaintenance?key=YOUR_ACCESS_KEY", 403
    data = load_data()
    data["settings"]["maintenance"] = False
    save_data(data)
    return redirect(url_for("login"))


# ==================== DASHBOARD ====================
@app.route("/dashboard")
@login_required
def dashboard():
    u = session["username"]
    data = load_data()
    us = {k: v for k, v in data["servers"].items() if v.get("owner") == u}
    changed = False
    for n, c in us.items():
        if c.get("pid") and not is_process_alive(c["pid"]):
            c["status"] = "stopped"
            c["pid"] = None
            changed = True
    if changed:
        save_data(data)
    r = sum(1 for v in us.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=us, running=r, total=len(us), username=u)


@app.route("/api/stats")
@login_required
def stats():
    return jsonify({
        "cpu": psutil.cpu_percent(interval=0.1),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage("/").percent,
    })


# ==================== CRUD ====================
def get_owned(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return data, None, ({"success": False, "error": "Server not found"}, 404)
    if c.get("owner") != session["username"]:
        return data, None, ({"success": False, "error": "Access denied"}, 403)
    return data, c, None


@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if runtime not in ("python", "node", "static"):
        runtime = "python"
    if not NAME_RE.match(name) or name in (".", ".."):
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    data["servers"][name] = {
        "name": name, "owner": session["username"], "runtime": runtime,
        "status": "stopped", "main_file": "", "main_command": "",
        "port": 8080, "pid": None, "autostart": False,
        "created": datetime.now().isoformat(),
    }
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))


def stop_process(name, c):
    stopped = False
    e = RUNNING_PROCESSES.pop(name, None)
    if e:
        try:
            os.killpg(os.getpgid(e["proc"].pid), signal.SIGTERM)
        except Exception:
            try:
                e["proc"].terminate()
            except Exception:
                pass
        try:
            e["proc"].wait(timeout=5)
        except Exception:
            try:
                e["proc"].kill()
            except Exception:
                pass
        try:
            e["log_file"].close()
        except Exception:
            pass
        stopped = True
    if c.get("pid") and not stopped:
        kill_process(c["pid"])
    c["status"] = "stopped"
    c["pid"] = None


@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    c = data["servers"].get(name)
    if c and c.get("owner") == session["username"]:
        stop_process(name, c)
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))


def list_files(d, base=""):
    r = []
    if not d.exists():
        return r
    try:
        for e in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
            rel = f"{base}/{e.name}" if base else e.name
            if e.is_dir():
                r.append({"name": e.name, "path": rel, "type": "dir", "size": 0})
                r.extend(list_files(e, rel))
            else:
                r.append({"name": e.name, "path": rel, "type": "file", "size": e.stat().st_size})
    except Exception:
        pass
    return r


@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    c = data["servers"].get(name)
    if not c:
        return "Not found", 404
    if c.get("owner") != session["username"]:
        return "Access denied", 403
    if c.get("pid") and not is_process_alive(c["pid"]):
        c["status"] = "stopped"
        c["pid"] = None
        save_data(data)
    c.setdefault("main_command", "")
    ed = SERVERS_DIR / name / "extracted"
    return render_template("server.html", server_name=name, config=c, files=list_files(ed))


def safe_extract(z, dest):
    dest = dest.resolve()
    for m in z.infolist():
        target = (dest / m.filename).resolve()
        if target != dest and dest not in target.parents:
            raise ValueError("Unsafe path in zip")
    z.extractall(dest)


@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify(err[0]), err[1]
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file"}), 400
    f = request.files["file"]
    fname = secure_filename(f.filename or "")
    if not fname:
        return jsonify({"success": False, "error": "Empty filename"}), 400
    ed = SERVERS_DIR / name / "extracted"
    ed.mkdir(parents=True, exist_ok=True)
    up = SERVERS_DIR / name / f"upload_{fname}"
    f.save(up)
    extracted = []
    try:
        if fname.lower().endswith(".zip"):
            with zipfile.ZipFile(up, "r") as z:
                safe_extract(z, ed)
                extracted = [m.filename for m in z.infolist() if not m.is_dir()]
            up.unlink(missing_ok=True)
            COMMON = ["app.py", "main.py", "bot.py", "run.py", "index.js", "server.js", "main.js"]
            if not c.get("main_file"):
                for m in extracted:
                    if Path(m).name in COMMON:
                        c["main_file"] = m
                        break
        else:
            shutil.move(str(up), str(ed / fname))
            extracted = [fname]
            if not c.get("main_file") and fname.endswith((".py", ".js", ".ts")):
                c["main_file"] = fname
        data["servers"][name] = c
        save_data(data)
    except Exception as e:
        up.unlink(missing_ok=True)
        return jsonify({"success": False, "error": f"Failed: {str(e)}"}), 500
    return jsonify({"success": True, "files": extracted, "count": len(extracted)})


@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify(err[0]), err[1]
    p = request.get_json(silent=True) or {}
    c["main_file"] = str(p.get("main_file", c.get("main_file", ""))).strip()
    c["main_command"] = str(p.get("main_command", c.get("main_command", ""))).strip()
    try:
        c["port"] = int(p.get("port", c.get("port", 8080)))
    except (TypeError, ValueError):
        c["port"] = 8080
    save_data(data)
    return jsonify({"success": True})


@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify(err[0]), err[1]
    if c.get("pid") and is_process_alive(c["pid"]):
        return jsonify({"success": False, "error": "Already running"})
    ok, res = launch_server(name, c)
    if not ok:
        return jsonify({"success": False, "error": res})
    c["status"] = "running"
    c["pid"] = res
    c["autostart"] = True  # keep it alive: auto-restart on crash & after redeploy
    save_data(data)
    return jsonify({"success": True, "pid": res})


@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify(err[0]), err[1]
    c["autostart"] = False
    stop_process(name, c)
    save_data(data)
    return jsonify({"success": True})


@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify({"logs": err[0]["error"]})
    lp = SERVERS_DIR / name / "logs.txt"
    if not lp.exists():
        return jsonify({"logs": "No logs yet."})
    try:
        lines = lp.read_text(errors="replace").splitlines()
        content = "\n".join(lines[-300:])
        if len(lines) > 300:
            content = "... (last 300 lines) ...\n" + content
        return jsonify({"logs": content or "No output."})
    except Exception as e:
        return jsonify({"logs": f"Error: {e}"})


@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    data, c, err = get_owned(name)
    if err:
        return jsonify(err[0]), err[1]
    try:
        (SERVERS_DIR / name / "logs.txt").write_text("")
    except Exception:
        pass
    return jsonify({"success": True})


# ==================== RUN ====================
startup_recovery()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    app.run(host="0.0.0.0", port=port, debug=False)
