# Bind execution to accepted plans

Dev UI exposed P135: a child ran while its step stayed pending, cancellation was delivered late,
and a closed service cycle rejected the finished code. P136: a truncated result hid a ready artifact.
Bind root executor calls to explicit plan revisions/steps/attempts in the existing dispatch path;
keep semantic acceptance in ouroboros. Add immediate cancellation delivery, addressable artifact
listing, compact reads and double dev execution time budgets. No atom catalog or graph recovery.
