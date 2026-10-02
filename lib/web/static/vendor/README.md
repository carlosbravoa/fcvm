# Vendored third-party files

These are the web console's only third-party files. They are copied here
unchanged from the published npm packages so that `fcvm serve` works offline,
with no build step and no CDN. They are minified because that is how upstream
publishes them, not to hide anything. The readable TypeScript source is at
<https://github.com/xtermjs/xterm.js>.

| File | Package | Path in the package | Upstream source |
|---|---|---|---|
| `xterm.js` | [`@xterm/xterm` 6.0.0](https://www.npmjs.com/package/@xterm/xterm/v/6.0.0) | `lib/xterm.js` | [`src/`](https://github.com/xtermjs/xterm.js/tree/6.0.0/src) |
| `xterm.css` | [`@xterm/xterm` 6.0.0](https://www.npmjs.com/package/@xterm/xterm/v/6.0.0) | `css/xterm.css` | [`css/xterm.css`](https://github.com/xtermjs/xterm.js/blob/6.0.0/css/xterm.css) |
| `addon-fit.js` | [`@xterm/addon-fit` 0.11.0](https://www.npmjs.com/package/@xterm/addon-fit/v/0.11.0) | `lib/addon-fit.js` | [`addons/addon-fit/src/`](https://github.com/xtermjs/xterm.js/tree/6.0.0/addons/addon-fit/src) |

License: MIT, in `LICENSE.xterm`.

`SHA256SUMS` lists each file's hash, and the unit tests check it
(`tests/unit/test_vendor.py`). To check the files against upstream yourself:

```sh
curl -sL https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.js | cmp - xterm.js
curl -sL https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.css | cmp - xterm.css
curl -sL https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.js | cmp - addon-fit.js
```

To upgrade, download the new files the same way, then update this table and
regenerate the hashes with `sha256sum addon-fit.js xterm.css xterm.js > SHA256SUMS`.
