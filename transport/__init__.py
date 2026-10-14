"""Chat transports: the seam between user-facing transports and the supervisor.

The package depends on neither the supervisor nor the agent — the dependency runs
the other way, so a Kafka-backed gateway can later serve multiple instances
without any transport changing. See ``interfaces.py`` for the seam itself; the
concrete transports (dialogs, browser, a2a, skill bridge) migrate here from
``supervisor/messaging/`` and ``ouroboros/gateway|ingress/``.
"""
