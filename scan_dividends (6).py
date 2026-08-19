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
REPORT_PATH = os.path.join(BASE_DIR, "dividendenatlas.html")
LOG_PATH = os.path.join(BASE_DIR, "dividendenatlas.log")

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
FMP_STABLE_URL = "https://financialmodelingprep.com/stable"
TWELVE_DATA_URL = "https://api.twelvedata.com"
FINNHUB_URL = "https://finnhub.io/api/v1"

# --- Alarm-Schwellenwerte fuer "Mein Depot" (jederzeit anpassbar) ----------
YELLOW_PRICE_DROP_PCT = 5.0      # Kurs X% unter Kaufkurs -> gelb
DECLINE_TREND_DAYS = 5           # so viele Laeufe in Folge ruecklaeufig -> gelb
PRICE_HISTORY_KEEP = 60          # so viele Tages-Kurse pro Ticker aufheben

# --- Mindestrendite fuer den SCANNER (jederzeit anpassbar) -----------------
MIN_YIELD_PCT = 6.0              # Aktien unter dieser Rendite gelten als "nicht qualifiziert"
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
]

# Twelve Data nutzt "SYMBOL:BOERSE" statt FMP's "SYMBOL.LAND" - Mapping der
# Endungen, die in CANDIDATE_UNIVERSE verwendet werden, auf Twelve-Data-Boersen.
# Falls eine Boersen-Kuerzung nicht stimmt, scheitert nur dieser eine Ticker
# bei Twelve Data (wird geloggt), der Rest laeuft unbeeinflusst weiter.
TWELVE_DATA_EXCHANGE_MAP = {
    ".DE": "XETRA", ".SW": "SIX", ".L": "LSE",
    ".PA": "EURONEXT", ".MC": "BME", ".MI": "BIT",
    ".SI": "SGX", ".HK": "HKEX",
}


def to_twelve_data_symbol(ticker):
    """Wandelt z.B. 'BAS.DE' in 'BAS:XETRA' um. US-Ticker ohne Punkt
    bleiben unveraendert (Twelve Data braucht dort keine Boersen-Angabe)."""
    for suffix, exchange in TWELVE_DATA_EXCHANGE_MAP.items():
        if ticker.endswith(suffix):
            return f"{ticker[:-len(suffix)]}:{exchange}"
    return ticker


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    """Liest die API-Keys entweder aus Umgebungsvariablen (fuer GitHub
    Actions: FMP_API_KEY, TWELVE_DATA_API_KEY, FINNHUB_API_KEY als Secrets
    hinterlegt) ODER aus einer lokalen config.json (fuer manuelle Laeufe
    auf deinem Mac). Umgebungsvariablen haben Vorrang, falls beide da sind."""
    env_keys = {
        "fmp_api_key": os.environ.get("FMP_API_KEY"),
        "twelve_data_api_key": os.environ.get("TWELVE_DATA_API_KEY"),
        "finnhub_api_key": os.environ.get("FINNHUB_API_KEY"),
    }
    if any(env_keys.values()):
        return env_keys
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"Weder Umgebungsvariablen (FMP_API_KEY etc.) noch config.json unter "
            f"{CONFIG_PATH} gefunden. Lokal: config.json anlegen mit "
            f'{{"fmp_api_key": "...", "twelve_data_api_key": "...", "finnhub_api_key": "..."}}. '
            f"In GitHub Actions: die drei Secrets hinterlegen, siehe README."
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


def api_get(endpoint, api_key, params=None, base_url=None, key_param="apikey", source_label="FMP"):
    """Generischer GET-Helfer fuer alle drei Datenquellen (FMP, Twelve
    Data, Finnhub) - der Name des Key-Parameters unterscheidet sich
    zwischen den Anbietern (apikey vs. token)."""
    params = params or {}
    params[key_param] = api_key
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{base_url or FMP_BASE_URL}/{endpoint}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log(f"  {source_label}-Fehler (HTTP {e.code}) bei {endpoint}: {e.read()[:150]}")
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


def fetch_fundamentals(ticker, fmp_key, twelve_data_key, finnhub_key):
    """Versucht der Reihe nach FMP -> Twelve Data -> Finnhub. So muss
    vorher nicht bekannt sein, welcher Anbieter welchen Ticker abdeckt -
    das Skript findet es pro Aktie selbst heraus, indem es einfach die
    naechste Quelle probiert."""
    result = fetch_fundamentals_fmp(ticker, fmp_key)
    if result is not None:
        return result
    result = fetch_fundamentals_twelvedata(ticker, twelve_data_key)
    if result is not None:
        log(f"    -> ueber Twelve Data statt FMP erhalten")
        return result
    result = fetch_fundamentals_finnhub(ticker, finnhub_key)
    if result is not None:
        log(f"    -> ueber Finnhub statt FMP/Twelve Data erhalten")
    return result


def fetch_latest_grade(ticker, api_key):
    """Best-effort: neueste Analysten-Einschaetzung. Nicht garantiert im
    kostenlosen Kontingent enthalten - gibt bei Fehler einfach None zurueck,
    Rest des Tools funktioniert dann trotzdem weiter."""
    data = fmp_get("grades", api_key, params={"symbol": ticker}, base_url=FMP_STABLE_URL)
    if not data or not isinstance(data, list) or not data:
        return None
    latest = data[0]
    return {
        "date": latest.get("date"),
        "action": latest.get("action"),  # z.B. "upgrade" / "downgrade" / "maintain"
        "firm": latest.get("gradingCompany"),
        "new_grade": latest.get("newGrade"),
        "previous_grade": latest.get("previousGrade"),
    }


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
    }


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


def evaluate_scanner_candidate(f):
    """Risiko-Einstufung statt qualifiziert/nicht-qualifiziert: JEDE Aktie
    mit Rendite >= MIN_YIELD_PCT wird angezeigt (Filterung passiert beim
    Aufbau der Scanner-Liste), hier wird nur noch das RISIKO bewertet -
    niedrig/mittel/hoch, anhand Ausschuettungsquote und Verschuldung.
    Eine hohe Rendite ist also kein Ausschlussgrund mehr, sondern wird
    im Risiko sichtbar gemacht (hohe Rendite + hohe Ausschuettungsquote
    = typisches Muster einer Dividenden-Falle -> Risiko 'hoch')."""
    payout = f.get("payout_ratio_pct")
    yield_pct = f.get("dividend_yield_pct")
    debt = f.get("debt_to_equity")

    trap_warning = (payout is not None and payout > 100) or (
        yield_pct is not None and yield_pct > 12
    )

    if trap_warning:
        risk = "hoch"
    elif (payout is not None and payout <= 60) and (debt is None or debt <= 1.0):
        risk = "niedrig"
    elif (payout is not None and payout <= 85) and (debt is None or debt <= 2.0):
        risk = "mittel"
    else:
        risk = "hoch"

    return {
        "trap_warning": trap_warning,
        "risk": risk,
        "meets_min_yield": yield_pct is not None and yield_pct >= MIN_YIELD_PCT,
    }


def evaluate_depot_position(position, current, grade_info):
    """Status-Ampel fuer eine ECHTE Depot-Position. Gibt Fakten und
    Schwellenwert-Ueberschreitungen zurueck - keine Kauf-/Verkaufsempfehlung."""
    kaufkurs = position["kaufkurs"]
    price = current.get("price")
    payout = current.get("payout_ratio_pct")

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
    if not fmp_key and not twelve_data_key and not finnhub_key:
        log("Kein einziger API-Key (fmp_api_key/twelve_data_api_key/finnhub_api_key) "
            "in config.json gefunden - Abbruch.")
        return
    fehlende_quellen = [n for n, k in [("Twelve Data", twelve_data_key), ("Finnhub", finnhub_key)] if not k]
    if fehlende_quellen:
        log(f"Hinweis: kein Key fuer {', '.join(fehlende_quellen)} hinterlegt - "
            f"Ticker, die von den vorhandenen Quellen nicht geliefert werden, "
            f"werden dann einfach uebersprungen statt eine weitere Ausweich-Quelle zu versuchen.")

    depot_positions = load_depot()
    history = load_json(HISTORY_PATH, {})

    depot_tickers = {p["ticker"] for p in depot_positions}
    scanner_tickers = {t for t, _, _ in CANDIDATE_UNIVERSE}
    all_tickers = depot_tickers | scanner_tickers
    log(f"Hole Daten fuer {len(all_tickers)} Ticker ({len(depot_tickers)} im Depot, "
        f"{len(scanner_tickers)} im Scanner) ...")

    land_sektor_lookup = {t: (land, sektor) for t, land, sektor in CANDIDATE_UNIVERSE}

    for ticker in all_tickers:
        log(f"  Hole Daten fuer {ticker} ...")
        data = fetch_fundamentals(ticker, fmp_key, twelve_data_key, finnhub_key)
        if data is None:
            log(f"    -> keine Daten erhalten (weder FMP noch Twelve Data), ueberspringe.")
            time.sleep(0.3)
            continue
        land, sektor = land_sektor_lookup.get(ticker, ("unbekannt", "unbekannt"))
        data["land"] = land
        data["sektor"] = sektor
        data["price_history"] = history.get(ticker, {}).get("price_history", [])
        update_price_history(data, data["price"])
        data["evaluation"] = evaluate_scanner_candidate(data)
        data["last_updated"] = datetime.now().isoformat()

        if data["evaluation"]["meets_min_yield"] and fmp_key:
            # Kursentwicklung (1M/3M/1J) + ISIN nur fuer Aktien holen, die
            # ohnehin im Scanner erscheinen (>= Mindestrendite) - spart
            # Kontingent bei den vielen Aktien, die sowieso ausgefiltert werden.
            data.update(fetch_price_performance(ticker, fmp_key, data.get("price")))
            data["isin"] = fetch_isin(ticker, fmp_key)
        else:
            data["perf_1m"] = data["perf_3m"] = data["perf_1y"] = None
            data["isin"] = None

        if ticker in depot_tickers and fmp_key:
            # Analysten-Ratings/Zahltermin bisher nur ueber FMP abgefragt -
            # bei ueber Twelve Data bezogenen Depot-Positionen bleiben diese
            # beiden Felder leer, Rest des Tools funktioniert unveraendert.
            data["grade"] = fetch_latest_grade(ticker, fmp_key)
            data["next_payout"] = fetch_next_payout(ticker, fmp_key)

        history[ticker] = data
        time.sleep(0.3)  # sanftes Tempo, kein Ansturm auf die kostenlosen APIs

    save_json(HISTORY_PATH, history)

    # Depot-Bewertung
    depot_view = []
    for pos in depot_positions:
        current = history.get(pos["ticker"])
        if not current:
            continue
        ev = evaluate_depot_position(pos, current, current.get("grade"))
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

    build_report(depot_view, scanner_view, chance_hinweis)
    log(f"Fertig. Bericht aktualisiert: {REPORT_PATH}")


def build_report(depot_view, scanner_view, chance_hinweis):
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
<style>
:root{{
  --bg:#FFFFFF; --surface:#F4F4F6; --text:#1A1A1E; --muted:#75757C;
  --blue:#0F3460; --blueB:#1E5F8C; --blueSoft:#E3EDF7; --line:#DCE4EC; --gray:#8A99A8;
  --red:#D33; --redSoft:#FFE8E8; --green:#0A0; --greenSoft:#E8FFE8;
  --yellow:#B8860B; --yellowSoft:#FFF6DC; --orange:#CC6A1E; --orangeSoft:#FFEFDC;
}}
*{{box-sizing:border-box;}}
body{{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;margin:0;padding:29px;font-size:13px;}}
h1{{font-size:27px;margin:0 0 6px;}}
h2{{font-size:19px;margin:28px 0 10px;color:var(--blue);}}
.meta{{color:var(--muted);font-size:14px;margin-bottom:19px;}}
.summary{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;}}
.summary-card{{background:var(--surface);border-radius:10px;padding:14px 18px;min-width:140px;}}
.summary-card .big{{font-size:24px;font-weight:700;color:var(--blue);}}
.summary-card .label{{color:var(--muted);font-size:11px;text-transform:uppercase;}}
.summary-card.rot .big{{color:var(--red);}}
.summary-card.gelb .big{{color:var(--yellow);}}
.summary-card.gruen .big{{color:var(--green);}}
.chance-box{{background:var(--blueSoft);border:1px solid var(--blue);border-radius:8px;padding:14px 18px;margin-bottom:20px;}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;}}
th{{text-align:left;padding:8px 9px;color:var(--blue);font-size:11px;text-transform:uppercase;border-bottom:2px solid var(--line);}}
td{{padding:9px;border-bottom:1px solid var(--line);vertical-align:top;}}
tr:hover td{{background:var(--surface);}}
tr.rot td{{background:var(--redSoft);}}
tr.gelb td{{background:var(--yellowSoft);}}
.badge{{font-family:monospace;font-size:10px;padding:2px 6px;border-radius:8px;border:1px solid var(--gray);color:var(--gray);margin-right:3px;display:inline-block;margin-top:2px;}}
.badge.rot{{border-color:var(--red);color:var(--red);background:var(--redSoft);}}
.badge.gelb{{border-color:var(--yellow);color:var(--yellow);background:var(--yellowSoft);}}
.badge.orange{{border-color:var(--orange);color:var(--orange);background:var(--orangeSoft);}}
.badge.gruen{{border-color:var(--green);color:var(--green);background:var(--greenSoft);}}
.ampel{{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;vertical-align:middle;}}
.ampel.rot{{background:var(--red);}}
.ampel.gelb{{background:var(--yellow);}}
.ampel.gruen{{background:var(--green);}}
.yield{{font-family:monospace;font-weight:700;font-size:15px;}}
.reasons{{font-size:11px;color:var(--muted);margin-top:3px;}}
.hint{{color:var(--muted);font-size:12px;font-style:italic;margin-bottom:14px;}}
#scanner-tbody tr{{cursor:pointer;}}
.risk-filter-btn{{border:1px solid var(--line);background:var(--surface);border-radius:20px;padding:6px 14px;font-size:12px;cursor:pointer;margin-right:8px;color:var(--muted);}}
.risk-filter-btn.active{{border-color:var(--blue);color:var(--blue);background:var(--blueSoft);}}
.badge.niedrig{{border-color:var(--green);color:var(--green);background:var(--greenSoft);}}
.badge.mittel{{border-color:var(--orange);color:var(--orange);background:var(--orangeSoft);}}
.badge.hoch{{border-color:var(--red);color:var(--red);background:var(--redSoft);}}
.perf-pos{{color:var(--green);}}
.perf-neg{{color:var(--red);}}
.action-btn{{border:1px solid var(--line);background:var(--surface);border-radius:6px;padding:3px 8px;font-size:11px;cursor:pointer;margin:1px;}}
.action-btn:hover{{background:var(--blueSoft);}}
#detail-content h3{{margin-top:0;color:var(--blue);}}
#detail-content table{{font-size:12px;}}
#detail-content td{{padding:5px 8px;}}
#detail-content .close-btn{{float:right;cursor:pointer;font-size:20px;color:var(--muted);}}
</style>
</head>
<body>
<h1>Dividendenatlas</h1>
<div class="meta">Stand: <span id="stand"></span></div>

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
<div class="hint">Nur Inspiration, komplett getrennt vom Dividendendepot. Zeigt ausschliesslich Aktien mit Rendite ≥ {MIN_YIELD_PCT:.0f}%. Klick auf eine Zeile fuer das vollstaendige Profil.</div>
<div class="controls" style="margin-bottom:14px;">
  <button class="risk-filter-btn active" data-risk="alle" onclick="setRiskFilter('alle')">Alle</button>
  <button class="risk-filter-btn" data-risk="niedrig" onclick="setRiskFilter('niedrig')">Nur niedriges Risiko</button>
  <button class="risk-filter-btn" data-risk="mittel" onclick="setRiskFilter('mittel')">Nur mittleres Risiko</button>
  <button class="risk-filter-btn" data-risk="hoch" onclick="setRiskFilter('hoch')">Nur hohes Risiko</button>
</div>
<table id="scanner-tbl">
  <thead><tr>
    <th data-key="rank" title="Rang innerhalb der Risikostufe, sortiert nach Rendite">Rang</th>
    <th data-key="ticker" title="Boersenkuerzel">Ticker</th>
    <th data-key="name" title="Vollstaendiger Firmenname">Name</th>
    <th data-key="land" title="Land der Hauptbörse">Land</th>
    <th data-key="sektor" title="Wirtschaftssektor">Sektor</th>
    <th data-key="dividend_yield_pct" title="Aktuelle jaehrliche Dividendenrendite">Rendite</th>
    <th data-key="perf_1m" title="Kursentwicklung letzter Monat">1M</th>
    <th data-key="perf_3m" title="Kursentwicklung letzte 3 Monate">3M</th>
    <th data-key="perf_1y" title="Kursentwicklung letztes Jahr">1J</th>
    <th data-key="payout_ratio_pct" title="Anteil des Gewinns, der als Dividende ausgeschuettet wird">Ausschuett.-Quote</th>
    <th data-key="debt_to_equity" title="Verschuldung im Verhaeltnis zum Eigenkapital - niedriger ist sicherer">Verschuldung</th>
    <th data-key="evaluation.risk" title="Gesamteinschaetzung des Risikos: niedrig/mittel/hoch, basierend auf Ausschuettungsquote und Verschuldung">Risiko</th>
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


<script>
const PYTHON_DEPOT = {depot_payload};
const SCANNER = {scanner_payload};
const DEPOT_KEY = 'dividendenatlas_depot';
const YELLOW_PRICE_DROP_PCT = {YELLOW_PRICE_DROP_PCT};
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

function aggregateTransactions(transactions) {{
  let anteile = 0, kostenbasis = 0, erstesKaufdatum = null;
  transactions.forEach(t => {{
    if (t.type === 'buy') {{
      anteile += t.anteile;
      kostenbasis += t.anteile * t.kurs;
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
    kaufkurs: anteile > 0 ? kostenbasis / anteile : 0,
    kaufdatum: erstesKaufdatum,
  }};
}}

function addToDepot(ticker) {{
  const anteile = parseFloat(prompt('Wie viele Anteile von ' + ticker + '?'));
  if (!anteile || anteile <= 0) return;
  const kurs = parseFloat(prompt('Zu welchem Kaufkurs pro Anteil (in $)?'));
  if (!kurs || kurs <= 0) return;
  const datum = new Date().toISOString().slice(0, 10);
  const depot = getLocalDepot();
  const existing = depot.find(d => d.ticker === ticker);
  if (existing) {{
    existing.transactions.push({{type: 'buy', anteile, kurs, datum}});
  }} else {{
    depot.push({{ticker, transactions: [{{type: 'buy', anteile, kurs, datum}}], next_payout_manual: null}});
  }}
  saveLocalDepot(depot);
  renderDepot();
}}

function buyMore(ticker) {{
  const anteile = parseFloat(prompt('Zukauf: wie viele weitere Anteile von ' + ticker + '?'));
  if (!anteile || anteile <= 0) return;
  const kurs = parseFloat(prompt('Zu welchem Kurs pro Anteil (in $)?'));
  if (!kurs || kurs <= 0) return;
  const depot = getLocalDepot();
  const pos = depot.find(d => d.ticker === ticker);
  if (!pos) {{ alert('Diese Position wurde ueber my_depot.json angelegt - Zukaeufe dort bitte direkt in der Datei ergaenzen.'); return; }}
  pos.transactions.push({{type: 'buy', anteile, kurs, datum: new Date().toISOString().slice(0, 10)}});
  saveLocalDepot(depot);
  renderDepot();
}}

function sellPartial(ticker) {{
  const depot = getLocalDepot();
  const pos = depot.find(d => d.ticker === ticker);
  if (!pos) {{ alert('Diese Position wurde ueber my_depot.json angelegt - Verkaeufe dort bitte direkt in der Datei erfassen.'); return; }}
  const agg = aggregateTransactions(pos.transactions);
  const anteile = parseFloat(prompt(`Teilverkauf: wie viele der aktuell ${{agg.anteile}} Anteile von ${{ticker}} verkaufen?`));
  if (!anteile || anteile <= 0 || anteile > agg.anteile) {{ if (anteile > agg.anteile) alert('Mehr Anteile als vorhanden.'); return; }}
  const kurs = parseFloat(prompt('Zu welchem Verkaufskurs pro Anteil (in $)?'));
  if (!kurs || kurs <= 0) return;
  pos.transactions.push({{type: 'sell', anteile, kurs, datum: new Date().toISOString().slice(0, 10)}});
  if (aggregateTransactions(pos.transactions).anteile <= 0) {{
    // Position komplett verkauft - ganz aus dem Depot entfernen
    saveLocalDepot(depot.filter(d => d.ticker !== ticker));
  }} else {{
    saveLocalDepot(depot);
  }}
  renderDepot();
}}

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

function evaluateDepotPosition(agg, current) {{
  const kaufkurs = agg.kaufkurs;
  const price = current.price;
  const payout = current.payout_ratio_pct;

  const gainPct = (price && kaufkurs) ? ((price - kaufkurs) / kaufkurs * 100) : null;
  let renditeAufKaufkurs = null;
  if (current.dividend_yield_pct !== null && current.dividend_yield_pct !== undefined && price && kaufkurs) {{
    const dividendPerShare = current.dividend_yield_pct / 100 * price;
    renditeAufKaufkurs = dividendPerShare / kaufkurs * 100;
  }}

  const trend = isDecliningTrend(current.price_history);

  const gradeInfo = current.grade;
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
  // einheitlich behandelt werden koennen.
  const pythonAsTransactions = PYTHON_DEPOT.map(d => ({{
    ticker: d.ticker, _source: 'python', current: d.current,
    next_payout_manual: d.next_payout_manual,
    transactions: [{{type: 'buy', anteile: d.anteile, kurs: d.kaufkurs, datum: d.kaufdatum}}],
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
    tr.innerHTML = `
      <td><span class="ampel ${{ev.status}}"></span></td>
      <td><strong>${{d.ticker}}</strong></td>
      <td>${{c.name || ''}}</td>
      <td>${{d.anteile}}</td>
      <td>${{fmt(d.kaufkurs, ' $')}}</td>
      <td>${{d.kaufdatum || '–'}}</td>
      <td>${{fmt(c.price, ' $')}}</td>
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

function showDetail(ticker) {{
  const r = SCANNER.find(s => s.ticker === ticker);
  if (!r) return;
  const ev = r.evaluation;
  document.getElementById('detail-content').innerHTML = `
    <span class="close-btn" onclick="closeDetail()">×</span>
    <h3>${{r.name || r.ticker}} (${{r.ticker}})</h3>
    <table>
      <tr><td>ISIN</td><td style="font-family:monospace;">${{r.isin || 'nicht verfuegbar'}}</td></tr>
      <tr><td>Land</td><td>${{r.land}}</td></tr>
      <tr><td>Sektor</td><td>${{r.sektor}}</td></tr>
      <tr><td>Kurs</td><td>${{fmt(r.price, ' $')}}</td></tr>
      <tr><td>Marktkapitalisierung</td><td>${{r.market_cap ? (r.market_cap/1e9).toFixed(1) + ' Mrd. $' : '–'}}</td></tr>
      <tr><td>KGV</td><td>${{fmt(r.pe_ratio)}}</td></tr>
      <tr><td>Dividendenrendite</td><td>${{fmt(r.dividend_yield_pct, '%')}}</td></tr>
      <tr><td>Ausschuettungsquote</td><td>${{fmt(r.payout_ratio_pct, '%')}}</td></tr>
      <tr><td>Verschuldung (D/E)</td><td>${{fmt(r.debt_to_equity)}}</td></tr>
      <tr><td>Kursentw. 1 Monat</td><td>${{fmt(r.perf_1m, '%')}}</td></tr>
      <tr><td>Kursentw. 3 Monate</td><td>${{fmt(r.perf_3m, '%')}}</td></tr>
      <tr><td>Kursentw. 1 Jahr</td><td>${{fmt(r.perf_1y, '%')}}</td></tr>
      <tr><td>Datenquelle</td><td>${{r.data_source === 'twelvedata' ? 'Twelve Data' : r.data_source === 'finnhub' ? 'Finnhub' : 'FMP'}}</td></tr>
    </table>
    <h3 style="margin-top:18px;">Risiko-Einstufung: <span class="badge ${{ev.risk}}">${{ev.risk}}</span></h3>
    <p style="font-size:12px;color:var(--muted);">
      Niedrig: Ausschuettungsquote ≤60% UND Verschuldung ≤1,0<br>
      Mittel: Ausschuettungsquote ≤85% UND Verschuldung ≤2,0<br>
      Hoch: alles darueber, oder Ausschuettungsquote >100% / Rendite >12% (typisches Dividenden-Fallen-Muster)
    </p>
    ${{ev.trap_warning ? '<p style="color:var(--red);"><strong>⚠ Moegliche Dividenden-Falle</strong> - die hohe Rendite koennte Symptom eines Kurseinbruchs oder einer nicht finanzierbaren Ausschuettung sein, keine echte Chance.</p>' : ''}}
    <button class="action-btn" style="margin-top:12px;" onclick="addToDepot('${{r.ticker}}'); closeDetail();">→ ins Dividendendepot</button>
  `;
  document.getElementById('detail-panel').style.display = 'block';
  document.getElementById('detail-overlay').style.display = 'block';
}}

function closeDetail() {{
  document.getElementById('detail-panel').style.display = 'none';
  document.getElementById('detail-overlay').style.display = 'none';
}}

let riskFilter = 'alle';

function setRiskFilter(risk) {{
  riskFilter = risk;
  document.querySelectorAll('.risk-filter-btn').forEach(btn => {{
    btn.classList.toggle('active', btn.dataset.risk === risk);
  }});
  renderScanner();
}}

function renderScanner() {{
  const tbody = document.getElementById('scanner-tbody');
  tbody.innerHTML = '';
  const filtered = riskFilter === 'alle' ? SCANNER : SCANNER.filter(r => r.evaluation.risk === riskFilter);
  document.getElementById('scanner-count').textContent = filtered.length;
  filtered.forEach(r => {{
    const ev = r.evaluation;
    const tr = document.createElement('tr');
    tr.onclick = (e) => {{ if (e.target.tagName !== 'BUTTON') showDetail(r.ticker); }};
    tr.innerHTML = `
      <td>${{r.rank}}</td>
      <td><strong>${{r.ticker}}</strong>${{r.isin ? `<br><span style="color:var(--muted);font-size:10px;font-family:monospace;">${{r.isin}}</span>` : ''}}</td>
      <td>${{r.name || ''}}</td>
      <td>${{r.land}}</td>
      <td>${{r.sektor}} <span style="color:var(--muted);font-size:10px;">(${{r.data_source === 'twelvedata' ? 'TD' : r.data_source === 'finnhub' ? 'FH' : 'FMP'}})</span></td>
      <td class="yield">${{fmt(r.dividend_yield_pct, '%')}}</td>
      ${{perfCell(r.perf_1m)}}
      ${{perfCell(r.perf_3m)}}
      ${{perfCell(r.perf_1y)}}
      <td>${{fmt(r.payout_ratio_pct, '%')}}</td>
      <td>${{fmt(r.debt_to_equity)}}</td>
      <td>
        <span class="badge ${{ev.risk}}">${{ev.risk}}</span>
        ${{ev.trap_warning ? '<span class="badge rot">⚠ Falle</span>' : ''}}
      </td>
      <td><button class="action-btn" onclick="addToDepot('${{r.ticker}}')" title="Ins Dividendendepot uebernehmen">→ Depot</button></td>`;
    tbody.appendChild(tr);
  }});
}}

renderDepot();
renderScanner();
</script>
</body></html>"""
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
