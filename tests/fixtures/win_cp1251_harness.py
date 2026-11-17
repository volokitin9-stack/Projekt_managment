"""Run `python -m plan_agent ...` the way it behaves on Windows with a Russian code page:
every text-mode open() without an explicit encoding uses cp1251 (io.text_encoding(None)
returns "locale", as on Windows without UTF-8 mode).

Usage: python win_cp1251_harness.py <repository root> <plan_agent arguments...>
Any agent code that reads or writes text without encoding="utf-8" then behaves exactly
as on the Windows PC: it writes cp1251 or fails on characters such as «→»."""
import builtins
import io
import runpy
import sys

_open = io.open


def cp1251_default_open(file, mode="r", buffering=-1, encoding=None, *args, **kw):
    if "b" not in mode and encoding in (None, "locale"):
        encoding = "cp1251"
    return _open(file, mode, buffering, encoding, *args, **kw)


builtins.open = cp1251_default_open
io.open = cp1251_default_open
io.text_encoding = lambda encoding, stacklevel=2: encoding if encoding is not None else "locale"
sys.path.insert(0, sys.argv.pop(1))
sys.argv[0] = "plan_agent"
runpy.run_module("plan_agent", run_name="__main__", alter_sys=True)
