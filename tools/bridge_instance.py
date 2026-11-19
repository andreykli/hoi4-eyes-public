"""One Bridge owner per Experimental response channel; OS releases lock on exit."""
from contextlib import contextmanager
from pathlib import Path
import os

@contextmanager
def bridge_instance_lock(path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size==0:
            handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:yield True
        finally:
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_UN)

def unique_messages(messages):
    """Transport replay is identified by request, speaker and kind, never text."""
    result=[]; positions={}
    for message in messages:
        request=message.get('request_id')
        key=(request,message.get('speaker'),message.get('kind')) if request else None
        if key is not None and key in positions:
            result[positions[key]]=message
        else:
            if key is not None:positions[key]=len(result)
            result.append(message)
    return result
