"""Inspector Export page: summary · output settings · Export CTA · advanced · performance · log."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.inspector import Hint, PropertyRow, Section


def _sub_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("RowLabelStrong")
    lbl.setContentsMargins(0, 6, 0, 0)
    return lbl


def build_export_page(window) -> None:
    """Populate window.page_export."""
    # ---- summary ------------------------------------------------------------------------------
    summary = Section("Summary")
    card = QFrame()
    card.setObjectName("SummaryCard")
    card_l = QHBoxLayout(card)
    card_l.setContentsMargins(14, 10, 14, 10)
    card_l.setSpacing(18)
    window.lbl_export_out = QLabel("–")
    window.lbl_export_out.setObjectName("StatValue")
    out_col = QVBoxLayout()
    out_col.setSpacing(0)
    out_col.addWidget(window.lbl_export_out)
    cap = QLabel("Output length")
    cap.setObjectName("StatCaption")
    out_col.addWidget(cap)
    card_l.addLayout(out_col)
    window.lbl_export_meta = QLabel("")
    window.lbl_export_meta.setObjectName("RowHint")
    window.lbl_export_meta.setWordWrap(True)
    card_l.addWidget(window.lbl_export_meta, 1)
    summary.add(card)

    # ---- output -------------------------------------------------------------------------------
    window.btn_export_defaults.setObjectName("LinkButton")
    window.btn_export_defaults.setText("Reset")
    window.btn_export_defaults.setCursor(Qt.PointingHandCursor)
    window.btn_export_defaults.setToolTip("Restore the default export settings")
    output = Section("Output", action=window.btn_export_defaults)
    output.add_row("Profile", window.export_preset_combo,
                   tooltip="Starting point; changing any setting below switches to Custom")
    output.add_row("Format", window.container_combo)
    output.add_row("Resolution", window.resolution_combo)
    output.add_row("Frame rate", window.fps_combo)
    output.add_row("Quality", window.video_quality_combo)
    output.add_row("Output", window.output_mode_combo, tooltip="Single file, one file per clip, audio/video only "
                                                               "or the selected range")
    folder = QWidget()
    folder_l = QHBoxLayout(folder)
    folder_l.setContentsMargins(0, 0, 0, 0)
    folder_l.setSpacing(6)
    window.lbl_export_folder = QLabel("Source folder")
    window.lbl_export_folder.setObjectName("Muted")
    window.lbl_export_folder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    window.btn_export_folder = QPushButton("Change…")
    window.btn_export_folder.setObjectName("SecondarySmall")
    window.btn_export_folder.setCursor(Qt.PointingHandCursor)
    window.btn_export_folder.setToolTip("Default folder offered when you export")
    window.btn_export_folder.clicked.connect(window._choose_export_folder)
    folder_l.addWidget(window.lbl_export_folder, 1)
    folder_l.addWidget(window.btn_export_folder)
    output.add_row("Location", folder)
    window.export_compatibility_label.setObjectName("RowHint")
    output.add(window.export_compatibility_label)

    # ---- export action: sticky footer, always visible under the scrolling settings -------------
    action = QFrame()
    action.setObjectName("InspectorFooter")
    action.setAttribute(Qt.WA_StyledBackground, True)
    action_l = QVBoxLayout(action)
    action_l.setContentsMargins(16, 12, 16, 12)
    action_l.setSpacing(8)
    window.btn_export.setObjectName("PrimaryCTA")
    window.btn_export.setText("Export video")
    window.btn_export.setCursor(Qt.PointingHandCursor)
    window.btn_export.setIconSize(QSize(16, 16))
    window.btn_export.setToolTip("Render the edited video with these settings  (Ctrl+E)")
    action_l.addWidget(window.btn_export)
    window.export_progress.setTextVisible(False)
    window.export_progress.setFixedHeight(6)
    window.export_progress.setVisible(False)
    action_l.addWidget(window.export_progress)
    window.export_status.setObjectName("ExportStatus")
    window.export_status.setWordWrap(True)
    action_l.addWidget(window.export_status)
    window.btn_export_edl.setObjectName("LinkButton")
    window.btn_export_edl.setText("Export EDL for another editor…")
    window.btn_export_edl.setCursor(Qt.PointingHandCursor)
    window.btn_export_edl.setToolTip("Edit decision list (CMX 3600) for DaVinci Resolve, Premiere, …")
    action_l.addWidget(window.btn_export_edl)

    # ---- advanced -----------------------------------------------------------------------------
    adv = Section("Advanced", collapsible=True, expanded=False)
    adv.add(_sub_title("Video"))
    adv.add_row("Codec", window.codec_combo, tooltip="H.264 is the most compatible; HEVC and AV1 make smaller files")
    adv.add_row("Aspect", window.aspect_combo)
    adv.add_row("FPS mode", window.fps_mode_combo)
    adv.add_row("Pixel depth", window.pixel_depth_combo)
    adv.add_row("Color", window.color_mode_combo)
    adv.add(window.no_upscale_cb)
    adv.add(_sub_title("Rate control"))
    adv.add_row("Mode", window.rate_control_combo)
    window.export_bitrate_wrap = adv.add_row("Bitrate", window.video_bitrate_spin)
    window.export_target_size_wrap = adv.add_row("File size", window.target_size_spin)
    window.export_custom_quality_wrap = adv.add_row("CRF / QP", window.custom_quality_spin)
    adv.add(window.two_pass_cb)
    adv.add(_sub_title("Audio"))
    adv.add_row("Codec", window.audio_codec_combo)
    adv.add_row("Bitrate", window.audio_bitrate_combo)
    adv.add_row("Sample rate", window.sample_rate_combo)
    adv.add_row("Channels", window.channels_combo)
    adv.add(_sub_title("Rendering"))
    adv.add_row("Method", window.export_method_combo,
                tooltip="Auto chooses a strategy. Frame-changing options require accurate re-encoding.")
    adv.add_row("Cut edges", window.cut_quality_combo, tooltip="Re-encode quality around cut points")
    window.export_range_wrap = QWidget()
    rl = QVBoxLayout(window.export_range_wrap)
    rl.setContentsMargins(0, 0, 0, 0)
    rl.setSpacing(8)
    rl.addWidget(PropertyRow("Range start", window.range_start_spin))
    rl.addWidget(PropertyRow("Range end", window.range_end_spin))
    adv.add(window.export_range_wrap)
    window.export_options_toggle = adv.chevron
    window.export_options_advanced = adv.body

    # ---- performance --------------------------------------------------------------------------
    perf = Section("Performance", collapsible=True, expanded=False)
    window.hwaccel_cb.setText("Hardware decoding (d3d11va)")
    window.hwaccel_cb.setToolTip("Use the GPU to decode the source while exporting (if supported)")
    perf.add(window.hwaccel_cb)
    for spin in (window.parallel_workers_spin, window.chunk_count_spin):
        spin.setMaximumWidth(120)
    perf.add_row("Workers", window.parallel_workers_spin, tooltip="Parallel encoders (Auto = based on CPU/GPU)")
    perf.add_row("Chunks", window.chunk_count_spin, tooltip="Number of render chunks (Auto = based on length)")
    perf.add(_sub_title("Settings advisor"))
    perf.add(Hint("Optional benchmark on this video. It recommends codec, workers and chunks; "
                  "nothing changes until you apply it."))
    window.btn_export_advisor.setText("Run benchmark")
    window.btn_export_advisor.setObjectName("SecondarySmall")
    window.btn_export_advisor_apply.setObjectName("SecondarySmall")
    adv_row = QWidget()
    adv_l = QHBoxLayout(adv_row)
    adv_l.setContentsMargins(0, 0, 0, 0)
    adv_l.setSpacing(6)
    adv_l.addWidget(window.btn_export_advisor)
    adv_l.addWidget(window.btn_export_advisor_apply)
    adv_l.addStretch(1)
    perf.add(adv_row)
    window.export_advisor_result.setObjectName("RowHint")
    perf.add(window.export_advisor_result)
    window.btn_clear_export_cache.setObjectName("LinkButton")
    window.btn_clear_export_cache.setText("Clear render cache")
    window.btn_clear_export_cache.setToolTip("Delete cached render chunks and keyframe indexes")
    perf.add(window.btn_clear_export_cache)
    window.export_perf_toggle = perf.chevron
    window.export_perf_advanced = perf.body

    # ---- log ----------------------------------------------------------------------------------
    log = Section("Log", collapsible=True, expanded=False)
    logs_actions = QWidget()
    la = QHBoxLayout(logs_actions)
    la.setContentsMargins(0, 0, 0, 0)
    la.setSpacing(6)
    for b in (window.btn_export_copy_logs, window.btn_export_open_logs_folder):
        b.setObjectName("SecondarySmall")
        la.addWidget(b)
    la.addStretch(1)
    log.add(logs_actions)
    window.export_details.setVisible(True)
    log.add(window.export_details)
    window.btn_export_details.setVisible(False)

    def _sync_log(expanded: bool) -> None:
        window.btn_export_details.setChecked(bool(expanded))
        window._toggle_export_details()

    log.toggled.connect(_sync_log)
    window.export_log_section = log
    window.export_logs_block = log
    window.export_stats_block = None

    content = QWidget()
    col = QVBoxLayout(content)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(0)
    for s in (summary, output, adv, perf, log):
        col.addWidget(s)
    col.addStretch(1)
    scroll = QScrollArea()
    scroll.setObjectName("InspectorScroll")
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(content)
    lay = QVBoxLayout(window.page_export)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    lay.addWidget(scroll, 1)
    lay.addWidget(action)
    window.export_footer = action
    window.export_scroll = scroll
