"""The resident service: one per user and Theodore home (DESIGN §3.1, contracts C1 and C2).

`theodore service` runs this package in the runtime's Python. It holds the endpoint the relay and the front talk to,
the sessions, the licence claims in memory, the texts in memory (texts.py), the warm engine workers and the timers.
Standard library only, like the rest of the client; it never imports the engine.
"""
