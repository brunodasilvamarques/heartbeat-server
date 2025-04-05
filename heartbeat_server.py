from flask import Flask, request, jsonify
from datetime import datetime
import threading

app = Flask(__name__)
kiosks = {}

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    kiosk_id = data.get("kiosk_id")
    kiosk_name = data.get("kiosk_name", "Unknown Kiosk")
    currency_iso = data.get("currency_iso", "N/A")
    country = data.get("country", "Unknown Country")
    timestamp = datetime.utcnow()

    if kiosk_id:
        kiosks[kiosk_id] = {
            "name": kiosk_name,
            "currency": currency_iso,
            "country": country,
            "last_seen": timestamp
        }
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"error": "Missing kiosk_id"}), 400

@app.route('/')
def dashboard():
    now = datetime.utcnow()
    rows = []

    for kiosk_id, data in kiosks.items():
        delta = (now - data["last_seen"]).total_seconds()
        status = '🟢 Online' if delta < 300 else '🔴 Offline'

        rows.append(f"""
        <tr>
            <td>{kiosk_id}</td>
            <td>{data['name']}</td>
            <td>{data['country']}</td>
            <td>{data['currency']}</td>
            <td>{data['last_seen'].strftime('%Y-%m-%d %H:%M:%S')}</td>
            <td>{status}</td>
            <td>
                <form method="post" action="/delete/{kiosk_id}" onsubmit="return confirm('Are you sure you want to delete {kiosk_id}?');">
                    <button type="submit">Delete</button>
                </form>
            </td>
        </tr>
        """)

    return f"""
    <html>
    <head>
        <title>Kiosk Heartbeat Monitor</title>
    </head>
    <body>
        <h2>ChangeBox Kiosk Dashboard</h2>
        <table border=1 cellpadding=5>
            <tr><th>Kiosk Code</th><th>Kiosk Name</th><th>Country</th><th>Currency</th><th>Last Seen</th><th>Status</th><th>Actions</th></tr>
            {''.join(rows)}
        </table>
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
