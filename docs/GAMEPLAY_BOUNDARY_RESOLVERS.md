# Boundary resolver pluggabili — exact, Claude, Codex

9 ottobre 2026. Selezione del confine editoriale fra i `CandidateBoundary` di un `BoundaryContext`, con resolver intercambiabili. Gli agent scelgono SOLO fra candidati esistenti, dal solo context testuale; mai dal video.

Invariati in questa iterazione:
- Gameplay Detector (49/49 hash del freeze videoprova identici);
- result policy;
- exporter.

Nessun PyTorch, nessun cut finale.

```text
BoundaryContext ──► create_resolver(mode, ResolverOptions)
                        ├─ exact   ExactBoundaryResolver     (locale, deterministico)
                        ├─ claude  ClaudeBoundaryResolver    (CLI `claude` locale)
                        ├─ codex   CodexBoundaryResolver     (CLI `codex` locale)
                        ├─ heuristic  riservato, non implementato
                        └─ local      riservato, non implementato
                              ▼
                       BoundaryDecision ──► futuro gameplay cut
```

File:
- `analysis/boundaries/resolvers.py`: protocollo, factory, parser stretto, `resolve_contexts`.
- `analysis/boundaries/agents.py`: prompt, runner CLI, cache, log.
- `analysis/boundaries/models.py`: `BoundaryDecision` esteso.
- `analysis/boundaries/__main__.py`: modalità `--contexts`/`--resolver`.
- `tests/test_boundary_agents.py`.

## 1. Interfaccia

```python
class BoundaryResolver(Protocol):
    name: str
    def resolve(self, context: BoundaryContext) -> BoundaryDecision: ...

create_resolver(mode: "exact" | "claude" | "codex", options: ResolverOptions) -> BoundaryResolver
resolve_contexts(contexts, resolver) -> list[BoundaryDecision]
```

`ResolverOptions`:

|Campo|Default|Significato|
|---|---|---|
|`timeout_s`|120|timeout dell'agente|
|`fallback`|`"exact"`|`None` = l'errore viene propagato; solo resolver locali|
|`model`, `effort`|—|passati alla CLI|
|`executable`|—|altrimenti `AUTO_CUTTER_CLAUDE_CLI` / `AUTO_CUTTER_CODEX_CLI`, poi `PATH`|
|`use_cache`, `cache_directory`|attiva|cache delle decisioni|
|`log_path`|—|log locale|

`heuristic` e `local` sono nomi riservati: oggi sollevano "not implemented". Per aggiungerli basta registrarli in `create_resolver`, senza toccare il resto della pipeline. La futura CLI principale ReCut può mappare `--boundary-mode {exact,claude,codex}` direttamente su `create_resolver`.

## 2. BoundaryDecision

`boundary_type`, `timestamp` (sempre quello del candidato scelto), `candidate_index`/`selected_candidate`, `confidence`, `resolver` (chi ha prodotto la decisione), `requested_resolver` (chi era stato richiesto), `fallback`, `detail` (motivo breve dell'agente), `metadata`.

`metadata` contiene:
- `prompt_version`, `context_id` (prefisso dell'hash del context), `model`, `effort`, `cache_hit`, `runtime_s`;
- `cost_usd` e `agent_model` (Claude);
- `error_code` e `error_detail` in caso di fallback.

## 3–4. Implementazioni e comandi esatti

Entrambe:
- girano in una **directory temporanea vuota**, creata e poi cancellata;
- ricevono il prompt da **stdin**;
- fanno **1 chiamata per context**, senza retry né voti;
- non hanno accesso alla repository.

**Claude** (`claude` 2.1.295):

```text
claude -p --output-format json --json-schema <RESPONSE_SCHEMA>
       --tools "" --strict-mcp-config --no-session-persistence
       --system-prompt <SYSTEM_PROMPT> [--model M] [--effort E]      < prompt
```

- `--tools ""` disattiva tutti i tool built-in.
- `--strict-mcp-config` senza config esclude i server MCP.
- La risposta si legge da `structured_output` dell'envelope JSON (fallback: `result`). `is_error` o un `subtype` diverso da success è un errore.

**Codex** (`codex-cli` 0.161.0):

```text
codex exec --sandbox read-only --skip-git-repo-check --ephemeral --ignore-rules --color never
           -C <dir vuota> --output-schema <schema.json> -o <answer.json>
           [-m M] [-c model_reasoning_effort=E] -                     < system prompt + prompt
```

- Il JSON finale si legge da `-o`.
- Codex non ha un flag per rimuovere lo shell tool. Mitigazioni: sandbox read-only, directory vuota, sessione effimera, nessuna regola utente, istruzione esplicita di non usare comandi.

## 5. Rilevamento CLI

In ordine: `ResolverOptions.executable`, poi variabile d'ambiente, poi `shutil.which("claude"/"codex")`. Nessun path hardcoded. Su Windows `codex` si risolve in `codex.CMD` (shim npm). Se non viene trovata, l'errore è `cli_missing` e scatta il fallback, senza avviare processi.

## 6–7. Prompt (`boundary_agent_prompt_v1`)

- **System prompt fisso:** scegliere un solo candidato, mai inventare timestamp, nessun tool, solo JSON.
- **Prompt utente:** testo compatto (7.7k–11k caratteri sui context reali), con tempi assoluti e offset dall'anchor del confine.
  - `BOUNDARY` (START/END, risultato, partita completa oppure "video ends while still being played");
  - `GOAL` diverso per START ed END, più regole comuni (taglio editoriale naturale, ASR imperfetto, se l'evidenza è insufficiente → anchor con bassa confidenza);
  - `TIMELINE`: partita precedente, VS, risultato, partita successiva, EOF, finestra;
  - stato delle modalità;
  - `TRANSCRIPT` della sola finestra (al massimo 60 segmenti da 400 caratteri);
  - `SILENCES`, `GAMEPLAY MARKERS`, i 5 cambi visivi più forti;
  - `CANDIDATES`: indice, tempo, sorgenti e 8 feature leggibili (silenzio prima/dopo, dentro il parlato, densità del parlato a 5 s, gap del transcript, confine di frase, cambio visivo). Non le 45 feature grezze.

Mai inclusi: path del video, id della partita, fingerprint, impostazioni, nomi di repository/file. Lo verifica un test.

START:
- non tagliare il finale del contenuto precedente;
- il parlato che introduce la partita può iniziare prima del VS;
- non lasciare mezza frase.

END:
- le reaction/i commenti sulla partita rimossa vanno rimossi;
- non lasciare frasi riferite a una partita mai vista;
- non rimuovere il contenuto della partita o dell'argomento successivo;
- preferire silenzi e confini di frase.

La versione del prompt è nel prompt stesso, in metadata, nella chiave di cache e nel log. Per cambiare il prompt serve una nuova versione, così il dataset distingue le versioni.

## 8. Output strutturato e validazione

Lo schema è stretto: `selected_candidate` intero, `confidence` numero, `reason` stringa, tutti required, `additionalProperties: false`. Viene passato a `--json-schema` (Claude) e a `--output-schema` (Codex).

`parse_agent_response(strict=True)` rifiuta:
- JSON malformato o non-oggetto;
- chiavi inattese (per esempio `timestamp`);
- indice inesistente, negativo o booleano;
- confidenza fuori da [0, 1] o booleana;
- `reason` non stringa.

L'agente non può introdurre un timestamp proprio.

## 9. Timeout

`timeout_s` è configurabile (CLI `--agent-timeout`, default 120 s). Allo scadere viene terminato **l'intero albero di processi** (`taskkill /T /F` su Windows, necessario per lo shim `.cmd` → node), poi scatta il fallback.

Verificato dal vivo: codex con timeout 2 s → `FALLBACK(timeout)` in 2.1 s, 0 processi codex/node residui.

## 10. Fallback

Eventi che lo attivano: `cli_missing`, `process_failed` (exit ≠ 0 o envelope di errore), `timeout`, `invalid_output` (JSON, schema, candidato o confidenza non validi).

Effetto: la decisione dell'`ExactBoundaryResolver` con `fallback=True`, `requested_resolver=<agent>`, `resolver="exact"` e `error_code`/`error_detail` in metadata. La pipeline non fallisce mai per un agente.

- `--agent-fallback none` / `fallback=None` fa propagare l'errore.
- Un fallback remoto è rifiutato: solo resolver locali.

## 11. Determinismo

Nessuna delle due CLI espone la temperatura. Il determinismo possibile è strutturale:
- prompt identico per lo stesso context;
- 1 chiamata per context, senza voti;
- output vincolato allo schema;
- cache per riusare la stessa decisione.

`--agent-effort` (Claude `--effort`, Codex `model_reasoning_effort`) è configurabile; di default si usa la configurazione della CLI dell'utente (Codex qui: `xhigh`).

## 12. Cache

Chiave = sha256 di (hash canonico dell'intero `BoundaryContext`, resolver, `prompt_version`, model, effort). Directory separate per provider (`cache_root()/boundaries/agent_decisions/{claude,codex}`): Claude e Codex non si mescolano.

Si salvano solo le risposte valide, mai i fallback. Su un cache hit la risposta viene **rivalidata** contro il context corrente e non viene avviato nessun processo (verificato dal vivo: 4/4 hit, 0.3 s). Si disattiva con `--no-agent-cache`.

## 13. Privacy

`--resolver/--boundary-mode claude|codex` **invia il testo del context** (transcript della finestra, tempi, feature dei candidati) al provider configurato nella CLI dell'utente. La CLI di debug lo dichiara a ogni esecuzione con un avviso. `exact`, e in futuro `heuristic`/`local`, non inviano nulla.

Minimizzazione:
- solo la finestra locale;
- nessun path, id o metadato del video;
- nessun frame.

Nessuna analytics. Il log di debug è locale e non contiene né il transcript né la `reason`; contiene: tempo, context id, partita, tipo, resolver richiesto/usato, candidato, timestamp, confidenza, fallback, codice errore, runtime, cache hit, versione prompt, modello, costo.

## 14. CLI di debug

```bash
# riusa context salvati: nessun detector, ASR o FFmpeg
python -m analysis.boundaries --contexts .../final_smoke_it/removable_contexts.json \
    --select 1:END --select 3:END --resolver exact --resolver claude --resolver codex \
    [--agent-timeout 300] [--agent-model M] [--agent-effort E] [--agent-fallback exact|none] \
    [--no-agent-cache] [--agent-log decisions.jsonl] [--show-prompt] [--decisions-output decisions.json]
```

`--contexts` accetta:
- l'output `--output` del comando di analisi;
- una singola `recut_game_boundary_analysis`;
- un singolo context;
- una lista di context.

## 15. Test (`tests/test_boundary_agents.py`, 27; subprocess sempre mockato)

- **Decisioni valide:** Claude e Codex validi → candidato selezionato, con verifica di flag, schema e prompt via stdin.
- **Directory di lavoro:** temporanea, vuota e rimossa dopo la chiamata.
- **Fallback a exact:** JSON invalido, candidato inesistente, confidenza fuori range, chiavi extra, output strutturato assente, envelope di errore, exit ≠ 0, timeout (con kill dell'albero).
- **Codex:** risposte invalide e messaggio finale assente.
- **CLI:** mancante → fallback senza spawn; rilevamento da env/`PATH`; senza fallback l'errore si propaga.
- **Cache:** hit senza subprocess, cache separata per provider, chiave che cambia con context/modello, fallimenti mai in cache, `use_cache=False`.
- **Prompt:** START diverso da END; il prompt contiene transcript/silenzi/candidati ma non path, id, fingerprint, settings o cwd; terminal partial.
- **Parser, factory, log:** parser stretto, factory e modalità riservate, log JSONL locale senza transcript, CLI di confronto su context salvati.

## 16–18. Smoke reale (videoprova, solo context già salvati)

Output in [agent_smoke_v1/](gameplay_validation/videoprova/boundaries/agent_smoke_v1/):
- prompt inviati;
- `decisions.json`;
- log;
- rerun dalla cache;
- prova di timeout forzato.

Modello e effort predefiniti delle CLI dell'utente (Claude `claude-opus-5-5`; Codex quello del suo profilo, effort `xhigh`). Un confronto unico: 50.4 s, 0 fallback.

|Context|Resolver|#|Timestamp|Sorgenti candidato|Confidenza|Runtime|Costo|
|---|---|---:|---:|---|---:|---:|---:|
|G1 END|exact|3|1301.083|RESULT_ANCHOR+TRANSCRIPT_SEGMENT_END|1.00|—|—|
|G1 END|claude|15|1310.063|TRANSCRIPT_SEGMENT_START+SILENCE_MID|0.70|7.9 s|$0.064|
|G1 END|codex|14|1309.573|SILENCE_START|0.91|17.5 s|n/d|
|G3 END|exact|1|2791.983|RESULT_ANCHOR+TRANSCRIPT_SEGMENT_END+TRANSCRIPT_SEGMENT_START|1.00|—|—|
|G3 END|claude|10|2803.848|SILENCE_MID|0.75|5.6 s|$0.042|
|G3 END|codex|8|2802.573|TRANSCRIPT_SEGMENT_END+TRANSCRIPT_SEGMENT_START|0.88|19.2 s|n/d|

Motivazioni degli agenti:
- **G1 Claude:** "le reaction alla loss finiscono a +8.18 e il tema apertura busta inizia a +8.98".
- **G1 Codex:** "silenzio dopo l'ultima reaction, prima del nuovo tema (apertura busta)".
- **G3 Claude:** "le scuse finiscono a +10.6, il nuovo tema mazzi dopo il silenzio di ~2 s".
- **G3 Codex:** "le scuse finiscono qui e inizia la discussione su quale mazzo provare".

Entrambi gli agenti spostano l'END dall'anchor (+9 s per G1, +10.6–11.9 s per G3) verso i cambi di argomento già osservati nella sezione "Observed editorial signals". In entrambi i context scelgono candidati diversi ma vicini (0.5 s e 1.3 s).

**Nessuno è dichiarato corretto**: manca la ground truth manuale dei confini. Da osservare in futuro: nel G3 Claude taglia a metà del silenzio interno al segmento ASR che inizia con "Cosa vediamo provare"; solo un'etichetta manuale può dire quale scelta è migliore.

## 19. Suite

|Controllo|Esito|
|---|---|
|Gameplay + boundary + agent|406 passed|
|Full pytest|855 passed, 1 skipped (googleapiclient opzionale, preesistente)|
|Ruff|PASS|
|Mypy|PASS (72 file)|
|GUI smoke offscreen|exit 0|
|Detector freeze|49/49 invariati|

Fix incluso: `transcript_gap_before/after_s` limitati a ≥ 0 (un segmento che tocca il candidato entro la tolleranza dava −0.02 s), con un test di regressione. I context salvati in `final_smoke_it` non sono stati rigenerati.

## 20. Cosa manca per collegare il resolver a `LOSS → cut`

1. **Orchestrazione nella CLI principale:**
   - `--gameplay hearthstone --keep-games wins --boundary-mode M` in `automation/cli.py`;
   - analisi gameplay → `BoundaryAnalyzer` per le partite REMOVE/REMOVE_TO_EOF → `create_resolver(M)` → decisioni START/END;
   - privacy notice e opt-in espliciti per le modalità remote nella CLI e nella GUI.
2. **Da decisioni a `Segment` di taglio:**
   - [START.timestamp, END.timestamp], oppure fino a EOF per i terminal partial;
   - validazione START < END;
   - gestione delle sovrapposizioni tra partite consecutive (END di N oltre START di N+1).
3. **Merge con i silence cut e i manual cut** (`compute_classic_cuts`/`merge_overlaps`) e rispetto di `suppressed_cuts`/`user_override`; il cut di gameplay deve restare annullabile nell'editor (non distruttivo).
4. **Persistenza nel progetto:** decisioni, resolver, versione prompt e fallback, così che un'apertura successiva non richiami l'agente; `termination` persistita in `GameSegment` (schema v2).
5. **Ground truth dei confini e metrica:** errore in secondi e frase spezzata, per confrontare exact, claude e codex prima di scegliere il default. Il dataset (`dataset.py`) può raccogliere le scelte dell'utente come etichette.
6. **Policy di confidenza:** sotto una soglia → exact o richiesta di conferma all'utente; si decide dopo la ground truth.
7. **Exporter:** nessuna modifica necessaria in linea di principio (consuma i `Segment` di taglio), da verificare end-to-end su un VOD.

## Aggiornamento — pipeline CLI end-to-end

Il prompt è passato a `boundary_agent_prompt_v2` (etichette semantiche per segmento, partite vicine, shortlist non distruttiva) e i resolver sono collegati ai gameplay cut e all'export: vedi [GAMEPLAY_PIPELINE.md](GAMEPLAY_PIPELINE.md).
