"""Drive the installer prompts in a disposable test container using a real PTY."""
import os
import pty
import select
import signal
import sys
import time

prompts = [
    ('Mode [1]: ', '1'),
    ('Existing AWG config [', ''),
    ('Interface name [', ''),
    ('Public server hostname or IP: ', 'example.com'),
    ('Public UDP port [', ''),
    ('Telegram numeric owner ID: ', '123456'),
    ('Telegram Bot Token: ', '123456:TEST_TOKEN'),
    ('Telegram topic ID (blank for ordinary private chat): ', ''),
    ('Bot runtime: standalone or openclaw [standalone]: ', 'standalone'),
    ('Apply this configuration? [yes/NO]: ', 'yes'),
]
pid, fd = pty.fork()
if pid == 0:
    os.execv('/bin/bash', ['bash', '/work/install.sh'])
buffer = ''
index = 0
deadline = time.monotonic() + 300
try:
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 1)[0]:
            try:
                data = os.read(fd, 65536).decode(errors='replace')
            except OSError:
                break
            if not data:
                break
            print(data, end='', flush=True)
            buffer += data
            if index < len(prompts) and prompts[index][0] in buffer:
                os.write(fd, (prompts[index][1] + '\n').encode())
                index += 1
                buffer = ''
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            sys.exit(os.waitstatus_to_exitcode(status))
    else:
        os.kill(pid, signal.SIGKILL)
        raise SystemExit(f'Installer timed out at prompt {index}')
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))
finally:
    os.close(fd)
