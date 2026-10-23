# ship

Publish an HTML file to one of your Cloudflare domains with one command.

```bash
ship report.html demo.example.com        # https://demo.example.com/
ship report.html example.com/reports/q3  # https://example.com/reports/q3/
ship ./site/ app.example.com             # a whole folder (needs an index.html)
ship report.html                         # https://p.example.com/report-8f3a1c/
ship ls                                  # list what's published
ship rm demo.example.com                 # take it down
```

## How it works

```
                        ┌──────────────── Cloudflare ────────────────┐
ship report.html        │                                            │
demo.example.com ──────►│  R2 bucket "ship-sites"                    │
  1. upload files       │    demo.example.com/index.html             │
  2. attach hostname    │    example.com/reports/q3/index.html       │
                        │            ▲                               │
                        │            │ looks up  <hostname><path>    │
browser ───────────────►│  Worker "ship-router"  (worker.js)         │
https://demo.example.com│    one Worker serves every hostname        │
                        └────────────────────────────────────────────┘
```

- **One Worker serves every site.** It's deployed once by `ship setup`. For each
  request it reads the file at `<hostname><path>` from R2.
- **Publishing never redeploys anything.** It uploads files to R2 and, the
  first time a hostname is used, attaches that hostname to the Worker as a
  *Workers Custom Domain*. Cloudflare then creates the DNS record and the
  HTTPS certificate itself.
- **Safety:** if a hostname already has DNS records (your real site, email,
  etc.), ship refuses to take it over unless you pass `--force`.
- **Pages aren't indexed:** every response has `X-Robots-Tag: noindex`, so
  search engines skip them.

## Code tour (suggested reading order)

| File | What it does |
| --- | --- |
| `ship/targets.py` | Pure logic: parse `host/path`, pick files to upload, set content types |
| `ship/worker.js` | The Worker that serves the files (runs on Cloudflare) |
| `ship/cloudflare.py` | Small client for the Cloudflare REST API |
| `ship/storage.py` | Upload, list and delete files in R2 (through its S3-compatible API) |
| `ship/config.py` | Settings file plus Keychain storage for the token |
| `ship/cli.py` | The commands, tying everything together |

## Install & set up

```bash
git clone https://github.com/JoseUlisesQuevedo/ship.git
uv tool install --editable ./ship   # puts `ship` on your PATH
ship setup
```

`--editable` means edits to the code take effect right away, with no reinstall.

For `man ship`, link the man page where macOS looks for it:

```bash
mkdir -p ~/.local/share/man/man1
ln -sf "$PWD/ship/man/ship.1" ~/.local/share/man/man1/ship.1
```

Before running `ship setup`:

1. **Enable R2** once in the Cloudflare dashboard (sidebar → R2). The free
   tier is enough, but Cloudflare asks for a payment method.
2. **Create an API token** at https://dash.cloudflare.com/profile/api-tokens
   → *Create Token* → *Custom token*, with these permissions:

   | Scope | Permission | Level |
   | --- | --- | --- |
   | Account | Workers Scripts | Edit |
   | Account | Workers R2 Storage | Edit |
   | Account | Account Settings | Read |
   | Zone | Zone | Read |
   | Zone | DNS | Edit |
   | Zone | Workers Routes | Edit |

   Set Zone Resources to *All zones*, or only the domains you'll publish to.

The token is saved in the macOS Keychain, and the other settings go in
`~/.config/ship/config.json`. Re-running `ship setup` is safe; it also
redeploys `worker.js` if you've changed it.
