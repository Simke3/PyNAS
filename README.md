# PyNAS

Turn your PC into a small file server with a live dashboard. Add a folder, create a user, and open your files from your phone or any other device on your network.

PyNAS is a desktop app written in Python (PySide6). Its look is inspired by NAS dashboards, and it shares folders over HTTP.

> PyNAS is an independent project. It is not affiliated with, endorsed by or connected to TrueNAS or iXsystems.

## Features

- **Share folders** over your network: browse, download, upload and delete from a web browser
- **Works from a phone**: open the address PyNAS shows you, sign in, send files to your PC
- **Users and passwords**: sign-in is required, passwords are stored hashed (PBKDF2)
- **Read-only shares** for folders people may look at but not change
- **Live dashboard**: CPU per core, memory, disk space and network traffic
- Pages for Storage, Datasets, Shares, Credentials, Reporting and System

## Install

Requires Python 3.9 or newer.

```
pip install -r requirements.txt
python pynas.py
```

Keep `pynas.py`, `pynas.ico` and `pynas_icon_256.png` in the same folder so the app icon loads.

## Quick start

1. Open **Credentials** and add a user (username and password of at least 6 characters).
2. Open **Datasets** and create a folder, for example `documents`.
3. Open **Shares**, add a share that points at that folder (leave *Read-only* unchecked to allow uploads), then press **Start**.
4. On another device in the same network open the address shown in Shares, for example `http://192.168.1.15:8080`, and sign in.

Upload from a terminal:

```
curl -u username:password -T photo.jpg http://192.168.1.15:8080/documents/photo.jpg
```

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| F11 | Toggle full screen |
| Ctrl+Q | Quit |

Quitting PyNAS also stops the sharing service.

## Build a Windows .exe

```
pip install pyinstaller
pyinstaller --onefile --windowed --icon pynas.ico --add-data "pynas.ico;." --add-data "pynas_icon_256.png;." pynas.py
```

The result is in the `dist` folder. Antivirus software sometimes flags unsigned PyInstaller executables; this is a known false positive.

## Limitations

- **Connections are not encrypted.** PyNAS uses plain HTTP, so use it on a trusted home network. To reach it from outside, use a private network such as Tailscale. Do not forward a port on your router.
- The computer must be awake and PyNAS must be running for files to be available.
- PyNAS shares folders over HTTP only. It has no SMB or NFS drive mapping, ZFS, RAID or snapshots.
- Large uploads from a browser are sent in one piece; for multi-gigabyte files use `curl`.

## Settings location

Users, shares and the port are stored in `.pynas.json` in your home folder. Datasets are created in `~/PyNAS`.

## License

MIT, see [LICENSE](LICENSE). The software is provided as is, without warranty.
