"""Find names that a function uses but no module defines.

A function that refers to a missing global fails only when that line runs,
so a moved or deleted function can leave a break that no import shows.
This check reads every function's symbol table and reports each global name
that is neither defined in its module (after import) nor a builtin.

    python tests/check_names.py

Standard library only. Exit code 0 = no undefined names.
"""
import builtins
import importlib.util
import os
import symtable
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from anycam_modules import MODULES      # the files that make up the add-on
# (function, name) pairs that are known defects waiting for a decision.
# Empty since 3.0.0-rc1.3, which restored probe_http_identity (build plan B19).
KNOWN: set = set()


def _load(path: Path):
    os.environ.setdefault("INGRESS_PATH", "/api/hassio_ingress/TOKEN")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(path.stem, module)
    spec.loader.exec_module(module)
    return module


def _tables(table):
    yield table
    for child in table.get_children():
        yield from _tables(child)


def undefined_names(path: Path) -> list[tuple[str, int, str]]:
    """(function, line, name) for each global name with no definition."""
    source = path.read_text(encoding="utf-8")
    # camera_discovery.py is checked first; loading it loads the other files
    # and gives them the names they take from it (anycam_host.bind).
    module = sys.modules.get(path.stem) or _load(path)
    top = symtable.symtable(source, str(path), "exec")
    known = set(vars(module)) | set(dir(builtins))
    # A name some function creates with `global X; X = ...` exists at run time.
    for table in _tables(top):
        if table is not top:
            known |= {s.get_name() for s in table.get_symbols()
                      if s.is_declared_global() and s.is_assigned()}
    found = []
    for table in _tables(top):
        if table is top:
            continue
        for sym in table.get_symbols():
            if sym.is_referenced() and sym.is_global() and sym.get_name() not in known:
                found.append((table.get_name(), table.get_lineno(), sym.get_name()))
    return sorted(set(found), key=lambda f: (f[1], f[2]))


def main() -> int:
    bad = known = 0
    for name in MODULES:
        for func, line, missing in undefined_names(REPO / name):
            if (func, missing) in KNOWN:
                known += 1
                continue
            print(f"  FAIL  {name}:{line} {func}() uses undefined name {missing!r}")
            bad += 1
    print(f"  names: {len(MODULES)} module(s), {bad} new undefined name(s), "
          f"{known} known")
    print(f"{0 if bad else 1}/1 passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
