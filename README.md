# dnsguard

Every 60 s checks `https://test.nextdns.io`. If NextDNS isn't active, it logs into the
Bell Giga Hub (192.168.2.1) and sets DNS forwarding slots 1/2 back to NextDNS and turns
off Bell's slots 3/4/9/10. Before it writes anything it reads the router. If the router
already has the NextDNS settings, it only logs that and doesn't rewrite them.

Your settings and secrets live in `~/.dnsguard/`, outside the repo:

- `config.ini`: your router's MAC, NextDNS IPv4 addresses, linked-IP URL, optional
  ntfy alerts. Copy [config.example.ini](config.example.ini) and fill it in.
- `router_pw`: the router admin password (one line, only the password).
- `dnsguard.log`: the log.

Never commit `config.ini` or `router_pw`; the linked-IP URL contains a secret token.

- Home-network check: dnsguard only touches the router when the device at 192.168.2.1
  has the hardware address in `router_mac`. On other networks it logs once and skips.
  If you replace the router, update `router_mac`; `--dry-run` prints the current one.
- The machine running dnsguard must use the router's DNS (the DHCP default), not a
  hard-coded DNS or a VPN. Otherwise the check tests the wrong resolver.

## Windows (testing)

```powershell
cd $HOME\Downloads\dnsguard
py -m venv venv
.\venv\Scripts\pip install -r requirements.txt
mkdir $HOME\.dnsguard -Force
copy config.example.ini $HOME\.dnsguard\config.ini
notepad $HOME\.dnsguard\config.ini     # fill in your values, save
New-Item $HOME\.dnsguard\router_pw -ItemType File -Force   # create first, so Notepad doesn't add .txt
notepad $HOME\.dnsguard\router_pw      # type the router password, save
.\venv\Scripts\python dnsguard.py --dry-run
```

## Raspberry Pi OS (64-bit, production)

```bash
sudo apt install -y python3-venv
sudo useradd --system --home-dir /var/lib/dnsguard --create-home --shell /usr/sbin/nologin dnsguard
sudo mkdir -p /opt/dnsguard
sudo cp dnsguard.py requirements.txt /opt/dnsguard/
sudo python3 -m venv /opt/dnsguard/venv
sudo /opt/dnsguard/venv/bin/pip install -r /opt/dnsguard/requirements.txt

# settings and password: owned by dnsguard, mode 600
sudo install -d -o dnsguard -g dnsguard -m 700 /var/lib/dnsguard/.dnsguard
sudo install -o dnsguard -g dnsguard -m 600 config.example.ini /var/lib/dnsguard/.dnsguard/config.ini
sudo nano /var/lib/dnsguard/.dnsguard/config.ini
sudo install -o dnsguard -g dnsguard -m 600 /dev/null /var/lib/dnsguard/.dnsguard/router_pw
sudo nano /var/lib/dnsguard/.dnsguard/router_pw

# check login as the service user before enabling
sudo -u dnsguard -H /opt/dnsguard/venv/bin/python /opt/dnsguard/dnsguard.py --dry-run

sudo cp dnsguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dnsguard
journalctl -u dnsguard -f
```

## Test plan

1. **Dry run:** run `--dry-run`. You should see "Router login OK", each slot marked `ok`
   (if NextDNS is set now), and the full forwarding list. This run changes nothing.
2. **Single run while healthy:** `python dnsguard.py`. It prints nothing and exits.
3. **Break it:** in the router web UI, set DNS back to automatic (Bell) and save.
   Confirm https://test.nextdns.io shows `"status": "unconfigured"`. You may need
   `ipconfig /flushdns` on Windows first.
4. **Watch it fix:** run `python dnsguard.py --loop` (or watch `journalctl -u dnsguard -f`
   on the Pi). Within about 2 minutes you should see `NextDNS OFF - checking router`,
   `router updated (N values)`, then `restored OK`. Reload the router's DNS page to
   confirm that it shows the NextDNS servers.
5. **No loop when healthy:** if it logs `router already set to NextDNS; check fails for
   another reason`, the router is fine. Look at this machine's DNS, the router's IPv6 DNS,
   or your NextDNS linked IP.

## License

MIT. See [LICENSE](LICENSE).
