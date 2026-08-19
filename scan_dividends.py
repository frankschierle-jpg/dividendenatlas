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
MIN_YIELD_PCT = 5.0              # Aktien unter dieser Rendite gelten als "nicht qualifiziert"
MAX_YIELD_PCT = 8.0              # darueber: Dividenden-Fallen-Warnung statt Qualifikation

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
]

# Twelve Data nutzt "SYMBOL:BOERSE" statt FMP's "SYMBOL.LAND" - Mapping der
# Endungen, die in CANDIDATE_UNIVERSE verwendet werden, auf Twelve-Data-Boersen.
# Falls eine Boersen-Kuerzung nicht stimmt, scheitert nur dieser eine Ticker
# bei Twelve Data (wird geloggt), der Rest laeuft unbeeinflusst weiter.
TWELVE_DATA_EXCHANGE_MAP = {
    ".DE": "XETRA", ".SW": "SIX", ".L": "LSE",
    ".PA": "EURONEXT", ".MC": "BME", ".MI": "BIT",
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
    siehe to_twelve_data_symbol()."""
    if not api_key:
        return None
    td_symbol = to_twelve_data_symbol(ticker)
    quote = api_get("quote", api_key, params={"symbol": td_symbol}, base_url=TWELVE_DATA_URL,
                     source_label="Twelve Data")
    stats = api_get("statistics", api_key, params={"symbol": td_symbol}, base_url=TWELVE_DATA_URL,
                     source_label="Twelve Data")
    if not quote or quote.get("status") == "error":
        log(f"  Twelve-Data-Fehler bei {td_symbol}: {(quote or {}).get('message', 'keine Antwort')}")
        return None
    stats = stats or {}
    stat = stats.get("statistics", {}) if isinstance(stats, dict) else {}
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
    """Qualitaetskriterien fuer den SCANNER (Kandidatensuche) - warnt vor
    Dividenden-Fallen. Grenzwerte bewusst so gewaehlt, dass reale, etablierte
    hoeher-rentierende Zahler (z.B. Tabak/REITs mit ~80% Ausschuettung)
    nicht faelschlich rausfallen, aber echte Fallen (>100%, >8% Rendite als
    Symptom eines Kurseinbruchs) klar markiert werden."""
    payout = f.get("payout_ratio_pct")
    yield_pct = f.get("dividend_yield_pct")
    debt = f.get("debt_to_equity")

    payout_ok = payout is not None and 0 < payout <= 85
    yield_reasonable = yield_pct is not None and MIN_YIELD_PCT <= yield_pct <= MAX_YIELD_PCT
    debt_ok = debt is None or debt <= 2.0
    trap_warning = (yield_pct is not None and yield_pct > MAX_YIELD_PCT + 0.5) or (
        payout is not None and payout > 100
    )
    qualifies = payout_ok and yield_reasonable and debt_ok

    # Einfacher, transparenter Qualitaets-Score fuers Ranking: Rendite
    # zaehlt positiv (aber gedeckelt bei 8%, um Renditejagd zu vermeiden),
    # hohe Ausschuettungsquote und hohe Verschuldung ziehen ab.
    score = min(yield_pct or 0, MAX_YIELD_PCT)
    score -= max(0, (payout or 0) - 50) / 10
    score -= (debt or 0) * 2

    return {
        "payout_ok": payout_ok, "yield_reasonable": yield_reasonable,
        "debt_ok": debt_ok, "trap_warning": trap_warning,
        "qualifies": qualifies, "score": round(score, 2),
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

    # Scanner-Ranking: nach Qualitaets-Score sortiert, mit Rang
    scanner_view = [history[t] for t in scanner_tickers if t in history]
    scanner_view.sort(key=lambda e: e["evaluation"]["score"], reverse=True)
    for i, e in enumerate(scanner_view):
        e["rank"] = i + 1

    # "Chance"-Hinweis: bester qualifizierender Scanner-Kandidat vs.
    # schwaechste Depot-Position (nach Rendite auf Kaufkurs)
    chance_hinweis = None
    qualifying_scanner = [e for e in scanner_view if e["evaluation"]["qualifies"]]
    if depot_view and qualifying_scanner:
        weakest = min(depot_view, key=lambda d: d["depot_evaluation"]["rendite_auf_kaufkurs"] or 0)
        best_candidate = qualifying_scanner[0]
        if (best_candidate["dividend_yield_pct"] or 0) > (
            weakest["depot_evaluation"]["rendite_auf_kaufkurs"] or 0
        ) + 1.0:  # mind. 1 Prozentpunkt besser, um Rauschen zu vermeiden
            chance_hinweis = (
                f"{best_candidate['ticker']} ({best_candidate['dividend_yield_pct']:.1f}% Rendite, "
                f"Rang {best_candidate['rank']} im Scanner) schlaegt aktuell deine schwaechste "
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
    <th></th><th>Ticker</th><th>Name</th><th>Anteile</th><th>Kaufkurs</th><th>Kaufdatum</th>
    <th>Akt. Kurs</th><th>Kursentw.</th><th>Rendite auf Kaufkurs</th><th>Akt. Marktrendite</th>
    <th>Ausschuett.-Quote</th><th>Naechster Zahltermin</th><th>Hinweise</th><th></th>
  </tr></thead>
  <tbody id="depot-tbody"></tbody>
</table>

<h2>Watchlist (gerankt, {len(scanner_view)} Kandidaten)</h2>
<div class="hint">Nur Inspiration, komplett getrennt vom Dividendendepot. Score = Rendite (gedeckelt bei {MAX_YIELD_PCT:.0f}%) minus Abzuege fuer hohe Ausschuettungsquote/Verschuldung. Mindestrendite fuer "qualifiziert": {MIN_YIELD_PCT:.0f}%.</div>
<table id="scanner-tbl">
  <thead><tr>
    <th data-key="rank">Rang</th><th data-key="ticker">Ticker</th><th data-key="name">Name</th>
    <th data-key="land">Land</th><th data-key="sektor">Sektor</th><th data-key="dividend_yield_pct">Rendite</th>
    <th data-key="payout_ratio_pct">Ausschuett.-Quote</th><th data-key="debt_to_equity">Verschuldung</th>
    <th data-key="evaluation.score">Score</th><th>Status</th>
  </tr></thead>
  <tbody id="scanner-tbody"></tbody>
</table>

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

// --- localStorage-Depotverwaltung ------------------------------------
// Positionen, die du per Klick aus der Watchlist hinzufuegst, werden HIER
// im Browser gespeichert (nicht in my_depot.json) - bleiben aber erhalten,
// solange du denselben Browser/Rechner benutzt.
function getLocalDepot() {{
  try {{ return JSON.parse(localStorage.getItem(DEPOT_KEY) || '[]'); }}
  catch(e) {{ return []; }}
}}
function saveLocalDepot(list) {{ localStorage.setItem(DEPOT_KEY, JSON.stringify(list)); }}

function addToDepot(ticker) {{
  const anteile = parseFloat(prompt('Wie viele Anteile von ' + ticker + '?'));
  if (!anteile || anteile <= 0) return;
  const kaufkurs = parseFloat(prompt('Zu welchem Kaufkurs pro Anteil (in $)?'));
  if (!kaufkurs || kaufkurs <= 0) return;
  const jetzt = new Date();
  const kaufdatum = jetzt.toISOString().slice(0, 10);
  const kaufzeit = jetzt.toTimeString().slice(0, 5);
  const depot = getLocalDepot();
  depot.push({{ticker, anteile, kaufkurs, kaufdatum, kaufzeit, next_payout_manual: null}});
  saveLocalDepot(depot);
  renderDepot();
}}

function editKaufdatum(idx) {{
  const depot = getLocalDepot();
  const neu = prompt('Neues Kaufdatum (JJJJ-MM-TT):', depot[idx].kaufdatum);
  if (!neu) return;
  depot[idx].kaufdatum = neu;
  saveLocalDepot(depot);
  renderDepot();
}}

function removeFromDepot(idx) {{
  const depot = getLocalDepot();
  depot.splice(idx, 1);
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

function evaluateDepotPosition(pos, current) {{
  const kaufkurs = pos.kaufkurs;
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
  // Python-seitige Positionen (aus my_depot.json) + im Browser
  // hinzugefuegte Positionen zusammenfuehren.
  const localDepot = getLocalDepot();
  const combined = [...PYTHON_DEPOT.map((d, i) => ({{...d, _source: 'python', _idx: i}})),
                    ...localDepot.map((d, i) => ({{...d, _source: 'local', _idx: i}}))];
  const scannerByTicker = {{}};
  SCANNER.forEach(s => {{ scannerByTicker[s.ticker] = s; }});

  return combined.map(pos => {{
    // Python-Positionen bringen ihre "current"-Daten schon mit,
    // im Browser hinzugefuegte muessen sich aus der Watchlist bedienen.
    const current = pos.current || scannerByTicker[pos.ticker];
    if (!current) return null;
    const ev = pos.depot_evaluation || evaluateDepotPosition(pos, current);
    return {{...pos, current, depot_evaluation: ev}};
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
    const removeBtn = d._source === 'local'
      ? `<button onclick="removeFromDepot(${{d._idx}})" title="Aus Depot entfernen">🗑</button>`
      : '';
    const editDateBtn = d._source === 'local'
      ? `<button onclick="editKaufdatum(${{d._idx}})" title="Kaufdatum aendern">✎</button>` : '';
    tr.innerHTML = `
      <td><span class="ampel ${{ev.status}}"></span></td>
      <td><strong>${{d.ticker}}</strong></td>
      <td>${{c.name || ''}}</td>
      <td>${{d.anteile}}</td>
      <td>${{fmt(d.kaufkurs, ' $')}}</td>
      <td>${{d.kaufdatum}} ${{editDateBtn}}</td>
      <td>${{fmt(c.price, ' $')}}</td>
      <td class="yield">${{fmt(ev.gain_pct, '%')}}</td>
      <td class="yield">${{fmt(ev.rendite_auf_kaufkurs, '%')}}</td>
      <td>${{fmt(c.dividend_yield_pct, '%')}}</td>
      <td>${{fmt(c.payout_ratio_pct, '%')}}</td>
      <td>${{c.next_payout || d.next_payout_manual || '–'}}</td>
      <td>${{reasons.map(r => `<span class="badge ${{ev.status}}">${{r}}</span>`).join('')}}</td>
      <td>${{removeBtn}}</td>`;
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

function scoreClass(score) {{
  if (score >= 3) return 'gruen';
  if (score >= 0) return 'orange';
  return 'rot';
}}

function renderScanner() {{
  const tbody = document.getElementById('scanner-tbody');
  tbody.innerHTML = '';
  SCANNER.forEach(r => {{
    const ev = r.evaluation;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${{r.rank}}</td>
      <td><strong>${{r.ticker}}</strong></td>
      <td>${{r.name || ''}}</td>
      <td>${{r.land}}</td>
      <td>${{r.sektor}} <span style="color:var(--muted);font-size:10px;">(${{r.data_source === 'twelvedata' ? 'TD' : r.data_source === 'finnhub' ? 'FH' : 'FMP'}})</span></td>
      <td class="yield">${{fmt(r.dividend_yield_pct, '%')}}</td>
      <td>${{fmt(r.payout_ratio_pct, '%')}}</td>
      <td>${{fmt(r.debt_to_equity)}}</td>
      <td><span class="badge ${{scoreClass(ev.score)}}" style="font-size:13px;font-weight:700;">${{fmt(ev.score)}}</span></td>
      <td>
        <span class="badge ${{ev.qualifies ? 'gruen' : ''}}">${{ev.qualifies ? 'qualifiziert ✓' : 'nicht qualifiziert ✗'}}</span>
        ${{ev.trap_warning ? '<span class="badge rot">⚠ moegliche Dividenden-Falle</span>' : ''}}
      </td>
      <td><button onclick="addToDepot('${{r.ticker}}')" title="Ins Dividendendepot uebernehmen">→ ins Depot</button></td>`;
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
