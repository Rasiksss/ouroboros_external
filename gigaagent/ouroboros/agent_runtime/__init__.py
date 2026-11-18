"""The component that claims durable tasks and runs them.

Everything that decides a task is this instance's and executes it lives here: the slots,
the agent construction, the per-run stop signals, the drain, and the loop over them. The
dependency points one way — this package names the supervisor's durable store and its
queue, and no module under ``supervisor/`` names this package or the agent it builds.

Deliberately empty of re-exports: every module here imports the agent, so a package
attribute would drag the whole agent in behind any import of any part of it.
"""
