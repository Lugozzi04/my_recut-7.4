"""Inspector pages: Edit (cuts, preset, detection, advanced) and Analysis (mode, AI, gameplay)."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui.gameplay_editing_ui import BOUNDARY_CHOICES
from ui.widgets.inspector import Hint, LegendItem, PropertyRow, Section, SegmentedControl, ToggleRow, ValueHeader


def _scroll_page(page: QWidget, sections: list[QWidget], *, name: str) -> QScrollArea:
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    content = QWidget()
    content.setObjectName("InspectorContent")
    col = QVBoxLayout(content)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(0)
    for s in sections:
        col.addWidget(s)
    col.addStretch(1)
    scroll = QScrollArea()
    scroll.setObjectName("InspectorScroll")
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(content)
    scroll.setObjectName(name)
    scroll.setProperty("inspectorScroll", True)
    lay.addWidget(scroll)
    return scroll


def _badge(text: str, kind: str = "muted") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("Badge")
    lbl.setProperty("kind", kind)
    lbl.setAlignment(Qt.AlignCenter)
    return lbl


def _icon_button(tip: str) -> QToolButton:
    b = QToolButton()
    b.setObjectName("IconButton")
    b.setToolTip(tip)
    b.setFixedSize(30, 30)
    b.setIconSize(QSize(16, 16))
    b.setCursor(Qt.PointingHandCursor)
    return b


def _compact(widget: QWidget, width: int = 120) -> QWidget:
    widget.setMaximumWidth(width)
    widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return widget


# --------------------------------------------------------------------------------------------
# Edit page
# --------------------------------------------------------------------------------------------
def build_main_page(window) -> None:
    """Populate window.page_main (Edit tab)."""
    # ---- Cuts: legend counts + selection -----------------------------------------------------
    window.lbl_cuts_total = QLabel("")
    window.lbl_cuts_total.setObjectName("SectionMeta")
    cuts = Section("Cuts", action=window.lbl_cuts_total)
    window.section_cuts = cuts

    grid_w = QWidget()
    grid = QGridLayout(grid_w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(18)
    grid.setVerticalSpacing(6)
    window.cut_count_labels = {}
    window.cut_legend_items = {}
    for n, (key, text, hatched) in enumerate((
        ("auto", "Automatic", False), ("manual", "Manual", False),
        ("gameplay", "WIN kept", False), ("restored", "Restored", True),
    )):
        count = QLabel("0")
        item = LegendItem("#888", text, hatched=hatched, count_label=count)
        window.cut_count_labels[key] = count
        window.cut_legend_items[key] = item
        grid.addWidget(item, n // 2, n % 2)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    cuts.add(grid_w)

    card = QFrame()
    card.setObjectName("SelectionCard")
    card.setAttribute(Qt.WA_StyledBackground, True)
    card_l = QVBoxLayout(card)
    card_l.setContentsMargins(12, 10, 12, 10)
    card_l.setSpacing(6)
    window.lbl_sel_title = QLabel("No cut selected")
    window.lbl_sel_title.setObjectName("RowLabelStrong")
    window.lbl_sel_detail = Hint("Click a cut on the timeline to inspect it. Drag across empty space to select "
                                 "several. Delete restores the selection.")
    card_l.addWidget(window.lbl_sel_title)
    card_l.addWidget(window.lbl_sel_detail)
    actions = QWidget()
    actions_l = QHBoxLayout(actions)
    actions_l.setContentsMargins(0, 2, 0, 0)
    actions_l.setSpacing(6)
    window.btn_sel_restore = QPushButton("Restore")
    window.btn_sel_restore.setObjectName("SecondarySmall")
    window.btn_sel_restore.setToolTip("Keep this part of the video (Del). Undo with Ctrl+Z.")
    window.btn_sel_jump = QPushButton("Go to cut")
    window.btn_sel_jump.setObjectName("SecondarySmall")
    window.btn_sel_jump.setToolTip("Move the playhead to the start of the selected cut")
    window.btn_island_reset = QPushButton("Reset boundaries")
    window.btn_island_reset.setObjectName("SecondarySmall")
    window.btn_island_reset.setToolTip("Restore this game's START/END to what the pipeline found (Ctrl+Z undoes)")
    window.btn_island_reset.setVisible(False)
    for b in (window.btn_sel_restore, window.btn_island_reset, window.btn_sel_jump):
        b.setCursor(Qt.PointingHandCursor)
        b.setIconSize(QSize(14, 14))
        actions_l.addWidget(b)
    actions_l.addStretch(1)
    window.sel_actions = actions
    card_l.addWidget(actions)
    window.selection_card = card
    cuts.add(card)
    window.btn_sel_restore.clicked.connect(window._restore_selected_cuts)
    window.btn_sel_jump.clicked.connect(window._jump_to_selected_cut)
    window.btn_island_reset.clicked.connect(window._reset_selected_island)

    # ---- Preset -------------------------------------------------------------------------------
    window.btn_preset_more = _icon_button("Preset actions: save changes, save as new, rename, delete")
    window.btn_preset_more.clicked.connect(window._open_preset_menu)
    if not hasattr(window, "lbl_preset_state"):
        window.lbl_preset_state = _badge("Manual")
    if not hasattr(window, "lbl_preset_meta"):
        window.lbl_preset_meta = Hint("")
    window.lbl_preset_meta.setObjectName("RowHint")
    window.lbl_preset_meta.setWordWrap(True)
    preset = Section("Preset", action=window.lbl_preset_state)
    window.section_preset = preset
    row = QWidget()
    row_l = QHBoxLayout(row)
    row_l.setContentsMargins(0, 0, 0, 0)
    row_l.setSpacing(6)
    window.preset_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    window.preset_combo.setMinimumContentsLength(8)
    window.preset_combo.setToolTip("Saved detection settings")
    row_l.addWidget(window.preset_combo, 1)
    row_l.addWidget(window.btn_preset_more)
    preset.add(row)
    preset.add(window.lbl_preset_meta)
    # legacy buttons stay alive (their slots are reused by the ⋯ menu) but are not shown
    for b in (window.btn_preset_save, window.btn_preset_save_as, window.btn_preset_manage, window.btn_preset_delete):
        b.setVisible(False)

    # ---- Detection (classic) ----------------------------------------------------------------
    detection = Section("Detection")
    window.section_detection = detection
    # classic-only controls live in one container: hidden in AI mode, where a note explains why
    controls = QWidget()
    controls_l = QVBoxLayout(controls)
    controls_l.setContentsMargins(0, 0, 0, 0)
    controls_l.setSpacing(8)
    window.detection_controls = controls
    detection.add(controls)
    window.lbl_intensity_value = QLabel("45")
    controls_l.addWidget(ValueHeader(
        "Cut intensity", window.lbl_intensity_value,
        tooltip="How short a pause must be to get cut, and how tight the edges are.",
    ))
    window.slider_precision.setToolTip("Higher removes shorter pauses and trims closer to speech. Below 10 = no cuts.")
    controls_l.addWidget(window.slider_precision)
    window.lbl_precision.setObjectName("RowHint")
    window.lbl_precision.setWordWrap(True)
    controls_l.addWidget(window.lbl_precision)
    window.section_intensity = controls

    controls_l.addSpacing(6)

    window.lbl_threshold_semantic = QLabel("45%")
    controls_l.addWidget(ValueHeader(
        "Voice threshold", window.lbl_threshold_semantic,
        tooltip="Audio quieter than this level counts as silence.",
    ))
    window.threshold_slider = QSlider(Qt.Horizontal)
    window.threshold_slider.setRange(int(window.threshold_pct.minimum()), int(window.threshold_pct.maximum()))
    window.threshold_slider.setValue(int(window.threshold_pct.value()))
    window.threshold_slider.setToolTip("Higher treats more quiet speech as silence (more cuts). "
                                       "The dashed line on the AUDIO lane shows the level while you adjust it.")
    thr_row = QWidget()
    thr_l = QHBoxLayout(thr_row)
    thr_l.setContentsMargins(0, 0, 0, 0)
    thr_l.setSpacing(10)
    thr_l.addWidget(window.threshold_slider, 1)
    window.threshold_pct.setFixedWidth(64)
    window.threshold_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    thr_l.addWidget(window.threshold_pct)
    controls_l.addWidget(thr_row)
    window.threshold_note = Hint("Audio quieter than this counts as silence. Raise it if background noise or "
                                 "breathing is kept; lower it if quiet words get cut.")
    controls_l.addWidget(window.threshold_note)
    window.threshold_meter.setVisible(False)
    window.section_threshold = controls
    window.threshold_controls_row = thr_row
    window.threshold_controls_layout = None

    def _slider_to_spin(v: int) -> None:
        if int(window.threshold_pct.value()) != int(v):
            window.threshold_pct.setValue(int(v))

    def _spin_to_slider(v: int) -> None:
        if int(window.threshold_slider.value()) != int(v):
            window.threshold_slider.blockSignals(True)
            window.threshold_slider.setValue(int(v))
            window.threshold_slider.blockSignals(False)

    window.threshold_slider.valueChanged.connect(_slider_to_spin)
    window.threshold_pct.valueChanged.connect(_spin_to_slider)

    window.edit_ai_note = Hint("AI analysis is active: cut detection is driven by the AI tuning in the "
                               "Analysis tab. Switch back to Classic to use these controls.")
    window.edit_ai_note.setVisible(False)
    detection.add(window.edit_ai_note)

    # ---- Advanced (collapsed) -----------------------------------------------------------------
    window.lbl_adv_state_badge = _badge("Custom", "warning")
    window.lbl_adv_state_badge.setVisible(False)
    advanced = Section("Advanced", collapsible=True, expanded=False, action=window.lbl_adv_state_badge)
    window.section_advanced = advanced
    window.section_advanced_root = advanced
    window._adv_section_badges = {}
    window._adv_section_header_layouts = []

    def group(key: str, title: str, reset_cb, rows: list[tuple[str, QWidget, str]],
              extra: list[QWidget] | None = None) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 6)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        t = QLabel(title)
        t.setObjectName("RowLabelStrong")
        head.addWidget(t)
        badge = _badge("Custom", "warning")
        badge.setVisible(False)
        window._adv_section_badges[key] = badge
        head.addWidget(badge)
        head.addStretch(1)
        reset = QPushButton("Reset")
        reset.setObjectName("LinkButton")
        reset.setCursor(Qt.PointingHandCursor)
        reset.setToolTip(f"Restore default {title.lower()} settings")
        reset.clicked.connect(reset_cb)
        head.addWidget(reset)
        lay.addLayout(head)
        for label, widget, tip in rows:
            _compact(widget, 132)
            lay.addWidget(PropertyRow(label, widget, tooltip=tip))
        for widget in extra or []:
            lay.addWidget(widget)
        return w

    def after_reset() -> None:
        try:
            window._refresh_advanced_ui_state()
        except Exception:
            pass

    def reset_padding() -> None:
        window.pre_pad_s.setValue(window.pre_pad_s_default)
        window.post_pad_s.setValue(window.post_pad_s_default)
        window.min_cut_s.setValue(window.min_cut_s_default)
        after_reset()

    def reset_detection() -> None:
        window.attack_ms.setValue(window.attack_ms_default)
        window.release_ms.setValue(window.release_ms_default)
        window.gain_affects_detection.setChecked(window.gain_affects_detection_default)
        after_reset()

    def reset_smoothing() -> None:
        window.smoothing_mode.setCurrentText(window.smoothing_mode_default)
        window.merge_pauses_ms.setValue(window.merge_pauses_ms_default)
        after_reset()

    def reset_audio() -> None:
        window.gain_db.setValue(window.gain_db_default)
        window.normalize_lufs.setChecked(window.normalize_lufs_default)
        window.lufs_target.setValue(window.lufs_target_default)
        window.limiter.setChecked(window.limiter_default)
        after_reset()

    window.adv_block_timing = group("padding", "Padding", reset_padding, [
        ("Before speech", window.pre_pad_s, "Audio kept before each voice segment"),
        ("After speech", window.post_pad_s, "Audio kept after each voice segment"),
        ("Shortest cut", window.min_cut_s, "Pauses shorter than this are never cut"),
    ])
    window.adv_block_detection = group("detection", "Voice detection", reset_detection, [
        ("Attack", window.attack_ms, "How quickly voice is confirmed (shorter = faster, longer = steadier)"),
        ("Release", window.release_ms, "How quickly silence is confirmed"),
    ], extra=[window.gain_affects_detection])
    window.adv_block_smoothing = group("smoothing", "Smoothing", reset_smoothing, [
        ("Smoothing", window.smoothing_mode, "Reduces rapid on/off flicker between speech and silence"),
        ("Merge pauses", window.merge_pauses_ms, "Pauses shorter than this are treated as continuous speech"),
    ])
    window.adv_block_merging = window.adv_block_smoothing
    window.adv_block_audio = group("audio", "Export audio", reset_audio, [
        ("Gain", window.gain_db, "Volume change applied to the exported audio"),
        ("LUFS target", window.lufs_target, "Loudness target when normalising (YouTube ≈ −14 LUFS)"),
    ], extra=[window.normalize_lufs, window.limiter])
    window._adv_section_badges["merging"] = window._adv_section_badges["smoothing"]
    for blk in (window.adv_block_timing, window.adv_block_detection, window.adv_block_smoothing,
                window.adv_block_audio):
        advanced.add(blk)
    window.gain_affects_detection.setToolTip("Also apply the export gain when detecting voice (usually off)")
    window.normalize_lufs.setToolTip("Level the exported loudness to the LUFS target")
    window.limiter.setToolTip("Prevent clipping after gain or loudness changes")

    # legacy references kept for code that toggles the old panel
    window.advanced_panel = advanced.body
    window.adv_header = advanced
    window.adv_header_layout = None
    window.advanced_scroll = None
    window._advanced_focus_hidden_widgets = []
    window.lbl_adv_title.setVisible(False)
    window.lbl_adv_hint.setVisible(False)
    window.btn_adv_toggle.setVisible(False)

    def _on_adv_toggled(expanded: bool) -> None:
        window._adv_open = bool(expanded)

    advanced.toggled.connect(_on_adv_toggled)

    window.main_scroll = _scroll_page(window.page_main, [cuts, preset, detection, advanced],
                                      name="InspectorScroll")


# --------------------------------------------------------------------------------------------
# Analysis page
# --------------------------------------------------------------------------------------------
def build_analysis_page(window) -> None:
    """Populate window.page_analysis (Analysis tab)."""
    mode = Section("Analysis mode")
    window.section_analysis_mode = mode
    window.btn_mode_classic = QToolButton()
    window.btn_mode_classic.setText("Classic")
    window.btn_mode_classic.setToolTip("Fast audio-level analysis. Runs automatically on import.")
    window.btn_mode_ai = QToolButton()
    window.btn_mode_ai.setText("AI")
    window.btn_mode_ai.setToolTip("Speech-aware analysis with speaker detection. Slower, runs on demand.")
    window.mode_segment = SegmentedControl([window.btn_mode_classic, window.btn_mode_ai])
    for b in (window.btn_mode_classic, window.btn_mode_ai):
        b.setFixedHeight(28)
    window.btn_mode_classic.clicked.connect(lambda: window.analysis_mode_toggle.setChecked(False))
    window.btn_mode_ai.clicked.connect(lambda: window.analysis_mode_toggle.setChecked(True))
    mode.add(window.mode_segment)
    window.lbl_mode_desc = Hint("")
    mode.add(window.lbl_mode_desc)
    window.lbl_analysis_status = QLabel("")
    window.lbl_analysis_status.setObjectName("StatusLine")
    window.lbl_analysis_status.setWordWrap(True)
    mode.add(window.lbl_analysis_status)
    # the old switch keeps the state (many code paths read/write it) but is not displayed
    window.analysis_mode_toggle.setVisible(False)

    # ---- AI tuning ----------------------------------------------------------------------------
    ai = Section("AI tuning")
    window.section_ai = ai
    window.lbl_ai_aggr_value = QLabel(str(int(window.ai_aggr_slider.value())))
    ai.add(ValueHeader("Aggressiveness", window.lbl_ai_aggr_value,
                       tooltip="Higher removes more non-speech."))
    window.ai_aggr_slider.valueChanged.connect(lambda v: window.lbl_ai_aggr_value.setText(str(int(v))))
    ai.add(window.ai_aggr_slider)
    for label, widget, tip in (
        ("Min speech", window.ai_min_speech_ms, "Ignore speech bursts shorter than this"),
        ("Merge gap", window.ai_merge_gap_ms, "Join speech segments separated by less than this"),
        ("Speakers", window.ai_expected_speakers, "Expected number of speakers (Auto detects it)"),
    ):
        _compact(widget, 132)
        ai.add(PropertyRow(label, widget, tooltip=tip))
    window.btn_ai_process.setObjectName("PrimaryCTA")
    window.btn_ai_process.setText("Run AI analysis")
    window.btn_ai_process.setCursor(Qt.PointingHandCursor)
    window.btn_ai_reprocess.setObjectName("PrimaryCTA")
    window.btn_ai_reprocess.setText("Re-run AI analysis")
    window.btn_ai_reprocess.setCursor(Qt.PointingHandCursor)
    ai.add(window.btn_ai_process)
    ai.add(window.btn_ai_reprocess)
    # ai_options_panel visibility is driven by MainWindow._update_ai_options_panel
    window.ai_options_panel = ai

    # ---- AI results ---------------------------------------------------------------------------
    stats = Section("AI results")
    tiles = QWidget()
    tiles_l = QHBoxLayout(tiles)
    tiles_l.setContentsMargins(0, 0, 0, 0)
    tiles_l.setSpacing(8)
    for caption, value in (("Speakers", window.ai_stats_speakers), ("Voice", window.ai_stats_voice),
                           ("Other", window.ai_stats_noise), ("Speech", window.ai_stats_total)):
        tile = QFrame()
        tile.setObjectName("SummaryCard")
        tl = QVBoxLayout(tile)
        tl.setContentsMargins(10, 8, 10, 8)
        tl.setSpacing(0)
        value.setObjectName("StatValue")
        cap = QLabel(caption)
        cap.setObjectName("StatCaption")
        tl.addWidget(value)
        tl.addWidget(cap)
        tiles_l.addWidget(tile, 1)
    stats.add(tiles)
    window.ai_stats_speaker_list.setObjectName("RowHint")
    stats.add(window.ai_stats_speaker_list)
    window.ai_stats_timeline.setMinimumHeight(28)
    stats.add(window.ai_stats_timeline)
    window.ai_stats_panel = stats

    # ---- Gameplay editing (runs the same service as the CLI pipeline) ------------------------
    gp = Section("Gameplay editing", subtitle="Hearthstone")
    window.section_gameplay = gp
    window.chk_gameplay_apply = ToggleRow(
        "Keep only the selected games",
        "Removes everything outside the kept games; audio cuts still apply inside them.")
    window.chk_gameplay_apply.setToolTip("Turn the gameplay edit on/off without losing it (Ctrl+Z also works)")
    gp.add(window.chk_gameplay_apply)
    window.gameplay_keep_combo = QComboBox()
    window.gameplay_keep_combo.addItem("Complete wins only", "wins")
    gp.add(PropertyRow("Keep", window.gameplay_keep_combo,
                       tooltip="Games that are kept: every complete WIN (losses, partial games and the rest "
                               "of the stream are removed)"))
    window.gameplay_boundary_combo = QComboBox()
    for key, label, tip in BOUNDARY_CHOICES:
        window.gameplay_boundary_combo.addItem(label, key)
        window.gameplay_boundary_combo.setItemData(window.gameplay_boundary_combo.count() - 1, tip, Qt.ToolTipRole)
    gp.add(PropertyRow("Boundaries", window.gameplay_boundary_combo,
                       tooltip="How each kept game's START/END is chosen"))
    window.gameplay_end_policy_combo = QComboBox()
    for key, label, tip in (("tight", "Tight", "Reaction to the win plus one essential sentence"),
                            ("balanced", "Balanced", "Reaction plus a short recap specific to the game (default)"),
                            ("extended", "Extended", "Reaction plus a longer explanation specific to the game")):
        window.gameplay_end_policy_combo.addItem(label, key)
        window.gameplay_end_policy_combo.setItemData(window.gameplay_end_policy_combo.count() - 1, tip,
                                                     Qt.ToolTipRole)
    window.gameplay_end_policy_combo.setCurrentIndex(1)
    gp.add(PropertyRow("End of games", window.gameplay_end_policy_combo,
                       tooltip="With Claude/Codex: how much talk after each win is kept. Chat, unrelated talk "
                               "and the move to the next game are always cut."))
    window.lbl_gameplay_boundary_hint = Hint("")
    gp.add(window.lbl_gameplay_boundary_hint)
    window.btn_gameplay_run = QPushButton("Detect games")
    window.btn_gameplay_run.setObjectName("PrimaryCTA")
    window.btn_gameplay_run.setCursor(Qt.PointingHandCursor)
    window.btn_gameplay_run.setToolTip("Find the games and their results, then keep the complete wins. "
                                       "Runs in the background; the editor stays usable.")
    gp.add(window.btn_gameplay_run)
    window.gameplay_progress = QProgressBar()
    window.gameplay_progress.setRange(0, 100)
    window.gameplay_progress.setTextVisible(False)
    window.gameplay_progress.setFixedHeight(6)
    window.gameplay_progress.setVisible(False)
    gp.add(window.gameplay_progress)
    window.lbl_gameplay_status = Hint("")
    window.lbl_gameplay_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
    gp.add(window.lbl_gameplay_status)
    window.btn_gameplay_reset_all = QPushButton("Reset all boundaries")
    window.btn_gameplay_reset_all.setObjectName("LinkButton")
    window.btn_gameplay_reset_all.setCursor(Qt.PointingHandCursor)
    window.btn_gameplay_reset_all.setToolTip("Restore every kept game to the boundaries found by the pipeline")
    gp.add(window.btn_gameplay_reset_all)
    gp.add(window.hero_groups_panel)

    window.analysis_scroll = _scroll_page(window.page_analysis, [mode, ai, stats, gp], name="InspectorScroll")


