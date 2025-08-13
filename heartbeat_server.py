from flask import Flask, request, jsonify, Response, redirect, send_file, render_template_string
from datetime import datetime, timezone
from zoneinfo import ZoneInfo  # for BST/GMT friendly formatting
from collections import defaultdict
import threading
import json
import os
import base64
import csv
import schedule
import time
import msal
import requests
import glob
import re

# ==== Offline alert config ====
OFFLINE_THRESHOLD_SECONDS = 300  # 5 minutes
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
            "software_version": data.get("software_version", "Unknown Version"),
        }
        # reset offline alert on successful heartbeat
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
            status = '🟢 Online' if delta < 300 else '🔴 Offline'
            camera_status = data.get("camera_status", "Not Connected ❌") if delta < 300 else "Unknown ⚠️"

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
            address = data.get("address", "None")

            # ✅ Calculate average footfall from kiosk-specific master JSON
            total_footfall = 0
            days_recorded = 0
            kiosk_master = {}  # always init

            # ✅ Find the latest yearly JSON for this kiosk
            matching_files = sorted(glob.glob(os.path.join(DATA_DIR, f"{kiosk_id}_master_data*.json")))
            if matching_files:
                kiosk_master_file = matching_files[-1]
                with open(kiosk_master_file, "r") as f:
                    kiosk_master = json.load(f)
                for date_key, date_data in kiosk_master.items():
                    if date_key in ["kiosk_code", "kiosk_name", "country"]:
                        continue

                    daily_left = len(date_data.get("footfall", {}).get("left", []))
                    daily_right = len(date_data.get("footfall", {}).get("right", []))
                    total_footfall += (daily_left + daily_right)
                    days_recorded += 1

            # ✅ If still no historical data, try today’s key
            if days_recorded == 0:
                today_key = datetime.now().strftime("%Y-%m-%d")
                today_data = kiosk_master.get(today_key, {})
                today_left = len(today_data.get("footfall", {}).get("left", []))
                today_right = len(today_data.get("footfall", {}).get("right", []))
                total_footfall = today_left + today_right
                days_recorded = 1 if total_footfall > 0 else 0

            average_footfall = round(total_footfall / days_recorded) if days_recorded else 0

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
                "average_footfall": average_footfall,
                "address": address,
                "version": data.get("software_version", "Unknown")
            })

    html_rows = render_template_string("""
    {% for row in table_rows %}
        {% if loop.first or loop.previtem.country != row.country %}
        <h3 style='color:#2D6AFF'>{{ row.country }}</h3>
        <table>
            <tr>
                <th>Kiosk Code</th>
                <th>Kiosk Name</th>
                <th>Last Heartbeat</th>
                <th>Status</th>
                <th>Camera Status</th>
                <th>Last Restricted Detection</th>
                <th>Restricted User</th>
                <th>Restricted Users List</th>
                <th>Average Footfall</th>
                <th>Address</th>
                <th>Software Version</th>
                {% if request.is_admin %}
                <th>Actions</th>
                {% endif %}
            </tr>
        {% endif %}
        <tr>
            <td>{{ row.kiosk_id }}</td>
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
            <td>{{ row.average_footfall }}</td>
            <td>{{ row.address }}</td>
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
        <title>ChangeBox Kiosk Heartbeat Monitor</title>
        <style>
            body {
                background-color: #1b1b1b;
                color: white;
                font-family: 'Segoe UI', sans-serif;
            }
            button:hover {
                background-color: #1A4ED8;
                cursor: pointer;
            }
            h2 {
                color: #ffffff;
                margin-bottom: 5px;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }
            th, td {
                border: 1px solid #333;
                padding: 8px;
                text-align: center;
            }
            th {
                background-color: #333;
                color: #2D6AFF;
            }
            tr:nth-child(even) { background-color: #2b2b2b; }
            tr:hover { background-color: #333333; }
        </style>
        <script>
            function verifyDelete(kioskId) {
                var username = prompt("Enter username:");
                var password = prompt("Enter password:");
                return username === "ChangeBoxAdmin" && password === "Admin@@55";
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
                      timeZoneName: 'short'   // e.g., BST, GMT, PDT
                    });
                    el.textContent = local;
                });
            });
        </script>
    </head>
    <body>
        <div style="text-align:center; margin-bottom: 20px;">
            <img src="https://raw.githubusercontent.com/brunodasilvamarques/heartbeat-server/main/assets/changebox_logo.png" height="50" style="margin-bottom:10px;">
            <h2 style="margin-top: -5px;">Face Recognition Monitor Dashboard</h2>
            <div style="text-align:right; margin-bottom:10px;">
                <form method="GET" action="/download_combined_csv" style="display:inline-block;">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">
                        Download CSV (All Kiosks)
                    </button>
                </form>
            </div>
        </div>
    """ + html_rows + """
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

@app.route('/upload_json', methods=['POST'])
def upload_json():
    uploaded_file = request.files.get('file')
    if uploaded_file:
        filename = uploaded_file.filename
        file_bytes = uploaded_file.read()

        try:
            json_data = json.loads(file_bytes)
        except json.JSONDecodeError:
            return "❌ Invalid JSON format", 400

        if not isinstance(json_data, dict) or "kiosk_code" not in json_data:
            return "❌ Missing required data (kiosk_code)", 400

        save_path = os.path.join("data", filename)
        with open(save_path, "w") as f:
            json.dump(json_data, f, indent=2)

        print(f"✅ Uploaded valid master_data file to: {save_path}")
        return f"✅ Uploaded {filename}", 200

    return "❌ No file uploaded", 400
        
@app.route('/download_combined_csv')
@require_auth
def download_combined_csv():
    combined_csv = "ChangeBox_Face Recognition and Footfall_Combined Data.csv"

    all_rows = []
    for kiosk_file in glob.glob(os.path.join(DATA_DIR, "*_master_data*.json")):
        with open(kiosk_file, "r") as f:
            kiosk_data = json.load(f)

        kiosk_code = kiosk_data.get("kiosk_code", "Unknown")
        kiosk_name = kiosk_data.get("kiosk_name", "Unknown")
        country = kiosk_data.get("country", "Unknown")

        for date_key, date_data in kiosk_data.items():
            if date_key in ["kiosk_code", "kiosk_name", "country"]:
                continue

            # General Users
            general_users_list = date_data.get("general_users", [])
            for entry in general_users_list:
                # entry looks like "1 - 12:47:37"
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "General User", "-", time_seen, "-", 1
                ])

            # Restricted Users
            restricted_users = date_data.get("restricted_users", {})
            for user, times in restricted_users.items():
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Restricted User", user,
                    times.get("first_seen", ""), times.get("last_seen", ""), 1
                ])

            # Footfall Left
            foot_left_list = date_data.get("footfall", {}).get("left", [])
            for entry in foot_left_list:
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Footfall Left", "-", time_seen, "-", 1
                ])

            foot_right_list = date_data.get("footfall", {}).get("right", [])
            for entry in foot_right_list:
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Footfall Right", "-", time_seen, "-", 1
                ])

    # Write combined CSV
    with open(combined_csv, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Date", "Country", "Kiosk Code", "Kiosk Name", "Type", "User/Detail", "First Seen", "Last Seen", "Count"])
        writer.writerows(all_rows)

    # ✅ Email CSV immediately after generating it
    email_combined_csv(combined_csv)
    return send_file(combined_csv, as_attachment=True)

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
        
def email_combined_csv(file_path):
    access_token = get_access_token()
    if not access_token:
        print("❌ No access token for email")
        return

    with open(file_path, "rb") as f:
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": os.path.basename(file_path),
            "contentBytes": base64.b64encode(f.read()).decode()
        }

    email_data = {
        "message": {
            "subject": "📊 Weekly ChangeBox Face Recognition and Footfall CSV Report",
            "body": {"contentType": "HTML", "content": "Attached is the combined CSV from all kiosks."},
            "toRecipients": [{"emailAddress": {"address": "b.marques@fcceinnovations.com"}}],
            "attachments": [attachment]
        }
    }

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    requests.post(f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail", headers=headers, json=email_data)
    
def send_weekly_csv():
    print("📧 Generating weekly CSV for email...")
    combined_csv = "ChangeBox_Face Recognition and Footfall_Combined Data.csv"

    all_rows = []
    for kiosk_file in glob.glob(os.path.join(DATA_DIR, "*_master_data*.json")):
        with open(kiosk_file, "r") as f:
            kiosk_data = json.load(f)

        kiosk_code = kiosk_data.get("kiosk_code", "Unknown")
        kiosk_name = kiosk_data.get("kiosk_name", "Unknown")
        country = kiosk_data.get("country", "Unknown")

        for date_key, date_data in kiosk_data.items():
            if date_key in ["kiosk_code", "kiosk_name", "country"]:
                continue

            # General Users
            general_users_list = date_data.get("general_users", [])
            for entry in general_users_list:
                # entry looks like "1 - 12:47:37"
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "General User", "-", time_seen, "-", 1
                ])

            # Restricted Users
            restricted_users = date_data.get("restricted_users", {})
            for user, times in restricted_users.items():
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Restricted User", user,
                    times.get("first_seen", ""), times.get("last_seen", ""), 1
                ])

            # Footfall Left
            foot_left_list = date_data.get("footfall", {}).get("left", [])
            for entry in foot_left_list:
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Footfall Left", "-", time_seen, "-", 1
                ])

            foot_right_list = date_data.get("footfall", {}).get("right", [])
            for entry in foot_right_list:
                parts = entry.split(" - ")
                time_seen = parts[1] if len(parts) > 1 else "-"
                all_rows.append([
                    date_key, country, kiosk_code, kiosk_name, "Footfall Right", "-", time_seen, "-", 1
                ])


    with open(combined_csv, "w", newline="") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Date", "Country", "Kiosk Code", "Kiosk Name", "Type", "User/Detail", "First Seen", "Last Seen", "Count"])
        writer.writerows(all_rows)

    # ✅ Email the generated CSV
    email_combined_csv(combined_csv)
    print("✅ Weekly CSV emailed successfully!")
    
def check_offline_alerts():
    now = datetime.now(timezone.utc)
    changed = False
    for kiosk_id, info in kiosks.items():
        last_seen = info.get("last_seen")
        # ensure last_seen is aware datetime
        if isinstance(last_seen, str):
            try:
                last_seen = datetime.fromisoformat(last_seen)
            except Exception:
                continue

        delta = (now - last_seen).total_seconds()
        was_sent = info.get("offline_alert_sent", False)

        if delta > OFFLINE_THRESHOLD_SECONDS and not was_sent:
            # Prefer currency_iso → mapped country code (e.g., USD→USA)
            iso_cur = info.get("currency_iso") or ""
            mapped_code = map_currency_to_country_code(iso_cur)

            # Human labels
            name = info.get("kiosk_name", "Unknown Kiosk")
            country_name = info.get("country", "Unknown Country")
            last_local = format_london(last_seen)

            # ✉️ Subject and body
            subject = f"⚠️ FRS Offline | Kiosk: {mapped_code} - {name}"
            body = (
                "The Face Recognition Software for the kiosk detailed below has gone offline.\n\n"
                f"Country: {country_name}\n"
                f"Kiosk ID: {kiosk_id}\n"
                f"Kiosk Name: {name}\n"
                f"Last heartbeat (UK local time): {last_local}\n"
                f"Offline threshold: {OFFLINE_THRESHOLD_SECONDS}s "
                f"({OFFLINE_THRESHOLD_SECONDS // 60} minute{'s' if OFFLINE_THRESHOLD_SECONDS // 60 != 1 else ''})\n"
            )

            send_text_email(subject, body, [ALERT_RECIPIENT])
            info["offline_alert_sent"] = True
            changed = True

        # Optional recovery email (uncomment if you want an 'online again' notice)
        # if delta <= OFFLINE_THRESHOLD_SECONDS and was_sent:
        #     iso = info.get("currency_iso") or info.get("country") or "Unknown"
        #     name = info.get("kiosk_name", "Unknown Kiosk")
        #     subject = f"✅ Kiosk Back Online: {iso} - {name}"
        #     body = f"Kiosk {iso} - {name} is back online at {now.isoformat()} UTC."
        #     send_text_email(subject, body, [ALERT_RECIPIENT])
        #     info["offline_alert_sent"] = False
        #     changed = True

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
        
def schedule_weekly_email():
    schedule.every().monday.at("09:00").do(send_weekly_csv)
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# ---- Start background jobs once, for both Gunicorn and flask run ----
_background_jobs_started = False
def start_background_jobs_once():
    global _background_jobs_started
    if _background_jobs_started:
        return
    print("🔧 Starting background jobs: weekly email + offline monitor")
    threading.Thread(target=schedule_weekly_email, daemon=True).start()
    threading.Thread(target=offline_monitor_loop, daemon=True).start()
    _background_jobs_started = True

@app.before_request
def _kick_jobs():
    # Flask 3.x: before_first_request is gone. This runs on the first request,
    # but our start_background_jobs_once() ensures it only starts once per process.
    start_background_jobs_once()

if __name__ == '__main__':
    # When running with `python heartbeat_server.py`, start jobs here too.
    start_background_jobs_once()
    app.run(host="0.0.0.0", port=5000)


