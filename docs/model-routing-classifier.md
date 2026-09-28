# Routing adattivo di modello ed effort tramite classificatore

Stato: **Fase 1 in modalità `shadow` implementata** (`feature_flow/routing.py`);
`advisory`, `enforced` e Fase 2 restano proposte.
Data: 2026-09-28.

## Problema

Oggi il routing è statico: `workflow.json` associa a ogni workflow (`docs`, `code`,
`e2e`) una coppia autore/reviewer con modello ed effort fissi. `run_artifact()` copia
quelle route in `state["author_route"]` / `state["reviewer_route"]` alla creazione
dello stato e non le cambia più, salvo fallback.

Conseguenze osservate (vedi WF-06 in `workflow_problem_to_fix.md`):

- una feature banale (rinominare un campo, aggiungere un endpoint CRUD) paga lo stesso
  `xhigh` di autore e la stessa review Opus di una migrazione di autenticazione;
- non esiste il caso opposto: una task rischiosa non può ricevere più effort di quanto
  la matrice preveda;
- il costo per gate è quindi scollegato dalla difficoltà reale della task.

## Idea

Introdurre un passo di **classificazione** economico, eseguito prima di ogni gate,
che assegna alla task una *classe di difficoltà* e dei *flag di rischio*. La classe
seleziona una variante della route già dichiarata in configurazione.

Il classificatore **non sceglie mai direttamente un nome di modello**: risponde a
domande tipizzate e il runner traduce le risposte in route tramite regole e tabelle
validate. In questo modo restano valide tutte le garanzie di `validate-config`
(indipendenza autore/reviewer, raggiungibilità dei fallback, sandbox Codex).

## Perché Jev

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) (TypeSafe AI,
settembre 2026) è il primo modello "System One": *unstructured state in, typed
probabilistic decisions out*. Non genera testo: riceve uno stato e un insieme di
domande tipizzate e restituisce, in un unico passaggio parallelo, risposte con
probabilità calibrate. È addestrato con *Reinforcement Learning for Calibrated
Decisions* (RLCD), cioè ottimizzato per probabilità epistemicamente oneste più che per
la preferenza umana.

Il post di lancio indica tra i casi d'uso principali gli *AI-powered workflows /
smart if-statements*: "classify, route, score, extract, or branch where hand-written
logic is too brittle". Il routing di modello ed effort è esattamente questo caso.

Caratteristiche rilevanti e loro conseguenze:

| Caratteristica | Conseguenza per il routing |
|---|---|
| Output type-safe: struttura e valori possibili definiti in anticipo | nessun parsing, impossibile ottenere un tier o un modello fuori elenco |
| Probabilità calibrate e confidenza per ogni risposta | le soglie hanno un significato reale; si ragiona sulla distribuzione, non sull'argmax |
| Risposte simili per input simili | routing più stabile di un LLM generativo (resta comunque il congelamento per hash) |
| 70–500 ms per chiamata, input 0,042 USD/M token, output gratuito | costo trascurabile rispetto a una review Opus (~1,36 USD, WF-06) |
| Pensato per "many independent, decomposed questions" combinate nel codice | domande piccole e indipendenti, livello calcolato dal runner (vedi sotto) |
| Nessuna spiegazione testuale | l'audit si basa su segnali deterministici + probabilità per domanda |
| Debole sul ragionamento procedurale (fonti terze) | conteggi e regole (righe del diff, percorsi sensibili) li calcola il runner |
| Sensibile all'ordine delle opzioni in `choice` (fonti terze) | per grandezze ordinali si usa `score`, dove l'ordine è parte della definizione |

Jev resta sostituibile da un LLM piccolo o da un classificatore addestrato sul ledger
(Fase 3), purché rispetti lo stesso contratto.

## API

Jev è raggiungibile tramite la Decisions API di OpenRouter (endpoint **alpha**):

```text
POST https://openrouter.ai/api/alpha/decisions
model: ~typesafe/jev-latest   (jev-1.13 al 2026-09-19)
```

La richiesta contiene uno `state` (stringa, oggetto o array) e un insieme di
`questions` con chiave. Ogni domanda ha `type`, `instructions` e `criteria`:

| Tipo | `criteria` | Risposta |
|---|---|---|
| `noul` (sì/no) | opzionale, definizione di `true` e `false` | una probabilità, **senza** `confidence` |
| `choice` | oggetto etichetta → descrizione (max 255 opzioni) | `choice`, `probabilities` per etichetta, `confidence` |
| `score` | array ordinato di livelli, indice 0 = più basso | `score` (media pesata), `probabilities` per livello, `confidence` |

Esempi di risposta per singola domanda:

```json
{"type": "noul", "noul": 0.98}
{"type": "score", "score": 1.05, "probabilities": {"0": 0, "1": 0.95, "2": 0.05}, "confidence": 0.92}
```

Da verificare sulla documentazione ufficiale prima dell'implementazione: l'envelope
esatto della risposta, se e come fissare una versione specifica al posto di
`jev-latest`, limiti di input.

## Contratto del classificatore

### Input

Costruito deterministicamente dal runner, **corto e denso** (il post stesso nota che
input brevi favoriscono il modello; per noi conta anche la privacy):

| Gate | Fonte principale dello stato |
|---|---|
| `requirements.md` | testo della task di roadmap, `mission.md`/`tech-stack.md` riassunti |
| `plan.md` | `requirements.md` accettato: criteri di accettazione, domini citati |
| `code` | `plan.md` accettato: file/moduli toccati, linguaggi, migrazioni, dipendenze nuove |
| `validation.md` | diff accettato: righe cambiate, file, test presenti, flag del gate `code` |

Più segnali calcolati senza modello e passati come campi dello stato: dimensione del
diff, numero di file, percorsi sensibili (`auth`, `migrations/`, `security`,
`payment`, config CI), round di review consumati nei gate precedenti della feature.

Punto chiave: **il classificatore gira dopo che il gate precedente è stato accettato**.
La difficoltà del codice si stima molto meglio da un `plan.md` approvato che dalla
riga di roadmap; per questo la classificazione viene ricalcolata per gate.

### Domande (una sola chiamata)

Invece di chiedere "quanto è difficile?", si pongono domande piccole e indipendenti:

| Chiave | Tipo | Criteria |
|---|---|---|
| `scope` | `score` | `["una funzione o un file", "pochi file in un modulo", "più moduli", "trasversale all'architettura"]` |
| `ambiguity` | `score` | `["requisiti completi e verificabili", "alcuni punti da interpretare", "decisioni di design aperte"]` |
| `novel_pattern` | `noul` | richiede pattern, librerie o infrastruttura nuovi per il repository? |
| `tests_exist` | `noul` | l'area toccata è già coperta da test automatici? |
| `risk_security` | `noul` | tocca autenticazione, permessi o segreti? |
| `risk_data_migration` | `noul` | richiede una migrazione di dati o schema? |
| `risk_public_api` | `noul` | modifica un'API pubblica o un contratto esterno? |
| `review_rounds` | `score` | `["accettato al primo round", "serve un secondo round", "servono tre round"]` |

Esempio di richiesta per il gate `code`:

```json
{
  "model": "~typesafe/jev-latest",
  "state": {
    "gate": "code",
    "task": "Upload del token credenziale GA4 dalla UI",
    "plan_summary": "Nuovo endpoint POST /credentials, reducer frontend, cifratura a riposo",
    "signals": {"planned_files": 7, "languages": ["python", "typescript"],
                "sensitive_paths": ["backend/auth/"], "previous_gate_rounds": 2}
  },
  "questions": {
    "scope": {
      "type": "score",
      "instructions": "Ampiezza del cambiamento pianificato nel repository",
      "criteria": ["una funzione o un file", "pochi file in un modulo",
                   "più moduli", "trasversale all'architettura"]
    },
    "risk_security": {
      "type": "noul",
      "instructions": "Il cambiamento tocca autenticazione, permessi o segreti?"
    }
  }
}
```

(Le altre domande seguono lo stesso schema.)

### Dalla risposta al livello

Il livello `S/M/L/XL` **non è chiesto al modello**: lo calcola il runner con una
formula deterministica e versionata in configurazione.

1. La distribuzione di base del livello è quella di `scope` (livello 0 → `S`, …,
   3 → `XL`).
2. La distribuzione viene spostata di un livello verso l'alto (con tetto a `XL`) se
   vale almeno una di: `P(novel_pattern) > 0,5`, `P(ambiguity = 2) > 0,5`,
   `P(tests_exist) < 0,5`.
3. Se `P(review_rounds ≥ 1) > rounds_threshold` il livello minimo è `L`.

Il runner registra:

```json
{
  "tier_probs": {"S": 0.05, "M": 0.55, "L": 0.30, "XL": 0.10},
  "tier": "L",
  "risk_flags": ["security"],
  "answers": {"scope": {...}, "risk_security": {"noul": 0.83}, "...": "..."},
  "confidence": 0.62,
  "classifier_model": "~typesafe/jev-latest",
  "formula_version": 1,
  "input_hash": "sha256:..."
}
```

Non c'è campo `rationale`: Jev non la produce. La spiegazione nel ledger sono le
singole risposte, più leggibili di un voto unico.

## Dal livello alla route

### Fase 1 — solo effort (consigliata come primo passo)

Il modello resta quello della matrice; il livello modifica soltanto l'effort. Il grafo
dei fallback non cambia, quindi la validazione esistente resta sufficiente.

```json
"routing": {
  "mode": "shadow",
  "classifier": {"provider": "openrouter", "model": "~typesafe/jev-latest", "timeout_s": 5},
  "formula_version": 1,
  "min_confidence": 0.7,
  "underestimate_budget": 0.25,
  "risk_threshold": 0.3,
  "rounds_threshold": 0.5,
  "effort_by_tier": {
    "code": {
      "S":  {"author": "medium", "reviewer": "high"},
      "M":  {"author": "high",   "reviewer": "high"},
      "L":  {"author": "xhigh",  "reviewer": "high"},
      "XL": {"author": "xhigh",  "reviewer": "xhigh"}
    }
  },
  "reviewer_floor_on_risk": "xhigh"
}
```

Regole deterministiche applicate dal runner, non dal modello:

1. **Scelta sulla distribuzione, non sull'argmax.** Si sceglie il livello più basso
   `t` tale che `P(livello > t) ≤ underestimate_budget`. Nell'esempio l'argmax è `M`
   (0,55), ma `P(L)+P(XL) = 0,40 > 0,25`, quindi si sceglie `L`. Sottostimare costa
   più che sovrastimare e la calibrazione permette di dichiararlo esplicitamente.
2. `confidence` di `scope` sotto `min_confidence` → route statica attuale
   (comportamento di oggi).
3. Un flag di rischio si attiva se la sua probabilità supera `risk_threshold` (bassa di
   proposito, le domande `noul` non hanno confidenza) oppure se un segnale
   deterministico lo impone (per esempio un percorso `migrations/` nel diff).
   Qualsiasi flag → effort del reviewer almeno `reviewer_floor_on_risk`. Si può
   risparmiare sull'autore, mai sulla review di codice rischioso.
4. Escalation monotona: se un round termina in `needs_changes`, il round successivo
   sale di un livello di effort. Non si scende mai all'interno dello stesso gate.
5. Errore, timeout o risposta non conforme del classificatore → route statica, evento
   registrato nel ledger, nessun consumo della catena di fallback (cfr. WF-07).

### Fase 2 — anche il modello

Solo dopo aver misurato la Fase 1. Ogni livello diventa una matrice completa
(`author`, `reviewer`, `fallbacks`) e `validate-config` va esteso per validare
ciascuna matrice come oggi valida `docs`/`code`/`e2e`. Il costo di validazione cresce
col numero di livelli: tenerli a 3–4.

### Fase 3 — verifica della calibrazione sullo storico

Le evaluation pubblicate da TypeSafe misurano l'accordo con modelli di riferimento
(media di GPT-6 Astra e Fable 5.1), non con esiti reali, e il post stesso ne dichiara
i possibili bias. La validazione che conta è quindi sui propri dati: il ledger contiene
già, per ogni gate, round di review, modelli ed esiti. Con qualche decina di feature si
può:

- misurare la calibrazione di `review_rounds` e dei flag di rischio contro gli esiti
  reali (Brier score, reliability diagram);
- confrontare Jev con una baseline tabellare locale (regressione logistica sui soli
  segnali deterministici): se è equivalente, è più riproducibile e non invia dati a
  terzi;
- ricalibrare soglie e formula (`formula_version`) sui dati reali.

## Integrazione nel runner

- `run_artifact()`, alla creazione dello stato: dopo `workflow = workflow_config(...)`
  chiamare `classify(root, unit, state)` e derivare `author_route`/`reviewer_route`.
- Salvare nello stato e nel ledger la classificazione completa, legata all'hash del
  target (stesso principio di WF-02: la provenienza va legata all'hash, non allo stato
  corrente).
- La classificazione è **congelata per gate**: un `run` ripetuto non la ricalcola se
  `input_hash` (stato inviato + domande + `formula_version`) non è cambiato.
- `feature-flow status` espone livello, flag e route effettive.
- `--fake` usa un classificatore deterministico per i test.
- Il classificatore non è un provider autore/reviewer: ha una configurazione separata
  (`routing.classifier`) e non entra nell'enum `provider` delle route, quindi
  `validate-config` sulle matrici non cambia.
- Chiamata HTTP con la libreria standard (`urllib`), coerente con l'assenza di
  dipendenze del runner; chiave da `FEATURE_FLOW_OPENROUTER_API_KEY`, mai da
  `workflow.json`. Si usa una variabile dedicata e non `OPENROUTER_API_KEY` perché
  quest'ultima è spesso già impostata per altri strumenti: l'invio di estratti del
  repository a terzi deve essere una scelta esplicita per questo runner.

### Stato dell'implementazione (Fase 1, `shadow`)

- `routing.mode` accetta `off` e `shadow`; `advisory`/`enforced` sono rifiutati da
  `validate-config` finché non implementati.
- La classificazione avviene alla creazione dello stato di ogni gate, dentro il lock.
- Senza chiave API il classificatore risulta `skipped`: nessuna chiamata di rete e nessun
  blocco nel ledger. Con chiave, esito `classified` o `error`: in entrambi i casi il
  blocco `## <unit> - classification` finisce nel ledger (senza campo `artifact:`, così
  non interferisce con `gate_accepted`).
- Richiesta, risposta raw e record sono salvati localmente in
  `<state_dir>/classifications/`; nel ledger (versionato) vanno solo probabilità,
  livello e suggerimenti, non gli estratti inviati.
- Le domande sono inviate in inglese; il significato è quello della tabella sopra.
- Il parser accetta le risposte al primo livello o sotto `answers`/`decisions`, finché
  l'envelope non è verificato sulla documentazione ufficiale.
- `--fake` e i test usano `fake_classifier`, con lo stesso envelope dell'API.

## Modalità di rilascio

1. **`shadow`**: il classificatore gira e la sua scelta viene solo registrata nel
   ledger; il routing resta statico. Serve a raccogliere dati senza rischio.
2. **`advisory`**: la route suggerita viene mostrata in `status`/output e l'utente la
   può accettare con un flag (`--adaptive-routing`).
3. **`enforced`**: il routing adattivo diventa default, con la route statica come rete
   di sicurezza per bassa confidenza ed errori.

## Come misurare se funziona

Per ogni gate confrontare, tra shadow e enforced:

- costo e latenza per gate (serve registrare token/costo nel ledger, utile anche a WF-06);
- numero di round di review fino ad `accepted`;
- tasso di `needs_user` e di finding in `validation.md` che riaprono il codice (WF-22);
- *sottostima*: gate classificati `S`/`M` che hanno richiesto ≥ 2 round. È l'errore
  costoso, e va tenuto sotto una soglia esplicita prima di passare a `enforced`.

## Rischi

- **Sottostima silenziosa**: mitigata da floor sul reviewer, escalation monotona,
  scelta sulla distribuzione e soglia di confidenza.
- **Disponibilità**: Jev è in early access e la Decisions API di OpenRouter è alpha.
  La route statica resta sempre il ripiego e il classificatore ha un timeout breve.
- **Prezzo**: il post ammette che non può dimostrare che il prezzo non sia
  sovvenzionato. Anche con un costo 100 volte superiore la classificazione resterebbe
  trascurabile rispetto a una review.
- **Deriva del modello**: `jev-latest` può cambiare versione e invalidare la
  calibrazione misurata. Registrare sempre il modello nel ledger e fissare una versione
  appena l'API lo consente.
- **Domain shift**: la calibrazione dichiarata vale in aggregato e sulle evaluation del
  fornitore, non necessariamente sui propri repository; per questo `shadow` è
  obbligatoria prima di `enforced`.
- **Dati inviati a terzi**: il classificatore riceve estratti di artifact del progetto
  consumer. Su repository aziendali va verificato che l'invio a OpenRouter/TypeSafe
  sia ammesso; in caso contrario si usa la baseline tabellare locale della Fase 3.
- **Prompt injection dal repository**: l'output è vincolato a domande e opzioni chiuse
  e può solo selezionare route già validate, quindi non può introdurre modelli o
  sandbox non previsti.

## Usi collaterali

### Classificazione degli errori provider (WF-07)

Una domanda `choice` su stderr/exit code del provider (`rete`, `autenticazione`,
`modello non disponibile`, `rate limit`, `output invalido`, `errore interno`) decide se
applicare un fallback di modello o un retry. Le regex deterministiche restano
prioritarie (`ENOTFOUND` è rete senza bisogno di un modello); Jev copre i casi non
riconosciuti.

### Pre-check tra autore e reviewer (idea futura)

Il post indica anche "score, judge, verify, guardrail" come caso d'uso. Un controllo
rapido come "il `plan.md` copre tutti i criteri di accettazione?" potrebbe alimentare
effort ed escalation del reviewer. **Non** deve mai sostituire il reviewer né
accettare un gate: romperebbe l'indipendenza autore/reviewer.

## Prerequisiti consigliati

Prima di implementare conviene chiudere WF-02 (provenienza per hash), WF-07
(classificazione degli errori provider) e aggiungere la registrazione di token/costo
per run: senza queste basi non è possibile né attribuire correttamente le route
adattive né misurarne il beneficio.

## Fonti

- [Introducing System One Models & Jev — TypeSafe AI](https://typesafe.ai/blog/introducing-system-one-models-and-jev) (fonte primaria)
- [Jev su OpenRouter — documentazione](https://openrouter.ai/docs/guides/community/jev)
- [souvikr/jev-test](https://github.com/souvikr/jev-test): formato di richiesta/risposta e benchmark indipendente (17 casi)
- [MindStudio — Jev tested](https://www.mindstudio.ai/blog/jev-system-one-model-classification) e [Kartik Pansuriya — Calibration beats accuracy](https://www.kartikpansuriya.com/blog/jev-system-one-model-calibrated-decisions): limiti osservati (ordine delle opzioni, ragionamento procedurale, calibrazione in aggregato)
