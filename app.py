# BALESHWAR GAMING - Hosting Platform
# Admin: BALESHWAR GAMING
# Features: Login, Dashboard, Admin Panel, Limit 2GB, 5 Projects, Voting, Social Links

from flask import Flask, request, jsonify, render_template_string, redirect, session
import os, json, datetime

app = Flask(__name__)
app.secret_key = "baleshwar-gaming-secret"

# --- DEFAULT SETTINGS - Admin badal sakta hai ---
DEFAULT_PER_PROJECT_GB = 2
DEFAULT_MAX_PROJECTS = 5

# Database - Simple JSON me
USERS_FILE = "users.json"
PROJECTS_FILE = "projects.json"
SOCIAL_FILE = "social_links.json"
VOTES_FILE = "votes.json"

def load(f):
    if not os.path.exists(f): return []
    try:
        with open(f) as x: return json.load(x)
    except: return []

def save(f, data):
    with open(f, "w") as x: json.dump(data, x, indent=2)

# --- HOME / LOGIN PAGE ---
HTML = """
<!DOCTYPE html><html><head><title>BALESHWAR GAMING Hosting</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{background:#000;color:#fff;font-family:Arial;text-align:center}
.green{color:#00ff88}.box{border:2px solid #00ff88;border-radius:15px;padding:20px;margin:15px}
input{width:90%;padding:12px;margin:8px;border-radius:10px;border:1px solid #00ff88;background:#111;color:#fff}
button{background:#00ff88;color:#000;padding:12px 25px;border:none;border-radius:10px;font-weight:bold}
a{color:#00ff88;text-decoration:none}
.social{display:flex;justify-content:center;gap:15px;margin-top:15px}
.social img{width:50px;height:50px;border-radius:10px}
</style></head>
<body>
<h1 class="green">BALESHWAR GAMING</h1>
<h3>Premium Hosting Platform</h3>
<div class="box">
<h2>LOGIN</h2>
<form method="POST" action="/login">
<input name="email" placeholder="Enter Your Email" required><br>
<input name="password" type="password" placeholder="Enter Your Password" required><br>
<button>SIGN UP / LOGIN</button>
</form>
<p><a href="/signup">CREATE ACCOUNT</a></p>
<p style="color:red">{{msg}}</p>
</div>
<div class="box"><h3>Admin Social Links</h3><div class="social" id="social"></div></div>
<div class="box"><h3>VOTING</h3>
<button onclick="vote('GOOD')">👍 GOOD</button>
<button onclick="vote('BAD')">👎 BAD</button><br><br>
<input id="fb" placeholder="Feedback...">
<button onclick="sendFb()">Submit Feedback</button>
</div>
<script>
fetch('/api/social').then(r=>r.json()).then(d=>{
 let h=''; d.forEach(s=>{
   let logo=s.logo || (s.link.includes('youtube')?'https://cdn-icons-png.flaticon.com/512/1384/1384060.png':s.link.includes('instagram')?'https://cdn-icons-png.flaticon.com/512/2111/2111463.png':'https://cdn-icons-png.flaticon.com/512/733/733585.png');
   h+=`<a href="${s.link}" target="_blank"><img src="${logo}"><br>${s.title}</a>`;
 }); document.getElementById('social').innerHTML=h;
});
function vote(v){fetch('/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:v})}).then(()=>alert('Thanks for Voting: '+v))}
function sendFb(){let t=document.getElementById('fb').value; fetch('/api/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:'FEEDBACK',text:t})}).then(()=>alert('Feedback Sent'))}
</script></body></html>
"""

@app.route("/")
def home():
    return render_template_string(HTML, msg="")

@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email","").strip().lower()
    pwd = request.form.get("password","")
    users = load(USERS_FILE)
    for u in users:
        if u["email"]==email:
            if u["password"]!=pwd:
                return render_template_string(HTML, msg="Incorrect Password")
            session["user"]=email
            return redirect("/dashboard")
    # New user auto create
    users.append({"email":email,"password":pwd,"limit_gb":DEFAULT_PER_PROJECT_GB,"max_projects":DEFAULT_MAX_PROJECTS,"date":str(datetime.date.today())})
    save(USERS_FILE, users)
    session["user"]=email
    return redirect("/dashboard")

@app.route("/signup")
def signup():
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect("/")
    user = session["user"]
    projects = [p for p in load(PROJECTS_FILE) if p["user"]==user]
    users = load(USERS_FILE)
    me = [u for u in users if u["email"]==user][0] if users else {"limit_gb":2,"max_projects":5}
    is_admin = (user=="nadanitiwari89@gmail.com")
    all_users = users if is_admin else []
    html = f"""
    <body style="background:#000;color:#fff;font-family:Arial;padding:15px">
    <h2 style="color:#00ff88">Dashboard - {user}</h2>
    <div style="border:2px solid #00ff88;padding:15px;border-radius:10px">
    <b>Your Limit:</b> Per Project {me.get('limit_gb',2)} GB | Max Projects {me.get('max_projects',5)}<br>
    <b>Your Projects:</b> {len(projects)} / {me.get('max_projects',5)}
    </div><br>
    <form method="POST" action="/create-project">
    <input name="name" placeholder="Project Name - ex: mera-game" required style="padding:10px;width:70%">
    <button style="background:#00ff88;padding:10px;border-radius:8px">+ Create Project</button>
    </form><hr>
    <h3>Hosting Projects</h3>
    """
    for p in projects:
        html += f"<div style='border:1px solid #00ff88;padding:10px;margin:8px;border-radius:10px'>"
        html += f"<b>{p['name']}</b> - {p['date']} - Size: {p.get('size','0 MB')}<br>"
        html += f"<a href='/site/{p['name']}' target='_blank' style='color:#00ff88'>OPEN / VIEW WEBSITE</a> | "
        html += f"<a href='/delete/{p['name']}' style='color:red'>Delete</a><br>"
        html += f"Live Link: /site/{p['name']}<br>"
        html += f"<form method='POST' action='/upload/{p['name']}' enctype='multipart/form-data'><input type='file' name='file' required><button>Upload File (.py/.html/.css/.js/.json/.txt/.png)</button></form>"
        html += f"Files: {', '.join(p.get('files',[]))}</div>"
    if is_admin:
        html += "<hr><h2 style='color:#00ff88'>ADMIN PANEL</h2>"
        html += "<h3>All Users - Project Name - Date - Size</h3>"
        for u in all_users:
            ups = [p for p in load(PROJECTS_FILE) if p["user"]==u["email"]]
            html += f"{u['email']} - Projects: {len(ups)} - Limit: {u.get('limit_gb')}GB / {u.get('max_projects')} projects<br>"
        html += """
        <h3>Admin - Change User Limit</h3>
        <form method="POST" action="/admin/set-limit">
        <input name="email" placeholder="User Email" required>
        <input name="limit_gb" placeholder="Per Project GB - ex: 2" type="number">
        <input name="max_projects" placeholder="Max Projects - ex: 10" type="number">
        <button>Update Limit</button></form>
        <h3>Admin - Social Links Manager</h3>
        <form method="POST" action="/admin/social">
        <input name="title" placeholder="Title - ex: YouTube" required>
        <input name="link" placeholder="Paste Link - https://..." required>
        <input name="logo" placeholder="Logo URL OR Leave Blank for Auto Logo">
        <button>Add Link</button></form>
        <p>Auto Logo: YouTube link = YouTube logo, Instagram link = Instagram logo, WhatsApp link = WhatsApp logo. Ya Gallery se logo upload karke uska link yaha dal do.</p>
        """
    html += "<br><a href='/logout' style='color:red'>Logout</a></body>"
    return html

@app.route("/create-project", methods=["POST"])
def create_project():
    if "user" not in session: return redirect("/")
    name = request.form.get("name","").strip().replace(" ","-").lower()
    projects = load(PROJECTS_FILE)
    users = load(USERS_FILE)
    me = [u for u in users if u["email"]==session["user"]][0]
    my = [p for p in projects if p["user"]==session["user"]]
    if len(my) >= me.get("max_projects",5):
        return "Limit Reached! Contact Admin to Increase Limit. <a href='/dashboard'>Back</a>"
    projects.append({"user":session["user"],"name":name,"date":str(datetime.date.today()),"size":"0 MB","files":[]})
    save(PROJECTS_FILE, projects)
    return redirect("/dashboard")

@app.route("/upload/<name>", methods=["POST"])
def upload(name):
    if "user" not in session: return redirect("/")
    f = request.files.get("file")
    if not f: return "No File"
    # Allowed files
    allowed = [".py",".html",".css",".js",".json",".txt",".png",".jpg",".jpeg"]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed:
        return f"Not Allowed: {ext} - Allowed: .py .html .css .js .json .txt .png .jpg <a href='/dashboard'>Back</a>"
    folder = f"projects/{name}"
    os.makedirs(folder, exist_ok=True)
    f.save(os.path.join(folder, f.filename))
    projects = load(PROJECTS_FILE)
    for p in projects:
        if p["name"]==name:
            if f.filename not in p.get("files",[]): p["files"].append(f.filename)
            # Size check - 2GB default
            total = sum(os.path.getsize(os.path.join(folder, x)) for x in os.listdir(folder) if os.path.isfile(os.path.join(folder,x)))
            p["size"] = f"{round(total/1024/1024,2)} MB"
    save(PROJECTS_FILE, projects)
    return redirect("/dashboard")

@app.route("/site/<name>")
def site(name):
    folder = f"projects/{name}"
    index = os.path.join(folder, "index.html")
    if os.path.exists(index):
        with open(index) as f: return f.read()
    return f"<h1>{name} - Website Live!</h1><p>Upload index.html to show your website here.</p><p>Project by BALESHWAR GAMING Hosting</p>"

@app.route("/delete/<name>")
def delete(name):
    projects = [p for p in load(PROJECTS_FILE) if p["name"]!=name]
    save(PROJECTS_FILE, projects)
    return redirect("/dashboard")

@app.route("/admin/set-limit", methods=["POST"])
def set_limit():
    users = load(USERS_FILE)
    for u in users:
        if u["email"]==request.form.get("email"):
            if request.form.get("limit_gb"): u["limit_gb"]=int(request.form.get("limit_gb"))
            if request.form.get("max_projects"): u["max_projects"]=int(request.form.get("max_projects"))
    save(USERS_FILE, users)
    return redirect("/dashboard")

@app.route("/admin/social", methods=["POST"])
def add_social():
    links = load(SOCIAL_FILE)
    links.append({"title":request.form.get("title"),"link":request.form.get("link"),"logo":request.form.get("logo","")})
    save(SOCIAL_FILE, links)
    return redirect("/dashboard")

@app.route("/api/social")
def api_social():
    return jsonify(load(SOCIAL_FILE))

@app.route("/api/vote", methods=["POST"])
def vote():
    v = load(VOTES_FILE); v.append(request.json); save(VOTES_FILE, v)
    return jsonify({"ok":True})

@app.route("/logout")
def logout():
    session.clear(); return redirect("/")

if __name__=="__main__":
    os.makedirs("projects", exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))