import os
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
import json
import time
import uuid
from flask import Flask, request, jsonify, redirect, session
import dateparser
from datetime import datetime, timedelta
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from db import query
from dotenv import load_dotenv
load_dotenv()

# Use credentials file path from .env (GOOGLE_CLIENT_SECRETS)
CLIENT_SECRETS_FILE = os.environ.get('GOOGLE_CLIENT_SECRETS', '.env')
OAUTH_REDIRECT = os.environ.get('OAUTH_REDIRECT', f'http://localhost:{PORT}/oauth2callback')
SCOPES = ['https://www.googleapis.com/auth/calendar.events']

# UTIL: parse natural language datetime (multilingual)
def parse_datetime(text, settings=None):
    dt = dateparser.parse(text, settings=settings or {'PREFER_DATES_FROM': 'future'})
    return dt

# Create reminder in DB
def create_reminder(phone, text, due_dt):
    rid = str(uuid.uuid4())
    ts = int(due_dt.timestamp() * 1000)
    created = int(time.time() * 1000)
    query('INSERT INTO reminders (id, phone, text, due_at, timezone, created_at, sent) VALUES (?,?,?,?,?,?,0)',
          (rid, phone, text, ts, None, created), commit=True)
    return rid

# List reminders
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your-default-secret-key')


@app.route('/reminders/<phone>', methods=['GET'])
def list_reminders(phone):
    rows = query('SELECT id, text, due_at, sent FROM reminders WHERE phone = ? ORDER BY due_at', (phone,))
    rems = []
    for r in rows:
        rems.append({'id': r[0], 'text': r[1], 'due_at': datetime.fromtimestamp(r[2]/1000).isoformat(), 'sent': bool(r[3])})
    return jsonify(success=True, reminders=rems)

# MCP invoke endpoint (add/list/delete)
@app.route('/mcp/invoke', methods=['POST'])
def mcp_invoke():
    data = request.get_json(force=True)
    tool = data.get('tool')
    user = data.get('user') or {}
    input_ = data.get('input') or {}
    phone = user.get('phone')

    if not tool or not phone:
        return jsonify(success=False, error='missing tool or user.phone'), 400

    if tool == 'addReminder':
        text = input_.get('text')
        if not text:
            return jsonify(success=False, error='missing input.text'), 400
        dt = parse_datetime(text)
        if not dt:
            return jsonify(success=True, reply="I couldn't find a clear date/time. Try: 'remind me tomorrow at 9am'" )
        rid = create_reminder(phone, text, dt)
        # Optionally create calendar event if user connected
        try:
            from google.oauth2.credentials import Credentials
            rows = query('SELECT credentials FROM oauth_tokens WHERE phone = ?', (phone,), fetch_one=True)
            if rows:
                cred_json = rows[0]
                creds = Credentials.from_authorized_user_info(json.loads(cred_json), SCOPES)
                service = build('calendar', 'v3', credentials=creds)
                event = {
                    'summary': text,
                    'start': {'dateTime': dt.isoformat(), 'timeZone': 'UTC'},
                    'end': {'dateTime': (dt + timedelta(minutes=30)).isoformat(), 'timeZone': 'UTC'},
                }
                created = service.events().insert(calendarId='primary', body=event).execute()
                event_id = created.get('id')
                query('UPDATE reminders SET calendar_event_id = ? WHERE id = ?', (event_id, rid), commit=True)
        except Exception:
            # ignore calendar failures for now
            pass

        return jsonify(success=True, reply=f'Reminder created for {dt.isoformat()}', reminder_id=rid)

    if tool == 'listReminders':
        rows = query('SELECT id, text, due_at, sent FROM reminders WHERE phone = ? ORDER BY due_at', (phone,))
        formatted = []
        for r in rows:
            formatted.append({'id': r[0], 'text': r[1], 'due_at': datetime.fromtimestamp(r[2]/1000).isoformat(), 'sent': bool(r[3])})
        return jsonify(success=True, reminders=formatted)

    if tool == 'deleteReminder':
        rid = input_.get('id')
        if not rid:
            return jsonify(success=False, error='missing input.id'), 400
        query('DELETE FROM reminders WHERE id = ?', (rid,), commit=True)
        return jsonify(success=True, reply='Deleted.')

    return jsonify(success=False, error='unknown tool'), 400

# OAuth: start flow to get user authorization (stores credentials per phone)
@app.route('/authorize')
def authorize():
    phone = request.args.get('phone')
    if not phone:
        return 'missing phone in query', 400
    flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT)
    auth_url, state = flow.authorization_url(access_type="offline")
    session["state"] = state  # Store Google's random state
    session["phone"] = phone  # Store phone separately
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    try:
        state = request.args.get("state")
        if state != session.get("state"):
            return "Invalid state", 400
        phone = session.get("phone")
        if not phone:
            return "Missing phone in session", 400

        flow = Flow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT, state=state)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        cred_json = creds.to_json()
        query('INSERT OR REPLACE INTO oauth_tokens (phone, credentials) VALUES (?,?)', (phone, cred_json), commit=True)
        return f'Google Calendar connected for {phone}. You can close this browser.'
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return f"Internal Server Error: {e}<br><pre>{tb}</pre>", 500

# Scheduler job: find due reminders and POST to PUCH_NOTIFY_URL

def check_and_send_reminders():
    now_ms = int(time.time() * 1000)
    rows = query('SELECT id, phone, text, due_at FROM reminders WHERE due_at <= ? AND sent = 0', (now_ms,))
    for r in rows:
        rid, phone, text, due_at = r
        payload = {
            'to': phone,
            'message': f'🔔 Reminder: {text} (scheduled)'
        }
        if PUCH_NOTIFY_URL:
            try:
                requests.post(PUCH_NOTIFY_URL, json=payload, timeout=5)
            except Exception as e:
                app.logger.error('Failed to send notify: %s', e)
                continue
        else:
            app.logger.info('Would notify %s: %s', phone, text)
        query('UPDATE reminders SET sent = 1 WHERE id = ?', (rid,), commit=True)

# Start scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_and_send_reminders, trigger='interval', seconds=60)
scheduler.start()

def init_db():
    query('CREATE TABLE IF NOT EXISTS oauth_tokens (phone TEXT PRIMARY KEY, credentials TEXT);', commit=True)
    query('CREATE TABLE IF NOT EXISTS reminders (id TEXT PRIMARY KEY, phone TEXT, text TEXT, due_at INTEGER, timezone TEXT, created_at INTEGER, sent INTEGER, calendar_event_id TEXT);', commit=True)

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT)