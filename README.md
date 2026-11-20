# VIP Hosting (Render)

Environment me kuch set karna zaruri nahi (secret key apne aap banti hai).
Optional: SITE_NAME, DATA_DIR=/var/data (Render Disk lagao to data redeploy par safe rahega).

Admin panel: https://<your-app>.onrender.com/admin-penel
Pehli baar jo Gmail + password daaloge wahi hamesha ke liye admin banega.

Start command: gunicorn -w 1 --threads 16 --timeout 120 -b 0.0.0.0:$PORT app:app

Public reviews: home page par koi bhi (login ke bina) star rating aur review de sakta hai.
Hosting:
- Bot Hosting: Python/Node bot ya script. Console me command, password, Ctrl+C sab chalta hai.
- Website Hosting: HTML/JSON/CSS/JS ya web app. Public link milta hai.
