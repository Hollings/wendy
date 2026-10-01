"""Optional real CLI contract against a local fake API; no paid model requests."""
import asyncio
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.skipif(not os.getenv('WENDY_TEST_CLAUDE'), reason='Set WENDY_TEST_CLAUDE to test the installed CLI')
async def test_real_cli_accepts_two_turns_without_closing_stdin(tmp_path):
    hook = tmp_path / 'hook.py'
    hook.write_text('''import json,sys
from pathlib import Path
event=json.load(sys.stdin)
with Path('hook-events.txt').open('a') as out: out.write(event['hook_event_name']+'\\n')
print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':'INBOX_FIXTURE_OBSERVATION'}}))
''')
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'hooks': {'UserPromptSubmit': [{'hooks': [
        {'type': 'command', 'command': f'python "{hook}"'}]}]}}))
    observed = []
    class API(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if '/count_tokens' in self.path:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"input_tokens":10}')
                return
            observed.append('INBOX_FIXTURE_OBSERVATION' in json.dumps(data.get('messages')))
            message = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant',
                       'model': data.get('model'), 'content': [], 'stop_reason': None, 'stop_sequence': None,
                       'usage': {'input_tokens': 10, 'output_tokens': 1}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            events = [
                {'type': 'message_start', 'message': message},
                {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Fixture response.'}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 3}},
                {'type': 'message_stop'},
            ]
            for event in events:
                self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), API)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'CLAUDE_', 'WENDY_'))}
    env.update(ANTHROPIC_API_KEY='isolated-fixture-key', ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
               CLAUDE_CONFIG_DIR=str(tmp_path / 'claude'), HOME=str(tmp_path),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1')
    proc = await asyncio.create_subprocess_exec(
        os.environ['WENDY_TEST_CLAUDE'], '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
        '--verbose', '--model', 'haiku', '--strict-mcp-config', '--session-id', str(uuid.uuid4()),
        '--settings', str(settings),
        env=env, cwd=tmp_path, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, limit=1024 * 1024,
    )
    try:
        for _ in range(2):
            envelope = {'type': 'user', 'message': {'role': 'user', 'content': 'Fixture wake.'},
                        'parent_tool_use_id': None, 'session_id': ''}
            proc.stdin.write((json.dumps(envelope) + '\n').encode())
            await proc.stdin.drain()
            async with asyncio.timeout(35):
                while True:
                    line = await proc.stdout.readline()
                    assert line, 'CLI exited instead of accepting another turn'
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('type') == 'result':
                        assert not event.get('is_error'), event
                        break
            assert proc.returncode is None
        assert (tmp_path / 'hook-events.txt').read_text().count('UserPromptSubmit') == 2
        assert len(observed) >= 2 and all(observed)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        server.shutdown()
        server.server_close()
