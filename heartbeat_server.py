from flask import Flask, request, jsonify, Response, redirect
from datetime import datetime
from collections import defaultdict
import threading
import json
import os
import base64

app = Flask(__name__)
kiosks = {}

DATA_FILE = "kiosks_data.json"

USERNAME = "ChangeBoxAdmin"
PASSWORD = "Admin@@55"

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
    address = data.get("address")

    timestamp = datetime.utcnow()

    if kiosk_id:
        kiosks[kiosk_id] = {
            "kiosk_name": kiosk_name,
            "currency_iso": currency_iso,
            "country": country,
            "address": address,
            "last_seen": datetime.utcnow(),
            "last_restricted_timestamp": data.get("last_restricted_timestamp", "None"),
            "last_restricted_user": data.get("restricted_user_name", "None"),
            "restricted_list": sorted(data.get("restricted_users_list", []))  # ✅ Sorted alphabetically
        }

        save_kiosks()
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"error": "Missing kiosk_id"}), 400

def require_auth(func):
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != USERNAME or auth.password != PASSWORD:
            return Response("Unauthorized", 401, {"WWW-Authenticate": "Basic realm='Login Required'"})
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@app.route('/')
@require_auth
def dashboard():
    now = datetime.utcnow()
    grouped = defaultdict(list)
    for kiosk_id, data in kiosks.items():
        grouped[data['country']].append((kiosk_id, data))

    html_rows = ""

    for country, group in sorted(grouped.items()):
        html_rows += f"<h3 style='color:#2D6AFF'>{country}</h3>"
        html_rows += """
        <table>
            <tr>
                <th>Kiosk Code</th>
                <th>Kiosk Name</th>
                <th>Last Heartbeat</th>
                <th>Status</th>
                <th>Last Restricted Detection</th>
                <th>Restricted User</th>
                <th>Restricted Users List</th>
                <th>Address</th>
                <th>Actions</th>
            </tr>
        """
        for kiosk_id, data in sorted(group):
            delta = (now - data["last_seen"]).total_seconds()
            status = '🟢 Online' if delta < 300 else '🔴 Offline'
            last_seen_local = data['last_seen'].strftime('%Y-%m-%d %H:%M:%S') + " UTC"
            
            # ✅ Fixed key names
            restricted_time = data.get("last_restricted_timestamp", "None")
            restricted_name = data.get("last_restricted_user", "None")

            raw_list = data.get("restricted_list", [])
            if not raw_list or raw_list == ["None"]:
                restricted_list = "None"
            else:
                restricted_list = "<br>".join(sorted(str(name) for name in raw_list))

            address = data.get("address", "None")

            html_rows += f"""
            <tr>
                <td>{kiosk_id}</td>
                <td>{data.get('kiosk_name', 'Unknown')}</td>
                <td><span class="utc-time" data-time="{data['last_seen'].isoformat()}">Loading...</span></td>
                <td>{status}</td>
                <td>{restricted_time}</td>
                <td>{restricted_name}</td>
                <td>{restricted_list}</td>
                <td>{address}</td>
                <td>
                    <form method="post" action="/delete/{kiosk_id}" onsubmit="return verifyDelete('{kiosk_id}')">
                        <button type="submit">Delete</button>
                    </form>
                </td>
            </tr>
            """
        html_rows += "</table><br>"

    return f"""
    <html>
    <head>
        <title>ChangeBox Kiosk Heartbeat Monitor</title>
        <style>
            body {{
                background-color: #1b1b1b;
                color: white;
                font-family: 'Segoe UI', sans-serif;
            }}
            h2 {{
                color: #ffffff;
                margin-bottom: 5px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }}
            th, td {{
                border: 1px solid #333;
                padding: 8px;
                text-align: center;
            }}
            th {{
                background-color: #333;
                color: #2D6AFF;
            }}
            tr:nth-child(even) {{ background-color: #2b2b2b; }}
            tr:hover {{ background-color: #333333; }}
        </style>
        <script>
            function verifyDelete(kioskId) {{
                var username = prompt("Enter username:");
                var password = prompt("Enter password:");
                return username === "ChangeBoxAdmin" && password === "Admin@@55";
            }}
        </script>
    </head>
    <body>
        <div style="text-align:center; margin-bottom: 20px;">
            <img src="https://raw.githubusercontent.com/brunodasilvamarques/heartbeat-server/main/assets/changebox_logo.png" height="50" style="margin-bottom:10px;">
            <h2>Face Recognition Monitor Dashboard</h2>
        </div>
        {html_rows}

        <script>
            document.addEventListener('DOMContentLoaded', () => {
                const timeElems = document.querySelectorAll('.utc-time');
                timeElems.forEach(el => {
                    const iso = el.getAttribute('data-time');
                    const local = new Date(iso).toLocaleString();
                    el.textContent = local;
                });
            });
        </script>
        </body>
        </html>
        """
    
@app.route('/delete/<kiosk_id>', methods=['POST'])
@require_auth
def delete_kiosk(kiosk_id):
    if kiosk_id in kiosks:
        del kiosks[kiosk_id]
        save_kiosks()
    return redirect("/")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
