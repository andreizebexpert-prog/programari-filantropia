"""
Verifica locurile libere la Spitalul Filantropia pentru unul sau mai multi
medici si trimite notificari (Telegram, email, WhatsApp) cand:
  - apare un interval liber nou;
  - un interval care era liber dispare (s-a ocupat).
Raspunde si la comenzi pe Telegram: check / verifica (cu sau fara "/").

Secrete / variabile de mediu (oricare canal lipsa este sarit):
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID       - Telegram (mai multe id-uri: 1;2)
  GMAIL_USER, GMAIL_APP_PASSWORD         - email prin Gmail
  EMAIL_TO                               - optional, mai multe: a@x.ro;b@y.ro
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

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bucharest")
except Exception:                                   # pragma: no cover
    TZ = timezone.utc

# --- Configurare ---------------------------------------------------------
# Medicii urmariti. Numerele se iau din URL-ul paginii de programare:
# ...appointment?specialty=XX&service=YY&doctor=ZZZZ
DOCTORS = [
    {"doctor": "2153", "specialty": "10", "service": "56"},  # Dr. Mustata
    {"doctor": "1641", "specialty": "10", "service": "56"},  # temporar
]
# Cat timp sta activa o rulare si cat de des verifica site-ul in ea.
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES") or 25)
INTERVAL_SECONDS = 60
# Mod test (din "Run workflow"): o singura verificare pe alt medic, fara stare.
TEST_MODE = bool(os.environ.get("TEST_MODE"))
if TEST_MODE:
    DOCTORS = [{"doctor": os.environ["DOCTOR"],
                "specialty": os.environ.get("SPECIALTY") or "10",
                "service": os.environ.get("SERVICE") or "56"}]
# -------------------------------------------------------------------------

BASE = "https://programare.spitalulfilantropia.ro"
STATE_FILE = "state.json"
NO_SLOTS = "Nu exista locuri disponibile"
CHECK_WORDS = {"check", "verifica", "verificare", "verific", "status"}
HELP_WORDS = {"start", "help", "ajutor"}


def env(name):
    return (os.environ.get(name) or "").strip()


def split_list(value):
    return [x for x in re.split(r"[;,\s]+", value or "") if x]


def now_local():
    return datetime.now(TZ)


def api_url(doc):
    return (f"{BASE}/frontend/slots?showAll=true&specialty={doc['specialty']}"
            f"&service={doc['service']}&doctor={doc['doctor']}")


def page_url(doc):
    return (f"{BASE}/appointment?specialty={doc['specialty']}"
            f"&service={doc['service']}&doctor={doc['doctor']}"
            f"&availability=all")


def fetch(url, referer):
    req = urllib.request.Request(url, headers={
        "Accept": "*/*",
        "Referer": referer,
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
        if not m or "slots-container" not in card:
            continue
        date = m.group(1).rsplit("-", 1)[0]          # 2026-10-19
        label = re.search(r'class="badge[^"]*">([^<]+)<', card)
        label = label.group(1).strip() if label else date
        slots_html = card.split("slots-container", 1)[1]
        if NO_SLOTS in slots_html:
            continue
        text = strip_tags(slots_html.split(">", 1)[1])
        times = list(dict.fromkeys(re.findall(r"\b\d{1,2}:\d{2}\b", text)))
        available[date] = {"label": label,
                           "slots": times or [text[:200] or "(interval nou)"]}
    return available, len(cards), doctor_name


# --- Canale de notificare -------------------------------------------------

def telegram_api(method, params, timeout=30):
    token = env("TELEGRAM_TOKEN")
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/{method}", data,
            timeout=timeout) as r:
        return json.loads(r.read().decode())


def telegram_send(chat_id, text):
    telegram_api("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true"})


def send_telegram(subject, lines, link):
    chats = split_list(env("TELEGRAM_CHAT_ID"))
    if not (env("TELEGRAM_TOKEN") and chats):
        return None
    body = [f"🩺 <b>{html.escape(subject)}</b>", ""]
    body += [html.escape(l) for l in lines]
    body += ["", f'<a href="{link}">Deschide pagina de programări</a>']
    for chat in chats:
        telegram_send(chat, "\n".join(body))
    return "Telegram"


def send_email(subject, lines, link):
    user, password = env("GMAIL_USER"), env("GMAIL_APP_PASSWORD")
    if not (user and password):
        return None
    recipients = split_list(env("EMAIL_TO")) or [user]
    msg = EmailMessage()
    msg["Subject"] = f"🩺 {subject}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(lines + ["", f"Programare: {link}"]))
    body = "<br>".join(html.escape(l) for l in lines)
    msg.add_alternative(
        f'<div style="font-family:Arial,sans-serif;font-size:15px;'
        f'line-height:1.5">{body}<br><br>'
        f'<a href="{link}">Deschide pagina de programări</a></div>',
        subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, password.replace(" ", ""))
        refused = s.send_message(msg, to_addrs=recipients)
    if refused:
        raise RuntimeError(f"adrese refuzate: {', '.join(refused)}")
    return f"Email ({len(recipients)} destinatari)"


def send_whatsapp(subject, lines, link):
    phone, apikey = env("WHATSAPP_PHONE"), env("WHATSAPP_APIKEY")
    if not (phone and apikey):
        return None
    text = "\n".join([f"🩺 *{subject}*", ""] + lines + ["", link])
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey})
    urllib.request.urlopen(url, timeout=30).read()
    return "WhatsApp"


def notify(subject, lines, link):
    sent, errors = [], []
    for channel in (send_telegram, send_email, send_whatsapp):
        try:
            name = channel(subject, lines, link)
            if name:
                sent.append(name)
        except Exception as e:                       # un canal picat nu le
            errors.append(f"{channel.__name__}: {e}")  # opreste pe celelalte
    print("  Trimis pe:", ", ".join(sent) or "niciun canal")
    for e in errors:
        print("  EROARE la", e)
    if not sent:
        errors.append("Nicio notificare nu a putut fi trimisă.")
    return errors


# --- Verificarea ----------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    # Format vechi (un singur medic): {"notified": {data: [ore]}}
    if "notified" in state:
        old = state.pop("notified")
        state.setdefault("doctors", {})["2153"] = {
            "name": "", "seen": {d: {"label": d, "slots": s}
                                 for d, s in old.items()}}
    state.setdefault("doctors", {})
    return state


def save_state(state):
    if TEST_MODE:
        return
    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def check_doctor(doc, state):
    """Verifica un medic, trimite notificari pentru schimbari si intoarce
    un rezumat pentru raspunsul la /check."""
    doc_state = state["doctors"].setdefault(doc["doctor"],
                                            {"name": "", "seen": {}})
    result = {"doctor": doc["doctor"], "name": doc_state.get("name") or "",
              "available": {}, "total": 0, "error": None,
              "notify_errors": []}
    try:
        page = fetch(api_url(doc), page_url(doc))
        available, total, name = parse(page)
        if total == 0:
            raise RuntimeError("niciun card în răspuns (verifică specialty/"
                               "service/doctor): " + strip_tags(page)[:150])
    except Exception as e:
        result["error"] = str(e)
        print(f"  [{doc['doctor']}] EROARE: {e}")
        return result                     # starea ramane neschimbata

    name = name or doc_state.get("name") or f"medicul {doc['doctor']}"
    doc_state["name"] = name
    result.update(name=name, available=available, total=total)
    print(f"  [{doc['doctor']}] {name}: {total} zile, "
          f"{len(available)} cu locuri libere.")

    seen = doc_state.get("seen", {})
    today = now_local().strftime("%Y-%m-%d")
    new_lines, gone_lines = [], []
    for d in sorted(available):
        added = [s for s in available[d]["slots"]
                 if s not in seen.get(d, {}).get("slots", [])]
        if added:
            new_lines.append(f"• {available[d]['label']}: " + ", ".join(added))
    for d in sorted(seen):
        if d < today:                     # ziua a trecut, nu e o "ocupare"
            continue
        still = available.get(d, {}).get("slots", [])
        removed = [s for s in seen[d]["slots"] if s not in still]
        if removed:
            gone_lines.append(f"• {seen[d]['label']}: " + ", ".join(removed))

    if new_lines or gone_lines:
        prefix = "[TEST] " if TEST_MODE else ""
        if new_lines and gone_lines:
            subject = f"{prefix}Schimbări la {name}"
        elif new_lines:
            subject = f"{prefix}Locuri libere la {name}"
        else:
            subject = f"{prefix}S-au ocupat locuri la {name}"
        all_lines = [f"• {available[d]['label']}: "
                     + ", ".join(available[d]["slots"])
                     for d in sorted(available)]
        sections = []
        if new_lines:
            sections.append(["🆕 Au apărut:"] + new_lines)
        if gone_lines:
            sections.append(["❌ S-au ocupat:"] + gone_lines)
        # Lista completa doar daca spune ceva in plus fata de "Au aparut"
        if all_lines and all_lines != new_lines:
            sections.append(["📅 Toate locurile libere acum:"] + all_lines)
        elif not all_lines:
            sections.append(["Nu mai este niciun loc liber."])
        lines = []
        for sec in sections:
            lines += ([""] if lines else []) + sec
        result["notify_errors"] = notify(subject, lines, page_url(doc))

    doc_state["seen"] = available
    return result


def check_all(state):
    stamp = now_local().strftime("%H:%M:%S")
    print(f"[{stamp}] Verificare")
    results = [check_doctor(doc, state) for doc in DOCTORS]
    save_state(state)
    return results


def status_message(results):
    lines = [f"🔎 <b>Verificare la {now_local().strftime('%d.%m %H:%M')}</b>"]
    for r in results:
        doc = next(d for d in DOCTORS if d["doctor"] == r["doctor"])
        lines += ["", f"<b>{html.escape(r['name'] or 'Medic ' + r['doctor'])}</b>"]
        if r["error"]:
            lines.append(f"⚠️ Eroare: {html.escape(r['error'][:200])}")
        elif r["available"]:
            lines.append("✅ Locuri libere:")
            lines += [f"• {html.escape(v['label'])}: " + ", ".join(v["slots"])
                      for _, v in sorted(r["available"].items())]
            lines.append(f'<a href="{page_url(doc)}">Programează</a>')
        else:
            lines.append(f"❌ Nu sunt locuri libere ({r['total']} zile verificate)")
    return "\n".join(lines)


HELP_TEXT = ("Scrie <b>check</b> sau <b>verifica</b> (cu sau fără /) și "
             "verific imediat locurile la medicii urmăriți.\n"
             "Notificările pentru locuri noi sau ocupate vin automat.")


# --- Comenzi Telegram -----------------------------------------------------

def telegram_enabled():
    return bool(env("TELEGRAM_TOKEN") and env("TELEGRAM_CHAT_ID")) \
        and not TEST_MODE


def poll_commands(state, timeout):
    """Asteapta mesaje pe Telegram pana la `timeout` secunde. Intoarce
    lista de chat-uri care au cerut verificare si cele care au cerut help."""
    allowed = set(split_list(env("TELEGRAM_CHAT_ID")))
    offset = state.get("telegram_offset", 0)
    try:
        resp = telegram_api("getUpdates", {
            "offset": offset, "timeout": int(max(0, timeout)),
            "allowed_updates": '["message"]'}, timeout=timeout + 15)
    except Exception as e:
        print("  EROARE Telegram getUpdates:", e)
        time.sleep(min(timeout, 10))
        return [], []
    check_chats, help_chats = [], []
    for upd in resp.get("result", []):
        state["telegram_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        if chat not in allowed:
            continue                      # ignoram pe oricine altcineva
        word = (msg.get("text") or "").strip().lower()
        word = word.lstrip("/").split("@")[0].split()[0] if word else ""
        word = word.replace("ă", "a").replace("ș", "s").replace("ş", "s")
        target = check_chats if word in CHECK_WORDS else help_chats
        if chat not in target:
            target.append(chat)
    return check_chats, help_chats


def handle_commands(state, timeout):
    check_chats, help_chats = poll_commands(state, timeout)
    for chat in help_chats:
        if chat not in check_chats:
            try:
                telegram_send(chat, HELP_TEXT)
            except Exception as e:
                print("  EROARE Telegram:", e)
    if not check_chats:
        return None
    print("  Comandă check primită pe Telegram")
    results = check_all(state)
    text = status_message(results)
    for chat in check_chats:
        try:
            telegram_send(chat, text)
        except Exception as e:
            print("  EROARE Telegram:", e)
    return results


# --- Programul principal --------------------------------------------------

def main():
    if TEST_MODE:
        results = check_all({"doctors": {}})
        print("Mod test: starea nu a fost salvată.")
        bad = [r for r in results if r["error"] or r["notify_errors"]]
        sys.exit(1 if bad else 0)

    state = load_state()
    deadline = time.time() + LOOP_MINUTES * 60
    next_check = time.time()
    ok = {d["doctor"]: 0 for d in DOCTORS}
    notify_errors = []

    def record(results):
        for r in results or []:
            if not r["error"]:
                ok[r["doctor"]] += 1
            notify_errors.extend(r["notify_errors"])

    while time.time() < deadline:
        if time.time() >= next_check:
            record(check_all(state))
            next_check = time.time() + INTERVAL_SECONDS
        wait = min(next_check, deadline) - time.time()
        if wait <= 0:
            continue
        if telegram_enabled():
            record(handle_commands(state, min(wait, 50)))
        else:
            time.sleep(wait)

    if telegram_enabled():                # confirmam ultimele mesaje citite
        try:
            telegram_api("getUpdates", {
                "offset": state.get("telegram_offset", 0), "timeout": 0})
        except Exception:
            pass
    save_state(state)

    print("Gata:", ", ".join(f"{d}: {n} verificări reușite"
                             for d, n in ok.items()))
    # Rularea pica (si GitHub trimite email) daca un medic nu a putut fi
    # verificat deloc sau daca un canal de notificare a dat eroare.
    if not all(ok.values()) or notify_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
