# GBB_SoftwareUpdater (v6)

Windows-Software-Updater mit GUI und Provider-Kette:
**Chocolatey -> WinGet -> Interner Installer -> Lokal/USB -> Quelle erforderlich**
oder (bei aktivierter Option) **Lokal/USB -> Chocolatey -> WinGet -> Interner Installer -> Quelle erforderlich**.

## Projektsetup

- Nur **eine** virtuelle Umgebung verwenden: `GBB_SoftwareUpdater/.venv`
- Keine äußere Workspace-`.venv` nutzen

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Dev Start

```powershell
.\scripts\run_dev.ps1
```

## Clean Build

```powershell
.\scripts\clean.ps1
.\scripts\build.ps1
```

Root-Wrapper bleibt verfügbar:

```powershell
.\build.ps1
```

## One-file EXE

- Build-Ausgabe: `dist/GBB_SoftwareUpdater.exe`
- Optionales EXE-Icon: `assets/logo.ico` (wird beim Build automatisch verwendet, falls vorhanden)
- UI-Logo/Fenster-Icon: `assets/logo.png`
- Runtime-Dateien bei EXE-Start:
  - `%LOCALAPPDATA%\GBB_SoftwareUpdater\config.json`
  - `%LOCALAPPDATA%\GBB_SoftwareUpdater\logs\`
  - `%LOCALAPPDATA%\GBB_SoftwareUpdater\logs\reports\`

`config.example.json` ist die Repo-Vorlage. `config.json` wird zur Laufzeit erstellt (oder als Dev-Fallback aus dem Projekt gelesen).

## Software-Provider Konfiguration

In `config.json` (oder Settings-Dialog) pro Software:

- `enabled`
- `display_name`
- `choco_package`
- `winget_id`
- `internal_installer.path`
- `internal_installer.type` (`auto` / `msi` / `exe`)
- `internal_installer.silent_args`
- `search_terms`
- `local_patterns` (optional, z. B. `["Firefox*.exe", "AcroRdr*.exe", "*.msi"]`)

Beispiele:

- **Avaya Workplace**: `winget_id = Avaya.AvayaWorkplace` (wenn Choco fehlt)
- **OpenText**: bei produktabhängiger Lage internen Installerpfad setzen; ohne Quelle => `Quelle erforderlich`
- **OpenText Core Endpoint Protection (Webroot-Agent)**: Standard-URL `https://anywhere.webrootcloudav.com/zerol/wsasme.exe`; für **stilles Hintergrund-Setup** den **Site-Keycode** aus der Management Console als `internal_installer.endpoint_keycode` hinterlegen (Format `XXXX-XXXX-XXXX-XXXX-XXXX`). Der Agent wird nach Download in genau diese **`.exe`-Datei umbenannt** und gestartet (wie in der Herstellerdoku). Alternativ lokal die von der Console heruntergeladene Keycode-EXE angeben. (Sehr alte Systeme: Hersteller-Link `wsasmefnl.exe` — hier nicht als Standard gesetzt.)
- **Avaya / OpenText MSI (Beispiel)**:
  - `internal_installer.path = \\fileserver\software\Avaya\AvayaWorkplaceSetup.msi`
  - `internal_installer.type = msi`
  - `internal_installer.silent_args = /qn /norestart`

Bei `*.msi` (oder `type = msi`) wird nativ über `msiexec` installiert:

- `msiexec /i "<path>" /qn /norestart [silent_args]`

## Offline-/USB-Installationsmodus

- Neuer UI-Bereich:
  - **Installationsquelle wählen**
  - **Quelle scannen**
  - Anzeige **Aktive lokale Quelle: <Pfad>**
  - Checkbox **Lokale Quelle bevorzugen**
- Unterstützte Struktur:
  - verschachtelt, z. B. `D:\Software\Firefox\Firefox Setup.exe`
  - flach, z. B. `D:\Software\Firefox Setup.exe`
- Unterstützte Dateitypen:
  - `.exe` (Silent-Args aus `config.json`; ohne Args Warnung möglich interaktiv)
  - `.msi` (`msiexec /i "<installer>" /qn /norestart`)
  - `.ps1` (Sicherheitswarnung im Log; nur gezieltes Matching)
  - `.bat` wird erkannt, aber aus Sicherheitsgründen nicht automatisch gestartet
- Sicherheit:
  - Nur bekannte/zugeordnete Installer pro ausgewähltem Programm
  - Pfad und SHA256-Hash werden ins Log und in den Report geschrieben

## Entfernen / Dry-Run

- Button: **Ausgewählte entfernen**
- Sicherheitsdialog vor produktivem Remove
- Dry-Run simuliert Install/Update/Remove (`DRY-RUN: würde entfernen`)

## Mandatory Install Mode

- Checkbox: **Pflichtsoftware erzwingen**
- Installiert alle aktivierten Programme im Best-Effort-Verfahren.
- Vor der Installation wird ein Source-Check ausgeführt.
- Provider-Kette:
  1. Chocolatey definierter Paketname
  2. Chocolatey dynamische Suche
  3. WinGet definierte ID
  4. WinGet dynamische Suche
  5. Interner Installer
  6. Quelle erforderlich
- Bereits vorhandene Programme werden nicht unnötig neu installiert.

## Robuste Fehlerbehandlung (v6+)

- **Installer-Lock / Fehler 1618**
  - Exitcode `1618` und typische Installer-Lock-Indikatoren werden erkannt.
  - Zusätzlich werden laufende Prozesse geprüft: `msiexec.exe`, `setup.exe`, `installer.exe`, `OfficeClickToRun.exe`.
  - Bei Lock erfolgt automatisches Warten (`30s`) und Retry (max. `5` Versuche), mit Log:
    - `Installer-Lock erkannt, warte auf laufende Installation...`
- **Reboot Pending**
  - Geprüfte Registry-Schlüssel:
    - `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending`
    - `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired`
    - `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\PendingFileRenameOperations`
  - UI-Hinweis: `Neustart empfohlen/erforderlich`
  - CSV-Report enthält zusätzliche Spalte `Neustart erforderlich` (`yes/no`)
- **TeamViewer Hash mismatch**
  - Standardpaket bleibt `teamviewer` (nicht `teamviewer-qs`).
  - Bei Chocolatey Hash mismatch wird automatisch auf WinGet-Fallback gewechselt.
  - Es wird **nicht** mit `--ignore-checksums` gearbeitet (Sicherheits- und Integritätsgründe).
  - Log:
    - `Chocolatey Hash mismatch, wechsel zu WinGet.`
- **Avaya / OpenText Quellenlogik**
  - Keine hardcodierten lokalen `D:\`-Abhängigkeiten.
- Avaya-Reihenfolge: WinGet (`Avaya.AvayaWorkplace`) -> interner Installer (MSI/EXE) -> Chocolatey fallback.
- OpenText-Reihenfolge (Mandatory Internal Installer Mode): interner Installer (MSI/EXE) -> optional Configure-Schritt per `EConfig.ps1 -rfile <response_file>` -> optional WinGet/Chocolatey Fallback nur bei fehlender interner Quelle -> `Quelle erforderlich`.
  - Es wird kein lokaler Installer gestartet, wenn die Datei nicht existiert.
- OpenText Preflight vor internem Lauf:
  - Installer-Datei vorhanden
  - Response-File (optional) vorhanden
  - Adminrechte vorhanden
  - Port-Hinweis 389/636 wird geloggt

## Quellen-Caching

- Dynamisch gefundene Treffer werden in `config.json` gespeichert:
  - `software_providers.<key>.resolved.choco_package`
  - `software_providers.<key>.resolved.winget_id`
  - `software_providers.<key>.resolved.last_verified`
- Beim nächsten Lauf werden diese aufgelösten Quellen bevorzugt genutzt.

## Sicherheitsregel bei Quellen

- Keine unbekannten Direktdownloads von Webseiten.
- Erlaubte Quellen sind:
  - Chocolatey
  - WinGet
  - konfigurierte interne Installer (Pfad/URL in `config.json`)
