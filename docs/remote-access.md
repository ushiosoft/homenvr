# Remote access

Watch homenvr from anywhere — the app's one real convenience — without the Hik
cloud, ads, or opening ports on your router. Two approaches; **Tailscale is
recommended**.

homenvr is a plain HTTP service on port 8971 (LAN), so remote access is just a
secure network overlay in front of it. Nothing about the recorder changes.

---

## Option A — Tailscale (recommended)

A private WireGuard network (your "tailnet") linking your devices. No ports
forwarded, works behind CGNAT, per-device access control. Free for personal use.

### A1. Tailscale on the host  ← simplest, nothing to change in homenvr

Install Tailscale on the rack server itself:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then from any device also on your tailnet (phone with the Tailscale app, laptop,
etc.) open:

```
http://<rack-server-tailscale-ip>:8971
```

(Find the IP with `tailscale ip -4`, or use its MagicDNS name,
`http://<hostname>:8971`.) That's it — the existing `docker compose up -d` stays
exactly as is. Keep homenvr's login on as a second layer.

### A2. Tailscale as a Docker sidecar  (fully containerized, HTTPS)

If you'd rather not install anything on the host, an optional overlay runs
Tailscale in a container and serves homenvr on your tailnet over HTTPS (with an
automatic tailnet TLS cert):

1. Create an auth key at
   <https://login.tailscale.com/admin/settings/keys> (reusable; ephemeral off so
   the node persists).
2. Put it in `.env`:
   ```
   TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx
   ```
3. Start with the overlay:
   ```bash
   docker compose up -d        # with COMPOSE_PROFILES=tailscale in .env
   ```
4. Open **https://homenvr.<your-tailnet>.ts.net** from any device on the tailnet.

The sidecar (the `tailscale` service in `docker-compose.yml`, profile `tailscale`, +
`ts-serve.json`) proxies the
tailnet to homenvr over the internal Docker network; your LAN access on `:8971`
keeps working unchanged. Verified: the sidecar boots in userspace mode and
registers with the Tailscale control plane; the only input it needs is your
`TS_AUTHKEY`.

Low-latency live view (WebRTC, the go2rtc sidecar in `docker-compose.yml`) also
works remotely: `ts-serve.json` forwards TCP 8555 to go2rtc, and go2rtc advertises
the sidecar's tailnet IP. Set `WEBRTC_TS_IP` in `.env` to that IP (`docker exec
homenvr-tailscale tailscale ip -4`). If WebRTC can't connect, the page falls back
to HLS by itself.

To enable HTTPS certs on your tailnet once: in the admin console enable MagicDNS
and HTTPS (Settings → DNS). Without that, change `ts-serve.json` to serve plain
HTTP on port 80 instead of 443/HTTPS.

---

## Option B — Cloudflare Tunnel

Gives a public HTTPS hostname with no open ports; best if you want a shareable
URL for family who won't install Tailscale. Put **Cloudflare Access** in front so
it isn't open to the world.

```bash
# on the rack server, after creating a tunnel in the Cloudflare dashboard:
cloudflared tunnel run --url http://localhost:8971 <tunnel-name>
```

Map the tunnel to a hostname (e.g. `cctv.yourdomain.com`) and add an Access
policy restricting it to your email. homenvr's own login remains a second layer.

---

## Security notes

- **Set a real `UI_PASSWORD`** before exposing homenvr beyond the LAN. The test
  value (`user` / `123456`) is fine only for local testing.
- Prefer Tailscale/Access over a bare router port-forward. If you must
  port-forward, put a reverse proxy with TLS and IP allowlisting in front.
- The NVR itself should stay off the internet — homenvr is the only thing that
  needs to be reachable, and only to you.
