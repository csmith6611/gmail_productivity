
"""
gmail_receipt_watcher.py
- Runs a loop: every `PING_INTERVAL_SECONDS` ping the paperless server.
- If ping OK, search Gmail for messages with SUBJECT_TO_SEARCH newer than last run.
- Converts HTML body to PDF and uploads to paperless.
"""

import array
import os
import time
import json
import logging
import base64
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup  # optional, if you want to parse HTML
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from docx2pdf import convert
from dotenv import load_dotenv

load_dotenv()



import base64


# === CONFIG - edit these ===
CONFIG = {
    "CREDENTIALS_FILE": 'credentials.json',
    "TOKEN_FILE": f"{os.environ.get('TOKEN_FILE_NAME')}",
    "LAST_RUN_FILE": "last_run.json",         
    "USER_ID": "me",
    "PING_URL": f"http://{os.environ.get('PAPERLESS_HOSTNAME')}:{os.environ.get('PAPERLESS_PORT')}/health/", 
    "PING_INTERVAL_SECONDS": 10 * 60,        
    "PAPERLESS_UPLOAD_URL":f"http://{os.environ.get('PAPERLESS_HOSTNAME')}:{os.environ.get('PAPERLESS_PORT')}/api/documents/post_document/",
    "PAPERLESS_AUTH": {                       
        "token": f"{os.environ.get('PAPERLESS_TOKEN')}",   
        "basic": None                       
    },
    "DOWNLOAD_DIR": "downloads",
    "SYNTHETICS_DIR": "synthetics",             
             
    "LOG_FILE": "gmail_receipt_watcher.log",
    "SCOPES": ["https://www.googleapis.com/auth/gmail.readonly"],  
}
# ============================

# Logging
logger = logging.getLogger("gmail_receipt_watcher")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(CONFIG["LOG_FILE"])
fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(ch)

os.makedirs(CONFIG["DOWNLOAD_DIR"], exist_ok=True)

import email
from email import policy
from email.parser import BytesParser
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from bs4 import BeautifulSoup

def eml_to_pdf(eml_path, pdf_path):
    # Parse the EML file
    with open(eml_path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    subject = msg['subject'] or "(No Subject)"
    from_ = msg['from'] or ""
    to = msg['to'] or ""
    date = msg['date'] or ""

    # Get the plain text body
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body_text = part.get_content()
                break
        else:
            body_text = "(No plain text body found)"
    else:
        body_text = msg.get_content()

    # Create PDF
    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter
    y = height - 50

    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, subject)
    y -= 30

    c.setFont("Helvetica", 10)
    c.drawString(50, y, f"From: {from_}")
    y -= 15
    c.drawString(50, y, f"To: {to}")
    y -= 15
    c.drawString(50, y, f"Date: {date}")
    y -= 25

    for line in body_text.splitlines():
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica", 10)
        c.drawString(50, y, line)
        y -= 12

    c.save()
    return pdf_path

# --- Gmail auth and client ---
def get_gmail_service():
    creds = None
    token_path = Path(CONFIG["TOKEN_FILE"])
    creds_path = Path(CONFIG["CREDENTIALS_FILE"])
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), CONFIG["SCOPES"])
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning("Failed to refresh token: %s", e)
                creds = None
        if not creds:
            if not creds_path.exists():
                raise FileNotFoundError(f"Missing credentials file at {creds_path.resolve()}")
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), CONFIG["SCOPES"])
            creds = flow.run_local_server(port=0)
            # save token
            with open(CONFIG["TOKEN_FILE"], "w") as f:
                f.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    return service

# --- last run timestamp helpers ---
def load_last_run():
    p = Path(CONFIG["LAST_RUN_FILE"])
    if not p.exists():
        ts = int(time.time()) - 24*3600*30*3
        save_last_run(ts)
        return ts
    try:
        with p.open("r") as f:
            data = json.load(f)
            return int(data.get("last_run", int(time.time()) - 24*3600*30*3))
    except Exception as e:
        logger.exception("Failed to read last run file: %s", e)
        ts = int(time.time()) - 24*3600*30*3
        save_last_run(ts)
        return ts
    
# --- last run timestamp helpers ---
def load_queries():
    p = Path(CONFIG["QUERIES_PATH"])
    try:
        with p.open("r") as f:
            data = json.load(f)
            
            return [str(item) for item in list(data['queries'])]
    except Exception as e:
       logger.error(e)
       raise e

def save_last_run(epoch_seconds):
    with open(CONFIG["LAST_RUN_FILE"], "w") as f:
        json.dump({"last_run": int(epoch_seconds)}, f)

# --- ping paperless ---
def ping_paperless():
    try:
        resp = requests.get(CONFIG["PING_URL"], timeout=8)
        logger.debug("Ping response: %s", resp.status_code)
        return resp.status_code == 200
    except Exception as e:
        logger.debug("Ping failed: %s", e)
        return False

# --- search Gmail ---
def build_search_query(since_epoch, query):
    # Gmail accepts 'after:YYYY/MM/DD' or 'after:UNIX' (UNIX days?). Safer: use after:timestamp in seconds
    # Use subject search:
    # example: subject:"Payment Receipt from Shady Grove Fertility" after: <unix timestamp>
    return f'{query} after:{int(since_epoch)}'

def list_messages(service, query):
    try:
        response = service.users().messages().list(userId=CONFIG["USER_ID"], q=query, maxResults=50).execute()
        messages = response.get("messages", [])
        return messages
    except Exception as e:
        logger.exception("Gmail list failed: %s", e)
        return []

def get_message_full(service, msg_id):
    try:
        raw_message =  service.users().messages().get(userId=CONFIG["USER_ID"], id=msg_id, format="raw").execute()
        raw_data = base64.urlsafe_b64decode(raw_message["raw"].encode("ASCII"))
        with open("downloads/raw.eml", "wb") as f:
            f.write(raw_data)
        return service.users().messages().get(userId=CONFIG["USER_ID"], id=msg_id, format="full").execute()
    except Exception as e:
        logger.exception("Failed get message %s: %s", msg_id, e)
        return None

# attachments handling
def save_attachments(service, msg):
    saved = []
    payload = msg.get("payload", {})
    parts = payload.get("parts") or []
    for p in parts:
        if p.get("filename"):
            att_id = p.get("body", {}).get("attachmentId")
            if att_id:
                att = service.users().messages().attachments().get(userId=CONFIG["USER_ID"], messageId=msg["id"], id=att_id).execute()
                data = att.get("data")
                if data:
                    file_data = base64.urlsafe_b64decode(data.encode("ASCII"))
                    filename = p.get("filename")
                    outpath = Path(CONFIG["DOWNLOAD_DIR"]) / filename
                    with open(outpath, "wb") as f:
                        f.write(file_data)
                    saved.append(str(outpath))
    return saved

# --- upload to paperless ---
def upload_to_paperless(file_path, metadata=None):
    url = CONFIG["PAPERLESS_UPLOAD_URL"]
    headers = {}
    auth = None
    token = CONFIG["PAPERLESS_AUTH"].get("token")
    if token:
        headers["Authorization"] = f"Token {token}"
    elif CONFIG["PAPERLESS_AUTH"].get("basic"):
        auth = CONFIG["PAPERLESS_AUTH"]["basic"]
    files = {"document": open(file_path, "rb")}
    data = metadata or {}
    try:
        resp = requests.post(url, headers=headers, auth=auth, files=files, data=data, timeout=30)
        files["document"].close()
        logger.debug("Upload response code: %s text: %s", resp.status_code, resp.text[:200])
        return resp.status_code in (200, 201)
    except Exception as e:
        logger.exception("Upload failed: %s", e)
        try:
            files["document"].close()
        except:
            pass
        return False


# --- helper: parse RFC822 date to epoch if needed ---
def gmail_date_to_epoch(headers):
    # headers is list of dicts with name/value
    import email.utils
    for h in headers:
        if h.get("name", "").lower() == "date":
            parsed = email.utils.parsedate_to_datetime(h.get("value"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
    return int(time.time())

# --- process single message ---
def process_message(service, msg):
    msg_full = get_message_full(service, msg["id"])
    if not msg_full:
        return None
    epoch = gmail_date_to_epoch(msg_full.get("payload", {}).get("headers", []))

    # first try attachments
    atts = save_attachments(service, msg_full)
    if atts:
        file_to_upload = atts[0]
    else:
        
        file_to_upload = eml_to_pdf("downloads/raw.eml", "synthetics/raw.pdf")

    if not file_to_upload:
        logger.error("No file to upload for message %s", msg["id"])
        return epoch

    metadata = {"source": "gmail_watcher", "title": CONFIG["SUBJECT_TO_SEARCH"]}
    ok = upload_to_paperless(file_to_upload, metadata=metadata)
    if ok:
        logger.info("Uploaded %s for message %s", file_to_upload, msg["id"])
        try:
            Path(file_to_upload).unlink()
        except Exception:
            pass
    else:
        logger.error("Failed upload for %s (message %s)", file_to_upload, msg["id"])

    return epoch


def setup_dirs():
    d = Path(CONFIG["DOWNLOAD_DIR"])
    s = Path(CONFIG["SYNTHETICS_DIR"])

    if not d.exists():
        d.mkdir()
    if not s.exists():
        s.mkdir()
    return



# --- main loop ---
def main_loop():
    service = get_gmail_service()
    last_run = load_last_run()
    logger.info("Starting; last_run=%s (%s)", last_run, datetime.fromtimestamp(last_run, tz=timezone.utc).isoformat())

    setup_dirs()

    queries = load_queries()

    while True:
        try:
            ping_ok = ping_paperless()
            if not ping_ok:
                logger.info("Ping failed or paperless not healthy; skipping search this cycle.")
            else:
                
                for query in queries: 
                    q = build_search_query(last_run, query)
                    logger.info("Searching Gmail with query: %s", q)
                    messages = list_messages(service, q)
                    if not messages:
                        logger.info("No new messages found.")
                    else:
                        logger.info("Found %d messages", len(messages))
                        for m in messages:
                            e = process_message(service, m)

                last_run = datetime.now().timestamp()
                save_last_run(last_run)
                logger.info("Updated last_run to %s", last_run)
            logger.debug("Sleeping %s seconds...", CONFIG["PING_INTERVAL_SECONDS"])
        except Exception:
            logger.exception("Main loop iteration failed")
        time.sleep(CONFIG["PING_INTERVAL_SECONDS"])


if __name__ == "__main__":
    main_loop()
