# Release Guide

- Version in `VERSION.txt` und `CHANGELOG.md` aktualisieren.
- `python -m compileall .` ausfuehren.
- `pyinstaller -y --clean Compexx-InstallTool.spec` ausfuehren.
- Ausgabe pruefen: `dist/Compexx-InstallTool.exe`.
- SHA256 dokumentieren und Release verteilen.
