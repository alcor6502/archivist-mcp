# Archivist MCP <img align="right" src="https://img.shields.io/badge/License-MIT-yellow.svg">

<img src="https://img.shields.io/badge/version-1.7.0-blue.svg"> <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white"> <img src="https://img.shields.io/badge/Unraid-7-F15A2C.svg"> <img src="https://img.shields.io/badge/MCP-21%20tools-8A63D2.svg">

**A document vault Claude can read and write, git-versioned on every write,
self-hosted on your own server.**

No data leaves your machine except towards the conversation that asked for it.
Every change is a commit. Nothing is deleted by accident, and what is deleted
can be recovered.

🇮🇹 [Leggi in italiano](README.it.md)

---

## Why it exists

Anyone working with an LLM on something serious hits the same wall: **the
conversations do not remember.** Every chat starts from nothing, and the
material that should accumulate — decisions, data, working notes — ends up
scattered between attachments re-uploaded every time and old chats you can no
longer find.

The obvious answer is "put the files in a shared folder". But a shared folder
solves half the problem and creates the other half:

| | Synced folder | Archivist |
|---|---|---|
| The model reads the files | you re-upload them by hand | it reads them when it needs them |
| The model writes the files | no | yes, with a commit |
| Two conversations writing at once | last one wins, silently | the second is **refused** and told why |
| "How did this file look on Tuesday?" | depends on the service's bin | `read_at`, always |
| "What changed" | nothing | full git history |
| One project must not see another | nothing | datasets with keys |

The real leap is not the access: it is **git underneath**. Once every write is a
commit, you stop being afraid to let a model write. If it gets something wrong,
you go back. If two chats collide, it tells you instead of letting the last one
win. If a file disappears, it was only moved.

### Datasets

The vault root holds **datasets**: top-level directories, each with its own
independent git repository. The name is borrowed from ZFS, for the same reason:
a dataset is a unit that moves, replicates and restores on its own, without
touching the others.

```
vault/
├── keys.txt                  ← key registry
├── Example Project/          ← a dataset, with its own .git
│   ├── 01 Notes/
│   └── Trash/
└── Scratch/                  ← another dataset, with its own .git
```

**Every path starts with a dataset name.** There is no root-level operation at
all — and the entire protection model follows from that single rule, with no
exception lists to maintain.

### Keys

A dataset with a line in `keys.txt` is **locked**: every call must carry its
key. Without a line it is **open**.

The key is not there to keep strangers out — OAuth does that, and only one
account gets in at all. It is there to **separate projects from each other**.
The concrete case: a chat opened on the fly, outside any project, that starts
reading a serious project's data because it knows the data exists. The key lives
in the project's instructions, so only conversations started inside that project
have it in context.

From this follows a rule that replaces three mechanisms that would otherwise be
needed: **the presence of a key is the declaration that this data matters.** A
dataset with a key cannot be dropped by the tools, full stop. One without a key
can, because it was born to be thrown away.

---

## How it is built

Every piece was chosen for a specific reason, and the reasons are worth stating:
they are the same ones you need if you want to adapt it.

### MCP — Model Context Protocol

The protocol the model uses to talk to external tools. An MCP server exposes
**tools**: functions with a name, typed parameters and a description. The model
reads the descriptions and decides on its own when to call them.

That has a consequence which governs the whole design: **every tool's
description is loaded into the context of every conversation**, always, even
when none of them is used. Hence the dense docstrings here, and hence the
refusal to multiply tools for fun — each one is a fixed cost on every chat.

### FastMCP

The Python implementation of the protocol. It handles HTTP transport, schema
serialisation and — the part that really earns its keep — the whole **OAuth 2.1
dance with Dynamic Client Registration and PKCE** that a remote connector
requires. Writing that by hand would have been the bulk of the work.

### OAuth 2.1 with GitHub login

The service has no users of its own: it delegates login to GitHub and then
**refuses anyone who is not the single configured username**. Anyone on GitHub
can *attempt* to log in; the refusal comes from the server, not from GitHub.

Why GitHub rather than a password: a password on an exposed service is a secret
living in plaintext somewhere with no revocation. An OAuth identity has expiry,
revocation and no client-side secret.

### Tailscale Funnel

The service listens on `127.0.0.1` and does **not know** how traffic reaches it.
The Funnel runs in the same container and publishes that port on a public HTTPS
URL with a valid certificate, without opening a single port on the router and
without exposing a home IP address.

The decoupling is deliberate: put a reverse proxy there instead tomorrow and not
a line of code changes.

### Git, server-side

Every write is a commit. This is not a backup: it is **a memory of intent**.
`history` says what happened, `diff` what changed, `read_at` how it was,
`dataset_restore` puts it back.

And one detail that makes the difference in daily use: if something writes into
the vault **outside** the tools — over SMB, by hand, with an editor — the server
notices and commits those changes **separately**, with an honest message, before
running its own. Tool commits stay pure and the history never lies by accident.

### Docker

The container starts as root only to fix permissions, then **drops privileges**
and runs as `nobody:users` with umask 000, so the files stay usable from SMB
shares too.

### Blocking preflight

Ten checks run at startup. If **a single one** fails, the service **does not
start** — and a check that crashes counts as failed, not as passed.

It looks excessive until it happens to you: a wrong mount that makes the vault
appear empty, a Funnel publishing the wrong port, a node key with expiry still
enabled that will switch everything off in six months. A service that refuses to
start and tells you why beats one that starts and misbehaves.

---

## Architecture

```
   The model (hosted)
        │  HTTPS + OAuth 2.1 (DCR + PKCE)
        ▼
   Tailscale Funnel  ──►  https://<host>.<tailnet>.ts.net
        │  (in the same container)
        ▼
   127.0.0.1:3000   server.py  ── 21 MCP tools
        │                        ├─ GitHub identity filter
        │                        └─ source IP filter
        ▼
   vault.py  ── VaultRoot (datasets, keys)  ──►  Dataset (files + git)
        │
        ▼
   /vault  ── one git repository per dataset
```

---

## Installation

<details>
<summary><b>1 · Prerequisites</b></summary>

- A Tailscale tailnet with **MagicDNS** and **HTTPS Certificates** enabled.
- Unraid 7 with the **Tailscale plugin** installed: it provides the Docker hook
  that gives the container its own Tailscale identity. Do not uninstall it, even
  if Tailscale on the host is disabled.
- On the host: **Allow Tailscale Funnel = No**. The Funnel belongs to the
  container, not to the host.
- An **SSD** pool for the vault. Spinning disks pay the spin-up on every touch,
  and this service touches often.
- A GitHub account.

</details>

<details>
<summary><b>2 · GitHub OAuth application</b> — five minutes</summary>

`github.com` → Settings → Developer settings → OAuth Apps → **New OAuth App**

| Field | Value |
|---|---|
| Application name | anything, e.g. `archivist-mcp` |
| Homepage URL | `https://<host>.<tailnet>.ts.net` |
| Authorization callback URL | `https://<host>.<tailnet>.ts.net/auth/callback` |

*Generate a new client secret*, then store **Client ID** and **Client Secret** in
your password manager: the secret is shown once, but never expires.

⚠ **A new application per service.** Do not reuse another container's: there is
only one callback and the two will fight over it.

</details>

<details>
<summary><b>3 · The vault on disk</b></summary>

```sh
zfs create <ssd-pool>/Vault
mkdir "/mnt/<ssd-pool>/Vault/Example Project"
chown -R 99:100 "/mnt/<ssd-pool>/Vault"
chmod -R 777 "/mnt/<ssd-pool>/Vault"
```

Copy your files in with `rsync` or `scp`. The git repository is created by the
server at first boot: no `git init` by hand, and the files need not belong to
anyone in particular — the entrypoint declares `safe.directory` to avoid git's
*dubious ownership*.

⚠ **ZFS snapshots on the `Vault` dataset**, not on individual projects. Snapshots
are the net for a catastrophe and they protect the `.git` directories too;
day-to-day rollback is git's job, not theirs.

⚠ Mount the **direct pool path** (`/mnt/<pool>/Vault`) in the container, never
`/mnt/user/...`: no FUSE in the middle.

</details>

<details>
<summary><b>4 · The key registry</b></summary>

```sh
printf 'Example Project\tk7m2xq4p\n' > "/mnt/<ssd-pool>/Vault/keys.txt"
chown 99:100 "/mnt/<ssd-pool>/Vault/keys.txt"
chmod 640    "/mnt/<ssd-pool>/Vault/keys.txt"
```

Dataset name, **TAB**, key. One line per dataset; blank lines and lines starting
with `#` are comments.

Eight alphanumeric characters are plenty: OAuth is already in front, and the
threat is a conversation guessing, not a brute-force attack. Avoid `0/O` and
`1/l`, since you will retype them by hand.

`640` owned by `99:100`: the service reads it, the world does not. **Not**
root-only — the service does not run as root and could not open it.

The file is **hot-reloaded**: add or remove a line from a file manager and it
takes effect immediately, with no restart.

It lives inside the vault yet is unreachable from the tools, because its name is
not a dataset name — the same check that stops `..` and `.git` stops it.

</details>

<details>
<summary><b>5 · Build the image</b></summary>

```sh
docker build --no-cache -t archivist-mcp .
```

⚠ **`--no-cache` is not pedantry.** Docker's build cache has been known to
report `CACHED` for a layer whose file had changed. You lose an hour testing the
old image, convinced you fixed something.

Before installing, test the engine with no network and no Docker:

```sh
python3 test_vault.py     # 125 checks, all must pass
```

Half of those checks verify things that must **not** happen — traversal, wrong
keys, dropping protected datasets — and they are the ones that matter most.

</details>

<details>
<summary><b>6 · The container</b></summary>

Import `archivist-mcp.template.xml` into Unraid, or create the container by
hand. Every field carries its own description in the UI; here is the summary.

**Paths**

| Name | Host → Container |
|---|---|
| Vault | `/mnt/<pool>/Vault/` → `/vault` |
| App Data | `/mnt/user/appdata/archivist-mcp/data` → `/data` |
| Tailscale State | `/mnt/user/appdata/archivist-mcp/ts-state` → `/var/lib/tailscale` |

**Variables**

| Variable | Value |
|---|---|
| `VAULT_ROOT` | `/vault` |
| `KEYS_FILE` | `/vault/keys.txt` |
| `GIT_RETENTION_MONTHS` | `0` (disabled) |
| `BASE_URL` | `https://<host>.<tailnet>.ts.net` — **no trailing slash** |
| `GITHUB_CLIENT_ID` | from step 2 |
| `GITHUB_CLIENT_SECRET` | from step 2 |
| `ALLOWED_GITHUB_LOGIN` | your GitHub username |
| `JWT_SIGNING_KEY` | `openssl rand -hex 32` |
| `PORT` | `3000` |
| `ANTHROPIC_CIDR` | `160.79.104.0/21` |

**Tailscale**: Enabled `true`, Hostname `<host>`, Serve `funnel`, Serve Port
**equal to `PORT`**, State Dir `/var/lib/tailscale`.

Then **Apply**, never Restart. Restart reboots the existing container with the
old configuration; only Apply recreates it from the updated template.

</details>

<details>
<summary><b>7 · First start and connecting</b></summary>

In the container log you should see, in order: git init per dataset, the
permission pass, the privilege drop, **preflight 10/10**, and then the server
starting.

If preflight blocks, the message names the check and the reason. It is not a
warning: the service did not start.

Then, in the client: **Settings → Connectors → Add custom connector**, URL
`https://<host>.<tailnet>.ts.net/mcp`. The GitHub login opens, you authorise,
and the tools appear.

Try these first, in order:

```
vault_status()                                  → must list the datasets
dataset_status("Example Project", "")           → must be REFUSED
dataset_status("Example Project", "k7m2xq4p")   → must answer
dataset_create("Scratch")                       → created open
list_files("Scratch")                           → works with no key
dataset_drop("Example Project", "<manifest>")   → must be REFUSED
```

Finally paste the key into the **instructions of the project** the dataset
belongs to. From then on only conversations started inside that project have it
in context.

</details>

<details>
<summary><b>8 · After any change to the tools</b></summary>

There are **three cache layers**: the server, the connector and the chat
session.

After any change to the tool surface — names, parameters, docstrings — you must
**disconnect and reconnect the connector**, and test **in a new conversation**.
Skip that and you will see the old tools and conclude the deployment failed.

Changes to internal behaviour (limits, formats, logic) do not alter the surface:
recreating the container is enough.

</details>

---

## Maintenance and failures

<details>
<summary><b>The safe — what cannot be regenerated</b></summary>

| Item | Where it lives | If you lose it |
|---|---|---|
| `GITHUB_CLIENT_ID` + `SECRET` | the GitHub OAuth App | make a new one in 5 minutes, then update the template |
| `JWT_SIGNING_KEY` | only in the template | stored tokens become unreadable: reconnect the connector. **But never change it without reason** — the effect is the same |
| The keys in `keys.txt` | the vault | they must be rewritten, and re-pasted into the project instructions |
| **The vault** | the ZFS dataset + snapshots + git | the only real loss |

⚠ The template Unraid saves under `/boot/config/plugins/dockerMan/templates-user/`
contains the secrets **in plaintext**, masked fields included. That backup is
sensitive material: the shareable copy is the sanitised template in this repo.

</details>

<details>
<summary><b>Traps already paid for</b></summary>

- **Docker's build cache lies.** Always `--no-cache` after touching sources.
- **Restart ≠ Apply.** Restart reuses the old configuration.
- **`mkstemp` creates 0600, ignoring the umask.** The code does an explicit
  `chmod 666` after every atomic write, or new files would not be writable from
  SMB.
- **git and *dubious ownership*.** The entrypoint declares `safe.directory`
  before touching any repository.
- **Funnel permission is tied to the node identity.** Recreate the container and
  lose `ts-state`, and the node comes back as new with the Funnel needing
  re-authorisation. In the tailnet policy, granting Funnel to `autogroup:member`
  is more robust than naming specific nodes.
- **Node key expiry is a scheduled outage.** Disable it in the admin console,
  under Machines. Preflight checks it precisely because it is silent: everything
  works for six months, then stops.
- **Tailscale auto-updates can break the Funnel.** It happened with 1.102.1,
  where a regression made incoming Funnel connections fail; fixed in **1.102.2**
  on 4 August 2026. If it happens to you, compare the version against the
  changelog before hunting for the fault at home.

</details>

<details>
<summary><b>The service will not start</b></summary>

Preflight names the failing check. The frequent ones:

| Check | What to look at |
|---|---|
| `datasets` | the vault mount is wrong, or points at an empty folder |
| `git` | the repositories do not exist yet: relaunch, boot creates them |
| `keys` | `keys.txt` is not readable by `99:100`, or a line has no TAB |
| `oauth` | a variable is still a placeholder, or `BASE_URL` is not https |
| `token_store` | `FASTMCP_HOME` is not under `/data`: tokens would not survive |
| `funnel` | the Funnel is off, or publishes a port other than `PORT` |
| `node_key` | the node key still has an expiry date |
| `public_dns` | the `BASE_URL` hostname does not resolve |

To test while skipping the network checks:
`PREFLIGHT_SKIP="funnel,node_key,public_dns"`. Never in production.

</details>

<details>
<summary><b>The connector will not connect</b></summary>

Almost always `BASE_URL` does not match the callback registered on GitHub
**exactly** — scheme included, trailing slash included. It is the number one
first-run mistake.

If the service answers but no tools appear, it is caching: disconnect and
reconnect the connector, then open a fresh conversation.

</details>

<details>
<summary><b>Something got messed up in the vault</b></summary>

In order of severity:

```
history("Example Project/file.md", 20)         what happened
read_at("Example Project/file.md", "<hash>")   how it was
write_file(...)                                put it back

diff("HEAD~5", "Example Project")              what changed across the dataset
dataset_restore("Example Project", "<hash>", "<manifest>", key)
```

`dataset_restore` rewrites **every** file in the dataset, but as a forward
commit: history is not lost and this too can be undone.

Underneath everything sits the ZFS snapshot, which is the net for when git
itself is gone.

</details>

---

## Usage guide

<details>
<summary><b>The five rules</b></summary>

**1. Every tool returns a verdict, not a dump.** The result is a small object
holding facts: hashes, counts, bytes, the commit id. Content travels only when
asked for. To *know*, use `search`, `manifest`, `list_files`; to *read*,
`read_file`.

**2. The sha256 is the unit of truth.** Every read gives it, every write demands
it:

```
read_file("X") → sha256: a3f9…
                 ↓
write_file("X", new, expected_sha256="a3f9…")
```

If the file changed in the meantime, the write is **refused without touching
anything**. That is compare-and-swap. For a new file: `expected_sha256="new"`.

**3. Nothing is deleted.** There is no `delete` tool. Disposal is `move_path`
into `Trash/`, and `move_path` never overwrites.

**4. Every write is atomic, verified and committed.** Lock, optional commit of
external changes, write to a temporary file, `os.replace` (atomic), read back and
compare hashes, commit. If a tool fails, the vault is exactly as it was.

**5. `external_commit_first` is not an error.** It means the repo was dirty and
changes from outside were committed separately before yours.

</details>

<details>
<summary><b>Which tool for which job</b></summary>

| You want to | Use | Sha? |
|---|---|---|
| add lines to a register | `append` | no |
| change a phrase or a number | `edit_file` | yes |
| rewrite the file, or create it | `write_file` | yes (`"new"` if new) |
| move, rename, trash | `move_path` | no |
| know whether something exists, and where | `search` | — |
| know which files exist | `list_files` | — |
| compare two trees | `manifest` | — |
| read text | `read_file` | — |
| read a PDF or binary | `read_binary` | — |
| read many at once | `archive` | — |
| read how it was before | `read_at` | — |

`append` needs no sha because it **never touches existing bytes**: no conflict is
possible, so there is nothing to protect. It is the right operation for logs and
registers.

`edit_file` sends only the two fragments instead of the whole file: on an 80 KB
file that is the difference between a light call and a heavy one.

</details>

<details>
<summary><b>The 21 tools</b></summary>

**Vault level** — no key

| Tool | What it does |
|---|---|
| `vault_status()` | the vault answers, dataset list with open/locked. Nothing else |
| `reference_guide()` | the quick guide, served from the image |
| `dataset_create(name)` | create an open, empty dataset |
| `dataset_drop(dataset, expected_manifest)` | delete an **open** dataset; locked ones refuse |

**Dataset level** — all accept `key`

| Tool | What it does |
|---|---|
| `dataset_status` | files, trash, git, commits, repository size |
| `list_files` | recursive listing with size and sha per file |
| `read_file` | UTF-8 text plus sha |
| `read_binary` | any file as base64, max 2 MB |
| `read_at` | the file as it was at a revision |
| `append` | a block at the end, max 64 KB, no sha |
| `write_file` | whole file, CAS |
| `edit_file` | surgical replacement, CAS |
| `write_binary` | binary from base64, CAS |
| `move_path` | move or rename inside a dataset |
| `search` | server-side grep, `file:line:text` |
| `manifest` | the fingerprint of a tree in one number |
| `archive` | tar.gz as base64, everything in one call |
| `history` | the last N git entries |
| `diff` | differences between two revisions |
| `dataset_restore` | ⚠ bring the whole dataset back to a revision |
| `trash_purge` | empty the trash before a date |

Each tool's exact parameters are in its own description, which the model already
has in context: they are not duplicated here. `reference_guide()` returns the
rules and recipes in compact form, for when a conversation gets lost.

</details>

<details>
<summary><b>Limits</b></summary>

| Limit | Value |
|---|---|
| text read and write | 2 MB |
| binaries | 2 MB |
| `append` block | 64 KB |
| listable files | 3,000 |
| `search` lines | 200 |
| `diff` | 60 KB (truncates, does not fail) |
| `archive` input | 30 MB uncompressed |
| `archive` output | 5 MB of tgz |

The binary limits are calibrated on actual consumption: a file larger than 2 MB
is not usable inside a conversation anyway. A talking refusal beats a silent
failure further down. Above that threshold files travel over SMB or `scp`, and
the vault acts as the archivist.

</details>

<details>
<summary><b>Recipes</b></summary>

```
change a number:      read_file → sha → edit_file(path, old, new, sha)
add to a log:         append(path, line)
create a document:    write_file(path, content, "new")
archive something:    move_path("X/doc.md", "X/Trash/doc.md")
find something:       search("term", "X") → read_file on the right file only
recover content:      history → read_at(path, hash) → write_file
compare two moments:  manifest before, manifest after — equal means nothing moved
full audit:           manifest → list_files → archive → verify hashes → manifest
```

</details>

<details>
<summary><b>Errors and what to do</b></summary>

| Message | Cure |
|---|---|
| `dataset ... is protected: a key is required` | the key is in the project's instructions |
| `no such dataset` | `vault_status` lists them |
| `CONFLICT: expected sha ...` | re-read, reconcile, retry |
| `the file already exists` | you used `"new"` on an existing file |
| `the file does not exist: ... "new"` | the opposite |
| `old_text NOT found` | re-read and copy the exact fragment |
| `old_text found N times` | widen the context until it is unique |
| `path not allowed` | there is a `..` or `.git` in the path |
| `destination already exists` | `move_path` never overwrites |
| `more than 3000 files` | go one level deeper |
| `block too large (max 64000)` | `append` is not for rewrites |

A failing tool never leaves a partial write.

</details>

---

## What it deliberately does not do

No "run command" tool. No file deletion. No `git gc --prune` on demand. No
dumps: every tool returns a verdict, because every byte coming back lands in the
conversation's context, and context is the scarce resource.

And one thing worth stating plainly: **dataset keys are not authentication.**
The service recognises a single account, and all of its conversations share one
identity — the server cannot tell them apart. The keys work because a
conversation without the key in its context cannot invent it: they are a boundary
between projects, not a defence against an attacker. That is what OAuth is for.

## Package contents

| File | |
|---|---|
| `vault.py` | the engine: `VaultRoot` and `Dataset` |
| `server.py` | the 21 MCP tools, contracts in the docstrings |
| `preflight.py` | the 10 blocking checks |
| `reference-guide.md` | the compact guide served by `reference_guide()` |
| `entrypoint.sh` | init, permissions, privilege drop, preflight, start |
| `Dockerfile` · `requirements.txt` | the image |
| `archivist-mcp.template.xml` | Unraid template, every field documented |
| `test_vault.py` | 125 checks on the engine, no network needed |

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
