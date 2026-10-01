#!/usr/bin/env python3
"""Manual unread notices, or opt-in automatic observations at safe boundaries."""
import json
import os
import sys
import urllib.request


def main(event=None):
    token = os.getenv('WENDY_API_TOKEN')
    if not token or os.getenv('WENDY_ENRICHMENT') == '1':
        return
    event = event if event is not None else json.load(sys.stdin)
    name = event.get('hook_event_name')
    if name == 'Stop' and event.get('stop_hook_active'):
        return
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.getenv('WENDY_PROXY_PORT', '8945')}/api/message_delivery",
        data=json.dumps({'hook': name}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            delivery = json.load(response)
    except (OSError, ValueError):
        return
    text = delivery.get('observation')
    if not text:
        return
    if name == 'Stop':
        print(json.dumps({'decision': 'block', 'reason': text}), flush=True)
    else:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}), flush=True)
    if delivery.get('delivery_id'):
        # Publish the observation before acknowledging it. A lost response is
        # retried instead of silently advancing the unread cursor.
        req.data = json.dumps({'ack': delivery['delivery_id']}).encode()
        try:
            with urllib.request.urlopen(req, timeout=3):
                pass
        except OSError:
            pass


if __name__ == '__main__':
    main()
