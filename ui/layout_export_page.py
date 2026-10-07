from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .ui_helpers import hint, section_title


def build_export_page(window) -> None:
    """Populate window.page_export."""
    exp_l = QVBoxLayout(window.page_export)
    exp_l.setContentsMargins(4, 4, 4, 4)
    exp_l.setSpacing(0)

    window.page_export.setStyleSheet(
        "QWidget { font-family: 'Segoe UI Variable Text', 'Segoe UI', 'Bahnschrift', 'Arial'; background: transparent; }"
    )

    card_exp = QFrame()
    card_exp.setObjectName("Card")
    card_exp.setAttribute(Qt.WA_StyledBackground, True)
    root_l = QVBoxLayout(card_exp)
    root_l.setContentsMargins(12, 12, 12, 12)
    root_l.setSpacing(12)

    def block_frame(
        title: str,
        desc: str = "",
        *,
        right_widget: QWidget | None = None,
        tooltip: str | None = None,
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
        lbl = section_title(title)
        lbl.setObjectName("InspectorBlockTitle")
        if tooltip:
            lbl.setToolTip(tooltip)
        head_l.addWidget(lbl)
        head_l.addStretch(1)
        if right_widget is not None:
            head_l.addWidget(right_widget)
        lay.addWidget(head)
        if desc:
            desc_lbl = hint(desc)
            desc_lbl.setObjectName("FieldHint")
            lay.addWidget(desc_lbl)
        return frame, lay

    def field_widget(title: str, control: QWidget, tooltip: str = "") -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        label = section_title(title)
        label.setObjectName("FieldTitle")
        if tooltip:
            label.setToolTip(tooltip)
            control.setToolTip(tooltip)
        lay.addWidget(label)
        lay.addWidget(control)
        return wrap

    # -------------------------
    # Quality & Compatibility
    # -------------------------
    qc_block, qc_l = block_frame(
        "Quality & Compatibility",
        "Start from a profile, then customize only what the delivery needs.",
        right_widget=window.btn_export_defaults,
    )

    qc_l.addWidget(field_widget("Profile", window.export_preset_combo))

    basic_grid_widget = QWidget()
    basic_grid = QGridLayout(basic_grid_widget)
    basic_grid.setContentsMargins(0, 0, 0, 0)
    basic_grid.setHorizontalSpacing(10)
    basic_grid.setVerticalSpacing(10)
    basic_grid.setColumnStretch(0, 1)
    basic_grid.setColumnStretch(1, 1)
    basic_grid.addWidget(
        field_widget(
            "Codec",
            window.codec_combo,
            "H.264 is most compatible; HEVC and AV1 produce smaller files.",
        ),
        0,
        0,
    )
    basic_grid.addWidget(field_widget("Format", window.container_combo), 0, 1)
    basic_grid.addWidget(field_widget("Resolution", window.resolution_combo), 1, 0)
    basic_grid.addWidget(field_widget("Frame rate", window.fps_combo), 1, 1)
    basic_grid.addWidget(field_widget("Video quality", window.video_quality_combo), 2, 0)
    basic_grid.addWidget(field_widget("Output", window.output_mode_combo), 2, 1)
    qc_l.addWidget(basic_grid_widget)

    window.export_compatibility_label.setObjectName("FieldHint")
    qc_l.addWidget(window.export_compatibility_label)

    options_toggle = QToolButton()
    options_toggle.setObjectName("AdvSectionToggle")
    options_toggle.setText("Advanced output settings")
    options_toggle.setCheckable(True)
    options_toggle.setChecked(False)
    options_toggle.setArrowType(Qt.RightArrow)
    options_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    window.export_options_toggle = options_toggle
    qc_l.addWidget(options_toggle)

    options_advanced = QWidget()
    options_l = QVBoxLayout(options_advanced)
    options_l.setContentsMargins(0, 4, 0, 0)
    options_l.setSpacing(10)

    transform_grid_widget = QWidget()
    transform_grid = QGridLayout(transform_grid_widget)
    transform_grid.setContentsMargins(0, 0, 0, 0)
    transform_grid.setHorizontalSpacing(10)
    transform_grid.setVerticalSpacing(10)
    transform_grid.setColumnStretch(0, 1)
    transform_grid.setColumnStretch(1, 1)
    transform_grid.addWidget(field_widget("Aspect", window.aspect_combo), 0, 0)
    transform_grid.addWidget(field_widget("FPS mode", window.fps_mode_combo), 0, 1)
    transform_grid.addWidget(field_widget("Pixel depth", window.pixel_depth_combo), 1, 0)
    transform_grid.addWidget(field_widget("Color", window.color_mode_combo), 1, 1)
    transform_grid.addWidget(window.no_upscale_cb, 2, 0, 1, 2)
    options_l.addWidget(transform_grid_widget)

    rate_grid_widget = QWidget()
    rate_grid = QGridLayout(rate_grid_widget)
    rate_grid.setContentsMargins(0, 0, 0, 0)
    rate_grid.setHorizontalSpacing(10)
    rate_grid.setVerticalSpacing(10)
    rate_grid.setColumnStretch(0, 1)
    rate_grid.setColumnStretch(1, 1)
    rate_grid.addWidget(field_widget("Rate control", window.rate_control_combo), 0, 0)
    window.export_bitrate_wrap = field_widget("Video bitrate", window.video_bitrate_spin)
    rate_grid.addWidget(window.export_bitrate_wrap, 0, 1)
    window.export_target_size_wrap = field_widget("Approximate size", window.target_size_spin)
    rate_grid.addWidget(window.export_target_size_wrap, 1, 0)
    window.export_custom_quality_wrap = field_widget("CRF / QP", window.custom_quality_spin)
    rate_grid.addWidget(window.export_custom_quality_wrap, 1, 1)
    rate_grid.addWidget(window.two_pass_cb, 2, 0, 1, 2)
    options_l.addWidget(rate_grid_widget)

    audio_grid_widget = QWidget()
    audio_grid = QGridLayout(audio_grid_widget)
    audio_grid.setContentsMargins(0, 0, 0, 0)
    audio_grid.setHorizontalSpacing(10)
    audio_grid.setVerticalSpacing(10)
    audio_grid.setColumnStretch(0, 1)
    audio_grid.setColumnStretch(1, 1)
    audio_grid.addWidget(field_widget("Audio codec", window.audio_codec_combo), 0, 0)
    audio_grid.addWidget(field_widget("Audio bitrate", window.audio_bitrate_combo), 0, 1)
    audio_grid.addWidget(field_widget("Sample rate", window.sample_rate_combo), 1, 0)
    audio_grid.addWidget(field_widget("Channels", window.channels_combo), 1, 1)
    options_l.addWidget(audio_grid_widget)

    pipeline_grid_widget = QWidget()
    pipeline_grid = QGridLayout(pipeline_grid_widget)
    pipeline_grid.setContentsMargins(0, 0, 0, 0)
    pipeline_grid.setHorizontalSpacing(10)
    pipeline_grid.setVerticalSpacing(10)
    pipeline_grid.setColumnStretch(0, 1)
    pipeline_grid.setColumnStretch(1, 1)
    pipeline_grid.addWidget(
        field_widget(
            "Export method",
            window.export_method_combo,
            "Auto chooses a strategy. Frame-changing options require accurate re-encoding.",
        ),
        0,
        0,
    )
    pipeline_grid.addWidget(
        field_widget("Cut border quality", window.cut_quality_combo),
        0,
        1,
    )
    options_l.addWidget(pipeline_grid_widget)

    window.export_range_wrap = QWidget()
    range_l = QGridLayout(window.export_range_wrap)
    range_l.setContentsMargins(0, 0, 0, 0)
    range_l.setHorizontalSpacing(10)
    range_l.setColumnStretch(0, 1)
    range_l.setColumnStretch(1, 1)
    range_l.addWidget(field_widget("Range start", window.range_start_spin), 0, 0)
    range_l.addWidget(field_widget("Range end", window.range_end_spin), 0, 1)
    options_l.addWidget(window.export_range_wrap)

    options_advanced.setVisible(False)
    window.export_options_advanced = options_advanced

    def _sync_options_advanced(_checked: bool) -> None:
        open_now = bool(options_toggle.isChecked())
        options_toggle.setArrowType(Qt.DownArrow if open_now else Qt.RightArrow)
        options_advanced.setVisible(open_now)

    options_toggle.toggled.connect(_sync_options_advanced)
    qc_l.addWidget(options_advanced)
    root_l.addWidget(qc_block)

    # -------------------------
    # Performance
    # -------------------------
    perf_toggle = QToolButton()
    perf_toggle.setObjectName("AdvSectionToggle")
    perf_toggle.setCheckable(True)
    perf_toggle.setChecked(False)
    perf_toggle.setArrowType(Qt.RightArrow)
    perf_toggle.setToolTip("Show advanced performance tuning")
    window.export_perf_toggle = perf_toggle

    perf_block, perf_l = block_frame(
        "Performance",
        "Hardware decode is safe for most users. Workers/chunks are power-user tuning.",
        right_widget=perf_toggle,
    )

    window.hwaccel_cb.setText("Use hardware decode (d3d11va)")
    window.hwaccel_cb.setToolTip("Uses GPU decoding to speed up export (if supported).")
    perf_l.addWidget(window.hwaccel_cb)

    advisor_wrap = QWidget()
    advisor_l = QVBoxLayout(advisor_wrap)
    advisor_l.setContentsMargins(0, 4, 0, 0)
    advisor_l.setSpacing(6)
    advisor_title = section_title("Settings advisor")
    advisor_title.setObjectName("FieldTitle")
    advisor_l.addWidget(advisor_title)
    advisor_l.addWidget(hint("Optional benchmark. Review the result, then apply it explicitly."))
    window.btn_export_advisor.setToolTip(
        "Benchmark high-quality encoders on the current video and recommend codec, workers, and chunks."
    )
    window.export_advisor_result.setObjectName("FieldHint")
    advisor_actions = QWidget()
    advisor_actions_l = QHBoxLayout(advisor_actions)
    advisor_actions_l.setContentsMargins(0, 0, 0, 0)
    advisor_actions_l.setSpacing(8)
    advisor_actions_l.addWidget(window.btn_export_advisor, stretch=1)
    advisor_actions_l.addWidget(window.btn_export_advisor_apply)
    advisor_l.addWidget(advisor_actions)
    advisor_l.addWidget(window.export_advisor_result)
    perf_l.addWidget(advisor_wrap)

    perf_adv = QWidget()
    perf_adv_l = QVBoxLayout(perf_adv)
    perf_adv_l.setContentsMargins(0, 0, 0, 0)
    perf_adv_l.setSpacing(10)

    workers_wrap = QWidget()
    workers_l = QVBoxLayout(workers_wrap)
    workers_l.setContentsMargins(0, 0, 0, 0)
    workers_l.setSpacing(4)
    workers_title = section_title("Parallel workers")
    workers_title.setObjectName("FieldTitle")
    workers_l.addWidget(workers_title)
    workers_l.addWidget(hint("0 = Auto"))
    window.parallel_workers_spin.setMaximumWidth(160)
    workers_l.addWidget(window.parallel_workers_spin)
    window.parallel_workers_spin.setToolTip(
        "Auto chooses based on CPU/GPU. Increase only if your system stays stable during export."
    )
    perf_adv_l.addWidget(workers_wrap)

    chunks_wrap = QWidget()
    chunks_l = QVBoxLayout(chunks_wrap)
    chunks_l.setContentsMargins(0, 0, 0, 0)
    chunks_l.setSpacing(4)
    chunks_title = section_title("Chunk count")
    chunks_title.setObjectName("FieldTitle")
    chunks_l.addWidget(chunks_title)
    chunks_l.addWidget(hint("0 = Auto"))
    window.chunk_count_spin.setMaximumWidth(160)
    chunks_l.addWidget(window.chunk_count_spin)
    window.chunk_count_spin.setToolTip(
        "Auto chooses based on duration/segments/workers. Higher values create more chunks."
    )
    perf_adv_l.addWidget(chunks_wrap)

    window.btn_clear_export_cache.setObjectName("SmallSecondary")
    window.btn_clear_export_cache.setToolTip(
        "Delete cached render chunks and keyframe indexes. Future exports will rebuild them."
    )
    perf_adv_l.addWidget(window.btn_clear_export_cache)

    perf_adv.setVisible(False)
    window.export_perf_advanced = perf_adv

    def _sync_perf_adv(_checked: bool) -> None:
        open_now = bool(window.export_perf_toggle.isChecked())
        window.export_perf_toggle.setArrowType(Qt.DownArrow if open_now else Qt.RightArrow)
        window.export_perf_advanced.setVisible(open_now)

    perf_toggle.toggled.connect(_sync_perf_adv)
    perf_l.addWidget(perf_adv)
    root_l.addWidget(perf_block)

    # -------------------------
    # Actions
    # -------------------------
    actions_block, actions_l = block_frame(
        "Export",
        "The main action follows the selected format and output mode. EDL remains optional.",
    )

    window.btn_export.setToolTip("Start rendering with the selected output settings.")
    window.btn_export_edl.setToolTip("Export an EDL file to use in other editors.")
    window.export_status.setObjectName("ExportStatus")

    actions_l.addWidget(window.btn_export)
    actions_l.addWidget(window.export_progress)
    actions_l.addWidget(window.export_status)

    edl_row = QWidget()
    edl_row_l = QHBoxLayout(edl_row)
    edl_row_l.setContentsMargins(0, 2, 0, 0)
    edl_row_l.setSpacing(8)
    edl_row_l.addWidget(window.btn_export_edl, stretch=1)
    actions_l.addWidget(edl_row)
    root_l.addWidget(actions_block)

    # -------------------------
    # Logs accordion
    # -------------------------
    if not hasattr(window, "btn_export_copy_logs"):
        window.btn_export_copy_logs = QPushButton("Copy logs")
    if not hasattr(window, "btn_export_open_logs_folder"):
        window.btn_export_open_logs_folder = QPushButton("Open logs folder")
    window.btn_export_copy_logs.setObjectName("SmallSecondary")
    window.btn_export_open_logs_folder.setObjectName("SmallSecondary")

    logs_actions = QWidget()
    logs_actions_l = QHBoxLayout(logs_actions)
    logs_actions_l.setContentsMargins(0, 0, 0, 0)
    logs_actions_l.setSpacing(6)
    logs_actions_l.addWidget(window.btn_export_copy_logs)
    logs_actions_l.addStretch(1)
    logs_actions_l.addWidget(window.btn_export_details)

    logs_block, logs_l = block_frame(
        "Export logs",
        "Detailed diagnostics are hidden by default. Open only when troubleshooting.",
        right_widget=None,
    )
    window.btn_export_details.setToolTip("Show detailed export progress and diagnostics.")
    window.btn_export_details.setMinimumHeight(30)
    logs_l.addWidget(logs_actions)
    logs_l.addWidget(window.export_details)
    root_l.addWidget(logs_block)
    window.export_logs_block = logs_block

    # -------------------------
    # OG Statistics (Export tab only)
    # -------------------------
    if hasattr(window, "web_stats_full") and window.web_stats_full is not None:
        stats_block, stats_l = block_frame(
            "Statistics",
            "Original stats panel restored here to fill the export tab and keep review metrics visible.",
        )
        try:
            window.web_stats_full.setContentsMargins(0, 0, 0, 0)
            # Keep full stats readable without forcing oversized right-panel scroll
            # on laptop screens.
            window.web_stats_full.setMinimumHeight(420)
            window.web_stats_full.setMaximumHeight(720)
        except Exception:
            pass
        stats_l.addWidget(window.web_stats_full)
        root_l.addWidget(stats_block)
        window.export_stats_block = stats_block
    else:
        window.export_stats_block = None

    # Scrollable content
    content = QWidget()
    content.setAttribute(Qt.WA_StyledBackground, True)
    content.setStyleSheet("background: transparent;")
    content_l = QVBoxLayout(content)
    content_l.setContentsMargins(0, 0, 0, 0)
    content_l.setSpacing(8)
    content_l.addWidget(card_exp)
    content_l.addStretch(1)

    scroll = QScrollArea()
    scroll.setObjectName("ExportScroll")
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

    exp_l.addWidget(scroll, stretch=1)
