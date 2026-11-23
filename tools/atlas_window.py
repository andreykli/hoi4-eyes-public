"""Windows shortcut and dedicated Atlas window using the installed Edge runtime."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
from queue import Queue, Empty
from threading import Thread

HOTKEY_ID = 0x4559
DEFAULT_KEY = 'F10'


def open_atlas(profile, maker=False, *, fleet=False, meetings=False):
    executable = shutil.which('msedge')
    if not executable:
        for key in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            candidate = Path(os.environ.get(key, 'C:/Program Files')) / 'Microsoft/Edge/Application/msedge.exe'
            if candidate.is_file():
                executable = str(candidate)
                break
    if not executable:
        raise OSError('Microsoft Edge is required for the standalone Atlas window.')
    url = 'http://127.0.0.1:5016' + ('/meetings' if meetings else '/fleet' if fleet else '/world' + ('#name' if maker else ''))
    subprocess.Popen([executable, f'--user-data-dir={Path(profile) / "atlas-window"}', f'--app={url}'])


class AtlasHotkey:
    def __init__(self):
        self.native = ctypes.WinDLL('user32', use_last_error=True) if os.name == 'nt' else None
        self.key = None
        self.commands = Queue()
        self.events = Queue()
        self.worker = None
        self.last_error = 0

    def set(self, key):
        if key not in ('Off', 'F8', 'F9', 'F10', 'F11'):
            raise ValueError('Invalid Atlas key')
        if not self.native:
            if key == 'Off':
                return
            raise OSError('Atlas shortcuts require Windows.')
        if self.worker is None:
            self.worker = Thread(target=self.run, daemon=True)
            self.worker.start()
        result = Queue()
        self.commands.put((key, result))
        try:
            registered = result.get(timeout=5)
        except Empty as error:
            raise OSError('Atlas shortcut registration timed out.') from error
        if not registered:
            raise OSError(self.last_error, 'Atlas shortcut is already in use or unavailable.')

    def register(self, key):
        previous = self.key
        if previous == key:
            return True
        self.unregister()
        if key == 'Off':
            self.key = key
            return True
        ctypes.set_last_error(0)
        if self.native and self.native.RegisterHotKey(None, HOTKEY_ID, 0x4003, 0x6F + int(key[1:])):
            self.key = key
            self.last_error = 0
            return True
        self.last_error = ctypes.get_last_error()
        if (previous and previous != 'Off' and self.native
                and self.native.RegisterHotKey(None, HOTKEY_ID, 0x4003, 0x6F + int(previous[1:]))):
            self.key = previous
        return False

    def run(self):
        message = wintypes.MSG()
        while True:
            try:
                key, result = self.commands.get(timeout=0.05)
            except Empty:
                pass
            else:
                if key is None:
                    self.unregister()
                    return
                result.put(self.register(key))
            while self.native.PeekMessageW(ctypes.byref(message), ctypes.c_void_p(-1), 0x0312, 0x0312, 1):
                if message.wParam == HOTKEY_ID:
                    self.events.put(True)

    def pressed(self):
        try:
            return self.events.get_nowait()
        except Empty:
            return False

    def close(self):
        if self.worker:
            self.commands.put((None, None))
            self.worker.join(timeout=1)

    def unregister(self):
        if self.native and self.key not in (None, 'Off'):
            self.native.UnregisterHotKey(None, HOTKEY_ID)
        self.key = None
