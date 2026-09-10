from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = [(str(root / "src/translator/resources/web"), "translator/resources/web")]
binaries = []
hiddenimports = ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.h11_impl",
                 "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on",
                 "keyring.backends.Windows", "soundcard.mediafoundation"]
for name in ("ngrok", "soundcard", "keyring"):
    data, binary, imports = collect_all(name)
    datas += data
    binaries += binary
    hiddenimports += imports

a = Analysis([str(root / "packaging/entry.py")], pathex=[str(root / "src")],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["pytest", "ruff", "tkinter", "IPython"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Translator", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
agent_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TranslatorAgent", debug=False,
                bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, agent_exe, a.binaries, a.datas, strip=False, upx=False, name="Translator-windows-x64")
