# mos-templates-unraid

Docker templates for [MOS Hub](https://docs.mos-official.net/docs/MOS-Hub/MOS-Hub-Settings), converted from Unraid's Community Applications feed.

[![Update Templates](https://github.com/websterwh/mos-templates-unraid/actions/workflows/update-templates.yml/badge.svg)](https://github.com/websterwh/mos-templates-unraid/actions/workflows/update-templates.yml)

## Add to MOS

In MOS, go to **Settings → System Configuration → MOS Hub Settings** and add:

```
https://github.com/websterwh/mos-templates-unraid
```

Refresh the Hub, then check **Docker → MOS Hub** for the templates.

## What's in here

Everything in `docker/` is pulled from Unraid's Community Applications feed. Plugins and broken feed entries are filtered out, so what's left are working docker templates - repository, ports, paths, and env vars all carried over. A scheduled workflow re-pulls the feed weekly, so new apps show up and removed ones disappear without anyone touching this repo by hand.

Host paths are set to `/mnt/cache/...` by default, matching MOS's own convention for published templates.

## Contributing

Templates are regenerated from the upstream feed on every run, so a PR editing a file in `docker/` will get overwritten the next time it runs. If a specific app's template is wrong, the fix usually belongs in that app's own Unraid CA template upstream. If the conversion logic itself is broken, that's a real bug here and worth a PR.

## Credit

Template data comes from Unraid's Community Applications project. This repo just converts it for MOS.
