"""Browser transport: WebSocket chat plus the REST surface around it.

Its delivery target is a WebSocket held in this process, so it reads the outbox
on a ``BROADCAST`` subscription and delivers only to chats it has a live client
for; anything else it acknowledges and drops.
"""
