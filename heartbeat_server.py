from flask import Flask, request, jsonify
from datetime import datetime
from collections import defaultdict
import threading
import json
import os

app = Flask(__name__)
kiosks = {}
load_kiosks()

DATA_FILE = "kiosks_data.json"

def save_kiosks():
    with open(DATA_FILE, "w") as f:
        json.dump({k: {**v, "last_seen": v["last_seen"].isoformat()} for k, v in kiosks.items()}, f)

def load_kiosks():
    global kiosks
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            raw = json.load(f)
            kiosks = {k: {**v, "last_seen": datetime.fromisoformat(v["last_seen"])} for k, v in raw.items()}

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
            "name": kiosk_name,
            "currency": currency_iso,
            "country": country,
            "last_seen": timestamp,
            "restricted_time": restricted_time,
            "restricted_name": restricted_name,
            "address": address
        }
        save_kiosks()
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"error": "Missing kiosk_id"}), 400

@app.route('/')
def dashboard():
    now = datetime.utcnow()
    grouped = defaultdict(list)

    # Group by country
    for kiosk_id, data in kiosks.items():
        grouped[data['country']].append((kiosk_id, data))

    html_rows = ""

    for country, group in sorted(grouped.items()):
        html_rows += f"<h3 style='color:#6D28D9'>{country}</h3>"
        html_rows += """
        <table>
            <tr>
                <th>Kiosk Code</th>
                <th>Kiosk Name</th>
                <th>Last Heartbeat</th>
                <th>Status</th>
                <th>Last Restricted Detection</th>
                <th>Restricted User</th>
                <th>Address</th>
                <th>Actions</th>
            </tr>
        """
        for kiosk_id, data in sorted(group):
            delta = (now - data["last_seen"]).total_seconds()
            status = '🟢 Online' if delta < 300 else '🔴 Offline'
            restricted_time = data.get("restricted_time", "None")
            restricted_name = data.get("restricted_name", "None")
            address = data.get("address", "None")
            html_rows += f"""
            <tr>
                <td>{kiosk_id}</td>
                <td>{data['name']}</td>
                <td>{data['last_seen'].strftime('%Y-%m-%d %H:%M:%S')}</td>
                <td>{status}</td>
                <td>{restricted_time}</td>
                <td>{restricted_name}</td>
                <td>{address}</td>
                <td>
                    <form method="post" action="/delete/{kiosk_id}" onsubmit="return confirm('Are you sure you want to delete {kiosk_id}?');">
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
                color: #6D28D9;
            }}
            tr:nth-child(even) {{ background-color: #2b2b2b; }}
            tr:hover {{ background-color: #333333; }}
        </style>
    </head>
    <body>
        <img src="https://raw.githubusercontent.com/brunodasilvamarques/heartbeat-server/main/assets/changebox_logo.png" height="50" style="margin-bottom:10px;">
        <h2>Face Recognition Monitor Dashboard</h2>
        {html_rows}
    </body>
    </html>
    """

@app.route('/delete/<kiosk_id>', methods=['POST'])
def delete_kiosk(kiosk_id):
    if kiosk_id in kiosks:
        del kiosks[kiosk_id]
    return dashboard()
    

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
