# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_root = Path.cwd()
customtkinter_datas = collect_data_files("customtkinter")

datas = list(customtkinter_datas)
if (project_root / "config.example.json").exists():
    datas.append((str(project_root / "config.example.json"), "."))
if (project_root / "config.json").exists():
    datas.append((str(project_root / "config.json"), "."))
if (project_root / "VERSION.txt").exists():
    datas.append((str(project_root / "VERSION.txt"), "."))
if (project_root / "assets").exists():
    datas.append((str(project_root / "assets"), "assets"))
icon_path = project_root / "assets" / "logo.ico"
png_icon_path = project_root / "assets" / "logo.png"
generated_icon_path = project_root / "assets" / "_generated_logo.ico"
exe_icon = None
if icon_path.exists():
    exe_icon = str(icon_path.resolve())
elif png_icon_path.exists():
    try:
        from PIL import Image  # type: ignore

        with Image.open(png_icon_path) as img:
            converted = img.convert("RGBA")
            converted.save(generated_icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        exe_icon = str(generated_icon_path.resolve())
    except Exception:
        exe_icon = None

a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=["darkdetect"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="GBB_SoftwareUpdater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=exe_icon,
)
