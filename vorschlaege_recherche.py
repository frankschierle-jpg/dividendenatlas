#!/usr/bin/env python3
"""
vorschlaege_recherche.py

Nutzt die Anthropic-API (Claude + serverseitige Websuche) um neue
Dividendenaktien-Kandidaten vorzuschlagen, die noch NICHT in stammdaten.json
stehen. Ergebnis wird an vorschlaege.json angehaengt (nicht ueberschrieben) -
im Bericht erscheinen sie als Karten mit Uebernehmen-/Kopier-Knopf, siehe
scan_dividends.py.

WICHTIG - anders als die Finanzdaten-APIs im Hauptskript:
- Das ist KEIN kostenloses Kontingent, sondern echtes, nutzungsbasiertes
  API-Kontingent (Anthropic-Konsole). Deshalb bewusst NUR manuell ausgeloest
  (workflow_dispatch), kein taeglicher Automatik-Lauf.
- Die Websuche laeuft serverseitig bei Anthropic - dieses Skript muss die
  Suchergebnisse nicht selbst verarbeiten.

ABLAUF (dreistufig, jede Stufe fuer sich absicherbar):
  1. RECHERCHE  - das Modell sucht frei im Web und schreibt auf, was es
                  gefunden hat. Freitext ist hier ausdruecklich erlaubt.
  2. STRUKTUR   - derselbe Text wird in einem zweiten, billigen Aufruf OHNE
                  Websuche in ein Werkzeug mit festem Schema gegossen
                  (tool_choice erzwingt den Aufruf). Damit kann die Antwort
                  gar nicht mehr "Prosa statt JSON" sein - genau der Fehler,
                  an dem Lauf #6 gescheitert ist.
  3. PRUEFUNG   - jeder vorgeschlagene Ticker wird gegen die Boerse
                  gegengeprueft: Gibt es ihn ueberhaupt (in Yahoo-
                  Schreibweise)? Wie hoch ist die Rendite aus den
                  TATSAECHLICH gezahlten Ausschuettungen der letzten 12
                  Monate? Kandidaten, die die Mindestrendite real verfehlen,
                  werden aussortiert. Das Modell liefert also nur noch Ideen,
                  die Zahlen kommen aus der Boerse.

Ein Lauf ohne Treffer ist KEIN Fehler: das Skript endet dann mit Code 0 und
einer Erklaerung. Nur echte Stoerungen (fehlender API-Key, API-Ausfall)
brechen mit Code 1 ab.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta

try:
    import anthropic
except ImportError:
    print("Das 'anthropic'-Paket fehlt - in der GitHub Action wird es per "
          "'pip install anthropic' installiert, siehe Workflow-Datei.")
    sys.exit(1)

STAMMDATEN_PATH = "stammdaten.json"
VORSCHLAEGE_PATH = "vorschlaege.json"

# Haiku statt Sonnet - deutlich guenstiger, fuer diese eher mechanische
# Aufgabe (suchen, Zahlen extrahieren) ausreichend. Ueber die Umgebung
# umstellbar, falls ein Lauf mal gruendlicher sein soll.
MODELL = os.environ.get("VORSCHLAEGE_MODELL", "claude-haiku-4-5-20251001")
ANZAHL_VORSCHLAEGE = int(os.environ.get("VORSCHLAEGE_ANZAHL", "3"))
MIN_RENDITE = 5.0
MAX_PAYOUT = 80.0
# Obergrenze fuer Websuchen pro Lauf - begrenzt die Kosten. 8 statt 6: mit
# nur 6 Suchen und ueber 180 auszuschliessenden Tickern kam das Modell
# regelmaessig mit leeren Haenden zurueck.
MAX_SUCHEN = int(os.environ.get("VORSCHLAEGE_MAX_SUCHEN", "8"))
# Grosszuegiges Token-Budget fuer die Recherchestufe: die Suchergebnisse
# zaehlen mit hinein. Mit den alten 2000 Tokens brach das Modell seine
# Antwort mitten in der Begruendung ab ("in der verbleibenden Zeit ...").
MAX_TOKENS_RECHERCHE = int(os.environ.get("VORSCHLAEGE_MAX_TOKENS", "8000"))

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

# Bevorzugte Quellen - Websuche laesst sich ueber die API nicht hart auf
# bestimmte Domains beschraenken, deshalb nur als Empfehlung im Prompt.
BEVORZUGTE_QUELLEN = [
    "stockanalysis.com", "finanzen.net", "onvista.de", "aktien.guide",
    "eulerpool.com", "simplywall.st", "dividendenkalender.de",
]

KANDIDATEN_TOOL = {
    "name": "kandidaten_melden",
    "description": (
        "Meldet die recherchierten Aktien-Kandidaten strukturiert zurueck. "
        "Dieses Werkzeug MUSS aufgerufen werden - auch dann, wenn die Recherche "
        "nichts Brauchbares ergeben hat. In dem Fall wird 'kandidaten' einfach "
        "als leere Liste uebergeben und der Grund in 'hinweis' erklaert."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kandidaten": {
                "type": "array",
                "description": "Die gefundenen Kandidaten. Darf leer sein.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": (
                                "Boersenkuerzel in YAHOO-FINANCE-Schreibweise - das ist "
                                "zwingend, sonst findet das Tool spaeter keinen Kurs. "
                                "Beispiele: 'MAIN' (USA, ohne Suffix), 'BMW.DE' (Xetra), "
                                "'AV.L' (London), '7240.T' (Tokio), 'AGS.BR' (Bruessel), "
                                "'ZURN.SW' (Schweiz), 'CPFE3.SA' (Sao Paulo)."
                            ),
                        },
                        "name": {"type": "string", "description": "Vollstaendiger Firmenname"},
                        "land": {"type": "string", "description": "Land auf Deutsch"},
                        "sektor": {"type": "string", "description": "Sektor auf Deutsch"},
                        "dividend_per_share": {
                            "type": "number",
                            "description": "Jaehrliche Dividende je Aktie in Boersenwaehrung. Weglassen, wenn unsicher.",
                        },
                        "payout_ratio_pct": {
                            "type": "number",
                            "description": "Ausschuettungsquote in Prozent. Weglassen, wenn unsicher.",
                        },
                        "isin": {"type": "string", "description": "ISIN. Weglassen, wenn unsicher."},
                        "geschaetzte_rendite_pct": {
                            "type": "number",
                            "description": "Recherchierte Dividendenrendite in Prozent.",
                        },
                        "grund": {
                            "type": "string",
                            "description": "1-2 Saetze Begruendung MIT konkreten Zahlen und Quelle.",
                        },
                    },
                    "required": ["ticker", "name", "grund"],
                },
            },
            "hinweis": {
                "type": "string",
                "description": (
                    "Kurze Einordnung des Laufs - besonders wichtig, wenn weniger "
                    "Kandidaten gefunden wurden als gewuenscht oder gar keine."
                ),
            },
        },
        "required": ["kandidaten"],
    },
}


def lade_json(pfad, default):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


# ---------------------------------------------------------------------------
# Stufe 3: Gegenpruefung an der Boerse
# ---------------------------------------------------------------------------
def yahoo_abrufen(ticker, timeout=20):
    """Holt Kurs + tatsaechlich gezahlte Ausschuettungen in EINEM Aufruf.
    Bewusst dieselbe kostenlose Quelle wie im Hauptskript, damit ein
    vorgeschlagener Ticker garantiert auch dort funktioniert."""
    url = f"{YAHOO_CHART_URL}/{ticker}?range=2y&interval=1d&events=div"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            daten = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} - Ticker bei Yahoo nicht gefunden"
    except Exception as e:  # Netzwerk, Timeout, kaputtes JSON
        return None, f"Abruf fehlgeschlagen: {e}"

    treffer = ((daten or {}).get("chart") or {}).get("result")
    if not treffer:
        return None, "keine verwertbaren Kursdaten"
    return treffer[0], None


def pruefe_kandidat(ticker, abrufer=yahoo_abrufen):
    """Gibt (geprueft_dict, fehlertext) zurueck. geprueft_dict enthaelt Kurs,
    Waehrung, die Summe der Ausschuettungen der letzten 12 Monate und die
    daraus berechnete Rendite - also genau die Zahlen, die das Tool spaeter
    auch selbst verwendet."""
    ergebnis, fehler = abrufer(ticker)
    if fehler:
        return None, fehler

    meta = ergebnis.get("meta") or {}
    kurs = meta.get("regularMarketPrice")
    if not kurs:
        return None, "kein aktueller Kurs verfuegbar"

    roh = ((ergebnis.get("events") or {}).get("dividends") or {})
    zahlungen = []
    for eintrag in roh.values():
        try:
            ts = int(eintrag["date"])
            betrag = float(eintrag["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if betrag > 0:
            zahlungen.append((ts, betrag))
    zahlungen.sort()

    grenze = (datetime.now() - timedelta(days=365)).timestamp()
    letzte_12m = [(ts, b) for ts, b in zahlungen if ts >= grenze]
    summe = round(sum(b for _, b in letzte_12m), 6)

    geprueft = {
        "kurs": kurs,
        "waehrung": meta.get("currency"),
        "dividende_12m": summe if letzte_12m else None,
        "anzahl_zahlungen_12m": len(letzte_12m),
        "letzte_zahlung": (datetime.fromtimestamp(letzte_12m[-1][0]).strftime("%Y-%m-%d")
                           if letzte_12m else None),
        "rendite_pct": round(summe / kurs * 100, 2) if letzte_12m and kurs else None,
        "geprueft_am": date.today().isoformat(),
    }
    if not letzte_12m:
        return geprueft, "keine Ausschuettung in den letzten 12 Monaten"
    return geprueft, None


# ---------------------------------------------------------------------------
# Stufe 1 + 2: Recherche und Strukturierung
# ---------------------------------------------------------------------------
def baue_recherche_prompt(bekannte_ticker):
    quellen_text = ", ".join(BEVORZUGTE_QUELLEN)
    # Die Ausschlussliste ist inzwischen sehr lang (190+ Ticker). Sie
    # vollstaendig in den Prompt zu schreiben frisst Kontext und lenkt ab -
    # die harte Pruefung passiert ohnehin spaeter in Python. Deshalb hier
    # nur als Hinweis mit gekuerzter Liste.
    liste = sorted(t for t in bekannte_ticker if t)
    gekuerzt = ", ".join(liste[:120])
    rest = f" ... und {len(liste) - 120} weitere" if len(liste) > 120 else ""
    return f"""Du recherchierst fuer ein privates Dividendenaktien-Tool.

AUFGABE: Finde bis zu {ANZAHL_VORSCHLAEGE} Aktien (weltweit, alle Boersen), die
JETZT eine Dividendenrendite von mindestens {MIN_RENDITE:.0f}% haben UND eine
Ausschuettungsquote unter {MAX_PAYOUT:.0f}% - also keine Dividenden-Falle, bei der
die Rendite nur wegen Kursverfall hoch aussieht.

Nutze die Websuche fuer AKTUELLE Zahlen. Bevorzugte Quellen: {quellen_text}.

Schon bekannt, bitte NICHT vorschlagen:
{gekuerzt}{rest}

WICHTIG zur Erwartungshaltung:
- Lieber EIN gut belegter Kandidat als drei geratene. Weniger als
  {ANZAHL_VORSCHLAEGE} ist voellig in Ordnung, null auch.
- Rendite und Dividende werden anschliessend automatisch an der Boerse
  gegengeprueft und notfalls korrigiert - du musst sie nicht auf die zweite
  Nachkommastelle treffen. Entscheidend ist, dass die Firma grundsaetzlich
  ins Raster passt.
- Das Boersenkuerzel muss dagegen EXAKT in Yahoo-Finance-Schreibweise sein
  (z.B. BMW.DE, AV.L, 7240.T, AGS.BR, MAIN) - ein falsches Kuerzel macht den
  Vorschlag wertlos, weil das Tool dazu keinen Kurs findet.

Schreib in dieser Antwort einfach auf, was du gefunden hast, mit Zahlen und
Quellen. Die maschinenlesbare Form kommt in einem zweiten Schritt."""


def recherche_durchfuehren(client, bekannte_ticker):
    print(f"[1/3] Recherche mit {MODELL} (max. {MAX_SUCHEN} Websuchen) ...")
    antwort = client.messages.create(
        model=MODELL,
        max_tokens=MAX_TOKENS_RECHERCHE,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SUCHEN}],
        messages=[{"role": "user", "content": baue_recherche_prompt(bekannte_ticker)}],
    )
    teile = [b.text for b in antwort.content if getattr(b, "type", None) == "text"]
    text = "\n".join(teile).strip()
    suchen = sum(1 for b in antwort.content if getattr(b, "type", None) == "server_tool_use")
    print(f"      {suchen} Websuche(n), {len(text)} Zeichen Rechercheergebnis, "
          f"Stop-Grund: {antwort.stop_reason}")
    if antwort.stop_reason == "max_tokens":
        print("      HINWEIS: Token-Budget ausgeschoepft - Ergebnis kann abgeschnitten sein. "
              "Notfalls VORSCHLAEGE_MAX_TOKENS hochsetzen.")
    return text


def strukturieren(client, recherche_text):
    """Zwingt die Antwort ins Schema. Kein Freitext mehr moeglich, weil
    tool_choice den Werkzeugaufruf vorschreibt."""
    print("[2/3] Ergebnis in feste Form bringen (erzwungener Werkzeugaufruf) ...")
    antwort = client.messages.create(
        model=MODELL,
        max_tokens=2000,
        tools=[KANDIDATEN_TOOL],
        tool_choice={"type": "tool", "name": "kandidaten_melden"},
        messages=[{
            "role": "user",
            "content": (
                "Hier ist das Ergebnis einer Aktienrecherche. Trage die darin genannten "
                "Kandidaten unveraendert in das Werkzeug ein - erfinde nichts dazu und "
                "lass nichts weg. Enthaelt der Text keine konkreten Kandidaten, rufe das "
                "Werkzeug mit einer leeren Liste auf und erklaere es im Hinweis.\n\n"
                "--- Rechercheergebnis ---\n" + (recherche_text or "(leer)")
            ),
        }],
    )
    for block in antwort.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "kandidaten_melden":
            return block.input.get("kandidaten") or [], block.input.get("hinweis") or ""
    return None, ""


def notfall_parsen(text):
    """Letzte Rueckfallebene, falls der Werkzeugaufruf wider Erwarten
    ausbleibt: nach einem JSON-Array im Freitext suchen (auch in ```json-
    Bloecken). Findet sich keins, ist das kein Fehler - dann gab es eben
    keine Kandidaten."""
    if not text:
        return []
    ohne_fences = re.sub(r"```(?:json)?", "", text)
    treffer = re.search(r"\[\s*\{.*\}\s*\]", ohne_fences, re.DOTALL)
    if not treffer:
        return []
    try:
        daten = json.loads(treffer.group(0))
        return daten if isinstance(daten, list) else []
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Kein ANTHROPIC_API_KEY gesetzt - Abbruch.")
        sys.exit(1)

    stammdaten = lade_json(STAMMDATEN_PATH, {})
    vorschlaege = lade_json(VORSCHLAEGE_PATH, [])
    bereits_bekannt = set(stammdaten.keys()) | {v.get("ticker") for v in vorschlaege}

    client = anthropic.Anthropic(api_key=api_key)

    try:
        recherche_text = recherche_durchfuehren(client, bereits_bekannt)
        kandidaten, hinweis = strukturieren(client, recherche_text)
    except Exception as e:
        # Echte Stoerung (Netz, Kontingent, API-Fehler) - die darf auffallen.
        print(f"FEHLER beim API-Aufruf: {type(e).__name__}: {e}")
        sys.exit(1)

    if kandidaten is None:
        print("      Werkzeugaufruf blieb aus - versuche Freitext zu lesen.")
        kandidaten = notfall_parsen(recherche_text)

    if hinweis:
        print(f"      Hinweis des Modells: {hinweis}")

    if not kandidaten:
        print("\nKeine neuen Kandidaten gefunden. Das ist kein Fehler - die Kriterien "
              f"(Rendite >= {MIN_RENDITE:.0f}%, Ausschuettungsquote < {MAX_PAYOUT:.0f}%, "
              f"nicht unter den {len(bereits_bekannt)} bereits bekannten Tickern) sind eng. "
              "vorschlaege.json bleibt unveraendert.")
        return

    print(f"[3/3] {len(kandidaten)} Kandidat(en) an der Boerse gegenpruefen ...")
    heute = date.today().isoformat()
    hinzugefuegt, verworfen = 0, []

    for v in kandidaten:
        ticker = (v.get("ticker") or "").strip()
        if not ticker:
            verworfen.append("(ohne Ticker)")
            continue
        if ticker in bereits_bekannt:
            verworfen.append(f"{ticker}: steht schon in stammdaten.json/vorschlaege.json")
            continue

        geprueft, fehler = pruefe_kandidat(ticker)
        if geprueft is None:
            verworfen.append(f"{ticker}: {fehler}")
            continue
        if fehler:  # Kurs da, aber keine Ausschuettung im Fenster
            verworfen.append(f"{ticker}: {fehler}")
            continue

        rendite = geprueft["rendite_pct"]
        if rendite is None or rendite < MIN_RENDITE:
            verworfen.append(
                f"{ticker}: tatsaechliche Rendite {rendite}% liegt unter {MIN_RENDITE:.0f}% "
                f"(vorgeschlagen war {v.get('geschaetzte_rendite_pct', '?')}%)")
            continue

        v["ticker"] = ticker
        v["geprueft"] = geprueft
        v["vorgeschlagen_am"] = heute
        vorschlaege.append(v)
        bereits_bekannt.add(ticker)
        hinzugefuegt += 1
        print(f"  + {ticker} ({v.get('name', '?')}) - {rendite}% aus "
              f"{geprueft['anzahl_zahlungen_12m']} tatsaechlichen Ausschuettungen "
              f"({geprueft['dividende_12m']} {geprueft.get('waehrung') or ''})")

    for grund in verworfen:
        print(f"  - verworfen: {grund}")

    if hinzugefuegt:
        with open(VORSCHLAEGE_PATH, "w", encoding="utf-8") as f:
            json.dump(vorschlaege, f, ensure_ascii=False, indent=2)
        print(f"\nFertig: {hinzugefuegt} neue Vorschlaege in {VORSCHLAEGE_PATH} "
              f"({len(vorschlaege)} insgesamt).")
    else:
        print("\nKein Kandidat hat die Gegenpruefung bestanden - vorschlaege.json "
              "bleibt unveraendert. Das ist kein Fehler.")


if __name__ == "__main__":
    main()
