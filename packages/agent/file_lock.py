"""File locks shared by the web process and its tools on Windows and POSIX."""
import os
import time

LOCK_EX = 2
LOCK_NB = 4

def flock(file, flags):
    if os.name != 'nt':
        import fcntl
        return fcntl.flock(file, flags)
    import msvcrt
    if os.fstat(file.fileno()).st_size==0:
        file.write('\0'); file.flush()
    while True:
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if flags & LOCK_NB:
                raise BlockingIOError('文件正在被其他操作使用') from error
            time.sleep(.05)
