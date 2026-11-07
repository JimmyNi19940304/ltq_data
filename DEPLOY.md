# Running Ladder Index on an Ubuntu server

Tested on Ubuntu 22.04 and 24.04, 64-bit, with Python 3.9 or newer.

## 1. System packages

One command, once:

```bash
sudo apt update
sudo apt install -y python3-venv unzip curl libicu-dev
```

`libicu` is the one people miss. ThermoRawFileParser is a self-contained .NET
program and will not start without it, with an error that does not mention it.

## 2. Setup

From inside the unzipped folder:

```bash
bash setup.sh
```

It asks where the data should live, then creates a virtual environment, installs
the Python libraries, downloads ThermoRawFileParser, writes `config.json` and
checks that all of it works. Takes two or three minutes, mostly downloading.

Pass the folder as an argument to skip the question:

```bash
bash setup.sh /srv/ltq-data
```

Use `bash setup.sh` rather than `./setup.sh` the first time: a `.zip` does not
always carry the executable bit. The script fixes that for itself and the others
as it runs.

Nothing is installed system-wide. Everything lives in this folder and the data
folder you name, so removing them removes the installation.

## 3. Your ladder files

Copy the ladder `.raw` files into the `ladders` folder setup created — from
another machine:

```bash
scp *.raw you@server:/srv/ltq-data/ladders/
```

Compound `.raw` files do not have to live there. In the app you can either upload
them through the browser or type a path that is already on the server; a path is
much quicker, since a 250 MB upload through a browser buys you nothing when the
file is already on the machine.

## 4. Start it

**To try it out**, with the apps stopping when you close the terminal:

```bash
./run.sh
```

**To run it properly**, starting on boot and restarting after a crash:

```bash
./install-services.sh
```

That installs two systemd services, `ltq-index` and `ltq-calibrate`, running as
your own user rather than root.

```bash
sudo systemctl status ltq-index        # is it running
sudo journalctl -u ltq-index -f        # watch the log
sudo systemctl restart ltq-index ltq-calibrate
./install-services.sh --remove         # uninstall them
```

Both scripts serve on every network interface so you can reach the server from
your own machine. For one machine only, set `HOST=127.0.0.1` before either
command. To change ports, set `MAIN_PORT` and `CAL_PORT`.

## 5. Firewall

If `ufw` is enabled, open the two ports. Restrict them to your own network rather
than the whole internet:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 5000,5001 proto tcp
```

This matters more than it looks. The path fields let anyone who can open the page
ask the server to read any file your account can read, which is exactly what you
want on a lab network and exactly what you do not want on a public address. Put
it on a trusted network, or behind a VPN, and do not forward these ports.

## 6. Open the pages

| | |
| --- | --- |
| Compounds | `http://<server>:5000` |
| Calibration | `http://<server>:5001` |

`hostname -I` on the server gives you its address.

The usual order is calibration first — read the ladders, pick a standard, save
it — then the compounds app, which puts every compound on that standard's time
axis.

## Where everything lives

```
<install folder>/
    .venv/                  the Python libraries        ~390 MB
    tools/                  ThermoRawFileParser         ~110 MB
    workspace/scan-cache/   converted scans             ~65 MB per .raw
    config.json             which data folder to use

<data folder>/
    ladders/                your ladder .raw files
    standard_ladder/        the saved standard, and archived previous ones
    compound/               normalized results, one folder per compound
```

Reading a `.raw` takes about 25 seconds the first time and is then cached, so the
same file is instant afterwards. The cache grows by roughly 65 MB per file and is
never cleaned up on its own. It is safe to delete whenever you want the space
back — files are simply re-read on next use:

```bash
rm -rf workspace/scan-cache/*
```

Budget around 1 GB for the installation plus 65 MB for each `.raw` file you
process. Results in the data folder are small.

## If something goes wrong

**The page says the .raw reader is missing.** `libicu` is usually the cause:

```bash
sudo apt install -y libicu-dev
./tools/ThermoRawFileParser/ThermoRawFileParser --version   # should print 2.0.0.0
```

**Setup could not download the parser.** Some networks block `api.github.com`
while allowing ordinary downloads; the script already falls back to a direct
link. If both are blocked, copy `tools/ThermoRawFileParser/` from a machine that
has it, then `chmod +x tools/ThermoRawFileParser/ThermoRawFileParser`.

**Nothing responds from another machine.** Check the apps are bound to all
interfaces, not just localhost:

```bash
ss -ltn | grep -E '5000|5001'     # want 0.0.0.0:5000, not 127.0.0.1:5000
```

Then check the firewall with `sudo ufw status`.

**Port already in use.** `sudo ss -ltnp | grep 5000` names the process. Either
stop it, or run on other ports with `MAIN_PORT` and `CAL_PORT`.

**A service will not start.** `sudo journalctl -u ltq-index -n 50 --no-pager`
gives the reason. Most often the install folder has moved, or the data folder in
`config.json` no longer exists.

**Out of disk.** Clear the scan cache as above; it is the only thing that grows.

## Updating

Unzip the new version over the old folder, keeping `config.json`, `workspace/`
and `tools/`, then:

```bash
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart ltq-index ltq-calibrate
```

Your data folder is untouched by an update.
