# OpenChamber panel

This optional extension displays Tarmac readings inside OpenChamber. It isn't installed by the Python package. The manifest declares SDK 2.3.2 and OpenChamber >=2.3.0; compatibility with a particular host version needs testing.

Build prerequisites are Node/npm, the SDK and esbuild. From this directory:

```sh
npm ci
npm run build
```

The build creates `panel/main.js`. The manifest also references `icon.svg`, which isn't included. Supply an icon you have permission to distribute before adding this folder in OpenChamber Settings → Extensions. The supplied mascot PNGs don't directly satisfy that SVG path. Consent to the extension's host-side service only after reviewing the relay source.

The relay connects to the core service at `http://127.0.0.1:8767`. Visible panels read active status every 45 seconds; hidden panels read passive snapshots every five minutes. Refresh can poll providers; sign-in can open Terminal and OAuth. The relay checks an OpenChamber service token but doesn't send Tarmac's source-CRUD token. Provider access and credential behavior follow the core's configuration.
