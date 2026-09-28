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

> Nota sul nome: "jev" non corrisponde a un modello che conosco. Nel documento il
> classificatore è trattato come componente astratto: può essere un LLM piccolo
> (es. `claude-haiku-4-5`, effort `low`), un modello locale o, a regime, un
> classificatore ML classico addestrato sullo storico del ledger (vedi Fase 3).

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

Output strutturato (JSON schema, come le risposte autore/reviewer):

```json
{
  "tier": "S | M | L | XL",
  "risk_flags": ["security", "data_migration", "public_api", "concurrency"],
  "confidence": 0.0,
  "rationale": "max 2 frasi"
}
```

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
  "classifier": {"provider": "claude", "model": "claude-haiku-4-5", "effort": "low"},
  "min_confidence": 0.7,
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

1. `confidence < min_confidence` → si usa la route statica attuale (comportamento di oggi).
2. Qualsiasi `risk_flag` → effort del reviewer almeno `reviewer_floor_on_risk`.
   Si può risparmiare sull'autore, mai sulla review di codice rischioso.
3. Escalation monotona: se un round termina in `needs_changes`, il round successivo
   sale di un livello di effort. Non si scende mai all'interno dello stesso gate.
4. Il classificatore che fallisce non blocca il workflow: si ricade sulla route statica
   e l'evento viene registrato (niente consumo della catena di fallback, cfr. WF-07).

### Fase 2 — anche il modello

Solo dopo aver misurato la Fase 1. Ogni tier diventa una matrice completa
(`author`, `reviewer`, `fallbacks`) e `validate-config` va esteso per validare
ciascuna matrice come oggi valida `docs`/`code`/`e2e`. Il costo di validazione cresce
col numero di tier: tenerli a 3–4.

### Fase 3 — classificatore addestrato sullo storico

Il ledger contiene già, per ogni gate, round di review, modelli, esiti e (se
aggiunto) costo/token. Con qualche decina di feature si può sostituire o affiancare
l'LLM con un modello tabellare (es. regressione logistica o gradient boosting sui
segnali deterministici sopra) che predice "numero di round attesi" o "probabilità di
`needs_changes`". È più economico, spiegabile e riproducibile di un LLM.

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
- **Costo del classificatore**: deve essere trascurabile rispetto al risparmio; va
  alimentato con input riassunti, non con il context pack completo.
- **Non determinismo**: mitigato congelando la classificazione per `input_hash`.
- **Prompt injection dal repository**: il classificatore legge artifact del progetto
  consumer; il suo output è vincolato a uno schema chiuso e può solo scegliere tra route
  già validate, quindi non può introdurre modelli o sandbox non previsti.

## Prerequisiti consigliati

Prima di implementare conviene chiudere WF-02 (provenienza per hash), WF-07
(classificazione degli errori provider) e aggiungere la registrazione di token/costo
per run: senza queste basi non è possibile né attribuire correttamente le route
adattive né misurarne il beneficio.
