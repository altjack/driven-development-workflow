# Driven Development Workflow

`feature-flow` è un runner deterministico per sviluppare una feature attraverso
quattro gate indipendenti:

1. definizione e review dei requirements;
2. definizione e review del piano;
3. implementazione e review del codice;
4. validazione E2E e review finale.

Il runner è implementato in Python, ma può essere utilizzato su repository
scritti in qualsiasi linguaggio. Opera tramite Git, file Markdown e processi
esterni, senza imporre uno stack applicativo al progetto consumer.

## Prerequisiti

Sulla macchina che esegue il workflow devono essere disponibili:

- [uv](https://docs.astral.sh/uv/);
- Git;
- Python 3.11 o successivo, installato o gestibile da `uv`;
- le CLI dei provider configurati in `feature_flow/workflow.json`;
- autenticazione valida per le CLI `codex` e `claude`.

Il progetto consumer deve essere un repository Git con almeno un commit. Prima
di iniziare è consigliato avere un working tree pulito: durante il gate `code`
il runner crea automaticamente un commit con le modifiche accettate.

## Installazione

Da un checkout di questo repository:

```bash
uv tool install /path/to/driven-development-workflow
```

Verificare che il comando sia disponibile:

```bash
feature-flow --help
```

Per aggiornare l'installazione dopo una nuova versione:

```bash
uv tool install --reinstall /path/to/driven-development-workflow
```

Il package non deve essere copiato o installato come dipendenza applicativa nel
progetto consumer: `feature-flow` è un tool globale e riceve il percorso del
repository tramite `--repo`.

## Preparazione di un progetto

Entrare nel repository consumer e creare un branch dedicato:

```bash
cd /path/to/project
git switch -c feature/user-login
```

Il workflow non viene eseguito direttamente su `main` o `master`.

Creare la directory della feature. Il percorso deve contenere il nome completo
del branch:

```bash
mkdir -p specs/features/feature/user-login
```

La struttura minima del progetto diventa:

```text
project/
├── .git/
├── specs/
│   └── features/
│       └── feature/
│           └── user-login/
└── ...
```

È possibile fornire ulteriore contesto ai modelli attraverso questi file
opzionali:

```text
AGENTS.md
CLAUDE.md
specs/mission.md
specs/tech-stack.md
specs/roadmap.md
```

`specs/roadmap.md` dovrebbe descrivere la task selezionata, mentre missione e
tech stack aiutano autori e reviewer a rispettare i contratti del progetto.

## Controllo iniziale

Ispezionare lo stato senza modificare il repository:

```bash
feature-flow status --repo .
```

Simulare il comando `run` senza creare stato operativo o chiamare provider:

```bash
feature-flow run --repo . --dry-run
```

L'output è JSON compatto. Il campo `next` indica il gate corrente e il ruolo che
deve intervenire.

## Validazione senza provider reali

Prima del primo utilizzo è possibile verificare l'orchestrazione con il provider
deterministico integrato:

```bash
feature-flow run --repo . --fake
```

Ogni esecuzione completa un solo gate. Ripetere quindi il comando per attraversare
requirements, piano, codice e validazione:

```bash
feature-flow run --repo . --fake
feature-flow run --repo . --fake
feature-flow run --repo . --fake
feature-flow run --repo . --fake
```

La modalità fake modifica il repository e, durante il gate `code`, crea un
commit di prova. Va quindi utilizzata su un branch o repository sacrificabile.

## Esecuzione reale

Avviare il gate corrente:

```bash
feature-flow run --repo .
```

Il comando coordina autore e reviewer fino a quando il gate viene accettato,
richiede una decisione utente oppure raggiunge il limite massimo di review.
Dopo un gate accettato, eseguire nuovamente lo stesso comando per procedere:

```bash
# 1. requirements.md
feature-flow run --repo .

# 2. plan.md
feature-flow run --repo .

# 3. implementazione e review del codice
feature-flow run --repo .

# 4. validation.md
feature-flow run --repo .
```

È possibile controllare il progresso in qualsiasi momento:

```bash
feature-flow status --repo .
```

## Artifact prodotti

Per il branch `feature/user-login`, il workflow produce:

```text
specs/features/feature/user-login/
├── requirements.md
├── plan.md
├── validation.md
└── review-ledger.md
```

Il ledger registra hash, round di review, modelli utilizzati, decisioni e
commit del codice. Un artifact viene considerato accettato soltanto se il suo
hash corrente coincide con quello approvato nel ledger e non restano elementi
aperti.

Al termine della validation il workflow richiede un consenso esplicito prima di
committare questi quattro artifact. Per autorizzare il commit:

```bash
feature-flow resolve commit_artifacts --answer "commit" --repo .
feature-flow run --repo .
```

Per chiudere il workflow lasciandoli nel working tree:

```bash
feature-flow resolve commit_artifacts \
  --answer "leave uncommitted" \
  --repo .
feature-flow run --repo .
```

Il commit degli artifact è separato dal commit applicativo e include soltanto i
quattro file elencati sopra.

Lo stato operativo, le risposte raw dei provider e i log non vengono aggiunti al
progetto. Sono conservati localmente sotto:

```text
~/.local/state/feature-flow/
```

## Decisioni richieste all'utente

Quando il workflow restituisce `status: needs_user`, l'output contiene:

- l'identificativo del finding;
- la domanda;
- le eventuali scelte;
- il comando da utilizzare per riprendere.

Registrare la decisione:

```bash
feature-flow resolve FINDING_ID \
  --answer "Decisione e motivazione" \
  --repo .
```

Poi riprendere il gate:

```bash
feature-flow run --repo .
```

## Comportamento Git

Il runner:

- rifiuta branch detached e non avvia feature su `main` o `master`;
- esclude `specs/` dal diff applicativo sottoposto alla code review;
- esclude file sensibili comuni, chiavi, `.env*` e `node_modules`;
- crea automaticamente il commit del codice accettato;
- rileva modifiche successive tramite hash e riapre il gate necessario;
- consente un solo writer concorrente per repository e branch.

Non iniziare un gate `code` con modifiche applicative non correlate nel working
tree: potrebbero entrare nel diff della feature.

## Utilizzo da un'altra directory

Non è necessario entrare nel repository consumer:

```bash
feature-flow status --repo /path/to/project
feature-flow run --repo /path/to/project
```

## Sviluppo del package

Validare la configurazione dei modelli e dei fallback:

```bash
uv run feature-flow validate-config
```

Per controllare un file candidato senza sostituire quello incluso nel package:

```bash
uv run feature-flow validate-config --config /path/to/workflow.json
```

La validazione controlla versione, campi richiesti, sandbox Codex, indipendenza dei
modelli, trigger duplicati, copertura delle route terminali e raggiungibilità dei
fallback.

Eseguire la suite:

```bash
uv run python -m unittest discover -s tests -v
```

Costruire source distribution e wheel:

```bash
uv build
```

Durante lo sviluppo, la CLI può essere eseguita senza installazione globale:

```bash
uv run feature-flow --help
```
