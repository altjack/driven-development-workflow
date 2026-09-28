# Routing adattivo di modello ed effort tramite classificatore

Stato: **proposta** (nessuna modifica al runner).
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

Il classificatore **non sceglie mai direttamente un nome di modello**: produce
un'etichetta e il runner la traduce in route tramite una tabella validata. In questo
modo restano valide tutte le garanzie di `validate-config` (indipendenza
autore/reviewer, raggiungibilità dei fallback, sandbox Codex).

## Perché Jev

[Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) (TypeSafe AI,
settembre 2026) è un modello "System One": riceve uno stato in linguaggio naturale e
domande tipizzate, e restituisce **decisioni tipizzate con probabilità calibrate**
invece di testo. Le primitive sono tre:

- **Choice**: una distribuzione di probabilità su un elenco chiuso di opzioni;
- **Score**: un valore su una scala;
- **yes/no**: la probabilità che un'affermazione sia vera.

Caratteristiche rilevanti per questo caso d'uso, secondo le fonti pubbliche:

| Caratteristica | Conseguenza per il routing |
|---|---|
| Output conforme allo schema, niente testo libero | nessun parsing, impossibile ottenere un tier o un modello fuori elenco |
| Probabilità calibrate su tutte le opzioni | la soglia di confidenza ha un significato reale e si può ragionare sul rischio, non solo sull'argmax |
| Latenza nell'ordine dei ms–centinaia di ms, input ~0,042 USD/M token, output gratuito | il costo della classificazione è trascurabile rispetto a una review Opus (~1,36 USD, WF-06) |
| Nessuna spiegazione testuale | l'audit si basa su segnali deterministici + probabilità registrate, non su una rationale |
| Debole sul ragionamento procedurale | conteggi e regole (righe del diff, percorsi sensibili) vanno calcolati dal runner, non chiesti al modello |
| Sensibile all'ordine delle opzioni | l'ordine delle opzioni va fissato in configurazione (vedi Rischi) |
| Calibrazione valida in aggregato, non sul singolo caso né sotto domain shift | serve la fase `shadow` sui propri repository prima di fidarsi |

Jev è quindi adatto al ruolo di classificatore; resta sostituibile da un LLM piccolo o da
un classificatore addestrato sul ledger (Fase 3), purché rispetti lo stesso contratto.

## Contratto del classificatore

Input (costruito deterministicamente dal runner, piccolo per costruzione):

| Gate | Segnali disponibili |
|---|---|
| `requirements.md` | testo della task di roadmap, `mission.md`/`tech-stack.md` riassunti |
| `plan.md` | `requirements.md` accettato: numero di criteri di accettazione, domini citati |
| `code` | `plan.md` accettato: file/moduli toccati, linguaggi, migrazioni, dipendenze nuove |
| `validation.md` | diff accettato: righe cambiate, file, test presenti, flag del gate `code` |

Più segnali calcolati senza modello: dimensione del diff, numero di file, presenza di
percorsi sensibili (`auth`, `migrations/`, `security`, `payment`, config CI), round di
review consumati nei gate precedenti della stessa feature.

Domande poste a Jev (una sola chiamata):

| Domanda | Tipo | Opzioni |
|---|---|---|
| Difficoltà della task | Choice | `S`, `M`, `L`, `XL` (ordine fisso) |
| "Il cambiamento tocca autenticazione, permessi o segreti?" | yes/no | — |
| "Il cambiamento richiede una migrazione di dati o schema?" | yes/no | — |
| "Il cambiamento modifica un'API pubblica o un contratto esterno?" | yes/no | — |
| Round di review attesi | Score | 1–3 (`limits.max_reviews`) |

Il runner normalizza la risposta in:

```json
{
  "tier_probs": {"S": 0.05, "M": 0.55, "L": 0.30, "XL": 0.10},
  "tier": "L",
  "risk_flags": ["public_api"],
  "risk_probs": {"security": 0.08, "data_migration": 0.02, "public_api": 0.71},
  "expected_rounds": 1.6,
  "confidence": 0.62
}
```

Non c'è campo `rationale`: Jev non la produce. La spiegazione nel ledger è data dai
segnali deterministici in input e dalle probabilità.

Punto chiave: **il classificatore gira dopo che il gate precedente è stato accettato**.
La difficoltà del codice si stima molto meglio da un `plan.md` approvato che dalla
riga di roadmap; per questo la classe viene ricalcolata per gate e non una volta per
feature.

## Dalla classe alla route

### Fase 1 — solo effort (consigliata come primo passo)

Il modello resta quello della matrice; la classe modifica soltanto l'effort. Il grafo
dei fallback non cambia, quindi la validazione esistente resta sufficiente.

```json
"routing": {
  "mode": "shadow",
  "classifier": {"provider": "typesafe", "model": "jev"},
  "min_confidence": 0.7,
  "underestimate_budget": 0.25,
  "risk_threshold": 0.3,
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

1. **Scelta del tier sulla distribuzione, non sull'argmax.** Si sceglie il tier più
   basso `t` tale che `P(tier > t) ≤ underestimate_budget`. Nell'esempio sopra
   l'argmax è `M` (0,55), ma `P(L)+P(XL) = 0,40 > 0,25`, quindi si sceglie `L`.
   Sottostimare costa più che sovrastimare e la calibrazione di Jev permette di
   dichiararlo esplicitamente.
2. `confidence < min_confidence` → si usa la route statica attuale (comportamento di oggi).
3. Un flag di rischio si attiva se la sua probabilità supera `risk_threshold` (bassa di
   proposito) oppure se un segnale deterministico lo impone (per esempio un percorso
   `migrations/` nel diff). Qualsiasi flag → effort del reviewer almeno
   `reviewer_floor_on_risk`. Si può risparmiare sull'autore, mai sulla review di
   codice rischioso.
4. Escalation monotona: se un round termina in `needs_changes`, il round successivo
   sale di un livello di effort. Non si scende mai all'interno dello stesso gate.
5. Il classificatore che fallisce non blocca il workflow: si ricade sulla route statica
   e l'evento viene registrato (niente consumo della catena di fallback, cfr. WF-07).

### Fase 2 — anche il modello

Solo dopo aver misurato la Fase 1. Ogni tier diventa una matrice completa
(`author`, `reviewer`, `fallbacks`) e `validate-config` va esteso per validare
ciascuna matrice come oggi valida `docs`/`code`/`e2e`. Il costo di validazione cresce
col numero di tier: tenerli a 3–4.

### Fase 3 — verifica della calibrazione sullo storico

Il ledger contiene già, per ogni gate, round di review, modelli, esiti e (se
aggiunto) costo/token. Con qualche decina di feature si può:

- misurare la calibrazione di Jev sui propri repository (Brier score, reliability
  diagram di `P(tier ≥ L)` contro "gate con ≥ 2 round");
- confrontarlo con una baseline tabellare (regressione logistica sui soli segnali
  deterministici): se la baseline è equivalente, è più riproducibile e non invia dati
  a terzi;
- ricalibrare le soglie (`underestimate_budget`, `risk_threshold`) sui dati reali.

## Integrazione nel runner

- `run_artifact()`, alla creazione dello stato: dopo `workflow = workflow_config(...)`
  chiamare `classify(root, unit, state)` e derivare `author_route`/`reviewer_route`.
- Salvare nello stato e nel ledger `classification: {tier, risk_flags, confidence,
  classifier_model, input_hash}`, legata all'hash del target (stesso principio di
  WF-02: la provenienza va legata all'hash, non allo stato corrente).
- La classificazione è **congelata per gate**: un `run` ripetuto non la ricalcola se
  l'`input_hash` non è cambiato, così routing e audit restano deterministici.
- `feature-flow status` espone tier e route effettive.
- `--fake` usa un classificatore deterministico per i test.
- Jev non è un provider autore/reviewer: il classificatore ha una configurazione
  separata (`routing.classifier`) e non entra nell'enum `provider` delle route, quindi
  `validate-config` sulle matrici non cambia. La chiave API arriva da variabile
  d'ambiente, mai da `workflow.json`.

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

- **Sottostima silenziosa**: mitigata da floor sul reviewer, escalation monotona e
  soglia di confidenza.
- **Costo del classificatore**: trascurabile con Jev; va comunque alimentato con input
  riassunti, non con il context pack completo, per latenza e privacy.
- **Non determinismo e sensibilità all'ordine delle opzioni**: l'ordine dei tier è
  fissato in configurazione e fa parte dell'`input_hash`; la classificazione è
  congelata per hash. In alternativa, visto il costo trascurabile, si può interrogare
  Jev con l'ordine diretto e inverso e mediare le distribuzioni.
- **Domain shift**: la calibrazione pubblicata vale su popolazioni generali; per questo
  la modalità `shadow` è obbligatoria prima di `enforced`.
- **Dati inviati a terzi**: il classificatore riceve estratti di artifact del progetto
  consumer. Su repository aziendali va verificato che l'invio a TypeSafe sia
  ammesso; in caso contrario si usa la baseline tabellare locale della Fase 3.
- **Prompt injection dal repository**: il classificatore legge artifact del progetto
  consumer; il suo output è vincolato a uno schema chiuso e può solo scegliere tra route
  già validate, quindi non può introdurre modelli o sandbox non previsti.

## Uso collaterale: classificazione degli errori provider (WF-07)

Lo stesso meccanismo risolve WF-07 quasi a costo zero: una domanda Choice su
stderr/exit code del provider (`rete`, `autenticazione`, `modello non disponibile`,
`rate limit`, `output invalido`, `errore interno`) decide se applicare un fallback di
modello o un retry. Le regex deterministiche restano prioritarie (`ENOTFOUND` è rete
senza bisogno di un modello); Jev copre i casi non riconosciuti.

## Prerequisiti consigliati

Prima di implementare conviene chiudere WF-02 (provenienza per hash), WF-07
(classificazione degli errori provider) e aggiungere la registrazione di token/costo
per run: senza queste basi non è possibile né attribuire correttamente le route
adattive né misurarne il beneficio.
