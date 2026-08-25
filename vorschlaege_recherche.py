#!/usr/bin/env python3
"""
vorschlaege_recherche.py

Nutzt die Anthropic-API (Claude + serverseitige Websuche) um neue
Dividendenaktien-Kandidaten vorzuschlagen, die noch NICHT in stammdaten.json
stehen. Ergebnis wird an vorschlaege.json angehaengt (nicht ueberschrieben) -
im Bericht erscheinen sie als Karten mit Kopier-Knopf, siehe scan_dividends.py.

WICHTIG - anders als die Finanzdaten-APIs im Hauptskript:
- Das ist KEIN kostenloses Kontingent, sondern echtes, nutzungsbasiertes
  API-Kontingent (Anthropic-Konsole). Deshalb bewusst NUR manuell ausgeloest
  (workflow_dispatch), kein taeglicher Automatik-Lauf.
- Die Websuche laeuft serverseitig bei Anthropic - dieses Skript muss die
  Suchergebnisse nicht selbst verarbeiten, nur die finale Text-Antwort.
"""
import json
import os
import re
import sys
from datetime import date

try:
    import anthropic
except ImportError:
    print("Das 'anthropic'-Paket fehlt - in der GitHub Action wird es per "
          "'pip install anthropic' installiert, siehe Workflow-Datei.")
    sys.exit(1)

STAMMDATEN_PATH = "stammdaten.json"
VORSCHLAEGE_PATH = "vorschlaege.json"
MODELL = "claude-sonnet-5"
ANZAHL_VORSCHLAEGE = 5
MIN_RENDITE = 5.0

# Bevorzugte Quellen - Websuche laesst sich ueber die API nicht hart auf
# bestimmte Domains beschraenken (anders als in diesem Chat-Tool), deshalb
# nur als Empfehlung im Prompt, keine technische Garantie.
BEVORZUGTE_QUELLEN = [
    "finanzen.net", "onvista.de", "aktien.guide", "eulerpool.com",
    "simplywall.st", "stockanalysis.com", "dividendenkalender.de",
]


def lade_json(pfad, default):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def baue_prompt(bekannte_ticker):
    quellen_text = ", ".join(BEVORZUGTE_QUELLEN)
    return f"""Du hilfst bei der Recherche fuer ein privates Dividendenaktien-Tool.

Schlage {ANZAHL_VORSCHLAEGE} NEUE Dividendenaktien vor (weltweit, alle Boersen
erlaubt), die JETZT eine aktuelle Dividendenrendite von mindestens
{MIN_RENDITE:.0f}% haben UND eine Ausschuettungsquote von unter 80% (also
KEINE Dividenden-Falle - Rendite hoch wegen Kursverfall oder nicht
finanzierbarer Ausschuettung waere ein Ausschlussgrund).

Nutze Websuche, um AKTUELLE, ECHTE Zahlen zu pruefen - keine veralteten oder
geratenen Werte. Bevorzuge Quellen wie {quellen_text}, wenn verfuegbar.

Diese Ticker sind BEREITS bekannt - schlage sie NICHT erneut vor:
{', '.join(sorted(bekannte_ticker)) or '(noch keine)'}

Antworte AUSSCHLIESSLICH mit einem JSON-Array, KEINE Erklaerung davor oder
danach, in genau diesem Format (Beispiel mit einem Eintrag):
[
  {{
    "ticker": "TICKER.SUFFIX",
    "name": "Firmenname",
    "land": "Land auf Deutsch",
    "sektor": "Sektor auf Deutsch",
    "dividend_per_share": 1.23,
    "payout_ratio_pct": 45.0,
    "isin": "XX0000000000",
    "geschaetzte_rendite_pct": 6.5,
    "grund": "Kurze, konkrete Begruendung MIT Zahlen (1-2 Saetze) und Quelle"
  }}
]

Felder, die du nicht sicher recherchieren konntest (z.B. ISIN), einfach
weglassen statt zu raten."""


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Kein ANTHROPIC_API_KEY gesetzt - Abbruch.")
        sys.exit(1)

    stammdaten = lade_json(STAMMDATEN_PATH, {})
    vorschlaege = lade_json(VORSCHLAEGE_PATH, [])
    bereits_bekannt = set(stammdaten.keys()) | {v.get("ticker") for v in vorschlaege}

    client = anthropic.Anthropic(api_key=api_key)
    prompt = baue_prompt(bereits_bekannt)

    print(f"Frage {MODELL} nach {ANZAHL_VORSCHLAEGE} neuen Kandidaten (mit Websuche) ...")
    response = client.messages.create(
        model=MODELL,
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Websuche laeuft serverseitig - wir brauchen nur die Text-Bloecke der
    # finalen Antwort, keine eigene Tool-Use-Schleife.
    text_teile = [block.text for block in response.content if block.type == "text"]
    volltext = "\n".join(text_teile).strip()

    match = re.search(r"\[.*\]", volltext, re.DOTALL)
    if not match:
        print("Konnte keine JSON-Liste in der Antwort finden. Antwort-Anfang:")
        print(volltext[:500])
        sys.exit(1)

    try:
        neue_vorschlaege = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print(f"JSON-Parsing fehlgeschlagen: {e}. Antwort-Anfang:")
        print(volltext[:500])
        sys.exit(1)

    heute = date.today().isoformat()
    hinzugefuegt = 0
    for v in neue_vorschlaege:
        ticker = v.get("ticker")
        if not ticker or ticker in bereits_bekannt:
            print(f"  Uebersprungen (schon bekannt oder ungueltig): {ticker}")
            continue
        v["vorgeschlagen_am"] = heute
        vorschlaege.append(v)
        bereits_bekannt.add(ticker)
        hinzugefuegt += 1
        print(f"  + {ticker} ({v.get('name', '?')}) - {v.get('geschaetzte_rendite_pct', '?')}% Rendite")

    with open(VORSCHLAEGE_PATH, "w", encoding="utf-8") as f:
        json.dump(vorschlaege, f, ensure_ascii=False, indent=2)

    print(f"\nFertig: {hinzugefuegt} neue Vorschlaege zu {VORSCHLAEGE_PATH} hinzugefuegt "
          f"({len(vorschlaege)} insgesamt).")


if __name__ == "__main__":
    main()
