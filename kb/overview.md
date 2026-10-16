# Fatcopy — overview

A Windows file-copy tool written in Python: a window application plus a command
line tool, both built from one copy engine. It is fast because whole files are
copied in parallel by a pool of worker threads, and a single large file is read
and written at the same time; it is trustworthy because every new copy is
checked and anything that failed or was left half-written is named at the end.

## Why it exists
The user asked for "a Windows software to copy fast, in Python". Everything is
standard library (`tkinter`, `threading`, `ctypes`) — no third-party runtime
dependency — so the built executable needs nothing installed on the target
machine.

## Running it from source
```
python -m fatcopy --gui                      # the window
python -m fatcopy "D:\Photos" "E:\Backup"    # command line
```

## Building the two executables
```
python build.py --tests
```
produces `dist\Fatcopy.exe` (window application — the one to double-click) and
`dist\fatcopy-cli.exe` (console tool for scripts). Two executables, because a
windowed program has no console to print to and a console program would flash a
black window behind its dialogs. See [[features/build]] for the switches.

## Where things live
- [[architecture]] — the pieces and how they fit.
- [[features/copy-engine]] — the engine in detail (the part worth reading).
- [[features/gui]] — the window and its threading rules.
- [[features/cli]] — options and exit codes.
- [[gotchas]] — traps to know before editing.
- [[conventions]] — how the code and these notes are organised.
- [[cheatsheet]] — the commands reached for most.
- [[changelog]] — the only place history lives.
- [[about-you]] — the user: style, preferences, rules.
