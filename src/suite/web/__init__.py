"""The web UI: a thin read-and-review layer over the engine's run folders.

``settings`` holds the server configuration, ``service`` is the one module that talks to the
engine (it reads run folders and returns plain data). Nothing here sends a prompt or changes how
a verdict is decided.
"""
