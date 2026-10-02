"""Late binding between camera_discovery.py and the modules split from it.

camera_discovery.py is the entry point and imports the other modules, so
they cannot import it back: a second copy of it would load, with its own
separate cameras and state. Two mechanisms connect them the other way:

  NEEDS  each module lists the camera_discovery.py names it uses. bind() sets
         those names on the module once, at start-up. Right for functions and
         for objects that are changed in place (CAMERAS, the logger).
  H      reads a camera_discovery.py value at the moment of use. Needed for
         the few values that file replaces while it runs (the focused camera).

The release gate fails if a NEEDS name is one that is replaced at run time.
"""


class _Host:
    """Attribute access to camera_discovery.py's module globals, live."""

    def __init__(self) -> None:
        object.__setattr__(self, "_globals", None)

    def __getattr__(self, name: str) -> object:
        g = object.__getattribute__(self, "_globals")
        if g is None:
            raise RuntimeError("anycam_host.bind() has not run yet")
        try:
            return g[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("H is read-only; camera_discovery.py owns these values")


H = _Host()


def bind(main_globals: dict, *modules: object) -> None:
    """Give each module the camera_discovery.py names in its NEEDS list."""
    object.__setattr__(H, "_globals", main_globals)
    for module in modules:
        for name in module.NEEDS:
            setattr(module, name, main_globals[name])
