# Delivery findings (Phase 0)

Tested against Claude Code 2.1.288 on Linux. All tests used a script posting to a session's inbox socket.

## Mechanism

- Every session binds a unix socket, path in `~/.claude/sessions/<pid>.json` (`messagingSocketPath`) and in `CLAUDE_CODE_MESSAGING_SOCKET` for the session's children.
- Wire format: one JSON line per message, `{"type":"user","message":{"role":"user","content":"<text>"}}`.
- Auth line `{"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}` is optional on Linux. The token is only given to the session's own children.
- The socket sends no reply, so delivery cannot be confirmed from the socket.

## Results

| Case | Result |
|---|---|
| Idle receiver, sender is a child of the session | Delivered, starts a new turn |
| Idle receiver, detached sender (`setsid`), no token, no auth line | Delivered, starts a new turn |
| Busy receiver | Delivered between tool calls, running work is not interrupted |
| Sender identity fields in the JSON (`from`, `from_name`, `fromName`) | Ignored, the receiver sees no sender name |
| Receiver in `bypassPermissions`, default settings, unattested sender | Held. An approval dialog opens in the receiver and the session shows status `waiting`. Nothing reaches Claude until a human approves |
| Same receiver started with `crossSessionInbound` set to `accept` | Delivered, starts a new turn |

## Consequences for the design

- The daemon needs only a session's socket path to deliver. No token or key files are needed.
- Sender identity must be part of the message text, for example `[room: name] author: ...`.
- Sessions in `bypassPermissions` must set `crossSessionInbound` to `accept` to receive room wake-ups. Otherwise wake-ups wait in an approval dialog and expire (default 5 minutes).
- Delivery is inferred, not acknowledged: watch the session `status` in its session file and the member's read cursor.
- Held messages are visible to the user in that session, which is the intended safety behavior.

## Not tested

- Behavior when a session is in `refuse` mode.
- Message bursts and the receiver's loop throttling (docs: repeated messages per sender are rate-limited, at most 50 queued).
- Other platforms and Claude Code versions.
