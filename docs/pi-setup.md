# Raspberry Pi setup (Pi 3B+)

**Status: UNTESTED.** The install script and service file were written but never run on a real Pi.
Expect small problems on the first run. Tell the maintainer what you hit.

## What you need

- Raspberry Pi 3B+ (1 GB RAM)
- microSD card, 16 GB or bigger, A1 or A2 rated
- Power supply for the Pi (5 V, 2.5 A)
- An Ethernet cable. Wired is much more reliable than Wi-Fi. Wi-Fi drops show up as gaps in footage.
- Your U1 on the same network, with its IP address

## 1. Put the operating system on the card

1. On your computer, install **Raspberry Pi Imager**.
2. Choose **Raspberry Pi OS Lite (64-bit)**. Lite has no desktop, which saves RAM.
3. Click the gear icon (or "Edit settings") before writing and set:
   - hostname: `guardian`
   - enable **SSH** with a password
   - a username and password
   - Wi-Fi: leave off if you use the cable
   - time zone
4. Write the card, put it in the Pi, plug in the cable, then power.

## 2. Log in

After a minute or two, from your computer:

```
ssh YOURUSER@guardian.local
```

If `guardian.local` does not work, find the Pi's address in your router's device list and use that.

Check the clock is right (a wrong clock breaks video and event timing):

```
timedatectl
```

It should say `System clock synchronized: yes`.

## 3. Copy Guardian to the Pi

From your computer, in the folder that holds the project (cmd or PowerShell). This copies the
code only, not your secrets:

```
scp -r src packaging docs tools config.example.yaml pyproject.toml README.md LICENSE YOURUSER@guardian.local:u1-guardian/
```

(Make the folder first with `ssh YOURUSER@guardian.local mkdir u1-guardian`.)
Do **not** copy your `.env` file, your `.venv` folder, or `recordings`.

## 4. Install

On the Pi:

```
cd ~/u1-guardian
bash packaging/pi/install.sh
```

It will:

- install FFmpeg and Python tools
- install Guardian
- ask for your U1's IP address and write `/etc/u1-guardian/config.yaml`
- make a random access token and print it (save it, the dashboard asks for it once)
- keep the rolling camera buffer in RAM at `/dev/shm/u1-guardian`, so the SD card is only written when an incident happens
- start Guardian as a service that restarts on its own and starts at boot

Then open `http://guardian.local:8080/` on your computer.

## 5. The camera limit (important)

The U1 camera only gives pictures while the Snapmaker app is showing the camera view. (Whether the
printer's touchscreen does this too is UNVERIFIED.) If nobody is looking, there is no footage, and Guardian will say so on the incident
instead of pretending. Keep the camera view open on a phone or tablet during prints you care about.

## 6. Email alerts (optional)

Guardian does not include an email key. Make your own free Resend account and key, then:

```
sudo nano /etc/u1-guardian/env        # add: GUARDIAN_RESEND_KEY=your_key_here
sudo nano /etc/u1-guardian/config.yaml   # set notifications.email.enabled: true and to_addrs
sudo systemctl restart guardian
```

## 7. Gate 0 check (does the Pi keep up?)

Let it run for an hour with the camera view open on a phone, then check:

```
systemctl status guardian            # still running?
journalctl -u guardian --since "1 hour ago" | grep -i -E "error|warn"
vcgencmd measure_temp                # under 80 C
vcgencmd get_throttled               # should be throttled=0x0
free -m                              # memory available stays comfortable
top -bn1 | head -15                  # CPU is not pinned at 100 percent
```

Also open the dashboard and press "Save incident now". The clip should be `complete` after about
a minute. If the Pi cannot keep up, run Guardian on a regular computer instead. The code is the same.

## Useful commands

```
journalctl -u guardian -f            # live log
sudo systemctl restart guardian
sudo systemctl stop guardian
```
