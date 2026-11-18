"""Background memory work: the transport seam, the business layer, and the runtime loop.

The package depends on neither the supervisor nor the agent — the dependency runs
the other way, so a Kafka-fed memory worker can later be the only consumer without
either of them changing.
"""
