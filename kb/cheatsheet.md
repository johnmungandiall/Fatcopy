# Cheatsheet

## Run from source
```
python -m fatcopy --gui                        # the window
python -m fatcopy "D:\Photos" "E:\Backup"      # copy a folder
python -m fatcopy "C:\big.iso" "E:\"           # copy one file
```

## Build
```
python build.py --tests        # test, then build both executables into dist\
python build.py --target gui   # only dist\Fatcopy.exe
python build.py --onedir       # folder instead of single file (starts faster)
```

## Verify the project is healthy
```
python -m pytest -q                          # expected: 45 passed, 1 skipped
python -c "import fatcopy.gui"               # window module imports cleanly
python -m fatcopy --version
```

## Copy with the options that matter
```
fatcopy "D:\Photos" "E:\Backup" --verify content --exclude "*.tmp"
fatcopy "C:\Videos\film.mkv" "\\nas\media" --threads 4
fatcopy "E:\OldDrive" "E:\NewDrive" --move --json > report.json
fatcopy "D:\Data" "E:\Copy" --dry-run          # look first, write nothing
```

## Settings file (the window writes this)
```
%APPDATA%\Fatcopy\settings.json        # plus settings.json.bak
```
Corrupt it on purpose to check the recovery path: the file is moved to `.bak`
and the window starts with defaults.

## Where to look when something is wrong
| Symptom | First place to look |
|---|---|
| Speed is poor | `CopyOptions.threads` and `pipeline_min_bytes` — `fatcopy/core.py#CopyOptions.normalized` |
| A file was skipped | `CopyStats.skipped` reasons, set in `fatcopy/core.py#_apply_overwrite_policy` |
| Copy stopped early | `summary()["partial_files"]` — `fatcopy/core.py#_note_incomplete` |
| Long path fails | every probe must use `fatcopy/core.py#extended_path` |
| Timestamps missing | order inside `fatcopy/core.py#_apply_metadata` |
| Window hangs | a widget touched from a worker thread — see `FatcopyApp._pump` |
