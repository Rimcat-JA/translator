"""Single entry point for source and Windows packaged use."""

import argparse
import asyncio
import json
import importlib
import logging
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser

from translator.assets import ensure_frontend, source_root, web_directory
from translator.config import Settings, data_directory
from translator.instance import InstanceLock, contact_instance
from translator.logging_config import configure_logging


def notify_error(message: str):
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if (getattr(sys, "frozen", False) and os.name == "nt" and sys.stderr is None
            and not os.environ.get("TRANSLATOR_NO_DIALOG")):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Translator", 0x10)


def open_browser(url: str) -> bool:
    try:
        opened = webbrowser.open(url)
        if not opened:
            notify_error("ブラウザを開けませんでした。既定のブラウザを設定して再起動してください。")
        return opened
    except Exception:
        notify_error("ブラウザを開けませんでした。既定のブラウザを設定して再起動してください。")
        return False


def doctor(directory: Path):
    settings = Settings(directory)
    native = {}
    for module in (["ngrok", "soundcard"] if sys.platform == "win32" else ["ngrok"]):
        try:
            importlib.import_module(module)
            native[module] = "available"
        except Exception:
            native[module] = "unavailable"
    result = {
        "version": "0.2.0", "python": sys.version.split()[0], "platform": sys.platform,
        "packaged": bool(getattr(sys, "frozen", False)), "data_directory": str(directory),
        "ui_assets": (web_directory() / "index.html").is_file(),
        "secure_storage": settings.secrets.backend is not None,
        "providers": settings.secrets.public(),
        "loopback_supported": sys.platform == "win32",
        "running": contact_instance(directory, "status") is not None,
        "recovery_warning": settings.recovery_warning,
        "native_dependencies": native,
    }
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif getattr(sys, "frozen", False) and os.name == "nt":
        target = directory / "diagnostics.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not os.environ.get("TRANSLATOR_NO_DIALOG"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"診断結果を保存しました。\n{target}", "Translator", 0x40)
    return result


async def _start(args, settings: Settings, lock: InstanceLock):
    from translator.runtime import Runtime
    runtime = Runtime(settings, demo=args.demo, local_port=args.local_port,
                      hub_port=args.hub_port, development=args.command == "dev")
    dev_processes = []
    try:
        await runtime.start()
        lock.publish(runtime.local_origin, runtime.instance_id, runtime.instance_secret)
        if args.command == "dev":
            npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
            for surface, port in (("local", 5173), ("participant", 5174)):
                env = os.environ.copy() | {"TRANSLATOR_SURFACE": surface,
                    "TRANSLATOR_LOCAL_ORIGIN": runtime.local_origin,
                    "TRANSLATOR_HUB_ORIGIN": runtime.hub_origin,
                    "TRANSLATOR_API_ORIGIN": runtime.hub_origin if surface == "participant" else runtime.local_origin}
                dev_processes.append(subprocess.Popen([npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
                    cwd=source_root() / "frontend", env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        if not args.no_browser:
            origin = "http://127.0.0.1:5173" if args.command == "dev" else runtime.local_origin
            open_browser(origin + "/#bootstrap=" + runtime.auth.issue_bootstrap())
        if sys.stdout is not None:
            print(f"Translator ready: {runtime.local_origin} (audio is stopped)", flush=True)
        logging.getLogger("translator").info("RUNTIME_READY")
        await runtime.wait()
    finally:
        for process in dev_processes:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                process.kill()
        await runtime.close()


def main():
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="Translator — 字幕とPC音声共有")
    parser.add_argument("command", nargs="?", choices=("start", "dev", "doctor", "stop"), default="start")
    parser.add_argument("--demo", action="store_true", help="完全ローカルのデモを選択して起動")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--local-port", type=int, default=8765)
    parser.add_argument("--hub-port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, help="設定ディレクトリ（試験・分離起動用）")
    args = parser.parse_args()
    if args.no_browser:
        os.environ["TRANSLATOR_NO_DIALOG"] = "1"
    if args.data_dir:
        os.environ["TRANSLATOR_DATA_DIR"] = str(args.data_dir.resolve())
    directory = data_directory()
    configure_logging(directory)
    if args.command == "doctor":
        doctor(directory)
        return
    if args.command == "stop":
        result = contact_instance(directory, "stop")
        if sys.stdout is not None:
            print("停止を要求しました。" if result else "起動中のTranslatorはありません。")
        return
    lock = InstanceLock(directory)
    if not lock.acquire():
        # A second process may arrive while the first is still building the UI.
        result = None
        for _ in range(20):
            result = contact_instance(directory)
            if result:
                break
            time.sleep(0.25)
        if result:
            if not args.no_browser:
                open_browser(result["url"])
            if sys.stdout is not None:
                print("起動中のTranslatorを再表示しました。")
            return
        notify_error("Translatorは起動処理中、または応答待ちです。しばらく待って再起動してください。")
        raise SystemExit(1)
    try:
        ensure_frontend()
        asyncio.run(_start(args, Settings(directory), lock))
    except KeyboardInterrupt:
        pass
    except (RuntimeError, ValueError) as error:
        # Only our own bounded error codes/messages are shown, never API response bodies.
        message = str(error) if type(error) in (RuntimeError, ValueError) else "STARTUP_FAILED"
        notify_error(f"{message}\nログ: {directory / 'runtime.log'}")
        logging.getLogger("translator").error("STARTUP_FAILED")
        raise SystemExit(1) from None
    except Exception:
        notify_error(f"起動に失敗しました。診断コマンドを実行してください。\nログ: {directory / 'runtime.log'}")
        logging.getLogger("translator").error("STARTUP_FAILED_UNEXPECTED")
        raise SystemExit(1) from None
    finally:
        lock.release()


if __name__ == "__main__":
    main()
