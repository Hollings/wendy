"""Wendy's own durable, conversation-scoped environment preferences."""
from __future__ import annotations


def preferences(state, channel_id: int) -> dict:
    row = state._get_conn().execute(
        'SELECT message_delivery, keep_warm FROM conversation_environment WHERE channel_id=?', (channel_id,),
    ).fetchone()
    return {'messages': row['message_delivery'] if row else 'manual',
            'client': 'warm' if row is None or row['keep_warm'] else 'cold'}


def configure(state, channel_id: int, setting: str, value: str) -> dict:
    allowed = {'messages': ('manual', 'auto'), 'client': ('warm', 'cold')}
    if setting not in allowed or value not in allowed[setting]:
        raise ValueError('Use wenv messages manual|auto or wenv client warm|cold')
    conn = state._get_conn()
    conn.execute('INSERT OR IGNORE INTO conversation_environment(channel_id) VALUES (?)', (channel_id,))
    column = 'message_delivery' if setting == 'messages' else 'keep_warm'
    conn.execute(f'UPDATE conversation_environment SET {column}=? WHERE channel_id=?',
                 (value if setting == 'messages' else int(value == 'warm'), channel_id))
    conn.commit()
    return preferences(state, channel_id)
