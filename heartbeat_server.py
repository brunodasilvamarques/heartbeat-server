from flask import Flask, request, jsonify, Response, redirect, send_file, render_template_string
from datetime import datetime
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
    restricted_time = data.get("last_restricted_timestamp")
    restricted_name = data.get("restricted_user_name")
    camera_status = data.get("camera_status", "Not Connected ❌")
    address = data.get("address")

    timestamp = datetime.utcnow()

    if kiosk_id:
        kiosks[kiosk_id] = {
            "kiosk_name": kiosk_name,
            "currency_iso": currency_iso,
            "country": country,
            "address": address,
            "ip_address": data.get("ip_address", kiosk_id),
            "last_seen": datetime.utcnow(),
            "last_restricted_timestamp": data.get("last_restricted_timestamp", "None"),
            "last_restricted_user": data.get("restricted_user_name", "None"),
            "restricted_list": sorted(data.get("restricted_users_list", [])),
            "camera_status": camera_status,
            "today_general_count": data.get("today_general_count", 0),
            "today_restricted_count": data.get("today_restricted_count", 0),
            "software_version": data.get("software_version", "Unknown Version"),
        }
        save_kiosks()
        
        # ✅ Save face detection data to daily_user_counts.json
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")  # ✅ Move this up
            face_file = os.path.join(DATA_DIR, f"daily_user_counts_{today}.json")
            face_data = data.get("face_detection_data", {})

            if os.path.exists(face_file):
                with open(face_file, "r") as f:
                    full_face_data = json.load(f)
            else:
                full_face_data = {}

            if today not in full_face_data:
                full_face_data[today] = {}

            full_face_data[today][kiosk_id] = {
                "kiosk_name": kiosk_name,
                "country": country,
                "general": face_data.get("general", 0),
                "restricted": face_data.get("restricted", {})
            }

            with open(face_file, "w") as f:
                json.dump(full_face_data, f, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to save daily face data: {e}")

        # ✅ Update master counts file
        try:
            master_file = "master_user_counts.json"
            today = datetime.utcnow().strftime("%Y-%m-%d")

            if os.path.exists(master_file):
                with open(master_file, "r") as f:
                    master_data = json.load(f)
            else:
                master_data = {}

            if today not in master_data:
                master_data[today] = {}

            master_data[today][kiosk_id] = {
                "kiosk_name": kiosk_name,
                "country": country,
                "footfall": data.get("today_general_count", 0),
                "restricted_users": data.get("today_restricted_count", 0),
                "footfall_left": data.get("footfall_left", 0),
                "footfall_right": data.get("footfall_right", 0)
            }

            with open(master_file, "w") as f:
                json.dump(master_data, f, indent=4)

        except Exception as e:
            print(f"Error updating master_user_counts.json: {e}")

        # ✅ Save user_count_timestamps.json from kiosk
        try:
            timestamp_file = os.path.join(DATA_DIR, f"daily_user_count_timestamps_{today}.json")
            incoming_data = data.get("timestamp_data", {})
            if os.path.exists(timestamp_file):
                with open(timestamp_file, "r") as f:
                    existing = json.load(f)
            else:
                existing = {}

            for date_key, date_data in incoming_data.items():
                if date_key not in existing:
                    existing[date_key] = {}

                for category in ["general", "restricted", "left", "right"]:
                    if category not in existing[date_key]:
                        existing[date_key][category] = []

                    existing[date_key][category].extend(date_data.get(category, []))

            with open(timestamp_file, "w") as f:
                json.dump(existing, f, indent=4)

        except Exception as e:
            print(f"⚠️ Failed to save timestamp data: {e}")

        return jsonify({"status": "ok"}), 200

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
    now = datetime.utcnow()
    grouped = defaultdict(list)

    master_file = "master_user_counts.json"
    master_data = {}
    try:
        if os.path.exists(master_file):
            with open(master_file, "r") as f:
                master_data = json.load(f)
    except Exception as e:
        print(f"Error reading master_user_counts.json: {e}")

    for kiosk_id, data in kiosks.items():
        grouped[data['country']].append((kiosk_id, data))

    table_rows = []

    for country, group in sorted(grouped.items()):
        for kiosk_id, data in sorted(group):
            delta = (now - data["last_seen"]).total_seconds()
            status = '🟢 Online' if delta < 300 else '🔴 Offline'
            camera_status = data.get("camera_status", "Not Connected ❌") if delta < 300 else "Unknown ⚠️"
            restricted_time = data.get("last_restricted_timestamp", "None")
            restricted_name = data.get("last_restricted_user", "None")
            raw_list = data.get("restricted_list", [])
            restricted_list = "None" if not raw_list or raw_list == ["None"] else "<br>".join(sorted(str(name) for name in raw_list))
            address = data.get("address", "None")

            # Calculate average footfall
            total_general = 0
            days_recorded = 0
            for date_entry, kiosks_on_date in master_data.items():
                if kiosk_id in kiosks_on_date:
                    footfall = kiosks_on_date[kiosk_id].get("footfall", 0)
                    if isinstance(footfall, dict):
                        total_general += footfall.get("total", 0)
                    else:
                        total_general += footfall
                    days_recorded += 1
            average_footfall = round(total_general / days_recorded) if days_recorded else 0

            table_rows.append({
                "country": data.get("country", "Unknown"),
                "kiosk_id": kiosk_id,
                "kiosk_name": data.get("kiosk_name", "Unknown"),
                "last_seen": data["last_seen"].isoformat(),
                "status": status,
                "camera_status": camera_status,
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
            <td><span class="utc-time" data-time="{{ row.last_seen }}">Loading...</span></td>
            <td>{{ row.status }}</td>
            <td>{{ row.camera_status }}</td>
            <td>{{ row.restricted_time }}</td>
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
                        second: '2-digit'
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
                <form method="GET" action="/download_csv" style="display:inline-block; margin-right:10px;">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">Download CSV (Footfall)</button>
                </form>
                <form method="GET" action="/download_csv_face" style="display:inline-block;">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">Download CSV (Face Detection)</button>
                </form>
                <form method="GET" action="/download_csv_timestamps" style="display:inline-block; margin-left:10px;">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">Download CSV (Timestamps)</button>
                </form>
                <form method="GET" action="/download_all_csvs" style="display:inline-block; margin-left:10px;">
                    <button type="submit" style="background-color:#2D6AFF; color:white; border:none; padding:6px 12px; border-radius:4px;">Download All CSVs (Weekly)</button>
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
    uploaded_file = request.files.get('file')  # ✅ Corrected here
    if uploaded_file:
        filename = uploaded_file.filename
        save_path = os.path.join("data", filename)
        uploaded_file.save(save_path)
        print(f"✅ Uploaded file to: {save_path}")
        return f"✅ Uploaded {filename}", 200
    return "❌ No file uploaded", 400
    
@app.route('/download_csv')
@require_auth
def download_csv():
    fetch_jsons_from_kiosks()  # ✅ Pull fresh data from kiosks
    master_file = "master_user_counts.json"
    output_csv = "daily_user_counts_Combined_ALL_DATES.csv"

    try:
        if not os.path.exists(master_file):
            return "No data to download", 404

        with open(master_file, "r") as f:
            master_data = json.load(f)

        with open(output_csv, "w", newline="") as csvfile:
            fieldnames = ["Date", "Country", "Kiosk Code", "Kiosk Name", "Footfall", "Footfall Left", "Footfall Right"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            for date_key, kiosks_on_date in master_data.items():
                for kiosk_id, info in kiosks_on_date.items():
                    writer.writerow({
                        "Date": date_key,
                        "Country": info.get("country", "Unknown"),
                        "Kiosk Code": kiosk_id,
                        "Kiosk Name": info.get("kiosk_name", "Unknown"),
                        "Footfall": info.get("footfall", {}).get("total") if isinstance(info.get("footfall", {}), dict) else info.get("footfall", 0),
                        "Footfall Left": info.get("footfall_left", 0),
                        "Footfall Right": info.get("footfall_right", 0)
                    })

        return send_file(output_csv, as_attachment=True)

    except Exception as e:
        return f"Error generating CSV: {str(e)}", 500
        
@app.route("/download_csv_face")
@require_auth
def download_csv_face():
    fetch_jsons_from_kiosks()  # ✅ Pull fresh data from kiosks
    counts_files = sorted(glob.glob("data/daily_user_counts_*.json"))
    output_csv = "daily_face_detection_Combined_ALL_DATES.csv"

    try:
        all_data = {}

        for file in counts_files:
            with open(file, "r") as f:
                json_data = json.load(f)
                for date_key, kiosks_on_date in json_data.items():
                    if date_key not in all_data:
                        all_data[date_key] = {}
                    for kiosk_id, data in kiosks_on_date.items():
                        all_data[date_key][kiosk_id] = data

        with open(output_csv, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Date", "Country", "Kiosk Code", "Kiosk Name", "General Users", "Restricted Users", "Restricted User", "First Seen", "Last Seen"])

            for date_key, kiosks_on_date in all_data.items():
                for kiosk_id, entry in kiosks_on_date.items():
                    if kiosk_id in ["general", "restricted"]:
                        continue
                    general = entry.get("general", 0)
                    restricted = entry.get("restricted", {})
                    restricted_total = len(restricted)
                    kiosk_name = entry.get("kiosk_name", "Unknown Kiosk")
                    country = entry.get("country", "Unknown")

                    if restricted:
                        for user, times in restricted.items():
                            writer.writerow([
                                date_key,
                                country,
                                kiosk_id,
                                kiosk_name,
                                general,
                                restricted_total,
                                user,
                                times.get("first_seen", ""),
                                times.get("last_seen", "")
                            ])
                    else:
                        writer.writerow([
                            date_key,
                            country,
                            kiosk_id,
                            kiosk_name,
                            general,
                            0,
                            "", "", ""
                        ])

        return send_file(output_csv, as_attachment=True)

    except Exception as e:
        return f"Error generating face detection CSV: {str(e)}", 500

        
@app.route("/download_csv_timestamps")
@require_auth
def download_csv_timestamps():
    fetch_jsons_from_kiosks()  # ✅ Pull fresh data from kiosks
    timestamp_files = sorted(glob.glob("data/daily_user_count_timestamps_*.json"))
    output_csv = "daily_user_count_timestamps_Combined_ALL_DATES.csv"

    try:
        with open(output_csv, "w", newline="") as csvfile:
            fieldnames = ["Date", "Country", "Kiosk Code", "Kiosk Name", "Type", "Time"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            for file in timestamp_files:
                with open(file, "r") as f:
                    data = json.load(f)

                for date_key, categories in data.items():
                    for type_name in ["general", "restricted", "left", "right"]:
                        for entry in categories.get(type_name, []):
                            if not isinstance(entry, dict):
                                continue
                            writer.writerow({
                                "Date": date_key,
                                "Country": entry.get("country", ""),
                                "Kiosk Code": entry.get("kiosk_code", ""),
                                "Kiosk Name": entry.get("kiosk_name", ""),
                                "Type": type_name.title(),
                                "Time": entry.get("time", "")
                            })

        return send_file(output_csv, as_attachment=True)

    except Exception as e:
        return f"Error generating timestamp CSV: {str(e)}", 500
        
def fetch_jsons_from_kiosks():
    for kiosk_id, info in kiosks.items():
        last_seen = info.get("last_seen")
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen)

        if (datetime.utcnow() - last_seen).total_seconds() > 300:
            print(f"⚠️ Skipping {kiosk_id} — Offline for over 5 minutes")
            continue

        try:
            ip = info.get("ip_address", kiosk_id)
            url = f"http://{ip}:5050/push_jsons"
            response = requests.post(url, timeout=10)

            if response.status_code == 200:
                print(f"📤 Push JSONs success for kiosk: {kiosk_id}")
            else:
                print(f"❌ Push JSONs failed for {kiosk_id}: {response.status_code}")
        except Exception as e:
            print(f"❌ Push JSONs error from {kiosk_id}: {e}")

    print("⏳ Waiting 10 seconds for kiosks to upload JSONs...")
    time.sleep(10)
        
@app.route("/download_all_csvs")
@require_auth
def download_all_csvs():
    fetch_jsons_from_kiosks()  # ✅ Pull fresh data from kiosks
    send_weekly_csv()  # Generate the files
    try:
        from zipfile import ZipFile
        zip_name = "weekly_csv_reports.zip"
        with ZipFile(zip_name, 'w') as zipf:
            for fname in [
                "daily_user_counts_Combined_ALL_DATES.csv",
                "daily_face_detection_Combined_ALL_DATES.csv",
                "daily_user_count_timestamps_Combined_ALL_DATES.csv"
            ]:
                if os.path.exists(fname):
                    zipf.write(fname)

        return send_file(zip_name, as_attachment=True)

    except Exception as e:
        return f"Error zipping CSVs: {str(e)}", 500


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
        
def send_weekly_csv():
    try:
        today_str = datetime.utcnow().strftime("%Y-%m-%d")

        # --- CSV 1: Footfall ---
        master_file = "master_user_counts.json"
        footfall_csv = "daily_user_counts_Combined_ALL_DATES.csv"

        if os.path.exists(master_file):
            with open(master_file, "r") as f:
                master_data = json.load(f)

            with open(footfall_csv, "w", newline="") as csvfile:
                fieldnames = ["Date", "Country", "Kiosk Code", "Kiosk Name", "Footfall", "Footfall Left", "Footfall Right"]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()

                for date_entry, kiosks_on_date in master_data.items():
                    for kiosk_id, info in kiosks_on_date.items():
                        writer.writerow({
                            "Date": date_entry,
                            "Country": info.get("country", "Unknown"),
                            "Kiosk Code": kiosk_id,
                            "Kiosk Name": info.get("kiosk_name", "Unknown"),
                            "Footfall": info.get("footfall", {}).get("total") if isinstance(info.get("footfall", {}), dict) else info.get("footfall", 0),
                            "Footfall Left": info.get("footfall_left", 0),
                            "Footfall Right": info.get("footfall_right", 0)
                        })

        # --- CSV 2: Face Detection ---
        counts_files = sorted(glob.glob("data/daily_user_counts_*.json"))
        combined_counts = {}

        for file in counts_files:
            with open(file, "r") as f:
                data = json.load(f)

            # Extract date from filename if top-level is kiosk_id
            match = re.search(r"daily_user_counts_(\d{4}-\d{2}-\d{2})\.json", file)
            file_date = match.group(1) if match else "Unknown"

            if file_date in data:
                kiosks_on_date = data[file_date]
            else:
                kiosks_on_date = data  # fallback if no date wrapper

            if any(key in ["general", "restricted"] for key in kiosks_on_date.keys()):
                print(f"⚠️ Skipping {file}: no kiosk data found")
                continue

            for kiosk_id, kiosk_data in kiosks_on_date.items():
                if file_date not in combined_counts:
                    combined_counts[file_date] = {}
                combined_counts[file_date][kiosk_id] = kiosk_data

        face_csv = "daily_face_detection_Combined_ALL_DATES.csv"
        with open(face_csv, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Date", "Country", "Kiosk Code", "Kiosk Name", "General Users", "Restricted Users", "Restricted User", "First Seen", "Last Seen"])

            for date_str, kiosks_on_date in combined_counts.items():
                for kiosk_id, counts in kiosks_on_date.items():
                    if kiosk_id in ["general", "restricted"]:
                        continue
                    general = counts.get("general", 0)
                    restricted = counts.get("restricted", {})
                    restricted_total = len(restricted)
                    kiosk_name = counts.get("kiosk_name", "Unknown Kiosk")
                    country = counts.get("country", "Unknown")

                    if restricted:
                        for user, times in restricted.items():
                            writer.writerow([
                                date_str,
                                country,
                                kiosk_id,
                                kiosk_name,
                                general,
                                restricted_total,
                                user,
                                times.get("first_seen", ""),
                                times.get("last_seen", "")
                            ])
                    else:
                        writer.writerow([
                            date_str,
                            country,
                            kiosk_id,
                            kiosk_name,
                            general,
                            0,
                            "", "", ""
                        ])

        # --- CSV 3: Timestamps ---
        timestamp_files = sorted(glob.glob("data/daily_user_count_timestamps_*.json"))
        print("📂 Matched timestamp files:", timestamp_files)
        timestamp_csv = "daily_user_count_timestamps_Combined_ALL_DATES.csv"
        timestamp_rows = []

        for file in timestamp_files:
            with open(file, "r") as f:
                try:
                    data = json.load(f)
                    print(f"✅ Parsed timestamp file: {file} with keys: {list(data.keys())}")
                    for date_key, categories in data.items():
                        for category in ["general", "restricted", "left", "right"]:
                            for entry in categories.get(category, []):
                                if not isinstance(entry, dict):
                                    continue
                                timestamp_rows.append({
                                    "Date": date_key,
                                    "Country": entry.get("country", ""),
                                    "Kiosk Code": entry.get("kiosk_code", ""),
                                    "Kiosk Name": entry.get("kiosk_name", ""),
                                    "Type": category.title(),
                                    "Time": entry.get("time", "")
                                })
                except Exception as e:
                    print(f"❌ Failed to parse {file}: {e}")

        with open(timestamp_csv, "w", newline="") as csvfile:
            fieldnames = ["Date", "Country", "Kiosk Code", "Kiosk Name", "Type", "Time"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(timestamp_rows)

        # --- EMAIL ---
        access_token = get_access_token()
        if not access_token:
            print("❌ No access token. Cannot send email.")
            return

        attachments = []
        for fname in [footfall_csv, face_csv, timestamp_csv]:
            with open(fname, "rb") as f:
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": fname,
                    "contentBytes": base64.b64encode(f.read()).decode()
                })

        email_data = {
            "message": {
                "subject": "📊 Weekly ChangeBox Face-Detection & Footfall CSV Reports",
                "body": {
                    "contentType": "HTML",
                    "content": "Attached are the weekly CSV reports for Footfall, Face Detection, and Timestamps."
                },
                "toRecipients": [{"emailAddress": {"address": "b.marques@fcceinnovations.com"}}],
                "attachments": attachments
            }
        }

        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        response = requests.post(f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail", headers=headers, json=email_data)
        if response.status_code == 202:
            print("✅ Weekly CSVs emailed successfully.")
        else:
            print(f"❌ Email failed: {response.status_code} {response.text}")

    except Exception as e:
        print(f"❌ Failed to send weekly CSVs: {str(e)}")
        
def schedule_weekly_email():
    schedule.every().monday.at("09:00").do(send_weekly_csv)
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# Start the schedule on a background thread
threading.Thread(target=schedule_weekly_email, daemon=True).start()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
