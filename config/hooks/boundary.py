#!/usr/bin/env python3
"""One role-aware boundary hook; image analysis and async logging stay separate."""
import json
import os
import sys
import time
from pathlib import Path

import conversation_delivery
import task_mailbox


def main():
    started = time.monotonic()
    event = json.load(sys.stdin)
    worker = bool(os.getenv('WENDY_TASK_TOKEN'))
    try:
        if worker:
            task_mailbox.main(event)
        else:
            # Track successful explicit memory edits in this conversation only.
            response = event.get('tool_response')
            failed = isinstance(response, dict) and response.get('is_error', False)
            if event.get('tool_name') in ('Write', 'Edit') and not failed:
                path = event.get('tool_input', {}).get('file_path')
                journal = os.getenv('WENDY_MEMORY_JOURNAL')
                cid = os.getenv('WENDY_CHANNEL_ID')
                if path and journal and cid:
                    target = Path(path).resolve()
                    roots = [Path(journal).resolve(), Path('/data/wendy/claude_fragments/people')]
                    if any(target.is_relative_to(root) for root in roots):
                        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
                        from wendy.memory_reminders import record_write
                        record_write(int(cid))
            conversation_delivery.main(event)
    finally:
        # stderr is diagnostic, never an observation. No message bodies or tokens.
        print(json.dumps({'hook': event.get('hook_event_name'), 'worker': worker,
                          'duration_ms': round((time.monotonic() - started) * 1000)}), file=sys.stderr)


if __name__ == '__main__':
    main()
