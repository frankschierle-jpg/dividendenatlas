# Dividendenatlas ueber GitHub — Einrichtung (statt lokalem Server)

## 1. Neues GitHub-Repo anlegen
- github.com → "New repository" → z.B. Name "dividendenatlas" → **Private** wählen (deine Depot-Daten sind sonst öffentlich sichtbar!)

## 2. Dateien hochladen
Diese Dateien in den Repo-Hauptordner:
- `scan_dividends.py`
- `.github/workflows/scan.yml` (Ordnerstruktur beibehalten!)
- `my_depot.json` (falls schon vorhanden, sonst legt der erste Lauf automatisch eine Beispiel-Datei an)

**NICHT hochladen:** `config.json` (falls noch vorhanden) — die Keys kommen jetzt über GitHub Secrets, nicht über diese Datei.

## 3. API-Keys als Secrets hinterlegen
Im Repo: **Settings → Secrets and variables → Actions → New repository secret**

Drei Secrets anlegen:
| Name | Wert |
|---|---|
| `FMP_API_KEY` | dein FMP-Key |
| `TWELVE_DATA_API_KEY` | dein Twelve-Data-Key |
| `FINNHUB_API_KEY` | dein Finnhub-Key |

## 4. GitHub Pages aktivieren
**Settings → Pages** → Branch: `main`, Ordner: `/ (root)` → Speichern

Deine Adresse danach (ersetz DEINNAME und dividendenatlas mit deinen echten Werten):
```
https://DEINNAME.github.io/dividendenatlas/dividendenatlas.html
```

## 5. Ersten Lauf manuell anstoßen (nicht auf morgen 6 Uhr UTC warten)
Im Repo: **Actions-Tab → "Dividendenatlas taeglicher Scan" → "Run workflow"**

Nach ca. 1-2 Minuten sollte der Lauf grün (erfolgreich) sein, und die Webseite oben ist erreichbar.

## Was sich ändert
- **Kein Terminal, kein Server mehr nötig** — läuft komplett in der Cloud
- Läuft automatisch **täglich um 6 Uhr UTC** (Cron in der `scan.yml` anpassbar)
- Die "→ ins Depot"-Buttons funktionieren identisch weiter (Browser-localStorage)
- Bei Fehlern: **Actions-Tab** zeigt das komplette Log jedes Laufs, genau wie bisher im Terminal

## Falls ein Lauf rot (fehlgeschlagen) ist
Im Actions-Tab auf den Lauf klicken → Log aufklappen → mir die Fehlermeldung schicken, genau wie bisher.
