"""Read-only proof that a world export still belongs to the running HOI4 timeline."""
import ctypes
from ctypes import wintypes
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import time

PREFIX=re.compile(rb'^\[\d\d:\d\d:\d\d\]\[(\d{4})\.(\d{2})\.(\d{2})\.\d{2}\]')
LIMIT=1024*1024
MAX_GAME_DAYS=3
MAX_IDLE_SECONDS=1800


def process_stamp(pid):
    if os.name!='nt':
        return None
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:
        return None
    try:
        stamps=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*(ctypes.byref(value) for value in stamps)):
            return None
        return (stamps[0].dwHighDateTime<<32)|stamps[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def game_date(line):
    match=PREFIX.match(line)
    return date(*map(int,match.groups())).toordinal() if match else None


def capture(path,line,pid):
    marker=line.strip().encode('utf-8')
    try:
        day=game_date(marker)
        started=process_stamp(pid)
        if not day or not started or b'EYES_WORLD_V1|END|' not in marker:
            return None
        path=Path(path).resolve()
        with path.open('rb') as stream:
            stat=os.fstat(stream.fileno())
            start=max(0,stat.st_size-LIMIT)
            stream.seek(start)
            tail=stream.read(LIMIT)
        offset=tail.rfind(marker)
        if offset<0 or (offset and tail[offset-1:offset]!=b'\n'):
            return None
        return {'path':str(path),'device':stat.st_dev,'inode':stat.st_ino,'offset':start+offset,
                'length':len(marker),'hash':hashlib.sha256(marker).hexdigest(),'game_date':day,
                'process':started,'captured':time.time()}
    except (OSError,TypeError,ValueError):
        return None


def still_current(proof,pid,token,player,snapshot_day,*,max_idle_seconds=MAX_IDLE_SECONDS):
    """Require an unchanged native marker and at most three subsequent game days."""
    try:
        age=time.time()-proof['captured']
        if (age<0 or (max_idle_seconds is not None and age>max_idle_seconds)
                or process_stamp(pid)!=proof['process']):
            return False
        with Path(proof['path']).open('rb') as stream:
            stat=os.fstat(stream.fileno())
            offset,length=proof['offset'],proof['length']
            if (stat.st_dev,stat.st_ino)!=(proof['device'],proof['inode']):
                return False
            if not (0<length<=2048 and 0<=offset<=stat.st_size-length and stat.st_size-offset<=LIMIT):
                return False
            stream.seek(offset)
            marker=stream.read(length)
            if hashlib.sha256(marker).hexdigest()!=proof['hash']:
                return False
            if (b'EYES_WORLD_V1|END|'+token.encode('ascii')+b'|') not in marker:
                return False
            tail=stream.read(LIMIT)
        anchor=game_date(marker)
        if anchor!=proof['game_date']:
            return False
        for line in tail.splitlines():
            day=game_date(line)
            if day is not None and not anchor<=day<=anchor+MAX_GAME_DAYS:
                return False
            if any(marker in line for marker in (
                b'EYES_WORLD_V1|BEGIN|', b'EYES_HOI4_CAMPAIGN_FRESH_V1|',
                b'Resetting game', b'[[ Launching SINGLEPLAYER-game ]]',
            )):
                return False
            if b'EYES_AUTONOMY_V2|BEGIN|' in line:
                seed,_,native_day,actor,_=line.split(b'EYES_AUTONOMY_V2|BEGIN|',1)[1].strip().split(b'|')
                if (seed.decode('ascii')!=token or actor.decode('ascii')!=player
                        or not snapshot_day<=int(native_day)<=snapshot_day+MAX_GAME_DAYS):
                    return False
        return True
    except (OSError,TypeError,ValueError,KeyError,AttributeError):
        return False
