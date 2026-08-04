# Problemi e miglioramenti del workflow

Data osservazione: 2026-07-29.
Ultima revisione: 2026-07-29.

Questo documento raccoglie i problemi emersi durante:

- gli smoke test del package su una clone temporanea di `ga4_buddy`;
- il primo use case reale, `24-07-2026_ui-upload-token-credenziale-ga4`;
- i passaggi reali `requirements.md`, `plan.md` e `code`;
- le transizioni tra Codex autore e reviewer Codex/Claude previste dai singoli gate.

Le priorità usate sono:

- **P0**: può bloccare il workflow o comprometterne correttezza e audit;
- **P1**: rende il workflow fragile, costoso o difficile da operare;
- **P2**: miglioria importante ma non bloccante.

Gli stati usati sono:

- **Confermato**: il comportamento è ancora presente nel codice corrente;
- **Parzialmente corretto**: il percorso nominale è stato corretto, ma resta un rischio
  o manca una verifica di regressione;
- **Da riprodurre**: l'osservazione non è ancora attribuibile con certezza al runner;
- **Decisione prodotto**: il comportamento è intenzionale o documentato e richiede una
  scelta di UX, non una correzione automatica;
- **Corretto**: il criterio di verifica è soddisfatto e protetto da test.

## Riepilogo

| ID | Priorità | Problema | Stato |
|---|---:|---|---|
| WF-01 | P0 | Un errore transitorio di rete esaurisce i fallback e non è riprendibile | Confermato |
| WF-02 | P0 | La provenienza autore/reviewer nel ledger può diventare incorretta dopo un fallback | Confermato |
| WF-03 | P0 | L'indipendenza autore/reviewer è validata in config, non sullo stato runtime | Confermato |
| WF-04 | P1 | Nessun timeout o limite temporale esplicito per le CLI provider | Confermato |
| WF-05 | P1 | Il run non emette avanzamento intermedio | Confermato |
| WF-06 | P1 | Context pack molto grande, con latenza e costo elevati | Confermato |
| WF-07 | P1 | Fallback e retry non distinguono errori transitori da errori del modello | Confermato |
| WF-08 | P1 | Manca un comando supportato per retry/reset conservativo | Confermato |
| WF-09 | P0 | I commit del codice possono essere creati prima dell'accettazione finale | Confermato |
| WF-10 | P1 | I test del progetto consumer non sono ancora configurabili per linguaggio | Confermato |
| WF-11 | P2 | Bootstrap di branch e directory feature ancora manuale | Confermato |
| WF-12 | P2 | Il parser della roadmap non rappresenta correttamente lo stato delle fasi | Confermato |
| WF-13 | P2 | La chiusura riporta ancora `launcher slice not implemented` | Confermato |
| WF-14 | P1 | Configurazione modelli fragile e test troppo legati ai nomi dei modelli | Corretto |
| WF-15 | P2 | Consenso artifact basato su stringhe libere | Confermato |
| WF-16 | P2 | Possibile ritardo tra completamento del provider e rilascio visibile del processo | Da riprodurre |
| WF-17 | P0 | Il provider può modificare l'artifact senza completare l'handoff strutturato | Confermato |
| WF-18 | P2 | `status` può attendere a lungo durante un `run` | Da riprodurre |
| WF-19 | P1 | I log del provider non sono ispezionabili durante una chiamata bloccata | Confermato |
| WF-20 | P0 | La ripetizione di `run` può riapplicare l'autore su modifiche già presenti | Confermato |
| WF-21 | P0 | L'esito `needs_user` del reviewer non è sempre persistito nello stato | Parzialmente corretto |
| WF-22 | P1 | I finding di validation assegnati all'utente non riaprono il gate code | Confermato |
| WF-23 | P2 | La transizione tra gate accettati richiede un nuovo processo `run` | Decisione prodotto |
| WF-24 | P1 | Lo stato di recovery non è portabile insieme al branch | Confermato |
| WF-25 | P1 | Non esiste un comando supportato per committare artifact con validation pending | Confermato |

Totale: **7 P0**, **11 P1**, **7 P2**. Per stato: **20 confermati**,
**2 da riprodurre**, **1 parzialmente corretto**, **1 decisione prodotto**,
**1 corretto**.

## Elementi tracciati

### WF-01 — Provider failure terminale non riprendibile

**Priorità:** P0

**Stato:** Confermato

**Osservato**

La prima review reale con `claude-opus-5` è fallita per `ENOTFOUND`. Anche il fallback
`gpt-5.6-terra` ha fallito per lo stesso problema di connettività. Il workflow è entrato
in:

```text
status: needs_user
finding: reviewer_provider
resume_phase: null
```

`feature-flow resolve` non poteva riprendere il finding perché mancava una
`resume_phase`.

**Impatto**

Una breve interruzione di rete rende terminale una feature valida. Per continuare è
necessario modificare manualmente `state.json` o ricominciare su un altro branch.

**Miglioria proposta**

- Aggiungere `feature-flow retry --repo ...`.
- Conservare unità, fase, hash target e sessioni valide.
- Permettere il retry dello stesso provider oppure il ripristino del routing primario.
- Registrare retry e motivazione nel ledger.
- Non richiedere la cancellazione dello stato precedente.

**Criterio di verifica**

Simulare un `ENOTFOUND` al primo tentativo, ripristinare la connettività, eseguire
`feature-flow retry` e completare la review sullo stesso hash senza modifiche manuali.

### WF-02 — Provenienza errata nel review ledger dopo fallback

**Priorità:** P0

**Stato:** Confermato

**Osservato**

Il primo `requirements.md` è stato scritto da `gpt-5.6-sol`. Dopo il fallimento del
reviewer, il fallback ha sostituito entrambe le route. Il blocco di review nel ledger ha
poi riportato:

```text
author_model: claude-opus-5
author_session: none
```

anche se quella versione dell'artifact era stata prodotta da Codex.

**Causa probabile**

`append_review()` usa `state["author_model"]` e `state["author_session"]` correnti,
non la provenienza associata all'hash sottoposto a review.

**Impatto**

L'audit non permette di ricostruire correttamente chi ha prodotto una specifica
revisione dell'artifact.

**Miglioria proposta**

- Salvare `target_author_provider`, `target_author_model` e
  `target_author_session` quando cambia l'hash.
- Non sovrascrivere questi campi quando cambia soltanto la route futura.
- Scrivere nel ledger la provenienza legata a `target_hash`.

**Criterio di verifica**

Far scrivere un artifact a Codex, fallire il reviewer Claude e applicare un fallback:
il round deve continuare ad attribuire l'hash a Codex.

### WF-03 — Indipendenza autore/reviewer non garantita a runtime

**Priorità:** P0

**Stato:** Confermato

**Osservato**

`load_config()` impedisce coppie con lo stesso modello, ma uno stato ripreso o migrato
può contenere autore e reviewer uguali. Durante il recovery manuale è stato possibile
ottenere:

```text
author: claude-opus-5
reviewer: claude-opus-5
```

**Impatto**

Il workflow può effettuare auto-review, violando uno dei suoi invarianti principali.

**Miglioria proposta**

- Validare provider, modello e sessione prima di ogni chiamata.
- Rifiutare uno stato runtime in cui autore e reviewer non sono indipendenti.
- Aggiungere una migrazione/normalizzazione sicura dello stato.

**Criterio di verifica**

Alterare uno state fixture con la stessa route per entrambi i ruoli: `run` deve
fermarsi prima di invocare un provider e proporre un recovery valido.

### WF-04 — Assenza di timeout provider

**Priorità:** P1

**Stato:** Confermato

**Osservato**

- Un fallimento Claude per rete ha impiegato circa 213 secondi.
- La review Opus 5 riuscita ha impiegato circa 190 secondi.
- `subprocess.run()` non riceve un timeout.

**Impatto**

Una CLI bloccata può mantenere il lock del branch indefinitamente.

**Miglioria proposta**

- Timeout configurabile per autore e reviewer.
- Terminazione controllata del process group.
- Stato `provider_timeout` distinto da `provider_failure`.
- Retry con backoff e limite massimo.

### WF-05 — Nessun avanzamento intermedio

**Priorità:** P1

**Stato:** Confermato

**Osservato**

`feature-flow run` non emette output finché l'intero gate non termina. Durante chiamate
di diversi minuti non è visibile se il workflow sta:

- aspettando il lock;
- eseguendo l'autore;
- eseguendo il reviewer;
- applicando un fallback;
- aspettando una seconda review.

`feature-flow status` mostra la fase, ma richiede polling esterno e non espone durata,
PID o ultimo evento.

**Miglioria proposta**

- Eventi JSON Lines su stderr oppure opzione `--progress`.
- Campi `started_at`, `updated_at`, `provider_pid` e `last_event` nello stato.
- `feature-flow status --watch`.
- Output finale JSON invariato su stdout.

### WF-06 — Context pack troppo grande

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Nel primo use case reale:

- l'autore Codex ha ricevuto oltre 140k input token;
- la review Opus 5 ha letto centinaia di migliaia di token cached;
- una singola review Opus ha avuto un costo osservato di circa 1,36 USD.

Il context pack include documenti di progetto estesi, tra cui l'intera roadmap.

**Impatto**

Latenza, costo e rischio di distrazione crescono rapidamente su repository maturi.

**Miglioria proposta**

- Estrarre soltanto la fase di roadmap associata al branch.
- Includere sezioni pertinenti di mission e tech stack.
- Aggiungere budget per file e budget totale.
- Registrare dimensione e stima token del context pack prima della chiamata.
- Fermarsi se il budget viene superato, invece di inviare silenziosamente tutto.

### WF-07 — Errori transitori trattati come indisponibilità del modello

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Gli errori DNS di Claude e Codex hanno consumato la catena di fallback come se i
modelli fossero permanentemente non disponibili.

**Impatto**

Un problema comune a tutta la rete porta a cambiare modelli inutilmente e termina il
workflow senza aumentare la probabilità di successo.

**Miglioria proposta**

Classificare almeno:

- rete/DNS/timeout;
- autenticazione;
- modello inesistente o non autorizzato;
- rate limit;
- output strutturato invalido;
- errore interno del provider.

Applicare fallback di modello soltanto alle categorie pertinenti.

### WF-08 — Nessuna recovery API supportata

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Per riprendere il primo run reale è stato necessario:

1. fare una copia manuale di `state.json`;
2. cambiare `status`, `phase` e routing;
3. eliminare `finding` e `resume_phase`;
4. verificare manualmente che l'hash non fosse cambiato.

**Impatto**

La recovery è fragile e può produrre combinazioni di stato non validate.

**Miglioria proposta**

Introdurre comandi supportati:

```text
feature-flow retry
feature-flow retry --provider claude
feature-flow reset-unit requirements.md
feature-flow doctor
```

Ogni operazione deve creare un backup e una voce di audit.

### WF-09 — Commit del codice prima dell'accettazione finale

**Priorità:** P0

**Stato:** Confermato

**Osservato dal codice del runner**

`commit_code()` viene eseguito dopo ogni authoring del gate `code`, prima che il
reviewer accetti il diff. Se il reviewer richiede modifiche, il branch può contenere
commit intermedi non accettati. Se il gate termina in `needs_user`, quei commit restano.

**Impatto**

La cronologia Git non coincide necessariamente con il risultato approvato.

**Miglioria proposta**

- Mantenere le modifiche nel working tree fino all'accettazione, oppure
- usare commit temporanei chiaramente marcati e creare un commit finale/squash dopo
  consenso.
- Non riscrivere automaticamente la storia senza consenso.

**Criterio di verifica**

Una review `needs_changes` o `needs_user` non deve lasciare nella cronologia del branch
un commit presentato come accettato. Il commit applicativo finale deve corrispondere
all'hash approvato dal reviewer.

### WF-10 — Test consumer non ancora language-agnostic

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Non esiste ancora una configurazione consumer come `.feature-flow.json` per dichiarare:

- test;
- lint;
- build;
- E2E.

Il fake provider dichiara `python3 -m unittest: passed` senza eseguire realmente i test
di `ga4_buddy`. Anche nel run reale l'evidenza dei test dipende dalla dichiarazione del
modello.

**Impatto**

Il package è installabile indipendentemente dal progetto, ma i gate di verifica non
sono ancora realmente indipendenti dal linguaggio né deterministicamente eseguiti.

**Miglioria proposta**

- Aggiungere `.feature-flow.json`.
- Eseguire comandi configurati tramite un command runner del workflow.
- Salvare exit code, durata e log.
- Distinguere test dichiarati dal modello e test verificati dal runner.

### WF-11 — Bootstrap feature manuale

**Priorità:** P2

**Stato:** Confermato

**Osservato**

Per iniziare una feature sono richiesti manualmente:

- scelta della task;
- creazione del branch;
- creazione di `specs/features/<branch>/`.

Su `main` il runner risponde `select_roadmap_task`, ma non implementa l'azione.

**Miglioria proposta**

```text
feature-flow start --task <id> --branch <name>
```

Il comando dovrebbe verificare working tree pulito, task valida e collisioni.

### WF-12 — Roadmap summary impreciso

**Priorità:** P2

**Stato:** Confermato

**Osservato**

Su `ga4_buddy` il runner ha riportato:

```text
open: 28
active: 0
```

La roadmap usa stati come:

```text
> **Stato**: `[~]` in corso
```

mentre il parser considera principalmente checkbox di lista. Conta inoltre checkbox
interne alle sezioni UI come task di roadmap.

**Miglioria proposta**

- Definire uno schema esplicito per ID, stato e branch della fase.
- Non derivare lo stato da tutte le checkbox Markdown.
- Validare che una sola task sia attiva.

### WF-13 — Chiusura incompleta

**Priorità:** P2

**Stato:** Confermato

**Osservato**

Dopo `validation_accepted`, `status` restituisce:

```text
next.action: stop
next.reason: launcher slice not implemented
```

**Miglioria proposta**

Restituire uno stato terminale esplicito, ad esempio:

```text
state: completed
next.action: none
```

ed eventualmente suggerire push/PR senza eseguirli automaticamente.

### WF-14 — Configurazione modelli fragile

**Priorità:** P1

**Stato:** Corretto

**Problemi già incontrati**

- prefisso accidentale che rendeva `workflow.json` non valido;
- identificatore con punto invece di trattino;
- trigger fallback duplicati;
- sandbox Codex mancante;
- test con nomi modello hardcoded.

**Valutazione corrente**

Prima dell'intervento la suite conteneva 23 test: 17 passavano e 6 fallivano perché i
routing attesi non coincidevano con `workflow.json`. Alcune transizioni di fallback
erano inoltre irraggiungibili dalla coppia autore/reviewer iniziale.

**Intervento completato**

- Aggiunto `workflow.schema.json`, versionato e incluso nel package.
- Aggiunto `feature-flow validate-config`, con supporto per file candidati esterni.
- Validati campi richiesti, provider, effort, sandbox Codex e chiavi sconosciute.
- Validati auto-review, trigger duplicati, route terminali e fallback irraggiungibili.
- Riallineata la matrice: docs Sol/Opus, code Terra/Sol, E2E Sol/Opus.
- Parametrizzati dalla configurazione i test di flusso; soltanto il test della policy
  mantiene intenzionalmente i nomi dei modelli.
- Portata la suite a 27 test passanti su 27.

La verifica opzionale dell'effettiva disponibilità remota dei modelli resta fuori
perimetro: richiede accesso ai provider ed è più coerente con il futuro comando
`feature-flow doctor` previsto da WF-08.

**Criterio di verifica**

Soddisfatto: la suite è verde e `validate-config` rifiuta una transizione
irraggiungibile, un trigger duplicato, una coppia che causerebbe auto-review e una route
Codex senza sandbox.

### WF-15 — Consenso artifact basato su testo libero

**Priorità:** P2

**Stato:** Confermato

**Osservato**

Il consenso richiede:

```text
--answer commit
```

oppure:

```text
--answer "leave uncommitted"
```

`resolve` accetta però testo libero e la validazione effettiva avviene soltanto nel
successivo `run`.

**Miglioria proposta**

- Validare immediatamente la risposta contro `choices`.
- Esporre comandi espliciti `commit-artifacts` e `skip-artifacts`, oppure flag tipizzati.

### WF-16 — Finalizzazione del processo da riprodurre

**Priorità:** P2

**Stato:** Da riprodurre

**Osservato**

In almeno un run lo stato e il ledger risultavano già aggiornati mentre la sessione
shell non aveva ancora restituito il JSON finale. Questo ha reso ambiguo se il lock
fosse ancora attivo e ha favorito l'avvio prematuro di un secondo `run`.

**Nota**

Potrebbe dipendere dal wrapper PTY usato per il test e non dal workflow. Va riprodotto
direttamente in terminale prima di classificarlo come bug del runner.

**Miglioria proposta**

- Test di integrazione sul rilascio del lock e sull'exit del processo.
- Timestamp di chiusura nello stato.
- Messaggio chiaro se un secondo run trova un processo ancora attivo.

### WF-17 — Side effect dell'autore senza handoff strutturato

**Priorità:** P0

**Stato:** Confermato

**Osservato**

Durante il gate reale `code`, Codex ha modificato file Python e TypeScript nel working
tree. Il diff era rilevabile e `status` calcolava un hash del codice, ma il processo
provider non aveva ancora prodotto il file di risposta strutturata. Lo stato restava:

```text
status: running
phase: author_needed
target_hash: null
```

Il processo è rimasto attivo senza completare l'handoff. Dopo l'interruzione, le
modifiche erano ancora presenti ma il runner non disponeva di sessione, test dichiarati
o checkpoint utilizzabile per passare alla review.

**Impatto**

Lo stato persistito non rappresenta gli effetti già applicati al repository. Un crash,
un timeout o un'interruzione lascia un artifact parziale che richiede valutazione
manuale e che può essere modificato nuovamente dal tentativo successivo.

**Miglioria proposta**

- Introdurre uno stato esplicito `author_in_progress` con PID, sessione e hash iniziale.
- Eseguire l'autore in un worktree o staging isolato e applicare il diff soltanto dopo
  una risposta strutturata valida.
- In alternativa, salvare un checkpoint del diff e proporre `resume`, `review` o
  `discard` senza scegliere automaticamente.
- Verificare atomicamente risposta, lista test e diff prima della transizione a
  `review_needed`.
- Non perdere l'identificatore di sessione se il provider lo ha già emesso.

**Criterio di verifica**

Simulare un autore che modifica un file e poi resta bloccato prima della risposta:
il repository principale non deve contenere side effect non tracciati, oppure il
workflow deve rilevarli e offrire una recovery deterministica senza rilanciare
automaticamente l'autore.

### WF-18 — `status` può rallentare durante un run

**Priorità:** P2

**Stato:** Da riprodurre

**Osservato**

Durante il gate `code`, una chiamata a `feature-flow status` ha impiegato circa
142 secondi prima di restituire lo stato. Altri polling durante lo stesso run erano
invece immediati.

**Impatto**

Il comando pensato per osservare un processo lungo può risultare indisponibile proprio
quando serve diagnosticare un provider bloccato. Script di monitoraggio e interfacce
utente non possono distinguere un rallentamento Git o filesystem, il wrapper di
esecuzione, un provider lento o un processo terminato.

**Valutazione corrente**

`status` legge già `state.json` senza acquisire il workflow lock e lo stato viene
scritto tramite replace atomico. L'ipotesi che i 142 secondi dipendano dal lock del
runner non è quindi supportata dal codice corrente.

**Verifica proposta**

- Riprodurre il caso direttamente in terminale, senza wrapper PTY.
- Misurare separatamente lettura dello stato e comandi Git eseguiti da `status`.
- Aggiungere un test concorrente `run` + `status` con limite temporale.
- Implementare timeout o diagnostica aggiuntiva soltanto dopo aver identificato
  l'operazione bloccante.

### WF-19 — Diagnostica provider disponibile solo a fine chiamata

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Nel run `code` rimasto appeso, la directory del run conteneva soltanto
`author-schema.json`. I file `raw-*-codex.stdout`, `raw-*-codex.stderr`,
`raw-*-codex.response` e il risultato `01-author.json` non erano disponibili finché
`subprocess.run()` non terminava.

**Impatto**

Non è possibile capire se il provider sta ragionando, eseguendo test, aspettando rete
o bloccandosi dopo aver modificato il repository. L'interruzione perde inoltre
informazioni utili alla recovery.

**Miglioria proposta**

- Scrivere stdout e stderr progressivamente su file.
- Persistire comando sanitizzato, PID, timestamp di avvio e heartbeat.
- Conservare i log parziali anche su timeout o `KeyboardInterrupt`.
- Esporre i percorsi dei log tramite `feature-flow status`.

### WF-20 — Rilancio non idempotente dell'autore

**Priorità:** P0

**Stato:** Confermato

**Osservato**

Dopo l'interruzione del primo autore `code`, il working tree conteneva già un diff con
hash:

```text
sha256:8a8ad76c17914357bbcf59e31a55fbe58f411d82f306ee9de0c8fcf0debfdd4d
```

Il successivo `feature-flow run`, trovando ancora `phase: author_needed`, ha invocato
nuovamente l'autore sullo stesso working tree. Durante il secondo tentativo l'hash è
diventato:

```text
sha256:927d2b6c38df388ff73eec3d630abb4d0883b969d57cb89e2eb75b507393894b
```

**Impatto**

Un retry può sovrascrivere, ampliare o rendere incoerenti modifiche già prodotte senza
che il primo tentativo sia stato registrato. Non è possibile attribuire con certezza
le singole variazioni a una sessione.

**Miglioria proposta**

- Prima di rilanciare `author_needed`, confrontare il working tree con l'hash iniziale.
- Se esistono modifiche non checkpointate, fermarsi con un finding tipizzato
  `orphan_author_changes`.
- Offrire scelte validate: riprendere la sessione, accettare il diff per review,
  ripartire in worktree isolato o fermarsi.
- Non modificare o scartare il diff esistente senza consenso esplicito.

**Criterio di verifica**

Interrompere un autore dopo il primo side effect e rilanciare `run`: non deve partire
una seconda sessione finché il diff orfano non è stato risolto esplicitamente.

### WF-21 — Esito reviewer non sempre persistito nello stato

**Priorità:** P0

**Stato:** Parzialmente corretto

**Osservato**

La review `validation.md` con `claude-opus-5` ha prodotto una risposta strutturata
valida (`status: needs_user`, finding `V1` e `V2`) nel file del run, ma una lettura
successiva di `state.json` riportava ancora:

```text
status: running
phase: review_needed
review_round: 0
```

Il risultato era quindi disponibile nei log ma non nello snapshot usato da `status` e
dal run successivo.

**Valutazione corrente**

Il percorso nominale ora imposta `needs_user` e salva immediatamente `state.json`.
Resta però una finestra tra la scrittura del ledger e quella dello stato: le due
operazioni non costituiscono una transazione atomica e il caso osservato non è protetto
da un test di crash o interruzione.

**Impatto**

Il workflow può ripetere una review già conclusa o perdere il punto di ripresa. La
diagnostica e l'operatività divergono dalla risposta effettiva del modello.

**Miglioria proposta**

- Scrivere `state.json` e ledger in una transazione logica prima di chiudere il run.
- Verificare che la risposta persistita abbia lo stesso `run_id`, hash e sessione del
  file di output.
- Esporre un errore di consistenza se il provider output esiste ma lo stato non è
  aggiornato.

**Criterio di verifica**

Simulare un reviewer `needs_user` e un'interruzione tra output provider, ledger e stato:
il run successivo deve recuperare lo stesso `run_id`, hash, sessione e finding senza
ripetere la review.

### WF-22 — Finding di validation assegnati all'utente senza riapertura del gate code

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Opus ha rilevato un errore lint nel codice già accettato (`credentialUploadReducer`)
e lo ha assegnato all'utente come `V1`. La validation non può modificare il codice,
ma il workflow non offre una transizione tipizzata da `validation needs_user` a
`code author_needed`.

**Impatto**

Un gate precedente può essere tecnicamente accettato mentre la validazione scopre un
regression gate rosso, senza un percorso chiaro per correggerlo e rieseguire la review
del codice.

**Miglioria proposta**

- Aggiungere `feature-flow reopen code --finding V1` con nuovo target hash.
- Conservare il legame tra finding validation, commit e nuovo round code.
- Impedire l'accettazione della validation finché il codice riaperto non è nuovamente
  accettato.

### WF-23 — Nuovo processo richiesto tra gate accettati

**Priorità:** P2

**Stato:** Decisione prodotto

**Osservato**

Il ciclo interno prosegue automaticamente da review `needs_changes` ad author, ma su
`accepted` chiude il processo del gate. Dopo l'accettazione del codice è stato quindi
necessario un nuovo `feature-flow run` per avviare `validation.md`.

Questo comportamento coincide con il contratto descritto nel README, secondo cui ogni
esecuzione completa un solo gate. Non è quindi un difetto di correttezza finché tale
contratto resta intenzionale.

**Impatto**

L'operatore può interpretare la chiusura del processo come completamento del workflow,
anche se il gate successivo è già determinabile.

**Miglioria proposta**

- Decidere esplicitamente se il contratto deve restare “un gate per run”.
- Aggiungere una modalità `--continue` o rendere il comportamento predefinito la
  prosecuzione fino al prossimo gate interattivo.
- Fermarsi soltanto su `needs_user`, errore provider o consenso artifact.
- Mostrare chiaramente il prossimo gate se il processo deve comunque terminare.

### WF-24 — Recovery non portabile tra macchine

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Lo stato operativo e i log sono salvati sotto `~/.local/state/feature-flow/<repo-hash>`
e non nel repository. Per riprendere su un'altra macchina sono disponibili branch e
artifact, ma non necessariamente `state.json`, sessioni provider, finding e run log.

**Impatto**

Una feature con verifiche pending può essere ripresa soltanto ricostruendo manualmente
il contesto o trasferendo directory di stato non versionate.

**Miglioria proposta**

- Salvare nel repository un checkpoint portabile e sanitizzato sotto
  `specs/features/<feature>/.feature-flow/`.
- Separare dati non condivisibili (token, raw log) dal manifest di ripresa.
- Aggiungere `feature-flow export-state` e `feature-flow import-state`.

### WF-25 — Commit artifact pending non supportato dal comando

**Priorità:** P1

**Stato:** Confermato

**Osservato**

Il consenso `commit_artifacts` è raggiungibile soltanto dopo `validation_accepted`.
Quando la validation è `needs_user` per verifiche manuali, è stato necessario creare
manualmente il commit degli artifact, pur volendo lasciare esplicitamente pending i
criteri di accettazione.

**Impatto**

L'operatore deve scegliere tra non versionare gli artifact o bypassare il percorso
supportato dal workflow. Questo rende il passaggio a un'altra macchina fragile e poco
auditabile.

**Miglioria proposta**

- Aggiungere `feature-flow commit-artifacts --pending --repo ...`.
- Registrare nel ledger che il commit contiene artifact non accettati e i finding
  aperti.
- Mantenere il blocco di completamento e impedire che il commit pending venga scambiato
  per `validation_accepted`.

## Correzioni consolidate e baseline

### Packaging e installazione

- Package Python installabile con `uv tool install`.
- Entry point `feature-flow`.
- Prompt, template e `workflow.json` inclusi nella wheel.
- Installazione e import delle risorse verificati fuori dal repository sorgente.

### Routing modelli

- Rimosso Fable 5 dalla matrice.
- Identificatori Claude corretti e poi aggiornati a `claude-opus-5`.
- Trigger fallback duplicati eliminati.
- Sandbox Codex autore aggiunta.

Il riallineamento tra test e matrice è stato completato da WF-14. La configurazione
dispone ora di schema versionato, validazione strutturale e controllo del grafo dei
fallback.

### Validazione del piano

- Corretto il falso positivo di `has_real_plan()`: il controllo cercava la sottostringa
  `TODO` e considerava non valido anche testo legittimo come `metodo`.
- Il marker viene ora riconosciuto soltanto come parola autonoma tramite
  `\bTODO\b`.

### Commit degli artifact con consenso

- Dopo la validation il workflow entra in `needs_user`.
- `commit` crea un commit separato con requirements, plan, validation e ledger.
- `leave uncommitted` chiude senza committarli.
- Il consenso non invalida più l'hash accettato della validation.

### Copertura corrente

- Baseline storica: 21 test del package passavano prima dell'avvio del primo use case
  reale.
- Smoke test completo su clone di `ga4_buddy` concluso con working tree pulito.
- Il primo reviewer reale `claude-opus-5` ha prodotto finding strutturati e una
  successiva review ha accettato i requirements corretti.
- Baseline corrente: 27 test eseguiti e 27 passati dopo la correzione di WF-14.

## Ordine di intervento consigliato

1. **Baseline verificabile — WF-16, WF-18, WF-21.**
   WF-14 ha ripristinato la baseline verde; aggiungere ora riproduzioni deterministiche
   per i comportamenti dubbi prima di modificare l'architettura.
2. **Integrità di repository, stato e audit — WF-17, WF-20, WF-02, WF-03, WF-09,
   WF-21.** Isolare o checkpointare gli effetti dell'autore, garantire idempotenza,
   legare la provenienza all'hash e rendere coerenti ledger, stato e commit.
3. **Controllo dei provider — WF-04, WF-07, WF-01, WF-08, WF-19, WF-05.**
   Introdurre prima timeout e classificazione degli errori, poi retry/recovery e infine
   progress e log incrementali.
4. **Validazione deterministica e costo — WF-10, WF-22, WF-06.**
   Eseguire i comandi consumer tramite il runner, supportare la riapertura del codice e
   applicare budget al context pack.
5. **Artifact e portabilità — WF-15, WF-25, WF-24.**
   Tipizzare le decisioni, supportare commit pending auditabili ed esportare un
   checkpoint portabile e sanitizzato.
6. **Lifecycle e UX — WF-13, WF-23, WF-11, WF-12.**
   Definire lo stato terminale e il contratto “un gate per run”, poi completare bootstrap
   e parser della roadmap.
