# Archivist MCP <img align="right" src="https://img.shields.io/badge/License-MIT-yellow.svg">

<img src="https://img.shields.io/github/v/tag/alcor6502/archivist-mcp?label=version&color=blue"> <img src="https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white"> <img src="https://img.shields.io/badge/Unraid-7-F15A2C.svg"> <img src="https://img.shields.io/badge/MCP-21%20tools-8A63D2.svg">

**Un magazzino di documenti che Claude può leggere e scrivere, versionato con
git a ogni scrittura, self-hosted a casa tua.**

Nessun dato esce dal tuo server se non verso la conversazione che l'ha chiesto.
Ogni modifica è un commit. Niente si cancella per sbaglio, e quello che si
cancella si recupera.

---

## Perché esiste

Chi lavora con Claude su qualcosa di serio si scontra presto con lo stesso muro:
**le conversazioni non ricordano.** Ogni chat riparte da zero, e il materiale che
dovrebbe accumularsi — decisioni, dati, note di lavoro — resta sparso fra
allegati ricaricati ogni volta e chat vecchie che non ritrovi più.

La risposta ovvia è "mettiamo i file in una cartella condivisa". Ma la cartella
condivisa risolve metà del problema e crea l'altra metà:

| | Cartella sincronizzata | Archivist |
|---|---|---|
| Claude legge i file | vanno ricaricati a mano | li legge quando gli servono |
| Claude scrive i file | no | sì, con commit |
| Due chat che scrivono insieme | l'ultima vince, in silenzio | la seconda viene **rifiutata** e avvisata |
| "Com'era questo file martedì?" | dipende dal cestino del servizio | `read_at`, sempre |
| "Cosa è cambiato" | niente | storia git completa |
| Un progetto non deve vedere l'altro | niente | dataset con chiave |

Il salto vero non è l'accesso: è **git sotto**. Quando ogni scrittura è un
commit, smetti di aver paura di far scrivere a un modello. Se sbaglia, torni
indietro. Se due chat si pestano i piedi, te lo dice invece di far vincere
l'ultima arrivata. Se un file sparisce, era solo spostato.

### I dataset

La radice del vault contiene **dataset**: directory di primo livello, ognuna col
proprio repository git indipendente. Il nome è preso in prestito da ZFS, e per lo
stesso motivo: un dataset è un'unità che si sposta, si replica e si ripristina da
sola, senza toccare le altre.

```
vault/
├── keys.txt                  ← registro delle chiavi
├── Example Project/          ← dataset, col suo .git
│   ├── 01 Notes/
│   └── Trash/
└── Scratch/                  ← un altro dataset, col suo .git
```

**Ogni chiamata nomina il suo dataset esplicitamente**, nel parametro `dataset`;
il `path` è relativo a quel dataset, e un path vuoto significa tutto il dataset.
Non esiste nessuna operazione di livello radice — ed è da questa singola regola
che discende tutta la protezione del sistema, senza liste di eccezioni da
mantenere. `keys.txt` non è soltanto rifiutato: non è nemmeno esprimibile,
perché non è un dataset.

```
dataset="Example Project", path="01 Notes/a.md"   →  quel file
dataset="Example Project", path=""                →  tutto il dataset
```

Anche i ritorni portano path relativi, ed è il punto di tutto l'impianto: la
stringa che un risultato ti consegna è la stessa che i documenti dentro il vault
usano, quindi un path copiato dall'uno all'altro continua a significare quello
che dice. Ripetere il dataset in testa al `path` è **rifiutato**, mai corretto
in silenzio — sulle letture esattamente come sulle scritture, perché una lettura
che normalizza insegna la forma sbagliata senza lamentarsi mai.

### Le chiavi

Un dataset che ha una riga in `keys.txt` è **locked**: ogni chiamata deve portare
la chiave. Senza riga è **open**.

La chiave non serve a tenere fuori gli estranei — di quello si occupa OAuth, e
solo un account può entrare. Serve a **separare i progetti fra loro**. Il caso
concreto: una chat aperta al volo, fuori da ogni progetto, che si mette a leggere
i dati di un progetto serio perché sa che esistono. La chiave sta nelle istruzioni
del progetto, quindi ce l'hanno in contesto solo le chat lanciate lì dentro.

Da qui discende una regola che sostituisce da sola tre meccanismi che sarebbero
serviti altrimenti: **la presenza di una chiave è la dichiarazione che quei dati
contano.** Un dataset con chiave non si droppa dai tool, punto. Uno senza sì,
perché è roba nata per essere buttata.

---

## Com'è fatto

Ogni pezzo è stato scelto per una ragione precisa, e vale la pena dirle: sono le
stesse che ti servono se vuoi adattarlo.

### MCP — Model Context Protocol

Il protocollo con cui Claude parla con strumenti esterni. Un server MCP espone
dei **tool**: funzioni con un nome, dei parametri tipizzati e una descrizione.
Claude legge le descrizioni e decide da solo quando chiamarli.

Questo ha una conseguenza che governa tutto il design: **la descrizione di ogni
tool viaggia in testa a ogni richiesta**, sempre, anche quando non ne usi
nessuno — e arriva *isolata*, letta senza le altre venti sotto gli occhi. Per
questo i tool non si moltiplicano per gusto, e per questo il lavoro è diviso:
la descrizione porta solo ciò che evita un danno quando il manuale non è stato
letto, mentre tutto ciò che richiede il quadro d'insieme sta in
`reference_guide()`, che si carica quando serve. La documentazione estesa per
gli umani sta in questo README, che non costa niente a nessuno.

### FastMCP

L'implementazione Python del protocollo. Gestisce il trasporto HTTP, la
serializzazione degli schemi e — la parte che vale davvero — l'intera danza
**OAuth 2.1 con Dynamic Client Registration e PKCE**, che è quello che Claude
pretende da un connettore remoto. Scriverla a mano sarebbe stato il grosso del
lavoro.

### OAuth 2.1 con login GitHub

Il servizio non ha utenti propri: delega il login a GitHub e poi **rifiuta
chiunque non sia l'unico username configurato**. Chiunque su GitHub può *tentare*
il login; il no lo dice il server, non GitHub.

Perché GitHub e non una password: una password su un servizio esposto è un
segreto che vive in chiaro da qualche parte e non ha revoca. Un'identità OAuth ha
scadenza, revoca e nessun segreto lato client.

### Tailscale Funnel

Il servizio ascolta su `127.0.0.1` e **non sa** come gli arriva il traffico. Il
Funnel gira nello stesso container e pubblica quella porta su un URL HTTPS
pubblico con certificato valido, senza aprire una singola porta sul router e
senza esporre l'IP di casa.

Il disaccoppiamento è voluto: se domani metti un reverse proxy al posto del
Funnel, non cambi una riga di codice.

### Git, server-side

Ogni scrittura fa un commit. Non è un backup: è **memoria dell'intenzione**.
`history` dice cosa è successo, `diff` cosa è cambiato, `read_at` com'era,
`dataset_restore` lo riporta indietro.

E c'è un dettaglio che fa la differenza nell'uso reale: se qualcuno scrive nel
vault **da fuori** dei tool — via SMB, a mano, con un editor — il server se ne
accorge e committa quelle modifiche **a parte**, con un messaggio onesto, prima
di eseguire la propria. I commit dei tool restano puri e la storia non mente
nemmeno per sbaglio.

### Docker su Unraid

Il container parte come root solo per sistemare i permessi, poi **lascia i
privilegi** e gira come `nobody:users` con umask 000, così i file restano
accessibili anche dalle share SMB.

### Preflight bloccante

All'avvio girano i controlli di preflight. Se **uno solo** fallisce, il servizio **non
parte** — e un controllo che va in crash conta come fallito, non come passato.

Sembra eccessivo finché non ti capita: un mount sbagliato che rende il vault
vuoto, un Funnel che pubblica la porta sbagliata, una chiave del nodo con
scadenza attiva che ti spegne tutto fra sei mesi. Meglio un servizio che si
rifiuta di partire dicendoti perché, di uno che parte e funziona male.

---

## Architettura

```
   Claude (server Anthropic)
        │  HTTPS + OAuth 2.1 (DCR + PKCE)
        ▼
   Tailscale Funnel  ──►  https://<host>.<tailnet>.ts.net
        │  (nello stesso container)
        ▼
   127.0.0.1:3000   server.py  ── 21 tool MCP
        │                        ├─ filtro identità GitHub
        │                        └─ filtro IP sorgente (lista)
        ▼
   vault.py  ── VaultRoot (dataset, chiavi)  ──►  Dataset (file + git)
        │
        ▼
   /vault  ── un repository git per dataset
```

---

## Installazione

<details>
<summary><b>1 · Prerequisiti</b></summary>

- Un tailnet Tailscale con **MagicDNS** e **HTTPS Certificates** attivi.
- Unraid 7 con il **plugin Tailscale** installato: fornisce l'hook Docker che dà
  al container un'identità Tailscale propria. Non disinstallarlo mai, anche se
  Tailscale sull'host è disattivato.
- Sull'host: **Allow Tailscale Funnel = No**. Il Funnel è del container, non
  dell'host.
- Un pool **SSD** per il vault. I dischi meccanici pagano lo spin-up a ogni
  tocco, e questo servizio tocca spesso.
- Un account GitHub.

</details>

<details>
<summary><b>2 · Applicazione OAuth su GitHub</b> — cinque minuti</summary>

`github.com` → Settings → Developer settings → OAuth Apps → **New OAuth App**

| Campo | Valore |
|---|---|
| Application name | un nome qualsiasi, es. `archivist-mcp` |
| Homepage URL | `https://<host>.<tailnet>.ts.net` |
| Authorization callback URL | `https://<host>.<tailnet>.ts.net/auth/callback` |

*Generate a new client secret*, poi salva **Client ID** e **Client Secret** nel
gestore di password: il secret si vede una volta sola, ma non scade.

⚠ **Un'applicazione nuova per ogni servizio.** Non riciclare quella di un altro
container: la callback è una sola e i due si contendono.

</details>

<details>
<summary><b>3 · Il vault sul disco</b></summary>

```sh
zfs create <pool-ssd>/Vault
mkdir "/mnt/<pool-ssd>/Vault/Example Project"
chown -R 99:100 "/mnt/<pool-ssd>/Vault"
chmod -R 777 "/mnt/<pool-ssd>/Vault"
```

Ci copi dentro i tuoi file con `rsync` o `scp`. Il repository git lo crea il
server al primo avvio: non serve fare `git init` a mano, e non serve che i file
appartengano a qualcuno in particolare — l'entrypoint dichiara `safe.directory`
per evitare il *dubious ownership* di git.

⚠ **Snapshot ZFS sul dataset `Vault`**, non sui singoli progetti. Gli snapshot
sono la rete per la catastrofe e proteggono anche i `.git`; il rollback
quotidiano è mestiere di git, non loro.

⚠ Nel container si monta il **path diretto del pool** (`/mnt/<pool>/Vault`), mai
`/mnt/user/...`: niente FUSE in mezzo.

</details>

<details>
<summary><b>4 · Il registro chiavi</b></summary>

```sh
printf 'Example Project\tk7m2xq4p\n' > "/mnt/<pool-ssd>/Vault/keys.txt"
chown 99:100 "/mnt/<pool-ssd>/Vault/keys.txt"
chmod 640    "/mnt/<pool-ssd>/Vault/keys.txt"
```

Nome del dataset, **TAB**, chiave. Una riga per dataset; righe vuote e righe che
iniziano con `#` sono commenti.

Otto caratteri alfanumerici bastano: davanti c'è già OAuth, e la minaccia è una
chat che tira a indovinare, non un attacco a forza bruta. Evita `0/O` e `1/l`,
che le ricopierai a mano.

`640` con owner `99:100`: il servizio legge, il resto del mondo no. **Non**
root-only — il servizio non gira come root e non riuscirebbe ad aprirlo.

Il file viene **riletto a caldo**: aggiungi o togli una riga dall'editor di
Unraid e ha effetto subito, senza riavviare niente.

Sta dentro il vault ma è irraggiungibile dai tool, perché il suo nome non è
quello di un dataset — lo ferma lo stesso controllo che ferma `..` e `.git`.

</details>

<details>
<summary><b>5 · Costruire l'immagine</b></summary>

```sh
mkdir -p /mnt/user/appdata/archivist-mcp/src
# copia qui i file del pacchetto, poi:
docker build --no-cache -t archivist-mcp /mnt/user/appdata/archivist-mcp/src
```

⚠ **`--no-cache` non è pedanteria.** La cache di Docker ha già mentito almeno una
volta, dichiarando `CACHED` uno strato il cui file era cambiato. Ci si perde
un'ora a collaudare l'immagine vecchia convinti di aver corretto qualcosa.

Prima di installare, collauda il motore senza rete e senza Docker:

```sh
python3 test_vault.py     # i controlli sul motore, devono passare tutti
```

Metà di quei controlli verifica cose che **non** devono succedere — traversal,
chiavi sbagliate, drop di dataset protetti — e sono quelli che contano di più.

</details>

<details>
<summary><b>6 · Il container</b></summary>

Importa `archivist-mcp.xml` in Unraid, oppure crea il container a mano.
Ogni campo ha la sua descrizione nell'interfaccia; qui il riassunto.

**Path**

| Nome | Host → Container |
|---|---|
| Vault | `/mnt/<pool>/Vault/` → `/vault` |
| App Data | `/mnt/user/appdata/archivist-mcp/data` → `/data` |
| Tailscale State | `/mnt/user/appdata/archivist-mcp/ts-state` → `/var/lib/tailscale` |

**Variabili**

| Variabile | Valore |
|---|---|
| `VAULT_ROOT` | `/vault` |
| `KEYS_FILE` | `/vault/keys.txt` |
| `GIT_RETENTION_MONTHS` | `0` (disattivata) |
| `BASE_URL` | `https://<host>.<tailnet>.ts.net` — **senza barra finale** |
| `GITHUB_CLIENT_ID` | dal punto 2 |
| `GITHUB_CLIENT_SECRET` | dal punto 2 |
| `ALLOWED_GITHUB_LOGIN` | il tuo username GitHub |
| `JWT_SIGNING_KEY` | `openssl rand -hex 32` |
| `PORT` | `3000` |
| `ALLOWED_CIDRS` | `160.79.104.0/21 # documented egress of the model provider` |

**Tailscale**: Enabled `true`, Hostname `<host>`, Serve `funnel`, Serve Port
**uguale a `PORT`**, State Dir `/var/lib/tailscale`.

Poi **Apply**, mai Restart. Restart riavvia il container esistente con la
configurazione vecchia; solo Apply lo ricrea leggendo il template aggiornato.

</details>

<details>
<summary><b>7 · Primo avvio e collegamento</b></summary>

Nei log del container devi vedere, in ordine: l'init dei repo git per dataset, la
sistemazione dei permessi, il drop dei privilegi, il **preflight 10/10**, e infine
l'avvio del server.

Se il preflight blocca, il messaggio dice quale controllo e perché. Non è un
avviso: il servizio non è partito.

Poi, in Claude: **Impostazioni → Connettori → Aggiungi connettore personalizzato**,
URL `https://<host>.<tailnet>.ts.net/mcp`. Si apre il login GitHub, autorizzi, e i
tool compaiono.

Prova subito, in quest'ordine:

```
vault_status()                                  → deve elencare i dataset
dataset_status("Example Project", "")           → deve essere RIFIUTATO
dataset_status("Example Project", "k7m2xq4p")   → deve rispondere
dataset_create("Scratch")                       → nasce aperto
list_files("Scratch")                           → funziona senza chiave
dataset_drop("Example Project", "<manifest>")   → deve essere RIFIUTATO
```

Infine incolla la chiave nelle **istruzioni del progetto** a cui il dataset
appartiene. Da quel momento solo le chat lanciate dentro quel progetto ce l'hanno
in contesto.

</details>

<details>
<summary><b>8 · Dopo ogni modifica ai tool</b></summary>

Ci sono **tre livelli di cache**: il server, il connettore e la sessione di chat.

Dopo qualunque cambiamento alla superficie dei tool — nomi, parametri, docstring —
serve **disconnettere e riconnettere il connettore** in Claude, e collaudare **in
una chat nuova**. Se salti questo passo vedrai i tool vecchi e penserai che il
deploy non abbia funzionato.

Le modifiche interne al comportamento (limiti, formati, logica) non cambiano la
superficie: basta ricreare il container.

</details>

---

## Manutenzione e guasti

<details>
<summary><b>La cassaforte — cosa non si rigenera</b></summary>

| Cosa | Dove vive | Se la perdi |
|---|---|---|
| `GITHUB_CLIENT_ID` + `SECRET` | OAuth App su GitHub | se ne crea una nuova in 5 minuti, poi si aggiorna il template |
| `JWT_SIGNING_KEY` | solo nel template | i token salvati diventano illeggibili: riconnetti il connettore e via. **Ma non cambiarla mai senza motivo**, perché l'effetto è lo stesso |
| Le chiavi in `keys.txt` | il vault | vanno riscritte, e ricopiate nelle istruzioni dei progetti |
| **Il vault** | il dataset ZFS + snapshot + git | l'unica perdita vera |

⚠ Il template che Unraid salva in
`/boot/config/plugins/dockerMan/templates-user/` contiene i segreti **in chiaro**
anche per i campi mascherati. Quel backup è materiale sensibile: la copia
condivisibile è il template sanitizzato del pacchetto.

</details>

<details>
<summary><b>Trappole già pagate</b></summary>

- **La cache di Docker mente.** Sempre `--no-cache` dopo aver toccato i sorgenti.
- **Restart ≠ Apply.** Restart riusa la configurazione vecchia.
- **`mkstemp` crea a 600 ignorando l'umask.** Il codice fa `chmod 666` esplicito
  dopo ogni scrittura atomica, altrimenti i file nuovi non sarebbero scrivibili da
  SMB.
- **`git` e il *dubious ownership*.** L'entrypoint dichiara `safe.directory` prima
  di toccare qualsiasi repository.
- **Il permesso Funnel è legato all'identità del nodo.** Se ricrei il container e
  perdi `ts-state`, il nodo si ripresenta come nuovo e il Funnel va riautorizzato.
  Nella policy del tailnet conviene concedere il Funnel a `autogroup:member`
  invece che a nodi specifici.
- **La scadenza della chiave del nodo è un guasto programmato.** Disattivala nella
  console Tailscale, alla voce Machines. Il preflight la controlla proprio perché
  è silenziosa: funziona tutto per sei mesi, poi smette.
- **Gli aggiornamenti automatici di Tailscale possono rompere il Funnel.** È
  successo con la 1.102.1, in cui una regressione faceva fallire le connessioni
  Funnel in ingresso; risolta nella **1.102.2** del 4 agosto 2026. Se ti capita,
  la diagnosi giusta è confrontare la versione con il changelog prima di cercare
  il guasto in casa propria.

</details>

<details>
<summary><b>Il servizio non parte</b></summary>

Il preflight dice quale controllo è fallito. I più frequenti:

| Controllo | Cosa guardare |
|---|---|
| `dataset` | il mount del vault è sbagliato, o punta a una cartella vuota |
| `git` | i repository non ci sono ancora: rilancia, li crea il boot |
| `chiavi` | `keys.txt` non è leggibile da `99:100`, o una riga non ha il TAB |
| `oauth` | una variabile è ancora `CAMBIAMI`, o `BASE_URL` non è https |
| `token_store` | `FASTMCP_HOME` non è sotto `/data`: i token non sopravvivrebbero |
| `funnel` | il Funnel non è attivo, o pubblica una porta diversa da `PORT` |
| `chiave_nodo` | la chiave del nodo ha una scadenza attiva |
| `dns_pubblico` | l'hostname di `BASE_URL` non risolve |

Per collaudare saltando i controlli di rete:
`PREFLIGHT_SKIP="funnel,chiave_nodo,dns_pubblico"`. Mai in esercizio.

</details>

<details>
<summary><b>Il connettore non si collega</b></summary>

Quasi sempre è `BASE_URL` che non combacia **esattamente** con la callback
registrata su GitHub — schema compreso, barra finale compresa. È l'errore numero
uno al primo avvio.

Se il servizio risponde ma i tool non compaiono, è cache: disconnetti e riconnetti
il connettore, poi apri una chat nuova.

</details>

<details>
<summary><b>Ho fatto un pasticcio nel vault</b></summary>

In ordine di gravità crescente:

```
history("Example Project", "file.md", 20)        cosa è successo
read_at("Example Project", "file.md", "<hash>") com'era
write_file(...)                                 rimettilo

diff("Example Project", "HEAD~5")               cosa è cambiato nel dataset
dataset_restore("Example Project", "<hash>", "<manifest>", key)
```

`dataset_restore` riscrive **tutti** i file del dataset, ma lo fa con un commit in
avanti: la storia non si perde e si torna indietro anche da lì.

Sotto a tutto c'è lo snapshot ZFS, che è la rete per quando è git stesso ad essere
andato.

</details>

---

## Guida d'uso

<details>
<summary><b>Le cinque regole</b></summary>

**1. Ogni tool restituisce un verdetto, non un dump.** Il ritorno è un oggetto
piccolo con dentro i fatti: sha, conteggi, byte, hash del commit. Il contenuto
viaggia solo quando l'hai chiesto. Per *sapere* si usano `search`, `manifest`,
`list_files`; per *leggere*, `read_file`.

**2. Lo sha256 è l'unità di verità.** Ogni lettura lo dà, ogni scrittura lo
pretende:

```
read_file(ds, "X") → sha256: a3f9…
                     ↓
write_file(ds, "X", nuovo, expected_sha256="a3f9…")
```

Se nel frattempo il file è cambiato, la scrittura è **rifiutata senza toccare
niente**. Si chiama compare-and-swap. Per creare un file nuovo:
`expected_sha256="new"`.

**Ogni scrittura restituisce lo sha nuovo**, quindi una catena non ha bisogno di
riletture intermedie: lo `sha256` che torna da `write_file` va dritto
nell'`edit_file` che segue. Anche `append` lo restituisce. `move_path` no, e non
serve — il contenuto non è cambiato, quindi vale ancora quello che avevi.

**3. Non si cancella.** Non esiste un tool `delete`. Lo smaltimento è `move_path`
verso `Trash/`, e `move_path` non sovrascrive mai.

**4. Ogni scrittura è atomica, verificata e committata.** Lock, eventuale commit
delle modifiche esterne, scrittura su temporaneo, `os.replace` (atomico),
rilettura e confronto sha, commit. Se un tool fallisce, il vault è esattamente
come prima.

**5. `external_commit_first` non è un errore.** Significa che il repo era
sporco e le modifiche arrivate da fuori sono state committate a parte prima della
tua.

</details>

<details>
<summary><b>Quale tool per quale mestiere</b></summary>

| Vuoi | Usa | Sha? |
|---|---|---|
| aggiungere righe a un registro o a un log | `append` | no |
| cambiare una frase o un numero | `edit_file` | sì |
| rifare il file, o crearlo | `write_file` | sì (`"new"` se nuovo) |
| scrivere un PDF o un binario | `write_binary` | sì |
| spostare, rinominare, cestinare | `move_path` | no |
| sapere se una cosa c'è e dove | `search` | — |
| sapere quali file ci sono | `list_files` | — |
| sapere se un albero è cambiato o no | `manifest` | — |
| sapere quanto è grosso un dataset, quanto sporco, quanti commit | `dataset_status` | — |
| sapere quando una cosa è cambiata, e prenderne l'hash | `history` | — |
| leggere testo | `read_file` | — |
| leggere un PDF o un binario | `read_binary` (serve una sandbox) | — |
| leggere un albero intero in un colpo | `archive` (serve una sandbox) | — |
| leggere com'era prima | `read_at` | — |
| vedere cosa è cambiato fra due momenti | `diff` | — |

`append` non chiede lo sha perché **non tocca mai i byte esistenti**: non c'è
conflitto possibile, quindi non c'è niente da proteggere. È l'operazione giusta
per log e registri.

`edit_file` fa viaggiare solo i due frammenti invece del file intero: su un file
da 80 KB è la differenza fra una chiamata leggera e una pesante.

</details>

<details>
<summary><b>I 21 tool</b></summary>

Ogni tool di livello dataset prende `dataset` per primo, e `path` relativo a
quello; un `path` vuoto significa tutto il dataset. `key` è accettato da tutti e
serve solo per quelli protetti: negli esempi è omesso. I ritorni sono elencati
per chiave, e ogni ritorno che porta un path porta anche `dataset`, così la
coppia si ricompone. Ogni scrittura può portare in più `external_commit_first`,
quando modifiche fatte fuori dai tool sono state committate prima della tua — è
un'informazione, non un errore.

### Livello vault — senza chiave

**`vault_status()`** — il vault risponde, e quali dataset esistono.
**Ritorna** `vault · version · guide · datasets[{name, state}]`

```
vault_status()
→ {"vault": "ok", "version": "…",
   "guide": "call reference_guide() for the manual",
   "datasets": [{"name": "Example Project", "state": "open"},
                {"name": "Ledger",          "state": "locked"}]}
```

**`reference_guide()`** — il manuale, servito dall'immagine.
**Ritorna** `version · guide`

```
reference_guide()
→ {"version": "…", "guide": "# Archivist MCP — manual\n\n## THE MODEL\n…"}
```

**`dataset_create(name)`** — un dataset nuovo: aperto, vuoto, col suo git.
**Ritorna** `dataset · state · git · note`

```
dataset_create("Scratch")
```

**`dataset_drop(dataset, expected_manifest)`** — cancella un dataset **aperto**
e tutto il suo contenuto. `expected_manifest` è il `manifest_sha256` corrente:
non si butta ciò che non si è guardato. Un dataset con chiave rifiuta.
**Ritorna** `dropped · files_removed · note`

```
manifest("Scratch")                       # → manifest_sha256: 7c1e…
dataset_drop("Scratch", "7c1e…")
```

Un nome di dataset è l'UNICA cosa che `dataset` accetta. `keys.txt`, i lockfile
e qualunque altra cosa stia nella radice del vault non sono dataset, quindi non
si possono nominare — il rifiuto non costa nessuna denylist.

### Livello dataset — tutti accettano anche `key=""`

**`dataset_status(dataset)`** — un dataset in dettaglio.
**Ritorna** `dataset · total_files · md_files · files_in_trash · git ·
total_commits · git_size_bytes · last_commit`

```
dataset_status("Example Project")
→ {"dataset": "Example Project", "total_files": 18, "md_files": 14,
   "files_in_trash": 2, "git": "clean", "total_commits": 62,
   "git_size_bytes": 423676,
   "last_commit": "2781f59 2026-08-08T18:30:51+02:00 edit: Notes.md"}
```

**`list_files(dataset, path="")`** — elenco ricorsivo con taglia e sha di ogni
file. Un path vuoto elenca tutto il dataset.
**Ritorna** `dataset · base · count · files[{path, size, sha256}]`
(su un file solo: `dataset · file · size · sha256`)

```
list_files("Example Project", "01 Notes")
→ {"dataset": "Example Project", "base": "01 Notes", "count": 2,
   "files": [{"path": "01 Notes/a.md", "size": 412, "sha256": "a3f9…"}, …]}
```

**`read_file(dataset, path)`** — un file di testo UTF-8. Lo `sha256` che
restituisce è quello che `write_file` e `edit_file` vogliono indietro.
**Ritorna** `dataset · path · size · sha256 · content`

```
read_file("Example Project", "01 Notes/a.md")
→ {"dataset": "Example Project", "path": "01 Notes/a.md", "size": 412,
   "sha256": "a3f9…", "content": "# Notes\n…"}
```

**`read_binary(dataset, path)`** — un file qualsiasi come base64. Max 2 MB.
Inutile senza una sandbox in cui decodificarlo.
**Ritorna** `dataset · path · size · sha256 · content_base64`

```
read_binary("Example Project", "Scans/invoice.pdf")
```

**`read_at(dataset, path, rev)`** — il file com'era a una revisione passata.
Sola lettura. `rev` è un hash corto da `history`, oppure `"HEAD~3"`. Le
revisioni sono quelle del dataset, non del vault.
**Ritorna** `dataset · path · rev · size · sha256 · content`

```
read_at("Example Project", "01 Notes/a.md", "HEAD~3")
```

**`search(dataset, pattern, path="", regex=False)`** — grep sul server: non
scarica niente. Solo file di testo; i binari si trovano per nome con
`list_files`.
**Ritorna** `dataset · base · pattern · files_scanned · matches · truncated ·
lines[]`

```
search("Example Project", "scadenza")
→ {"dataset": "Example Project", "pattern": "scadenza", "files_scanned": 14,
   "matches": 3, "truncated": false,
   "lines": ["01 Notes/a.md:31: la scadenza è …", …]}
```

**`manifest(dataset, path="")`** — l'impronta di un albero in un numero solo.
Due manifest uguali significano alberi identici. Obbligatorio per
`dataset_drop` e `dataset_restore`.
**Ritorna** `dataset · base · file_count · total_bytes · manifest_sha256`

```
manifest("Example Project")
→ {"dataset": "Example Project", "base": "", "file_count": 8,
   "total_bytes": 147600, "manifest_sha256": "24280b93…"}
```

**`archive(dataset, path="", pattern="*.md")`** — tutti i file che
corrispondono in UNA chiamata, come tar.gz in base64. Sostituisce centinaia di
`read_file` in un audit; serve una sandbox per estrarlo. I nomi dentro il tgz
sono relativi al dataset, quindi estrarlo riproduce l'albero del dataset.
**Ritorna** `dataset · base · file_count · original_bytes · tgz_bytes ·
tgz_base64`

```
archive("Example Project", "", "*.md")
```

**`append(dataset, path, text)`** — un blocco in coda a un file esistente. Non
tocca mai i byte già scritti, quindi **non chiede lo sha**: è l'operazione dei
log e dei registri. Max 64 KB.
**Ritorna** `dataset · path · size · sha256 · commit`

```
append("Example Project", "Log.md", "\n- 2026-08-08 · riconciliato\n")
→ {"dataset": "Example Project", "path": "Log.md", "size": 2140,
   "sha256": "b71c…", "commit": "9da9597"}
```

**`write_file(dataset, path, content, expected_sha256)`** — TUTTO il file.
Compare-and-swap: `expected_sha256` deve combaciare con lo sha corrente, oppure
`"new"` per un file che non esiste ancora. Se non combacia la scrittura è
rifiutata senza toccare niente. UTF-8, max 2 MB.
**Ritorna** `dataset · path · size · sha256 · commit`

```
read_file("Example Project", "a.md")       # → sha256: a3f9…
write_file("Example Project", "a.md", "# Notes\nriscritto\n", "a3f9…")
write_file("Example Project", "nuovo.md", "# Fresco\n", "new")
```

**`write_binary(dataset, path, content_base64, expected_sha256)`** — stesso
compare-and-swap, da base64. Max 2 MB decodificati. Confronta sempre lo sha
restituito con quello calcolato alla sorgente: il base64 viaggia come testo
generato.
**Ritorna** `dataset · path · size · sha256 · commit`

```
write_binary("Example Project", "Scans/invoice.pdf", "JVBERi0…", "new")
```

**`edit_file(dataset, path, old_text, new_text, expected_sha256)`** —
sostituisce `old_text`, che deve comparire **esattamente una volta**, con
`new_text`. Viaggiano solo i due frammenti, non il file. Stesso
compare-and-swap di `write_file`.
**Ritorna** `dataset · path · size · sha256 · commit`

```
read_file("Example Project", "a.md")       # → sha256: a3f9…
edit_file("Example Project", "a.md", "retention: 0", "retention: 6", "a3f9…")
```

**`move_path(dataset, src, dst)`** — sposta, rinomina o cestina. I due path
sono relativi allo stesso dataset, quindi uno spostamento fra dataset non è
nemmeno esprimibile. Non sovrascrive mai. Non esiste un tool per cancellare:
spostare in `Trash/` è la via di smaltimento, e azzera l'mtime del file così che
`trash_purge` possa datarlo.
**Ritorna** `dataset · from · to · trashed · commit`

```
move_path("Example Project", "a.md", "Trash/a.md")
→ {"dataset": "Example Project", "from": "a.md", "to": "Trash/a.md",
   "trashed": true, "commit": "0583255"}
```

**`history(dataset, path="", n=10)`** — gli ultimi n commit. Un path vuoto dà
la storia del dataset; un file dà la sua, seguendo anche i rinomini. L'hash
corto si passa tale e quale a `read_at` e `diff`.
**Ritorna** `dataset · path · entries[]`

```
history("Example Project", "a.md", 2)
→ {"dataset": "Example Project", "path": "a.md",
   "entries": ["2781f59 · 2026-08-08T18:30:51+02:00 · edit: a.md",
               "0583255 · 2026-08-08T18:22:33+02:00 · edit: a.md"]}
```

**`diff(dataset, rev_a, path="", rev_b="HEAD")`** — differenze fra due
revisioni. Un path vuoto dà il riepilogo per file; un file dà il suo diff
completo. Tronca a 60 KB invece di fallire.
**Ritorna** `dataset · path · from · to · diff`

```
diff("Example Project", "HEAD~1")
diff("Example Project", "HEAD~5", "a.md")
```

**`dataset_restore(dataset, rev, expected_manifest)`** — ⚠ riscrive OGNI file
del dataset riportandolo a `rev`. Non è distruttivo: è un commit **in avanti**,
quindi la storia non si perde e il restore stesso si può annullare. Prima
controlla la revisione con `history`.
**Ritorna** `dataset · restored_from · commit · file_count · manifest_sha256`

```
history("Example Project", "", 5)         # scegli la revisione
manifest("Example Project")               # → manifest_sha256: 2428…
dataset_restore("Example Project", "0583255", "2428…")
```

**`trash_purge(dataset, before)`** — svuota `Trash/` di tutto ciò che è stato
cestinato **prima** di una data ISO. La data è quella di cestinamento, non
quella dell'ultima modifica. I contenuti restano nella storia git: toglie
ingombro, non distrugge informazione.
**Ritorna** `dataset · before · removed · bytes_freed · files · note`
(più `commit`, quando qualcosa è stato davvero rimosso)

```
trash_purge("Example Project", "2026-06-01")
```

</details>

<details>
<summary><b>Limiti</b></summary>

| Limite | Valore |
|---|---|
| lettura e scrittura testo | 2 MB |
| binari | 2 MB |
| blocco di `append` | 64 KB |
| file elencabili | 3.000 |
| righe di `search` | 200 |
| `diff` | 60 KB (tronca, non fallisce) |
| `archive` in ingresso | 30 MB non compressi |
| `archive` in uscita | 5 MB di tgz |

I limiti sui binari sono tarati sul consumo reale: un file più grande di 2 MB non
è comunque utilizzabile dentro una conversazione. Meglio un rifiuto parlante che
un fallimento muto più a valle. Sopra quella soglia i file viaggiano via SMB o
`scp`, e il vault fa da archivista.

</details>

<details>
<summary><b>Ricette</b></summary>

Scritte con `X` al posto del dataset.

```
modificare un numero:   read_file → sha → edit_file(X, path, vecchio, nuovo, sha)
aggiungere a un log:    append(X, path, riga)
creare un documento:    write_file(X, path, contenuto, "new")
archiviare:             move_path(X, "doc.md", "Trash/doc.md")
trovare qualcosa:       search(X, "termine") → read_file solo sul file giusto
recuperare:             history → read_at(X, path, hash) → write_file
verificare due momenti: manifest prima, manifest dopo — uguali = niente si è mosso
audit completo:         manifest → list_files → archive → verifica sha → manifest
copiare fra dataset:    read_file("A", "x.md") → write_file("B", "x.md", …, "new")
```

</details>

<details>
<summary><b>Errori e cosa farci</b></summary>

I messaggi del server sono in inglese: qui sono riportati come escono davvero.

| Messaggio | Cura |
|---|---|
| `dataset ... is protected: a key is required` | la chiave sta nelle istruzioni del progetto |
| `no such dataset` | `vault_status` per l'elenco; il dataset va in `dataset`, non nel `path` |
| `path must be relative to the dataset` | il dataset è ripetuto in testa al `path`: toglilo |
| `path is relative to the dataset, not absolute` | togli la `/` iniziale |
| `CONFLICT: expected sha ...` | rileggi, riconcilia, riprova |
| `the file already exists` | hai usato `"new"` su un file esistente |
| `the file does not exist` | il contrario: passa `"new"` |
| `old_text NOT found` | rileggi e copia il frammento esatto |
| `old_text found N times` | allunga il contesto finché è unico |
| `path not allowed` | c'è un `..` o `.git` nel path |
| `destination already exists` | `move_path` non sovrascrive mai |
| `more than 3000 files` | scendi di un livello |
| `block too large` | `append` non è per riscritture |

Un errore non lascia mai scritture parziali.

</details>

---

## Cosa non fa, di proposito

Nessun tool "esegui comando". Nessuna cancellazione di file. Nessun `git gc
--prune` a richiesta. Nessun dump: ogni tool restituisce un verdetto, perché ogni
byte che torna indietro finisce nel contesto della conversazione, e il contesto è
la risorsa scarsa.

E una cosa che è bene dire chiaramente: **le chiavi dei dataset non sono
autenticazione.** Il servizio riconosce un solo account, e tutte le sue
conversazioni hanno la stessa identità — il server non può distinguerle. Le chiavi
funzionano perché una chat che non ha la chiave in contesto non può inventarla:
sono un confine fra progetti, non una difesa contro un attaccante. Quella è OAuth.

## Contenuto del pacchetto

| File | |
|---|---|
| `vault.py` | il motore: `VaultRoot` e `Dataset` |
| `server.py` | i 21 tool MCP; i parametri nello schema, la prosa nella guida |
| `preflight.py` | i controlli bloccanti d'avvio, e il parser del filtro IP |
| `reference-guide.md` | la guida compatta servita da `reference_guide()` |
| `entrypoint.sh` | init, permessi, drop privilegi, preflight, avvio |
| `Dockerfile` · `requirements.txt` | immagine |
| `archivist-mcp.xml` | template Unraid, ogni campo descritto |
| `test_vault.py` | i controlli sul motore, senza rete |

## Licenza e crediti

Questo progetto è rilasciato con licenza **MIT** — vedi [LICENSE](LICENSE).

Si appoggia a componenti di terze parti, che restano dei rispettivi autori con
le rispettive licenze:

| Componente | Autore | Licenza |
|---|---|---|
| [FastMCP](https://github.com/jlowin/fastmcp) | Jeremiah Lowin | Apache-2.0 |
| [Tailscale](https://tailscale.com) | Tailscale Inc. | BSD-3-Clause |
| [Model Context Protocol](https://modelcontextprotocol.io) | Anthropic | MIT |
| Python, git, Debian slim | rispettivi progetti | PSF / GPL-2.0 / varie |

L'immagine Docker distribuita contiene questi componenti installati: le loro
licenze viaggiano con loro dentro l'immagine, come richiesto.
