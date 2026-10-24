# Security

homenvr handles camera footage and NVR credentials, so security reports are
welcome and taken seriously.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting instead:
the repository's **Security** tab → **Report a vulnerability**. Include what you
found, how to reproduce it, and which version (commit) you ran. You'll get an
answer within a week.

## Scope

In scope: the homenvr app (`app/`), its Docker image and compose files, and the
documented deployment (go2rtc and Tailscale sidecars). Issues in Hikvision
firmware, go2rtc, Tailscale or other upstream projects belong with those
projects.

## Running it safely

- homenvr is meant for your LAN or a private network (Tailscale). Don't
  port-forward it to the internet; if you must expose it, put a reverse proxy
  with TLS and access control in front.
- Set a strong `UI_PASSWORD`; the UI uses HTTP Basic auth.
- Keep the NVR itself off the internet. `.env`, `config.yml` and
  `settings.json` hold credentials — keep them out of version control (they are
  gitignored).
