# Clips — fase 1: motore e CLI (clip orizzontali)

17 ottobre 2026. Clip da 20-60 s ricavate da un montaggio (progetto `.autocutter` o video esportato),
renderizzate dalle **sorgenti originali** con gli stessi tagli del video finale. Nessuna chiamata a
provider. La pagina Clips (fase 2) e il formato Shorts 9:16 con sottotitoli (fase 3) usano questo motore.

```powershell
python main.py clips --video "D:\Videos\Hunter.mp4"                 # proposte (clips.json)
python main.py clips --video "D:\Videos\Hunter.mp4" --render C01,C03  # render delle scelte
python main.py clips --project "VOD.autocutter" --render all --max 45 --json
```

Opzioni: `--output` (default `<nome>_clips` accanto al file), `--analysis` (cartella di un run di
packaging), `--count` (10), `--min`/`--max` (20/60 s), `--refresh`, `--codec` (auto: H.264 hardware,
altrimenti libx264), `--no-loudnorm`.

## Funzionamento

- **Analisi:** si riusa in sola lettura un run di packaging dello **stesso montaggio** (stesso digest della
  timeline: `<progetto>/packaging`, `<nome>_packaging`, `<clips>/analysis`). Altrimenti parte l'analisi
  locale del packaging (`analyze_only`, 24 highlight) in `<clips>/analysis`; le cache condivise la rendono
  rapida dopo un packaging.
- **Candidati** (`analysis/clips/candidates.py`): ogni highlight viene allargato a frasi intere (inizio
  di frase, fine frase entro ±6 s, perché Whisper punteggia poco nel parlato veloce), portato a
  20-60 s, tenuto **dentro la sua partita**; saluti/call to action e intro/outro del canale sono
  penalizzati; i momenti sovrapposti oltre il 30% vengono scartati.
- **Render** (`analysis/clips/render.py`): un FFmpeg per clip; ogni pezzo tenuto è tagliato dalla
  sorgente (seek accurato), con micro-fade audio a ogni giunzione, concat, 1920×1080 (pad), loudnorm
  -14 LUFS, AAC 192 kbps, `+faststart`. Scrive `*.partial.mp4`, valida stream e durata (±1 s) e solo
  allora promuove il file. Ctrl+C annulla il processo in corso.
- **`clips.json`:** proposte, spunte, esportazioni; un `--refresh` dello stesso montaggio conserva le
  spunte dei momenti già scelti.

Verifica reale (WARLOCK `Selected_games.mp4`): 10 proposte da 23-41 s, intro esclusa; C01 e C06
renderizzate con `h264_amf`, 1080p60, -13,7 LUFS integrati.

# Fase 2 — pagina Clips (GUI)

Quinto tab dell'inspector, **Clips** (Alt+5). Il tab Thumbnails si chiama ora **Covers** perché con
cinque tab l'etichetta veniva troncata.

- **Source:** *Current project* (snapshot del montaggio corrente in `<progetto>_clips/` accanto al
  `.autocutter` salvato; per un progetto non salvato in `Exports/clips/project-…`, cartella ricordata nel
  progetto, chiave `clips`) oppure *Video*: export del progetto (`— export`: clip dal VOD originale con i
  tagli di quell'export) o file scelto con **…** (`— file`).
- **Length** (20–60 s) e **Moments** (10), **Find moments** in background (annullabile),
  **Recompute proposals** ricalcola mantenendo le spunte degli stessi momenti.
- **Moments:** una scheda per proposta con frame, orari nel montaggio, durata, risultato della
  partita, frase, note (saluti/intro). Spunta, **In/Out** (secondi del montaggio, minimo 3 s: ricalcola
  pezzi sorgente e testo) e **Preview**, che riproduce i pezzi tenuti uno dopo l'altro saltando i tagli.
  Spunte e trim vengono salvati subito in `clips.json`.
- **Export N clips:** solo le spunte, 16:9 1080p, loudness -14 LUFS opzionale, avanzamento per clip,
  annullabile; **Open clips folder** a fine export. Alla chiusura dell'app un job in corso viene annullato.

Verificato con test GUI offscreen (sorgenti, snapshot, spunte/trim/salvataggio, export solo delle
spunte, tab nella MainWindow) e screenshot sulla run WARLOCK reale. La **Preview** usa QtMultimedia e va
provata nell'app vera.

# Fase 3 — Shorts 9:16 e sottotitoli

```powershell
python main.py clips --video "...\Selected_games.mp4" --preview C01          # PNG del layout
python main.py clips --video "...\Selected_games.mp4" --render C01 --format vertical
python main.py clips --project "VOD.autocutter" --render all --format both --no-subtitles
```

- **Layout** (`analysis/clips/layout.py`), 1080×1920: webcam dello stream in alto (ROI del packaging,
  ritagliata all'aspetto del pannello senza deformare, 30-42 % dell'altezza, arrotondata sempre verso
  l'interno della ROI), board in basso (ritaglio centrale a tutta altezza, all'aspetto del pannello). Senza
  webcam: board 3:4 al centro su sfondo sfocato dello stesso video.
- **Sottotitoli** (`analysis/clips/captions.py`): parole Whisper (tempi del montaggio → tempi della clip),
  1-3 parole alla volta (nuovo blocco su pausa > 0,45 s o punteggiatura, max 18 caratteri), maiuscolo,
  bianco con contorno nero, **parola pronunciata in giallo**, sulla giunzione webcam/board. Righe senza
  tempi per parola: parole distribuite uniformemente. Font: Arial Black (poi Impact/Arial) copiato in
  `<clips>/.fonts`; FFmpeg gira nella cartella delle clip, quindi `.ass` e font sono relativi e nessun
  percorso Windows va escapato nel filtro.
- **Render:** stesso comando della 16:9 (pezzi tenuti, fade, loudnorm), ma i pezzi sono normalizzati
  alla risoluzione sorgente e poi passano dal layout + `ass`. Nome `C01_…_9x16.mp4`, file `C01_9x16.ass`
  conservato accanto.
- **GUI:** Format *16:9 · 1080p* / *Shorts 9:16* / *Both*; con Shorts compaiono *Captions on Shorts* e
  **Shorts layout preview** (PNG con lo stesso grafo FFmpeg sul frame della prima clip spuntata). Il bottone
  mostra quanti file usciranno, es. "Export 2 clips (4 files)".

Limite noto: la ROI webcam è quella principale del video; nelle scene in cui l'overlay dello stream si
sposta (es. schermate di scelta) il ritaglio non lo segue.

Verifica reale (WARLOCK, C01): 1080×1920 60 fps `h264_amf`, sottotitoli sincronizzati sulla giunzione.

# Clip finder v2 — come vengono scelte le clip (17 ottobre 2026)

La v1 riusava i ~14 highlight del packaging (pensati per le copertine) e proponeva anche letture della chat
e saluti. La v2 è un motore dedicato, ispirato a come lavorano i generatori online e alla ricerca sugli
highlight dei livestream:

- **OpusClip** valuta *Hook*, *Flow*, *Value*, *Trend*; segnali da trascrizione (hook, arco narrativo,
  intensità emotiva), audio (tono, ritmo, risate) e video
  ([virality score](https://help.opus.pro/docs/article/virality-score),
  [API](https://www.opus.pro/blog/viral-moment-detection-api)).
- **Eklipse** (gaming): eventi di gioco, picchi audio, velocità della chat, modelli per gioco
  ([guida](https://eklipse.gg/help/how-to-make-stream-highlights-with-eklipse/)).
- Ricerca: fondere audio, linguaggio ed eventi di gioco batte i segnali singoli
  ([Ringer 2022](https://etheses.whiterose.ac.uk/32406/)); audio + chat battono ognuno da solo.
- Short-form: il gancio nei primi 2-3 s, partire tardi, payoff non tagliato, 20-45 s
  ([OpenClip](https://openclip.app/guides/hook-optimization-guide),
  [Eklipse hooks](https://blog.eklipse.gg/social-media/how-to-create-a-strong-hook-for-short-form-video-content.html)).

## 1. Ranking locale (`analysis/clips/scoring.py`)

Ogni finestra che inizia su una frase e finisce su una frase (20-60 s) riceve un punteggio spiegabile:

| Segnale | Peso | Cosa misura |
|---|---|---|
| arousal | 0,28 | voce più forte del *proprio* tono abituale (percentile p50→p95 dello streamer), picco + media dei momenti alti |
| reaction | 0,24 | sfoghi: parole "hype" (letale, top deck, rotto, assurdo, imprecazioni…), frasi ("non ci credo", "ma dai", "no no"), risate, esclamazioni, ripetizioni |
| hook | 0,16 | energia e reazioni nei primi 3 s, apertura con ! o ? |
| game | 0,12 | contiene gli ultimi 75 s di una partita (turno letale/risultato; WIN > LOSS) |
| payoff | 0,10 | il picco cade dentro la clip con almeno 1,5 s di reazione dopo |
| visual | 0,05 | grande cambiamento della board |
| speech | 0,05 | quota di parlato |

Penalità: lettura chat / ringraziamenti sub, donazioni, follow; saluti e call to action; apertura lenta
("allora", "comunque"…) senza gancio; poco parlato; fuori dalle partite; oltre 45 s. Poi: niente
sovrapposizioni (> 25 %), massimo 3 clip per partita. I segnali audio/visivi sono quelli del packaging,
riletti dalla cache.

## 2. Rerank AI (`analysis/clips/ai_rank.py`, default Codex)

Le 40 finestre migliori (v2: 24-30), con la trascrizione e 10 s di contesto prima e dopo, vanno all'agente
(Codex o Claude) che, come il virality score: dà voti 0-10 a hook/flow/value/emotion e 0-100 overall,
**scarta** le clip deboli (chat, payoff mancante, contesto necessario), **stringe inizio e fine** scegliendo
tra le righe offerte, scrive un **titolo** e un motivo. Punteggio finale = 70 % AI + 30 % locale. Solo
testo, nessun video, una chiamata in cache. Se la CLI manca o fallisce resta il ranking locale, con avviso.
CLI `--ai codex|claude|none`; GUI *Ranking*: AI · Codex (default), AI · Claude, Local only.

Prova reale (WARLOCK, 40 min): locale v2 in 25 s; con Codex +1 minuto. In testa "Sei a zero, pretendo una
leggendaria" (81/100, hook 9) e "Ho sbagliato tutto anche il mana" (84/100); clip strette fino a 20 s;
6 finestre su 20 scartate con motivo.

## Export riconosciuti

L'export normale salva anche `export_edit.autocutter` accanto ai timestamp: Covers e Clips riconoscono il
video e usano il VOD originale. Dopo un export il video compare da solo nella tendina Video delle Clips; un
file scelto con "…" viene riconosciuto se accanto c'è il suo montaggio.

# Clip finder v3 — risultato, board, taratura e misura (18 ottobre 2026)

La v3 parte dalla v2 e tiene solo i cambiamenti che, misurati su momenti segnati a mano in 3 montaggi reali,
migliorano la media senza peggiorare nessun video (sezione *Taratura*). La v2 resta come profilo `v2` per il
confronto; il default è `v3`.

| Segnale v3 | Peso | Cosa cambia rispetto alla v2 |
|---|---|---|
| arousal | 0,28 | invariato (picco della voce rispetto al proprio tono abituale) |
| reaction | **0,32** | era 0,24: le reazioni dette (imprecazioni, "non ci credo", risate) sono il segnale più affidabile |
| hook | **0,08** | era 0,16: premiare l'energia dei primi 3 s faceva partire le clip già sul picco |
| game | 0,12 | **ancora sul VICTORY/DEFEAT**: istante esatto della schermata (marker del detector/progetto, altrimenti la fine della partita, che nelle isole e negli export per classe termina sul risultato). Pieno se la clip contiene il risultato e finisce entro 8 s, poi cala (lobby); 55 % se finisce nei 35 s prima (turno decisivo). Si provano anche finestre che finiscono 4 s dopo il risultato. Se il detector non ha visto la fine della partita vale la regola v2 (ultimi 75 s) |
| payoff | 0,10 | una clip che *inizia* sul picco vale 0,4 (v2: 0,6) |
| visual | 0,05 | **solo la board**: quota di pixel cambiati nell'area di gioco (x 20-80 %, y 17-83 %), webcam esclusa con margine, tracker e angoli dell'overlay fuori; normalizzata nel video stesso (75° → 98° percentile) perché la board di Hearthstone si anima sempre |
| speech | 0,05 | invariato |
| tension | 0 | calcolata e mostrata nei segnali (salto della voce dopo 10 s di calma) ma **non pesa**: aggiungerla peggiorava il ranking |

Altre correzioni:
- **Finali senza punteggiatura.** Whisper punteggia solo il 31-37 % delle righe: la v2 scartava ogni fine
  riga senza punto seguita da parlato, perdendo molti buoni finali. La v3 li accetta con una piccola penalità
  (0,04, nota "ends mid-sentence").
- **Partite senza fine vista.** Se il detector non vede la schermata del risultato, tutte le partite
  "finiscono" a fine video e si sovrappongono: ogni clip finiva nella prima partita e il limite di 3 per
  partita bloccava quasi tutto. Ora ogni partita termina dove inizia la successiva.
- **Candidati per l'AI.** Il limite di 3 clip per partita valeva anche per i candidati mandati all'AI: con
  4 partite Codex vedeva al massimo 12 finestre. Ora il limite cresce con il pool, che passa da 30 a 40
  (prompt ≤ ~55k caratteri).

Il cambiamento della board richiede una decodifica dei keyframe in più (`analyze_kept_board` in
`analysis/packaging/visual.py`, cache `board_change_v2`): ~20-100 s su 40-50 minuti la prima volta, poi
istantaneo. Se fallisce si usa il cambiamento dell'intero fotogramma, con avviso. I fotogrammi del VOD Priest
confermano che la fine della partita cade sulla schermata del risultato (±5 s).

## Taratura (18 ottobre 2026)

**Momenti di riferimento.** Ho letto per intero le trascrizioni di 3 montaggi e segnato i momenti da clip come
farebbe un editor: **A** = clip forte, **B** = buona. File in `docs/clip_marks/`:

| Video | Durata | Partite | A | B |
|---|---|---|---|---|
| WARLOCK `Selected_games.mp4` | 39:48 | non rilevate | 9 | 6 |
| Priest, export per classe (`selection-60e3…/edit.autocutter`) | 39:02 | 4, con risultato | 13 | 13 |
| Priest_2 `Priest_2.mp4` (stessa live, altro montaggio) | 50:12 | 4, fine non vista | 13 | 14 |

Limite: i momenti vengono dalla **trascrizione**, non dalla visione del video. Le reazioni senza parole
(espressioni, gesti) sono sottorappresentate e i segnali testuali possono risultare favoriti. I due Priest sono
la stessa live: contano metà ciascuno.

**Metrica.** nDCG@10: ogni momento conta una volta, alla prima proposta che lo copre (A = 1, B = 0,5), con più
valore in alto. Più recall dei momenti A e precisione (proposte che coprono un momento).

**Metodo.**
1. *Ricerca libera dei pesi* (4.000 combinazioni, validazione incrociata Priest ↔ WARLOCK): **non trasferisce**.
   I pesi trovati su un video peggiorano l'altro; con 15-27 momenti per video si impara il rumore.
2. *Diagnosi dei mancati*: il segnale voce vale 1,0 su metà delle finestre (non distingue), i finali senza
   punteggiatura scartano finestre buone, le partite sovrapposte bloccano Priest_2, il pool AI è limitato a 12.
3. *Un cambiamento alla volta sulla v2*, tenuto solo se migliora la media senza peggiorare nessun video:

| Variante (sulla v2) | Media | Priest | WARLOCK | Priest_2 |
|---|---|---|---|---|
| v2 | 0,675 | 0,80 | 0,58 | 0,75 |
| + inizio sul picco 0,4 | +0,021 | +0,03 | +0,03 | 0 |
| + finali senza punteggiatura | +0,008 | +0,03 | 0 | 0 |
| hook 0,16 → 0,08 | +0,031 | +0,03 | +0,05 | 0 |
| reaction 0,24 → 0,32 | +0,010 | +0,03 | +0,01 | −0,01 |
| + tensione (peso 0,08) | −0,003 | −0,06 | +0,02 | +0,01 |
| + voce sostenuta (media del 20 % più forte) | −0,049 | −0,04 | −0,07 | −0,01 |
| **v3 finale** (le prime quattro + ancora sul risultato + board) | **0,712** | **0,93** | 0,59 | 0,74 |

Con la tensione aggiunta alla v3 finale: Priest 0,93 → 0,72. Il miglioramento locale si concentra sul video
con i risultati delle partite noti; su WARLOCK e Priest_2 la v3 resta pari alla v2.

**Con l'AI (default).** I momenti A presenti nei candidati mandati a Codex passano dal 63 % (v2, pool 30
limitato a 12 per i video con partite) all'**86 %** (v3, pool 40). Prova reale v3 + Codex: Priest nDCG 0,86
(8 proposte su 10 su un momento A), WARLOCK 0,71 (v2 locale: 0,80 e 0,58). La v2 + Codex non è stata
rimisurata.

## Misurare il ranking: `--marks`

Scrivi in un file di testo i momenti che taglieresti tu, nel tempo del **video montato** (quello che guardi):

```text
# Priest 17/10
12:34 lethal con il drago
36:50-37:10 esplosione su Medivh
1:02:03
```

```powershell
python main.py clips --project "VOD.autocutter" --marks momenti.txt --count 10
```

Ogni profilo (`v2`, `v3`, più le proposte salvate in `clips.json`, ad esempio quelle ordinate dall'AI)
propone `--count` clip sugli stessi segnali e viene misurato con: **recall** (momenti segnati trovati),
**precision** (proposte che contengono un momento segnato) e il **rango** della prima proposta che trova ogni
momento. Un istante è trovato se cade nella clip (±2 s), un intervallo se si sovrappone per almeno metà.
Non manda niente a nessun provider e non modifica `clips.json`. Bastano 5-10 momenti su 2-3 VOD per capire se
una modifica ai pesi aiuta.

Non usato per scelta: la chat di Twitch. Idee successive: movimento in webcam, tipi di clip (giocata,
spiegazione, momento divertente), AI che guarda anche i fotogrammi, pesi appresi dalle clip esportate.

# Clip di Twitch → Short di YouTube (18 ottobre 2026)

Una clip di Twitch (link o file già scaricato) diventa **uno Short pronto da pubblicare**, 1080×1920, seguendo
le regole della forma breve:

| Regola | Come la applica ReCut (`analysis/clips/short.py`) |
|---|---|
| Primo secondo: il momento clou o una reazione forte, niente intro | Trova il **picco**: voce più forte del solito (media su 1,5 s) + parole di reazione della riga detta. Se il picco non è già nei primi 2 s, **cold open**: ~2 s del picco (da 0,7 s prima a 1,5 s dopo, tagliati su parole intere) vanno all'inizio, poi la clip parte dalla preparazione. Il silenzio iniziale oltre 1,5 s viene tolto |
| Verticale: facecam sopra, plancia sotto ingrandita sulla giocata | Webcam in alto (stesso layout delle clip). La plancia è **ritagliata dove cambia** negli 8 s prima del picco (webcam e tracker esclusi), con zoom fino a ×1,5 quanto basta a contenere la giocata. Se l'azione copre tutta la plancia: centrata, senza zoom |
| Sottotitoli grandi | Parola per parola, parola detta in giallo, corpo **0,105 × larghezza** (113 px). Una riga troppo lunga viene rimpicciolita solo quanto serve per restare nel quadro. Le elisioni restano unite ("C'È", "L'HO") |
| 20-40 s, finale che invoglia a riguardare | Una clip di Twitch è già scelta da una persona: **se ci sta, resta intera** (tolti intro muta e coda silenziosa oltre 1,5 s). Se è più lunga, si taglia dalla preparazione alla **fine della reazione**, senza coda: il loop torna al cold open |

**Webcam.** L'analisi delle clip cerca il volto sui pochi fotogrammi degli highlight, troppo pochi in 30-60 s.
Per gli Short si guardano 24 fotogrammi a piena risoluzione con volto frontale **e di profilo** (lo streamer
guarda lo schermo). Vale solo un volto nello stesso punto in almeno il 40 % dei fotogrammi **che si muove**: i
volti disegnati nel gioco (eroi, carte) restano fermi e vengono scartati. Senza webcam: solo plancia su sfondo
sfocato.

**Uso.**
```powershell
python main.py short "https://clips.twitch.tv/NomeClip"            # scarica e crea lo Short
python main.py "https://www.twitch.tv/canale/clip/NomeClip"          # un link a una clip va da solo su `short`
python main.py short "D:\Clips\clip.mp4" --no-cold-open --max 35    # file già scaricato
```
Opzioni: `--output`, `--max` (40), `--no-cold-open`, `--no-zoom`, `--no-subtitles`, `--codec`, `--no-loudnorm`,
`--json`. Nella GUI: pagina **Clips → Twitch clip → Short** (link o "…", tre spunte, *Make Short*; alla fine
anteprima 9:16, titolo per YouTube e *Open folder*).

**File prodotti** (in `<clip>_clips`, accanto alla clip): `<clip>_short.mp4`, `<clip>_short_preview.png`
(primo fotogramma con layout e sottotitoli), `<clip>_short.json` (piano: cold open, parte usata, picco, zoom,
note; titolo suggerito = titolo della clip + `#Shorts`). Le clip scaricate vanno in
`<cartella download>/twitch_clips/<id>.mp4` con i metadati pubblici in `<id>.twitch.json`.

**Limiti.** Se lo streamer ha già sottotitoli in diretta nella scena, togliere *Big captions* per non averli
doppi. Lo zoom è fisso per tutto lo Short (nessun movimento di camera). Il picco è il momento più forte per
voce e parole: una giocata muta e senza reazione non viene riconosciuta come picco.

Prove reali: due clip pubbliche di streamer italiani di Hearthstone (57 s → 34 s attorno al picco con cold open;
35 s tenuta intera + cold open di 3 s, webcam trovata di profilo con il cappello) e un pezzo di 50 s di un VOD
lugozzi (21 s + cold open, plancia zoomata ×1,47 sulla giocata).
