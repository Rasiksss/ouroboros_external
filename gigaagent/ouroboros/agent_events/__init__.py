"""The seam between a run and whoever consumes what it reports.

Neither side owns it: the runtime publishes, the supervisor consumes, and a package
belonging to either would make the other import it. Modelled on ``background_memory``,
whose transport seam answered the same questions first.
"""
