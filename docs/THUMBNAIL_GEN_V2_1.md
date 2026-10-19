# Thumbnail generator v2.1 — full generative restored as default

16 ottobre 2026. Le copertine V4/V4.1 (`reference_preserving`) erano giudicate inaccettabili:
il modello di immagini riceveva solo un frame di gameplay con l'ordine di non disegnare volti,
carte o testo, e ReCut incollava sopra crop webcam/carta e tipografia locale. Risultato: look
"screenshot con adesivi", toppa grigia della webcam riprodotta, carta duplicata, testo piccolo.
Il raw generato era praticamente identico al backdrop inviato.

Riferimento buono: run del 14 ottobre (Packaging V3, engine `gen`, mode `fast`, generatore
`thumbnail_gen_v2`), copertine approvate in `Desktop\copertine`.

## Cosa cambia

| | V4.1 (16 ott) | gen v2.1 (default) |
|---|---|---|
| Strategia | `reference_preserving` | `full_generative` |
| Planning | `packaging_fast_gen_gameplay_v1` + design packs | `packaging_fast_gen_v2` (contratto del 14 ott: 4 ruoli) |
| Immagini al modello | 1 backdrop per variante | webcam + alternative, board, crop carta, ≤2 copertine di stile |
| Chi compone | compositing locale + tipografia locale | il modello (testo compreso) |
| Output | contain/padding 1920×1080 | centre-crop 16:9 → 1920×1080 |

Il prompt è `thumbnail_gen_v2` (`GENERATOR_BASE`) con tre aggiunte (`GENERATOR_VERSION =
thumbnail_gen_v2.1`):

1. **Frame puliti** — `clean_frame()`: l'overlay webcam dello stream che tocca un bordo viene
   tagliato via come striscia intera (massimo 30% del frame) invece di essere coperto da una toppa
   sfocata; altrimenti inpainting come prima.
2. **Fedeltà carta** — "Animate it, do not change it": stessa carta, nome, testo e numeri
   leggibili; glow/aura/energia solo dietro e intorno ai bordi, niente effetti "spaghetti" sopra.
3. **Riferimenti di stile** — fino a 2 copertine approvate del profilo creator (categorie diverse
   quando possibile) o riferimenti guidati di ruolo `style`; dichiarate come finitura, mai contenuto
   né prova di gameplay. La cache `generated` include il digest dei file di stile.

## Compatibilità

- `reference_preserving` resta selezionabile (GUI: *Image strategy*; CLI/progetto:
  `creative_direction.generation_strategy`).
- `CreativeDirection.strategy_version = 2`: una direzione salvata senza questo campo (scritta dalla
  V4, dove `reference_preserving` era solo il default) viene migrata a `full_generative`. Una scelta
  esplicita salvata dopo questa versione viene rispettata.
- Le evidenze V4 (export per classe, Creative Focus, crop webcam primary/alt) restano invariate.
- Il report indica `pipeline_revision: 4.2`, `generation.generation_strategy`,
  `generation.generator_version`, `generation.generator_base` e `generation.style_references`.

## Importare le copertine approvate nel profilo

```powershell
python main.py packaging-style import lugozzi --thumbnails "C:\Users\jakub\Desktop\copertine\C2.png" --categories decisive_card
python main.py packaging-style import lugozzi --thumbnails "C:\Users\jakub\Desktop\copertine\concept_01.png" --categories unexpected_reaction,surprising_result
```

Le categorie sono gli story type delle opportunità (`decisive_card`, `unexpected_reaction`,
`surprising_result`, `impressive_board`, `comeback`, `strategic_decision`, ...).

## v2.2 — varietà del portfolio e volto grande (16 ottobre 2026)

- **Bug rotazione stili:** `choose_compositions` escludeva stili testo e gradazioni usati nelle ultime
  2 run. Con 6 opzioni per tipo, una run sì e una no ne restava **una sola**, assegnata a tutte e 4 le
  varianti (es. Mech Hunter: tutto "cold blue with a red accent" + testo giallo 3D). Ora ogni run ha
  4 stili e 4 gradazioni **distinti**, ordinati dal meno usato di recente (`_least_recent`), con
  tie-break stabile per video. Le scelte salvate con il vecchio algoritmo non vengono riusate
  (`CHOICE_VERSION = 2` in `composition_history.json`).
- **Volto:** quando la webcam è nel pacchetto il creator è un soggetto principale (≥ ~1/3
  dell'altezza), mai una faccina in un angolo; le ricette `small-*` sono state rese "large". Il
  chooser penalizza le ricette senza volto quando il pacchetto ne contiene uno. Varianti senza webcam:
  "creator: not in this thumbnail".
- **Elementi:** volto, ogni carta e ogni ritratto eroe contano come elemento forte (max 2-3, hook a
  parte). Il planner sa che una schermata VS/mulligan da sola non è un momento.
- `GENERATOR_VERSION = thumbnail_gen_v2.2`.

## Pagina Thumbnails — Source

Due scelte:

- **Current project** — senza selezione nel tab Export: una copertina unica. Con *Single video —
  selected classes*: un set per quel video. Con *Videos by class*: compare **Covers for**, una spunta
  per ogni video in uscita (es. `Mage · 3 WIN`); le copertine si generano solo per quelli spuntati,
  l'export non cambia.
- **Video** — tendina con gli export del progetto (`— export`, riusano snapshot, partite e keep) e i
  file scelti con **…** (`— file`, partite rilevate sul video).
