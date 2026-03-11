from flask import Flask, request, jsonify, Response, redirect, render_template_string
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo  # for BST/GMT friendly formatting
from collections import defaultdict
import threading
import json
import os
import time
import msal
import requests
import re
import psycopg
from urllib.parse import urlparse

# ==== Offline alert config ====
OFFLINE_THRESHOLD_SECONDS = 7200  # 2 hours
ALERT_RECIPIENT = "b.marques@fcceinnovations.com"

def map_currency_to_country_code(iso: str) -> str:
    """GBP→GBR, USD→USA, EUR→EUR; otherwise uppercase passthrough."""
    if not iso:
        return "Unknown"
    iso = iso.upper()
    return {"GBP": "GBR", "USD": "USA", "EUR": "EUR"}.get(iso, iso)

def format_london(dt_aware: datetime) -> str:
    """Return '12 Aug 2025 21:33:31 BST' (Europe/London)."""
    try:
        return dt_aware.astimezone(ZoneInfo("Europe/London")).strftime("%d %b %Y %H:%M:%S %Z")
    except Exception:
        # Fallback to ISO if anything odd happens
        return dt_aware.isoformat()
        
def format_london_iso(dt_aware: datetime) -> str:
    """Return 'YYYY-MM-DD HH:MM:SS BST' (Europe/London)."""
    try:
        return dt_aware.astimezone(ZoneInfo("Europe/London")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt_aware.isoformat()

app = Flask(__name__)
kiosks = {}

DATA_FILE = "kiosks_data.json"
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# ====== Optional server settings.json (simple, for future config) ======
SERVER_SETTINGS_PATH = os.environ.get("SERVER_SETTINGS_PATH", "server_settings.json").strip()

DEFAULT_SERVER_SETTINGS = {
    "maintenance_mode": {
        # future use (e.g., if you later add endpoints to show/track MM state)
        "maintenance_mode_dir": "data/maintenance_mode",
        "maintenance_mode_key": "IsMaintenanceModeTemporary"
    }
}

def load_server_settings() -> dict:
    if not os.path.exists(SERVER_SETTINGS_PATH):
        try:
            with open(SERVER_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SERVER_SETTINGS, f, indent=2)
        except Exception:
            pass
        return DEFAULT_SERVER_SETTINGS

    try:
        with open(SERVER_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return DEFAULT_SERVER_SETTINGS

        # shallow merge defaults
        merged = DEFAULT_SERVER_SETTINGS.copy()
        merged.update(data)
        # merge nested maintenance_mode
        if isinstance(data.get("maintenance_mode"), dict):
            mm = DEFAULT_SERVER_SETTINGS["maintenance_mode"].copy()
            mm.update(data["maintenance_mode"])
            merged["maintenance_mode"] = mm
        return merged
    except Exception:
        return DEFAULT_SERVER_SETTINGS

_SERVER_SETTINGS = load_server_settings()

def server_setting(section: str, key: str, default=None):
    try:
        return _SERVER_SETTINGS.get(section, {}).get(key, default)
    except Exception:
        return default

# Ensure the directory exists (harmless even if unused right now)
_mm_dir = str(server_setting("maintenance_mode", "maintenance_mode_dir", default="data/maintenance_mode"))
os.makedirs(_mm_dir, exist_ok=True)

def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    # Render Postgres requires SSL
    return psycopg.connect(DATABASE_URL, sslmode="require")

def db_upsert_daily(kiosk_code: str, day_str: str, payload: dict):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into kiosk_daily (kiosk_code, day, payload)
                values (%s, %s::date, %s::jsonb)
                on conflict (kiosk_code, day)
                do update set payload = excluded.payload, received_at = now()
                """,
                (kiosk_code, day_str, json.dumps(payload)),
            )
        conn.commit()
        
def db_avg_footfall_last_7_days(kiosk_code: str) -> float:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                with last7 as (
                  select
                    day,
                    coalesce(jsonb_array_length(payload->'footfall'->'left'), 0)
                    + coalesce(jsonb_array_length(payload->'footfall'->'right'), 0) as footfall
                  from kiosk_daily
                  where kiosk_code = %s
                    and day >= (current_date - interval '6 days')
                )
                select coalesce(avg(footfall), 0) from last7
                """,
                (kiosk_code,),
            )
            (avg_val,) = cur.fetchone()
            return float(avg_val or 0)

@app.route('/upload_json', methods=['POST'])
def upload_json():
    """
    Accepts multipart upload field name 'file' containing JSON:
      {
        "kiosk_code": "DS01-0001",
        "date": "2026-02-08",
        ...
      }
    Stores to Postgres (kiosk_daily).
    """
    uploaded_file = request.files.get('file')
    if not uploaded_file:
        return jsonify({"ok": False, "error": "no file"}), 400

    try:
        payload = json.load(uploaded_file.stream)
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid json: {e}"}), 400

    kiosk_code = (payload.get("kiosk_code") or "").strip()
    day_str = (payload.get("date") or "").strip()

    if not kiosk_code or not day_str:
        return jsonify({"ok": False, "error": "missing kiosk_code or date"}), 400

    try:
        db_upsert_daily(kiosk_code, day_str, payload)
    except Exception as e:
        return jsonify({"ok": False, "error": f"db error: {e}"}), 500

    return jsonify({"ok": True, "kiosk_code": kiosk_code, "date": day_str}), 200

# ========== Microsoft Graph Email Setup ==========
TENANT_ID = "ce3cbfd0-f41e-440c-a359-65cdc219ff9c"  # your tenant
CLIENT_ID = "673e7dd3-45ba-4bb6-a364-799147e7e9fc"  # your app id
SENDER_EMAIL = "b.marques@fcceinnovations.com"     # your Microsoft account

ADMIN_USERNAME = "ChangeBoxAdmin"
ADMIN_PASSWORD = "Admin@@55"

USER_USERNAME = "ChangeBoxUser"
USER_PASSWORD = "UserFRM@@59"

def save_kiosks():
    with open(DATA_FILE, "w") as f:
        json.dump({k: {**v, "last_seen": v["last_seen"].isoformat()} for k, v in kiosks.items()}, f)

def load_kiosks():
    global kiosks
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
            kiosks = {k: {**v, "last_seen": datetime.fromisoformat(v["last_seen"])} for k, v in raw.items()}

load_kiosks()

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    kiosk_id = data.get("kiosk_id")
    kiosk_name = data.get("kiosk_name", "Unknown Kiosk")
    currency_iso = data.get("currency_iso", "N/A")
    country = data.get("country", "Unknown Country")
    camera_status = data.get("camera_status", "Not Connected ❌")
    address = data.get("address")

    if kiosk_id:
        kiosks[kiosk_id] = {
            "kiosk_name": kiosk_name,
            "currency_iso": currency_iso,
            "country": country,
            "address": address,
            "ip_address": data.get("ip_address", kiosk_id),
            "last_seen": datetime.now(timezone.utc),
            "last_restricted_timestamp": data.get("last_restricted_timestamp", "None"),
            "last_restricted_user": data.get("restricted_user_name", "None"),
            "restricted_list": sorted(data.get("restricted_users_list", [])),
            "camera_status": camera_status,
            "today_general_count": data.get("today_general_count", 0),
            "today_restricted_count": data.get("today_restricted_count", 0),

            # ✅ NEW: today's footfall from heartbeat payload
            "footfall_left": int(data.get("footfall_left", 0) or 0),
            "footfall_right": int(data.get("footfall_right", 0) or 0),

            "software_version": data.get("software_version", "Unknown Version"),
            # --- New state fields for edge-triggered alerts ---
            "status": "online",
            "ever_seen_online": True
        }
        # Reset the flag whenever we see a heartbeat (back online)
        kiosks[kiosk_id]["offline_alert_sent"] = False
        save_kiosks()
        return jsonify({"status": "ok"}), 200
        
    return jsonify({"error": "Missing kiosk_id"}), 400

def require_auth(func):
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth:
            return Response("Unauthorized", 401, {"WWW-Authenticate": "Basic realm='Login Required'"})

        # Save who is logged in
        request.is_admin = False

        if auth.username == ADMIN_USERNAME and auth.password == ADMIN_PASSWORD:
            request.is_admin = True
        elif auth.username == USER_USERNAME and auth.password == USER_PASSWORD:
            request.is_admin = False
        else:
            return Response("Unauthorized", 401, {"WWW-Authenticate": "Basic realm='Login Required'"})

        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@app.route('/')
@require_auth
def dashboard():
    now = datetime.now(timezone.utc)
    grouped = defaultdict(list)
    for kiosk_id, data in kiosks.items():
        grouped[data['country']].append((kiosk_id, data))

    table_rows = []

    for country, group in sorted(grouped.items()):
        for kiosk_id, data in sorted(group):
            delta = (now - data["last_seen"]).total_seconds()
            status = '🟢 Online' if delta <= OFFLINE_THRESHOLD_SECONDS else '🔴 Offline'
            camera_status = (
                data.get("camera_status", "Not Connected ❌")
                if delta <= OFFLINE_THRESHOLD_SECONDS else
                "Unknown ⚠️"
            )

            # --- NEW: split the camera status into icon (top) and text (bottom)
            camera_status_icon = ""
            camera_status_text = camera_status or "Unknown"

            if "✅" in camera_status_text:
                camera_status_icon = "✅"
            elif "❌" in camera_status_text:
                camera_status_icon = "❌"
            elif "⚠️" in camera_status_text:
                camera_status_icon = "⚠️"

            # Remove the icon from the label so the text is clean
            if camera_status_icon:
                camera_status_text = camera_status_text.replace(camera_status_icon, "").strip()

            restricted_time = data.get("last_restricted_timestamp", "None")
            restricted_name = data.get("last_restricted_user", "None")
            raw_list = data.get("restricted_list", [])
            restricted_list = "None" if not raw_list or raw_list == ["None"] else "<br>".join(sorted(str(name) for name in raw_list))
            # ✅ Use today's heartbeat footfall instead of average / DB calculation
            todays_footfall = int(data.get("footfall_left", 0) or 0) + int(data.get("footfall_right", 0) or 0)

            # ✅ Append row to table
            table_rows.append({
                "country": data.get("country", "Unknown"),
                "kiosk_id": kiosk_id,
                "kiosk_name": data.get("kiosk_name", "Unknown"),
                "last_seen_fmt": format_london_iso(data["last_seen"]),
                "status": status,
                "camera_status_icon": camera_status_icon,
                "camera_status_text": camera_status_text,
                "restricted_time": restricted_time,
                "restricted_name": restricted_name,
                "restricted_list": restricted_list,
                "todays_footfall": todays_footfall,
                "version": data.get("software_version", "Unknown")
            })

    html_rows = render_template_string("""
    {% for row in table_rows %}
        {% if loop.first or loop.previtem.country != row.country %}
        <h3 style='color:#2D6AFF'>{{ row.country }}</h3>
        <table>
            <tr>
                <th>Kiosk Name</th>
                <th>Last Heartbeat</th>
                <th>Status</th>
                <th>Camera Status</th>
                <th>Last Restricted Detection</th>
                <th>Restricted User</th>
                <th>Restricted Users List</th>
                <th>Today's Footfall</th>
                <th>Software Version</th>
                {% if request.is_admin %}
                <th>Actions</th>
                {% endif %}
            </tr>
        {% endif %}
        <tr>
            <td>{{ row.kiosk_name }}</td>
            <td>
              <div>{{ row.last_seen_fmt }}</div>
            </td>
            <td>{{ row.status }}</td>
            <td>
              <div style="display:flex; flex-direction:column; align-items:center; line-height:1.2;">
                <div style="font-size:20px; margin-bottom:4px;">{{ row.camera_status_icon }}</div>
                <div>{{ row.camera_status_text }}</div>
              </div>
            </td>
            <td>
              {% if row.restricted_time and row.restricted_time != 'None' %}
                <div>{{ row.restricted_time }}<br>
                  <small style="color:#aaa">Local Kiosk Time</small>
                </div>
              {% else %}
                {{ row.restricted_time }}
              {% endif %}
            </td>
            <td>{{ row.restricted_name }}</td>
            <td>{{ row.restricted_list|safe }}</td>
            <td>{{ row.todays_footfall }}</td>
            <td>{{ row.version }}</td>
            {% if request.is_admin %}
            <td>
                <form method="post" action="/delete/{{ row.kiosk_id }}" onsubmit="return verifyDelete('{{ row.kiosk_id }}')">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">Delete</button>
                </form>
            </td>
            {% endif %}
        </tr>
        {% if loop.last or loop.nextitem.country != row.country %}
        </table><br>
        {% endif %}
    {% endfor %}
    """, table_rows=table_rows, request=request)

    html_content = """
    <html>
    <head>
        <title>ChangeBox Heartbeat Dashboard</title>
        <meta http-equiv="refresh" content="60">
        <style>
            body { font-family: Arial; background-color: #f7f9fb; padding: 20px; }
            .container { max-width: 1300px; margin: auto; }
            table { width: 100%; border-collapse: collapse; margin-bottom: 40px; background: white; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: center; }
            th { background-color: #2D6AFF; color: white; }
            tr:nth-child(even) { background-color: #f2f2f2; }
        </style>
        <script>
            function verifyDelete(kioskId) {
                const answer = prompt("Type DELETE to confirm deletion of kiosk " + kioskId);
                return answer === "DELETE";
            }
            document.addEventListener('DOMContentLoaded', () => {
                const timeElems = document.querySelectorAll('.utc-time');
                timeElems.forEach(el => {
                    const iso = el.getAttribute('data-time');
                    const date = new Date(iso);
                    const local = date.toLocaleString(undefined, {
                      hour12: false,
                      year: 'numeric',
                      month: '2-digit',
                      day: '2-digit',
                      hour: '2-digit',
                      minute: '2-digit',
                      second: '2-digit',
                      timeZoneName: 'short'
                    });
                    el.textContent = local;
                });
            });
        </script>
    </head>
    <body>
        <div class="container">
            <div style="text-align:center; margin-bottom: 20px;">
                <img src="https://raw.githubusercontent.com/brunodasilvamarques/heartbeat-server/main/assets/changebox_logo.png" height="50" style="margin-bottom:10px;">
                <h2 style="margin-top: -5px;">Face Recognition Monitor Dashboard</h2>
            </div>

            """ + html_rows + """
        </div>
    </body>
    </html>
    """

    return render_template_string(html_content)
    
@app.route('/delete/<kiosk_id>', methods=['POST'])
@require_auth
def delete_kiosk(kiosk_id):
    if kiosk_id in kiosks:
        del kiosks[kiosk_id]
        save_kiosks()
    return redirect("/")
    
@app.route('/check_files', methods=['GET'])
def check_files():
    existing = [f for f in os.listdir("data") if f.endswith(".json")]
    return jsonify(existing), 200

def get_access_token():
    try:
        CLIENT_SECRET = "wJS8Q~Nbfs1cxhmuB1pQLk4cFB~l0X_KiYFBxbfE"

        app = msal.ConfidentialClientApplication(
            CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{TENANT_ID}",
            client_credential=CLIENT_SECRET
        )
        token_result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

        if "access_token" in token_result:
            print("✅ Successfully obtained access token.")
            return token_result["access_token"]
        else:
            print(f"❌ Failed to get access token: {token_result}")
            return None
    except Exception as e:
        print(f"❌ Exception getting access token: {str(e)}")
        return None
        
def send_text_email(subject, body, recipients):
    access_token = get_access_token()
    if not access_token:
        print("❌ No access token for alert email")
        return

    email_data = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        },
        "saveToSentItems": "true",
    }
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail",
        headers=headers, json=email_data
    )
    if resp.status_code == 202:
        print("✅ Offline alert email sent.")
    else:
        print(f"❌ Failed to send offline alert: {resp.status_code} - {resp.text}")
      
    
def check_offline_alerts():
    now = datetime.now(timezone.utc)
    changed = False

    for kiosk_id, info in kiosks.items():
        last_seen = info.get("last_seen")
        if isinstance(last_seen, str):
            try:
                last_seen = datetime.fromisoformat(last_seen)
            except Exception:
                continue

        delta_sec = (now - last_seen).total_seconds()
        current_status = "online" if delta_sec <= OFFLINE_THRESHOLD_SECONDS else "offline"

        prev_status = info.get("status", "unknown")
        ever_seen = info.get("ever_seen_online", False)
        was_sent = info.get("offline_alert_sent", False)
        last_alert_iso = info.get("last_offline_alert_at")

        # Transition detection
        if prev_status != current_status:
            info["status"] = current_status
            changed = True

        # Only alert on ONLINE → OFFLINE, and only if we've seen it online before
        if prev_status == "online" and current_status == "offline" and ever_seen and not was_sent:
            iso_cur = info.get("currency_iso") or ""
            mapped_code = map_currency_to_country_code(iso_cur)
            name = info.get("kiosk_name", "Unknown Kiosk")
            country_name = info.get("country", "Unknown Country")
            last_local = format_london(last_seen)

            subject = f"⚠️ FRS Offline | Kiosk: {mapped_code} - {name}"
            body = (
                "The Face Recognition Software for the kiosk detailed below has gone offline.\n\n"
                f"Country: {country_name}\n"
                f"Kiosk ID: {kiosk_id}\n"
                f"Kiosk Name: {name}\n"
                f"Last heartbeat (UK local time): {last_local}\n"
                f"Offline threshold reached: {OFFLINE_THRESHOLD_SECONDS // 3600} hour(s) "
                f"({OFFLINE_THRESHOLD_SECONDS // 60} minutes)\n"
            )

            send_text_email(subject, body, [ALERT_RECIPIENT])
            info["offline_alert_sent"] = True
            info["last_offline_alert_at"] = now.isoformat()
            changed = True

        # OPTIONAL: clear flag automatically once it’s back online (no email)
        if current_status == "online" and was_sent:
            info["offline_alert_sent"] = False
            changed = True

    if changed:
        save_kiosks()

def offline_monitor_loop():
    print("🛰️ Offline monitor loop started; checking every 60s")
    while True:
        try:
            check_offline_alerts()
        except Exception as e:
            print(f"⚠️ Offline monitor error: {e}")
        time.sleep(60)  # check every minute

# ---- Start background jobs once, for both Gunicorn and flask run ----
_background_jobs_started = False
def start_background_jobs_once():
    global _background_jobs_started
    if _background_jobs_started:
        return
    print("🔧 Starting background jobs: offline monitor")
    threading.Thread(target=offline_monitor_loop, daemon=True).start()
    _background_jobs_started = True

@app.before_request
def _kick_jobs():
    # Flask 3.x: before_first_request is gone. This runs on the first request,
    # but our start_background_jobs_once() ensures it only starts once per process.
    start_background_jobs_once()
    # Start background jobs even if nobody has hit the server yet
    start_background_jobs_once()

if __name__ == '__main__':
    # When running with `python heartbeat_server.py`, start jobs here too.
    start_background_jobs_once()
    app.run(host="0.0.0.0", port=5000)


