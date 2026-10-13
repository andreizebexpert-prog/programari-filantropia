"""
Verifica locurile libere la Spitalul Filantropia pentru un medic si trimite
notificare (Telegram, email, WhatsApp) cand apare un interval nou.

Secrete / variabile de mediu (oricare canal lipsa este sarit):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID       - Telegram
  GMAIL_USER, GMAIL_APP_PASSWORD         - email prin Gmail
  EMAIL_TO                               - optional, implicit GMAIL_USER
  WHATSAPP_PHONE, WHATSAPP_APIKEY        - WhatsApp prin CallMeBot
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

# --- Configurare ---------------------------------------------------------
SPECIALTY = os.environ.get("SPECIALTY") or "10"   # Obstetrica-Ginec.
SERVICE = os.environ.get("SERVICE") or "56"        # Consult CNAS + ecografie
DOCTOR = os.environ.get("DOCTOR") or "2153"        # Dr. MUSTATA LAURA MIHAELA
# Cat timp sta activa o rulare si cat de des verifica in ea.
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES") or 9)
INTERVAL_SECONDS = 60
# Mod test: ruleaza o singura data pe alt medic, fara stare.
TEST_MODE = bool(os.environ.get("TEST_MODE"))
# -------------------------------------------------------------------------

BASE = "https://programare.spitalulfilantropia.ro"
API_URL = (f"{BASE}/frontend/slots?showAll=true"
           f"&specialty={SPECIALTY}&service={SERVICE}&doctor={DOCTOR}")
PAGE_URL = (f"{BASE}/appointment?specialty={SPECIALTY}"
            f"&service={SERVICE}&doctor={DOCTOR}&availability=all")
STATE_FILE = "state.json"
NO_SLOTS = "Nu exista locuri disponibile"


def env(name):
    return (os.environ.get(name) or "").strip()


def fetch(url):
    req = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Referer": PAGE_URL,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/154.0.0.0 Safari/537.36"),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse(page):
    """Intoarce ({data: {label, slots}} pentru zilele cu locuri libere,
    numarul total de zile gasite, numele medicului)."""
    cards = page.split('class="doctor-card-item')[1:]
    available = {}
    doctor_name = ""
    for card in cards:
        name = re.search(r"<h3[^>]*>([^<]+)</h3>", card)
        if name and not doctor_name:
            doctor_name = html.unescape(name.group(1)).strip()
        m = re.search(r'data-card-id="([^"]+)"', card)
        if not m:
            continue
        date = m.group(1).rsplit("-", 1)[0]          # 2026-10-19
        label = re.search(r'class="badge[^"]*">([^<]+)<', card)
        label = label.group(1).strip() if label else date
        slots_html = card.split("slots-container", 1)[1]
        if NO_SLOTS in slots_html:
            continue
        text = strip_tags(slots_html.split(">", 1)[1])
        times = re.findall(r"\b\d{1,2}:\d{2}\b", text)
        available[date] = {"label": label,
                           "slots": times or [text[:200] or "(interval nou)"]}
    return available, len(cards), doctor_name


# --- Canale de notificare -------------------------------------------------

def send_telegram(subject, lines):
    token, chat_id = env("TELEGRAM_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return None
    body = [f"🩺 <b>{html.escape(subject)}</b>", ""]
    body += [html.escape(l) for l in lines]
    body += ["", f'<a href="{PAGE_URL}">Deschide pagina de programări</a>']
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": "\n".join(body), "parse_mode": "HTML",
        "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)
    return "Telegram"


def send_email(subject, lines):
    user, password = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD")
    if not (user and password):
        return None
    msg = EmailMessage()
    msg["Subject"] = f"🩺 {subject}"
    msg["From"] = user
    msg["To"] = env("EMAIL_TO") or user
    msg.set_content("\n".join(lines + ["", f"Programare: {PAGE_URL}"]))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password.replace(" ", ""))
        s.send_message(msg)
    return "Email"


def send_whatsapp(subject, lines):
    phone, apikey = env("WHATSAPP_PHONE"), env("WHATSAPP_APIKEY")
    if not (phone and apikey):
        return None
    text = "\n".join([f"🩺 *{subject}*", ""] + lines + ["", PAGE_URL])
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey})
    urllib.request.urlopen(url, timeout=30).read()
    return "WhatsApp"


def notify(subject, lines):
    sent, errors = [], []
    for channel in (send_telegram, send_email, send_whatsapp):
        try:
            name = channel(subject, lines)
            if name:
                sent.append(name)
        except Exception as e:                       # un canal picat nu le
            errors.append(f"{channel.__name__}: {e}")  # opreste pe celelalte
    print("Trimis pe:", ", ".join(sent) or "niciun canal")
    for e in errors:
        print("EROARE la", e)
    if not sent:
        raise RuntimeError("Nicio notificare nu a putut fi trimisă.")
    return errors


# --- Logica principala ----------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def check_once(state):
    """O verificare. Intoarce lista de erori de notificare."""
    page = fetch(API_URL)
    available, total, doctor_name = parse(page)
    if total == 0:
        raise RuntimeError("Nu am găsit niciun card. Răspuns: " + page[:300])
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{stamp}] {total} zile verificate, {len(available)} cu locuri libere.")

    seen = state.get("notified", {})
    new = {d: v for d, v in available.items()
           if set(v["slots"]) - set(seen.get(d, []))}
    errors = []
    if new:
        subject = f"Locuri libere la {doctor_name or 'medicul urmărit'}"
        if TEST_MODE:
            subject = "[TEST] " + subject
        lines = [f"• {new[d]['label']}: " + ", ".join(new[d]["slots"])
                 for d in sorted(new)]
        errors = notify(subject, lines)
    # Tinem minte doar ce e liber acum: daca un loc se ocupa si apoi se
    # elibereaza din nou, primesti iar notificare.
    state["notified"] = {d: v["slots"] for d, v in available.items()}
    return errors


def main():
    if TEST_MODE:
        errors = check_once({})
        print("Mod test: starea nu a fost salvată.")
        sys.exit(1 if errors else 0)

    state = load_state()
    deadline = time.time() + LOOP_MINUTES * 60
    ok_checks, failures, notify_errors = 0, [], []
    while True:
        try:
            notify_errors += check_once(state)
            ok_checks += 1
            save_state(state)
        except Exception as e:
            print("EROARE la verificare:", e)
            failures.append(str(e))
        if time.time() + INTERVAL_SECONDS > deadline:
            break
        time.sleep(INTERVAL_SECONDS)

    print(f"Gata: {ok_checks} verificări reușite, {len(failures)} eșuate.")
    # Rularea pica (si GitHub trimite email) daca nicio verificare nu a mers
    # sau daca un canal de notificare a dat eroare.
    if ok_checks == 0 or notify_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
