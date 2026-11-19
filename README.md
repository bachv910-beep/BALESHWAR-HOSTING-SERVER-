# VIP Hosting (Render)

Environment me kuch set karna zaruri nahi (secret key apne aap ban jaati hai).
Optional: SITE_NAME, DATA_DIR=/var/data (Render Disk lagao to data safe rahega).

Admin: deploy ke turant baad /admin/login (footer ka chhota shield icon) kholo.
Pehli baar jo Gmail + password daaloge wahi hamesha ke liye admin ho jaata hai.

Start command: gunicorn -w 1 --threads 16 --timeout 120 -b 0.0.0.0:$PORT app:app
