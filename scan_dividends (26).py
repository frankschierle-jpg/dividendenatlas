#!/usr/bin/env python3
"""
Dividendenatlas — persoenliches Dividenden-Depot mit Alarm-Ampel
=====================================================================
Zwei getrennte Bereiche:
  1. "Mein Depot"   - deine ECHTEN Positionen (Ticker, Anteile, Kaufkurs,
                       Kaufdatum), die du in my_depot.json eintraegst.
                       Wird taeglich neu bewertet: Kursentwicklung,
                       Rendite auf Kaufkurs, Ausschuettungsquote,
                       Status-Ampel (rot/gelb/gruen).
  2. "Scanner"      - automatisch durchsuchte, bewertete und GERANKTE
                       Kandidatenliste als Inspirationsquelle - komplett
                       getrennt von deinem echten Depot.

WICHTIG: Dieses Tool gibt dir Fakten und Schwellenwert-Ueberschreitungen,
KEINE Kauf-/Verkaufsempfehlungen. Die Entscheidung bleibt bei dir.

Datenquelle: Financial Modeling Prep (FMP), kostenloser API-Key noetig.
Anmeldung: https://site.financialmodelingprep.com/developer/docs

Status-Ampel-Logik fuer "Mein Depot" (Schwellenwerte unten als Konstanten,
jederzeit anpassbar):
  ROT    - Dividende gekuerzt/ausgesetzt ODER Ausschuettungsquote > 100%
  GELB   - Kurs >= 5% unter Kaufkurs
           ODER seit 5 aufeinanderfolgenden Checks tendenziell ruecklaeufig
           ODER neue ruecklaeufige Analysten-Einschaetzung seit letztem Check
  GRUEN  - nichts davon zutreffend
  CHANCE - ein Scanner-Kandidat schlaegt aktuell deine schwaechste Position
           (nur Hinweis, keine eigene Ampel-Farbe im roten/gelben Sinn)

WICHTIG - manuelle Pflege noetig:
Die Scanner-Kandidatenliste (CANDIDATE_UNIVERSE) ist eine solide, aber
NICHT tagesaktuelle Auswahl bekannter Dividend Aristocrats/Kings + einiger
hoeher rentierender, etablierter Zahler. Bitte gelegentlich gegenchecken
und aktualisieren. Die Finanzkennzahlen selbst sind dagegen immer live.
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HISTORY_PATH = os.path.join(BASE_DIR, "history.json")
DEPOT_PATH = os.path.join(BASE_DIR, "my_depot.json")
DISCOVERED_PATH = os.path.join(BASE_DIR, "discovered_candidates.json")
REPORT_PATH = os.path.join(BASE_DIR, "dividendenatlas.html")
RECHERCHE_PATH = os.path.join(BASE_DIR, "recherche.html")
LOG_PATH = os.path.join(BASE_DIR, "dividendenatlas.log")

# ---------------------------------------------------------------------------
# Logo: Muenze mit Wachstumspfeil, in den Marken-Farben (Navy-Blau + Pink).
# Wird dreifach genutzt: neben dem Titel, als Favicon (Browser-Tab) und als
# Homescreen-Icon auf dem Handy (ueber base64-Data-URI, keine externe Datei
# noetig).
# ---------------------------------------------------------------------------
LOGO_SVG = """<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
  <rect width="100" height="100" rx="22" fill="#0F3460"/>
  <circle cx="46" cy="58" r="26" fill="none" stroke="#FFFFFF" stroke-width="5"/>
  <text x="46" y="68" text-anchor="middle" fill="#FFFFFF" font-size="28" font-weight="800" font-family="Arial,sans-serif">€</text>
  <path d="M62 26 L78 26 L78 42" stroke="#E91E63" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M78 26 L56 48" stroke="#E91E63" stroke-width="6" fill="none" stroke-linecap="round"/>
</svg>"""
LOGO_DATA_URI = "data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode("utf-8")).decode("ascii")

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
FMP_STABLE_URL = "https://financialmodelingprep.com/stable"
TWELVE_DATA_URL = "https://api.twelvedata.com"
FINNHUB_URL = "https://finnhub.io/api/v1"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co"
MARKETSTACK_URL = "https://api.marketstack.com/v1"
LEEWAY_URL = "https://api.leeway.tech/api/v1/public"
BRAPI_URL = "https://brapi.dev/api"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_QUOTESUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary"

# --- Alarm-Schwellenwerte fuer "Mein Depot" (jederzeit anpassbar) ----------
YELLOW_PRICE_DROP_PCT = 5.0      # Kurs X% unter Kaufkurs -> gelb
DECLINE_TREND_DAYS = 5           # so viele Laeufe in Folge ruecklaeufig -> gelb
PRICE_HISTORY_KEEP = 60          # so viele Tages-Kurse pro Ticker aufheben

# --- Mindestrendite fuer den SCANNER (jederzeit anpassbar) -----------------
MIN_YIELD_PCT = 5.0              # Aktien unter dieser Rendite gelten als "nicht qualifiziert"
MAX_YIELD_PCT = 8.0              # darueber: Dividenden-Fallen-Warnung statt Qualifikation

# Twelve Data erlaubt im kostenlosen Plan nur 8 Anfragen/Minute (gemessen
# am echten Fehler-Log: "18 credits used, limit being 8") - ohne aktive
# Bremse rennt der Fallback sofort ins 429-Limit, bevor er je Daten
# zurueckbekommt. Etwas konservativer als 8 gewaehlt (6), um Sicherheitsabstand
# zu anderen evtl. gleichzeitig laufenden Anfragen zu haben.
TWELVE_DATA_MAX_PER_MINUTE = 6
_twelve_data_call_times = []


def throttle_twelve_data():
    """Wartet, falls in den letzten 60 Sekunden schon zu viele Twelve-Data-
    Anfragen liefen - verhindert sinnloses Anrennen gegen das 429-Limit."""
    global _twelve_data_call_times
    now = time.time()
    _twelve_data_call_times = [t for t in _twelve_data_call_times if now - t < 60]
    if len(_twelve_data_call_times) >= TWELVE_DATA_MAX_PER_MINUTE:
        wait = 60 - (now - _twelve_data_call_times[0]) + 0.5
        if wait > 0:
            log(f"  Twelve Data Rate-Limit-Schutz: warte {wait:.1f}s ...")
            time.sleep(wait)
    _twelve_data_call_times.append(time.time())

# Alpha Vantage: hartes Limit von 25 Anfragen/TAG (nicht pro Minute) im
# kostenlosen Plan. Da GitHub Actions das Skript nur 1x/Tag startet, reicht
# ein einfacher Zaehler pro Lauf - kein Bedarf, das ueber Laeufe hinweg zu
# speichern. Sicherheitsabstand: bei 22 stoppen, nicht erst bei 25.
ALPHA_VANTAGE_MAX_PER_RUN = 22
_alpha_vantage_calls_used = 0


def alpha_vantage_quota_available():
    return _alpha_vantage_calls_used < ALPHA_VANTAGE_MAX_PER_RUN


# Leeway.tech: 50 Anfragen/Tag im kostenlosen Plan. Sicherheitsabstand:
# bei 45 stoppen.
LEEWAY_MAX_PER_RUN = 45
_leeway_calls_used = 0


def leeway_quota_available():
    return _leeway_calls_used < LEEWAY_MAX_PER_RUN

# ---------------------------------------------------------------------------
# Scanner-Kandidaten: bekannte Dividend Aristocrats/Kings (USA) + solide
# europaeische Langzeit-Dividendenzahler, bewusst ueber viele Sektoren
# gestreut. NICHT vollstaendig, NICHT garantiert tagesaktuell.
# Format: (Ticker, Land, Sektor)
# ---------------------------------------------------------------------------
CANDIDATE_UNIVERSE = [
    ("JNJ", "USA", "Gesundheit"), ("PG", "USA", "Konsumgueter"), ("KO", "USA", "Konsumgueter"),
    ("PEP", "USA", "Konsumgueter"), ("MMM", "USA", "Industrie"), ("MCD", "USA", "Konsumgueter"),
    ("WMT", "USA", "Einzelhandel"), ("LOW", "USA", "Einzelhandel"), ("CAT", "USA", "Industrie"),
    ("CL", "USA", "Konsumgueter"), ("CLX", "USA", "Konsumgueter"), ("ABBV", "USA", "Gesundheit"),
    ("ABT", "USA", "Gesundheit"), ("AFL", "USA", "Versicherung"), ("ADP", "USA", "Dienstleistung"),
    ("BDX", "USA", "Gesundheit"), ("CVX", "USA", "Energie"), ("CB", "USA", "Versicherung"),
    ("CINF", "USA", "Versicherung"), ("CTAS", "USA", "Dienstleistung"), ("DOV", "USA", "Industrie"),
    ("ECL", "USA", "Chemie"), ("EMR", "USA", "Industrie"), ("XOM", "USA", "Energie"),
    ("GD", "USA", "Industrie"), ("GPC", "USA", "Einzelhandel"), ("HRL", "USA", "Konsumgueter"),
    ("ITW", "USA", "Industrie"), ("KMB", "USA", "Konsumgueter"), ("LIN", "USA", "Chemie"),
    ("MKC", "USA", "Konsumgueter"), ("MDT", "USA", "Gesundheit"), ("NUE", "USA", "Grundstoffe"),
    ("PPG", "USA", "Chemie"), ("ROP", "USA", "Industrie"), ("SHW", "USA", "Chemie"),
    ("SYY", "USA", "Konsumgueter"), ("TROW", "USA", "Finanzen"), ("TGT", "USA", "Einzelhandel"),
    ("GWW", "USA", "Industrie"), ("ATO", "USA", "Versorger"), ("ESS", "USA", "Immobilien"),
    ("FRT", "USA", "Immobilien"), ("SPGI", "USA", "Finanzen"), ("ADM", "USA", "Konsumgueter"),
    ("CHD", "USA", "Konsumgueter"), ("SJM", "USA", "Konsumgueter"),
    ("MO", "USA", "Tabak"), ("PM", "USA", "Tabak"), ("VZ", "USA", "Telekom"),
    ("O", "USA", "Immobilien"), ("DOC", "USA", "Immobilien"), ("WPC", "USA", "Immobilien"),
    ("DUK", "USA", "Versorger"), ("SO", "USA", "Versorger"), ("ED", "USA", "Versorger"),
    ("PFE", "USA", "Gesundheit"), ("T", "USA", "Telekom"),
    ("NESN.SW", "Schweiz", "Konsumgueter"), ("ROG.SW", "Schweiz", "Gesundheit"),
    ("ULVR.L", "UK", "Konsumgueter"), ("BATS.L", "UK", "Tabak"), ("IMB.L", "UK", "Tabak"),
    ("DGE.L", "UK", "Konsumgueter"), ("VOD.L", "UK", "Telekom"), ("NG.L", "UK", "Versorger"),
    ("SHEL.L", "UK", "Energie"), ("BP.L", "UK", "Energie"), ("GSK.L", "UK", "Gesundheit"),
    ("ALV.DE", "Deutschland", "Versicherung"), ("MUV2.DE", "Deutschland", "Versicherung"),
    ("BAS.DE", "Deutschland", "Chemie"), ("DTE.DE", "Deutschland", "Telekom"),
    ("SAN.PA", "Frankreich", "Gesundheit"), ("TTE.PA", "Frankreich", "Energie"),
    ("ORA.PA", "Frankreich", "Telekom"), ("ENGI.PA", "Frankreich", "Versorger"),
    ("IBE.MC", "Spanien", "Versorger"), ("ENEL.MI", "Italien", "Versorger"),
    # --- Neu recherchiert (aktuelle "beste Dividendenaktien 2026"-Quellen,
    # Stand August 2026) - erweitert um Kanada, mehr US-Sektoren, hoehere
    # Risikoklasse REITs/BDCs (fuer den Risiko-Filter interessant) ---
    ("ENB", "Kanada", "Energie"), ("EPD", "USA", "Energie"),
    ("BIP", "Kanada", "Infrastruktur"), ("UVV", "USA", "Tabak"),
    ("EPR", "USA", "Immobilien"), ("ARCC", "USA", "Finanzen"),
    ("AGNC", "USA", "Immobilien"), ("PFLT", "USA", "Finanzen"),
    ("DX", "USA", "Immobilien"), ("STAG", "USA", "Immobilien"),
    ("NTGY.MC", "Spanien", "Versorger"),
    # --- Asien (recherchiert): v.a. Singapur-REITs mit dokumentierten
    # >6%-Renditen und "Buy"-Einstufungen, plus ein chinesischer Oelwert.
    # WICHTIG: unsicher, ob unsere drei kostenlosen Datenquellen diese
    # Boersen (SGX, HKEX) ueberhaupt abdecken - unverifiziert, bis der
    # naechste echte Lauf es zeigt.
    ("MINT.SI", "Singapur", "Immobilien"), ("MLT.SI", "Singapur", "Immobilien"),
    ("0857.HK", "China", "Energie"),
    ("MAIN", "USA", "Finanzen"), ("LTC", "USA", "Immobilien"), ("MBG.DE", "Deutschland", "Automobil"),
    # --- Deutschland (finanzen100.de-Recherche vom User, Top-Dividendenrendite) ---
    # WICHTIG: freenet (Ausschuettungsquote 100,29%) und Telefonica Deutschland
    # (-737,62%!) sind selbst laut Quelle bereits auffaellig - klassische
    # Warnmuster, die unser Risiko-Modell als "hoch" einstufen sollte, sobald
    # echte Kennzahlen ankommen. Bewusst trotzdem aufgenommen, damit du das
    # im Tool selbst siehst statt es zu verpassen.
    # Zusaetzlich unsicher: Telefonica Deutschland und Deutsche Wohnen koennten
    # inzwischen von der Boerse genommen / Squeeze-out unterzogen worden sein -
    # falls der Ticker durchgehend scheitert, ist das vermutlich der Grund.
    ("FNTN.DE", "Deutschland", "Telekom"), ("VOW3.DE", "Deutschland", "Automobil"),
    ("VNA.DE", "Deutschland", "Immobilien"),
    ("PAH3.DE", "Deutschland", "Automobil"), ("DWNI.DE", "Deutschland", "Immobilien"),
    ("LEG.DE", "Deutschland", "Immobilien"), ("DEQ.DE", "Deutschland", "Immobilien"),
    ("HNR1.DE", "Deutschland", "Versicherung"), ("SAX.DE", "Deutschland", "Medien"),
    ("SIX2.DE", "Deutschland", "Autovermietung"),
    # --- Japan/Asien (Parqet-Recherche) - bewusst NICHT vorgefiltert, das
    # Risiko soll das Tool selbst zeigen. Nur Ticker aufgenommen, deren
    # Tokio-Boersen-Kuerzel mir sicher bekannt sind - viele der anderen
    # Parqet-Kandidaten sind sehr obskure Kleinstfirmen, deren exakten
    # Ticker ich nicht sicher genug kenne, um sie nicht falsch einzutragen.
    # UNSICHER: ob unsere 5 Datenquellen Tokio (TSE) ueberhaupt abdecken -
    # noch nicht getestet, kann auch hier komplett scheitern.
    ("5401.T", "Japan", "Grundstoffe"),  # Nippon Steel, ~10,89% Rendite laut Parqet
    # Weitere Parqet-Japan/HK-Kandidaten - Tokio-Kuerzel nach bestem Wissen,
    # NICHT alle mit letzter Sicherheit geprueft. Scheitert ein Ticker,
    # wird er einfach uebersprungen (kein Schaden) - bewusst inkl. zwei
    # REITs mit Ausschuettungsquote >100% als reale "Falle"-Beispiele
    # fuer den Risiko-Filter.
    ("9364.T", "Japan", "Logistik"), ("5938.T", "Japan", "Baustoffe"),
    ("8395.T", "Japan", "Finanzen"), ("7177.T", "Japan", "Finanzen"),
    ("8616.T", "Japan", "Finanzen"), ("4183.T", "Japan", "Chemie"),
    ("3002.T", "Japan", "Konsumgueter"), ("1833.T", "Japan", "Industrie"),
    ("8985.T", "Japan", "Immobilien"),  # Japan Hotel REIT, Ausschuett. ~106% - Falle
    ("8963.T", "Japan", "Immobilien"),  # Invincible Investment, Ausschuett. ~112% - Falle
    ("8960.T", "Japan", "Immobilien"), ("8972.T", "Japan", "Immobilien"),
    ("8150.T", "Japan", "Industrie"), ("7240.T", "Japan", "Automobil"),
    ("0010.HK", "China", "Immobilien"),  # Hang Lung Group
    ("NDA-FI.HE", "Finnland", "Finanzen"),  # Nordea Bank, Nordische Grossbank
    # --- Grosse Nutzer-Recherche-Ergaenzung (mehrere Quellen, teils
    # widerspruechlich zu unserer vorherigen Recherche - siehe Kommentare
    # bei einzelnen Titeln in MANUAL_RESEARCH_DATA). Nur Ticker aufgenommen,
    # bei denen ich mir der Boersen-Kennung sicher genug bin. ---
    ("BMW.DE", "Deutschland", "Automobil"), ("DTG.DE", "Deutschland", "Automobil"),
    ("CON.DE", "Deutschland", "Automobil"), ("SDF.DE", "Deutschland", "Chemie"),
    ("EVK.DE", "Deutschland", "Chemie"), ("WCH.DE", "Deutschland", "Chemie"),
    ("ET", "USA", "Energie"), ("KMI", "USA", "Energie"), ("OKE", "USA", "Energie"),
    ("NLY", "USA", "Immobilien"), ("OHI", "USA", "Immobilien"), ("CCI", "USA", "Immobilien"),
    ("DOW", "USA", "Chemie"), ("LYB", "USA", "Chemie"), ("TFC", "USA", "Finanzen"),
    ("KEY", "USA", "Finanzen"), ("KIM", "USA", "Immobilien"),
    ("TRP", "Kanada", "Energie"), ("BCE", "Kanada", "Telekom"), ("TU", "Kanada", "Telekom"),
    ("BNS", "Kanada", "Finanzen"), ("MFC", "Kanada", "Finanzen"), ("PBA", "Kanada", "Energie"),
    ("WCP", "Kanada", "Energie"),  # Whitecap Resources - TSX, monatliche Dividende
    ("LGEN.L", "UK", "Finanzen"), ("AV.L", "UK", "Versicherung"), ("MNG.L", "UK", "Finanzen"),
    ("HSBA.L", "UK", "Finanzen"), ("GLEN.L", "UK", "Grundstoffe"), ("TW.L", "UK", "Immobilien"),
    ("ISP.MI", "Italien", "Finanzen"), ("SRG.MI", "Italien", "Versorger"), ("TRN.MI", "Italien", "Versorger"),
    ("MB.MI", "Italien", "Finanzen"), ("UCG.MI", "Italien", "Finanzen"),
    ("ELE.MC", "Spanien", "Versorger"), ("CS.PA", "Frankreich", "Versicherung"), ("EN.PA", "Frankreich", "Industrie"),
    ("SREN.SW", "Schweiz", "Versicherung"), ("ZURN.SW", "Schweiz", "Versicherung"),
    ("PBR", "Brasilien", "Energie"), ("BBAS3.SA", "Brasilien", "Finanzen"),
    ("CPFE3.SA", "Brasilien", "Versorger"), ("BBSE3.SA", "Brasilien", "Versicherung"),
    ("EC", "Kolumbien", "Energie"),
    ("SWED-A.ST", "Schweden", "Finanzen"), ("TRYG.CO", "Daenemark", "Versicherung"),
    ("STG.CO", "Daenemark", "Konsumgueter"),
    # --- Verifizierungsrunde: echte Ticker fuer zuvor zurueckgestellte
    # Kleinwerte gefunden (ISIN/WKN gegengeprueft). ---
    ("BIJ.DE", "Deutschland", "Einzelhandel"), ("HABA.DE", "Deutschland", "Immobilien"),
    ("HOT.DE", "Deutschland", "Bau"), ("PWO.DE", "Deutschland", "Automobilzulieferer"),
    ("MUX.DE", "Deutschland", "Beteiligungen"), ("JST.DE", "Deutschland", "Automobilzulieferer"),
    ("030000.KS", "Suedkorea", "Marketing"), ("035250.KS", "Suedkorea", "Freizeit"),
    ("004990.KS", "Suedkorea", "Mischkonzern"),
    # --- Zweite Verifizierungsrunde: weitere echte Ticker bestaetigt ---
    ("B7E.DE", "Deutschland", "Beteiligungen"), ("WSU.DE", "Deutschland", "Industrie"),
    ("HAW.DE", "Deutschland", "Konsumgueter"), ("NWX.DE", "Deutschland", "Handel"),
    ("LEI.DE", "Deutschland", "Konsumgueter"), ("M12.DE", "Deutschland", "Gesundheit"),
    ("EDL.DE", "Deutschland", "Medien"),
    ("BCH", "Chile", "Finanzen"),
]

# Twelve Data nutzt "SYMBOL:BOERSE" statt FMP's "SYMBOL.LAND" - Mapping der
# Endungen, die in CANDIDATE_UNIVERSE verwendet werden, auf Twelve-Data-Boersen.
# Falls eine Boersen-Kuerzung nicht stimmt, scheitert nur dieser eine Ticker
# bei Twelve Data (wird geloggt), der Rest laeuft unbeeinflusst weiter.
TWELVE_DATA_EXCHANGE_MAP = {
    ".DE": "XETRA", ".SW": "SIX", ".L": "LSE",
    ".PA": "EURONEXT", ".MC": "BME", ".MI": "BIT",
    ".SI": "SGX", ".HK": "HKEX", ".T": "TSE",
}


def to_twelve_data_symbol(ticker):
    """Wandelt z.B. 'BAS.DE' in 'BAS:XETRA' um. US-Ticker ohne Punkt
    bleiben unveraendert (Twelve Data braucht dort keine Boersen-Angabe)."""
    for suffix, exchange in TWELVE_DATA_EXCHANGE_MAP.items():
        if ticker.endswith(suffix):
            return f"{ticker[:-len(suffix)]}:{exchange}"
    return ticker


# Leeway.tech nutzt "SYMBOL.BOERSENCODE" (z.B. bestaetigtes Doku-Beispiel:
# "DTE.XETRA" fuer Deutsche Telekom). Nur XETRA ist aus der Dokumentation
# WIRKLICH bestaetigt - die anderen sind meine beste Vermutung anhand
# gaengiger Boersen-Kuerzel, noch nicht verifiziert.
LEEWAY_EXCHANGE_MAP = {
    ".DE": "XETRA", ".PA": "XPAR", ".MC": "XMAD", ".MI": "XMIL",
    ".L": "XLON", ".SW": "XSWX",
}


def to_leeway_symbol(ticker):
    """Wandelt z.B. 'DTE.DE' in 'DTE.XETRA' um."""
    for suffix, exchange in LEEWAY_EXCHANGE_MAP.items():
        if ticker.endswith(suffix):
            return f"{ticker[:-len(suffix)]}.{exchange}"
    return ticker


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    """Liest die API-Keys entweder aus Umgebungsvariablen (fuer GitHub
    Actions: als Secrets hinterlegt) ODER aus einer lokalen config.json
    (fuer manuelle Laeufe auf deinem Mac). Umgebungsvariablen haben
    Vorrang, falls beide da sind."""
    env_keys = {
        "fmp_api_key": os.environ.get("FMP_API_KEY"),
        "twelve_data_api_key": os.environ.get("TWELVE_DATA_API_KEY"),
        "finnhub_api_key": os.environ.get("FINNHUB_API_KEY"),
        "alpha_vantage_api_key": os.environ.get("ALPHA_VANTAGE_API_KEY"),
        "marketstack_api_key": os.environ.get("MARKETSTACK_API_KEY"),
        "leeway_api_key": os.environ.get("LEEWAY_API_KEY"),
        "brapi_api_key": os.environ.get("BRAPI_API_KEY"),
        "financialdata_api_key": os.environ.get("FINANCIALDATA_API_KEY"),
        "eodhd_api_key": os.environ.get("EODHD_API_KEY"),
    }
    if any(env_keys.values()):
        return env_keys
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"Weder Umgebungsvariablen (FMP_API_KEY etc.) noch config.json unter "
            f"{CONFIG_PATH} gefunden. Lokal: config.json anlegen mit den 8 API-Keys "
            f"(der achte, financialdata_api_key, ist optional). "
            f"In GitHub Actions: die Secrets hinterlegen, siehe README."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_depot():
    """Laedt deine echten Positionen. Legt beim ersten Mal eine
    Beispiel-Datei mit Erklaerung an, falls noch keine existiert."""
    if not os.path.exists(DEPOT_PATH):
        example = {
            "_hinweis": (
                "Trage hier deine ECHTEN Positionen ein: Ticker, Anzahl "
                "Anteile, Kaufkurs (pro Anteil), Kaufdatum (YYYY-MM-DD). "
                "next_payout_manual nur ausfuellen, falls die API keinen "
                "Termin liefert - sonst einfach null lassen."
            ),
            "positionen": [
                {"ticker": "JNJ", "anteile": 10, "kaufkurs": 155.00,
                 "kaufdatum": "2026-01-15", "next_payout_manual": None}
            ],
        }
        save_json(DEPOT_PATH, example)
        log(f"my_depot.json existierte nicht - Beispiel-Datei angelegt unter {DEPOT_PATH}")
        return []
    data = load_json(DEPOT_PATH, {"positionen": []})
    return data.get("positionen", [])


_fmp_quota_exhausted = False  # wird True, sobald FMP einmal mit "Limit Reach"
# (Tageskontingent voll) antwortet. Ab dann wird JEDER weitere FMP-Call in
# DIESEM Lauf uebersprungen (kein Netzwerk-Request mehr) - vorher wurde
# trotzdem fuer jeden einzelnen Ticker weiter versucht, obwohl das
# Kontingent laengst leer war. Das kostete bei 90+ nordamerikanischen
# Tickern (FMP steht dort an erster Stelle der Kette) unnoetig ~180-200
# Anfragen, die von vornherein zum Scheitern verurteilt waren.


def api_get(endpoint, api_key, params=None, base_url=None, key_param="apikey", source_label="FMP"):
    """Generischer GET-Helfer fuer alle Datenquellen - der Name des
    Key-Parameters unterscheidet sich zwischen den Anbietern (apikey vs.
    token). Bei api_key=None wird KEIN Key-Parameter angehaengt (fuer
    Quellen wie Frankfurter, die gar keinen Key brauchen)."""
    global _fmp_quota_exhausted
    if source_label == "FMP" and _fmp_quota_exhausted:
        return None
    params = params or {}
    if api_key is not None:
        params[key_param] = api_key
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base_url or FMP_BASE_URL}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()[:150]
        log(f"  {source_label}-Fehler (HTTP {e.code}) bei {endpoint}: {body}")
        if source_label == "FMP" and e.code == 429 and b"Limit Reach" in body:
            if not _fmp_quota_exhausted:
                log("  FMP-Tageskontingent erreicht - FMP wird fuer den Rest "
                    "dieses Laufs uebersprungen (spart Anfragen, die ohnehin "
                    "scheitern wuerden).")
            _fmp_quota_exhausted = True
        return None
    except Exception as e:
        log(f"  {source_label}-Fehler bei {endpoint}: {e}")
        return None


# Rueckwaerts-kompatibler Alias, da fmp_get() an mehreren Stellen im
# Skript bereits verwendet wird (Grades, Next-Payout).
def fmp_get(endpoint, api_key, params=None, base_url=None):
    return api_get(endpoint, api_key, params=params, base_url=base_url,
                   key_param="apikey", source_label="FMP")


def fetch_fundamentals_fmp(ticker, api_key):
    """Nutzt die neuen 'stable'-Endpunkte (Symbol als Query-Parameter),
    da die alten /v3/-Endpunkte fuer neu registrierte FMP-Nutzer (nach
    31.08.2025) abgeschaltet wurden."""
    if not api_key:
        return None
    quote = fmp_get("quote", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    ratios = fmp_get("ratios-ttm", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    if not quote or not ratios:
        return None
    q = quote[0] if isinstance(quote, list) and quote else None
    r = ratios[0] if isinstance(ratios, list) and ratios else None
    if not q or not r:
        return None
    return {
        "ticker": ticker,
        "name": q.get("name"),
        "price": q.get("price"),
        "market_cap": q.get("marketCap"),
        "pe_ratio": q.get("pe") or r.get("priceToEarningsRatioTTM"),
        "dividend_yield_pct": (r.get("dividendYielPercentageTTM")
                                or r.get("dividendYieldPercentageTTM")
                                or (r.get("dividendYieldTTM", 0) or 0) * 100),
        "payout_ratio_pct": (r.get("dividendPayoutRatioTTM", 0) or 0) * 100,
        "debt_to_equity": r.get("debtToEquityRatioTTM"),
        "current_ratio": r.get("currentRatioTTM"),
        "data_source": "fmp",
    }


def fetch_fundamentals_twelvedata(ticker, api_key):
    """Ausweich-Quelle Nr. 1, falls FMP diesen Ticker nicht liefert (z.B.
    wegen 402 'Premium'-Sperre oder 429-Tageslimit). Nutzt /quote fuer
    Kurs/Name und /statistics fuer Ausschuettungsquote/Verschuldung/KGV.
    Symbol-Format unterscheidet sich von FMP: 'BAS:XETRA' statt 'BAS.DE',
    siehe to_twelve_data_symbol().

    WICHTIG: /statistics ist im kostenlosen Twelve-Data-Plan gesperrt
    (bestaetigt per 403 "exclusively with pro or ultra..."). Wir fragen
    DESHALB zuerst /statistics ab - schlaegt das fehl, brechen wir sofort
    ab, ohne den zweiten Aufruf (/quote) zu verschwenden. Ausserdem eine
    Rate-Limit-Bremse (throttle_twelve_data), da der kostenlose Plan nur
    8 Anfragen/Minute erlaubt."""
    if not api_key:
        return None
    td_symbol = to_twelve_data_symbol(ticker)
    throttle_twelve_data()
    stats = api_get("statistics", api_key, params={"symbol": td_symbol}, base_url=TWELVE_DATA_URL,
                     source_label="Twelve Data")
    stat = (stats or {}).get("statistics", {}) if isinstance(stats, dict) else {}
    if not stat:
        log(f"  Twelve Data lieferte keine Kennzahlen fuer {td_symbol} "
            f"(vermutlich /statistics im Free-Plan gesperrt) - werte als Fehlschlag, "
            f"spare mir den Kurs-Aufruf")
        return None
    throttle_twelve_data()
    quote = api_get("quote", api_key, params={"symbol": td_symbol}, base_url=TWELVE_DATA_URL,
                     source_label="Twelve Data")
    if not quote or quote.get("status") == "error":
        log(f"  Twelve-Data-Fehler bei {td_symbol}: {(quote or {}).get('message', 'keine Antwort')}")
        return None
    dividends = stat.get("dividends_and_splits", {}) if isinstance(stat, dict) else {}
    valuation = stat.get("valuations_metrics", {}) if isinstance(stat, dict) else {}
    financials = stat.get("financials", {}) if isinstance(stat, dict) else {}
    try:
        price = float(quote.get("close")) if quote.get("close") else None
    except (TypeError, ValueError):
        price = None
    return {
        "ticker": ticker,
        "name": quote.get("name"),
        "price": price,
        "market_cap": valuation.get("market_capitalization"),
        "pe_ratio": valuation.get("trailing_pe"),
        "dividend_yield_pct": (dividends.get("forward_annual_dividend_yield", 0) or 0) * 100,
        "payout_ratio_pct": (dividends.get("payout_ratio", 0) or 0) * 100,
        "debt_to_equity": (financials.get("balance_sheet", {}) or {}).get("total_debt_to_equity"),
        "current_ratio": None,
        "data_source": "twelvedata",
    }


def fetch_fundamentals_finnhub(ticker, api_key):
    """Ausweich-Quelle Nr. 2, falls weder FMP noch Twelve Data liefern.
    Finnhub nutzt denselben Punkt-Notation-Stil wie FMP (z.B. 'BAS.DE'),
    daher KEINE Symbol-Umwandlung noetig - anders als bei Twelve Data.
    Braucht drei Aufrufe: /quote (Kurs), /stock/profile2 (Name/Marktkap.),
    /stock/metric (Ausschuettungsquote/Verschuldung/KGV/Rendite)."""
    if not api_key:
        return None
    quote = api_get("quote", api_key, params={"symbol": ticker}, base_url=FINNHUB_URL,
                     key_param="token", source_label="Finnhub")
    profile = api_get("stock/profile2", api_key, params={"symbol": ticker}, base_url=FINNHUB_URL,
                       key_param="token", source_label="Finnhub")
    metrics = api_get("stock/metric", api_key, params={"symbol": ticker, "metric": "all"},
                       base_url=FINNHUB_URL, key_param="token", source_label="Finnhub")
    if not quote or quote.get("c") in (None, 0):
        log(f"  Finnhub-Fehler bei {ticker}: keine verwertbare Kursantwort")
        return None
    m = (metrics or {}).get("metric", {}) if isinstance(metrics, dict) else {}
    if not m:
        # /stock/metric ist entweder rate-limitiert (429) oder liefert
        # nichts - ohne echte Kennzahlen ist der Kurs allein nutzlos fuer
        # unsere Zwecke. Lieber ehrlich als Fehlschlag werten, statt eine
        # Aktie mit lauter 0%/None-Werten als "erfolgreich geladen" zu fuehren.
        log(f"  Finnhub lieferte nur Kurs, keine Kennzahlen fuer {ticker} "
            f"(vermutlich Rate-Limit bei /stock/metric) - werte als Fehlschlag")
        return None
    return {
        "ticker": ticker,
        "name": (profile or {}).get("name"),
        "price": quote.get("c"),
        "market_cap": (profile or {}).get("marketCapitalization"),
        "pe_ratio": m.get("peTTM") or m.get("peAnnual"),
        "dividend_yield_pct": m.get("currentDividendYieldTTM") or m.get("dividendYieldIndicatedAnnual") or 0,
        "payout_ratio_pct": (m.get("payoutRatioTTM", 0) or 0) * 100
                            if m.get("payoutRatioTTM", 0) and m.get("payoutRatioTTM", 0) < 5
                            else (m.get("payoutRatioTTM") or 0),
        "debt_to_equity": m.get("totalDebt/totalEquityQuarterly") or m.get("totalDebt/totalEquityAnnual"),
        "current_ratio": m.get("currentRatioQuarterly"),
        "data_source": "finnhub",
    }


def fetch_fundamentals_alphavantage(ticker, api_key):
    """Ausweich-Quelle Nr. 3: Alpha Vantage OVERVIEW-Endpunkt. Anders als
    Twelve Data/Finnhub gibt es Hinweise auf ECHTE globale Abdeckung im
    kostenlosen Plan - aber hartes 25-Anfragen/Tag-Limit, siehe
    alpha_vantage_quota_available(). Keine direkte 'Ausschuettungsquote'
    im Datensatz - wird aus DividendPerShare/EPS berechnet. Verschuldung
    (debt_to_equity) ist im OVERVIEW-Endpunkt nicht enthalten und bleibt
    None (wird von der Risiko-Bewertung als neutral behandelt)."""
    global _alpha_vantage_calls_used
    if not api_key or not alpha_vantage_quota_available():
        return None
    _alpha_vantage_calls_used += 1
    data = api_get("query", api_key, params={"function": "OVERVIEW", "symbol": ticker},
                   base_url=ALPHA_VANTAGE_URL, key_param="apikey", source_label="Alpha Vantage")
    if not data or not data.get("Symbol") or data.get("Note") or data.get("Information"):
        # "Note"/"Information" statt echter Daten = Rate-Limit erreicht
        # oder Ticker nicht gefunden
        log(f"  Alpha-Vantage-Fehler bei {ticker}: {(data or {}).get('Note') or (data or {}).get('Information') or 'kein Symbol in Antwort'}")
        return None
    try:
        dividend_yield_pct = float(data.get("DividendYield") or 0) * 100
    except (TypeError, ValueError):
        dividend_yield_pct = 0
    try:
        dps = float(data.get("DividendPerShare") or 0)
        eps = float(data.get("EPS") or 0)
        payout_ratio_pct = (dps / eps * 100) if eps > 0 else None
    except (TypeError, ValueError):
        payout_ratio_pct = None
    try:
        price = float(data.get("AnalystTargetPrice") or 0) or None
    except (TypeError, ValueError):
        price = None
    return {
        "ticker": ticker,
        "name": data.get("Name"),
        "price": price,
        "market_cap": data.get("MarketCapitalization"),
        "pe_ratio": data.get("PERatio"),
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio_pct": payout_ratio_pct,
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "alphavantage",
    }


FALLBACK_FX_RATES_TO_EUR = {
    "EUR": 1.0, "USD": 0.92, "GBP": 1.17, "CHF": 1.06, "JPY": 0.0061,
    "HKD": 0.118, "SGD": 0.685, "KRW": 0.00067, "BRL": 0.17,
}


def fetch_live_fx_rates():
    """Holt echte, aktuelle Wechselkurse ueber Frankfurter - komplett
    kostenlos, KEIN API-Key noetig, keine Anmeldung (offizielle EZB-Daten,
    taeglich aktualisiert, 32 Hauptwaehrungen). Faellt bei Fehler auf die
    festen Naeherungswerte zurueck (FALLBACK_FX_RATES_TO_EUR)."""
    data = api_get("latest", None, params={"base": "EUR"},
                    base_url=FRANKFURTER_URL, source_label="Frankfurter")
    rates_eur_base = (data or {}).get("rates")  # z.B. {"USD": 1.09, "GBP": 0.86, ...} = 1 EUR ist X der Waehrung
    if not rates_eur_base:
        log("  Frankfurter-Fehler: keine verwertbare Antwort, nutze feste Naeherungswerte")
        return dict(FALLBACK_FX_RATES_TO_EUR)
    result = {"EUR": 1.0}
    for ccy in ("USD", "GBP", "CHF", "JPY", "HKD", "SGD", "KRW", "BRL"):
        if ccy in rates_eur_base and rates_eur_base[ccy]:
            # rates_eur_base[ccy] = wie viel dieser Waehrung 1 EUR ist ->
            # umgekehrt: wie viel EUR ist 1 Einheit dieser Waehrung
            result[ccy] = round(1 / rates_eur_base[ccy], 6)
        else:
            result[ccy] = FALLBACK_FX_RATES_TO_EUR.get(ccy, 1.0)
    log(f"  Live-Wechselkurse geholt: {result}")
    return result


def fetch_fundamentals_brapi(ticker, api_key):
    """Suedamerika-priorisierte Quelle: brapi.dev, ein dediziertes B3/
    Brasilien-Tool (nicht als 'global' getarnt). Braucht das Ticker-Format
    ohne '.SA'-Endung (z.B. 'PETR4' statt 'PETR4.SA'). WICHTIG: die
    Dividendenrendite steckt NICHT in der einfachen Kurs-Antwort, sondern
    nur wenn man explizit modules=defaultKeyStatistics + dividends=true
    mit anfragt (live gegengeprueft anhand offizieller Doku/Beispiele)."""
    if not api_key:
        return None
    brapi_symbol = ticker[:-3] if ticker.endswith(".SA") else ticker
    data = api_get(f"quote/{brapi_symbol}", api_key,
                    params={"modules": "defaultKeyStatistics", "dividends": "true"},
                    base_url=BRAPI_URL, key_param="token", source_label="brapi.dev")
    results = (data or {}).get("results") if isinstance(data, dict) else None
    if not results:
        log(f"  brapi.dev-Fehler bei {brapi_symbol}: keine verwertbare Antwort")
        return None
    r = results[0]
    key_stats = r.get("defaultKeyStatistics") or {}
    try:
        dividend_yield_pct = float(key_stats.get("dividendYield") or 0) * 100
    except (TypeError, ValueError):
        dividend_yield_pct = 0
    if not dividend_yield_pct:
        log(f"  brapi.dev lieferte keine Dividendenrendite fuer {brapi_symbol} - werte als Fehlschlag")
        return None
    return {
        "ticker": ticker,
        "name": r.get("longName") or r.get("shortName"),
        "price": r.get("regularMarketPrice"),
        "market_cap": key_stats.get("marketCap") or r.get("marketCap"),
        "pe_ratio": key_stats.get("trailingPE") or key_stats.get("forwardPE"),
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio_pct": None,
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "brapi",
    }


def fetch_fundamentals_leeway(ticker, api_key):
    """Europa-priorisierte Quelle: Leeway.tech. Dokumentation nennt
    explizit XETRA/Euronext-Unterstuetzung und Dividendendaten (nicht
    nur Marketing-Floskel 'global') - aber NOCH NICHT selbst mit echtem
    Key getestet. Nur XETRA-Boersencode ist aus der Doku bestaetigt."""
    if not api_key or not leeway_quota_available():
        return None
    global _leeway_calls_used
    _leeway_calls_used += 1
    leeway_symbol = to_leeway_symbol(ticker)
    data = api_get(f"fundamentals/{leeway_symbol}", api_key, base_url=LEEWAY_URL,
                    key_param="apitoken", source_label="Leeway.tech")
    if not data or not isinstance(data, dict):
        log(f"  Leeway-Fehler bei {leeway_symbol}: keine verwertbare Antwort")
        return None
    general = data.get("General", {})
    highlights = data.get("Highlights", {})
    valuation = data.get("Valuation", {})
    # Genauer Feldname fuer den aktuellen Kurs bei Leeway ist noch nicht
    # bestaetigt (nicht in der Doku-Kurzfassung gesehen) - bleibt bewusst
    # None statt einen falschen Feldnamen zu raten.
    price = None
    try:
        dividend_yield_pct = float(highlights.get("DividendYield") or 0) * 100
    except (TypeError, ValueError):
        dividend_yield_pct = 0
    if not dividend_yield_pct:
        log(f"  Leeway lieferte keine Dividendenrendite fuer {leeway_symbol} - werte als Fehlschlag")
        return None
    return {
        "ticker": ticker,
        "name": general.get("Name"),
        "price": price,
        "market_cap": highlights.get("MarketCapitalization"),
        "pe_ratio": highlights.get("PERatio") or valuation.get("TrailingPE"),
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio_pct": None,  # nicht sicher, ob Leeway das getrennt ausweist
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "leeway",
    }


def fetch_fundamentals_marketstack(ticker, api_key):
    """Ausweich-Quelle Nr. 4: Marketstack. WICHTIG - unsicher,
    ob Marketstack ueberhaupt Dividendenkennzahlen liefert (Recherche
    deutete auf reine Kursdaten hin) - falls ja, wird es hier ausgewertet;
    falls nein, scheitert diese Funktion einfach sauber (kein Schaden)."""
    if not api_key:
        return None
    data = api_get("eod/latest", api_key, params={"symbols": ticker},
                    base_url=MARKETSTACK_URL, key_param="access_key", source_label="Marketstack")
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    if not rows:
        log(f"  Marketstack-Fehler bei {ticker}: keine Kursdaten erhalten")
        return None
    row = rows[0]
    # Marketstack liefert (Stand unserer Recherche) keine Dividendenrendite -
    # ohne die kann die Aktie unseren Mindestrendite-Filter nicht erfuellen,
    # daher hier bewusst als Fehlschlag werten statt eine unbrauchbare
    # 0%-Rendite als "Erfolg" auszugeben.
    log(f"  Marketstack lieferte nur Kursdaten fuer {ticker}, keine Dividendenrendite - "
        f"werte als Fehlschlag (Marketstack scheint keine Dividendenkennzahlen zu fuehren)")
    return None


# ---------------------------------------------------------------------------
# Manuelle Recherche-Datenbank - letzter Fallback, falls ALLE APIs scheitern
# (was bei vielen internationalen Tickern staendig passiert, siehe die
# vielen 402/403/429-Fehler oben). Werte stammen aus echten, im Chat
# recherchierten Artikeln (dividenden.guru, finanzen100.de, Parqet Japan
# u.a.) - siehe "quelle"/"stand" je Eintrag. KEIN Live-Kurs verfuegbar,
# daher price=None (UI zeigt dann "-" statt eines Kurses, aber Rendite/
# Risiko-Bewertung funktionieren trotzdem normal).
# Format: ticker -> {name, dividend_yield_pct, payout_ratio_pct, quelle, stand}
# ---------------------------------------------------------------------------
MANUAL_RESEARCH_DATA = {
    "MAIN": {"name": "Main Street Capital", "dividend_yield_pct": 6.8, "payout_ratio_pct": None,
             "quelle": "dividenden.guru", "stand": "2026-08"},
    "LTC": {"name": "LTC Properties", "dividend_yield_pct": 6.5, "payout_ratio_pct": None,
            "quelle": "dividenden.guru", "stand": "2026-08"},
    "MBG.DE": {"name": "Mercedes-Benz Group", "dividend_yield_pct": 6.97, "payout_ratio_pct": 59.40,
               "pe_ratio": 8.2, "market_cap": 45e9, "isin": "DE0007100000",
               "quelle": "finanzen100.de / onvista.de", "stand": "2026-08"},
    "FNTN.DE": {"name": "freenet", "dividend_yield_pct": 8.59, "payout_ratio_pct": 100.29,
                "quelle": "finanzen100.de - ACHTUNG Ausschuett. >100%", "stand": "2026-08"},
    "VOW3.DE": {"name": "Volkswagen Vz.", "dividend_yield_pct": 7.44, "payout_ratio_pct": 31.37,
                "pe_ratio": 4.3, "market_cap": 38e9, "isin": "DE0007664039",
                "quelle": "finanzen100.de / onvista.de", "stand": "2026-08"},
    "VNA.DE": {"name": "Vonovia SE", "dividend_yield_pct": 6.19, "payout_ratio_pct": 51.42,
               "pe_ratio": 5.5, "market_cap": 20e9, "isin": "DE000A1ML7J1",
               "quelle": "finanzen100.de / onvista.de", "stand": "2026-08"},
    "PAH3.DE": {"name": "Porsche Automobil Holding", "dividend_yield_pct": 6.06, "payout_ratio_pct": 22.65,
                "pe_ratio": 3.5, "market_cap": 4.5e9, "isin": "DE000PAH0038",
                "quelle": "finanzen100.de / onvista.de", "stand": "2026-08"},
    "DWNI.DE": {"name": "Deutsche Wohnen", "dividend_yield_pct": 0.2, "payout_ratio_pct": 46.17,
                "pe_ratio": 7.5, "market_cap": 7.35e9, "isin": "DE000A0HN5C6",
                "quelle": "tradingview.com/finanzen.net (Stand Aug 2026) - WICHTIG: Dividende offenbar auf "
                          "0,04 EUR/Aktie gekuerzt (vorher 5,91% Rendite angenommen) - erscheint deshalb nicht "
                          "mehr in der Watchlist, kein Fehler. Grund vermutlich Mehrheitsuebernahme durch "
                          "Vonovia (Beherrschungsvertrag).", "stand": "2026-08"},
    "LEG.DE": {"name": "LEG Immobilien", "dividend_yield_pct": 5.91, "payout_ratio_pct": 34.04,
               "pe_ratio": 92.1, "market_cap": 3.8e9, "isin": "DE000LEG1110",
               "quelle": "finanzen.net (Stand Aug 2026) - KGV bei Immobilienwerten oft wenig aussagekraeftig "
                         "wegen Buchwertanpassungen, hier ungewoehnlich hoch", "stand": "2026-08"},
    "DEQ.DE": {"name": "Deutsche EuroShop", "dividend_yield_pct": 5.64, "payout_ratio_pct": 69.25,
               "pe_ratio": 6.5, "market_cap": 1.48e9, "isin": "DE0007480204",
               "quelle": "finanzen.net / eulerpool.com (Stand Aug 2026)", "stand": "2026-08"},
    "HNR1.DE": {"name": "Hannover Rueck", "dividend_yield_pct": 5.16, "payout_ratio_pct": 56.66,
                "pe_ratio": 11.5, "market_cap": 30e9, "isin": "DE0008402215",
                "quelle": "eulerpool.com / onvista.de (Stand Aug 2026)", "stand": "2026-08"},
    "SAX.DE": {"name": "Stroeer SE", "dividend_yield_pct": 5.12, "payout_ratio_pct": 80.74,
               "quelle": "finanzen100.de", "stand": "2026-08"},
    "SIX2.DE": {"name": "Sixt SE", "dividend_yield_pct": 5.06, "payout_ratio_pct": 52.02,
                "pe_ratio": 11.2, "market_cap": 3.2e9, "isin": "DE0007231326",
                "quelle": "finanzen.net / onvista.de (Stand Aug 2026)", "stand": "2026-08"},
    "5401.T": {"name": "Nippon Steel", "dividend_yield_pct": 10.89, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "9364.T": {"name": "Kamigumi", "dividend_yield_pct": 6.67, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "5938.T": {"name": "Lixil Corp", "dividend_yield_pct": 5.14, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "8395.T": {"name": "Hyakugo Bank", "dividend_yield_pct": 6.53, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "7177.T": {"name": "GMO Financial Holdings", "dividend_yield_pct": 6.14, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "8616.T": {"name": "Tokai Tokyo Financial Holdings", "dividend_yield_pct": 6.04, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "4183.T": {"name": "Mitsui Chemicals", "dividend_yield_pct": 5.11, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "3002.T": {"name": "Gunze", "dividend_yield_pct": 5.10, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "1833.T": {"name": "Okumura Corp", "dividend_yield_pct": 7.02, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "8985.T": {"name": "Japan Hotel REIT", "dividend_yield_pct": 6.83, "payout_ratio_pct": 106.11,
               "quelle": "Parqet Japan - ACHTUNG Ausschuett. >100%", "stand": "2026-08"},
    "8963.T": {"name": "Invincible Investment Corp", "dividend_yield_pct": 6.82, "payout_ratio_pct": 112.42,
               "quelle": "Parqet Japan - ACHTUNG Ausschuett. >100%", "stand": "2026-08"},
    "8960.T": {"name": "United Urban Investment Corp", "dividend_yield_pct": 5.43, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "8972.T": {"name": "Kenedix Office Investment Corp", "dividend_yield_pct": 5.44, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "8150.T": {"name": "Sanshin Denki", "dividend_yield_pct": 6.81, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "7240.T": {"name": "NOK Corp", "dividend_yield_pct": 8.22, "payout_ratio_pct": None,
               "quelle": "Parqet Japan", "stand": "2026-08"},
    "0010.HK": {"name": "Hang Lung Group", "dividend_yield_pct": 6.80, "payout_ratio_pct": None,
                "quelle": "Parqet China", "stand": "2026-08"},
    "NDA-FI.HE": {"name": "Nordea Bank Abp", "dividend_yield_pct": 8.52, "payout_ratio_pct": 64.7,
                  "pe_ratio": 11.9, "market_cap": 58e9, "isin": "FI4000297767",
                  "quelle": "Simply Wall St / onvista.de (Stand Aug 2026)", "stand": "2026-08"},
    # --- Grosse Nutzer-Recherche-Ergaenzung: Werte aus vom Nutzer
    # bereitgestellten Quellen (aktienfinder.net, captrader.com,
    # consorsbank.de u.a.), NICHT von mir selbst gegengeprueft. Bitte als
    # Ausgangspunkt, nicht als geprueften Fakt behandeln. ---
    "BMW.DE": {"name": "BMW", "dividend_yield_pct": 7.5, "payout_ratio_pct": 37.5,
               "pe_ratio": 5.5, "market_cap": 34e9, "isin": "DE0005190003",
               "quelle": "dividenden-kalender.de / aktien.guide (Stand Aug 2026)", "stand": "2026-08"},
    "DTG.DE": {"name": "Daimler Truck Holding", "dividend_yield_pct": 5.1, "payout_ratio_pct": 100.0,
               "pe_ratio": 25.0, "market_cap": 33e9, "isin": "DE000DTR0CK8",
               "quelle": "dividenden-kalender.de/eulerpool.com (Stand Aug 2026) - ACHTUNG: Ausschuett.-Quote "
                         "zwischen Quellen sehr volatil (70-121%), Gewinn ist stark eingebrochen (-32% ggue. "
                         "Vorjahr) - Wert mit Vorsicht behandeln", "stand": "2026-08"},
    "CON.DE": {"name": "Continental", "dividend_yield_pct": 5.0, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "SDF.DE": {"name": "K+S", "dividend_yield_pct": 1.1, "payout_ratio_pct": None,
               "isin": "DE000KSAG888",
               "quelle": "AlleAktien (Stand Aug 2026) - WICHTIG: Rendite auf ~1,1% eingebrochen (Kali-Preise "
                         "stark gefallen), alte Annahme von 5,4% war veraltet - erscheint deshalb nicht mehr "
                         "in der Watchlist, kein Fehler.", "stand": "2026-08"},
    "EVK.DE": {"name": "Evonik Industries", "dividend_yield_pct": 6.5, "payout_ratio_pct": 180.0,
               "pe_ratio": 69.0, "market_cap": 7.9e9, "isin": "DE000EVNK013",
               "quelle": "aktien.guide/eulerpool.com (Stand Aug 2026) - ACHTUNG: Ausschuett.-Quote laut "
                         "mehreren Quellen zwischen 155% und 205% (Gewinn stark eingebrochen), KGV wegen "
                         "Gewinneinbruch kaum aussagekraeftig - echte Dividenden-Fallen-Warnung", "stand": "2026-08"},
    "WCH.DE": {"name": "Wacker Chemie", "dividend_yield_pct": 2.6, "payout_ratio_pct": None,
               "isin": "DE000WCH8881",
               "quelle": "eulerpool.com/traderfox.com (Stand Aug 2026) - WICHTIG: Rendite auf ~2,6-3% "
                         "eingebrochen (Nettoverlust von -821 Mio. EUR im letzten Jahr, Dividende von 12 auf "
                         "3 EUR gekuerzt), alte Annahme von 5,2% war veraltet - erscheint deshalb nicht mehr "
                         "in der Watchlist, kein Fehler. KGV extrem volatil (15 bis 182) wegen Verlustjahr, "
                         "nicht sinnvoll interpretierbar.", "stand": "2026-08"},
    "ET": {"name": "Energy Transfer", "dividend_yield_pct": 7.8, "payout_ratio_pct": None,
           "quelle": "Nutzer-Recherche (captrader.com)", "stand": "2026-08"},
    "KMI": {"name": "Kinder Morgan", "dividend_yield_pct": 5.9, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "OKE": {"name": "Oneok", "dividend_yield_pct": 5.4, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "NLY": {"name": "Annaly Capital Management", "dividend_yield_pct": 13.2, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche - Mortgage-REIT, sehr hohes Risiko", "stand": "2026-08"},
    "OHI": {"name": "Omega Healthcare Investors", "dividend_yield_pct": 6.8, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "CCI": {"name": "Crown Castle", "dividend_yield_pct": 5.5, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "DOW": {"name": "Dow Inc.", "dividend_yield_pct": 5.2, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "LYB": {"name": "LyondellBasell", "dividend_yield_pct": 5.4, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "TFC": {"name": "Truist Financial", "dividend_yield_pct": 5.1, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "KEY": {"name": "KeyCorp", "dividend_yield_pct": 5.5, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "KIM": {"name": "Kimco Realty", "dividend_yield_pct": 5.2, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "TRP": {"name": "TC Energy", "dividend_yield_pct": 4.0, "payout_ratio_pct": 100.0,
            "pe_ratio": 20.0, "market_cap": 66e9, "isin": "CA87807B1076",
            "quelle": "aktien.guide/simplywall.st (Stand Aug 2026) - ACHTUNG: Ausschuett.-Quote laut mehreren "
                      "unabhaengigen Quellen konsistent bei 98-103% - Dividende ist NICHT durch den Gewinn "
                      "gedeckt, echte Dividenden-Fallen-Warnung. Rendite je nach Quelle/Datum stark schwankend "
                      "(3,5-5,7%), 4% als vorsichtige Mitte gewaehlt.", "stand": "2026-08"},
    "BCE": {"name": "BCE Inc.", "dividend_yield_pct": 5.3, "payout_ratio_pct": 33.0,
            "pe_ratio": 10.5, "market_cap": 24e9, "isin": "CA05534B7604",
            "quelle": "simplywall.st/stockanalysis.com (Stand Aug 2026) - WICHTIG: BCE hat im Feb. 2026 die "
                      "Dividende massiv gekuerzt (von ca. 3,99 auf 1,75 USD/Aktie, -56%) - alte Annahme von "
                      "8,7% Rendite war veraltet. Nach der Kuerzung ist die Ausschuettungsquote (~33%) wieder "
                      "gesund gedeckt.", "stand": "2026-08"},
    "TU": {"name": "Telus", "dividend_yield_pct": 9.3, "payout_ratio_pct": 227.0,
           "quelle": "simplywall.st (Stand Aug 2026) - ACHTUNG: Ausschuett.-Quote bei 227% - Dividende ist "
                     "weit mehr als der komplette Gewinn, klare Dividenden-Fallen-Warnung trotz jahrelang "
                     "stabiler/steigender Dividende.", "stand": "2026-08"},
    "BNS": {"name": "Bank of Nova Scotia", "dividend_yield_pct": 4.0, "payout_ratio_pct": 60.0,
            "pe_ratio": 15.0, "market_cap": 90e9, "isin": "CA0641491075",
            "quelle": "gurufocus.com/aktien.guide (Stand Aug 2026) - Rendite auf ~3,6-4,4% gefallen "
                      "(mehrere konvergierende Quellen), alte Annahme von 5,9% war zu hoch - erscheint "
                      "damit nicht mehr in der Watchlist.", "stand": "2026-08"},
    "MFC": {"name": "Manulife Financial", "dividend_yield_pct": 5.1, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "PBA": {"name": "Pembina Pipeline", "dividend_yield_pct": 5.6, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "WCP": {"name": "Whitecap Resources", "dividend_yield_pct": 4.3, "payout_ratio_pct": 74.0,
            "quelle": "Nutzer-Recherche - Rendite liegt aktuell UNTER der 5%-Mindestschwelle, "
                      "erscheint deshalb momentan nicht in der Watchlist (kein Fehler)", "stand": "2026-08"},
    "LGEN.L": {"name": "Legal & General", "dividend_yield_pct": 7.0, "payout_ratio_pct": 150.0,
               "pe_ratio": 45.0, "market_cap": 20e9, "isin": "GB0005603997",
               "quelle": "stockanalysis.com/gurufocus.com (Stand Aug 2026) - ACHTUNG: Ausschuett.-Quote "
                         "zwischen Quellen SEHR stark schwankend (59% bis 272%), bei Versicherern durch "
                         "IFRS-Gewinnvolatilitaet oft wenig verlaesslich - mit Vorsicht behandeln.",
               "stand": "2026-08"},
    "AV.L": {"name": "Aviva", "dividend_yield_pct": 7.2, "payout_ratio_pct": None,
             "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "MNG.L": {"name": "M&G plc", "dividend_yield_pct": 9.1, "payout_ratio_pct": None,
              "quelle": "Nutzer-Recherche - sehr hohe Rendite, genau pruefen", "stand": "2026-08"},
    "HSBA.L": {"name": "HSBC Holdings", "dividend_yield_pct": 4.5, "payout_ratio_pct": 50.0,
               "pe_ratio": 13.0, "market_cap": 170e9, "isin": "GB0005405286",
               "quelle": "stockanalysis.com/dividendpedia.com (Stand Aug 2026) - Rendite je nach Quelle/Zeitpunkt "
                         "zwischen 3,6% und 5,3% schwankend, 4,5% als Mitte gewaehlt.", "stand": "2026-08"},
    "GLEN.L": {"name": "Glencore", "dividend_yield_pct": 1.7, "payout_ratio_pct": None,
               "quelle": "stockinvest.us (Stand Aug 2026) - WICHTIG: Rendite nur noch ~1,7% (war 5,2%), "
                         "erscheint deshalb nicht mehr in der Watchlist, kein Fehler.", "stand": "2026-08"},
    "TW.L": {"name": "Taylor Wimpey", "dividend_yield_pct": 7.1, "payout_ratio_pct": None,
             "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "ISP.MI": {"name": "Intesa Sanpaolo", "dividend_yield_pct": 7.5, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "SRG.MI": {"name": "Snam", "dividend_yield_pct": 5.8, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "TRN.MI": {"name": "Terna", "dividend_yield_pct": 5.1, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "MB.MI": {"name": "Mediobanca", "dividend_yield_pct": 10.5, "payout_ratio_pct": None,
              "quelle": "Nutzer-Recherche - sehr hohe Rendite, genau pruefen", "stand": "2026-08"},
    "UCG.MI": {"name": "UniCredit", "dividend_yield_pct": 5.3, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "ELE.MC": {"name": "Endesa", "dividend_yield_pct": 6.5, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "CS.PA": {"name": "AXA", "dividend_yield_pct": 5.8, "payout_ratio_pct": None,
              "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "EN.PA": {"name": "Bouygues", "dividend_yield_pct": 5.3, "payout_ratio_pct": None,
              "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "SREN.SW": {"name": "Swiss Re", "dividend_yield_pct": 5.9, "payout_ratio_pct": None,
                "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "ZURN.SW": {"name": "Zurich Insurance Group", "dividend_yield_pct": 5.1, "payout_ratio_pct": None,
                "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "PBR": {"name": "Petrobras", "dividend_yield_pct": 14.5, "payout_ratio_pct": None,
            "quelle": "Nutzer-Recherche - sehr hoch, politisch/zyklisch riskant", "stand": "2026-08"},
    "BBAS3.SA": {"name": "Banco do Brasil", "dividend_yield_pct": 9.3, "payout_ratio_pct": None,
                 "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "CPFE3.SA": {"name": "CPFL Energia", "dividend_yield_pct": 8.2, "payout_ratio_pct": None,
                 "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "BBSE3.SA": {"name": "BB Seguridade", "dividend_yield_pct": 7.8, "payout_ratio_pct": None,
                 "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "EC": {"name": "Ecopetrol", "dividend_yield_pct": 11.5, "payout_ratio_pct": None,
           "quelle": "Nutzer-Recherche - sehr hoch, politisch/zyklisch riskant", "stand": "2026-08"},
    "SWED-A.ST": {"name": "Swedbank", "dividend_yield_pct": 8.0, "payout_ratio_pct": None,
                  "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "TRYG.CO": {"name": "Tryg A/S", "dividend_yield_pct": 5.6, "payout_ratio_pct": None,
                "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    "STG.CO": {"name": "Scandinavian Tobacco Group", "dividend_yield_pct": 6.4, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche", "stand": "2026-08"},
    # --- Verifizierungsrunde: Ticker gegengeprueft (ISIN/WKN bestaetigt),
    # Renditen aus Nutzer-Recherche uebernommen (nicht von mir selbst
    # gegengeprueft, ausser wo explizit vermerkt). ---
    "BIJ.DE": {"name": "Bijou Brigitte", "dividend_yield_pct": 12.7, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche - sehr hohe Rendite, genau pruefen (ISIN DE0005229504 bestaetigt)",
               "stand": "2026-08"},
    "HABA.DE": {"name": "Hamborner REIT", "dividend_yield_pct": 9.0, "payout_ratio_pct": None,
                "quelle": "Nutzer-Recherche (ISIN DE000A3H2333 bestaetigt)", "stand": "2026-08"},
    "HOT.DE": {"name": "Hochtief", "dividend_yield_pct": 7.0, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche (ISIN DE0006070006 bestaetigt)", "stand": "2026-08"},
    "PWO.DE": {"name": "Progress-Werk Oberkirch", "dividend_yield_pct": 5.87, "payout_ratio_pct": None,
               "quelle": "boerse.de - live gegengeprueft, ISIN DE0006968001 bestaetigt", "stand": "2026-08"},
    "MUX.DE": {"name": "Mutares", "dividend_yield_pct": 7.5, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche (ISIN DE000A2NB650 bestaetigt)", "stand": "2026-08"},
    "JST.DE": {"name": "Jost Werke", "dividend_yield_pct": 7.3, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche (ISIN DE000JST4000 bestaetigt)", "stand": "2026-08"},
    "030000.KS": {"name": "Cheil Worldwide", "dividend_yield_pct": 6.3, "payout_ratio_pct": None,
                  "quelle": "Nutzer-Recherche (KRX-Ticker 030000 bestaetigt)", "stand": "2026-08"},
    "035250.KS": {"name": "Kangwon Land", "dividend_yield_pct": 6.2, "payout_ratio_pct": None,
                  "quelle": "Nutzer-Recherche (KRX-Ticker 035250 bestaetigt)", "stand": "2026-08"},
    "004990.KS": {"name": "LOTTE Corp.", "dividend_yield_pct": 5.2, "payout_ratio_pct": None,
                  "quelle": "Nutzer-Recherche (KRX-Ticker 004990 bestaetigt)", "stand": "2026-08"},
    # --- Zweite Verifizierungsrunde: Renditen SELBST live gegengeprueft
    # (nicht nur Nutzer-Angabe uebernommen) ---
    "B7E.DE": {"name": "Blue Cap", "dividend_yield_pct": 10.0, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche, Ticker/ISIN selbst bestaetigt (DE000A0JM2M1) - "
                         "Rendite selbst nicht direkt verifiziert, mit Vorsicht behandeln", "stand": "2026-08"},
    "WSU.DE": {"name": "WashTec", "dividend_yield_pct": 5.5, "payout_ratio_pct": 107.0,
               "quelle": "Live gegengeprueft (boerse.de/parqet/finanzen.net/aktien.guide): "
                         "5,25-6,58% je nach Quelle/Datum, Ausschuettungsquote ~107-109% (!) "
                         "- ACHTUNG Warnsignal trotz moderater Rendite", "stand": "2026-08"},
    "HAW.DE": {"name": "Hawesko Holding", "dividend_yield_pct": 5.5, "payout_ratio_pct": None,
               "quelle": "Live gegengeprueft (divvydiary/finanzen.net/wallstreet-online): "
                         "4,98-6,40% je nach Quelle/Datum", "stand": "2026-08"},
    "NWX.DE": {"name": "Nordwest Handel", "dividend_yield_pct": 6.0, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche, ISIN selbst bestaetigt (DE0006775505)", "stand": "2026-08"},
    "LEI.DE": {"name": "Leifheit", "dividend_yield_pct": None, "payout_ratio_pct": None,
               "quelle": "Nutzer-Recherche, Ticker bestaetigt - keine eigene Renditeangabe "
                         "gefunden, bitte selbst pruefen", "stand": "2026-08"},
    "M12.DE": {"name": "M1 Kliniken", "dividend_yield_pct": 6.5, "payout_ratio_pct": 42.06,
               "quelle": "Live gegengeprueft (simplywall.st/investing.com): 6,5-6,6% Rendite, "
                         "42% Ausschuettungsquote - solide Kombination", "stand": "2026-08"},
    "EDL.DE": {"name": "Edel SE", "dividend_yield_pct": 5.45, "payout_ratio_pct": None,
               "quelle": "Live gegengeprueft (boerse.de), ISIN DE0005649503 bestaetigt", "stand": "2026-08"},
    "BCH": {"name": "Banco de Chile", "dividend_yield_pct": 5.8, "payout_ratio_pct": None,
            "quelle": "Live gegengeprueft (investing.com/stockanalysis): 5,6-5,99% Rendite, "
                      "NYSE-ADR-Ticker BCH", "stand": "2026-08"},
    # --- Explizit AUSGESCHLOSSEN, da echte Zahlen die Nutzer-Angaben
    # widerlegen (unter unserer 5%-Schwelle): ---
    # Telefonica Brasil (VIV): echte Rendite nur 1,24% (nicht 5-6% wie
    # angegeben) - NICHT aufgenommen.
    # SQM (Sociedad Quimica y Minera): echte Rendite nur 1,42% (nicht 5,4%
    # wie angegeben) - NICHT aufgenommen.
    # --- Widerspruechliche Faelle: unterschiedliche Quellen nennen sehr
    # unterschiedliche Renditen fuer dieselbe Aktie. Hier NICHT einfach
    # eine Zahl gewaehlt, sondern die niedrigere/vorsichtigere Angabe
    # genommen UND der Widerspruch explizit vermerkt - bitte selbst
    # gegenchecken, bevor du dich darauf verlaesst.
    "IBE.MC": {"name": "Iberdrola", "dividend_yield_pct": 1.07, "payout_ratio_pct": 20.29,
               "quelle": "WIDERSPRUCH: investing.com/stockanalysis nennen 0,02%-2,56%, "
                         "Nutzer-Quelle nennt 4,8%. Niedrigere, besser belegte Zahl gewaehlt - "
                         "damit klar unter 5%, wuerde eigentlich nicht qualifizieren.", "stand": "2026-08"},
    "ENEL.MI": {"name": "Enel", "dividend_yield_pct": 2.29, "payout_ratio_pct": 150.18,
                "quelle": "WIDERSPRUCH: Quellen nennen 2,29%-6,1%, Ausschuett. teils 150% "
                          "(!) - klares Warnsignal falls das stimmt. Niedrigere Zahl gewaehlt.",
                "stand": "2026-08"},
    "TTE.PA": {"name": "TotalEnergies", "dividend_yield_pct": 0.84, "payout_ratio_pct": 70.14,
               "quelle": "WIDERSPRUCH: Quellen nennen 0,84%-7,48% je nach Zeitpunkt/Basis, "
                         "Nutzer-Quelle nennt 6,4%. Niedrigere, aktuellere Zahl gewaehlt - "
                         "damit klar unter 5%.", "stand": "2026-08"},
}


def fetch_fundamentals_manual_research(ticker):
    """Letzter Fallback: manuell recherchierte Werte aus echten Artikeln
    (siehe MANUAL_RESEARCH_DATA). Kein Live-Kurs verfuegbar - price bleibt
    None, das UI zeigt dann 'Ort' statt Zahl bei Kurs/Kursentwicklung/ISIN,
    aber Rendite und Risiko-Bewertung funktionieren normal. market_cap,
    pe_ratio und isin sind OPTIONAL im Eintrag - viele manuelle Eintraege
    haben urspruenglich nur Rendite/Ausschuettungsquote recherchiert; wo
    zusaetzlich KGV/Marktkapitalisierung/ISIN bekannt sind, werden sie mit
    ausgegeben (kein API-Call, reine Handrecherche)."""
    entry = MANUAL_RESEARCH_DATA.get(ticker)
    if not entry:
        return None
    return {
        "ticker": ticker,
        "name": entry["name"],
        "price": None,
        "market_cap": entry.get("market_cap"),
        "pe_ratio": entry.get("pe_ratio"),
        "dividend_yield_pct": entry["dividend_yield_pct"],
        "payout_ratio_pct": entry["payout_ratio_pct"],
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "manuell",
        "manual_source_note": f"{entry['quelle']} (Stand {entry['stand']})",
        "manual_isin": entry.get("isin"),
    }


def get_ticker_region(ticker):
    """Bestimmt die Region anhand der Ticker-ENDUNG (nicht des Land-Feldes!),
    da das die tatsaechliche Boerse widerspiegelt, auf der der Ticker
    gehandelt wird - wichtig z.B. bei US-gelisteten Aktien auslaendischer
    Firmen (Petrobras PBR, Ecopetrol EC, Banco de Chile BCH sind alle
    normale US-Ticker ohne Endung, obwohl die Firmen aus Suedamerika sind)."""
    if any(ticker.endswith(s) for s in (".DE", ".PA", ".MC", ".MI", ".L", ".SW", ".CO", ".ST")):
        return "europa"
    if any(ticker.endswith(s) for s in (".T", ".HK", ".SI", ".KS")):
        return "asien"
    if ticker.endswith(".SA"):
        return "suedamerika"
    return "nordamerika"


ENRICH_TOP_N = 30  # Nur die Top-30-Watchlist-Kandidaten (nach Risiko/Rendite
# sortiert) + alle Depot-Positionen bekommen ISIN/News/Analysten-Grades/
# Zahltermin - der Rest bekommt nur die Basisdaten (Kurs, Rendite,
# Ausschuettungsquote). Grund: bei 187+ Tickern reicht das FMP-Tages-
# kontingent (300 Anfragen/Tag laut FMP-Dashboard) nicht annaehernd, um das fuer ALLE zu holen -
# das fuehrte bisher dazu, dass praktisch JEDE ISIN-Anfrage mit HTTP 429
# ("Limit Reach") scheiterte. Hochsetzen, sobald ein zweiter FMP-Account/
# Key im Einsatz ist.

AUTO_DISCOVER_MAX_NEW = 40  # neue, bisher unbekannte Ticker pro Lauf, die
# komplett durch die Anreicherungs-Pipeline laufen (Kontingent!). Hochgesetzt
# von 12 auf 40, um den Kandidaten-Pool schneller wachsen zu lassen - jeder
# neue Ticker kostet bis zu ~5 FMP-Calls, falls er die Mindestrendite
# erreicht. Im Log ("dividendenatlas.log") nach HTTP-429-Fehlern schauen,
# falls das FMP-Kontingent (300/Tag) dadurch zu oft ausgeht - dann eher
# wieder runtersetzen oder einen zweiten, separaten FMP-Key/Account nutzen.


def fetch_screener_candidates(api_key, limit=1000):
    """Best-effort: EIN einziger FMP-Call an den (stable) Screener-Endpunkt,
    um automatisch neue Dividendenkandidaten zu FINDEN statt sie von Hand
    in CANDIDATE_UNIVERSE einzutragen - der eigentliche 'Auto-Scanner'.
    KEIN neuer API-Key noetig, laeuft ueber das bestehende FMP-Kontingent.

    UNSICHER: FMP hat den Screener-Endpunkt fuer NEU REGISTRIERTE Nutzer
    (nach 31.08.2025 - wie hier) teils auf bezahlte Plaene beschraenkt.
    Ob es bei diesem Account funktioniert, zeigt erst der naechste echte
    Lauf. Deshalb komplett best-effort: liefert bei Fehler/leerer Antwort
    einfach [] zurueck, der Rest des Tools laeuft unveraendert weiter -
    bricht also nichts, falls der Screener nicht verfuegbar ist.

    Deckt zuverlaessig nur US-/kanadische Grossboersen ab (keine Europa-/
    Asien-Abdeckung) - ergaenzt eure Handrecherche, ersetzt sie nicht."""
    if not api_key:
        return []
    data = fmp_get("company-screener", api_key, params={
        "dividendMoreThan": 0.01,
        "isActivelyTrading": "true",
        "isEtf": "false",
        "isFund": "false",
        "limit": limit,
    }, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list):
        log("  Auto-Scanner: Screener-Endpunkt lieferte nichts (evtl. im "
            "aktuellen FMP-Plan nicht enthalten) - wird uebersprungen, "
            "Rest des Tools laeuft normal weiter.")
        return []
    log(f"  Auto-Scanner: {len(data)} Aktien vom Screener erhalten.")
    return data


def load_discovered_candidates():
    return load_json(DISCOVERED_PATH, {})


EODHD_URL = "https://eodhd.com/api"
_eodhd_calls_used = 0
EODHD_DAILY_LIMIT = 18  # absichtlich unter dem offiziellen 20er-Limit, als Puffer


def eodhd_quota_available():
    return _eodhd_calls_used < EODHD_DAILY_LIMIT


EODHD_EXCHANGE_MAP = {
    "DE": "XETRA", "L": "LSE", "MI": "MI", "MC": "MC", "HE": "HE", "PA": "PA",
    "SW": "SW", "T": "TSE", "HK": "HK", "SA": "SA", "TO": "TO",
}


def to_eodhd_symbol(ticker):
    """Wandelt unser Tickerformat (z.B. 'VOW3.DE') in EODHDs Format
    ('VOW3.XETRA') um. Boersen-Codes sind teils UNSICHER (v.a. Japan 'TSE'
    und Toronto 'TO') - nicht aus offizieller EODHD-Doku pro Ticker
    bestaetigt, nur aus allgemeinen Beispielen abgeleitet. Bei Fehlschlag
    zeigt das Diagnose-Logging in fetch_fundamentals_eodhd, woran es liegt."""
    if "." in ticker:
        base, suffix = ticker.rsplit(".", 1)
        return f"{base}.{EODHD_EXCHANGE_MAP.get(suffix, suffix)}"
    return f"{ticker}.US"


def fetch_fundamentals_eodhd(ticker, api_key):
    """Zusaetzliche Quelle: EODHD. Deckt laut eigener Doku nur zuverlaessig
    US-Boersen im Gratisplan ab ('limited fundamentals for US exchanges') -
    bei europaeischen/asiatischen Tickern also UNSICHER, ob es ueberhaupt
    etwas liefert. Trotzdem versucht, weil im Erfolgsfall auch ISIN direkt
    mitkommt (General.ISIN) - best-effort, mit Diagnose-Logging."""
    global _eodhd_calls_used
    if not api_key or not eodhd_quota_available():
        return None
    eodhd_symbol = to_eodhd_symbol(ticker)
    _eodhd_calls_used += 1
    data = api_get(f"fundamentals/{eodhd_symbol}", api_key, params={"fmt": "json"},
                   base_url=EODHD_URL, key_param="api_token", source_label="EODHD")
    if not data or not isinstance(data, dict):
        log(f"  EODHD lieferte keine verwertbare Antwort fuer {eodhd_symbol}")
        return None
    general = data.get("General", {}) or {}
    highlights = data.get("Highlights", {}) or {}
    valuation = data.get("Valuation", {}) or {}
    try:
        dividend_yield_pct = float(highlights.get("DividendYield") or 0) * 100
    except (TypeError, ValueError):
        dividend_yield_pct = 0
    if not dividend_yield_pct:
        log(f"  EODHD lieferte keine Dividendenrendite fuer {eodhd_symbol} - werte als Fehlschlag")
        return None
    return {
        "ticker": ticker,
        "name": general.get("Name"),
        "price": None,
        "market_cap": highlights.get("MarketCapitalization"),
        "pe_ratio": highlights.get("PERatio") or valuation.get("TrailingPE"),
        "dividend_yield_pct": dividend_yield_pct,
        "payout_ratio_pct": None,  # nicht direkt in Highlights enthalten
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "eodhd",
        "manual_isin": general.get("ISIN"),
    }


def fetch_fundamentals(ticker, fmp_key, twelve_data_key, finnhub_key,
                        alpha_vantage_key=None, marketstack_key=None, leeway_key=None,
                        brapi_key=None, eodhd_key=None):
    """Regionsbasiertes Routing statt einer Universal-Kette fuer alle
    Ticker: jede Region bekommt die Quellen-Reihenfolge, die dafuer
    tatsaechlich Sinn ergibt - Quellen, die fuer eine Region bereits
    bestaetigt IMMER scheitern (z.B. Finnhub ausserhalb USA/Kanada, 0%
    Erfolgsquote in allen bisherigen Laeufen), werden dort gar nicht erst
    versucht. Spart Kontingent und Laufzeit."""
    region = get_ticker_region(ticker)

    if region == "nordamerika":
        chain = [
            ("FMP", lambda: fetch_fundamentals_fmp(ticker, fmp_key)),
            ("Finnhub", lambda: fetch_fundamentals_finnhub(ticker, finnhub_key)),
            ("Alpha Vantage", lambda: fetch_fundamentals_alphavantage(ticker, alpha_vantage_key)),
            ("Marketstack", lambda: fetch_fundamentals_marketstack(ticker, marketstack_key)),
        ]
    elif region == "europa":
        chain = [
            ("Leeway.tech", lambda: fetch_fundamentals_leeway(ticker, leeway_key)),
            ("FMP", lambda: fetch_fundamentals_fmp(ticker, fmp_key)),
            ("EODHD", lambda: fetch_fundamentals_eodhd(ticker, eodhd_key)),
            ("Twelve Data", lambda: fetch_fundamentals_twelvedata(ticker, twelve_data_key)),
            ("Alpha Vantage", lambda: fetch_fundamentals_alphavantage(ticker, alpha_vantage_key)),
        ]
    elif region == "asien":
        chain = [
            ("FMP", lambda: fetch_fundamentals_fmp(ticker, fmp_key)),
            ("EODHD", lambda: fetch_fundamentals_eodhd(ticker, eodhd_key)),
            ("Twelve Data", lambda: fetch_fundamentals_twelvedata(ticker, twelve_data_key)),
            ("Alpha Vantage", lambda: fetch_fundamentals_alphavantage(ticker, alpha_vantage_key)),
        ]
    else:  # suedamerika
        chain = [
            ("brapi.dev", lambda: fetch_fundamentals_brapi(ticker, brapi_key)),
            ("FMP", lambda: fetch_fundamentals_fmp(ticker, fmp_key)),
            ("Alpha Vantage", lambda: fetch_fundamentals_alphavantage(ticker, alpha_vantage_key)),
        ]

    for source_name, fetch_fn in chain:
        result = fetch_fn()
        if result is not None:
            log(f"    -> ueber {source_name} erhalten (Region: {region})")
            return result

    result = fetch_fundamentals_manual_research(ticker)
    if result is not None:
        log(f"    -> ueber manuelle Recherche-Datenbank erhalten (kein Live-Kurs)")
    return result


def fetch_recent_grades(ticker, api_key, limit=3):
    """Best-effort: die letzten bis zu 3 Analysten-Einschaetzungen (nicht nur
    die neueste). WICHTIG: kein zusaetzlicher API-Call noetig - der FMP-
    Endpunkt 'grades' liefert ohnehin eine Liste zurueck, vorher wurde nur
    der erste Eintrag verwendet. Nicht garantiert im kostenlosen Kontingent
    enthalten - gibt bei Fehler einfach [] zurueck, Rest des Tools
    funktioniert dann trotzdem weiter."""
    data = fmp_get("grades", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "date": g.get("date"),
            "action": g.get("action"),  # z.B. "upgrade" / "downgrade" / "maintain"
            "firm": g.get("gradingCompany"),
            "new_grade": g.get("newGrade"),
            "previous_grade": g.get("previousGrade"),
        }
        for g in data[:limit]
    ]


def apply_analyst_grades_to_risk(evaluation, grades):
    """Stuft das Risiko eine Stufe hoeher, wenn unter den letzten bis zu 3
    Analysten-Einschaetzungen mehr Herab- als Hochstufungen sind - analog
    zur News-Sentiment-Logik oben. Einzelne 'maintain'/'reiterate'-Eintraege
    zaehlen weder fuer noch gegen die Aktie."""
    grades = grades or []
    downgrades = sum(1 for g in grades if g.get("action") == "downgrade")
    upgrades = sum(1 for g in grades if g.get("action") == "upgrade")
    evaluation["analyst_downgrade_warning"] = downgrades > 0 and downgrades > upgrades
    if evaluation["analyst_downgrade_warning"]:
        evaluation["risk"] = RISK_STEP_UP[evaluation["risk"]]
    return evaluation


FINANCIALDATA_URL = "https://financialdata.net/api/v1"


def fetch_next_payout_financialdata(ticker, api_key):
    """Zusaetzliche, OPTIONALE Zahltermin-Quelle ueber financialdata.net -
    braucht einen NEUEN, separaten Key (kostenlos registrierbar auf
    financialdata.net). Laut Anbieter-Dokumentation deckt sie explizit
    auch internationale Werte ab, nicht nur US - genau die Luecke, die
    FMPs 'US-fokussierter' Gratis-Plan bei Auslandswerten wie Whitecap hat.
    UNSICHER: wir haben den genauen Auth-Mechanismus und das exakte
    Freikontingent nicht selbst verifiziert (kein eigener Test-Account) -
    deshalb komplett best-effort mit Diagnose-Logging bei Fehlschlag, damit
    ihr beim ersten echten Lauf seht, ob Key/Format passen."""
    if not api_key:
        return None
    data = api_get("dividends", api_key, params={"identifier": ticker},
                    base_url=FINANCIALDATA_URL, key_param="key", source_label="financialdata.net")
    if not data or not isinstance(data, list) or not data:
        return None
    latest = data[0]
    if latest.get("payment_date"):
        return latest["payment_date"]
    # Fallback, falls nur das Ex-Datum geliefert wird: grobe Schaetzung wie
    # beim FMP-Pendant (typischer Verzug Ex-Datum -> Auszahlung ~3 Wochen).
    ex_date = latest.get("ex_date")
    if ex_date:
        try:
            d = datetime.strptime(ex_date, "%Y-%m-%d")
            return (d + timedelta(days=21)).strftime("%Y-%m-%d") + " (geschaetzt)"
        except Exception:
            return None
    return None


def fetch_next_payout(ticker, api_key):
    """Best-effort: naechster erwarteter Ausschuettungstermin. Faellt auf
    None zurueck, falls nicht verfuegbar - dann traegt man's manuell in
    my_depot.json unter next_payout_manual ein."""
    data = fmp_get("dividends", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list) or not data:
        return None
    # Grobe Schaetzung: letztes bekanntes Zahldatum + ca. 3 Monate,
    # da die meisten hier gelisteten Aktien quartalsweise auszahlen.
    try:
        last_date = datetime.strptime(data[0]["paymentDate"], "%Y-%m-%d")
        return (last_date + timedelta(days=91)).strftime("%Y-%m-%d") + " (geschaetzt)"
    except Exception:
        return None


def fetch_isin(ticker, api_key):
    """Best-effort: ISIN (internationale Wertpapierkennnummer) ueber
    FMP's Profil-Endpunkt - damit kannst du die Aktie bei JEDEM Broker
    suchen, nicht nur mit dem US-Ticker. Faellt bei Fehler auf None
    zurueck, das UI zeigt dann weiterhin nur den Ticker."""
    if not api_key:
        return None
    data = fmp_get("profile", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list) or not data:
        return None
    return data[0].get("isin")


def fetch_news_sentiment(ticker, api_key):
    """Holt aktuelle News-Schlagzeilen (mit echtem Link) ueber FMP und
    bewertet sie mit VADER - einem etablierten, kostenlosen lexikon-
    basierten Sentiment-Verfahren (auch in der Finanzbranche genutzt),
    NICHT nur Keyword-Abgleich. Gibt pro Artikel einen echten Score
    zwischen -1 (sehr negativ) und +1 (sehr positiv) zurueck."""
    if not api_key:
        return None
    data = fmp_get("news/stock", api_key, params={"symbols": ticker, "limit": 5}, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list) or not data:
        return None
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
    except ImportError:
        log("  vaderSentiment nicht installiert - News werden ohne Sentiment-Wert angezeigt")
        analyzer = None
    articles, scores = [], []
    for item in data[:5]:
        title = item.get("title", "")
        url = item.get("url") or item.get("link")
        published = item.get("publishedDate") or item.get("date")
        sentiment_score = None
        if analyzer and title:
            sentiment_score = analyzer.polarity_scores(title)["compound"]
            scores.append(sentiment_score)
        articles.append({"title": title, "url": url, "published": published, "sentiment_score": sentiment_score})
    return {
        "articles": articles,
        "avg_sentiment": (sum(scores) / len(scores)) if scores else None,
    }


def fetch_news_sentiment_finnhub(ticker, api_key):
    """Fallback-Newsquelle ueber Finnhub - KEIN neuer Key noetig, ihr habt
    ihn schon fuer die Fundamentaldaten-Kette. Genau wie dort zuverlaessig
    nur fuer USA/Kanada (Finnhub blockt andere Boersen mit 403) - wird nur
    versucht, wenn FMPs News-Endpunkt nichts liefert (spart Kontingent bei
    Aktien, wo FMP sowieso funktioniert)."""
    if not api_key:
        return None
    heute = datetime.now().strftime("%Y-%m-%d")
    vor_30_tagen = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    data = api_get("company-news", api_key, params={"symbol": ticker, "from": vor_30_tagen, "to": heute},
                    base_url=FINNHUB_URL, key_param="token", source_label="Finnhub (News)")
    if not data or not isinstance(data, list) or not data:
        return None
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        analyzer = SentimentIntensityAnalyzer()
    except ImportError:
        analyzer = None
    articles, scores = [], []
    for item in data[:5]:
        title = item.get("headline", "")
        url = item.get("url")
        published = (datetime.fromtimestamp(item["datetime"]).strftime("%Y-%m-%d")
                     if item.get("datetime") else None)
        sentiment_score = None
        if analyzer and title:
            sentiment_score = analyzer.polarity_scores(title)["compound"]
            scores.append(sentiment_score)
        articles.append({"title": title, "url": url, "published": published, "sentiment_score": sentiment_score})
    return {
        "articles": articles,
        "avg_sentiment": (sum(scores) / len(scores)) if scores else None,
    }


def fetch_recommendation_trend_finnhub(ticker, api_key):
    """Fallback-'Analysteneinschaetzung' ueber Finnhub - KEIN neuer Key
    noetig. WICHTIG, anderes Format als FMPs Grades: kein einzelnes Up-/
    Downgrade EINER Firma, sondern ein monatlicher KONSENS (Anzahl Kauf-/
    Halten-/Verkaufen-Einstufungen ueber alle erfassten Analysten). Wird
    auf unser bestehendes Grades-Schema abgebildet (action wird aus dem
    Konsens abgeleitet: mehr Verkaufen als Kaufen = 'downgrade' usw.),
    damit Steckbrief und Risiko-Logik unveraendert funktionieren. 'firm'
    zeigt entsprechend 'Analysten-Konsens (Finnhub)' statt eines
    Institutsnamens - so ist im UI klar erkennbar, dass es sich um eine
    andere Datenart handelt. Nur USA/Kanada (siehe oben)."""
    if not api_key:
        return []
    data = api_get("stock/recommendation", api_key, params={"symbol": ticker},
                    base_url=FINNHUB_URL, key_param="token", source_label="Finnhub (Empfehlungen)")
    if not data or not isinstance(data, list):
        return []
    grades = []
    for period in data[:3]:
        buy = (period.get("strongBuy") or 0) + (period.get("buy") or 0)
        sell = (period.get("strongSell") or 0) + (period.get("sell") or 0)
        hold = period.get("hold") or 0
        if sell > buy:
            action = "downgrade"
        elif buy > sell:
            action = "upgrade"
        else:
            action = "maintain"
        grades.append({
            "date": period.get("period"),
            "action": action,
            "firm": "Analysten-Konsens (Finnhub)",
            "new_grade": f"{buy} Kaufen / {hold} Halten / {sell} Verkaufen",
            "previous_grade": None,
        })
    return grades


NEWS_SENTIMENT_WARNING_THRESHOLD = -0.3  # VADER-Skala -1 (sehr negativ) bis +1 (sehr positiv)
RISK_STEP_UP = {"niedrig": "mittel", "mittel": "hoch", "hoch": "hoch"}


def apply_news_sentiment_to_risk(evaluation, news):
    """Stuft das Risiko eine Stufe hoeher, wenn die aktuellen News im
    Schnitt deutlich negativ sind (VADER-Score < -0.3) - z.B. bei
    Gewinnwarnungen, Kuerzungsankuendigungen etc. Wird NACH dem News-
    Abruf aufgerufen, da die urspruengliche Risiko-Einstufung (Ausschuett./
    Verschuldung) vor dem News-Abruf passiert."""
    avg = (news or {}).get("avg_sentiment")
    evaluation["sentiment_warning"] = avg is not None and avg < NEWS_SENTIMENT_WARNING_THRESHOLD
    if evaluation["sentiment_warning"]:
        evaluation["risk"] = RISK_STEP_UP[evaluation["risk"]]
    return evaluation


def fetch_price_history_twelvedata(ticker, api_key):
    """Ergaenzende Kurs-/Kursverlauf-Quelle ueber Twelve Data's /time_series
    Endpunkt - NICHT derselbe wie /statistics (der ist bezahlpflichtig
    gesperrt), sondern ein separates Feature mit dem groesseren 800er-
    Tageskontingent. Nutzt denselben Key, den wir schon fuer die Fallback-
    Kette haben - kein neuer Account noetig, und konkurriert NICHT mit
    Leeways separatem 50er-Kontingent."""
    if not api_key:
        return None
    td_symbol = to_twelve_data_symbol(ticker)
    throttle_twelve_data()
    data = api_get("time_series", api_key,
                    params={"symbol": td_symbol, "interval": "1day", "outputsize": "260"},
                    base_url=TWELVE_DATA_URL, source_label="Twelve Data (Kursverlauf)")
    values = (data or {}).get("values")
    if not values or not isinstance(values, list):
        log(f"  Twelve Data (Kursverlauf) lieferte keine verwertbaren Daten fuer {td_symbol}")
        return None
    try:
        # Twelve Data liefert neueste zuerst
        closes = [float(v["close"]) for v in values if v.get("close")]
    except (KeyError, ValueError, TypeError):
        return None
    if not closes:
        return None
    current_price = closes[0]

    def pct_change(handelstage):
        idx = min(handelstage, len(closes) - 1)
        old = closes[idx]
        return round((current_price - old) / old * 100, 2) if old else None

    # chart_series fuer den Steckbrief-Chart: Twelve Data liefert neueste
    # zuerst, fuer den Chart (links=alt, rechts=neu) drehen wir das um.
    chart_series = downsample_series([
        (v["datetime"][:10], float(v["close"])) for v in reversed(values) if v.get("close")
    ])

    return {
        "price": current_price,
        "perf_1m": pct_change(21),
        "perf_3m": pct_change(63),
        "perf_1y": pct_change(min(252, len(closes) - 1)),
        "chart_series": chart_series,
    }


def _yahoo_raw(field_dict, key):
    """Yahoo verpackt Zahlenwerte oft als {'raw': 12.3, 'fmt': '12,30'} statt
    als einfache Zahl - dieser Helfer zieht in beiden Faellen den Rohwert."""
    v = (field_dict or {}).get(key)
    if isinstance(v, dict):
        return v.get("raw")
    return v


def fetch_fundamentals_yahoo(ticker):
    """Kostenlose, KEIN API-Key noetige Fundamentaldaten-Quelle ueber Yahoo
    Finance's quoteSummary-Endpunkt (anderer Endpunkt als der Chart/Kurs-
    Endpunkt oben, aber derselbe inoffizielle Dienst). Liefert KGV,
    Marktkapitalisierung, Ausschuettungsquote und Rendite - fuer PRAKTISCH
    JEDE Boerse weltweit, im Gegensatz zu EODHD/Finnhub (dort laut eigener
    Doku nur verlaesslich fuer US-Boersen). Deshalb als universeller
    Luecken-Fueller fuer FEHLENDE Kennzahlen gedacht, unabhaengig davon,
    woher die Grunddaten (Rendite/Kurs) kamen.

    UNSICHER: Dieser spezielle Endpunkt (quoteSummary) hat laut mehreren
    Quellen seit 2024 teils eine Cookie/"Crumb"-Pflicht - ob das hier
    zuschlaegt (der Chart-Endpunkt oben ist davon bisher NICHT betroffen),
    zeigt erst der echte Lauf. Komplett best-effort mit Diagnose-Logging."""
    url = f"{YAHOO_QUOTESUMMARY_URL}/{ticker}?modules=summaryDetail,defaultKeyStatistics,price"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read()[:150]
        log(f"  Yahoo-Kennzahlen-Fehler bei {ticker}: HTTP {e.code}, Antwort-Anfang: {body}")
        return None
    except Exception as e:
        log(f"  Yahoo-Kennzahlen-Fehler bei {ticker}: {e}")
        return None

    qs = (data or {}).get("quoteSummary") or {}
    result_list = qs.get("result")
    error = qs.get("error")
    if error or not result_list:
        log(f"  Yahoo-Kennzahlen lieferten nichts Verwertbares fuer {ticker} "
            f"(HTTP {status}): {error or 'leeres Ergebnis'}")
        return None

    r = result_list[0]
    summary = r.get("summaryDetail") or {}
    keystats = r.get("defaultKeyStatistics") or {}
    price_mod = r.get("price") or {}

    dividend_yield = _yahoo_raw(summary, "dividendYield")
    payout_ratio = _yahoo_raw(summary, "payoutRatio")

    return {
        "ticker": ticker,
        "name": price_mod.get("longName") or price_mod.get("shortName"),
        "price": _yahoo_raw(price_mod, "regularMarketPrice"),
        "market_cap": _yahoo_raw(summary, "marketCap") or _yahoo_raw(price_mod, "marketCap"),
        "pe_ratio": _yahoo_raw(summary, "trailingPE") or _yahoo_raw(keystats, "forwardPE"),
        "dividend_yield_pct": (dividend_yield * 100) if dividend_yield else None,
        "payout_ratio_pct": (payout_ratio * 100) if payout_ratio else None,
        "debt_to_equity": None,
        "current_ratio": None,
        "data_source": "yahoo",
    }


def fetch_price_history_yahoo(ticker):
    """Kostenlose, KEIN API-Key noetige Kursquelle ueber Yahoo Finance's
    Chart-Endpunkt (dieselben inoffiziellen Endpunkte, auf denen auch die
    bekannte 'yfinance'-Bibliothek aufbaut). WICHTIG - zwei Dinge bewusst
    so gewaehlt, um Kontingent/Zeit zu sparen:
      1. EIN EINZIGER HTTP-Call liefert Kurs UND 1-Jahres-Historie in
         einer Antwort (anders als Twelve Data, das dafuer 2 Calls
         braucht: throttle + eigenes Tageskontingent).
      2. Ticker-Format ist praktisch identisch zu unserem eigenen
         CANDIDATE_UNIVERSE (z.B. 'BAS.DE', 'NESN.SW', '5401.T',
         '0857.HK', 'PETR4.SA') - KEINE Symbol-Umwandlung noetig, anders
         als bei Twelve Data/Leeway.
    UNSICHER: Yahoo aendert diese inoffiziellen Endpunkte immer wieder
    ohne Ankuendigung (z.B. Cookie/"Crumb"-Pflicht fuer manche Anfragen
    seit 2024) und koennte - wie Stooq - von GitHub-Actions-Cloud-IPs
    haerter gedrosselt werden als von einer privaten IP. Deshalb mit
    Diagnose-Logging (roher HTTP-Status + erste Zeichen der Antwort),
    damit man ein Scheitern einordnen kann statt nur "Fehler" zu sehen."""
    url = f"{YAHOO_CHART_URL}/{ticker}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read()[:150]
        log(f"  Yahoo-Fehler bei {ticker}: HTTP {e.code}, Antwort-Anfang: {body}")
        return None
    except Exception as e:
        log(f"  Yahoo-Fehler bei {ticker}: {e}")
        return None

    result_list = ((data or {}).get("chart") or {}).get("result")
    chart_error = ((data or {}).get("chart") or {}).get("error")
    if chart_error:
        log(f"  Yahoo lieferte einen Fehler fuer {ticker} (HTTP {status}): {chart_error}")
        return None
    if not result_list:
        log(f"  Yahoo lieferte keine verwertbaren Kursdaten fuer {ticker} "
            f"(HTTP {status}), Antwort-Anfang: {raw[:150]}")
        return None

    result = result_list[0]
    try:
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    # Yahoo liefert None fuer Tage ohne Handel (Feiertage etc.) - raus damit,
    # Timestamp und Kurs bleiben dabei gepaart (fuer den Chart brauchen wir
    # beides, nicht nur die Kurse).
    pairs = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
    if not pairs:
        return None
    closes = [c for _, c in pairs]

    current_price = closes[-1]

    def pct_change(handelstage):
        idx = max(0, len(closes) - 1 - handelstage)
        old = closes[idx]
        return round((current_price - old) / old * 100, 2) if old else None

    chart_series = downsample_series([
        (datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), c) for ts, c in pairs
    ])

    return {
        "price": current_price,
        "perf_1m": pct_change(21),
        "perf_3m": pct_change(63),
        "perf_1y": pct_change(min(252, len(closes) - 1)),
        "chart_series": chart_series,
    }


def fetch_price_history_stooq(ticker):
    """Kostenlose, OEFFENTLICHE CSV-Schnittstelle von Stooq - kein API-Key
    noetig, offizieller dokumentierter Download-Endpunkt (kein Scraping,
    wird sogar von der bekannten Python-Bibliothek pandas-datareader
    offiziell unterstuetzt). Liefert NUR Kurs + Kursverlauf, KEINE
    Dividendenrendite/Ausschuettungsquote - dient deshalb als Ergaenzung
    fuer fehlende Kursdaten (z.B. bei manuell recherchierten Eintraegen
    ohne Live-Kurs), nicht als eigenstaendige Fundamentaldaten-Quelle.
    Letzte Reserve in der Kette, NACH Yahoo - bislang 100% Fehlschlagquote
    in GitHub Actions, vermutlich IP-Blockade (siehe Diagnose-Logging
    unten, das das naechste Mal genauer zeigen sollte, WORAN es scheitert)."""
    stooq_symbol = ticker.lower() if "." in ticker else f"{ticker.lower()}.us"
    url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
            csv_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read()[:150]
        log(f"  Stooq-Fehler bei {stooq_symbol}: HTTP {e.code}, Antwort-Anfang: {body}")
        return None
    except Exception as e:
        log(f"  Stooq-Fehler bei {stooq_symbol}: {e}")
        return None

    lines = csv_text.strip().split("\n")
    if len(lines) < 2 or "Date" not in lines[0]:
        log(f"  Stooq lieferte keine verwertbaren Kursdaten fuer {stooq_symbol} "
            f"(HTTP {status}), Antwort-Anfang: {csv_text[:150]!r}")
        return None

    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) >= 5:
            try:
                rows.append((parts[0], float(parts[4])))  # (Datum, Schlusskurs)
            except ValueError:
                continue
    if not rows:
        return None

    closes = [c for _, c in rows]
    current_price = closes[-1]

    def pct_change(handelstage):
        idx = max(0, len(closes) - 1 - handelstage)
        old = closes[idx]
        return round((current_price - old) / old * 100, 2) if old else None

    return {
        "price": current_price,
        "perf_1m": pct_change(21),   # ca. 21 Handelstage = 1 Monat
        "perf_3m": pct_change(63),
        "perf_1y": pct_change(252),
        "chart_series": downsample_series(rows),
    }


def fetch_price_performance(ticker, api_key, current_price):
    """Best-effort: Kursentwicklung 1 Monat / 3 Monate / 1 Jahr, ueber
    FMP's historische Tagesschlusskurse. Nicht garantiert im kostenlosen
    Kontingent enthalten - liefert bei Fehler einfach ueberall None,
    das UI zeigt dann '-' statt eines Prozentwerts."""
    if not api_key or not current_price:
        return {"perf_1m": None, "perf_3m": None, "perf_1y": None}
    data = fmp_get("historical-price-eod/light", api_key,
                    params={"symbol": ticker, "from": (datetime.now() - timedelta(days=380)).strftime("%Y-%m-%d")},
                    base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list):
        return {"perf_1m": None, "perf_3m": None, "perf_1y": None}
    # FMP liefert neueste zuerst - fuer jeden Zeithorizont den Kurs suchen,
    # der am naechsten an "vor X Tagen" liegt.
    def price_before(days):
        target = datetime.now() - timedelta(days=days)
        best = None
        for row in data:
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d")
            except Exception:
                continue
            if d <= target and (best is None or d > best[0]):
                best = (d, row.get("price") or row.get("close"))
        return best[1] if best else None

    def pct_change(days):
        old_price = price_before(days)
        if not old_price:
            return None
        return round((current_price - old_price) / old_price * 100, 2)

    return {
        "perf_1m": pct_change(30),
        "perf_3m": pct_change(91),
        "perf_1y": pct_change(365),
        "chart_series": downsample_series(sorted(
            [(row["date"], row.get("price") or row.get("close"))
             for row in data if row.get("date") and (row.get("price") or row.get("close"))],
            key=lambda pair: pair[0]
        )),
    }


def downsample_series(pairs, max_points=60):
    """Reduziert eine (Datum, Kurs)-Liste auf max. max_points gleichmaessig
    verteilte Punkte - haelt den Chart-Datenanteil im HTML klein, auch wenn
    der Rohverlauf 250+ Tagesschlusskurse hat. Der letzte (aktuellste) Punkt
    bleibt immer erhalten."""
    if len(pairs) <= max_points:
        return pairs
    step = len(pairs) / max_points
    indices = sorted(set(int(i * step) for i in range(max_points)))
    sampled = [pairs[i] for i in indices]
    if sampled[-1] != pairs[-1]:
        sampled.append(pairs[-1])
    return sampled


def update_price_history(entry, current_price):
    """Haengt den aktuellen Kurs an die gespeicherte Historie an (gedeckelt
    auf PRICE_HISTORY_KEEP Eintraege), damit ein 'seit X Laeufen ruecklaeufig'
    erkannt werden kann."""
    history_list = entry.get("price_history", [])
    today = datetime.now().strftime("%Y-%m-%d")
    if not history_list or history_list[-1]["date"] != today:
        history_list.append({"date": today, "price": current_price})
    history_list = history_list[-PRICE_HISTORY_KEEP:]
    entry["price_history"] = history_list
    return history_list


def is_declining_trend(price_history, days=DECLINE_TREND_DAYS):
    """Einfache, nachvollziehbare Definition: der Kurs von vor `days`
    gespeicherten Laeufen war hoeher als der aktuelle - UND es gab
    unterwegs keinen neuen Hochpunkt (also eine echte Abwaertstendenz,
    kein Auf-und-Ab)."""
    if len(price_history) < days + 1:
        return False, "noch nicht genug Historie"
    window = price_history[-(days + 1):]
    prices = [p["price"] for p in window]
    if prices[-1] >= prices[0]:
        return False, None
    # kein neuer Hochpunkt innerhalb des Fensters nach dem Start
    if max(prices[1:]) > prices[0]:
        return False, None
    return True, f"seit {days} Laeufen ruecklaeufig ({prices[0]:.2f} -> {prices[-1]:.2f})"


# Payout-Schwellen fuer die meisten Sektoren, abgeglichen mit mehreren
# unabhaengigen Ratgebern (finanzen.net "magisches Quadrat": 25-75%;
# Investing.com Academy/XTB/Captrader: "gesund" 30-60%, "Warnsignal" ab
# 70-80%; ING: kein fester Prozentwert, aber "nahe/ueber 100%" als klares
# Warnsignal). Wir uebernehmen den am haeufigsten genannten Bereich.
PAYOUT_NIEDRIG_MAX = 60.0
PAYOUT_MITTEL_MAX = 80.0   # vorher 85 - mehrere Quellen nennen 80% als Warnschwelle
# REITs (und aehnliche Immobilien-/Beteiligungsvehikel) sind PER GESETZ
# verpflichtet, den Grossteil ihres Gewinns auszuschuetten (in den USA
# z.B. mind. 90% des steuerpflichtigen Gewinns) - eine Ausschuettungsquote
# von 85% ist dort NORMAL, nicht riskant. Mehrere Ratgeber (Captrader,
# Investing.com Academy) nennen REITs/Versorger explizit als Ausnahme von
# den generischen Payout-Schwellen. Wir erkennen das grob ueber den Sektor.
REIT_SEKTOREN = {"Immobilien"}
PAYOUT_NIEDRIG_MAX_REIT = 75.0
PAYOUT_MITTEL_MAX_REIT = 95.0


def evaluate_scanner_candidate(f):
    """Risiko-Einstufung statt qualifiziert/nicht-qualifiziert: JEDE Aktie
    mit Rendite >= MIN_YIELD_PCT wird angezeigt (Filterung passiert beim
    Aufbau der Scanner-Liste), hier wird nur noch das RISIKO bewertet -
    niedrig/mittel/hoch, anhand Ausschuettungsquote und Verschuldung.
    Eine hohe Rendite ist also kein Ausschlussgrund mehr, sondern wird
    im Risiko sichtbar gemacht (hohe Rendite + hohe Ausschuettungsquote
    = typisches Muster einer Dividenden-Falle -> Risiko 'hoch').

    WICHTIG zur Verschuldung (Debt/Equity): anders als bei der Ausschuett-
    ungsquote gibt es dafuer KEINE einheitliche, von Banken/Brokern
    veroeffentlichte Schwelle (ING/Consorsbank etc. pruefen stattdessen
    eher den freien Cashflow qualitativ, ohne festen Prozentwert). Die
    hier genutzten Werte (<=1,0 / <=2,0) sind eine allgemein gebraeuchliche
    Bilanzkennzahl-Faustregel, keine Eins-zu-eins-Uebernahme eines
    bestimmten Broker-Ratingmodells - transparent so im Steckbrief
    ausgewiesen."""
    payout = f.get("payout_ratio_pct")
    yield_pct = f.get("dividend_yield_pct")
    debt = f.get("debt_to_equity")
    is_reit = f.get("sektor") in REIT_SEKTOREN
    niedrig_max = PAYOUT_NIEDRIG_MAX_REIT if is_reit else PAYOUT_NIEDRIG_MAX
    mittel_max = PAYOUT_MITTEL_MAX_REIT if is_reit else PAYOUT_MITTEL_MAX

    trap_warning = (payout is not None and payout > 100) or (
        yield_pct is not None and yield_pct > 12
    )

    if trap_warning:
        risk = "hoch"
    elif (payout is not None and payout <= niedrig_max) and (debt is None or debt <= 1.0):
        risk = "niedrig"
    elif (payout is not None and payout <= mittel_max) and (debt is None or debt <= 2.0):
        risk = "mittel"
    else:
        risk = "hoch"

    return {
        "trap_warning": trap_warning,
        "risk": risk,
        "meets_min_yield": yield_pct is not None and yield_pct >= MIN_YIELD_PCT,
        "is_reit": is_reit,
        "payout_niedrig_max": niedrig_max,
        "payout_mittel_max": mittel_max,
    }


def evaluate_depot_position(position, current, grades):
    """Status-Ampel fuer eine ECHTE Depot-Position. Gibt Fakten und
    Schwellenwert-Ueberschreitungen zurueck - keine Kauf-/Verkaufsempfehlung."""
    kaufkurs = position["kaufkurs"]
    price = current.get("price")
    payout = current.get("payout_ratio_pct")
    grade_info = grades[0] if grades else None

    gain_pct = ((price - kaufkurs) / kaufkurs * 100) if price and kaufkurs else None
    rendite_auf_kaufkurs = None
    if current.get("dividend_yield_pct") is not None and price and kaufkurs:
        # Dividende pro Anteil aus aktueller Marktrendite * aktuellem Kurs
        # zurueckrechnen, dann auf den Kaufkurs beziehen.
        dividend_per_share = current["dividend_yield_pct"] / 100 * price
        rendite_auf_kaufkurs = dividend_per_share / kaufkurs * 100

    declining, decline_reason = is_declining_trend(current.get("price_history", []))

    grade_downgrade = False
    grade_note = None
    if grade_info and grade_info.get("action") == "downgrade":
        grade_downgrade = True
        grade_note = (f"{grade_info.get('firm', 'Analyst')}: "
                      f"{grade_info.get('previous_grade')} -> {grade_info.get('new_grade')} "
                      f"({grade_info.get('date')})")

    red_reasons = []
    if payout is not None and payout > 100:
        red_reasons.append("Ausschuettungsquote > 100% (nicht mehr finanzierbar)")

    yellow_reasons = []
    if gain_pct is not None and gain_pct <= -YELLOW_PRICE_DROP_PCT:
        yellow_reasons.append(f"Kurs {abs(gain_pct):.1f}% unter Kaufkurs")
    if declining:
        yellow_reasons.append(decline_reason)
    if grade_downgrade:
        yellow_reasons.append(f"neue ruecklaeufige Analysteneinschaetzung: {grade_note}")

    if red_reasons:
        status = "rot"
    elif yellow_reasons:
        status = "gelb"
    else:
        status = "gruen"

    return {
        "gain_pct": gain_pct,
        "rendite_auf_kaufkurs": rendite_auf_kaufkurs,
        "status": status,
        "red_reasons": red_reasons,
        "yellow_reasons": yellow_reasons,
    }


def main():
    cfg = load_config()
    fmp_key = cfg.get("fmp_api_key")
    twelve_data_key = cfg.get("twelve_data_api_key")
    finnhub_key = cfg.get("finnhub_api_key")
    alpha_vantage_key = cfg.get("alpha_vantage_api_key")
    marketstack_key = cfg.get("marketstack_api_key")
    leeway_key = cfg.get("leeway_api_key")
    brapi_key = cfg.get("brapi_api_key")
    eodhd_key = cfg.get("eodhd_api_key")
    financialdata_key = cfg.get("financialdata_api_key")
    if not any([fmp_key, twelve_data_key, finnhub_key, alpha_vantage_key, marketstack_key, leeway_key, brapi_key]):
        log("Kein einziger API-Key in config.json gefunden - Abbruch.")
        return
    fehlende_quellen = [n for n, k in [("Twelve Data", twelve_data_key), ("Finnhub", finnhub_key),
                                       ("Alpha Vantage", alpha_vantage_key), ("Marketstack", marketstack_key),
                                       ("Leeway.tech", leeway_key), ("brapi.dev", brapi_key),
                                       ("financialdata.net", financialdata_key)]
                        if not k]
    if fehlende_quellen:
        log(f"Hinweis: kein Key fuer {', '.join(fehlende_quellen)} hinterlegt - "
            f"Ticker, die von den vorhandenen Quellen nicht geliefert werden, "
            f"werden dann einfach uebersprungen statt eine weitere Ausweich-Quelle zu versuchen.")

    depot_positions = load_depot()
    history = load_json(HISTORY_PATH, {})
    discovered = load_discovered_candidates()

    depot_tickers = {p["ticker"] for p in depot_positions}
    scanner_tickers = {t for t, _, _ in CANDIDATE_UNIVERSE}
    scanner_tickers |= {t for t, info in discovered.items() if info.get("qualified")}

    land_sektor_lookup = {t: (land, sektor) for t, land, sektor in CANDIDATE_UNIVERSE}
    for t, info in discovered.items():
        land_sektor_lookup[t] = (info.get("land", "unbekannt"), info.get("sektor", "unbekannt"))

    # --- Auto-Scanner: automatisch neue Kandidaten finden (kostenlos, ueber
    # das bestehende FMP-Kontingent) - siehe fetch_screener_candidates() ---
    auto_discover_neu = []
    if fmp_key:
        bereits_bekannt = scanner_tickers | depot_tickers | set(discovered.keys())
        screener_results = fetch_screener_candidates(fmp_key)
        kandidaten = [d for d in screener_results
                      if d.get("symbol") and d["symbol"] not in bereits_bekannt]
        auto_discover_neu = kandidaten[:AUTO_DISCOVER_MAX_NEW]
        if auto_discover_neu:
            log(f"  Auto-Scanner: {len(auto_discover_neu)} neue, bisher unbekannte "
                f"Ticker werden probeweise durchlaufen (kostet Kontingent!): "
                f"{', '.join(d['symbol'] for d in auto_discover_neu)}")
            for d in auto_discover_neu:
                symbol = d["symbol"]
                scanner_tickers.add(symbol)
                land_sektor_lookup[symbol] = (d.get("country") or "unbekannt", d.get("sector") or "unbekannt")

    all_tickers = depot_tickers | scanner_tickers
    log(f"Hole Daten fuer {len(all_tickers)} Ticker ({len(depot_tickers)} im Depot, "
        f"{len(scanner_tickers)} im Scanner, davon {len(auto_discover_neu)} neu vom Auto-Scanner) ...")

    # --- DURCHGANG 1: Basisdaten (Kurs, Rendite, Ausschuettungsquote,
    # Verschuldung) fuer ALLE Ticker. Bewusst OHNE ISIN/News/Grades/Zahl-
    # termin - das waeren pro Aktie 4 weitere FMP-Calls, und bei 187+
    # Tickern reicht das 250er-Tageskontingent dafuer strukturell nicht
    # (siehe DURCHGANG 2 unten, der genau deshalb nur eine Top-Auswahl
    # anreichert statt alle). ---
    for ticker in all_tickers:
        log(f"  Hole Daten fuer {ticker} ...")
        data = fetch_fundamentals(ticker, fmp_key, twelve_data_key, finnhub_key,
                                   alpha_vantage_key, marketstack_key, leeway_key, brapi_key, eodhd_key)
        if data is None:
            log(f"    -> keine Daten erhalten (weder FMP noch Twelve Data), ueberspringe.")
            time.sleep(0.3)
            continue
        land, sektor = land_sektor_lookup.get(ticker, ("unbekannt", "unbekannt"))
        data["land"] = land
        data["sektor"] = sektor

        # NEU: fehlende Kennzahlen (v.a. KGV/Marktkap/Ausschuettungsquote -
        # oft leer bei manuell recherchierten oder ueber Finnhub/brapi
        # bezogenen Eintraegen) kostenlos ueber Yahoo nachfuellen. Laeuft
        # fuer ALLE Ticker (kein Kontingent-Limit wie bei FMP), da Yahoo
        # praktisch jede Boerse weltweit abdeckt.
        if data.get("pe_ratio") is None or data.get("market_cap") is None or data.get("payout_ratio_pct") is None:
            yahoo_kennzahlen = fetch_fundamentals_yahoo(ticker)
            if yahoo_kennzahlen:
                ergaenzt = []
                for feld in ("pe_ratio", "market_cap", "payout_ratio_pct"):
                    if data.get(feld) is None and yahoo_kennzahlen.get(feld) is not None:
                        data[feld] = yahoo_kennzahlen[feld]
                        ergaenzt.append(feld)
                if ergaenzt:
                    log(f"    -> {', '.join(ergaenzt)} via Yahoo ergaenzt")

        data["price_history"] = history.get(ticker, {}).get("price_history", [])
        update_price_history(data, data["price"])
        data["evaluation"] = evaluate_scanner_candidate(data)
        data["last_updated"] = datetime.now().isoformat()
        data["perf_1m"] = data["perf_3m"] = data["perf_1y"] = None
        data["isin"] = None
        data["news"] = None
        data["chart_series"] = None
        data["grades"] = []
        data["next_payout"] = None

        history[ticker] = data
        time.sleep(0.3)  # sanftes Tempo, kein Ansturm auf die kostenlosen APIs

    # --- DURCHGANG 2: FMP-lastige Anreicherung (Kursverlauf, ISIN, News,
    # Analysten-Grades, Zahltermin) NUR fuer eine begrenzte Top-Auswahl +
    # Depot. Dieselbe Sortierung wie spaeter die Watchlist (Risiko, dann
    # Rendite), damit die "wichtigsten" Aktien das knappe FMP-Kontingent
    # bekommen statt es auf 90+ Kandidaten zu verteilen und ueberall nur
    # HTTP-429 zu kassieren. ---
    RISK_ORDER_ENRICH = {"niedrig": 0, "mittel": 1, "hoch": 2}
    qualifizierte = [t for t in scanner_tickers
                     if t in history and history[t].get("evaluation", {}).get("meets_min_yield")]
    qualifizierte.sort(key=lambda t: (RISK_ORDER_ENRICH.get(history[t]["evaluation"].get("risk"), 2),
                                       -(history[t].get("dividend_yield_pct") or 0)))
    enrich_tickers = list(dict.fromkeys(qualifizierte[:ENRICH_TOP_N] + [t for t in depot_tickers if t in history]))
    log(f"Anreicherung (ISIN/News/Grades/Zahltermin) fuer {len(enrich_tickers)} "
        f"Top-Kandidaten + Depot (von {len(qualifizierte)} qualifizierten) - "
        f"schont das FMP-Kontingent ...")

    for ticker in enrich_tickers:
        data = history[ticker]
        if fmp_key:
            # Kursentwicklung (1M/3M/1J) + ISIN + News-Sentiment.
            data.update(fetch_price_performance(ticker, fmp_key, data.get("price")))
            data["isin"] = fetch_isin(ticker, fmp_key) or data.get("manual_isin")
            data["news"] = fetch_news_sentiment(ticker, fmp_key)
            if not data["news"] and finnhub_key:
                data["news"] = fetch_news_sentiment_finnhub(ticker, finnhub_key)
                if data["news"]:
                    log(f"    -> News via Finnhub ergaenzt (FMP lieferte nichts)")
            data["evaluation"] = apply_news_sentiment_to_risk(data["evaluation"], data["news"])

        if data.get("price") is None or data.get("perf_1y") is None:
            # Reihenfolge bewusst so gewaehlt, dass moeglichst wenig
            # KONTINGENT pro Lauf verbraucht wird:
            #   1. Yahoo zuerst - kein Key, kein Tageslimit, EIN Call
            #      liefert Kurs + volle Historie (kostet uns nichts).
            #   2. Twelve Data erst danach - teilt sich das 800er-
            #      Tageskontingent mit den Fundamentaldaten-Abfragen,
            #      also nur anfassen, wenn Yahoo nichts liefert.
            #   3. Stooq als letzte Reserve (bisher 100% Fehlschlag in
            #      GitHub Actions, vermutlich IP-Blockade).
            supplement = fetch_price_history_yahoo(ticker)
            quelle = "Yahoo"
            if not supplement:
                supplement = fetch_price_history_twelvedata(ticker, twelve_data_key)
                quelle = "Twelve Data"
            if not supplement:
                supplement = fetch_price_history_stooq(ticker)
                quelle = "Stooq"
            if supplement:
                if data.get("price") is None:
                    data["price"] = supplement["price"]
                for feld in ("perf_1m", "perf_3m", "perf_1y"):
                    if data.get(feld) is None:
                        data[feld] = supplement[feld]
                if not data.get("chart_series") and supplement.get("chart_series"):
                    data["chart_series"] = supplement["chart_series"]
                log(f"    -> Kurs/Kursverlauf via {quelle} ergaenzt")

        if fmp_key:
            # Analysten-Ratings + Zahltermin: bisher nur fuer Depot-Positionen
            # geholt, jetzt fuer JEDEN Watchlist-Kandidaten (Tabelle zeigt den
            # Zahltermin jetzt als Spalte, Steckbrief die letzten 3 Ratings).
            # KOSTEN-HINWEIS: das sind 2 zusaetzliche FMP-Calls pro Aktie, die
            # es in die Watchlist schafft (vorher 0) - bei chronisch knappem
            # FMP-Kontingent (300/Tag) im Auge behalten; falls es zu oft
            # ausgeht, liesse sich das leicht auf die Top-N Kandidaten
            # begrenzen statt auf alle.
            data["grades"] = fetch_recent_grades(ticker, fmp_key)
            if not data["grades"] and finnhub_key:
                data["grades"] = fetch_recommendation_trend_finnhub(ticker, finnhub_key)
                if data["grades"]:
                    log(f"    -> Analysten-Konsens via Finnhub ergaenzt (FMP lieferte nichts)")
            data["next_payout"] = fetch_next_payout(ticker, fmp_key)
            if not data["next_payout"] and financialdata_key:
                data["next_payout"] = fetch_next_payout_financialdata(ticker, financialdata_key)
                if data["next_payout"]:
                    log(f"    -> Auszahlungstermin via financialdata.net ergaenzt (FMP lieferte nichts)")
            data["evaluation"] = apply_analyst_grades_to_risk(data["evaluation"], data["grades"])

        history[ticker] = data
        time.sleep(0.3)

    save_json(HISTORY_PATH, history)

    if auto_discover_neu:
        heute = datetime.now().strftime("%Y-%m-%d")
        for d in auto_discover_neu:
            symbol = d["symbol"]
            qualified = bool(history.get(symbol, {}).get("evaluation", {}).get("meets_min_yield"))
            discovered[symbol] = {
                "land": d.get("country") or "unbekannt",
                "sektor": d.get("sector") or "unbekannt",
                "qualified": qualified,
                "checked_on": heute,
            }
            log(f"  Auto-Scanner: {symbol} {'qualifiziert - dauerhaft in Watchlist uebernommen' if qualified else 'erreicht Mindestrendite nicht - wird nicht erneut versucht'}")
        save_json(DISCOVERED_PATH, discovered)

    # Depot-Bewertung
    depot_view = []
    for pos in depot_positions:
        current = history.get(pos["ticker"])
        if not current:
            continue
        ev = evaluate_depot_position(pos, current, current.get("grades"))
        depot_view.append({**pos, "current": current, "depot_evaluation": ev})

    # Scanner: NUR NOCH Aktien mit Rendite >= MIN_YIELD_PCT anzeigen (statt
    # qualifiziert/nicht-qualifiziert), sortiert nach Risiko (niedrig zuerst),
    # innerhalb eines Risiko-Ranges nach Rendite absteigend.
    RISK_ORDER = {"niedrig": 0, "mittel": 1, "hoch": 2}
    scanner_view = [history[t] for t in scanner_tickers
                    if t in history and history[t].get("evaluation", {}).get("meets_min_yield")]
    scanner_view.sort(key=lambda e: (RISK_ORDER.get(e["evaluation"].get("risk"), 2),
                                      -(e.get("dividend_yield_pct") or 0)))
    for i, e in enumerate(scanner_view):
        e["rank"] = i + 1

    # "Chance"-Hinweis: bester Kandidat mit Risiko 'niedrig' oder 'mittel'
    # vs. schwaechste Depot-Position (nach Rendite auf Kaufkurs)
    chance_hinweis = None
    solide_kandidaten = [e for e in scanner_view if e["evaluation"]["risk"] != "hoch"]
    if depot_view and solide_kandidaten:
        weakest = min(depot_view, key=lambda d: d["depot_evaluation"]["rendite_auf_kaufkurs"] or 0)
        best_candidate = solide_kandidaten[0]
        if (best_candidate["dividend_yield_pct"] or 0) > (
            weakest["depot_evaluation"]["rendite_auf_kaufkurs"] or 0
        ) + 1.0:  # mind. 1 Prozentpunkt besser, um Rauschen zu vermeiden
            chance_hinweis = (
                f"{best_candidate['ticker']} ({best_candidate['dividend_yield_pct']:.1f}% Rendite, "
                f"Risiko {best_candidate['evaluation']['risk']}) schlaegt aktuell deine schwaechste "
                f"Position {weakest['ticker']} ({(weakest['depot_evaluation']['rendite_auf_kaufkurs'] or 0):.1f}% "
                f"Rendite auf Kaufkurs) - koennte einen Blick wert sein."
            )

    fx_rates = fetch_live_fx_rates()
    build_report(depot_view, scanner_view, chance_hinweis, fx_rates)
    build_recherche_page()
    log(f"Fertig. Bericht aktualisiert: {REPORT_PATH}")


# ---------------------------------------------------------------------------
# Recherche-Seite: statische Linkliste zu externen Dividenden-Recherche-
# Quellen. Wird NICHT automatisiert abgefragt/gescraped (nur echte Links
# zum Selbst-Anklicken) - neue Quellen einfach hier ergaenzen.
# ---------------------------------------------------------------------------
RECHERCHE_LINKS = [
    ("Dividenden Guru - Beste Dividendenaktien", "https://dividenden.guru/beste-dividendenaktien/",
     "Kuratierte Top-Picks Deutschland/USA/Welt, monatlich aktualisiert - Claude konnte lesen"),
    ("Börse Online - Morningstar-Empfehlungen", "https://www.boerse-online.de/nachrichten/aktien/",
     "Regelmaessige Artikel zu unterbewerteten Qualitaets-Dividendenaktien - Claude konnte lesen"),
    ("Aktienfinder - Dividenden-Kalender", "https://aktienfinder.net/dividenden-kalender",
     "Ausschuettungstermine - Claude konnte NICHT lesen (JavaScript-App)"),
    ("Aktienfinder - Dividenden-Aristokraten", "https://aktienfinder.net/dividenden-aristokraten",
     "Qualitaets-Aristokraten-Liste - Claude konnte NICHT lesen (JavaScript-App), bitte selbst pruefen"),
    ("justETF - Top-Ausschuettungsrendite", "https://www.justetf.com/de/market-overview/top-50-aktien-etfs-mit-der-hoechsten-ausschuettungsrendite-in-eur.html",
     "Nur ETFs, keine Einzelaktien - Claude konnte lesen"),
    ("Finanzen100 - Top100 Dividendenrendite DE", "https://www.finanzen100.de/top100/die-deutschen-aktien-mit-der-hochsten-dividendenrendite/",
     "Deutsche Top-Dividendenrenditen - Claude konnte NICHT lesen (Zugriff blockiert), bitte selbst pruefen"),
    ("Parqet - Hoechste Dividendenrendite Japan", "https://parqet.com/de/insights/hoechste-dividendenrendite/japan",
     "ACHTUNG: reine Rendite-Sortierung ohne Qualitaetsfilter, viele Kleinstwerte/Fallen - Claude konnte lesen, aber bewusst nicht uebernommen"),
    ("Parqet - Hoechste Dividendenrendite China", "https://parqet.com/de/insights/hoechste-dividendenrendite/china",
     "Gleiche Einschraenkung wie Japan-Liste - noch nicht geprueft"),
    ("Morningstar - Top 10 Eurozone", "https://global.morningstar.com/de/aktien/die-10-besten-dividenden-aktien-aus-der-eurozone",
     "Noch nicht geprueft, bitte selbst ansehen"),
    ("Simply Wall St - 3 European Dividend Stocks", "https://simplywall.st/de/stocks/ch/insurance/vtx-sren/swiss-re-shares/news/3-european-dividend-stocks-to-consider-6",
     "Noch nicht geprueft, bitte selbst ansehen"),
    ("DAS INVESTMENT - DZ Bank Aristokraten Europa", "https://www.dasinvestment.com/dividendenaristokraten-aus-europa-dz-bank-studie/",
     "Claude konnte lesen: 15 Aristokraten, meist unter 5% Rendite (z.B. Air Liquide 2,3%) - passt eher zu 'sicher' als zu 'hohe Rendite'"),
    ("Aktien.guide - Dividendenadel", "https://aktien.guide/dividendenadel",
     "Noch nicht geprueft, bitte selbst ansehen"),
    ("MarketScreener - Aktien Suedamerika", "https://de.marketscreener.com/boerse/aktien/sudamerika/",
     "Noch nicht geprueft, bitte selbst ansehen"),
]


def build_recherche_page():
    rows = "".join(
        f'<tr><td><a href="{url}" target="_blank" rel="noopener">{name}</a></td><td>{beschreibung}</td></tr>'
        for name, url, beschreibung in RECHERCHE_LINKS
    )
    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Recherche - Dividendenatlas</title>
<style>
:root{{
  --bg:#FFFFFF; --surface:#F4F4F6; --text:#1A1A1E; --muted:#75757C;
  --blue:#0F3460; --blueSoft:#E3EDF7; --line:#DCE4EC;
}}
*{{box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;margin:0;padding:29px;font-size:14px;}}
h1{{font-size:27px;margin:0 0 6px;color:var(--blue);}}
.meta{{color:var(--muted);font-size:14px;margin-bottom:19px;}}
.back{{display:inline-block;margin-bottom:20px;color:var(--blue);text-decoration:none;font-size:13px;}}
.back:hover{{text-decoration:underline;}}
table{{width:100%;border-collapse:collapse;}}
th{{text-align:left;padding:10px;color:var(--blue);font-size:12px;text-transform:uppercase;border-bottom:2px solid var(--line);}}
td{{padding:12px 10px;border-bottom:1px solid var(--line);}}
a{{color:var(--blue);font-weight:600;}}
.hint{{color:var(--muted);font-size:12px;font-style:italic;margin-top:20px;}}
</style>
</head>
<body>
<a class="back" href="dividendenatlas.html">← zurueck zum Dividendenatlas</a>
<h1>Recherche</h1>
<div class="meta">Externe Quellen fuer neue Dividendenaktien-Ideen - manuell pruefen, nicht automatisiert abgefragt.</div>
<table>
  <thead><tr><th>Quelle</th><th>Beschreibung</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<div class="hint">Findest du hier interessante neue Aktien mit ≥{MIN_YIELD_PCT:.0f}% Rendite? Schick mir den Link/Ticker im Chat, dann ergaenze ich sie in die Kandidatenliste.</div>
</body></html>"""
    with open(RECHERCHE_PATH, "w", encoding="utf-8") as f:
        f.write(html)


def build_report(depot_view, scanner_view, chance_hinweis, fx_rates=None):
    fx_rates = fx_rates or dict(FALLBACK_FX_RATES_TO_EUR)
    fx_json = json.dumps(fx_rates)
    depot_payload = json.dumps(depot_view, ensure_ascii=False, default=str)
    scanner_payload = json.dumps(scanner_view, ensure_ascii=False, default=str)

    ampel_counts = {"rot": 0, "gelb": 0, "gruen": 0}
    for d in depot_view:
        ampel_counts[d["depot_evaluation"]["status"]] += 1

    avg_rendite_kaufkurs = (
        sum(d["depot_evaluation"]["rendite_auf_kaufkurs"] or 0 for d in depot_view) / len(depot_view)
        if depot_view else 0
    )

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Dividendenatlas</title>
<link rel="icon" type="image/svg+xml" href="{LOGO_DATA_URI}">
<link rel="apple-touch-icon" href="{LOGO_DATA_URI}">
<style>
:root{{
  --bg:#FFFFFF; --surface:#F7F8FA; --text:#0A1929; --muted:#5B7290;
  --blue:#0F3460; --blueB:#1E5F8C; --blueSoft:#E3EDF7; --line:#E4E9EF; --gray:#8A99A8;
  --pink:#E91E63; --pinkSoft:#FDE7EE;
  --red:#D32F2F; --redSoft:#FDECEC; --green:#1B8A3E; --greenSoft:#E8F7ED;
  --yellow:#B8860B; --yellowSoft:#FFF6DC; --orange:#CC6A1E; --orangeSoft:#FFEFDC;
  --shadow: 0 1px 3px rgba(15,52,96,0.08), 0 1px 2px rgba(15,52,96,0.06);
}}
*{{box-sizing:border-box;}}
body{{background:var(--surface);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     margin:0;padding:0;font-size:13.5px;line-height:1.5;}}
.page-wrap{{max-width:1400px;margin:0 auto;padding:0 24px 40px;}}
.top-bar{{background:var(--blue);color:#fff;padding:22px 24px;margin-bottom:24px;}}
.top-bar h1{{font-size:26px;margin:0;font-weight:800;letter-spacing:-0.3px;}}
.top-bar .meta{{color:#C7D6E8;font-size:13px;margin-top:4px;}}
.top-bar .meta a{{color:#fff;font-weight:600;}}
h2{{font-size:18px;margin:30px 0 12px;color:var(--blue);font-weight:800;letter-spacing:-0.2px;}}
.summary{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px;}}
.summary-card{{background:#fff;border-radius:10px;padding:16px 20px;min-width:140px;box-shadow:var(--shadow);border:1px solid var(--line);}}
.summary-card .big{{font-size:25px;font-weight:800;color:var(--blue);}}
.summary-card .label{{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:0.4px;font-weight:600;}}
.summary-card.rot .big{{color:var(--red);}}
.summary-card.gelb .big{{color:var(--yellow);}}
.summary-card.gruen .big{{color:var(--green);}}
.chance-box{{background:var(--pinkSoft);border:1px solid var(--pink);border-radius:10px;padding:15px 20px;margin-bottom:20px;box-shadow:var(--shadow);}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;background:#fff;border-radius:10px;overflow:hidden;box-shadow:var(--shadow);}}
th{{text-align:left;padding:11px 10px;color:var(--blue);font-size:10.5px;text-transform:uppercase;letter-spacing:0.3px;font-weight:700;
   border-bottom:2px solid var(--line);background:var(--surface);}}
td{{padding:10px;border-bottom:1px solid var(--line);vertical-align:top;}}
tr:hover td{{background:var(--blueSoft);}}
tr.rot td{{background:var(--redSoft);}}
tr.gelb td{{background:var(--yellowSoft);}}
.badge{{font-size:10px;font-weight:700;padding:3px 8px;border-radius:10px;border:1px solid var(--gray);color:var(--gray);margin-right:3px;display:inline-block;margin-top:2px;}}
.badge.rot{{border-color:var(--red);color:var(--red);background:var(--redSoft);}}
.badge.gelb{{border-color:var(--yellow);color:var(--yellow);background:var(--yellowSoft);}}
.badge.orange{{border-color:var(--orange);color:var(--orange);background:var(--orangeSoft);}}
.badge.gruen{{border-color:var(--green);color:var(--green);background:var(--greenSoft);}}
.ampel{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;vertical-align:middle;}}
.ampel.rot{{background:var(--red);}}
.ampel.gelb{{background:var(--yellow);}}
.ampel.gruen{{background:var(--green);}}
.yield{{font-weight:800;font-size:14.5px;color:var(--blue);}}
.reasons{{font-size:11px;color:var(--muted);margin-top:3px;}}
.hint{{color:var(--muted);font-size:12px;margin-bottom:14px;}}
#scanner-tbody tr{{cursor:pointer;}}
#scanner-tbl th[data-key]{{cursor:pointer;user-select:none;}}
#scanner-tbl th[data-key]:hover{{color:var(--pink);}}
.editable-value{{cursor:text;border-bottom:1.5px dotted var(--muted);padding-bottom:1px;}}
.editable-value:hover{{background:var(--blueSoft);}}
td.overridden{{background:var(--yellowSoft);}}
td.overridden .editable-value{{border-bottom-color:var(--yellow);}}
td.computed-hint .editable-value{{border-bottom-style:dashed;border-bottom-color:var(--blueB);color:var(--blueB);}}
.risk-filter-btn{{border:1px solid var(--line);background:#fff;border-radius:20px;padding:7px 15px;font-size:12px;font-weight:600;cursor:pointer;margin-right:8px;color:var(--muted);}}
.risk-filter-btn.active{{border-color:var(--pink);color:var(--pink);background:var(--pinkSoft);}}
.badge.niedrig{{border-color:var(--green);color:var(--green);background:var(--greenSoft);}}
.badge.mittel{{border-color:var(--orange);color:var(--orange);background:var(--orangeSoft);}}
.badge.hoch{{border-color:var(--red);color:var(--red);background:var(--redSoft);}}
.perf-pos{{color:var(--green);font-weight:700;}}
.perf-neg{{color:var(--red);font-weight:700;}}
.action-btn{{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 10px;font-size:11px;font-weight:600;cursor:pointer;margin:1px;color:var(--blue);}}
.action-btn:hover{{background:var(--pink);color:#fff;border-color:var(--pink);}}
#detail-content h3{{margin-top:0;color:var(--blue);font-weight:800;}}
#detail-content table{{font-size:12px;box-shadow:none;}}
#detail-content td{{padding:6px 8px;}}
#detail-content .close-btn{{float:right;cursor:pointer;font-size:22px;color:var(--muted);}}
</style>
</head>
<body>
<div class="top-bar">
  <div style="display:flex;align-items:center;gap:14px;">
    <div style="width:44px;height:44px;flex-shrink:0;">{LOGO_SVG}</div>
    <h1>Dividendenatlas</h1>
  </div>
  <div class="meta">Stand: <span id="stand"></span> · <a href="recherche.html">→ Recherche-Quellen</a></div>
</div>
<div class="page-wrap">

<h2>Dividendendepot (<span id="depot-count">{len(depot_view)}</span> Positionen)</h2>
<div class="summary">
  <div class="summary-card rot"><div class="big">{ampel_counts['rot']}</div><div class="label">Rot</div></div>
  <div class="summary-card gelb"><div class="big">{ampel_counts['gelb']}</div><div class="label">Gelb</div></div>
  <div class="summary-card gruen"><div class="big">{ampel_counts['gruen']}</div><div class="label">Gruen</div></div>
  <div class="summary-card"><div class="big" id="depot-avg-yield">{avg_rendite_kaufkurs:.2f}%</div><div class="label">Ø Rendite auf Kaufkurs</div></div>
</div>
{"<div class='chance-box'>💡 <strong>Chance:</strong> " + chance_hinweis + "</div>" if chance_hinweis else ""}
<div class="hint">Fakten und Schwellenwerte, keine Kauf-/Verkaufsempfehlung - die Entscheidung bleibt bei dir.</div>
<table id="depot-tbl">
  <thead><tr>
    <th title="Ampel-Status dieser Position: gruen = alles im gruenen Bereich, gelb = beobachten, rot = sofort handeln"></th>
    <th title="Boersenkuerzel der Aktie">Ticker</th>
    <th title="Vollstaendiger Firmenname">Name</th>
    <th title="Aktuell gehaltene Gesamt-Anzahl Anteile (nach Zukaeufen/Teilverkaeufen)">Anteile</th>
    <th title="Durchschnittlicher Kaufkurs pro Anteil, gewichtet ueber alle Kaeufe">Ø Kaufkurs</th>
    <th title="Datum des ersten Kaufs dieser Position">Kaufdatum</th>
    <th title="Aktueller Boersenkurs">Akt. Kurs</th>
    <th title="Prozentuale Kursveraenderung seit deinem (gewichteten) Kaufkurs">Kursentw.</th>
    <th title="Jaehrliche Dividende geteilt durch deinen Kaufkurs - deine persoenliche Rendite, nicht die aktuelle Marktrendite">Rendite auf Kaufkurs</th>
    <th title="Aktuelle Dividendenrendite zum heutigen Kurs - das, was ein Neukaeufer heute bekaeme">Akt. Marktrendite</th>
    <th title="Anteil des Gewinns, der als Dividende ausgeschuettet wird. Ueber 100% bedeutet: mehr ausgeschuettet als verdient - Warnsignal">Ausschuett.-Quote</th>
    <th title="Naechster erwarteter Ausschuettungstermin (geschaetzt oder manuell eingetragen)">Naechster Zahltermin</th>
    <th title="Konkrete Gruende, falls Status gelb/rot ist">Hinweise</th>
    <th title="Zukauf, Teilverkauf oder Entfernen dieser Position">Aktionen</th>
  </tr></thead>
  <tbody id="depot-tbody"></tbody>
</table>

<h2>Watchlist (<span id="scanner-count">{len(scanner_view)}</span> Kandidaten mit ≥{MIN_YIELD_PCT:.0f}% Rendite)</h2>
<div class="hint">Nur Inspiration, komplett getrennt vom Dividendendepot. Zeigt ausschliesslich Aktien mit Rendite ≥ {MIN_YIELD_PCT:.0f}%. Klick auf eine Zeile fuer das vollstaendige Profil. Klick auf eine Spaltenüberschrift zum Sortieren.</div>
<div class="controls" style="margin-bottom:10px;">
  <button class="risk-filter-btn active" data-risk="alle" onclick="toggleRiskFilter('alle')">Alle Risiken</button>
  <button class="risk-filter-btn active" data-risk="niedrig" onclick="toggleRiskFilter('niedrig')">Risiko gering</button>
  <button class="risk-filter-btn active" data-risk="mittel" onclick="toggleRiskFilter('mittel')">Risiko mittel</button>
  <button class="risk-filter-btn active" data-risk="hoch" onclick="toggleRiskFilter('hoch')">Risiko hoch</button>
  <button class="risk-filter-btn" id="warning-filter-btn" onclick="toggleWarningFilter()">⚠ Warnung</button>
</div>
<div class="controls" style="margin-bottom:14px;">
  <input type="text" id="scanner-search" placeholder="🔍 Suche: Ticker, Name, Land, Sektor, ISIN..." oninput="renderScanner()"
         style="padding:7px 12px;border-radius:20px;border:1px solid var(--line);font-size:12px;width:280px;box-sizing:border-box;">
  <select id="land-filter" onchange="setLandFilter(this.value)" style="padding:6px 10px;border-radius:20px;border:1px solid var(--line);font-size:12px;margin-right:8px;margin-left:8px;">
    <option value="alle">Alle Länder</option>
  </select>
  <select id="sektor-filter" onchange="setSektorFilter(this.value)" style="padding:6px 10px;border-radius:20px;border:1px solid var(--line);font-size:12px;">
    <option value="alle">Alle Sektoren</option>
  </select>
</div>
<table id="scanner-tbl">
  <thead><tr>
    <th data-key="rank" title="Rang innerhalb der Risikostufe, sortiert nach Rendite">Rang ⇅</th>
    <th data-key="ticker" title="Boersenkuerzel">Ticker ⇅</th>
    <th data-key="name" title="Vollstaendiger Firmenname">Name ⇅</th>
    <th data-key="land" title="Land der Hauptbörse">Land ⇅</th>
    <th data-key="sektor" title="Wirtschaftssektor">Sektor ⇅</th>
    <th data-key="price" title="Aktueller Kurs in Originalwaehrung">Kurs ⇅</th>
    <th data-key="dividend_per_share" title="Aktuelle Dividende pro Anteil (absolut, nicht Prozent)">Dividende ⇅</th>
    <th data-key="dividend_yield_pct" title="Aktuelle jaehrliche Dividendenrendite">Rendite ⇅</th>
    <th data-key="perf_1m" title="Kursentwicklung letzter Monat">1M ⇅</th>
    <th data-key="perf_3m" title="Kursentwicklung letzte 3 Monate">3M ⇅</th>
    <th data-key="perf_1y" title="Kursentwicklung letztes Jahr">1J ⇅</th>
    <th data-key="next_payout" title="Naechster erwarteter Auszahlungstermin (geschaetzt, siehe Steckbrief fuer Details)">Nächste Auszahlung ⇅</th>
    <th data-key="risk_order" title="Gesamteinschaetzung des Risikos: niedrig/mittel/hoch, basierend auf Ausschuettungsquote und Verschuldung">Risiko ⇅</th>
    <th title="Position ins Dividendendepot uebernehmen"></th>
  </tr></thead>
  <tbody id="scanner-tbody"></tbody>
</table>

<div id="detail-panel" style="display:none;position:fixed;top:0;right:0;width:420px;height:100%;background:var(--bg);
     box-shadow:-4px 0 20px rgba(0,0,0,0.15);padding:24px;overflow-y:auto;z-index:100;">
  <div id="detail-content"></div>
</div>
<div id="detail-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
     background:rgba(0,0,0,0.3);z-index:99;" onclick="closeDetail()"></div>

<div id="depot-modal-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
     background:rgba(0,0,0,0.4);z-index:200;" onclick="closeDepotModal()"></div>
<div id="depot-modal" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
     background:var(--bg);border-radius:12px;padding:26px;width:360px;z-index:201;box-shadow:0 10px 40px rgba(0,0,0,0.25);">
  <h3 id="depot-modal-title" style="margin-top:0;color:var(--blue);"></h3>
  <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Anzahl Anteile</label>
  <input type="number" id="modal-anteile" step="0.0001" min="0" style="width:100%;padding:9px;margin-bottom:14px;border:1px solid var(--line);border-radius:6px;box-sizing:border-box;">
  <label id="modal-kaufpreis-label" style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Kaufpreis pro Anteil</label>
  <div style="display:flex;gap:6px;margin-bottom:14px;">
    <input type="number" id="modal-kaufpreis" step="0.01" min="0" style="flex:2;padding:9px;border:1px solid var(--line);border-radius:6px;box-sizing:border-box;">
    <select id="modal-waehrung" style="flex:1;padding:9px;border:1px solid var(--line);border-radius:6px;">
      <option value="EUR">EUR</option><option value="USD">USD</option><option value="GBP">GBP</option>
      <option value="CHF">CHF</option><option value="JPY">JPY</option><option value="HKD">HKD</option>
      <option value="SGD">SGD</option>
    </select>
  </div>
  <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Datum</label>
  <input type="date" id="modal-datum" style="width:100%;padding:9px;margin-bottom:14px;border:1px solid var(--line);border-radius:6px;box-sizing:border-box;">
  <label style="display:block;font-size:12px;color:var(--muted);margin-bottom:4px;">Kommission (in gewählter Währung)</label>
  <input type="number" id="modal-kommission" step="0.01" min="0" value="0" style="width:100%;padding:9px;margin-bottom:6px;border:1px solid var(--line);border-radius:6px;box-sizing:border-box;">
  <div style="font-size:11px;color:var(--muted);margin-bottom:16px;">Depot wird immer in EUR geführt - Umrechnung erfolgt automatisch (Näherungskurse).</div>
  <div style="display:flex;gap:8px;">
    <button class="action-btn" onclick="submitDepotModal()" style="flex:1;padding:10px;">Speichern</button>
    <button class="action-btn" onclick="closeDepotModal()" style="flex:1;padding:10px;">Abbrechen</button>
  </div>
</div>


</div>

<script>
const PYTHON_DEPOT = {depot_payload};
const SCANNER = {scanner_payload};
const DEPOT_KEY = 'dividendenatlas_depot';
const YELLOW_PRICE_DROP_PCT = {YELLOW_PRICE_DROP_PCT};
const MIN_YIELD_PCT = {MIN_YIELD_PCT};
const DECLINE_TREND_DAYS = {DECLINE_TREND_DAYS};

document.getElementById('stand').textContent = new Date().toLocaleString('de-DE');

function fmt(v, suffix) {{
  if (v === null || v === undefined || isNaN(v)) return '–';
  return Math.round(v * 100) / 100 + (suffix || '');
}}

// --- localStorage-Depotverwaltung, jetzt mit Transaktions-Historie ----
// Jede Position speichert eine LISTE von Kaeufen/Verkaeufen, statt nur
// einem festen Einstiegspreis - so lassen sich Zukaeufe und Teilverkaeufe
// erfassen. Anteile/Ø-Kaufkurs werden daraus jeweils live berechnet.
function getLocalDepot() {{
  try {{ return JSON.parse(localStorage.getItem(DEPOT_KEY) || '[]'); }}
  catch(e) {{ return []; }}
}}
function saveLocalDepot(list) {{ localStorage.setItem(DEPOT_KEY, JSON.stringify(list)); }}

// Wechselkurse zu EUR - ueber Open Exchange Rates taeglich live geholt
// (faellt auf Naeherungswerte zurueck, falls kein Key hinterlegt/Abruf
// fehlschlaegt). Depot wird IMMER in EUR gefuehrt.
const FX_RATES_TO_EUR = {fx_json};

function aggregateTransactions(transactions) {{
  let anteile = 0, kostenbasis = 0, erstesKaufdatum = null;
  transactions.forEach(t => {{
    if (t.type === 'buy') {{
      // Kommission wird der Kostenbasis zugeschlagen - realistischer
      // Ø-Kaufkurs inklusive Transaktionskosten.
      const gesamtkosten = t.anteile * t.kurs_eur + (t.kommission_eur || 0);
      anteile += t.anteile;
      kostenbasis += gesamtkosten;
      if (!erstesKaufdatum || t.datum < erstesKaufdatum) erstesKaufdatum = t.datum;
    }} else if (t.type === 'sell') {{
      // Durchschnittskosten-Methode: Verkauf reduziert Anteile UND
      // Kostenbasis anteilig, der Ø-Kaufkurs der verbleibenden Anteile
      // bleibt dadurch unveraendert.
      const avgKurs = anteile > 0 ? kostenbasis / anteile : 0;
      anteile -= t.anteile;
      kostenbasis -= t.anteile * avgKurs;
    }}
  }});
  return {{
    anteile: Math.round(anteile * 10000) / 10000,
    kaufkurs: anteile > 0 ? kostenbasis / anteile : 0,  // immer in EUR
    kaufdatum: erstesKaufdatum,
  }};
}}

// --- Ein einziges Formular-Fenster statt mehrerer Pop-ups -------------
let depotModalContext = null;

function openDepotModal(ticker, mode) {{
  depotModalContext = {{ticker, mode}};
  const titel = mode === 'sell' ? 'Teilverkauf: ' : mode === 'buymore' ? 'Zukauf: ' : 'Ins Depot aufnehmen: ';
  document.getElementById('depot-modal-title').textContent = titel + ticker;
  document.getElementById('modal-anteile').value = '';
  document.getElementById('modal-kaufpreis').value = '';
  document.getElementById('modal-waehrung').value = 'EUR';
  document.getElementById('modal-datum').value = new Date().toISOString().slice(0, 10);
  document.getElementById('modal-kommission').value = '0';
  document.getElementById('modal-kaufpreis-label').textContent = mode === 'sell' ? 'Verkaufspreis pro Anteil' : 'Kaufpreis pro Anteil';
  document.getElementById('depot-modal').style.display = 'block';
  document.getElementById('depot-modal-overlay').style.display = 'block';
  document.getElementById('modal-anteile').focus();
}}

function closeDepotModal() {{
  document.getElementById('depot-modal').style.display = 'none';
  document.getElementById('depot-modal-overlay').style.display = 'none';
  depotModalContext = null;
}}

function submitDepotModal() {{
  const anteile = parseFloat(document.getElementById('modal-anteile').value);
  const kaufpreis = parseFloat(document.getElementById('modal-kaufpreis').value);
  const waehrung = document.getElementById('modal-waehrung').value;
  const datum = document.getElementById('modal-datum').value;
  const kommission = parseFloat(document.getElementById('modal-kommission').value) || 0;
  if (!anteile || anteile <= 0 || !kaufpreis || kaufpreis <= 0 || !datum) {{
    alert('Bitte Anteile, Preis und Datum ausfuellen.');
    return;
  }}
  const rate = FX_RATES_TO_EUR[waehrung] || 1.0;
  const kurs_eur = kaufpreis * rate;
  const kommission_eur = kommission * rate;
  const {{ticker, mode}} = depotModalContext;
  const depot = getLocalDepot();
  let pos = depot.find(d => d.ticker === ticker);

  if (mode === 'sell') {{
    if (!pos) {{ alert('Diese Position wurde ueber my_depot.json angelegt - Verkaeufe dort bitte direkt in der Datei erfassen.'); closeDepotModal(); return; }}
    const agg = aggregateTransactions(pos.transactions);
    if (anteile > agg.anteile) {{ alert(`Nur ${{agg.anteile}} Anteile vorhanden.`); return; }}
    pos.transactions.push({{type: 'sell', anteile, kurs_eur, kommission_eur, datum}});
    if (aggregateTransactions(pos.transactions).anteile <= 0) {{
      saveLocalDepot(depot.filter(d => d.ticker !== ticker));
    }} else {{
      saveLocalDepot(depot);
    }}
  }} else {{
    if (pos) {{
      pos.transactions.push({{type: 'buy', anteile, kurs_eur, kommission_eur, datum}});
    }} else {{
      depot.push({{ticker, transactions: [{{type: 'buy', anteile, kurs_eur, kommission_eur, datum}}], next_payout_manual: null}});
    }}
    saveLocalDepot(depot);
  }}
  closeDepotModal();
  renderDepot();
}}

function addToDepot(ticker) {{ openDepotModal(ticker, 'add'); }}
function buyMore(ticker) {{ openDepotModal(ticker, 'buymore'); }}
function sellPartial(ticker) {{ openDepotModal(ticker, 'sell'); }}

function removeFromDepot(ticker) {{
  const depot = getLocalDepot().filter(d => d.ticker !== ticker);
  saveLocalDepot(depot);
  renderDepot();
}}

// --- JS-Portierung der Ampel-Logik (fuer Positionen, die im Browser
// hinzugefuegt wurden - Python kennt diese ja erst beim naechsten Scan) --
function isDecliningTrend(priceHistory) {{
  const days = DECLINE_TREND_DAYS;
  if (!priceHistory || priceHistory.length < days + 1) return {{declining: false, reason: 'noch nicht genug Historie'}};
  const window = priceHistory.slice(-(days + 1));
  const prices = window.map(p => p.price);
  if (prices[prices.length - 1] >= prices[0]) return {{declining: false, reason: null}};
  if (Math.max(...prices.slice(1)) > prices[0]) return {{declining: false, reason: null}};
  return {{declining: true, reason: `seit ${{days}} Laeufen ruecklaeufig (${{prices[0].toFixed(2)}} -> ${{prices[prices.length-1].toFixed(2)}})`}};
}}

function inferCurrency(ticker) {{
  if (ticker.endsWith('.L')) return 'GBP';
  if (ticker.endsWith('.SW')) return 'CHF';
  if (ticker.endsWith('.T')) return 'JPY';
  if (ticker.endsWith('.HK')) return 'HKD';
  if (ticker.endsWith('.SI')) return 'SGD';
  if (ticker.endsWith('.KS')) return 'KRW';
  if (ticker.endsWith('.SA')) return 'BRL';
  if (ticker.endsWith('.DE') || ticker.endsWith('.PA') || ticker.endsWith('.MC') || ticker.endsWith('.MI')) return 'EUR';
  return 'USD';
}}

function evaluateDepotPosition(agg, current) {{
  const kaufkurs = agg.kaufkurs;  // immer in EUR
  // WICHTIG: current.price kommt aus der Watchlist in der Original-
  // Waehrung (z.B. USD/GBP/JPY) - fuer einen korrekten Vergleich mit dem
  // EUR-Kaufkurs muss er erst umgerechnet werden, sonst waeren Kursentw.
  // und Rendite auf Kaufkurs falsch (unterschiedliche Waehrungen).
  const ccy = inferCurrency(current.ticker || '');
  const rate = FX_RATES_TO_EUR[ccy] || 1.0;
  const price = current.price !== null && current.price !== undefined ? current.price * rate : null;
  const payout = current.payout_ratio_pct;

  const gainPct = (price && kaufkurs) ? ((price - kaufkurs) / kaufkurs * 100) : null;
  let renditeAufKaufkurs = null;
  if (current.dividend_yield_pct !== null && current.dividend_yield_pct !== undefined && price && kaufkurs) {{
    const dividendPerShare = current.dividend_yield_pct / 100 * price;
    renditeAufKaufkurs = dividendPerShare / kaufkurs * 100;
  }}

  const trend = isDecliningTrend(current.price_history);

  const gradeInfo = (current.grades && current.grades.length > 0) ? current.grades[0] : null;
  let gradeDowngrade = false, gradeNote = null;
  if (gradeInfo && gradeInfo.action === 'downgrade') {{
    gradeDowngrade = true;
    gradeNote = `${{gradeInfo.firm || 'Analyst'}}: ${{gradeInfo.previous_grade}} -> ${{gradeInfo.new_grade}} (${{gradeInfo.date}})`;
  }}

  const redReasons = [];
  if (payout !== null && payout !== undefined && payout > 100) {{
    redReasons.push('Ausschuettungsquote > 100% (nicht mehr finanzierbar)');
  }}

  const yellowReasons = [];
  if (gainPct !== null && gainPct <= -YELLOW_PRICE_DROP_PCT) {{
    yellowReasons.push(`Kurs ${{Math.abs(gainPct).toFixed(1)}}% unter Kaufkurs`);
  }}
  if (trend.declining) yellowReasons.push(trend.reason);
  if (gradeDowngrade) yellowReasons.push('neue ruecklaeufige Analysteneinschaetzung: ' + gradeNote);

  let status = 'gruen';
  if (redReasons.length) status = 'rot';
  else if (yellowReasons.length) status = 'gelb';

  return {{gain_pct: gainPct, rendite_auf_kaufkurs: renditeAufKaufkurs, status, red_reasons: redReasons, yellow_reasons: yellowReasons}};
}}

function buildDepotView() {{
  const localDepot = getLocalDepot();
  // Python-Positionen (my_depot.json, altes Format ohne Transaktionen)
  // in eine synthetische Transaktion umwandeln, damit beide Quellen
  // einheitlich behandelt werden koennen. my_depot.json-Werte werden als
  // bereits in EUR angenommen (keine Waehrungsangabe dort vorgesehen).
  const pythonAsTransactions = PYTHON_DEPOT.map(d => ({{
    ticker: d.ticker, _source: 'python', current: d.current,
    next_payout_manual: d.next_payout_manual,
    transactions: [{{type: 'buy', anteile: d.anteile, kurs_eur: d.kaufkurs, kommission_eur: 0, datum: d.kaufdatum}}],
  }}));
  const localAsTransactions = localDepot.map(d => ({{...d, _source: 'local'}}));
  const combined = [...pythonAsTransactions, ...localAsTransactions];

  const scannerByTicker = {{}};
  SCANNER.forEach(s => {{ scannerByTicker[s.ticker] = s; }});

  return combined.map(pos => {{
    const current = pos.current || scannerByTicker[pos.ticker];
    if (!current) return null;
    const agg = aggregateTransactions(pos.transactions);
    if (agg.anteile <= 0) return null;
    const ev = evaluateDepotPosition(agg, current);
    return {{...pos, ...agg, current, depot_evaluation: ev}};
  }}).filter(Boolean);
}}

function renderDepot() {{
  const depotView = buildDepotView();
  const tbody = document.getElementById('depot-tbody');
  tbody.innerHTML = '';
  const counts = {{rot: 0, gelb: 0, gruen: 0}};
  let totalInvested = 0, totalDividendJahr = 0;

  depotView.forEach(d => {{
    const ev = d.depot_evaluation;
    const c = d.current;
    counts[ev.status]++;
    if (d.anteile && d.kaufkurs) {{
      totalInvested += d.anteile * d.kaufkurs;
      if (ev.rendite_auf_kaufkurs) totalDividendJahr += d.anteile * d.kaufkurs * ev.rendite_auf_kaufkurs / 100;
    }}
    const tr = document.createElement('tr');
    tr.className = ev.status;
    const reasons = [...ev.red_reasons, ...ev.yellow_reasons];
    const isLocal = d._source === 'local';
    const aktionen = isLocal
      ? `<button class="action-btn" onclick="buyMore('${{d.ticker}}')" title="Weitere Anteile kaufen">+ Zukauf</button>
         <button class="action-btn" onclick="sellPartial('${{d.ticker}}')" title="Teil verkaufen">− Verkauf</button>
         <button class="action-btn" onclick="removeFromDepot('${{d.ticker}}')" title="Komplett entfernen">🗑</button>`
      : `<span style="color:var(--muted);font-size:11px;">via my_depot.json</span>`;
    const ccy = inferCurrency(d.ticker);
    const rate = FX_RATES_TO_EUR[ccy] || 1.0;
    const preisEur = (c.price !== null && c.price !== undefined) ? c.price * rate : null;
    tr.innerHTML = `
      <td><span class="ampel ${{ev.status}}"></span></td>
      <td><strong>${{d.ticker}}</strong></td>
      <td>${{c.name || ''}}</td>
      <td>${{d.anteile}}</td>
      <td>${{fmt(d.kaufkurs, ' €')}}</td>
      <td>${{d.kaufdatum || '–'}}</td>
      <td>${{fmt(preisEur, ' €')}}${{ccy !== 'EUR' && preisEur !== null ? `<br><span style="color:var(--muted);font-size:10px;">(${{fmt(c.price)}} ${{ccy}})</span>` : ''}}</td>
      <td class="yield">${{fmt(ev.gain_pct, '%')}}</td>
      <td class="yield">${{fmt(ev.rendite_auf_kaufkurs, '%')}}</td>
      <td>${{fmt(c.dividend_yield_pct, '%')}}</td>
      <td>${{fmt(c.payout_ratio_pct, '%')}}</td>
      <td>${{c.next_payout || d.next_payout_manual || '–'}}</td>
      <td>${{reasons.map(r => `<span class="badge ${{ev.status}}">${{r}}</span>`).join('')}}</td>
      <td>${{aktionen}}</td>`;
    tbody.appendChild(tr);
  }});

  document.getElementById('depot-count').textContent = depotView.length;
  const avgYield = totalInvested > 0 ? (totalDividendJahr / totalInvested * 100) : 0;
  document.getElementById('depot-avg-yield').textContent = avgYield.toFixed(2) + '%';
  ['rot', 'gelb', 'gruen'].forEach(s => {{
    const card = document.querySelectorAll('.summary-card.' + s + ' .big')[0];
    if (card) card.textContent = counts[s];
  }});
}}

function perfCell(v) {{
  if (v === null || v === undefined) return '<td>–</td>';
  const cls = v >= 0 ? 'perf-pos' : 'perf-neg';
  return `<td class="${{cls}}">${{v >= 0 ? '+' : ''}}${{fmt(v)}}%</td>`;
}}

function sentimentBadge(score) {{
  if (score === null || score === undefined) return '<span class="badge">n/a</span>';
  if (score >= 0.15) return `<span class="badge gruen">positiv (${{score.toFixed(2)}})</span>`;
  if (score <= -0.15) return `<span class="badge rot">negativ (${{score.toFixed(2)}})</span>`;
  return `<span class="badge orange">neutral (${{score.toFixed(2)}})</span>`;
}}

function buildTrapExplanation(r) {{
  const payout = r.payout_ratio_pct, yield_ = r.dividend_yield_pct;
  const gruende = [];
  if (payout !== null && payout !== undefined && payout > 100) {{
    gruende.push(`Die Ausschüttungsquote liegt bei <strong>${{fmt(payout,'%')}}</strong> - das Unternehmen zahlt also
      mehr an Dividende aus, als es im gleichen Zeitraum an Gewinn erwirtschaftet hat. Das ist nur kurzfristig
      möglich: aus Rücklagen, durch Verkauf von Vermögenswerten, oder indem neue Schulden aufgenommen werden, um die
      Dividende zu finanzieren. Keine dieser drei Optionen ist auf Dauer tragfähig. Historisch geht einer
      Ausschüttungsquote &gt;100% über mehrere Quartale hinweg häufig eine Dividendenkürzung voraus.`);
  }}
  if (yield_ !== null && yield_ !== undefined && yield_ > 12) {{
    gruende.push(`Die Dividendenrendite von <strong>${{fmt(yield_,'%')}}</strong> ist außergewöhnlich hoch - deutlich über
      dem, was gesunde, etablierte Dividendenzahler üblicherweise bieten (meist 2-6%). Eine derart hohe Rendite
      entsteht in den seltensten Fällen, weil ein Unternehmen besonders großzügig ist. Viel häufiger ist die Ursache
      ein stark gefallener Aktienkurs - die Rendite (Dividende ÷ Kurs) steigt automatisch, wenn der Kurs fällt, auch
      wenn die Dividende selbst unverändert bleibt. Ein solcher Kursverfall ist oft ein Signal, dass der Markt bereits
      eine bevorstehende Kürzung oder ernsthafte Geschäftsprobleme einpreist.`);
  }}
  if (gruende.length === 0) {{
    gruende.push(`Diese Aktie erfüllt eines der beiden automatischen Warnkriterien (Ausschüttungsquote &gt;100% oder
      Rendite &gt;12%) - die genauen aktuellen Werte findest du in der Tabelle oben.`);
  }}
  return `<div style="background:#FEF2F2;border-left:3px solid var(--red);padding:10px 14px;margin:10px 0;border-radius:4px;">
    <strong style="color:var(--red);">⚠ Warum diese Aktie eine Dividenden-Fallen-Warnung erhalten hat</strong>
    <ul style="margin:8px 0 4px 18px;padding:0;font-size:12px;line-height:1.5;">
      ${{gruende.map(g => `<li style="margin-bottom:8px;">${{g}}</li>`).join('')}}
    </ul>
    <p style="font-size:11px;color:var(--muted);margin-top:6px;">Das heißt nicht zwangsläufig, dass die Dividende
      bald gekürzt wird - aber das Risiko dafür ist spürbar erhöht. Prüf unbedingt die aktuellen Geschäftszahlen und
      News, bevor du auf Basis der Rendite allein investierst.</p>
  </div>`;
}}

function buildChartSection(r) {{
  const series = r.chart_series;
  if (!series || series.length < 2) {{
    return `<h3 style="margin-top:18px;">Kursverlauf</h3>
      <p style="font-size:12px;color:var(--muted);">Noch kein Kursverlauf verfuegbar - erscheint, sobald eine unserer Kursquellen (Yahoo/FMP/Twelve Data/Stooq) fuer diese Aktie historische Daten liefert.</p>`;
  }}
  const prices = series.map(p => p[1]);
  const min = Math.min(...prices), max = Math.max(...prices);
  const w = 380, h = 130, pad = 6;
  const range = (max - min) || 1;
  const points = series.map((p, i) => {{
    const x = pad + (i / (series.length - 1)) * (w - 2 * pad);
    const y = h - pad - ((p[1] - min) / range) * (h - 2 * pad);
    return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
  }}).join(' ');
  const first = series[0][1], last = series[series.length - 1][1];
  const trendColor = last >= first ? 'var(--green)' : 'var(--red)';
  return `<h3 style="margin-top:18px;">Kursverlauf (${{series[0][0]}} bis ${{series[series.length-1][0]}})</h3>
    <svg viewBox="0 0 ${{w}} ${{h}}" style="width:100%;height:110px;background:var(--bgSoft, #f7f7f9);border-radius:6px;">
      <polyline points="${{points}}" fill="none" stroke="${{trendColor}}" stroke-width="2"/>
    </svg>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-top:2px;">
      <span>${{min.toFixed(2)}}</span><span>${{max.toFixed(2)}}</span>
    </div>`;
}}

function buildRiskBreakdown(r) {{
  const ev = r.evaluation;
  const payout = r.payout_ratio_pct, debt = r.debt_to_equity, yield_ = r.dividend_yield_pct;
  const niedrigMax = ev.payout_niedrig_max, mittelMax = ev.payout_mittel_max;
  const row = (label, wert, schwelle, bestanden) => `
    <tr>
      <td>${{label}}</td>
      <td>${{wert}}</td>
      <td style="color:var(--muted);">${{schwelle}}</td>
      <td style="text-align:center;">${{bestanden === null ? '–' : (bestanden ? '<span style="color:var(--green);">✓</span>' : '<span style="color:var(--red);">✗</span>')}}</td>
    </tr>`;
  const payoutOk1 = payout !== null && payout !== undefined ? payout <= niedrigMax : null;
  const payoutOk2 = payout !== null && payout !== undefined ? payout <= mittelMax : null;
  const debtOk1 = (debt === null || debt === undefined) ? null : debt <= 1.0;
  const debtOk2 = (debt === null || debt === undefined) ? null : debt <= 2.0;
  const yieldOk = yield_ !== null && yield_ !== undefined ? yield_ <= 12 : null;
  return `
    <p style="font-size:11px;color:var(--muted);font-style:italic;">
      Schwellenwerte orientiert an mehreren unabhaengigen Ratgebern (u.a. finanzen.net, ING, Investing.com Academy) -
      Ausschuettungsquote ≤${{niedrigMax}}% gilt als solide, ≤${{mittelMax}}% als noch akzeptabel, darueber als Warnsignal.
      ${{ev.is_reit ? '<br><strong>Hinweis:</strong> Immobiliensektor (REIT-artig) - hier gelten hoehere Schwellen, da REITs strukturell/gesetzlich einen Grossteil ihres Gewinns ausschuetten muessen; eine hohe Quote ist hier normal, kein Warnsignal.' : ''}}
      Fuer die Verschuldung (D/E) gibt es keine einheitliche Broker-Vorgabe - die Werte hier (≤1,0 / ≤2,0) sind eine allgemeine Bilanzkennzahl-Faustregel.
    </p>
    <table style="width:100%;font-size:12px;">
      <thead><tr><td><strong>Kriterium</strong></td><td><strong>Wert</strong></td><td><strong>Schwelle (niedrig / mittel)</strong></td><td style="text-align:center;"><strong>Solide?</strong></td></tr></thead>
      ${{row('Ausschuettungsquote', fmt(payout, '%'), `≤${{niedrigMax}}% / ≤${{mittelMax}}%`, payoutOk1 === null ? null : payoutOk1)}}
      ${{payout !== null && payout !== undefined && !payoutOk1 ? row('&nbsp;&nbsp;↳ noch im mittleren Bereich?', '', '', payoutOk2) : ''}}
      ${{row('Verschuldung (D/E)', fmt(debt), '≤1,0 / ≤2,0', debtOk1 === null ? null : debtOk1)}}
      ${{debt !== null && debt !== undefined && !debtOk1 ? row('&nbsp;&nbsp;↳ noch im mittleren Bereich?', '', '', debtOk2) : ''}}
      ${{row('Dividendenrendite', fmt(yield_, '%'), '≤12% (darueber: Dividenden-Fallen-Verdacht)', yieldOk)}}
      ${{row('Ausschuettungsquote >100%?', payout !== null && payout !== undefined ? (payout > 100 ? 'ja' : 'nein') : '–', 'nicht ueberschritten', payout !== null && payout !== undefined ? payout <= 100 : null)}}
      ${{row('Analysten: mehr Down- als Upgrades (letzte 3)?', ev.analyst_downgrade_warning ? 'ja' : 'nein', 'nein', !ev.analyst_downgrade_warning)}}
      ${{row('News-Sentiment deutlich negativ?', ev.sentiment_warning ? 'ja' : 'nein', 'nein', !ev.sentiment_warning)}}
    </table>
    <p style="font-size:11px;color:var(--muted);margin-top:6px;">
      Ergebnis: Ausschuettungsquote und Verschuldung bestimmen die Basis-Einstufung (niedrig/mittel/hoch).
      Eine extreme Ausschuettungsquote (>100%) oder Rendite (>12%) hebt die Einstufung direkt auf 'hoch' (Dividenden-Fallen-Muster).
      Ueberwiegend negative Analysten-Einschaetzungen oder eine deutlich negative News-Lage heben die Einstufung zusaetzlich je eine Stufe an.
    </p>`;
}}

function buildGradesSection(grades) {{
  if (!grades || grades.length === 0) {{
    return '<h3 style="margin-top:20px;">Analysten-Einschätzungen</h3><p style="font-size:12px;color:var(--muted);">Keine aktuellen Einschätzungen verfügbar.</p>';
  }}
  const actionColor = a => a === 'downgrade' ? 'var(--red)' : a === 'upgrade' ? 'var(--green)' : 'var(--muted)';
  const actionLabel = a => a === 'downgrade' ? 'Herabstufung' : a === 'upgrade' ? 'Hochstufung' : (a || 'Bestätigung');
  const items = grades.map(g => `
    <div style="padding:8px 0;border-bottom:1px solid var(--line);font-size:12px;">
      <strong>${{g.firm || 'Analyst'}}</strong>
      <span style="color:${{actionColor(g.action)}};font-weight:600;"> · ${{actionLabel(g.action)}}</span>
      <div style="color:var(--muted);margin-top:2px;">${{g.previous_grade ? `${{g.previous_grade}} → ${{g.new_grade || '?'}}` : (g.new_grade || '?')}} <span style="font-size:10px;">(${{g.date || ''}})</span></div>
    </div>`).join('');
  return `<h3 style="margin-top:20px;">Analysten-Einschätzungen (letzte ${{grades.length}})</h3>
    <p style="font-size:11px;color:var(--muted);font-style:italic;margin-top:-6px;">Keine eigene Kauf-/Verkaufsempfehlung - nur Wiedergabe, was Analysten zuletzt eingeschätzt haben.</p>
    ${{items}}`;
}}

function buildNewsSection(news) {{
  if (!news || !news.articles || news.articles.length === 0) {{
    return '<h3 style="margin-top:20px;">Aktuelle News</h3><p style="font-size:12px;color:var(--muted);">Keine aktuellen News verfuegbar.</p>';
  }}
  const avgBadge = news.avg_sentiment !== null && news.avg_sentiment !== undefined
    ? `<span style="font-size:12px;color:var(--muted);">Ø-Sentiment: ${{sentimentBadge(news.avg_sentiment)}}</span>` : '';
  const items = news.articles.map(a => `
    <div style="padding:8px 0;border-bottom:1px solid var(--line);">
      <a href="${{a.url || '#'}}" target="_blank" rel="noopener" style="font-size:12px;color:var(--blue);font-weight:600;text-decoration:none;">${{a.title || '(kein Titel)'}}</a>
      <div style="margin-top:3px;">${{sentimentBadge(a.sentiment_score)}} <span style="color:var(--muted);font-size:10px;">${{a.published || ''}}</span></div>
    </div>`).join('');
  return `<h3 style="margin-top:20px;">Aktuelle News ${{avgBadge}}</h3>
    <p style="font-size:11px;color:var(--muted);font-style:italic;margin-top:-6px;">Sentiment via VADER (echte lexikonbasierte Analyse der Schlagzeile, kein Keyword-Abgleich).</p>
    ${{items}}`;
}}

function buildSWOT(r) {{
  const staerken = [], schwaechen = [], chancen = [], risiken = [];
  const yield_ = r.dividend_yield_pct, payout = r.payout_ratio_pct, debt = r.debt_to_equity;

  // Staerken/Schwaechen: aus den Kennzahlen selbst abgeleitet, keine
  // erfundenen Aussagen zu Marke/Management/Wettbewerb - nur das, was
  // die Zahlen tatsaechlich hergeben.
  if (payout !== null && payout !== undefined) {{
    if (payout <= 50) staerken.push(`Niedrige Ausschuettungsquote (${{payout.toFixed(1)}}%) - viel Spielraum, auch in schwaecheren Jahren die Dividende zu halten`);
    else if (payout <= 85) staerken.push(`Ausschuettungsquote (${{payout.toFixed(1)}}%) noch im ueblichen Rahmen`);
    else if (payout <= 100) schwaechen.push(`Hohe Ausschuettungsquote (${{payout.toFixed(1)}}%) - wenig Puffer bei Gewinnrueckgang`);
    else schwaechen.push(`Ausschuettungsquote ueber 100% (${{payout.toFixed(1)}}%) - es wird mehr ausgeschuettet als verdient, nicht dauerhaft durchhaltbar`);
  }} else {{
    schwaechen.push('Ausschuettungsquote nicht bekannt - Nachhaltigkeit der Dividende laesst sich nicht einschaetzen');
  }}

  if (debt !== null && debt !== undefined) {{
    if (debt <= 1.0) staerken.push(`Niedrige Verschuldung (D/E ${{debt.toFixed(2)}}) - finanziell solide aufgestellt`);
    else if (debt <= 2.0) staerken.push(`Verschuldung (D/E ${{debt.toFixed(2)}}) im akzeptablen Rahmen`);
    else schwaechen.push(`Hohe Verschuldung (D/E ${{debt.toFixed(2)}}) - anfaelliger bei steigenden Zinsen oder Gewinneinbruch`);
  }} else {{
    schwaechen.push('Verschuldungsgrad nicht bekannt');
  }}

  if (r.perf_1y !== null && r.perf_1y !== undefined) {{
    if (r.perf_1y > 0) staerken.push(`Kurs im letzten Jahr um ${{r.perf_1y.toFixed(1)}}% gestiegen`);
    else schwaechen.push(`Kurs im letzten Jahr um ${{Math.abs(r.perf_1y).toFixed(1)}}% gefallen`);
  }}

  // Chancen/Risiken: aus der Kombination der Werte und dem Risiko-Modell
  if (yield_ !== null && yield_ !== undefined && yield_ >= MIN_YIELD_PCT && r.evaluation.risk !== 'hoch') {{
    chancen.push(`Solide Rendite (${{yield_.toFixed(2)}}%) bei vertretbarem Risiko - passt zum Renditeziel, ohne offensichtliche Warnsignale`);
  }}
  if (r.evaluation.trap_warning) {{
    risiken.push('Kombination aus sehr hoher Rendite und hoher/unplausibler Ausschuettungsquote - typisches Muster einer Dividenden-Kuerzung in der Zukunft');
  }}
  if (r.data_source === 'manuell') {{
    risiken.push('Kein Live-Kurs verfuegbar - diese Einschaetzung basiert auf einer einmaligen Recherche, nicht auf aktuellen Zahlen');
  }}
  if (r.perf_1m !== null && r.perf_1m !== undefined && r.perf_1m < -10) {{
    risiken.push(`Deutlicher Kursrueckgang im letzten Monat (${{r.perf_1m.toFixed(1)}}%) - koennte auf ein aktuelles Problem hindeuten, das noch nicht in Ausschuettungsquote/Verschuldung sichtbar ist`);
  }}
  if (chancen.length === 0) chancen.push('Keine besondere Chance ueber die Basis-Rendite hinaus erkennbar aus den vorliegenden Zahlen');
  if (risiken.length === 0) risiken.push('Keine ueber die Risikostufe hinausgehenden Auffaelligkeiten in den vorliegenden Zahlen');

  return {{staerken, schwaechen, chancen, risiken}};
}}

function swotList(items) {{
  return '<ul style="margin:4px 0;padding-left:18px;font-size:12px;">' + items.map(i => `<li>${{i}}</li>`).join('') + '</ul>';
}}

function showDetail(ticker) {{
  const r = SCANNER.find(s => s.ticker === ticker);
  if (!r) return;
  const ev = r.evaluation;
  const swot = buildSWOT(r);
  document.getElementById('detail-content').innerHTML = `
    <span class="close-btn" onclick="closeDetail()">×</span>
    <h3>${{r.name || r.ticker}} (${{r.ticker}})</h3>
    <table>
      <tr><td>ISIN</td><td style="font-family:monospace;">${{r.isin || 'nicht verfuegbar'}}</td></tr>
      <tr><td>Land</td><td>${{r.land}}</td></tr>
      <tr><td>Sektor</td><td>${{r.sektor}}</td></tr>
      <tr><td>Kurs</td><td>${{fmt(r.price, ' $')}}</td></tr>
      <tr><td>Marktkapitalisierung</td><td>${{r.market_cap ? (r.market_cap/1e9).toFixed(1) + ' Mrd. $' : '–'}}
        <div style="font-size:11px;color:var(--muted);font-weight:normal;margin-top:2px;">Groesse des Unternehmens an der Boerse. Groesser (z.B. &gt;10 Mrd.) heisst meist stabiler und liquider handelbar; kleiner (&lt;2 Mrd., "Small Cap") heisst potenziell mehr Wachstum, aber auch mehr Kursschwankung und Risiko.</div></td></tr>
      <tr><td>KGV</td><td>${{fmt(r.pe_ratio)}}
        <div style="font-size:11px;color:var(--muted);font-weight:normal;margin-top:2px;">Kurs-Gewinn-Verhaeltnis: wie viele Jahresgewinne der aktuelle Kurs kostet. Niedrig (&lt;12) kann auf eine guenstige Bewertung ODER auf Zweifel des Marktes an der Zukunft hindeuten; hoch (&gt;25) auf hohe Wachstumserwartungen ODER eine teure Bewertung. Kein Wert fuer sich allein ist "gut" oder "schlecht" - immer im Sektor-Vergleich einordnen.</div></td></tr>
      <tr><td>Dividendenrendite</td><td>${{fmt(r.dividend_yield_pct, '%')}}</td></tr>
      <tr><td>Ausschuettungsquote</td><td>${{fmt(r.payout_ratio_pct, '%')}}</td></tr>
      <tr><td>Nächste erwartete Auszahlung</td><td>${{r.next_payout || 'nicht verfuegbar'}}</td></tr>
      ${{(r.debt_to_equity !== null && r.debt_to_equity !== undefined) ? `<tr><td>Verschuldung (D/E)</td><td>${{fmt(r.debt_to_equity)}}</td></tr>` : ''}}
      <tr><td>Kursentw. 1 Monat</td><td>${{fmt(r.perf_1m, '%')}}</td></tr>
      <tr><td>Kursentw. 3 Monate</td><td>${{fmt(r.perf_3m, '%')}}</td></tr>
      <tr><td>Kursentw. 1 Jahr</td><td>${{fmt(r.perf_1y, '%')}}</td></tr>
      <tr><td>Datenquelle</td><td>${{r.data_source === 'twelvedata' ? 'Twelve Data' : r.data_source === 'finnhub' ? 'Finnhub' : r.data_source === 'alphavantage' ? 'Alpha Vantage' : r.data_source === 'manuell' ? 'Manuell recherchiert' : 'FMP'}}</td></tr>
      ${{r.manual_source_note ? `<tr><td>Recherche-Quelle</td><td>${{r.manual_source_note}}</td></tr><tr><td colspan="2" style="color:var(--red);font-size:11px;">⚠ Kein Live-Kurs verfuegbar - Werte koennen veraltet sein</td></tr>` : ''}}
    </table>
    ${{buildChartSection(r)}}

    <h3 style="margin-top:18px;">Risiko-Einstufung: <span class="badge ${{ev.risk}}">${{ev.risk}}</span></h3>
    ${{buildRiskBreakdown(r)}}
    ${{ev.trap_warning ? buildTrapExplanation(r) : ''}}
    ${{ev.sentiment_warning ? '<p style="color:var(--red);"><strong>⚠ Negative News-Lage</strong> - aktuelle Schlagzeilen sind im Schnitt deutlich negativ (Sentiment-Analyse), Risiko deshalb automatisch hochgestuft.</p>' : ''}}
    ${{ev.analyst_downgrade_warning ? '<p style="color:var(--red);"><strong>⚠ Ueberwiegend Herabstufungen</strong> - unter den letzten Analysten-Einschaetzungen gibt es mehr Downgrades als Upgrades, Risiko deshalb automatisch hochgestuft.</p>' : ''}}

    ${{buildGradesSection(r.grades)}}
    ${{buildNewsSection(r.news)}}

    <h3 style="margin-top:20px;">SWOT-Analyse (datenbasiert)</h3>
    <p style="font-size:11px;color:var(--muted);font-style:italic;margin-top:-6px;">Abgeleitet ausschliesslich aus den obigen Kennzahlen - keine qualitativen Aussagen zu Geschaeftsmodell/Management, die wir nicht belegen koennen.</p>
    <div style="background:var(--greenSoft);border-left:3px solid var(--green);padding:8px 12px;margin-top:8px;border-radius:4px;">
      <strong style="color:var(--green);font-size:12px;">Staerken</strong>${{swotList(swot.staerken)}}
    </div>
    <div style="background:var(--redSoft);border-left:3px solid var(--red);padding:8px 12px;margin-top:8px;border-radius:4px;">
      <strong style="color:var(--red);font-size:12px;">Schwaechen</strong>${{swotList(swot.schwaechen)}}
    </div>
    <div style="background:var(--blueSoft);border-left:3px solid var(--blue);padding:8px 12px;margin-top:8px;border-radius:4px;">
      <strong style="color:var(--blue);font-size:12px;">Chancen</strong>${{swotList(swot.chancen)}}
    </div>
    <div style="background:var(--yellowSoft);border-left:3px solid var(--yellow);padding:8px 12px;margin-top:8px;border-radius:4px;">
      <strong style="color:var(--yellow);font-size:12px;">Risiken</strong>${{swotList(swot.risiken)}}
    </div>

    <button class="action-btn" style="margin-top:16px;" onclick="addToDepot('${{r.ticker}}'); closeDetail();">→ ins Dividendendepot</button>
  `;
  document.getElementById('detail-panel').style.display = 'block';
  document.getElementById('detail-overlay').style.display = 'block';
}}

function closeDetail() {{
  document.getElementById('detail-panel').style.display = 'none';
  document.getElementById('detail-overlay').style.display = 'none';
}}

let riskFilterSet = new Set(['niedrig', 'mittel', 'hoch']);
let onlyWarnings = false;
let landFilter = 'alle', sektorFilter = 'alle';
let scannerSortKey = 'rank', scannerSortDir = 1;
const RISK_ORDER_JS = {{niedrig: 0, mittel: 1, hoch: 2}};

// --- Manuelle Ueberschreibungen fuer Watchlist-Felder -----------------
// Jedes Feld (Rendite, Ausschuett.-Quote, Verschuldung, Kurs, 1M/3M/1J)
// laesst sich direkt anklicken und von Hand eintragen/korrigieren - z.B.
// wenn eine Quelle fehlt oder du selbst nachgeprueft hast. Wird pro
// Ticker in localStorage gespeichert, Risiko/Qualifikation werden danach
// automatisch neu berechnet.
const SCANNER_OVERRIDES_KEY = 'dividendenatlas_scanner_overrides';
const EDITABLE_FIELDS = {{
  dividend_yield_pct: {{suffix: '%', decimals: 2}},
  payout_ratio_pct: {{suffix: '%', decimals: 1}},
  debt_to_equity: {{suffix: '', decimals: 2}},
  price: {{suffix: '', decimals: 2}},
  dividend_per_share: {{suffix: '', decimals: 2}},
  perf_1m: {{suffix: '%', decimals: 1}},
  perf_3m: {{suffix: '%', decimals: 1}},
  perf_1y: {{suffix: '%', decimals: 1}},
}};

function getScannerOverrides() {{
  try {{ return JSON.parse(localStorage.getItem(SCANNER_OVERRIDES_KEY) || '{{}}'); }}
  catch(e) {{ return {{}}; }}
}}
function saveScannerOverrides(obj) {{ localStorage.setItem(SCANNER_OVERRIDES_KEY, JSON.stringify(obj)); }}

function applyScannerOverrides(baseData) {{
  const overrides = getScannerOverrides();
  return baseData.map(r => {{
    const ov = overrides[r.ticker] || {{}};
    const merged = {{...r, ...ov}};
    // Dividende pro Anteil automatisch aus Kurs * Rendite herleiten,
    // FALLS nicht manuell ueberschrieben und beide Werte bekannt sind.
    if (merged.dividend_per_share === undefined || merged.dividend_per_share === null) {{
      if (merged.price && merged.dividend_yield_pct !== null && merged.dividend_yield_pct !== undefined) {{
        merged.dividend_per_share = merged.price * merged.dividend_yield_pct / 100;
        merged._dividendComputed = true;
      }}
    }}
    return {{...merged, _overriddenFields: Object.keys(ov)}};
  }});
}}

const PAYOUT_NIEDRIG_MAX = 60.0, PAYOUT_MITTEL_MAX = 80.0;
const PAYOUT_NIEDRIG_MAX_REIT = 75.0, PAYOUT_MITTEL_MAX_REIT = 95.0;
const REIT_SEKTOREN_JS = new Set(['Immobilien']);

function evaluateScannerCandidateJS(f) {{
  const payout = f.payout_ratio_pct, yield_ = f.dividend_yield_pct, debt = f.debt_to_equity;
  const isReit = REIT_SEKTOREN_JS.has(f.sektor);
  const niedrigMax = isReit ? PAYOUT_NIEDRIG_MAX_REIT : PAYOUT_NIEDRIG_MAX;
  const mittelMax = isReit ? PAYOUT_MITTEL_MAX_REIT : PAYOUT_MITTEL_MAX;
  const trapWarning = (payout !== null && payout !== undefined && payout > 100) ||
                      (yield_ !== null && yield_ !== undefined && yield_ > 12);
  let risk;
  if (trapWarning) risk = 'hoch';
  else if (payout !== null && payout !== undefined && payout <= niedrigMax && (debt === null || debt === undefined || debt <= 1.0)) risk = 'niedrig';
  else if (payout !== null && payout !== undefined && payout <= mittelMax && (debt === null || debt === undefined || debt <= 2.0)) risk = 'mittel';
  else risk = 'hoch';
  return {{
    trap_warning: trapWarning, risk, is_reit: isReit,
    payout_niedrig_max: niedrigMax, payout_mittel_max: mittelMax,
    meets_min_yield: yield_ !== null && yield_ !== undefined && yield_ >= MIN_YIELD_PCT,
  }};
}}

function editableCell(ticker, field, value) {{
  const cfg = EDITABLE_FIELDS[field];
  const display = (value === null || value === undefined || isNaN(value)) ? '–' :
    (Math.round(value * Math.pow(10, cfg.decimals)) / Math.pow(10, cfg.decimals)) + cfg.suffix;
  return `<span class="editable-value" onclick="startEditCell(event, this, '${{ticker}}', '${{field}}')">${{display}}</span>`;
}}

function startEditCell(event, spanEl, ticker, field) {{
  event.stopPropagation();
  const overrides = getScannerOverrides();
  const current = (overrides[ticker] && overrides[ticker][field] !== undefined) ? overrides[ticker][field] : '';
  const input = document.createElement('input');
  input.type = 'number'; input.step = '0.01'; input.value = current;
  input.style.width = '68px'; input.style.padding = '3px';
  input.onclick = (e) => e.stopPropagation();
  input.onblur = () => saveEditCell(input, ticker, field);
  input.onkeydown = (e) => {{ if (e.key === 'Enter') input.blur(); }};
  spanEl.replaceWith(input);
  input.focus(); input.select();
}}

function getEffectivePrice(ticker) {{
  const base = SCANNER.find(s => s.ticker === ticker);
  const overrides = getScannerOverrides();
  const ov = overrides[ticker] || {{}};
  const p = (ov.price !== undefined) ? ov.price : (base ? base.price : null);
  return (p === null || p === undefined || p === 0) ? null : p;
}}

function saveEditCell(input, ticker, field) {{
  const val = input.value.trim();
  const overrides = getScannerOverrides();
  if (!overrides[ticker]) overrides[ticker] = {{}};
  if (val === '') {{
    delete overrides[ticker][field];
  }} else {{
    const num = parseFloat(val);
    overrides[ticker][field] = num;
    // Bidirektionale Kopplung: Dividende (absolut) <-> Rendite (%),
    // ueber den aktuell gueltigen Kurs (Original oder selbst ueberschrieben).
    const price = getEffectivePrice(ticker);
    if (price) {{
      if (field === 'dividend_per_share') {{
        overrides[ticker]['dividend_yield_pct'] = Math.round(num / price * 100 * 100) / 100;
      }} else if (field === 'dividend_yield_pct') {{
        overrides[ticker]['dividend_per_share'] = Math.round(price * num / 100 * 100) / 100;
      }}
    }}
  }}
  if (Object.keys(overrides[ticker]).length === 0) delete overrides[ticker];
  saveScannerOverrides(overrides);
  renderScanner();
}}

function resetScannerOverrides(ticker) {{
  const overrides = getScannerOverrides();
  delete overrides[ticker];
  saveScannerOverrides(overrides);
  renderScanner();
}}

function toggleRiskFilter(risk) {{
  if (risk === 'alle') {{
    riskFilterSet = new Set(['niedrig', 'mittel', 'hoch']);
  }} else if (riskFilterSet.size === 3) {{
    // Bisher war 'Alle Risiken' aktiv - der erste Klick auf einen
    // bestimmten Filter grenzt auf GENAU diesen einen ein, statt ihn nur
    // aus der Vollauswahl zu entfernen.
    riskFilterSet = new Set([risk]);
  }} else {{
    // Schon eingegrenzt - ein weiterer Klick ADDIERT sich zur Auswahl
    // dazu (bzw. entfernt genau dieses eine Risiko wieder, falls es schon
    // ausgewaehlt war).
    if (riskFilterSet.has(risk)) riskFilterSet.delete(risk); else riskFilterSet.add(risk);
    if (riskFilterSet.size === 0) riskFilterSet = new Set(['niedrig', 'mittel', 'hoch']); // nie komplett leer
  }}
  document.querySelectorAll('.risk-filter-btn[data-risk]').forEach(btn => {{
    const r = btn.dataset.risk;
    btn.classList.toggle('active', r === 'alle' ? riskFilterSet.size === 3 : riskFilterSet.has(r));
  }});
  renderScanner();
}}

function toggleWarningFilter() {{
  onlyWarnings = !onlyWarnings;
  document.getElementById('warning-filter-btn').classList.toggle('active', onlyWarnings);
  renderScanner();
}}

function setLandFilter(land) {{ landFilter = land; renderScanner(); }}
function setSektorFilter(sektor) {{ sektorFilter = sektor; renderScanner(); }}

function populateFilterDropdowns() {{
  const laender = [...new Set(SCANNER.map(r => r.land))].sort();
  const sektoren = [...new Set(SCANNER.map(r => r.sektor))].sort();
  const landSel = document.getElementById('land-filter');
  laender.forEach(l => {{ const o = document.createElement('option'); o.value = l; o.textContent = l; landSel.appendChild(o); }});
  const sektorSel = document.getElementById('sektor-filter');
  sektoren.forEach(s => {{ const o = document.createElement('option'); o.value = s; o.textContent = s; sektorSel.appendChild(o); }});
}}

function sortValue(r, key) {{
  if (key === 'risk_order') return RISK_ORDER_JS[r.evaluation.risk];
  if (key === 'payout_ratio_pct' || key === 'debt_to_equity' || key === 'perf_1m' || key === 'perf_3m' || key === 'perf_1y') {{
    const v = r[key];
    return (v === null || v === undefined) ? -Infinity : v;
  }}
  return r[key];
}}

function editableCellColored(ticker, field, value) {{
  const cfg = EDITABLE_FIELDS[field];
  const cls = (value !== null && value !== undefined && !isNaN(value))
    ? (value >= 0 ? 'perf-pos' : 'perf-neg') : '';
  const display = (value === null || value === undefined || isNaN(value)) ? '–' :
    (value >= 0 ? '+' : '') + (Math.round(value * Math.pow(10, cfg.decimals)) / Math.pow(10, cfg.decimals)) + cfg.suffix;
  return `<span class="editable-value ${{cls}}" onclick="startEditCell(event, this, '${{ticker}}', '${{field}}')">${{display}}</span>`;
}}

// Rotes Warndreieck mit weissem Ausrufezeichen
const TRAP_ICON = `<svg width="26" height="26" viewBox="0 0 24 24" style="vertical-align:middle;margin-left:4px;" title="Moegliche Dividenden-Falle">
  <path d="M12 2.5 L22.5 21 L1.5 21 Z" fill="#D32F2F"/>
  <text x="12" y="18.5" text-anchor="middle" fill="#FFFFFF" font-size="14" font-weight="800" font-family="Arial,sans-serif">!</text>
</svg>`;

function matchesSearch(r, term) {{
  return [r.ticker, r.name, r.land, r.sektor, r.isin]
    .some(v => v && String(v).toLowerCase().includes(term));
}}

function renderScanner() {{
  const tbody = document.getElementById('scanner-tbody');
  tbody.innerHTML = '';
  let dataWithOverrides = applyScannerOverrides(SCANNER).map(r => ({{...r, evaluation: evaluateScannerCandidateJS(r)}}));
  const searchTerm = (document.getElementById('scanner-search').value || '').trim().toLowerCase();
  let filtered = dataWithOverrides.filter(r =>
    riskFilterSet.has(r.evaluation.risk) &&
    (landFilter === 'alle' || r.land === landFilter) &&
    (sektorFilter === 'alle' || r.sektor === sektorFilter) &&
    (!onlyWarnings || r.evaluation.trap_warning) &&
    r.evaluation.meets_min_yield &&
    (!searchTerm || matchesSearch(r, searchTerm))
  );
  filtered.sort((a, b) => {{
    let av = sortValue(a, scannerSortKey), bv = sortValue(b, scannerSortKey);
    if (av === null || av === undefined) av = typeof bv === 'string' ? '' : -Infinity;
    if (bv === null || bv === undefined) bv = typeof av === 'string' ? '' : -Infinity;
    if (typeof av === 'string') return scannerSortDir * av.localeCompare(bv);
    return scannerSortDir * (av - bv);
  }});
  document.getElementById('scanner-count').textContent = filtered.length;
  filtered.forEach((r, i) => {{
    const ev = r.evaluation;
    const of_ = r._overriddenFields || [];
    const tr = document.createElement('tr');
    tr.onclick = (e) => {{ if (e.target.tagName !== 'BUTTON' && e.target.tagName !== 'INPUT' && !e.target.classList.contains('editable-value')) showDetail(r.ticker); }};
    const resetBtn = of_.length > 0
      ? `<button class="action-btn" onclick="event.stopPropagation(); resetScannerOverrides('${{r.ticker}}')" title="Manuelle Aenderungen zuruecksetzen">↺</button>` : '';
    const dividendClass = r._dividendComputed && !of_.includes('dividend_per_share') ? 'computed-hint' : (of_.includes('dividend_per_share') ? 'overridden' : '');
    tr.innerHTML = `
      <td>${{i + 1}}</td>
      <td><strong>${{r.ticker}}</strong>${{r.isin ? `<br><span style="color:var(--muted);font-size:10px;font-family:monospace;">${{r.isin}}</span>` : ''}}</td>
      <td>${{r.name || ''}}</td>
      <td>${{r.land}}</td>
      <td>${{r.sektor}} <span style="color:var(--muted);font-size:10px;">(${{r.data_source === 'twelvedata' ? 'TD' : r.data_source === 'finnhub' ? 'FH' : r.data_source === 'alphavantage' ? 'AV' : r.data_source === 'manuell' ? '📋 manuell' : 'FMP'}})</span></td>
      <td class="${{of_.includes('price') ? 'overridden' : ''}}">${{editableCell(r.ticker, 'price', r.price)}}</td>
      <td class="${{dividendClass}}" title="${{r._dividendComputed ? 'automatisch aus Kurs × Rendite berechnet' : ''}}">${{editableCell(r.ticker, 'dividend_per_share', r.dividend_per_share)}}</td>
      <td class="yield ${{of_.includes('dividend_yield_pct') ? 'overridden' : ''}}">${{editableCell(r.ticker, 'dividend_yield_pct', r.dividend_yield_pct)}}</td>
      <td class="${{of_.includes('perf_1m') ? 'overridden' : ''}}">${{editableCellColored(r.ticker, 'perf_1m', r.perf_1m)}}</td>
      <td class="${{of_.includes('perf_3m') ? 'overridden' : ''}}">${{editableCellColored(r.ticker, 'perf_3m', r.perf_3m)}}</td>
      <td class="${{of_.includes('perf_1y') ? 'overridden' : ''}}">${{editableCellColored(r.ticker, 'perf_1y', r.perf_1y)}}</td>
      <td>${{r.next_payout || '–'}}</td>
      <td>
        <span class="badge ${{ev.risk}}">${{ev.risk}}</span>
        ${{ev.trap_warning ? TRAP_ICON : ''}}
      </td>
      <td><button class="action-btn" onclick="event.stopPropagation(); addToDepot('${{r.ticker}}')" title="Ins Dividendendepot uebernehmen">→ Depot</button>${{resetBtn}}</td>`;
    tbody.appendChild(tr);
  }});
}}

document.getElementById('scanner-tbl').addEventListener('click', e => {{
  const th = e.target.closest('th[data-key]');
  if (!th) return;
  const key = th.dataset.key;
  if (scannerSortKey === key) scannerSortDir *= -1; else {{ scannerSortKey = key; scannerSortDir = 1; }}
  renderScanner();
}});

populateFilterDropdowns();
renderDepot();
renderScanner();
</script>
</body></html>"""
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
