#!/usr/bin/python3
"""Attach the manager plugin to an existing OpenClaw installation."""
import argparse
import json
import os
from pathlib import Path
import pwd
import tempfile


def atomic(path: Path, value: str, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner-id', required=True, type=int)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--topic-id', type=int)
    parser.add_argument('--openclaw-home', type=Path, default=Path('/var/lib/openclaw'))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    account = pwd.getpwnam('openclaw')
    openclaw_config = args.openclaw_home / '.openclaw/openclaw.json'
    if not args.token_file.is_file() or not openclaw_config.is_file():
        raise SystemExit('OpenClaw config or Telegram token file was not found')
    config = json.loads(openclaw_config.read_text())
    atomic(openclaw_config.with_suffix('.pre-awg-manager'), openclaw_config.read_text(),
           0o600, account.pw_uid, account.pw_gid)
    telegram_channel = config.setdefault('channels', {}).setdefault('telegram', {})
    telegram_channel['enabled'] = True
    telegram_channel['tokenFile'] = str(args.token_file)
    telegram_channel.pop('botToken', None)
    telegram_channel['dmPolicy'] = 'allowlist'
    telegram_channel['allowFrom'] = [str(args.owner_id)]
    telegram_channel['groupPolicy'] = 'disabled'
    actions = telegram_channel.setdefault('actions', {})
    actions['editForumTopic'] = False
    actions['createForumTopic'] = False
    plugins = config.setdefault('plugins', {})
    allow = plugins.setdefault('allow', [])
    if 'awg-telegram-manager' not in allow:
        allow.append('awg-telegram-manager')
    paths = plugins.setdefault('load', {}).setdefault('paths', [])
    plugin_path = '/opt/awg-telegram-manager/openclaw-plugin'
    if plugin_path not in paths:
        paths.append(plugin_path)
    plugins.setdefault('entries', {})['awg-telegram-manager'] = {'enabled': True}
    atomic(openclaw_config, json.dumps(config, indent=2) + '\n', 0o600, account.pw_uid, account.pw_gid)

    telegram = {'owner_id': args.owner_id, 'thread_id': args.topic_id,
                'token_file': str(args.token_file), 'helper': '/usr/local/sbin/awg-manager'}
    atomic(Path('/etc/awg-manager/telegram.json'), json.dumps(telegram, indent=2) + '\n',
           0o640, 0, account.pw_gid)
    settings_path = Path('/etc/awg-manager/config.json')
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings['endpoint'] = args.endpoint
    atomic(settings_path, json.dumps(settings, indent=2) + '\n', 0o600, 0, 0)
    notifications = {'chat_id': args.owner_id, 'thread_id': args.topic_id,
                     'token_file': str(args.token_file)}
    atomic(Path('/var/lib/awg-manager/notifications.json'), json.dumps(notifications, indent=2) + '\n',
           0o600, 0, 0)
    print('OpenClaw adapter configured without displaying secrets')


if __name__ == '__main__':
    main()
