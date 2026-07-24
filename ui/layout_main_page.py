from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .ui_helpers import field, hint, section_title


def build_main_page(window) -> None:
    """Populate window.page_main (Edit tab inspector)."""
    main_l = QVBoxLayout(window.page_main)
    main_l.setContentsMargins(4, 4, 4, 4)
    main_l.setSpacing(10)

    window.page_main.setStyleSheet(
        "QWidget { font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Bahnschrift', 'Arial'; background: transparent; }"
    )

    root_card = QFrame()
    root_card.setObjectName("Card")
    root_card.setAttribute(Qt.WA_StyledBackground, True)
    root_l = QVBoxLayout(root_card)
    root_l.setContentsMargins(12, 12, 12, 12)
    root_l.setSpacing(12)

    def block_frame(
        title: str,
        desc: str = "",
        *,
        badge: QLabel | None = None,
        right_widget: QWidget | None = None,
        title_tooltip: str | None = None,
    ) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("InspectorBlock")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        head = QWidget()
        head_l = QHBoxLayout(head)
        head_l.setContentsMargins(0, 0, 0, 0)
        head_l.setSpacing(8)

        title_lbl = section_title(title)
        title_lbl.setObjectName("InspectorBlockTitle")
        if title_tooltip:
            title_lbl.setToolTip(title_tooltip)
        head_l.addWidget(title_lbl, stretch=0)

        head_l.addStretch(1)

        if badge is not None:
            head_l.addWidget(badge, stretch=0)
        if right_widget is not None:
            head_l.addWidget(right_widget, stretch=0)

        lay.addWidget(head)
        if desc:
            desc_lbl = hint(desc)
            desc_lbl.setObjectName("FieldHint")
            lay.addWidget(desc_lbl)
        return frame, lay

    class _ResponsiveRow(QWidget):
        def __init__(self, items: list[tuple[QWidget, int]], breakpoint: int = 430):
            super().__init__()
            self._bp = int(max(220, breakpoint))
            self._lay = QBoxLayout(QBoxLayout.LeftToRight, self)
            self._lay.setContentsMargins(0, 0, 0, 0)
            self._lay.setSpacing(12)
            for w, stretch in items:
                self._lay.addWidget(w, stretch=stretch)
            self._apply_direction()

        def _apply_direction(self) -> None:
            vertical = self.width() > 0 and self.width() < self._bp
            self._lay.setDirection(QBoxLayout.TopToBottom if vertical else QBoxLayout.LeftToRight)
            self._lay.setSpacing(8 if vertical else 12)

        def resizeEvent(self, event):
            self._apply_direction()
            return super().resizeEvent(event)

    def row_two(left: QWidget, right: QWidget) -> QWidget:
        return _ResponsiveRow([(left, 1), (right, 1)], breakpoint=430)

    def make_badge(text: str, kind: str = "info") -> QLabel:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setObjectName("Badge")
        lbl.setProperty("kind", kind)
        return lbl

    # -------------------------
    # Preset
    # -------------------------
    if not hasattr(window, "btn_preset_save_as"):
        window.btn_preset_save_as = QPushButton("Save as...")
    if not hasattr(window, "btn_preset_manage"):
        window.btn_preset_manage = QPushButton("Manage")
    if not hasattr(window, "lbl_preset_meta"):
        window.lbl_preset_meta = QLabel("Select a preset to preview its tuning profile.")
        window.lbl_preset_meta.setObjectName("SubtleHint")
        window.lbl_preset_meta.setWordWrap(True)
    if not hasattr(window, "lbl_preset_state"):
        window.lbl_preset_state = make_badge("Manual", "muted")
    else:
        try:
            window.lbl_preset_state.setAlignment(Qt.AlignCenter)
        except Exception:
            pass

    preset_block, preset_l = block_frame(
        "Preset",
        "Reusable settings profiles for common editing styles.",
        badge=window.lbl_preset_state,
        title_tooltip="Choose a saved setup or store your current settings for later use.",
    )

    window.preset_combo.setMinimumWidth(0)
    window.preset_combo.setMinimumContentsLength(1)
    window.preset_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    window.preset_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    window.preset_combo.setToolTip("Select a saved preset.")
    preset_l.addWidget(window.preset_combo)

    window.lbl_preset_meta.setObjectName("PresetMeta")
    window.lbl_preset_meta.setWordWrap(True)
    preset_l.addWidget(window.lbl_preset_meta)

    actions = QWidget()
    actions_l = QHBoxLayout(actions)
    actions_l.setContentsMargins(0, 2, 0, 0)
    actions_l.setSpacing(8)
    for btn in (window.btn_preset_save, window.btn_preset_save_as, window.btn_preset_manage):
        try:
            btn.setMinimumHeight(34)
        except Exception:
            pass
        actions_l.addWidget(btn, stretch=1)
    preset_l.addWidget(actions)
    window.section_preset = preset_block
    root_l.addWidget(preset_block)

    # -------------------------
    # Analysis mode
    # -------------------------
    analysis_block, analysis_l = block_frame(
        "Analysis mode",
        "Classic is faster. AI gives cleaner speech cuts.",
        title_tooltip="Choose how cuts are detected.",
    )

    mode_row = QWidget()
    mode_l = QHBoxLayout(mode_row)
    mode_l.setContentsMargins(0, 0, 0, 0)
    mode_l.setSpacing(10)
    lbl_classic = QLabel("Classic")
    lbl_ai = QLabel("AI")
    lbl_classic.setCursor(Qt.PointingHandCursor)
    lbl_ai.setCursor(Qt.PointingHandCursor)
    lbl_classic.setToolTip("Use fast classic analysis.")
    lbl_ai.setToolTip("Use AI for cleaner speech detection.")
    lbl_classic.mousePressEvent = lambda _e: window.analysis_mode_toggle.setChecked(False)
    lbl_ai.mousePressEvent = lambda _e: window.analysis_mode_toggle.setChecked(True)

    switch_hit = QWidget()
    switch_hit_l = QHBoxLayout(switch_hit)
    switch_hit_l.setContentsMargins(6, 4, 6, 4)
    switch_hit_l.setSpacing(0)
    switch_hit_l.addWidget(window.analysis_mode_toggle, alignment=Qt.AlignCenter)
    switch_hit.setCursor(Qt.PointingHandCursor)
    switch_hit.setToolTip("Switch between Classic and AI analysis.")
    switch_hit.mousePressEvent = lambda _e: window.analysis_mode_toggle.toggle()

    mode_l.addWidget(lbl_classic, stretch=0)
    mode_l.addWidget(switch_hit, stretch=0)
    mode_l.addWidget(lbl_ai, stretch=0)
    mode_l.addStretch(1)
    analysis_l.addWidget(mode_row)

    # Keep this block compact; spare vertical space should go to the AI tuning
    # panel (or Advanced when visible), not to the mode switch row.
    analysis_block.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
    mode_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    switch_hit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    if hasattr(window, "analysis_mode_toggle") and not window.analysis_mode_toggle.toolTip():
        window.analysis_mode_toggle.setToolTip("Switch between Classic and AI analysis.")

    window.section_analysis_mode = analysis_block
    root_l.addWidget(analysis_block)

    # -------------------------
    # AI options panel
    # -------------------------
    if hasattr(window, "ai_options_panel"):
        window.ai_options_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        opt_l = QVBoxLayout(window.ai_options_panel)
        opt_l.setContentsMargins(12, 12, 12, 12)
        opt_l.setSpacing(8)
        opt_l.addWidget(section_title("AI cut tuning"))
        opt_l.addWidget(hint("Adjust aggressiveness, min speech, and merge gap before processing."))

        aggr_wrap = QWidget()
        aggr_l = QVBoxLayout(aggr_wrap)
        aggr_l.setContentsMargins(0, 0, 0, 0)
        aggr_l.setSpacing(4)
        aggr_l.addWidget(section_title("Aggressiveness"))
        aggr_l.addWidget(window.ai_aggr_slider)
        opt_l.addWidget(aggr_wrap)

        row_opts = _ResponsiveRow(
            [
                (field("Min speech", "Ignore very short speech bursts.", window.ai_min_speech_ms), 1),
                (field("Merge gap", "Merge nearby speech segments.", window.ai_merge_gap_ms), 1),
                (field("Speakers", "Optional expected speaker count.", window.ai_expected_speakers), 1),
            ],
            breakpoint=620,
        )
        opt_l.addWidget(row_opts)

        btn_row = QWidget()
        btn_row_l = QHBoxLayout(btn_row)
        btn_row_l.setContentsMargins(0, 0, 0, 0)
        btn_row_l.setSpacing(8)
        btn_row_l.addWidget(window.btn_ai_process)
        btn_row_l.addWidget(window.btn_ai_reprocess)
        opt_l.addWidget(btn_row)
        opt_l.addStretch(1)

        root_l.addWidget(window.ai_options_panel, stretch=1)

    # -------------------------
    # AI stats panel
    # -------------------------
    if hasattr(window, "ai_stats_panel"):
        ai_l = QVBoxLayout(window.ai_stats_panel)
        ai_l.setContentsMargins(12, 12, 12, 12)
        ai_l.setSpacing(8)
        ai_l.addWidget(section_title("AI stats"))
        ai_l.addWidget(hint("Speaker and voice distribution after AI analysis."))

        def stat_box(title: str, value_lbl: QLabel) -> QWidget:
            box = QFrame()
            box.setObjectName("StatMiniCard")
            box_l = QVBoxLayout(box)
            box_l.setContentsMargins(10, 8, 10, 8)
            box_l.setSpacing(2)
            t = QLabel(title)
            t.setObjectName("FieldHint")
            value_lbl.setObjectName("StatMiniValue")
            box_l.addWidget(t)
            box_l.addWidget(value_lbl)
            return box

        row_stats = QWidget()
        row_l = QHBoxLayout(row_stats)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(8)
        row_l.addWidget(stat_box("Speakers", window.ai_stats_speakers), stretch=1)
        row_l.addWidget(stat_box("Voice", window.ai_stats_voice), stretch=1)
        row_l.addWidget(stat_box("Noise", window.ai_stats_noise), stretch=1)
        ai_l.addWidget(row_stats)

        total_row = QWidget()
        total_l = QHBoxLayout(total_row)
        total_l.setContentsMargins(0, 0, 0, 0)
        total_l.setSpacing(6)
        total_l.addWidget(hint("Speech total"))
        total_l.addWidget(window.ai_stats_total)
        total_l.addStretch(1)
        ai_l.addWidget(total_row)
        ai_l.addWidget(window.ai_stats_speaker_list)
        ai_l.addWidget(window.ai_stats_timeline)
        root_l.addWidget(window.ai_stats_panel)

    # -------------------------
    # Cut intensity
    # -------------------------
    if not hasattr(window, "lbl_intensity_value"):
        window.lbl_intensity_value = make_badge("45%", "info")

    window.section_intensity = QFrame()
    window.section_intensity.setObjectName("InspectorBlock")
    intensity_l = QVBoxLayout(window.section_intensity)
    intensity_l.setContentsMargins(12, 12, 12, 12)
    intensity_l.setSpacing(8)

    intensity_head = QWidget()
    intensity_head_l = QHBoxLayout(intensity_head)
    intensity_head_l.setContentsMargins(0, 0, 0, 0)
    intensity_head_l.setSpacing(8)
    intensity_title = section_title("Cut intensity")
    intensity_title.setObjectName("InspectorBlockTitle")
    intensity_title.setToolTip("Controls how aggressive the automatic cutting is.")
    intensity_head_l.addWidget(intensity_title)
    intensity_head_l.addStretch(1)
    intensity_head_l.addWidget(window.lbl_intensity_value)
    intensity_l.addWidget(intensity_head)
    intensity_l.addWidget(hint("Lower = more cuts. Higher = tighter keeps and fewer edits."))
    intensity_l.addWidget(window.slider_precision)

    intensity_meta = QWidget()
    intensity_meta_l = QHBoxLayout(intensity_meta)
    intensity_meta_l.setContentsMargins(0, 0, 0, 0)
    intensity_meta_l.setSpacing(8)
    window.lbl_precision.setObjectName("StatusNote")
    intensity_meta_l.addWidget(window.lbl_precision, stretch=0)
    intensity_meta_l.addStretch(1)
    intensity_l.addWidget(intensity_meta)
    window.slider_precision.setToolTip("Adjust how aggressive the cut detection should be.")
    window.lbl_precision.setToolTip("A descriptive label for the current cut intensity.")
    root_l.addWidget(window.section_intensity)

    # -------------------------
    # Threshold + advanced
    # -------------------------
    if not hasattr(window, "lbl_threshold_semantic"):
        window.lbl_threshold_semantic = make_badge("Balanced", "info")

    window.section_threshold = QFrame()
    window.section_threshold.setObjectName("InspectorBlock")
    threshold_l = QVBoxLayout(window.section_threshold)
    threshold_l.setContentsMargins(12, 12, 12, 12)
    threshold_l.setSpacing(8)

    threshold_head = QWidget()
    threshold_head_l = QHBoxLayout(threshold_head)
    threshold_head_l.setContentsMargins(0, 0, 0, 0)
    threshold_head_l.setSpacing(8)
    threshold_title = section_title("Voice threshold")
    threshold_title.setObjectName("InspectorBlockTitle")
    threshold_title.setToolTip("Sets the loudness level considered as voice.")
    threshold_head_l.addWidget(threshold_title)
    threshold_head_l.addStretch(1)
    threshold_head_l.addWidget(window.lbl_threshold_semantic)
    threshold_l.addWidget(threshold_head)
    threshold_l.addWidget(hint("Lower keeps quieter speech. Higher skips more low-volume content."))

    # Compact threshold controls: keep meter + value on a single row so the
    # section stays readable even at the minimum window width.
    if hasattr(window, "threshold_meter"):
        try:
            window.threshold_meter.setFixedHeight(70)
        except Exception:
            pass

    thr_controls = QWidget()
    thr_controls_l = QBoxLayout(QBoxLayout.LeftToRight, thr_controls)
    thr_controls_l.setContentsMargins(0, 0, 0, 0)
    thr_controls_l.setSpacing(8)
    window.threshold_pct.setFixedWidth(96)
    thr_controls_l.addWidget(window.threshold_meter, stretch=0, alignment=Qt.AlignTop)
    thr_controls_l.addWidget(window.threshold_pct, stretch=0, alignment=Qt.AlignTop)
    thr_note = QLabel("Fine-tune before analyzing or export preview.")
    thr_note.setObjectName("FieldHint")
    thr_note.setWordWrap(True)
    thr_controls_l.addWidget(thr_note, stretch=1)
    threshold_l.addWidget(thr_controls)
    window.threshold_note = thr_note
    window.threshold_controls_row = thr_controls
    window.threshold_controls_layout = thr_controls_l

    window.threshold_pct.setToolTip("The silence threshold used for cut detection.")
    window.threshold_meter.setToolTip("Visual meter for the current voice threshold.")
    root_l.addWidget(window.section_threshold)

    # -------------------------
    # Advanced header
    # -------------------------
    if not hasattr(window, "lbl_adv_state_badge"):
        window.lbl_adv_state_badge = make_badge("Default", "muted")

    adv_block = QFrame()
    adv_block.setObjectName("InspectorBlock")
    adv_outer = QVBoxLayout(adv_block)
    adv_outer.setContentsMargins(12, 12, 12, 12)
    adv_outer.setSpacing(8)

    window.adv_header.setObjectName("AdvHeader")
    window.btn_adv_toggle.setObjectName("AdvToggle")
    window.btn_adv_toggle.setFixedSize(34, 34)

    adv_hdr = QBoxLayout(QBoxLayout.LeftToRight, window.adv_header)
    adv_hdr.setContentsMargins(6, 4, 6, 4)
    adv_hdr.setSpacing(8)
    adv_hdr.addWidget(window.btn_adv_toggle)

    title_wrap = QVBoxLayout()
    title_wrap.setContentsMargins(0, 0, 0, 0)
    title_wrap.setSpacing(1)
    window.lbl_adv_title.setObjectName("InspectorBlockTitle")
    window.lbl_adv_hint.setObjectName("FieldHint")
    window.lbl_adv_hint.setText("Hidden by default. Expand only when you need fine control.")
    title_wrap.addWidget(window.lbl_adv_title)
    title_wrap.addWidget(window.lbl_adv_hint)
    adv_hdr.addLayout(title_wrap, stretch=1)
    adv_hdr.addWidget(window.lbl_adv_state_badge, stretch=0, alignment=Qt.AlignRight | Qt.AlignVCenter)
    window.adv_header.setToolTip("Expand for fine-grained control. These settings are also stored in presets.")
    window.adv_header_layout = adv_hdr
    adv_outer.addWidget(window.adv_header)

    # -------------------------
    # Advanced panel content (collapsible sections)
    # -------------------------
    window.pre_pad_s.setMaximumWidth(160)
    window.post_pad_s.setMaximumWidth(160)
    window.min_cut_s.setMaximumWidth(160)
    window.gain_db.setMaximumWidth(160)
    window.attack_ms.setMaximumWidth(160)
    window.release_ms.setMaximumWidth(160)
    window.merge_pauses_ms.setMaximumWidth(160)
    window.lufs_target.setMaximumWidth(160)
    window.smoothing_mode.setMaximumWidth(160)

    window._adv_section_badges = {}
    window._adv_section_toggles = {}
    window._adv_section_contents = {}
    window._adv_section_header_layouts = []

    def _toggle_section(key: str) -> None:
        t = window._adv_section_toggles.get(key)
        c = window._adv_section_contents.get(key)
        if t is None or c is None:
            return
        open_now = bool(t.isChecked())
        c.setVisible(open_now)
        t.setArrowType(Qt.DownArrow if open_now else Qt.RightArrow)

    def _make_adv_section(
        key: str,
        title: str,
        subtitle: str,
        content: QWidget,
        *,
        reset_cb=None,
        default_open: bool = False,
    ) -> QFrame:
        frame = QFrame()
        frame.setObjectName("AdvancedSection")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        header = QFrame()
        header.setObjectName("AdvancedSectionHeader")
        hdr_l = QBoxLayout(QBoxLayout.LeftToRight, header)
        hdr_l.setContentsMargins(6, 4, 6, 4)
        hdr_l.setSpacing(8)

        toggle = QToolButton()
        toggle.setObjectName("AdvSectionToggle")
        toggle.setCheckable(True)
        toggle.setChecked(bool(default_open))
        toggle.setAutoRaise(True)
        toggle.setArrowType(Qt.DownArrow if default_open else Qt.RightArrow)
        toggle.setToolTip(f"Show/hide {title.lower()} settings")
        window._adv_section_toggles[key] = toggle

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(1)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("AdvancedSectionTitle")
        subtitle_lbl = QLabel(subtitle)
        subtitle_lbl.setObjectName("FieldHint")
        subtitle_lbl.setWordWrap(True)
        title_col.addWidget(title_lbl)
        title_col.addWidget(subtitle_lbl)

        badge = make_badge("Default", "muted")
        badge.setObjectName("AdvancedSectionBadge")
        window._adv_section_badges[key] = badge

        hdr_l.addWidget(toggle)
        hdr_l.addLayout(title_col, stretch=1)
        hdr_l.addWidget(badge)
        window._adv_section_header_layouts.append(hdr_l)

        if reset_cb is not None:
            btn_reset = QPushButton("Reset section")
            btn_reset.setObjectName("SmallSecondary")
            btn_reset.setMinimumHeight(28)
            btn_reset.clicked.connect(reset_cb)
            hdr_l.addWidget(btn_reset)

        header.mousePressEvent = lambda _e, k=key: (
            window._adv_section_toggles[k].toggle(),
            _toggle_section(k),
        )
        toggle.toggled.connect(lambda _v, k=key: _toggle_section(k))
        lay.addWidget(header)

        content_wrap = QWidget()
        content_wrap_l = QVBoxLayout(content_wrap)
        content_wrap_l.setContentsMargins(4, 2, 4, 2)
        content_wrap_l.setSpacing(10)
        content_wrap_l.addWidget(content)
        content_wrap.setVisible(bool(default_open))
        window._adv_section_contents[key] = content_wrap
        lay.addWidget(content_wrap)
        return frame

    def _after_section_reset():
        if hasattr(window, "_refresh_advanced_ui_state"):
            try:
                window._refresh_advanced_ui_state()
            except Exception:
                pass

    # Padding / timing
    padding_content = QWidget()
    padding_l = QVBoxLayout(padding_content)
    padding_l.setContentsMargins(0, 0, 0, 0)
    padding_l.setSpacing(10)
    padding_l.addWidget(
        row_two(
            field("Pre-padding", "Keep a little audio before each voice segment.", window.pre_pad_s),
            field("Post-padding", "Keep a little audio after each voice segment.", window.post_pad_s),
        )
    )
    padding_l.addWidget(field("Minimum cut duration", "Ignore tiny cuts to keep edits natural.", window.min_cut_s))
    window.pre_pad_s.setToolTip("Adds a small buffer before speech starts.")
    window.post_pad_s.setToolTip("Adds a small buffer after speech ends.")
    window.min_cut_s.setToolTip("Prevents very short cuts from being created.")

    def _reset_padding():
        window.pre_pad_s.setValue(window.pre_pad_s_default)
        window.post_pad_s.setValue(window.post_pad_s_default)
        window.min_cut_s.setValue(window.min_cut_s_default)
        _after_section_reset()

    # Detection
    detection_content = QWidget()
    detection_l = QVBoxLayout(detection_content)
    detection_l.setContentsMargins(0, 0, 0, 0)
    detection_l.setSpacing(10)
    detection_l.addWidget(
        row_two(
            field("Attack", "How quickly voice is confirmed.", window.attack_ms),
            field("Release", "How quickly silence is confirmed.", window.release_ms),
        )
    )
    detection_l.addWidget(window.gain_affects_detection)
    window.attack_ms.setToolTip("Shorter = faster detection, longer = more stable.")
    window.release_ms.setToolTip("Shorter = faster silence detection, longer = smoother.")
    window.gain_affects_detection.setToolTip("When ON, export gain also affects detection (usually keep OFF).")

    def _reset_detection():
        window.attack_ms.setValue(window.attack_ms_default)
        window.release_ms.setValue(window.release_ms_default)
        window.gain_affects_detection.setChecked(window.gain_affects_detection_default)
        _after_section_reset()

    # Smoothing
    smoothing_content = QWidget()
    smoothing_l = QVBoxLayout(smoothing_content)
    smoothing_l.setContentsMargins(0, 0, 0, 0)
    smoothing_l.setSpacing(10)
    smoothing_l.addWidget(field("Smoothing", "Reduces rapid cut flicker.", window.smoothing_mode))
    window.smoothing_mode.setToolTip("Higher smoothing = fewer rapid cut changes.")

    def _reset_smoothing():
        window.smoothing_mode.setCurrentText(window.smoothing_mode_default)
        _after_section_reset()

    # Merging
    merging_content = QWidget()
    merging_l = QVBoxLayout(merging_content)
    merging_l.setContentsMargins(0, 0, 0, 0)
    merging_l.setSpacing(10)
    merging_l.addWidget(field("Merge short pauses", "Treat brief pauses as continuous speech.", window.merge_pauses_ms))
    window.merge_pauses_ms.setToolTip("Treat short pauses as continuous speech.")

    def _reset_merging():
        window.merge_pauses_ms.setValue(window.merge_pauses_ms_default)
        _after_section_reset()

    # Audio
    audio_content = QWidget()
    audio_l = QVBoxLayout(audio_content)
    audio_l.setContentsMargins(0, 0, 0, 0)
    audio_l.setSpacing(10)
    audio_l.addWidget(field("Audio gain", "Boost or reduce volume during export only.", window.gain_db))
    audio_l.addWidget(window.normalize_lufs)

    limiter_row = QWidget()
    limiter_l = QHBoxLayout(limiter_row)
    limiter_l.setContentsMargins(0, 0, 0, 0)
    limiter_l.setSpacing(6)
    limiter_l.addWidget(window.limiter, stretch=1)

    audio_l.addWidget(
        row_two(
            field("LUFS target", "Consistent loudness target (export only).", window.lufs_target),
            limiter_row,
        )
    )
    window.gain_db.setToolTip("Adjust export volume without redoing detection.")
    window.normalize_lufs.setToolTip("Auto-level loudness for consistent volume.")
    window.lufs_target.setToolTip("Typical target: -14 LUFS (YouTube).")
    window.limiter.setToolTip("Prevents clipping after loudness changes.")

    def _reset_audio():
        window.gain_db.setValue(window.gain_db_default)
        window.normalize_lufs.setChecked(window.normalize_lufs_default)
        window.lufs_target.setValue(window.lufs_target_default)
        window.limiter.setChecked(window.limiter_default)
        _after_section_reset()

    adv_card = QFrame()
    adv_card.setObjectName("CardInner")
    adv_lay = QVBoxLayout(adv_card)
    adv_lay.setContentsMargins(12, 12, 12, 12)
    adv_lay.setSpacing(8)

    window.adv_block_timing = _make_adv_section(
        "padding",
        "Padding",
        "Keep context around detected speech.",
        padding_content,
        reset_cb=_reset_padding,
        default_open=True,
    )
    window.adv_block_detection = _make_adv_section(
        "detection",
        "Detection",
        "How quickly speech/silence is recognized.",
        detection_content,
        reset_cb=_reset_detection,
        default_open=True,
    )
    window.adv_block_smoothing = _make_adv_section(
        "smoothing",
        "Smoothing",
        "Reduce flicker in fast-changing audio.",
        smoothing_content,
        reset_cb=_reset_smoothing,
        default_open=False,
    )
    window.adv_block_merging = _make_adv_section(
        "merging",
        "Merging",
        "Control how short pauses are handled.",
        merging_content,
        reset_cb=_reset_merging,
        default_open=False,
    )
    window.adv_block_audio = _make_adv_section(
        "audio",
        "Audio",
        "Export loudness and limiting options.",
        audio_content,
        reset_cb=_reset_audio,
        default_open=False,
    )

    for sec in (
        window.adv_block_timing,
        window.adv_block_detection,
        window.adv_block_smoothing,
        window.adv_block_merging,
        window.adv_block_audio,
    ):
        adv_lay.addWidget(sec)
    adv_lay.addStretch(1)

    window.advanced_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    adv_wrap = QVBoxLayout(window.advanced_panel)
    adv_wrap.setContentsMargins(0, 0, 0, 0)
    adv_wrap.setSpacing(0)

    adv_scroll = QScrollArea()
    adv_scroll.setObjectName("AdvancedScroll")
    adv_scroll.setWidgetResizable(True)
    adv_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    adv_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    adv_scroll.setFrameShape(QFrame.NoFrame)
    adv_scroll.setWidget(adv_card)
    adv_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    try:
        adv_scroll.viewport().setAutoFillBackground(False)
        adv_scroll.viewport().setStyleSheet("background: transparent;")
    except Exception:
        pass
    window.advanced_scroll = adv_scroll

    adv_wrap.addWidget(adv_scroll, stretch=1)
    adv_outer.addWidget(window.advanced_panel, stretch=1)
    window.section_advanced_root = adv_block
    root_l.addWidget(adv_block, stretch=1)

    # Widgets hidden while Advanced is open (focus mode for readability).
    focus_hide_widgets = [
        getattr(window, "section_analysis_mode", None),
        getattr(window, "ai_options_panel", None),
        getattr(window, "ai_stats_panel", None),
        getattr(window, "section_intensity", None),
        getattr(window, "section_threshold", None),
    ]
    window._advanced_focus_hidden_widgets = [w for w in focus_hide_widgets if isinstance(w, QWidget)]

    content = QWidget()
    content.setAttribute(Qt.WA_StyledBackground, True)
    content.setStyleSheet("background: transparent;")
    content_l = QVBoxLayout(content)
    content_l.setContentsMargins(0, 0, 0, 0)
    content_l.setSpacing(8)
    content_l.addWidget(root_card)
    content_l.addStretch(1)

    scroll = QScrollArea()
    scroll.setObjectName("MainScroll")
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(content)
    scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    try:
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet("background: transparent;")
    except Exception:
        pass
    window.main_scroll = scroll

    main_l.addWidget(scroll, stretch=1)
