from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
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

    # -------------------------
    # Quality & Compatibility
    # -------------------------
    qc_block, qc_l = block_frame(
        "Quality & Compatibility",
        "Choose codec, export method, and cut-border quality.",
    )

    codec_wrap = QWidget()
    codec_l = QVBoxLayout(codec_wrap)
    codec_l.setContentsMargins(0, 0, 0, 0)
    codec_l.setSpacing(4)
    codec_title = section_title("Codec")
    codec_title.setObjectName("FieldTitle")
    codec_title.setToolTip("Choose the video format/encoder for export.")
    codec_l.addWidget(codec_title)
    codec_l.addWidget(window.codec_combo)
    window.codec_combo.setToolTip("H.264 is most compatible; HEVC/AV1 are smaller but need newer players.")
    qc_l.addWidget(codec_wrap)

    method_wrap = QWidget()
    method_l = QVBoxLayout(method_wrap)
    method_l.setContentsMargins(0, 0, 0, 0)
    method_l.setSpacing(4)
    method_title = section_title("Export method")
    method_title.setObjectName("FieldTitle")
    method_title.setToolTip("Pick the export pipeline based on speed vs. accuracy.")
    method_l.addWidget(method_title)
    method_l.addWidget(window.export_method_combo)
    window.export_method_combo.setToolTip(
        "Auto selects a strategy. Smart render copies middle GOPs. Accurate is slower but precise."
    )
    qc_l.addWidget(method_wrap)

    cutq_wrap = QWidget()
    cutq_l = QVBoxLayout(cutq_wrap)
    cutq_l.setContentsMargins(0, 0, 0, 0)
    cutq_l.setSpacing(4)
    cutq_title = section_title("Cut border quality")
    cutq_title.setObjectName("FieldTitle")
    cutq_title.setToolTip("Quality policy for non-keyframe cut borders (re-encoded regions only).")
    cutq_l.addWidget(cutq_title)
    cutq_l.addWidget(window.cut_quality_combo)
    window.cut_quality_combo.setToolTip(
        "Balanced keeps HQ on short cut borders. Faster modes reduce cut-border re-encode quality."
    )
    qc_l.addWidget(cutq_wrap)
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
        "Export MP4 is the main action. EDL is optional for external editors.",
    )

    window.btn_export.setToolTip("Start rendering the final MP4 file.")
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
