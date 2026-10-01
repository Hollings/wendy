#!/usr/bin/env python3
"""Deliver worker corrections at tool boundaries; guard unreported completion."""
import json
import os
import sys
import urllib.request


def main(event=None):
    token = os.getenv('WENDY_TASK_TOKEN')
    if not token:
        return
    event = event if event is not None else json.load(sys.stdin)
    port = os.getenv('WENDY_PROXY_PORT', '8945')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/tasks', data=b'{"command":"inbox"}',
                                     headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            inbox = json.load(response)
    except (OSError, ValueError):
        return  # An API outage cannot trap the worker in an endless hook loop.
    if inbox['phase'] == 'stopping':
        text = 'Stop requested. Preserve files, save a checkpoint if possible, and exit now.'
    elif inbox['messages']:
        text = 'Corrections from Wendy (read, incorporate, then wtask ack ID):\n' + json.dumps(inbox['messages'])
    elif event.get('hook_event_name') == 'Stop' and inbox['phase'] != 'finishing':
        text = 'Save your result with wtask finish, or wtask ask if blocked, before exiting. Include artifacts and verification.'
    else:
        return
    if event.get('hook_event_name') == 'Stop':
        if not event.get('stop_hook_active') and inbox['phase'] != 'stopping':
            print(json.dumps({'decision': 'block', 'reason': text}))
    else:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': text}}))


if __name__ == '__main__':
    main()
