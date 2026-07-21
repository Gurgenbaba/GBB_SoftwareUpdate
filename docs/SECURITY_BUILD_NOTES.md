# Security Build Notes — Compexx-InstallTool

## Warum gibt es Antivirus-False-Positives?

### Ursache 1: PyInstaller Onefile / Packed Payload

PyInstaller bundelt alle Python-Module, DLLs und Ressourcen in eine einzige EXE.
Beim Start extrahiert diese EXE ihre Bestandteile in ein temporaeres Verzeichnis (`%TEMP%\_MEIxxxxxx\`).

Dieses Verhalten — eine EXE schreibt ausfuehrbare Dateien nach `%TEMP%` und startet sie — ist
ein klassisches Dropper/Loader-Muster. AV-Engines wie Microsoft AMSI, heuristische Scanner
(z. B. Wacatac.B!ml) und ML-basierte Engines (Yandex Riskware.PyInstaller) erkennen dieses
Muster unabhaengig vom tatsaechlichen Inhalt.

### Ursache 2: Hohes Entropie-Overlay

Der komprimierte Python-Bytecode und die eingebetteten DLLs erzeugen einen hochentropischen
PE-Abschnitt. Hochentropie im Overlay ist ein Indikator fuer Packing/Verschluesselung — beides
Merkmale, die AV-Engines als verdaechtig einstufen.

### Ursache 3: Admin-Rechte und System-Aktionen

Compexx-InstallTool:
- Fordert UAC-Elevation an (`ShellExecuteW` mit `runas`)
- Fuehrt `choco`, `winget`, `msiexec`, `net user` aus
- Liest und schreibt die Windows-Registry
- Liest Registry-Uninstall-Eintraege

Diese Aktionen sind notwendig fuer einen Software-Updater, aber AV-Engines bewerten sie
zusammen mit dem PyInstaller-Onefile-Muster als erhoehtes Risiko.

---

## Empfohlene Build-Strategie

### Onedir (Enterprise, bevorzugt)

```powershell
.\scripts\build_onedir.ps1
```

**Vorteile:**
- Kein `%TEMP%`-Extraktion — die EXE laedt direkt aus dem Installationsordner
- Einzelne DLLs sichtbar und von Sicherheitsteams pruefbar
- Signifikant geringere False-Positive-Rate
- Schnellerer Startvorgang

**Deployment:** Den gesamten Ordner `dist\Compexx-InstallTool\` verteilen (z. B. per GPO,
SCCM, Intune). Nur die EXE zu verteilen genuegt nicht.

### Onefile (Einzeldatei, hoehere FP-Rate)

```powershell
.\build.ps1
```

Fuer interne Tests oder wenn ein Einzeldatei-Deployment zwingend erforderlich ist.
Onefile ist **nicht empfohlen** fuer Unternehmensumgebungen mit aktivem Virenschutz.

---

## Massnahmen zur Reduktion von False Positives

| Massnahme | Status |
|---|---|
| UPX-Kompression deaktiviert (`upx=False`) | Umgesetzt |
| Windows VersionInfo-Metadaten (CompanyName, FileDescription) | Umgesetzt |
| EXE-Name: "Compexx-InstallTool.exe" (kein generisches "Updater.exe") | Umgesetzt |
| Onedir-Build-Ziel verfuegbar | Umgesetzt |
| `shell=False` fuer alle subprocess-Aufrufe | Umgesetzt |
| HTTP-Downloads blockiert (nur HTTPS) | Umgesetzt |
| Lokale .ps1/.bat-Ausfuehrung blockiert | Umgesetzt |
| Download-Cache-Verzeichnis statt anonymer %TEMP%-Dateien | Umgesetzt |
| Startup-Transparenz-Logging | Umgesetzt |

---

## Code Signing (empfohlen)

Der effektivste Weg zur Eliminierung von AV-False-Positives ist ein **EV Code Signing Certificate**
(Extended Validation), ausgestellt von einer CA wie DigiCert, Sectigo oder GlobalSign.

Mit einem gueltigen EV-Zertifikat erhaelt die EXE sofort Windows SmartScreen-Vertrauen, und
die meisten AV-Engines reduzieren ihre Heuristik-Aggressivitaet gegenueber signierten Binaries.

Signierung nach dem Build:
```powershell
# Beispiel mit signtool.exe (Windows SDK)
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
    /a "dist\Compexx-InstallTool\Compexx-InstallTool.exe"
```

---

## Microsoft False-Positive-Meldung

Wenn Microsoft Defender einen False Positive meldet:

1. SHA256 der EXE ermitteln:
   ```powershell
   Get-FileHash -Algorithm SHA256 "dist\Compexx-InstallTool\Compexx-InstallTool.exe"
   ```

2. Meldung einreichen:
   [https://www.microsoft.com/en-us/wdsi/filesubmission](https://www.microsoft.com/en-us/wdsi/filesubmission)
   - Kategorie: "Incorrect detection"
   - Datei hochladen und Beschreibung beifuegen

3. Status pruefen: In der Regel innerhalb von 24-48 Stunden.

---

## Lokaler Defender-Scan

```powershell
.\tools\defender_scan.ps1
# oder gezielt:
.\tools\defender_scan.ps1 -Target "dist\Compexx-InstallTool"
```

Der SHA256-Hash und ein direkter VirusTotal-Link werden ausgegeben (kein automatischer Upload).

---

## Rebuild und Scan — Kurzreferenz

```powershell
# Onedir bauen (empfohlen)
.\scripts\build_onedir.ps1

# Onefile bauen (Einzeldatei, hoehere FP-Rate)
.\build.ps1

# Defender-Scan
.\tools\defender_scan.ps1

# SHA256 manuell
Get-FileHash -Algorithm SHA256 "dist\Compexx-InstallTool\Compexx-InstallTool.exe"
```

---

## SHA256-Referenz

> Hinweis: Hashes aendern sich bei jedem Build. Dieses Dokument enthaelt keine festen Hashes.
> Den aktuellen Hash nach dem Build aus dem Build-Output oder mit `Get-FileHash` ermitteln.
