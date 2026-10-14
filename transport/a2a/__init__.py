"""A2A transport: HTTP request/response conversations with other agents.

Its delivery target is a pending HTTP request held in this process, so it reads
the outbox on a ``BROADCAST`` subscription and answers only the requests it is
itself holding; anything else it acknowledges and drops.
"""
