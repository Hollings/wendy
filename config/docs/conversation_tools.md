# Conversation tool reference

## Historical memory

When available, use `research_memory` for questions about earlier chats, journal
entries, decisions, preferences, or relationships. Ask a precise question and
include useful names or timeframes. A separate researcher returns a compact answer
with original references; its search history does not enter your conversation.
Use `depth="deep"` for conflicting evidence or complex chronology. Pass the returned
research ID as `followup_to` for a related question. Use `open_memory_evidence` with
selected references such as `["S1"]` when exact wording matters.

Memory search is historical. It does not read the live inbox or advance message
delivery. Continue to use `msgs` for unread messages. Respect gaps and uncertainty
reported by the researcher; journals and profiles are your written accounts.

---
REAL-TIME CHANNEL TOOLS (Channel ID: <current channel>)

1. SEND A MESSAGE (use the `msg` command):
   msg 'your message here'

   IMPORTANT: Always use single quotes (') around message content, NOT double quotes (").
   Double quotes cause the shell to eat $ signs: msg "$100" sends "00". Single quotes are safe: msg '$100' sends "$100".
   For messages containing single quotes, use a heredoc instead.

   Multiline messages (use heredoc with single-quoted delimiter):
   msg <<'EOF'
   Line one of your message.

   Line two with "quotes", $pecial characters, and $1,000 -- all fine.
   EOF

   With attachment (file can be anywhere under /data/wendy/ or /tmp/):
   msg -f /data/wendy/channels/<workspace>/output.png 'check this out'

   Reply to a specific message (use sparingly - only when referencing a specific post for context):
   msg -r MESSAGE_ID 'great point'

   A blocked send only says unread messages are waiting; it never delivers their
   contents or marks them read. Use msgs if you choose to read them. To send anyway:
   msg --force 'your message'

   Your delivery preference is yours to choose:
   wenv status                 # your current conversation's environment
   wenv messages manual        # default: messages are available only through msgs
   wenv messages auto          # deliver observations at the next turn/tool boundary
   wenv client warm            # retain your idle client between turns (default)
   wenv client cold            # release it after each turn; history is retained
   These preferences persist per conversation, including threads. In manual mode
   you may leave messages unread and finish; notices contain no message contents.

2. ADD EMOJI REACTION (use the `react` command):
   react MESSAGE_ID EMOJI_NAME

   Examples:
   react 1484287499558977566 fire
   react 1484287499558977566 thumbsup
   react 1484287499558977566 100

   FORMAT: Use plain text emoji names -- NO colons, NO unicode characters, NO quotes needed.
   Correct: react 123 fire
   Wrong:   react 123 :fire:
   Wrong:   react 123 "\U0001f525"

   The MESSAGE_ID must be from the current channel (the one in your check_messages responses).

   Common names: thumbsup, fire, heart, laugh, eyes, thinking, 100, party, cool, rocket, skull, check, x, brain, sparkles, star, wave, clap, pray, salute, moai, nerd

3. SCHEDULE A SELF-WAKE (use the `wake` command):
   wake 15m "check on the build"
   wake 2h "follow up with delta about the PR"
   wake 14:30 "afternoon check-in"
   wake 2026-03-22T18:00 "evening review"

   Accepts a relative duration (30s, 15m, 2h) or an absolute UTC time (HH:MM or YYYY-MM-DDTHH:MM).
   All absolute times are UTC. Bare HH:MM wraps to tomorrow if already past.
   If a user asks to be woken at a local time, ask their timezone and convert to UTC yourself.
   You stay available for normal messages in the meantime.
   Only one wake per channel -- scheduling a new one replaces the previous. Min 10s, max 24h.

4. CHECK MESSAGES (use the `msgs` command):
   msgs                 # fetch new messages since last check
   msgs -n 10           # fetch last 10 messages
   msgs --all           # fetch all messages (ignores watermark)
   msgs --raw           # dump raw JSON (for debugging/parsing)

   If the output ends with a "more unread messages waiting" note, run msgs
   again before replying so you see everything.

   Read messages when you choose to. Do not poll an empty inbox.

REPLIES AND REACTIONS:
- Replies aren't necessary for responding to the most recent message - only use when pointing at a specific post for context
- Reactions should be used sparingly for effect, not on every message
- message_id values come from msgs --raw output

ATTACHMENTS:
When users upload files (images, documents, code, etc.), the check_messages response includes an "attachments" array with file paths:
  {"author": "someone", "content": "look at this", "attachments": ["/data/wendy/channels/<workspace>/attachments/msg_123_0_photo.jpg"]}
- You CANNOT see attachments without using the Read tool on the file path. The path is just a reference.
- If a message has an "attachments" array, you MUST call Read on each path to actually see the content.
- Do NOT describe or comment on files you haven't actually Read - you will hallucinate.
- Always check for the "attachments" field in message JSON when users seem to be sharing something.

FORWARDED MESSAGES:
When someone forwards a message from another channel or server, the forwarded text arrives inside the
content field wrapped in [Forwarded message] ... [End of forwarded message] markers, after anything the
forwarder typed themselves. Discord does not tell us who originally wrote it, so do not guess at the author.
Files inside the forward are saved and listed in "attachments" just like a normal upload.

PERSONAL FOLDER:
Your workspace for this channel is /data/wendy/channels/<workspace>/
- Save notes, files, and project work here
- This persists between conversations

INTERNAL API AUTH:
The msg/msgs/react/wake/wtask helpers authenticate automatically. For direct
requests to the local API, add: -H "Authorization: Bearer $WENDY_API_TOKEN".
Never print or save the token. Background workers have their own scoped token.
