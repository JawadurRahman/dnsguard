#!/usr/bin/env python3
"""
dnsguard: checks that NextDNS is active on the network and, if it isn't,
logs into the Bell Giga Hub and puts the NextDNS servers back.

Works on Linux (Raspberry Pi OS), macOS and Windows.

  python3 dnsguard.py            # check once
  python3 dnsguard.py --loop     # keep running, check every 60 seconds
  python3 dnsguard.py --dry-run  # log into the router, show DNS slots, change nothing

Your settings and the router password live outside this script, in ~/.dnsguard/
(Windows: C:\\Users\\<you>\\.dnsguard\\):
  config.ini   your settings - copy config.example.ini and fill it in
  router_pw    the router admin password, one line
Log is written to ~/.dnsguard/dnsguard.log
"""
import asyncio
import configparser
import datetime
import json
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from sagemcom_api.client import SagemcomClient
from sagemcom_api.enums import ApiMode, EncryptionMethod

# ---------- fixed settings (same for every Giga Hub) ----------
# Giga Hub DNS forwarding slots (captured from the router's own Save request)
FWD = "Device/DNS/Relay/Forwardings/Forwarding[@uid='{}']/{}"
DEFAULT_ISP_SLOTS = ["3", "4", "9", "10"]   # Bell's automatic DNS - keep these off
CONFIG_DIR = Path.home() / ".dnsguard"
ROUTER_PW_FILE = CONFIG_DIR / "router_pw"
CONFIG_FILE = CONFIG_DIR / "config.ini"
LOG_FILE = CONFIG_DIR / "dnsguard.log"


# ---------- your settings: ~/.dnsguard/config.ini (see config.example.ini) ----------
def _load_config():
    cp = configparser.ConfigParser()
    if not cp.read(CONFIG_FILE, encoding="utf-8-sig") or "dnsguard" not in cp:
        sys.exit(f"Missing {CONFIG_FILE} - copy config.example.ini there and fill it in.")
    c = cp["dnsguard"]
    missing = [k for k in ("router_mac", "nextdns_primary", "nextdns_secondary")
               if not c.get(k, "").strip()]
    if missing:
        sys.exit(f"{CONFIG_FILE}: please set {', '.join(missing)}")
    return c


_cfg = _load_config()
ROUTER_HOST = _cfg.get("router_host", "192.168.2.1")
ROUTER_USER = _cfg.get("router_user", "admin")
# dnsguard only touches the router when the device at ROUTER_HOST has this
# hardware (MAC) address, so it does nothing on other networks.
ROUTER_MAC = _cfg["router_mac"].strip()
NEXTDNS_PRIMARY = _cfg["nextdns_primary"].strip()
NEXTDNS_SECONDARY = _cfg["nextdns_secondary"].strip()
NEXTDNS_LINK_IP_URL = _cfg.get("link_ip_url", "").strip()
NTFY_URL = _cfg.get("ntfy_url", "").strip()
CHECK_EVERY_SECONDS = _cfg.getint("check_every_seconds", 60)
# -------------------------------

# The router API takes every value as a string; the library does str(value),
# so a Python True would be sent as "True". Send lowercase like the web UI.
WANTED = {
    FWD.format("1", "Enable"): "true",
    FWD.format("1", "DNSServer"): NEXTDNS_PRIMARY,
    FWD.format("2", "Enable"): "true",
    FWD.format("2", "DNSServer"): NEXTDNS_SECONDARY,
    **{FWD.format(uid, "Enable"): "false" for uid in DEFAULT_ISP_SLOTS},
}

if sys.platform == "win32":
    # aiohttp + the default Proactor loop on Windows can raise
    # "Event loop is closed" when the session is torn down.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def log(msg):
    CONFIG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg):
    if not NTFY_URL:
        return
    try:
        urllib.request.urlopen(
            urllib.request.Request(NTFY_URL, data=msg.encode(), method="POST"),
            timeout=10,
        )
    except Exception:
        pass


def link_ip():
    """IPv4 NextDNS only applies your profile if your public IP is linked."""
    if not NEXTDNS_LINK_IP_URL:
        return
    try:
        urllib.request.urlopen(NEXTDNS_LINK_IP_URL, timeout=10).close()
    except Exception:
        pass


def nextdns_on():
    # test.nextdns.io itself returns an HTML page that fetches a random
    # subdomain; that subdomain returns the JSON. Using a fresh random name
    # each time also sidesteps DNS caching.
    url = f"https://{uuid.uuid4().hex[:20]}.test.nextdns.io/"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.load(r).get("status") == "ok"
    except Exception:
        return False


def internet_up():
    """Plain TCP connect by IP, so it works even when DNS is broken."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=5).close()
        return True
    except OSError:
        return False


def router_mac():
    """MAC of ROUTER_HOST from the OS neighbour table, or None if not found."""
    try:   # a quick connection makes sure the table has a fresh entry
        socket.create_connection((ROUTER_HOST, 80), timeout=2).close()
    except OSError:
        pass
    try:
        if sys.platform == "win32":
            out = subprocess.run(["arp", "-a", ROUTER_HOST], capture_output=True,
                                 text=True, timeout=5).stdout
        else:
            out = Path("/proc/net/arp").read_text()
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == ROUTER_HOST:
            m = re.search(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", line, re.I)
            if m:
                return m.group(0).lower().replace("-", ":")
    return None


def on_home_network():
    return router_mac() == ROUTER_MAC.lower().replace("-", ":")


def _norm(v):
    return str(v).strip().lower()


async def _with_router(fn):
    password = ROUTER_PW_FILE.read_text(encoding="utf-8-sig").strip()
    async with SagemcomClient(
        ROUTER_HOST, ROUTER_USER, password, EncryptionMethod.SHA512,
        api_mode=ApiMode.LEGACY,
    ) as client:
        await client.login()
        try:
            return await fn(client)
        finally:
            try:
                await client.logout()   # free the router's session slot
            except Exception:
                pass


async def _read_slots(client):
    return await client.get_values_by_xpaths(
        {xp: xp for xp in WANTED}, ignore_unknown_paths=True
    )


def _mismatches(current):
    return {xp: (current.get(xp), want) for xp, want in WANTED.items()
            if _norm(current.get(xp)) != want}


def dry_run():
    async def run(client):
        current = await _read_slots(client)
        mac = router_mac()
        print(f"Router MAC: {mac}  ({'matches ROUTER_MAC' if on_home_network() else 'DOES NOT match ROUTER_MAC ' + ROUTER_MAC})")
        print("Router login OK. Forwarding slots:")
        for xp, want in WANTED.items():
            have = current.get(xp)
            mark = "ok " if _norm(have) == want else "XX "
            print(f"  {mark}{xp} = {have!r}   (want {want!r})")
        allf = await client.get_value_by_xpath(
            "Device/DNS/Relay/Forwardings", ignore_unknown_paths=True)
        print("\nAll forwarding entries on the router:")
        print(json.dumps(allf, indent=2, default=str))
        bad = _mismatches(current)
        print(f"\n{'Router already has NextDNS settings.' if not bad else f'{len(bad)} value(s) would be changed.'}"
              "  (dry run: nothing written)")
    asyncio.run(_with_router(run))


def restore_router_dns():
    """Talks to the Giga Hub's internal API directly (no browser).
    Repeats what the web UI sends when you save custom DNS, in one request.
    Returns the number of values changed (0 = router was already correct)."""
    async def run(client):
        bad = _mismatches(await _read_slots(client))
        if not bad:
            return 0
        await client.set_values_by_xpaths(WANTED)
        still_bad = _mismatches(await _read_slots(client))
        if still_bad:
            raise RuntimeError(f"router did not accept: {still_bad}")
        return len(bad)
    return asyncio.run(_with_router(run))


_was_on = None   # last known state, so --loop only logs "ON" when it changes
_away_logged = False


def main():
    global _was_on, _away_logged
    link_ip()

    if nextdns_on():
        if _was_on is not True:
            log("NextDNS ON")
        _was_on = True
        return
    _was_on = False
    if not internet_up():
        log("internet down, skipping")
        return
    time.sleep(5)               # avoid acting on a single blip
    if nextdns_on():
        return
    if not on_home_network():
        if not _away_logged:     # log once while away, not every minute
            log(f"NextDNS OFF but not on home network (router MAC {router_mac()}), skipping")
            _away_logged = True
        return
    _away_logged = False

    log("NextDNS OFF - checking router")
    try:
        changed = restore_router_dns()
    except Exception as e:
        log(f"restore failed: {e!r}")
        notify(f"NextDNS is OFF and restore FAILED: {e}")
        return

    if changed == 0:
        # Router is fine; the problem is elsewhere (this machine's DNS, IPv6 DNS,
        # IP not linked...). Don't keep rewriting the router.
        log("router already set to NextDNS; check fails for another reason")
        return

    log(f"router updated ({changed} values)")
    time.sleep(30)
    if nextdns_on():
        log("restored OK")
        notify("NextDNS was turned off at the router - restored automatically.")
    else:
        log("still off 30s after restore (may be DNS cache; will recheck)")
        notify("NextDNS router settings restored, but test still failing.")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    elif "--loop" in sys.argv:
        log("dnsguard started")
        try:
            while True:
                try:
                    main()
                except Exception as e:
                    log(f"error: {e!r}")
                time.sleep(CHECK_EVERY_SECONDS)
        except KeyboardInterrupt:
            log("dnsguard stopped")
    else:
        main()
