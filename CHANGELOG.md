# Changelog

## 5.0.0

- Health Summary Panel oben (Checks/Details, Warnungen und Konfiguration getrennt).
- Softwareliste: Suche, Auswahl **Nur fehlende** / **Nur Updates** / **Standard Patch Run**.
- Abschluss-Dialog nach Pruefung und Installation; optionaler PDF-Export (`fpdf2`).
- PyInstaller **one-file** (`dist/Compexx-InstallTool.exe`); beschreibbare Daten unter `%LOCALAPPDATA%\Compexx-InstallTool\`, Bundle-Ressourcen ueber `_MEIPASS` / `BUNDLE_DIR`.

## 4.0.0

- PyInstaller-Setup fuer distributable Windows-Build (one-folder) vorbereitet.
- Firmenbranding erweitert (Company-Header, optionales `assets/logo.png`, About-Dialog).
- Einstellungen-Dialog hinzugefuegt (schreibt `config.json`).
- Self-Health-Check beim Start integriert.
- Release-Dateien (`VERSION.txt`, `build.ps1`, `.spec`) ergaenzt.

## 3.0.0

- Dry-Run/Testmodus.
- `config.json`-Unterstuetzung.
- Laufhistorie in UI.
- Produktivmodus-Bestaetigung.

## 2.0.0

- CSV-Report nach Scan/Install.
- Retry-Strategie bei Chocolatey-Fehlern.
- Interne Installer mit Silent-Args.
- Verbesserte Erkennung und UI-Statusfarben.
