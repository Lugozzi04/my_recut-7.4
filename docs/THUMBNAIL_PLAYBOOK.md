# Copertine: playbook, ideazione, critica e "La tua idea" (19 ottobre 2026)

Le copertine generate erano ben rifinite ma con idee deboli ("Oltre un mazzo", "La distinzione conta",
"Aspetta a crearla"): il modello scriveva "messaggi" educativi ed era obbligato a 4 angoli diversi. La copertina
migliore del creatore (fatta ragionando con ChatGPT: **"NON CRAFTARLA!"** sulla carta peggiore della tier list,
volto scettico, tier list leggibile dietro, frecce e X rossa) è nata da un passaggio che mancava: **ragionare
sull'idea prima di disegnare, poi criticare e rifare**. Ora la pipeline lo fa.

## Il flusso (motore `gen`, strategia Full generative, il default)

| Fase | Cosa succede | File |
|---|---|---|
| Analisi | trascrizione, highlight, frame, webcam, **schermata finale** (stato finale di ciò che il video costruisce, es. la tier list completa) | `visual_assets.json` |
| **Ideazione** | promessa del video, pubblico, emozione; **8-12 agganci grezzi** per famiglia (avvertimento, giudizio, curiosità, contrasto, risultato estremo, reazione, sfida); scelta delle **2 idee** più forti con il perché; perché le altre perdono | `thumbnail_ideation.json` |
| Concetti | **2 idee × 2 varianti** (A/B): le varianti cambiano composizione, lato/dimensione del volto, espressione o parole del testo | `thumbnail_concepts.json` |
| Materiale | carta ufficiale (per nome, nella lingua del video), immagini del creatore, schermata finale | `official_cards.json` |
| Generazione | il modello d'immagine compone le 4 copertine 1920×1080 | `thumbnails/generated/` |
| **Critica** | un revisore le valuta (anche a misura di telefono, 168×94) con la checklist, le ordina e sceglie **una** da migliorare con 3-6 modifiche concrete | `thumbnail_critique.json` |
| **Revisione** | la scelta viene rigenerata con le modifiche: diventa `C?R` e la **preferita** | `thumbnails/generated/C?R.png` |

Prompt: `packaging_gen_playbook_v1` con regole `thumbnail_playbook_v2` (pianificazione), `thumbnail_gen_v2.3` (generazione),
`thumbnail_critique_v1`, `thumbnail_revision_v1`. Il vecchio contratto resta per la strategia sperimentale
Reference preserving (V4).

## Le linee guida (`analysis/packaging/playbook.py`)

- **Un'idea**, leggibile in un secondo a misura di telefono.
- **Testo di 2-4 parole**: imperativo, giudizio o esclamazione (NON CRAFTARLA!, TRAPPOLA, ROTTA?!), mai una frase
  astratta o un riassunto.
- **Coraggio** (v2): quando il creatore dà un verdetto, anche provvisorio, lo si dice chiaro; niente "ma", "tutto",
  "forse", "situazionale" nell'aggancio — le sfumature stanno nel video e nel titolo. Aggiunta dopo la prima prova
  reale, dove il modello aveva ammorbidito tutto ("GIOCABILE, MA ASPETTA", "NON CRAFTARE TUTTO").
- **Un oggetto batte una lista**: un video di classifica si vende con UNA carta precisa. Per tier list e guide
  almeno un'idea scelta deve riguardare una carta nominata (`card_name`, controllato in validazione).
- **Copertina = emozione e giudizio; titolo = chiarezza e promessa utile**; mai le stesse parole.
- **Gerarchia volto → testo → oggetto**, al massimo 3 elementi forti.
- **L'oggetto del video grande** (carta al 35-45 % dell'altezza), immagine ufficiale nella lingua del pubblico.
- **Contesto riconoscibile ma subordinato**: tier list / plancia dietro, etichette leggibili, sfocata e scurita
  30-40 %, mai nera.
- **Espressione e gesto coerenti col messaggio** (scettico + mano "stop" per un avvertimento); si può
  intensificare, mai cambiare persona.
- **Segni grafici con un significato**: frecce verso la carta, X rossa su una carta da evitare, spunta verde,
  aura dietro la carta. Niente decorazioni senza scopo, niente collage. (Prima erano vietati.)
- **Contrasto** rosso/arancio contro bianco/giallo; **verità**: niente carte, testi, risultati o numeri inventati.
- Per tipo di video: *tier list / guida* → l'avvertimento batte l'utilità (carta peggiore + warning, il titolo porta
  i safe craft); *highlight* → momento decisivo + reazione; *guida al mazzo* → carta firma + verdetto vero;
  *reazione* → espressione forte + oggetto dell'emozione.

**Font** (ricerca: [thumbnailtest](https://thumbnailtest.com/guides/youtube-thumbnail-fonts/),
[Snappa](https://snappa.com/blog/best-youtube-thumbnail-fonts/),
[CapCut](https://www.capcut.com/resource/youtube-thumbnail-font)): il modello d'immagine disegna il testo, quindi
gli stili sono descritti come aspetto di font precisi, tutti Google Fonts (SIL OFL): **Anton** (condensato, il più
usato), **Montserrat Black** (geometrico, contorno), **Bebas Neue** (alto e stretto, per parole lunghe come
CRAFTARLA/INGIOCABILE), **Luckiest Guy** (fumetto, gaming). Ogni variante ne riceve uno diverso.

**Checklist della critica** (0-10): idea, testo, gerarchia, oggetto, volto, contesto, rifinitura, coerenza col
titolo.

## Carte ufficiali (`analysis/packaging/card_art.py`)

Una carta letta in un frame (o chiesta per nome dal modello con `card_name`) viene cercata per nome in
[HearthstoneJSON](https://hearthstonejson.com) — inglese o italiano, piccoli errori di scrittura accettati, mai
indovinata se due carte sono ugualmente vicine — e scaricata come carta completa **nella lingua del video**
(es. "The Unknowable One" letto in una tier list inglese → "L'Inconoscibile"). Un `card_name` viene usato solo se la
carta è **letta in un frame, detta nel video o scritta nella tua idea**. Tutto in cache; senza rete resta il
ritaglio dal video. Le immagini delle carte sono © Blizzard Entertainment (HearthstoneJSON è un progetto non
ufficiale): la provenienza è registrata in `official_cards.json`. Disattivabili con *Official card images* /
`--no-official-cards`.

## "La tua idea" (GUI e CLI)

Nella pagina **Covers → Creative Direction**, in cima:

- **Your idea**: una riga o una descrizione, nelle tue parole. Se descrive una copertina precisa ("NON CRAFTARLA su
  Lama dell'Impero Nero, faccia schifata a destra, tier list dietro") diventa **C1** e le altre sono le alternative
  migliori; se è uno spunto (una carta, una parola, un tono) guida tutte le idee.
- **Add image…**: fino a 3 immagini da usare nella copertina (es. la carta). Il pianificatore le vede per prime
  (U1..U3) e le copertine che le richiedono le ricevono.
- Vale anche in modalità **Automatic**: non serve compilare altro.
- **More options ▸** contiene tutto il resto (modalità Guided, titolo/testo esatti, riferimenti, tipografia,
  strategia, profilo) più *Critique and improve the best cover* e *Official card images* (attivi di default).

CLI:
```powershell
python main.py packaging --project VOD.autocutter --idea "NON CRAFTARLA su Lama dell'Impero Nero" --idea-image lama.png
python main.py packaging --video video.mp4 --no-critique --no-official-cards
```

Costo: oltre alla pianificazione e alla generazione delle 4 copertine, la critica aggiunge **una chiamata
all'agente** (le 4 immagini + la striscia a misura di telefono) e **un'immagine** (la revisione).
