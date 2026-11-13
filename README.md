# Archivist MCP <img align="right" src="https://img.shields.io/badge/License-MIT-yellow.svg">

<img src="https://img.shields.io/github/v/tag/alcor6502/archivist-mcp?label=version&color=blue"> <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white"> <img src="https://img.shields.io/badge/Unraid-7-F15A2C.svg"> <img src="https://img.shields.io/badge/MCP-server-8A63D2.svg">

**A document vault an LLM can read and write, git-versioned on every write,
self-hosted on your own server.**

Archivist is an MCP server (Model Context Protocol: how an LLM client such as
Claude calls external tools) that gives your conversations a persistent set of
files. The vault is split into **datasets**, top-level folders with their own
git repository, and a key registry decides which ones a conversation may reach.
Every write is a commit, refused if the file changed since it was read, so a
model's mistake can always be undone. It runs as a Docker container on Unraid,
is published by Tailscale Funnel, and lets in a single GitHub account.

---

## Quick start

### Prerequisites

- **Unraid 7** with the **Tailscale plugin** (it gives the container its own
  Tailscale identity; keep it even if Tailscale on the host is off). On the
  host, **Allow Tailscale Funnel = No**: the Funnel belongs to the container.
- A **Tailscale** tailnet with **MagicDNS** and **HTTPS Certificates** on, and
  Funnel allowed in the tailnet policy (granting it to `autogroup:member`
  survives a recreated node).
- An **SSD** pool for the vault, and a **GitHub** account.

Below, `<host>.<tailnet>.ts.net` is the container's Tailscale name and
`<ssd-pool>` your pool.

### Steps

**1. GitHub OAuth application.** `github.com` → Settings → Developer settings →
OAuth Apps → **New OAuth App**:

| Field | Value |
|---|---|
| Application name | anything, e.g. `archivist-mcp` |
| Homepage URL | `https://<host>.<tailnet>.ts.net` |
| Authorization callback URL | `https://<host>.<tailnet>.ts.net/auth/callback` |

Generate a client secret; store **Client ID** and **Client Secret** in a
password manager (the secret is shown once). One application per service.

**2. The vault on disk.**

```sh
zfs create <ssd-pool>/Vault
mkdir "/mnt/<ssd-pool>/Vault/Example Project"
chown -R 99:100 "/mnt/<ssd-pool>/Vault"
chmod -R 777 "/mnt/<ssd-pool>/Vault"
```

Each top-level folder is a dataset. Copy files in with `rsync` or `scp`; the
server creates the git repositories itself.

**3. The key registry.**

```sh
printf 'Example Project | k7m2xq4p\nScratch |\n' > "/mnt/<ssd-pool>/Vault/keys.txt"
chown 99:100 "/mnt/<ssd-pool>/Vault/keys.txt"
chmod 640    "/mnt/<ssd-pool>/Vault/keys.txt"
```

`Example Project` is locked with the key `k7m2xq4p`, `Scratch` will be open, any
other dataset is closed ([Datasets and keys](#datasets-and-keys)).

**4. The container.** Import `archivist-mcp.xml` into Unraid, or create the
container by hand with the same fields. Fill in:

| Field | Value |
|---|---|
| Vault | `/mnt/<ssd-pool>/Vault/` → `/vault` (the pool path, never `/mnt/user/...`) |
| App Data | `/mnt/user/appdata/archivist-mcp/data` → `/data` |
| Tailscale State | `/mnt/user/appdata/archivist-mcp/ts-state` → `/var/lib/tailscale` |
| `BASE_URL` | `https://<host>.<tailnet>.ts.net`, **no trailing slash** |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | from step 1 |
| `ALLOWED_GITHUB_LOGIN` | your GitHub username, the only account let in |
| `JWT_SIGNING_KEY` | output of `openssl rand -hex 32` |
| Tailscale | Hostname `<host>`, Serve `funnel`, Serve Port = `PORT` (`3000`) |

Leave the rest at the defaults, then **Apply**.

**5. Node key expiry.** In the Tailscale admin console, under Machines,
disable key expiry for the container's node. The startup checks refuse to run
while the key can expire: if the first start stops on `node_key`, fix it and
start again.

**6. The log.** You should see `== git init (as root) ==`, the permission pass,
the privilege drop, the preflight with every line `OK`, then:

```
PREFLIGHT: 11/11 — starting.
== server ==
… archivist-mcp <version> (engine <version>) — starting on 127.0.0.1:3000 (http stateless) — base_url https://<host>.<tailnet>.ts.net — allowed user: <login> — IP filter: OFF (no IP filter) — token store: /data/fastmcp — retention: disabled
```

A `FAIL` line means the service did not start: see
[The service will not start](#the-service-will-not-start).

**7. Connect.** In Claude: Settings → Connectors → **Add custom connector**,
URL `https://<host>.<tailnet>.ts.net/mcp`. Authorise the GitHub login; the
tools appear.

**8. Check**, in a new conversation:

```
vault_status()                                      → Example Project locked, Scratch open
dataset_status("Example Project")                   → REFUSED: a key is required
dataset_status("Example Project", key="k7m2xq4p")   → answers
list_files("Scratch")                               → answers, no key
```

**9. Hand over the key.** Paste `k7m2xq4p` into the instructions of the client
project that works on `Example Project`: only its conversations will have it.

---

## Using it day to day

### Datasets and keys

Every call names a dataset in `dataset`; `path` is relative to it, and empty
means the whole dataset. Nothing at the vault root is reachable: `keys.txt`
cannot even be named. Dataset names use letters, digits, space, `.`, `_`, `-`,
and do not start with `.` or `_`.

| Line in `keys.txt` | State | A call |
|---|---|---|
| `name \| key` | **locked** | must carry the key |
| `name \|` (nothing after the bar) | **open** | needs no key |
| no line | **closed** | is refused |

- **Closed is the default**: a missing line, a typo in a name or a missing file
  makes datasets unreachable, never exposed. `vault_status()` lists the closed
  ones and `orphan_registry_lines`, registry names matching no dataset.
- **Hot-reloaded**: an edit applies at the next call. Spaces around the bar are
  ignored; `#` starts a comment. A malformed line stops the service at startup;
  while it runs, the line is ignored and its dataset stays closed.
- **No tool writes `keys.txt`**: a dataset made by `dataset_create` is closed
  until you give it a line.
- Eight alphanumeric characters make a good key (avoid `0/O`, `1/l`). Keep the
  file `640`, owned by `99:100`.
- **A key separates projects, it does not keep strangers out**: OAuth does. A
  chat without the key in its context cannot invent it. A locked dataset can
  never be dropped by the tools.

### Rules a client must know

The server sends its rules to the client at connection time. In short:

- **The sha256 is the unit of truth.** Every read returns it; every write
  demands it (`"new"` for a new file) and returns the new one. If the file
  changed meanwhile the write is refused with `CONFLICT`: re-read, reconcile,
  retry.
- **Every write is atomic, verified and committed**; a failed tool changes
  nothing. After a write lost in transport, compare the sha in `list_files`
  before retrying.
- **Nothing is deleted**: disposal is `move_path` into `Trash/`.
- **Fewer calls, and less in each, is faster**: `list_files` with `depth=1`
  to look around, `search` with `context=2` to find, `read_file` with
  `from_line` and `lines` for part of a long file, several files in one
  `read_files`, several changes in one `edit_file` with `edits`, `manifest` to
  know whether anything changed.
- **A PDF is not done until it has been looked at**: `render_pdf` with
  `preview=True` returns the pages as pictures in the same answer, and `view`
  shows any picture, PDF or folder already in the vault. The numbers a render
  returns say it was drawn, not what it looks like.
- **Files changed from outside** (SMB, by hand) are committed separately before
  the next write, which may return `external_commit_first`. Not an error.
- **No empty folders**: git does not keep them.
- `.gitignore`, `.gitattributes`, `.gitmodules` are readable, never writable:
  change them on the server.
- **Big binaries do not go through the model**: in `write_binary` the model
  types the base64. Copy large files over SMB instead. For PDFs, `render_pdf`
  draws one from data and `render_report` binds documents already in the vault.

### The tools

Every dataset-level tool also takes `key=""` as its **last** parameter, needed
only for locked datasets and best passed by name; it is omitted below. Returns,
limits and examples are on each tool's card in
[`reference-guide.md`](reference-guide.md), served by `reference_guide("<tool>")`.

| Tool | What it does |
|---|---|
| **`vault_status()`** | datasets and their state; no key |
| **`reference_guide(name="")`** | the manual, or one tool's card; no key |
| **`dataset_create(name)`** | a new, empty dataset, born closed |
| **`dataset_drop(dataset, expected_manifest)`** | delete an **open** dataset |
| **`dataset_status(dataset)`** | file counts, git state, commits, size |
| **`list_files(dataset, path="", depth=0, pattern="", hashes=True)`** | listing with size and sha per file; one level, a glob, or names only |
| **`read_file(dataset, path, from_line=0, lines=0)`** | one text file with its sha, or a run of its lines |
| **`read_files(dataset, paths)`** | several text files in one call |
| **`read_binary(dataset, path)`** | the original bytes of any file, as base64 |
| **`view(dataset, path="", pages="", size="medium")`** | a picture (HEIC too), the pages of a PDF or a folder, as reduced JPEGs |
| **`pdf_text(dataset, path, pages="")`** | a PDF's page count, outline and text; no OCR |
| **`read_at(dataset, path, rev)`** | a file at a past revision |
| **`search(dataset, pattern, path="", regex=False, context=0, ignore_case=False)`** | server-side grep, with lines of context |
| **`manifest(dataset, path="")`** | one fingerprint for a tree |
| **`archive(dataset, path="", pattern="*.md", max_chars=0)`** | files as one base64 tar.gz, for clients that run code |
| **`append(dataset, path, text, expected_sha256="")`** | a block at the end of a file |
| **`write_file(dataset, path, content, expected_sha256)`** | the whole file |
| **`edit_file(dataset, path, expected_sha256, old_text="", new_text="", edits=None)`** | replace unique fragments, one commit |
| **`write_binary(dataset, path, content_base64, expected_sha256)`** | a binary from base64 |
| **`render_pdf(dataset, path, document, expected_sha256="new", preview=False)`** | a PDF drawn from a JSON document of blocks; `preview` returns its pages as pictures |
| **`render_report(dataset, path, sources, layout=None, expected_sha256="new")`** | dataset Markdown bound into one PDF |
| **`move_path(dataset, src, dst)`** | move, rename or trash; never overwrites |
| **`history(dataset, path="", n=10)`** | the last commits |
| **`diff(dataset, rev_a, path="", rev_b="HEAD")`** | changes between revisions |
| **`dataset_restore(dataset, rev, expected_manifest)`** | the dataset back to a revision, as a new commit |
| **`trash_purge(dataset, before)`** | empty `Trash/` of what was trashed before a date |

---

## Details

### How it is built

```
client ──HTTPS, OAuth 2.1──► Tailscale Funnel ──► 127.0.0.1:3000 server.py ──► vault.py ──► /vault
                             (same container)     GitHub gate, IP filter       one git repository per dataset
```

- **FastMCP** implements the protocol, the HTTP transport and the OAuth 2.1
  flow, with DCR (Dynamic Client Registration) and PKCE (Proof Key for Code
  Exchange).
- **GitHub login**: every request, handshake included, is refused unless it
  comes from `ALLOWED_GITHUB_LOGIN`. A stranger never sees the tools.
- **Tailscale Funnel** publishes the port on public HTTPS with no router port
  opened. The server listens on loopback only.
- **Docker**: the entrypoint fixes ownership as root, then runs as
  `nobody:users` (99:100) with umask 000, so files stay usable over SMB.
- **Preflight**: eleven blocking checks (`preflight.py`). One fails or crashes,
  the service does not start.
- **The engine**, [`mcp_common_engine/`](mcp_common_engine/README.md), is the
  part that knows nothing about vaults: identity gate, designed refusals,
  configuration readers, preflight harness, log discipline. It is a folder of
  this repository, built into the image; its README describes each module.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_ROOT` · `KEYS_FILE` | `/vault` · `/vault/keys.txt` | vault and registry inside the container |
| `BASE_URL` | none | public https URL, no trailing slash, matching the GitHub callback |
| `GITHUB_CLIENT_ID` · `GITHUB_CLIENT_SECRET` | none | the OAuth application |
| `ALLOWED_GITHUB_LOGIN` | none | the only account let in |
| `JWT_SIGNING_KEY` | none | signs issued tokens; 32+ characters, keep it stable |
| `PORT` | `3000` | listening port, and the one the Funnel publishes |
| `ALLOWED_CIDRS` | empty | optional source-IP filter: `range # note ; range # note`. Empty: no filter |
| `GIT_RETENTION_MONTHS` | `0` | squash history older than N months at boot, rewriting later commit hashes; 0 disables (recommended) |
| `LOG_LEVEL` | `INFO` | `INFO` (boot report, refused calls, and one line per served call: tool, milliseconds, bytes returned — never a path) or `WARNING` (gate refusals and faults only) |
| `HTTP_MODE` | `stateless` | `stateless` or `stateful`; a missing variable means `stateful` |
| `VAULT_UID` · `VAULT_GID` | `99` · `100` | the service user |
| `PREFLIGHT_SKIP` | empty | checks to skip, comma-separated; testing only |

A placeholder (`CHANGEME`), an `http://` URL, a short JWT key or a malformed
`ALLOWED_CIDRS` block startup. With `ALLOWED_CIDRS` undefined, the deprecated
`ANTHROPIC_CIDR` is read, then a built-in range.

### Maintenance and failures

| Cannot be regenerated | If lost |
|---|---|
| GitHub client ID and secret | new OAuth application, update the container |
| `JWT_SIGNING_KEY` | tokens invalid: reconnect the connector |
| The keys | rewrite them, paste them into the projects again |
| **The vault** | the real loss: snapshot the whole `Vault` with ZFS |
| `/data` · `ts-state` | authorise again; the node returns as new and the Funnel needs authorising |

Unraid's saved template (`/boot/config/plugins/dockerMan/templates-user/`)
holds the secrets **in plaintext**.

**Updating.** Docker page → **Check for Updates**, then apply. **Apply, never
Restart**: Restart keeps the old configuration. Apply clears the container log,
so copy out what you need first. The startup line then shows the new version.
To go back, put the previous version tag in `Repository` instead of `:latest`.

- **Crossing 3.0.0**: registry lines become `name | key` (was a TAB). Rewrite
  `keys.txt` first.
- **Crossing 4.0.0**: a dataset with no line becomes **closed**. First give
  every dataset that must stay reachable without a key the line `name |`.

When an update changes tool names, parameters or descriptions, disconnect and
reconnect the connector and test in a **new** conversation, or you will see the
old tools.

#### The service will not start

| Check | Look at |
|---|---|
| `datasets` | wrong vault mount, or an empty folder |
| `readable` · `writable` | the mount reads short or refuses a write or delete |
| `git` | a repository is missing: start again, boot creates it |
| `keys` | `keys.txt` unreadable by `99:100`, or a malformed line (numbers printed, keys never) |
| `oauth` | a placeholder left, `BASE_URL` not https, JWT key short |
| `token_store` | `FASTMCP_HOME` not under `/data` |
| `funnel` | the Funnel is off or publishes another port |
| `node_key` | the node key still expires |
| `cidrs` | an `ALLOWED_CIDRS` entry does not parse |
| `public_dns` | the `BASE_URL` hostname does not resolve |

#### The connector will not connect

Almost always `BASE_URL` does not match the GitHub callback **exactly**. No
tools: reconnect, new conversation. A refused login shows in
`docker logs archivist-mcp 2>&1 | grep refused`. A message like *"server
hostname doesn't resolve or isn't reachable"* only means the client saw a
failure: retry, and if calls sent together keep failing, try the other
`HTTP_MODE`.

#### Something went wrong in the vault

```
history("Example Project", "file.md", 20)          what happened
read_at("Example Project", "file.md", "<hash>")    how it was, then write_file it back
dataset_restore("Example Project", "<hash>", "<manifest>", key="…")
```

A restore is a forward commit and can be undone. Below git, the ZFS snapshot.

#### Traps

Build with `docker build --no-cache` (the cache has lied), never on an Apple
Silicon Mac (arm64). A Tailscale update has broken the Funnel before (1.102.1):
check its changelog first.

### Limits

| Limit | Value |
|---|---|
| text read and write, binaries | 2 MB |
| `read_files` · `edit_file` `edits` | 50 paths, 2 MB in all · 50 changes |
| `append` block | 64 KB |
| listable files · `search` matches · a regex | 3,000 · 200 · 5 seconds per call |
| `diff` | 60 KB, truncated |
| `archive` | 30 MB in, 5 MB tgz out |
| `render_pdf` · `render_report` | 20 pages, 200 KB JSON · 200 pages, 5 MB Markdown |
| `view` · `pdf_text` | a source of 100 MB; 10 pages or one sheet of 30 files per call, 1,500 pixels · 50 pages per call |
| datasets | 200 |

The client also caps tool results; a large one may arrive as a file path
instead of data. `archive` with `max_chars=20000` refuses with the size instead
(`reference_guide("archive")`).

### What it deliberately does not do

No command execution, no file deletion, no writing `keys.txt`, no
`git gc --prune` on demand, no dumps: tools return small verdicts, because every
returned byte lands in the conversation's context.

### Running the test suites

```sh
scripts/test.sh
```

It builds a Python 3.12 virtual environment (`PYTHON` picks another,
`ARCHIVIST_WORK` reuses one), installs `requirements.txt` with `--no-deps` and
runs `test_engine.py` (the engine) and `test_vault.py` (the rest), each to its
own log. Trust the `exit=0` lines. No network, Docker or FastMCP. The renderers
need Pango and the Liberation and DejaVu fonts; outside Debian, point
`RENDER_FONT_DIR` and `REPORT_FONT_DIR` at them.

### Releasing

The version is `VERSION` in `server.py`; a release is a `v*` tag. The workflow
runs both suites, checks `VERSION` against the tag, builds the image, **starts**
it with `scripts/smoke.sh` (a stranger refused, both renderers drawing), and
only then publishes `ghcr.io/alcor6502/archivist-mcp`. A manual run publishes
nothing. In a fork, make the package public once. `scripts/ship.sh` is the
maintainer's commit-and-push helper.

### Package contents

| File | |
|---|---|
| `server.py` · `vault.py` · `preflight.py` | tools and startup · datasets, keys, git · startup checks |
| `render.py` · `report.py` | `render_pdf` (reportlab) · `render_report` (WeasyPrint) |
| `rxlimit.py` | the clock on a caller's regular expression: `search` and both `forbid` get 5 seconds per call |
| `look.py` | `view` and `pdf_text`: pictures reduced (Pillow, pillow-heif), PDF pages and text (pypdfium2) |
| `reference-guide.md` | the manual served to the model |
| `mcp_common_engine/` | the engine |
| `test_engine.py` · `test_vault.py` | the suites |
| `requirements.txt` · `requirements-server.txt` | the suite's pins · FastMCP, image only |
| `Dockerfile` · `entrypoint.sh` · `archivist-mcp.xml` | image · startup · Unraid template |
| `archivist-icon.png` | container and OAuth consent page icon |
| `scripts/` | `test.sh`, `ship.sh`, `smoke.sh`, and the renderer bench (`geolog.py`, `inject.py`, `fixtures/`) |

## Licence and credits

Released under the **MIT** licence — see [LICENSE](LICENSE).

It builds on third-party components, which remain their authors' under their own
licences:

| Component | Author | Licence |
|---|---|---|
| [FastMCP](https://github.com/jlowin/fastmcp) | Jeremiah Lowin | Apache-2.0 |
| [Tailscale](https://tailscale.com) | Tailscale Inc. | BSD-3-Clause |
| [Model Context Protocol](https://modelcontextprotocol.io) | Anthropic | MIT |
| Python, git, Debian slim | respective projects | PSF / GPL-2.0 / various |

The distributed Docker image contains these components installed: their licences
travel with them inside the image, as required.
