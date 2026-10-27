# File Sync Program

Windows app that copies a folder or network drive to a backup location, skipping files that haven't changed.

## Download and run

```
curl.exe -LO https://github.com/pf0952/file-sync-program/releases/latest/download/BackupProgram.exe
.\BackupProgram.exe
```

Works in Command Prompt and PowerShell. The exe isn't code-signed, so Windows may show a SmartScreen warning: click **More info**, then **Run anyway**.

## Build from source

```
pip install pyinstaller
pyinstaller --onefile --windowed --name BackupProgram backup_program.py
```
