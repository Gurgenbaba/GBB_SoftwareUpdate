# Release Guide

- Version in `VERSION.txt` und `CHANGELOG.md` aktualisieren.
- `python -m compileall .` ausfuehren.
- `pyinstaller -y --clean GBB_SoftwareUpdater.spec` ausfuehren.
- Ausgabe pruefen: `dist/GBB_SoftwareUpdater.exe`.
- SHA256 dokumentieren und Release verteilen.
