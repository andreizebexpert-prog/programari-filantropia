"""
Verifica locurile libere la Spitalul Filantropia pentru un medic si trimite
notificare pe Telegram cand apare un interval nou.

Variabile de mediu necesare:
  TELEGRAM_TOKEN    - token-ul botului (de la @BotFather)
  TELEGRAM_CHAT_ID  - id-ul chat-ului unde vin mesajele
"""
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --- Configurare ---------------------------------------------------------
SPECIALTY = 10   # Obstetrica-Ginec.
SERVICE = 56     # Consult Ambulatoriu CNAS + ecografie CNAS
DOCTOR = 2153    # Dr. MUSTATA LAURA MIHAELA
DOCTOR_NAME = "Dr. Mustață Laura Mihaela"
# -------------------------------------------------------------------------

BASE = "https://programare.spitalulfilantropia.ro"
API_URL = (f"{BASE}/frontend/slots?showAll=true"
           f"&specialty={SPECIALTY}&service={SERVICE}&doctor={DOCTOR}")
PAGE_URL = (f"{BASE}/appointment?specialty={SPECIALTY}"
            f"&service={SERVICE}&doctor={DOCTOR}&availability=all")
STATE_FILE = "state.json"
NO_SLOTS = "Nu exista locuri disponibile"


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
    """Intoarce {data: [intervale]} pentru zilele care AU locuri libere,
    plus numarul total de zile gasite (pentru verificare)."""
    cards = page.split('class="doctor-card-item')[1:]
    available = {}
    for card in cards:
        m = re.search(r'data-card-id="([^"]+)"', card)
        if not m:
            continue
        date = m.group(1).rsplit("-", 1)[0]          # 2026-10-19
        label = re.search(r'class="badge[^"]*">([^<]+)<', card)
        label = label.group(1).strip() if label else date
        slots_html = card.split("slots-container", 1)[1]
        if NO_SLOTS in slots_html:
            continue
        # Ceva a aparut in locul mesajului "nu exista locuri":
        # extragem orele daca se pot citi, altfel textul brut.
        text = strip_tags(slots_html.split(">", 1)[1])
        times = re.findall(r"\b\d{1,2}:\d{2}\b", text)
        available[date] = {"label": label,
                           "slots": times or [text[:200] or "(interval nou)"]}
    return available, len(cards)


def send_telegram(message):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    page = fetch(API_URL)
    available, total = parse(page)
    if total == 0:
        # Pagina nu mai arata cum ne asteptam -> run-ul pica si GitHub
        # trimite email, ca sa stii ca trebuie verificat scriptul.
        print("Nu am gasit niciun card in raspuns. Raspuns:\n", page[:500])
        sys.exit(1)

    print(f"{total} zile verificate, {len(available)} cu locuri libere.")

    state = load_state()
    seen = state.get("notified", {})
    new = {d: v for d, v in available.items()
           if set(v["slots"]) - set(seen.get(d, []))}

    if new:
        lines = [f"🩺 <b>Locuri libere la {DOCTOR_NAME}</b>", ""]
        for d in sorted(new):
            lines.append(f"• <b>{new[d]['label']}</b>: "
                         + ", ".join(new[d]["slots"]))
        lines += ["", f'<a href="{PAGE_URL}">Deschide pagina de programări</a>']
        send_telegram("\n".join(lines))
        print("Notificare trimisă.")

    # Tinem minte doar ce e liber acum: daca un loc se ocupa si apoi se
    # elibereaza din nou, primesti iar notificare.
    state["notified"] = {d: v["slots"] for d, v in available.items()}
    # Se schimba o data pe luna, ca repo-ul sa aiba activitate si GitHub
    # sa nu opreasca verificarea programata dupa 60 de zile.
    state["heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
