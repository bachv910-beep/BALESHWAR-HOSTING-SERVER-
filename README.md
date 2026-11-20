# VIP Hosting

No environment variables are required. The secret key is generated and stored automatically.
Optional: SITE_NAME, DATA_DIR (set DATA_DIR to a Render Disk path such as /var/data to keep data across redeploys).

Admin panel: https://<your-app>.onrender.com/admin-penel
The first Gmail and password entered there become the admin account permanently.

Start command: gunicorn -w 1 --threads 16 --timeout 120 -b 0.0.0.0:$PORT app:app

Ratings: anyone can rate and review from the home page, even without logging in.

Hosting types:
- Bot Hosting: Python or Node.js bots and scripts. Console commands, password prompts and Ctrl+C all work.
- Website Hosting: HTML / JSON / CSS / JS sites, or Python / Node web apps. You get a public link.
