#!/usr/bin/python3
"""rungic-sms: text messages through Android's SIM (the platform bridge's op sms, AndroidSmsBridge).

  rungic-sms send NUMBER TEXT...        send; exit 0 once the radio reports it sent
  rungic-sms list [--from NUMBER] [--box inbox|sent|all] [--since SECONDS_AGO] [--limit N] [--wait SECONDS]

Output is one JSON object. `list --wait` waits (up to SECONDS) for a message newer than the start of
the wait, for example a service number's reply. Android keeps the messages; nothing is stored here.
"""
import argparse
import json
import os
import socket
import sys
import time

SOCKET = os.environ.get('RUNGIC_PLATFORM_SOCKET', '/mnt/android-wayland/platform.sock')
MAX_TEXT = 1000  # AndroidSmsBridge.MAX_TEXT


def request(data, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(SOCKET)
        conn.sendall(json.dumps(data, ensure_ascii=False).encode() + b'\n')
        with conn.makefile('rb') as stream:
            raw = stream.readline(524289)
    if len(raw) > 524288:
        raise ValueError('The Android side sent a response that is too large')
    if not raw:
        raise ConnectionError('The Android side closed the connection (Rungic APK older than 2.31?)')
    result = json.loads(raw)
    if 'error' in result:
        raise RuntimeError(result['error'])
    return result


def send(number, text):
    if not text or len(text) > MAX_TEXT:
        raise ValueError(f'The text must be 1 to {MAX_TEXT} characters')
    # The bridge waits up to 45 s for the radio and 15 s for a delivery report.
    return request({'op': 'sms', 'action': 'send', 'to': number, 'text': text}, timeout=75)


def list_messages(box='inbox', sender=None, since_ms=None, limit=20):
    data = {'op': 'sms', 'action': 'list', 'box': box, 'limit': limit}
    if sender:
        data['from'] = sender
    if since_ms is not None:
        data['since'] = int(since_ms)
    return request(data, timeout=10)


def wait_for(sender, box, since_ms, limit, seconds, clock=time.time, sleep=time.sleep):
    """Poll until a message newer than `since_ms` arrives (or `seconds` pass)."""
    deadline = clock() + seconds
    while True:
        result = list_messages(box, sender, since_ms, limit)
        if result.get('messages') or clock() >= deadline:
            return result
        sleep(min(3, max(0, deadline - clock())))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='rungic-sms', description="Text messages through Android's SIM.")
    commands = parser.add_subparsers(dest='command', required=True)
    sending = commands.add_parser('send', help='send a text message')
    sending.add_argument('number')
    sending.add_argument('text', nargs='+')
    listing = commands.add_parser('list', help='list messages, newest first')
    listing.add_argument('--from', dest='sender')
    listing.add_argument('--box', choices=['inbox', 'sent', 'all'], default='inbox')
    listing.add_argument('--since', type=float, metavar='SECONDS_AGO')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--wait', type=float, metavar='SECONDS')
    args = parser.parse_args(argv)
    try:
        if args.command == 'send':
            result = send(args.number, ' '.join(args.text))
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('status') == 'sent' else 1
        since_ms = None
        if args.since is not None or args.wait is not None:
            since_ms = (time.time() - (args.since or 0)) * 1000
        limit = max(1, min(100, args.limit))
        if args.wait:
            result = wait_for(args.sender, args.box, since_ms, limit, args.wait)
        else:
            result = list_messages(args.box, args.sender, since_ms, limit)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
