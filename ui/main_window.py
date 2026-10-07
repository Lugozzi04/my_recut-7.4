from __future__ import annotations

import json
import os
import sys
import time
import uuid
import threading
import copy
import shutil
import hashlib
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from PySide6.QtCore import (
    Qt,
    QThread,
    QUrl,
    QTimer,
    Slot,
    QSettings,
    QStandardPaths,
    QLocale,
)
from PySide6.QtGui import QColor, QIcon, QDesktopServices
from PySide6.QtGui import QKeySequence, QShortcut, QCursor
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QFileDialog, QHBoxLayout,
    QToolButton, QPushButton, QLabel, QSlider, QSpinBox,
    QProgressBar, QComboBox, QCheckBox, QMenu,
    QStackedWidget, QApplication, QSizePolicy, QScrollArea, QPlainTextEdit,
)

from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

from utils.ffmpeg import (
    ensure_ffmpeg,
    FFmpegNotFound,
    ffprobe_duration_seconds,
    ffprobe_keyframes,
    clear_keyframe_cache,
)
from utils.codec_detection import resolve_video_codec
from utils.app_version import app_version
from utils.crash_handler import crash_logs_dir
from utils.diagnostics import create_support_bundle
from utils.redaction import redact_secrets
from utils.i18n import normalize_language, text as ui_text
from utils.runtime_paths import project_root, resource_path, config_root, logs_root
from utils.timefmt import fmt_hms

from analysis.audio_analyzer import AnalyzeWorker
from analysis.ai_backend import AiAnalyzeWorker, ai_dependency_status
from analysis.ai_pipeline import AiPipelineConfig
from analysis.intensity_map import map_intensity
from analysis.cut_engine import Segment, invert_to_keeps, merge_overlaps
from analysis.classic import compute_classic_cuts, threshold_amp_to_pct, threshold_pct_to_amp
from core.presets import PresetRepository, normalize_preset_cfg, default_presets_catalog
from automation.models import PipelineJob, PipelineState

from widgets.timeline import TimelineWidget
from widgets.threshold_meter import ThresholdMeter
from widgets.seek_slider import SeekJumpSlider
from export.advisor import ExportAdvisorWorker, ExportRecommendation
from export.exporter import ExportWorker
from export.settings import ExportSettings

from .frame_video_widget import FrameVideoWidget
from .icons import IconSet
from .theme import apply_theme
from .pro_messagebox import QMessageBox, get_int as pro_get_int, get_text as pro_get_text
from .layout_left import build_left_panel
from .layout_right import build_right_panel
from .main_window_components import (
    GlobalWheelBlocker,
    MiniTimelineWidget,
    NoWheelDoubleSpinBox,
    NoWheelSpinBox,
    ToggleSwitch,
    WebUiBridge,
    WheelOnlyIfFocusedFilter,
    qt_is_valid,
)
from .twitch_integration import TwitchIntegration, normalize_twitch_client_id
from core import Project, ProjectSession, Track, TrackState, Clip, Media
from core.project_file import (
    PROJECT_FORMAT,
    PROJECT_VERSION,
    ProjectFormatError,
    make_payload_portable,
    normalize_project_payload,
    relink_items_in_directory,
    resolve_project_items,
)

from PySide6.QtWidgets import QBoxLayout, QFrame, QSplitter, QVBoxLayout, QHBoxLayout

from PySide6.QtWebChannel import QWebChannel

from PySide6.QtWebEngineWidgets import QWebEngineView

class MainWindow(QMainWindow):
    @property
    def project(self) -> Project:
        return self._project_session.project

    @project.setter
    def project(self, value: Project) -> None:
        self._project_session.project = value

    @property
    def _tracks(self) -> list[TrackState]:
        return self._project_session.track_states

    @_tracks.setter
    def _tracks(self, value: list[TrackState]) -> None:
        self._project_session.track_states = value

    def __init__(self):
        super().__init__()
        self._project_session = ProjectSession.create_default("Untitled")
        self._project_root = project_root()
        try:
            saved_language = QSettings("Auto Cutter", "Auto Cutter").value("ui_language", "")
        except Exception:
            saved_language = ""
        self._ui_language = normalize_language(str(saved_language or QLocale.system().name()))
        self._session_log_path: Path | None = None
        self._session_log_lock = threading.Lock()
        self._session_log_started_ts = time.time()
        self._session_log_enabled = True
        self._init_session_logging()

        self.setWindowTitle("Auto Cutter - Voice Cuts")
        self.resize(1400, 860)
        # Responsive minimum size for laptop screens. The previous 1240x760
        # forced clipping/overflow on 1366x768 devices with DPI scaling.
        self.setMinimumSize(980, 620)
        self.setAcceptDrops(True)
        logo = self._project_root / "icons" / "logo" / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))

        # -----------------------------
        # Project tracks
        # -----------------------------
        self._tracks: list[TrackState] = [TrackState()]
        self._active_track_index: int = 0
        self._analysis_threads: dict[int, QThread] = {}
        self._analysis_workers: dict[int, AnalyzeWorker] = {}
        self._analysis_expected_path: dict[int, str] = {}
        self._analysis_job_ids: dict[int, int] = {}
        self._analysis_targets: dict[int, TrackState] = {}
        self._analysis_progress: dict[int, int] = {}
        self._analysis_job_seq: int = 0
        self._orphan_analysis_threads: list[QThread] = []
        self._orphan_analysis_workers: list[AnalyzeWorker] = []
        self._ai_threads: dict[int, QThread] = {}
        self._ai_workers: dict[int, object] = {}
        self._ai_expected_path: dict[int, str] = {}
        self._orphan_ai_threads: list[QThread] = []
        self._orphan_ai_workers: list[object] = []
        self._ai_processing: bool = False
        self._export_processing: bool = False
        self._export_abort_requested: bool = False
        self._workspace_resetting: bool = False
        self._pending_workspace_reset: bool = False
        self._pending_workspace_reset_since: float = 0.0
        self._pending_workspace_reset_force_applied: bool = False
        self._pending_workspace_reset_force_after_s: float = 0.9
        self._global_duration: float = 0.0
        self._warm_cache_queue: list[str] = []
        self._warm_cache_thread: threading.Thread | None = None
        self._warm_cache_lock = threading.Lock()
        self._warm_cache_stop = threading.Event()
        self._warm_cache_busy: bool = False
        # Qt Multimedia on Windows can occasionally leave the dedicated audio
        # player silent when the source is assigned during startup auto-restore
        # (before the first event-loop cycle). We schedule a deferred re-sync.
        self._startup_restore_audio_resync_pending: bool = False
        self._startup_restore_audio_resync_remaining: int = 0
        # Recent-file auto restore must not run inside __init__: loading media
        # too early can leave video/audio backends in a broken state on Windows.
        self._startup_auto_restore_pending: bool = True
        self._startup_auto_restore_started: bool = False
        self._media_debug_enabled: bool = str(
            os.environ.get("AUTO_CUTTER_MEDIA_DEBUG", "0")
        ).strip().lower() in ("1", "true", "yes", "on")
        self._media_debug_last_ts: float = 0.0
        self._media_debug_sync_focus_until: float = 0.0
        self._in_auto_restore_session: bool = False
        self._crash_recovery_offer_pending: bool = False
        self._crash_autosave_enabled: bool = True
        self._crash_autosave_interval_s: int = 45
        self._crash_autosave_timer: QTimer | None = None
        self._last_crash_autosave_sig: str = ""

        # -----------------------------
        # Project model (NLE-style)
        # -----------------------------
        self.project = Project.create_default("Untitled")
        self._project_file_path: Path | None = None
        self._offline_project_items: list[dict[str, Any]] = []
        self._timeline_track_map: list[dict] = []
        if self._tracks:
            self._ensure_audio_track_for_state(self._tracks[0])
            self._ensure_video_track_for_state(self._tracks[0])

        # -----------------------------
        # FFmpeg paths
        # -----------------------------
        self.ffmpeg_path: Optional[str] = None
        self.ffprobe_path: Optional[str] = None

        # -----------------------------
        # State (analysis)
        # -----------------------------
        self.input_path: Optional[str] = None
        self.duration: float = 0.0
        self.rms: Optional[np.ndarray] = None
        self.hop_s: float = 0.03

        self.cuts: list[Segment] = []
        self.keeps: list[Segment] = []
        self.cuts_enabled: bool = False

        # manual edits
        self.manual_cuts: list[Segment] = []
        self._pending_cut_start: float | None = None
        self._pending_cut_end: float | None = None

        # undo / redo
        self.suppressed_cuts: list[Segment] = []
        self._undo_stack: list[tuple[list[Segment], list[Segment]]] = []
        self._redo_stack: list[tuple[list[Segment], list[Segment]]] = []
        self._edit_undo_stack: list[dict] = []
        self._edit_redo_stack: list[dict] = []
        self._edit_history_limit: int = 120

        # skip (preview)
        self.skip_on: bool = True
        self._skip_guard = False
        self._user_scrubbing = False
        # guard against async QMediaPlayer position jitter after seeks
        self._pending_seek_target: float | None = None
        self._pending_seek_ts: float = 0.0
        self._pending_seek_tolerance_s: float = 0.6
        self._pending_seek_timeout_s: float = 0.9

        # WebUI zoom visual (0..max)
        self._web_zoom_steps = 0
        self._web_zoom_steps_max = 0
        self._ready_dot_state = "idle"
        self._export_dot_state = "idle"

        # edit tool mode
        self.tool_mode = "select"

        # -----------------------------
        # New advanced parameters (core-ready)
        # If UI widgets exist later, bind them; for now defaults are safe.
        # -----------------------------
        self.attack_ms_default = 120
        self.release_ms_default = 250
        self.smoothing_mode_default = "Medium"  # Off/Low/Medium/High (mapping later)
        self.merge_pauses_ms_default = 300

        self.normalize_lufs_default = False
        self.lufs_target_default = -14.0
        self.limiter_default = False

        # Export cut-quality policy defaults (recommended profile)
        self.cut_hq_enabled_default = True
        self.cut_hq_max_seconds_default = 6.0

        # -----------------------------
        # Icons / Theme
        # -----------------------------
        self.icons = IconSet(self._project_root / "icons")
        self._theme_light = False  # set by apply_theme_from_system()

        # -----------------------------
        # Player (single video track + parallel audio tracks)
        # -----------------------------
        self.video_widget = FrameVideoWidget(self._open_path, self.open_file)

        self.video_player = QMediaPlayer(self)
        self.video_audio = QAudioOutput(self)
        try:
            self.video_audio.setMuted(False)
        except Exception:
            pass
        self.video_player.setAudioOutput(self.video_audio)
        self.video_player.setVideoOutput(self.video_widget.video_sink)
        self.video_player.positionChanged.connect(self._on_position_changed)
        self.video_player.durationChanged.connect(self._on_player_duration_changed)
        self.video_player.playbackStateChanged.connect(self._on_playback_state_changed)
        try:
            self.video_player.mediaStatusChanged.connect(self._on_video_status_changed)
        except Exception:
            pass
        try:
            self.video_player.errorOccurred.connect(self._on_video_error)
        except Exception:
            pass

        # keep backward-compatible alias
        self.player = self.video_player

        # audio via dedicated player (MVP: active track only)
        self.audio_player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        try:
            self.audio_output.setMuted(False)
        except Exception:
            pass
        self.audio_player.setAudioOutput(self.audio_output)
        try:
            self.audio_player.mediaStatusChanged.connect(self._on_audio_status_changed)
        except Exception:
            pass
        try:
            self.audio_player.errorOccurred.connect(self._on_audio_error)
        except Exception:
            pass

        # preview volume (0..100%), default max = original source level
        self._preview_volume_pct = 100

        # mute video audio to avoid double audio; active track drives audio output
        try:
            self.video_audio.setMuted(True)
        except Exception:
            pass
        try:
            self.audio_output.setVolume(1.0)
        except Exception:
            pass
        try:
            self.video_audio.setVolume(1.0)
        except Exception:
            pass

        # legacy placeholder
        self.audio_engine = None

        # video concat state
        self._video_segments: list[dict[str, object]] = []
        self._video_segment_index: int = 0
        self._video_concat_duration: float = 0.0
        self._play_requested: bool = False
        # audio segments (active track)
        self._audio_segments: list[dict[str, object]] = []
        self._audio_segment_index: int = -1
        self._audio_segments_track_idx: int | None = None
        self._segment_seek_guard_until: float = 0.0
        self._audio_seek_guard_until: float = 0.0
        self._audio_requested_source_path: str = ""
        self._audio_requested_source_ts: float = 0.0
        self._audio_last_media_status = None
        self._video_last_media_status = None
        self._play_guard_until_ts: float = 0.0
        self._play_guard_reason: str = ""
        self._segment_guard_src_in: float = 0.0
        self._segment_guard_start: float = 0.0
        # debug (segment sync)
        self._debug_segments: bool = str(
            os.environ.get("AUTO_CUTTER_DEBUG_SEGMENTS", os.environ.get("RECUT_DEBUG_SEGMENTS", "0"))
        ).strip() not in ("", "0", "false", "False")
        self._debug_last_ts: float = 0.0
        self._debug_min_interval: float = 0.15
        # UI throttling during playback (keep preview smooth)
        self._ui_update_interval_s: float = 1.0 / 30.0
        self._last_ui_update_s: float = 0.0

        # -----------------------------
        # LEFT widgets (required by layout_left)
        # -----------------------------
        self.btn_open = QPushButton("Add Video")
        self.btn_open.setCheckable(True)
        self.btn_open.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.btn_open.setMaximumWidth(180)  # opzionale, ma consigliato
        self.btn_open.setFixedHeight(36)
        self.btn_open.setMinimumWidth(140)
        self.btn_open.setObjectName("Action")

        self.chk_skip = QCheckBox("Auto-skip cut sections during preview")
        self.chk_skip.setChecked(True)

        self.step_bar = QLabel("")
        self.step_bar.setTextFormat(Qt.RichText)
        self._set_stage("idle")

        self.lbl_file = QLabel('Drop a video on the preview area or click "Add Video".')
        self.lbl_time = QLabel("0:00 / 0:00")

        self.seek = SeekJumpSlider(Qt.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.sliderPressed.connect(self._on_seek_press)
        self.seek.sliderReleased.connect(self._on_seek_release)
        self.seek.valueChanged.connect(self._on_seek_value_changed)

        self.preview_volume_slider = QSlider(Qt.Vertical)
        self.preview_volume_slider.setRange(0, 100)
        self.preview_volume_slider.setSingleStep(5)
        self.preview_volume_slider.setPageStep(10)
        self.preview_volume_slider.setValue(100)
        self.preview_volume_slider.setToolTip("Preview volume")
        self.preview_volume_slider.valueChanged.connect(self._on_preview_volume_changed)

        self.preview_volume_label = QLabel("VOL")
        self.preview_volume_label.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self.preview_volume_value = QLabel("100%")
        self.preview_volume_value.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        self._apply_preview_volume(100)

        self.btn_play = QToolButton()
        self.btn_play.setToolTip("Play / Pause")

        self.btn_zoom_out = QToolButton()
        self.btn_zoom_out.setToolTip("Zoom out")

        self.btn_zoom_in = QToolButton()
        self.btn_zoom_in.setToolTip("Zoom in")

        self.btn_zoom_reset = QToolButton()
        self.btn_zoom_reset.setToolTip("Reset zoom")

        self.btn_layout = QToolButton()
        self.btn_layout.setText("Layout")
        self.btn_layout.setToolTip("Switch layout (swap video/timeline and filters)")
        self.btn_layout.setObjectName("SegmentLayout")

        # Cut tool (manual 2-click range) + split tool (clip split)
        self.btn_cut_tool = QToolButton()
        self.btn_cut_tool.setCheckable(True)
        self.btn_cut_tool.setToolTip("Cut tool (C) - click start, then end")
        self.btn_cut_tool.setFixedSize(32, 32)

        self.btn_split = QToolButton()
        self.btn_split.setCheckable(True)
        self.btn_split.setToolTip("Split tool (S)")
        self.btn_split.setFixedSize(32, 32)

        self.tool_row = QWidget()
        tool_l = QHBoxLayout(self.tool_row)
        tool_l.setContentsMargins(0, 0, 0, 0)
        tool_l.setSpacing(6)
        tool_l.addWidget(self.btn_cut_tool)
        tool_l.addWidget(self.btn_split)
        tool_l.addStretch(1)

        self.timeline = TimelineWidget()
        # WebUI zoom tracking (0..max)
        self._web_zoom_steps = 0
        self._web_zoom_steps_max = self._calc_zoom_steps_max()
        self.timeline.setSnapEnabled(bool(getattr(self, "_snap_enabled", False)))
        self.timeline.seekRequested.connect(self._seek_to)
        if hasattr(self.timeline, "trackSelected"):
            self.timeline.trackSelected.connect(self._on_timeline_track_selected)
        if hasattr(self.timeline, "clipMoveRequested"):
            self.timeline.clipMoveRequested.connect(self._on_clip_move_requested)
        if hasattr(self.timeline, "clipSwapRequested"):
            self.timeline.clipSwapRequested.connect(self._on_clip_swap_requested)
        if hasattr(self.timeline, "clipActivated"):
            self.timeline.clipActivated.connect(self._activate_track)
        self.timeline.cutClicked.connect(self._on_cut_clicked)
        self.timeline.cutsSelected.connect(self._on_cuts_selected)
        self.timeline.contextMenuRequested.connect(self._on_timeline_context_menu)

        # scrollable timeline for multi-track projects
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setFrameShape(QFrame.NoFrame)
        self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.timeline_scroll.setWidget(self.timeline)
        # Keep viewport transparent to avoid occasional white artifacts on some GPUs.
        try:
            self.timeline_scroll.setStyleSheet("background: transparent; border: none;")
            self.timeline_scroll.viewport().setAutoFillBackground(False)
            self.timeline_scroll.viewport().setStyleSheet("background: transparent;")
        except Exception:
            pass

        if hasattr(self.timeline, "scrubStarted"):
            self.timeline.scrubStarted.connect(lambda: self._set_user_scrubbing(True))
        if hasattr(self.timeline, "scrubEnded"):
            self.timeline.scrubEnded.connect(lambda: self._set_user_scrubbing(False))

        self.lbl_footer = QLabel("Duration - Output - Cuts")
        
        # Stats labels for right panel (created in layout_right if needed)
        self.stats_duration = None
        self.stats_output = None
        self.stats_cuts = None
        self.stats_kept = None
        self.stats_preset = None

        # -----------------------------
        # RIGHT side: segmented pages (required by layout_right)
        # -----------------------------
        self.pages = QStackedWidget()
        self.page_main = QWidget()
        self.page_export = QWidget()
        self.page_main.setAttribute(Qt.WA_StyledBackground, True)
        self.page_export.setAttribute(Qt.WA_StyledBackground, True)
        self.page_main.setStyleSheet("background: transparent;")
        self.page_export.setStyleSheet("background: transparent;")
        self.pages.addWidget(self.page_main)
        self.pages.addWidget(self.page_export)

        self.seg_main = QToolButton()
        self.seg_main.setText("Edit")
        self.seg_main.setCheckable(True)

        self.seg_export = QToolButton()
        self.seg_export.setText("Export")
        self.seg_export.setCheckable(True)

        # -----------------------------
        # Main page widgets (required by layout_main_page)
        # -----------------------------
        self.analysis_mode_toggle = ToggleSwitch()
        self.analysis_mode_toggle.setChecked(False)
        self.analysis_mode_toggle.setToolTip("Switch between Classic and AI analysis.")
        self.analysis_mode_toggle.toggled.connect(self._on_analysis_mode_toggled)
        self.analysis_mode = "classic"

        # AI stats panel widgets (created in layout_main_page)
        self.ai_options_panel = QFrame()
        self.ai_options_panel.setObjectName("CardInner")
        self.ai_options_panel.setVisible(False)
        self.ai_aggr_slider = QSlider(Qt.Horizontal)
        self.ai_aggr_slider.setRange(0, 100)
        self.ai_aggr_slider.setValue(50)
        self.ai_min_speech_ms = QSpinBox()
        self.ai_min_speech_ms.setRange(50, 2000)
        self.ai_min_speech_ms.setValue(300)
        self.ai_min_speech_ms.setSuffix(" ms")
        self.ai_merge_gap_ms = QSpinBox()
        self.ai_merge_gap_ms.setRange(0, 2000)
        self.ai_merge_gap_ms.setValue(200)
        self.ai_merge_gap_ms.setSuffix(" ms")
        self.ai_expected_speakers = QComboBox()
        self.ai_expected_speakers.addItems(["Auto", "1", "2", "3", "4"])
        self.ai_expected_speakers.setCurrentIndex(0)
        self.btn_ai_process = QPushButton("Process AI")
        self.btn_ai_reprocess = QPushButton("Reprocess AI")
        self.btn_ai_reprocess.setVisible(False)

        self.ai_stats_panel = QFrame()
        self.ai_stats_panel.setObjectName("CardInner")
        self.ai_stats_panel.setVisible(False)
        self.ai_stats_speakers = QLabel("-")
        self.ai_stats_voice = QLabel("-")
        self.ai_stats_noise = QLabel("-")
        self.ai_stats_total = QLabel("-")
        self.ai_stats_speaker_list = QLabel("")
        self.ai_stats_speaker_list.setWordWrap(True)
        self.ai_stats_speaker_list.setObjectName("SubtleHint")
        self.ai_stats_timeline = MiniTimelineWidget()

        self.presets_path = self._user_presets_path()
        self.presets: dict[str, dict] = {}
        self._applying_preset = False
        self._default_preset_name: Optional[str] = None
        self._preset_dirty = False
        self._preset_source_name: Optional[str] = None
        self._adv_open = False
        self._adv_locked = False  # lock UI hidden; keep controls enabled by default
        self._reset_buttons: list[QToolButton] = []
        self._layout_mode = "left"  # left=video left, right=video right
        self._mini_stats_enabled = True
        self._layout_switch_enabled = True
        self._advanced_enabled = True
        self._recent_enabled = False
        self._remember_filters_enabled = False
        self._remember_cuts_enabled = False
        self._pending_restore_filters: Optional[dict] = None
        self._theme_name = "Dark"
        # palette for per-track video colors
        self._video_color_cursor = 0
        self._video_color_palette: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]] = [
            ((70, 140, 90, 130), (90, 170, 110)),   # green
            ((70, 120, 170, 120), (90, 150, 210)),  # blue
            ((180, 120, 60, 130), (210, 150, 90)),  # orange
            ((140, 80, 180, 130), (170, 110, 210)), # purple
            ((180, 70, 70, 130), (210, 100, 100)),  # red
            ((70, 160, 160, 130), (90, 190, 190)),  # teal
            ((170, 160, 70, 130), (200, 190, 90)),  # yellow
            ((160, 80, 120, 130), (190, 110, 150)), # pink
        ]

        self.preset_combo = QComboBox()
        self.btn_preset_save = QPushButton("Save")
        self.btn_preset_save_as = QPushButton("Save as...")
        self.btn_preset_manage = QPushButton("Manage")
        self.btn_preset_delete = QPushButton("Delete")
        self.btn_preset_delete.setEnabled(False)

        self.slider_precision = QSlider(Qt.Horizontal)
        self.slider_precision.setRange(0, 100)
        self.slider_precision.setValue(45)

        self.lbl_precision = QLabel("Natural")

        self.threshold_pct = NoWheelSpinBox()
        self.threshold_pct.setRange(0, 100)
        self.threshold_pct.setValue(45)
        self.threshold_pct.setSuffix("%")

        self.threshold_meter = ThresholdMeter()
        self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
        self._refresh_threshold_ui_state()

        # Advanced header/panel (new UI pieces)
        self.adv_header = QWidget()
        self.btn_adv_toggle = QToolButton()
        self.btn_adv_toggle.setToolTip("Show/hide advanced options")
        # Make the whole Advanced header clickable
        self.adv_header.mousePressEvent = lambda event: self._toggle_advanced()
        

        self.lbl_adv_title = QLabel("Advanced")
        self.lbl_adv_hint = QLabel("Hidden by default. Settings are saved in presets.")
        self.lbl_adv_hint.setObjectName("SubtleHint")
        self.lbl_adv_hint.setWordWrap(True)

        self.btn_adv_lock = QToolButton()
        self.btn_adv_lock.setToolTip("Lock/unlock advanced panel")
        self.btn_adv_lock.setVisible(False)

        self.advanced_panel = QWidget()
        self.advanced_panel.setVisible(False)

        # Existing advanced controls (already present in your layout)
        self.pre_pad_s = NoWheelDoubleSpinBox()
        self.pre_pad_s.setRange(0.0, 3.0)
        self.pre_pad_s.setSingleStep(0.05)
        self.pre_pad_s.setDecimals(2)
        self.pre_pad_s.setValue(0.25)
        self.pre_pad_s.setSuffix(" s")
        self.pre_pad_s_default = 0.25

        self.post_pad_s = NoWheelDoubleSpinBox()
        self.post_pad_s.setRange(0.0, 3.0)
        self.post_pad_s.setSingleStep(0.05)
        self.post_pad_s.setDecimals(2)
        self.post_pad_s.setValue(0.25)
        self.post_pad_s.setSuffix(" s")
        self.post_pad_s_default = 0.25

        self.min_cut_s = NoWheelDoubleSpinBox()
        self.min_cut_s.setRange(0.0, 2.0)
        self.min_cut_s.setSingleStep(0.02)
        self.min_cut_s.setDecimals(2)
        self.min_cut_s.setValue(0.10)
        self.min_cut_s.setSuffix(" s")
        self.min_cut_s_default = 0.10

        self.gain_db = NoWheelDoubleSpinBox()
        self.gain_db.setRange(-24.0, 24.0)
        self.gain_db.setSingleStep(0.10)
        self.gain_db.setDecimals(2)
        self.gain_db.setValue(0.00)
        self.gain_db.setSuffix(" dB")
        self.gain_db_default = 0.00

        self.gain_affects_detection = QCheckBox("Apply gain to voice detection")
        self.gain_affects_detection.setChecked(False)
        self.gain_affects_detection_default = False
        # --- NEW Advanced: detection quality ---
        self.attack_ms = NoWheelSpinBox()
        self.attack_ms.setRange(0, 2000)
        self.attack_ms.setValue(self.attack_ms_default)
        self.attack_ms.setSuffix(" ms")

        self.release_ms = NoWheelSpinBox()
        self.release_ms.setRange(0, 2000)
        self.release_ms.setValue(self.release_ms_default)
        self.release_ms.setSuffix(" ms")

        self.smoothing_mode = QComboBox()
        self.smoothing_mode.addItems(["Off", "Low", "Medium", "High"])
        self.smoothing_mode.setCurrentText(self.smoothing_mode_default)

        self.merge_pauses_ms = NoWheelSpinBox()
        self.merge_pauses_ms.setRange(0, 5000)
        self.merge_pauses_ms.setValue(self.merge_pauses_ms_default)
        self.merge_pauses_ms.setSuffix(" ms")

        # --- NEW Export audio ---
        self.normalize_lufs = QCheckBox("Normalize loudness (LUFS)")
        self.normalize_lufs.setChecked(self.normalize_lufs_default)

        self.lufs_target = NoWheelDoubleSpinBox()
        self.lufs_target.setRange(-30.0, -6.0)
        self.lufs_target.setSingleStep(0.5)
        self.lufs_target.setDecimals(1)
        self.lufs_target.setValue(self.lufs_target_default)
        self.lufs_target.setSuffix(" LUFS")

        self.limiter = QCheckBox("Limiter")
        self.limiter.setChecked(self.limiter_default)
        # -----------------------------
        # Export page widgets (required by layout_export_page)
        # -----------------------------
        self.btn_export = QPushButton("Export video")
        self.btn_export.setObjectName("Primary")
        self.btn_export.setEnabled(False)

        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_progress.setTextVisible(True)
        self.export_progress.setFormat("%p%")

        self.export_status = QLabel("")

        self.btn_export_details = QPushButton("Show logs")
        self.btn_export_details.setCheckable(True)
        self.btn_export_details.setChecked(False)

        self.btn_export_copy_logs = QPushButton("Copy logs")
        self.btn_export_open_logs_folder = QPushButton("Open logs folder")

        self.export_details = QPlainTextEdit()
        self.export_details.setReadOnly(True)
        self.export_details.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.export_details.setMaximumBlockCount(2000)
        self.export_details.setMinimumHeight(160)
        self.export_details.setMaximumHeight(220)
        self.export_details.setVisible(False)
        self._last_export_detail_hint = ""

        self.btn_export_edl = QPushButton("Export EDL")
        self.btn_export_edl.setEnabled(False)

        self.codec_combo = QComboBox()
        self.codec_combo.addItem("Auto (recommended)", "auto")
        self.codec_combo.addItem("H.264 (NVIDIA NVENC)", "h264_nvenc")
        self.codec_combo.addItem("H.264 (Intel Quick Sync)", "h264_qsv")
        self.codec_combo.addItem("H.264 (AMD AMF) - fast & compatible", "h264_amf")
        self.codec_combo.addItem("HEVC/H.265 (AMD AMF) - better quality/size", "hevc_amf")
        self.codec_combo.addItem("HEVC/H.265 (NVIDIA NVENC)", "hevc_nvenc")
        self.codec_combo.addItem("HEVC/H.265 (Intel Quick Sync)", "hevc_qsv")
        self.codec_combo.addItem("AV1 (AMD AMF) - best compression", "av1_amf")
        self.codec_combo.addItem("AV1 (NVIDIA NVENC)", "av1_nvenc")
        self.codec_combo.addItem("AV1 (Intel Quick Sync)", "av1_qsv")
        self.codec_combo.addItem("H.264 (x264 software) - best quality, slower", "libx264")
        self.codec_combo.addItem("HEVC/H.265 (x265 software)", "libx265")
        self.codec_combo.addItem("AV1 (libaom software) - very slow", "libaom-av1")

        self.export_method_combo = QComboBox()
        self.export_method_combo.addItem("Auto", "auto")
        self.export_method_combo.addItem("Fast (chunked parallel)", "chunked_parallel")
        self.export_method_combo.addItem("Smart render (copy + re-encode edges)", "smart_render")
        self.export_method_combo.addItem("Hybrid smart (auto per segment)", "smart_hybrid")
        self.export_method_combo.addItem("Accurate (legacy, slower)", "filter_concat")
        self.export_method_combo.setCurrentIndex(0)

        self.cut_quality_combo = QComboBox()
        self.cut_quality_combo.addItem("Balanced (HQ on cuts <= 6s)", {"enabled": True, "max_seconds": 6.0})
        self.cut_quality_combo.addItem("Higher quality (HQ on cuts <= 12s)", {"enabled": True, "max_seconds": 12.0})
        self.cut_quality_combo.addItem("Faster (HQ on cuts <= 4s)", {"enabled": True, "max_seconds": 4.0})
        self.cut_quality_combo.addItem("Maximum speed (disable cut HQ)", {"enabled": False, "max_seconds": 0.0})
        self.cut_quality_combo.setCurrentIndex(0)

        self.parallel_workers_spin = NoWheelSpinBox()
        self.parallel_workers_spin.setRange(0, 100)
        self.parallel_workers_spin.setSpecialValueText("Auto")
        self.parallel_workers_spin.setValue(0)

        self.chunk_count_spin = NoWheelSpinBox()
        self.chunk_count_spin.setRange(0, 50)
        self.chunk_count_spin.setSpecialValueText("Auto")
        self.chunk_count_spin.setValue(0)

        self.hwaccel_cb = QCheckBox("Use HW decode (d3d11va)")
        self.hwaccel_cb.setChecked(True)

        self.export_preset_combo = QComboBox()
        self.export_preset_combo.addItem("Original - high quality", "original_hq")
        self.export_preset_combo.addItem("Web - high quality", "web_hq")
        self.export_preset_combo.addItem("Compact", "compact")
        self.export_preset_combo.addItem("Master", "master")
        self.export_preset_combo.addItem("Custom", "custom")
        self.btn_export_defaults = QPushButton("Default")

        self.container_combo = QComboBox()
        self.container_combo.addItem("MP4", "mp4")
        self.container_combo.addItem("Matroska (MKV)", "mkv")
        self.container_combo.addItem("QuickTime (MOV)", "mov")
        self.container_combo.addItem("WebM (AV1 + Opus)", "webm")

        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("Single video", "single")
        self.output_mode_combo.addItem("One file per clip", "per_clip")
        self.output_mode_combo.addItem("Audio only", "audio_only")
        self.output_mode_combo.addItem("Video only", "video_only")
        self.output_mode_combo.addItem("Selected timeline range", "selected_range")

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("Original", "source")
        self.resolution_combo.addItem("4K / 2160p", "2160p")
        self.resolution_combo.addItem("1440p", "1440p")
        self.resolution_combo.addItem("Full HD / 1080p", "1080p")
        self.resolution_combo.addItem("HD / 720p", "720p")

        self.aspect_combo = QComboBox()
        self.aspect_combo.addItem("Original", "source")
        self.aspect_combo.addItem("Landscape 16:9", "landscape")
        self.aspect_combo.addItem("Vertical 9:16", "vertical")
        self.aspect_combo.addItem("Square 1:1", "square")
        self.no_upscale_cb = QCheckBox("Do not upscale smaller sources")
        self.no_upscale_cb.setChecked(True)

        self.fps_combo = QComboBox()
        self.fps_combo.addItem("Original", "source")
        for fps_value in (24, 25, 30, 50, 60):
            self.fps_combo.addItem(f"{fps_value} FPS", str(fps_value))
        self.fps_mode_combo = QComboBox()
        self.fps_mode_combo.addItem("Constant frame rate (CFR)", "cfr")
        self.fps_mode_combo.addItem("Preserve variable frame rate (VFR)", "vfr")

        self.video_quality_combo = QComboBox()
        self.video_quality_combo.addItem("Maximum", "maximum")
        self.video_quality_combo.addItem("Very high", "very_high")
        self.video_quality_combo.addItem("High", "high")
        self.video_quality_combo.addItem("Balanced", "balanced")
        self.video_quality_combo.addItem("Compact", "compact")
        self.video_quality_combo.addItem("Custom", "custom")

        self.rate_control_combo = QComboBox()
        self.rate_control_combo.addItem("Constant quality", "quality")
        self.rate_control_combo.addItem("Target bitrate", "bitrate")
        self.rate_control_combo.addItem("Approximate file size", "target_size")
        self.video_bitrate_spin = NoWheelDoubleSpinBox()
        self.video_bitrate_spin.setRange(0.1, 500.0)
        self.video_bitrate_spin.setDecimals(1)
        self.video_bitrate_spin.setSingleStep(1.0)
        self.video_bitrate_spin.setSuffix(" Mbps")
        self.video_bitrate_spin.setValue(18.0)
        self.target_size_spin = NoWheelSpinBox()
        self.target_size_spin.setRange(1, 1_000_000)
        self.target_size_spin.setSuffix(" MB")
        self.target_size_spin.setValue(1500)
        self.custom_quality_spin = NoWheelSpinBox()
        self.custom_quality_spin.setRange(0, 51)
        self.custom_quality_spin.setValue(16)
        self.two_pass_cb = QCheckBox("Two-pass encoding (x264 bitrate modes)")

        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItem("Auto", "auto")
        self.audio_codec_combo.addItem("Copy when possible", "copy")
        self.audio_codec_combo.addItem("AAC", "aac")
        self.audio_codec_combo.addItem("Opus", "opus")
        self.audio_codec_combo.addItem("PCM 24-bit", "pcm_s24le")
        self.audio_bitrate_combo = QComboBox()
        for bitrate in (128, 192, 256, 320):
            self.audio_bitrate_combo.addItem(f"{bitrate} kbps", bitrate)
        self.audio_bitrate_combo.setCurrentIndex(3)
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItem("Original", "source")
        self.sample_rate_combo.addItem("44.1 kHz", "44100")
        self.sample_rate_combo.addItem("48 kHz", "48000")
        self.channels_combo = QComboBox()
        self.channels_combo.addItem("Original", "source")
        self.channels_combo.addItem("Mono", "mono")
        self.channels_combo.addItem("Stereo", "stereo")

        self.pixel_depth_combo = QComboBox()
        self.pixel_depth_combo.addItem("Original / automatic", "source")
        self.pixel_depth_combo.addItem("8-bit", "8")
        self.pixel_depth_combo.addItem("10-bit", "10")
        self.color_mode_combo = QComboBox()
        self.color_mode_combo.addItem("Preserve source metadata", "preserve")
        self.color_mode_combo.addItem("SDR Rec.709", "rec709")
        self.color_mode_combo.addItem("Rec.2020", "rec2020")
        self.color_mode_combo.addItem("Preserve HDR", "hdr_preserve")

        self.range_start_spin = NoWheelDoubleSpinBox()
        self.range_start_spin.setRange(0.0, 86400.0)
        self.range_start_spin.setDecimals(3)
        self.range_start_spin.setSuffix(" s")
        self.range_end_spin = NoWheelDoubleSpinBox()
        self.range_end_spin.setRange(0.0, 86400.0)
        self.range_end_spin.setDecimals(3)
        self.range_end_spin.setSuffix(" s")
        self.export_compatibility_label = QLabel("")
        self.export_compatibility_label.setWordWrap(True)

        self.btn_export_advisor = QPushButton("Benchmark & recommend")
        self.btn_export_advisor_apply = QPushButton("Apply recommendation")
        self.btn_export_advisor_apply.setEnabled(False)
        self.btn_export_advisor_apply.setVisible(False)
        self.btn_clear_export_cache = QPushButton("Clear render cache")
        self.export_advisor_result = QLabel(
            "Recommendation: run the optional benchmark. Your export settings will not be changed."
        )
        self.export_advisor_result.setWordWrap(True)
        self.export_advisor_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.export_advisor_thread = None
        self.export_advisor_worker = None
        self._last_export_recommendation = None
        self._applying_export_settings = False
        self._load_export_settings()

        self._wheel_filter = WheelOnlyIfFocusedFilter(self)
        # Spinboxes should only get focus by click (prevents accidental wheel changes)
        for w in (
            self.threshold_pct,
            self.pre_pad_s, self.post_pad_s, self.min_cut_s, self.gain_db,
            self.attack_ms, self.release_ms, self.merge_pauses_ms,
            self.lufs_target,
            self.parallel_workers_spin, self.chunk_count_spin,
            self.video_bitrate_spin, self.target_size_spin, self.custom_quality_spin,
            self.range_start_spin, self.range_end_spin,
        ):
            try:
                w.setFocusPolicy(Qt.ClickFocus)
            except Exception:
                pass

        # -----------------------------
        # Build UI (layout modules) + theme/icons
        # -----------------------------
        self._snap_enabled = False

        # 0) load UI preferences
        self._load_layout_pref()
        self._load_ui_prefs()
        self._load_theme_pref()
        # 1) prima inizializzi i WebViews
        self._init_web_views()

        # 2) poi costruisci il layout UNA VOLTA
        self._build_layout_modular()
        self._apply_window_size_pref()
        self._apply_layout_mode()
        self._apply_ui_prefs()
        self._sync_export_settings_ui()

        # 3) poi applichi tema / icone
        self._apply_theme_pref()

        self._wheel_filter = GlobalWheelBlocker(self)
        QApplication.instance().installEventFilter(self._wheel_filter)

        self._sync_icons()

        # -----------------------------
        # Presets
        # -----------------------------
        self._load_presets()
        self._populate_presets_combo()
        self._pick_default_preset()
        # Defer until first showEvent so the window/video sink/audio backend are ready.

        # -----------------------------
        # Signals
        # -----------------------------
        self.btn_open.clicked.connect(self.open_file)
        self.btn_play.clicked.connect(self.toggle_play)
        self.chk_skip.stateChanged.connect(self._on_skip_changed)

        self.btn_zoom_in.clicked.connect(self.zoom_in)
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        self.btn_zoom_reset.clicked.connect(self.zoom_reset)
        self.btn_layout.clicked.connect(self._toggle_layout_mode)
        self.btn_cut_tool.toggled.connect(self._on_cut_tool_toggled)
        self.btn_split.toggled.connect(self._on_split_toggled)

        self.slider_precision.valueChanged.connect(self._on_params_changed)
        self.threshold_pct.valueChanged.connect(self._on_params_changed)
        self.pre_pad_s.valueChanged.connect(self._on_params_changed)
        self.post_pad_s.valueChanged.connect(self._on_params_changed)
        self.min_cut_s.valueChanged.connect(self._on_params_changed)
        self.gain_db.valueChanged.connect(self._on_params_changed)
        self.gain_affects_detection.stateChanged.connect(self._on_params_changed)

        self.attack_ms.valueChanged.connect(self._on_params_changed)
        self.release_ms.valueChanged.connect(self._on_params_changed)
        self.smoothing_mode.currentIndexChanged.connect(self._on_params_changed)
        self.merge_pauses_ms.valueChanged.connect(self._on_params_changed)

        self.normalize_lufs.stateChanged.connect(self._on_params_changed)
        self.lufs_target.valueChanged.connect(self._on_params_changed)
        self.limiter.stateChanged.connect(self._on_params_changed)

        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.btn_preset_save.clicked.connect(self._on_preset_save)
        self.btn_preset_save_as.clicked.connect(self._on_preset_save_as)
        self.btn_preset_manage.clicked.connect(self._open_preset_manage_menu)
        self.btn_preset_delete.clicked.connect(self._on_preset_delete)

        self.btn_export.clicked.connect(self.export_mp4)
        self.btn_export_edl.clicked.connect(self.export_edl)
        self.btn_export_details.clicked.connect(self._toggle_export_details)
        self.btn_export_copy_logs.clicked.connect(self._copy_export_logs)
        self.btn_export_open_logs_folder.clicked.connect(self._open_export_logs_folder)
        self.btn_export_advisor.clicked.connect(self._start_export_advisor)
        self.btn_export_advisor_apply.clicked.connect(self._apply_export_recommendation)
        self.btn_export_defaults.clicked.connect(self._reset_export_settings)
        self.btn_clear_export_cache.clicked.connect(self._clear_export_cache)

        self.export_preset_combo.currentIndexChanged.connect(self._on_export_preset_changed)
        for control in self._export_setting_controls():
            if isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._on_export_setting_changed)
            elif isinstance(control, QCheckBox):
                control.stateChanged.connect(self._on_export_setting_changed)
            else:
                control.valueChanged.connect(self._on_export_setting_changed)

        self.seg_main.clicked.connect(lambda: self._switch_page(0))
        self.seg_export.clicked.connect(lambda: self._switch_page(1))

        if hasattr(self, "btn_ai_process"):
            self.btn_ai_process.clicked.connect(self._on_ai_process_clicked)
        if hasattr(self, "btn_ai_reprocess"):
            self.btn_ai_reprocess.clicked.connect(self._on_ai_reprocess_clicked)

        self.btn_adv_toggle.clicked.connect(self._toggle_advanced)
        # lock button hidden (no UI)

        if hasattr(self.timeline, "clipSplitRequested"):
            self.timeline.clipSplitRequested.connect(self._on_clip_split_requested)


        # Shortcuts
        QShortcut(QKeySequence.Undo, self, activated=self._undo_cuts)
        QShortcut(QKeySequence.Redo, self, activated=self._redo_cuts)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._redo_cuts)

        QShortcut(QKeySequence("Ctrl++"), self, activated=self.zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, activated=self.zoom_out)
        QShortcut(QKeySequence("C"), self, activated=self._toggle_cut_tool)
        QShortcut(QKeySequence("S"), self, activated=self._toggle_split_tool)
        QShortcut(QKeySequence("J"), self, activated=self.transport_seek_backward)
        QShortcut(QKeySequence("K"), self, activated=self.toggle_play)
        QShortcut(QKeySequence("L"), self, activated=self.transport_seek_forward)

        # initialize tool mode
        self._set_tool_mode(self.tool_mode)
        self._update_split_button_state()
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.zoom_reset)

        self._refresh_precision_label()
        self._refresh_threshold_ui_state()
        self._refresh_advanced_ui_state()
        self._update_preset_ui_state()
        self._sync_adv_ui()
        self._apply_advanced_lock_state()

        # Default page
        self._switch_page(0)
        self._apply_responsive_ui()
        self._init_crash_recovery()
        self._init_twitch_automation()

    def _apply_core_translations(self) -> None:
        language = str(getattr(self, "_ui_language", "en") or "en")
        translations = {
            "btn_open": "add_video",
            "seg_main": "main",
            "seg_export": "export",
            "btn_export": "export_mp4",
            "btn_export_edl": "export_edl",
            "btn_ai_process": "process_ai",
            "btn_ai_reprocess": "reprocess_ai",
            "btn_export_details": "show_logs",
            "btn_export_copy_logs": "copy_logs",
            "btn_export_open_logs_folder": "open_logs",
        }
        for attr, key in translations.items():
            widget = getattr(self, attr, None)
            if widget is not None and hasattr(widget, "setText"):
                widget.setText(ui_text(key, language))

    def _apply_accessibility_metadata(self) -> None:
        controls = {
            "btn_open": ("Add video", "Open one or more media files"),
            "btn_play": ("Play or pause", "Toggle timeline playback"),
            "btn_zoom_out": ("Zoom out timeline", "Decrease timeline zoom"),
            "btn_zoom_in": ("Zoom in timeline", "Increase timeline zoom"),
            "btn_zoom_reset": ("Reset timeline zoom", "Show the complete timeline"),
            "btn_cut_tool": ("Cut tool", "Mark a range to remove"),
            "btn_split": ("Split tool", "Split the selected clip at the playhead"),
            "preset_combo": ("Cut preset", "Select analysis and cut settings"),
            "codec_combo": ("Export codec", "Select automatic, hardware, or software video encoding"),
            "export_method_combo": ("Export method", "Select the rendering strategy"),
            "btn_export": ("Export MP4", "Render the current project to an MP4 file"),
            "export_progress": ("Export progress", "Current export completion percentage"),
            "timeline": ("Project timeline", "Review clips, cuts, and the playhead"),
        }
        for attr, (name, description) in controls.items():
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            try:
                widget.setAccessibleName(name)
                widget.setAccessibleDescription(description)
            except Exception:
                continue
        for view, name in (
            (getattr(self, "web_topbar", None), "Project actions and status"),
            (getattr(self, "web_transport", None), "Playback controls"),
            (getattr(self, "web_stats", None), "Project statistics"),
        ):
            if view is not None:
                view.setAccessibleName(name)

    def _show_quick_start(self, force: bool = True) -> None:
        settings = QSettings("Auto Cutter", "Auto Cutter")
        if not force and bool(int(settings.value("onboarding_complete_v1", 0) or 0)):
            return
        if self._ui_language == "it":
            message = (
                "1. Aggiungi o trascina un video.\n"
                "2. Scegli Classic o AI e analizza l'audio.\n"
                "3. Controlla i tagli nella timeline e correggili se necessario.\n"
                "4. Apri Esporta, lascia Codec su Auto e crea l'MP4.\n\n"
                "Scorciatoie: K riproduci/pausa, C taglio, S dividi, Ctrl+Z annulla."
            )
            title = "Guida rapida"
        else:
            message = (
                "1. Add or drop a video.\n"
                "2. Choose Classic or AI and analyze the audio.\n"
                "3. Review cuts on the timeline and adjust them if needed.\n"
                "4. Open Export, keep Codec on Auto, and create the MP4.\n\n"
                "Shortcuts: K play/pause, C cut, S split, Ctrl+Z undo."
            )
            title = "Quick start"
        QMessageBox.information(self, title, message)
        settings.setValue("onboarding_complete_v1", 1)

    def _create_diagnostics_bundle(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suggested = Path.home() / "Desktop" / f"AutoCutter-diagnostics-{stamp}.zip"
        output, _ = QFileDialog.getSaveFileName(
            self,
            ui_text("diagnostics", self._ui_language).replace("...", ""),
            str(suggested),
            "ZIP archive (*.zip)",
        )
        if not output:
            return
        try:
            bundle = create_support_bundle(
                Path(output),
                session_logs_dir=self._session_logs_dir(),
                crash_logs_dir=crash_logs_dir(),
                ffmpeg_path=self.ffmpeg_path,
                consistency_errors=self._project_session.consistency_errors(),
            )
            QMessageBox.information(
                self,
                "Diagnostics",
                f"Diagnostics bundle created:\n{bundle}\n\nReview its contents before sharing.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Diagnostics failed", str(exc))

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "Auto Cutter",
            (
                f"Auto Cutter {app_version()}\n\n"
                "Desktop video editor and automatic voice-cut workflow.\n\n"
                "Includes separate FFmpeg command-line programs licensed under "
                "GNU GPL version 3 or later. Source and license notices are "
                "included with the application."
            ),
        )

    def _open_third_party_notices(self) -> None:
        candidates = (
            resource_path("licenses", "THIRD_PARTY_NOTICES.md"),
            resource_path("THIRD_PARTY_NOTICES.md"),
        )
        notice = next((path for path in candidates if path.is_file()), None)
        if notice is None:
            QMessageBox.warning(self, "Third-party notices", "THIRD_PARTY_NOTICES.md was not found.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(notice)))

    def showEvent(self, event):
        ret = super().showEvent(event)
        self._app_log(
            "show_event",
            startup_auto_restore_pending=bool(getattr(self, "_startup_auto_restore_pending", False)),
            startup_auto_restore_started=bool(getattr(self, "_startup_auto_restore_started", False)),
        )
        self._media_dbg(
            f"showEvent pending_restore={bool(getattr(self, '_startup_auto_restore_pending', False))} "
            f"started={bool(getattr(self, '_startup_auto_restore_started', False))}"
        )
        if not bool(getattr(self, "_startup_auto_restore_pending", False)):
            return ret
        if bool(getattr(self, "_startup_auto_restore_started", False)):
            return ret
        self._startup_auto_restore_started = True
        try:
            QTimer.singleShot(0, self._run_deferred_startup_auto_restore)
        except Exception:
            # Fallback: run directly if timer setup fails for any reason.
            self._run_deferred_startup_auto_restore()
        return ret

    def _run_deferred_startup_auto_restore(self) -> None:
        if not bool(getattr(self, "_startup_auto_restore_pending", False)):
            return
        self._startup_auto_restore_pending = False
        self._app_log("startup_auto_restore_begin")
        self._media_dbg("deferred_startup_auto_restore begin")
        self._media_debug_snapshot("before_auto_restore")
        self._media_debug_sync_focus_until = time.monotonic() + 4.0
        try:
            recovered = False
            try:
                recovered = self._offer_crash_recovery_if_needed()
            except Exception:
                recovered = False
            if not recovered:
                self._auto_restore_session()
        finally:
            self._media_debug_snapshot("after_auto_restore")
            try:
                self._web_push_full_state()
            except Exception:
                pass
            try:
                self._update_split_button_state()
            except Exception:
                pass
            self._app_log("startup_auto_restore_end", input_loaded=bool(self.input_path))
            self._media_dbg("deferred_startup_auto_restore end")
            QTimer.singleShot(250, lambda: self._show_quick_start(force=False))

    def _timeline_zoom_signature(self):
        """
        Returns a zoom signature to detect timeline zoom changes.
        Does not depend on a single attribute to remain robust.
        """
        t = self.timeline
        sig = []
        for name in (
            "_zoom_index",
            "view_span",
            "view_start",
            "zoom",
            "_zoom",
            "zoom_factor",
            "scale",
            "px_per_sec",
            "pixels_per_second",
            "seconds_per_pixel",
        ):
            if hasattr(t, name):
                try:
                    sig.append((name, getattr(t, name)))
                except Exception:
                    pass
        # fallback: oggetto id (se non abbiamo nulla, almeno non crasha)
        return tuple(sig)

    def resizeEvent(self, event):
        try:
            self._apply_responsive_ui()
        except Exception:
            pass
        return super().resizeEvent(event)

    def _apply_responsive_ui(self) -> None:
        right_w = 0
        try:
            if hasattr(self, "_right_panel") and self._right_panel is not None:
                right_w = int(self._right_panel.width())
        except Exception:
            right_w = 0
        if right_w <= 1:
            try:
                right_w = max(0, int(self.width()))
            except Exception:
                right_w = 0

        total_w = 0
        try:
            total_w = int(self.width())
        except Exception:
            total_w = 0

        scale = self._ui_scale_factor()
        # Avoid oversized controls on high-DPI compact windows.
        scale_soft = min(scale, 1.12)

        # Responsive breakpoints tuned for laptops (including DPI scaling).
        compact = (total_w < int(1420 * scale)) or (right_w < int(580 * scale))
        minimum = (total_w < int(1240 * scale)) or (right_w < int(520 * scale))

        # Right panel width must adapt to small screens, otherwise left/timeline overflows.
        try:
            if hasattr(self, "_right_panel") and self._right_panel is not None:
                target_min = 340 if minimum else (360 if compact else 380)
                self._right_panel.setMinimumWidth(int(target_min))
        except Exception:
            pass

        # Keep left/right splitter ratio coherent after responsive min-width updates.
        try:
            if hasattr(self, "_main_splitter") and self._main_splitter is not None:
                self._apply_main_panel_ratio()
        except Exception:
            pass

        # Keep segmented tabs compact on laptops.
        try:
            seg_h = 42 if compact else 48
            if hasattr(self, "seg_main") and self.seg_main is not None:
                self.seg_main.setFixedHeight(seg_h)
            if hasattr(self, "seg_export") and self.seg_export is not None:
                self.seg_export.setFixedHeight(seg_h)
        except Exception:
            pass

        # Web rows scale with DPI and compact mode.
        try:
            if hasattr(self, "web_topbar") and self.web_topbar is not None:
                self.web_topbar.setFixedHeight(int(round((62 if minimum else (66 if compact else 70)) * scale_soft)))
            if hasattr(self, "web_transport") and self.web_transport is not None:
                # Keep extra headroom: browser-side controls (especially volume) can
                # be taller than expected on some DPI/theme combinations.
                self.web_transport.setFixedHeight(int(round((70 if minimum else (74 if compact else 78)) * scale_soft)))
            if hasattr(self, "web_stats") and self.web_stats is not None:
                stats_two_rows = False
                try:
                    stats_two_rows = int(self.web_stats.width()) > 0 and int(self.web_stats.width()) <= 980
                except Exception:
                    stats_two_rows = False
                base_h = 70 if minimum else (74 if compact else 78)
                if stats_two_rows:
                    base_h += 8
                stats_h = int(round(base_h * scale_soft))
                self.web_stats.setFixedHeight(max(66, min(96, stats_h)))
            if hasattr(self, "web_stats_full") and self.web_stats_full is not None:
                if minimum:
                    self.web_stats_full.setMinimumHeight(260)
                    self.web_stats_full.setMaximumHeight(460)
                elif compact:
                    self.web_stats_full.setMinimumHeight(300)
                    self.web_stats_full.setMaximumHeight(560)
                else:
                    self.web_stats_full.setMinimumHeight(420)
                    self.web_stats_full.setMaximumHeight(760)
        except Exception:
            pass

        # Keep reasonable floors, but let the splitter control most of the space.
        try:
            if hasattr(self, "video_widget") and self.video_widget is not None:
                if minimum:
                    self.video_widget.setMinimumHeight(int(round(125 * scale_soft)))
                elif compact:
                    self.video_widget.setMinimumHeight(int(round(145 * scale_soft)))
                else:
                    self.video_widget.setMinimumHeight(int(round(170 * scale_soft)))
        except Exception:
            pass

        try:
            if hasattr(self, "timeline_scroll") and self.timeline_scroll is not None:
                if minimum:
                    self.timeline_scroll.setMinimumHeight(int(round(112 * scale_soft)))
                elif compact:
                    self.timeline_scroll.setMinimumHeight(int(round(124 * scale_soft)))
                else:
                    self.timeline_scroll.setMinimumHeight(int(round(146 * scale_soft)))
        except Exception:
            pass

        btn_specs = [
            (getattr(self, "btn_preset_save", None), "Save"),
            (getattr(self, "btn_preset_save_as", None), "Save as..."),
            (getattr(self, "btn_preset_manage", None), "Manage"),
        ]
        for btn, full_text in btn_specs:
            if btn is None:
                continue
            try:
                btn.setText("" if compact else full_text)
                btn.setMinimumHeight(34)
                if compact:
                    btn.setMinimumWidth(36)
                    btn.setMaximumWidth(42)
                else:
                    btn.setMaximumWidth(16777215)
            except Exception:
                pass

        # Keep helper text visible; compact-mode hiding made the UI feel "empty".
        for root in (getattr(self, "page_main", None), getattr(self, "page_export", None)):
            if root is None:
                continue
            try:
                for lbl in root.findChildren(QLabel):
                    name = str(lbl.objectName() or "")
                    if name in {"FieldHint", "PresetMeta"}:
                        lbl.setVisible(True)
                    elif name == "SubtleHint":
                        lbl.setVisible(True)
            except Exception:
                pass

        try:
            if hasattr(self, "lbl_preset_meta") and self.lbl_preset_meta is not None:
                self.lbl_preset_meta.setVisible(True)
        except Exception:
            pass

        # Threshold block specific compaction to avoid clipping at the minimum width.
        try:
            if hasattr(self, "threshold_pct") and self.threshold_pct is not None:
                self.threshold_pct.setFixedWidth(84 if minimum else (90 if compact else 96))
        except Exception:
            pass
        try:
            if hasattr(self, "threshold_controls_layout") and self.threshold_controls_layout is not None:
                narrow_right = right_w < int(430 * scale_soft)
                self.threshold_controls_layout.setDirection(
                    QBoxLayout.TopToBottom if narrow_right else QBoxLayout.LeftToRight
                )
                self.threshold_controls_layout.setSpacing(6 if narrow_right else 8)
        except Exception:
            pass
        try:
            if hasattr(self, "threshold_meter") and self.threshold_meter is not None:
                self.threshold_meter.setFixedHeight(58 if minimum else (64 if compact else 70))
        except Exception:
            pass
        try:
            if hasattr(self, "threshold_note") and self.threshold_note is not None:
                self.threshold_note.setVisible(True)
        except Exception:
            pass

        # Keep status/helper labels concise and avoid collisions in narrow inspector widths.
        try:
            if hasattr(self, "lbl_precision") and self.lbl_precision is not None:
                self.lbl_precision.setVisible(True)
        except Exception:
            pass

        # Advanced headers: stack controls on narrow inspector widths to avoid text clipping.
        try:
            narrow_right = right_w < int(430 * scale_soft)
            very_narrow = right_w < int(395 * scale_soft)
            main_hdr = getattr(self, "adv_header_layout", None)
            if main_hdr is not None:
                main_hdr.setDirection(QBoxLayout.TopToBottom if narrow_right else QBoxLayout.LeftToRight)
                main_hdr.setSpacing(6 if narrow_right else 8)
            for hdr in getattr(self, "_adv_section_header_layouts", []) or []:
                try:
                    hdr.setDirection(QBoxLayout.TopToBottom if narrow_right else QBoxLayout.LeftToRight)
                    hdr.setSpacing(6 if narrow_right else 8)
                except Exception:
                    pass
            for badge_name in (
                "lbl_preset_state",
                "lbl_intensity_value",
                "lbl_threshold_semantic",
                "lbl_adv_state_badge",
            ):
                badge = getattr(self, badge_name, None)
                if badge is not None:
                    badge.setVisible(not very_narrow)
            for badge in getattr(self, "_adv_section_badges", {}).values():
                try:
                    badge.setVisible(not very_narrow)
                except Exception:
                    pass
        except Exception:
            pass

        # Preserve user-selected video/timeline split after any resize/responsive pass.
        try:
            self._apply_preview_timeline_ratio()
        except Exception:
            pass

        # Export logs panel height must scale on small displays to avoid clipping/white gaps.
        try:
            if hasattr(self, "export_details") and self.export_details is not None:
                if minimum:
                    self.export_details.setMinimumHeight(120)
                    self.export_details.setMaximumHeight(170)
                elif compact:
                    self.export_details.setMinimumHeight(140)
                    self.export_details.setMaximumHeight(200)
                else:
                    self.export_details.setMinimumHeight(160)
                    self.export_details.setMaximumHeight(220)
        except Exception:
            pass

        # Keep timeline visual clean (no unwanted bars in normal mode).
        try:
            self._update_timeline_scroll_policy()
        except Exception:
            pass

    def _calc_zoom_steps_max(self) -> int:
        levels = getattr(self.timeline, "_zoom_levels", None)
        if levels:
            try:
                return max(0, len(levels) - 1)
            except Exception:
                pass
        return 0

    def _push_zoom_to_web(self) -> None:
        steps_max = self._calc_zoom_steps_max()
        try:
            step = int(getattr(self.timeline, "_zoom_index", 0))
        except Exception:
            step = 0
        step = max(0, min(steps_max, step))
        self._web_zoom_steps = step
        self._web_zoom_steps_max = steps_max
        self._web_js(
            self.web_transport,
            f"uiSetZoom({int(step)}, {int(steps_max)});"
        )

    def zoom_in(self) -> None:
        before = self._timeline_zoom_signature()
        self.timeline.zoom_in()
        after = self._timeline_zoom_signature()
        if after != before:
            self._push_zoom_to_web()

    def zoom_out(self) -> None:
        before = self._timeline_zoom_signature()
        self.timeline.zoom_out()
        after = self._timeline_zoom_signature()
        if after != before:
            self._push_zoom_to_web()

    def zoom_reset(self) -> None:
        self.timeline.zoom_reset()
        self._push_zoom_to_web()

    def _ui_scale_factor(self) -> float:
        """
        Returns a conservative UI scale factor from logical DPI.
        Keeps controls readable on 125%/150% laptop scaling without hardcoding.
        """
        try:
            screen = self.screen()
            if screen is not None:
                dpi = float(screen.logicalDotsPerInch())
            else:
                dpi = float(QApplication.primaryScreen().logicalDotsPerInch())
        except Exception:
            dpi = 96.0
        scale = max(1.0, min(1.55, dpi / 96.0))
        return float(scale)

    def _clamp_preview_timeline_ratio(self, ratio: float) -> float:
        try:
            r = float(ratio)
        except Exception:
            r = 0.72
        return float(max(0.40, min(0.88, r)))

    def _clamp_main_panel_ratio(self, ratio: float) -> float:
        try:
            r = float(ratio)
        except Exception:
            r = 0.69
        return float(max(0.52, min(0.85, r)))

    def _on_preview_timeline_splitter_moved(self, *_args) -> None:
        splitter = getattr(self, "preview_timeline_splitter", None)
        if splitter is None:
            return
        try:
            sizes = [int(v) for v in splitter.sizes()]
        except Exception:
            return
        total = int(sum(v for v in sizes if v > 0))
        if total <= 0 or len(sizes) < 2:
            return
        self._preview_timeline_ratio = self._clamp_preview_timeline_ratio(float(sizes[0]) / float(total))

    def _on_main_panel_splitter_moved(self, *_args) -> None:
        splitter = getattr(self, "_main_splitter", None)
        if splitter is None:
            return
        try:
            sizes = [int(v) for v in splitter.sizes()]
        except Exception:
            return
        if len(sizes) < 2:
            return
        total = int(sum(v for v in sizes if v > 0))
        if total <= 0:
            return
        if self._layout_mode == "right":
            left_size = int(sizes[1])
        else:
            left_size = int(sizes[0])
        self._main_panel_ratio = self._clamp_main_panel_ratio(float(left_size) / float(total))

    def _apply_preview_timeline_ratio(self) -> None:
        splitter = getattr(self, "preview_timeline_splitter", None)
        if splitter is None:
            return
        try:
            sizes = [int(v) for v in splitter.sizes()]
        except Exception:
            sizes = []
        if len(sizes) < 2:
            return
        total = int(sum(v for v in sizes if v > 0))
        if total <= 0:
            try:
                total = int(splitter.size().height())
            except Exception:
                total = 0
        if total <= 0:
            return
        ratio = self._clamp_preview_timeline_ratio(getattr(self, "_preview_timeline_ratio", 0.72))
        # On compact laptop heights keep enough space for preview by default.
        try:
            h = int(self.height())
        except Exception:
            h = 0
        if h > 0 and h <= 760 and ratio < 0.58:
            ratio = 0.58
        elif h > 0 and h <= 860 and ratio < 0.54:
            ratio = 0.54
        top = max(1, int(round(total * ratio)))
        bottom = max(1, int(total - top))
        try:
            splitter.blockSignals(True)
            splitter.setSizes([top, bottom])
        finally:
            try:
                splitter.blockSignals(False)
            except Exception:
                pass

    def _apply_main_panel_ratio(self) -> None:
        splitter = getattr(self, "_main_splitter", None)
        if splitter is None:
            return
        try:
            sizes = [int(v) for v in splitter.sizes()]
        except Exception:
            sizes = []
        if len(sizes) < 2:
            return
        total = int(sum(v for v in sizes if v > 0))
        if total <= 0:
            try:
                total = int(splitter.size().width())
            except Exception:
                total = 0
        if total <= 0:
            return
        ratio = self._clamp_main_panel_ratio(getattr(self, "_main_panel_ratio", 0.69))
        left = max(1, int(round(total * ratio)))
        right = max(1, int(total - left))
        target = [right, left] if self._layout_mode == "right" else [left, right]
        try:
            splitter.blockSignals(True)
            splitter.setSizes(target)
        finally:
            try:
                splitter.blockSignals(False)
            except Exception:
                pass

    def _init_web_views(self) -> None:
        self.web_topbar = QWebEngineView()
        self.web_transport = QWebEngineView()
        self.web_stats = QWebEngineView()
        # These legacy pages were updated but never inserted in the layout.
        self.web_inspector = None
        self.web_stats_full = None
        self._web_ready: dict[QWebEngineView, bool] = {}
        self._web_js_queue: dict[QWebEngineView, list[str]] = {}
        self._web_js_flush_pending: set[int] = set()
        self._web_disabled = False

        scale = self._ui_scale_factor()
        scale_soft = min(scale, 1.12)
        self.web_topbar.setFixedHeight(int(round(70 * scale_soft)))
        self.web_transport.setFixedHeight(int(round(74 * scale_soft)))
        self.web_stats.setFixedHeight(int(round(78 * scale_soft)))

        self._web_channel = QWebChannel()
        self._web_bridge = WebUiBridge(self)
        self._web_channel.registerObject("bridge", self._web_bridge)

        for view in (self.web_topbar, self.web_transport, self.web_stats):
            view.page().setWebChannel(self._web_channel)
            view.setContextMenuPolicy(Qt.NoContextMenu)
            # Rimuovi lo sfondo bianco di default dei QWebEngineView
            view.setStyleSheet("background: transparent; border: none; margin: 0; padding: 0;")
            view.page().setBackgroundColor(Qt.transparent)
            # Rimuovi eventuali margini
            view.setContentsMargins(0, 0, 0, 0)
            self._web_ready[view] = False
            self._web_js_queue[view] = []
            view.loadFinished.connect(lambda ok, v=view: self._on_web_loaded(v, ok))
            view.destroyed.connect(lambda _=None, v=view: self._on_web_view_destroyed(v))

        # --- FIND WebUI folder robustly ---
        candidates = [
            self._project_root / "webui",
            self._project_root / "WebUI",
            Path.cwd() / "webui",
            Path.cwd() / "WebUI",
        ]
        webdir = next((p for p in candidates if p.exists() and p.is_dir()), None)

        if webdir is None:
            QMessageBox.critical(
                self,
                "WebUI not found",
                "Non trovo la cartella WebUI.\n"
                "Mi aspetto: <project>/WebUI\n\n"
                f"Ho cercato in:\n" + "\n".join(str(p) for p in candidates)
            )
            self._web_disabled = True
            return

        # --- verify required files exist ---
        required = {
            "topbar": webdir / "topbar.html",
            "transport": webdir / "bottombar.html",
            "stats": webdir / "stats.html",
            "css": webdir / "ui.css",
        }
        missing = [name for name, p in required.items() if not p.exists()]
        if missing:
            QMessageBox.critical(
                self,
                "WebUI files missing",
                "Mancano questi file dentro WebUI:\n"
                + "\n".join(f"- {m}: {required[m]}" for m in missing)
            )
            self._web_disabled = True
            return

        def _url_with_version(p: Path, extra_query: str | None = None) -> QUrl:
            url = QUrl.fromLocalFile(str(p))
            try:
                ver = int(os.path.getmtime(str(p)))
            except Exception:
                ver = 0
            q = f"v={ver}"
            if extra_query:
                q = f"{extra_query}&{q}"
            url.setQuery(q)
            return url

        # --- load pages (absolute file URLs, cache-busted) ---
        self.web_topbar.setUrl(_url_with_version(required["topbar"]))
        self.web_transport.setUrl(_url_with_version(required["transport"]))
        self.web_stats.setUrl(_url_with_version(required["stats"]))

    def _on_web_loaded(self, view: QWebEngineView, ok: bool) -> None:
        try:
            if hasattr(self, "_web_ready"):
                self._web_ready[view] = bool(ok)
        except Exception:
            pass
        if ok:
            # Keep WebUI vars aligned with Qt theme after each load.
            # Without this, some systems can show low-contrast text in light mode.
            try:
                self._web_apply_theme_vars()
            except Exception:
                pass
            try:
                self._schedule_web_js_flush(view)
            except Exception:
                pass
            # push current state when any view becomes ready
            try:
                QTimer.singleShot(0, self._web_push_full_state)
            except Exception:
                pass

    def _web_obj_valid(self, obj: Any) -> bool:
        try:
            return bool(obj) and bool(qt_is_valid(obj))
        except Exception:
            return bool(obj)

    def _on_web_view_destroyed(self, view: QWebEngineView) -> None:
        try:
            self._web_ready.pop(view, None)
        except Exception:
            pass
        try:
            self._web_js_queue.pop(view, None)
        except Exception:
            pass
        try:
            self._web_js_flush_pending.discard(id(view))
        except Exception:
            pass

    def _schedule_web_js_flush(self, view: QWebEngineView) -> None:
        if getattr(self, "_web_disabled", False):
            return
        if not self._web_obj_valid(view):
            return
        key = id(view)
        if key in self._web_js_flush_pending:
            return
        self._web_js_flush_pending.add(key)
        QTimer.singleShot(0, lambda v=view, k=key: self._flush_web_js(v, k))

    def _flush_web_js(self, view: QWebEngineView, key: int | None = None) -> None:
        try:
            if key is not None:
                self._web_js_flush_pending.discard(key)
        except Exception:
            pass
        if getattr(self, "_web_disabled", False):
            return
        if not self._web_obj_valid(view):
            try:
                self._web_js_queue.pop(view, None)
            except Exception:
                pass
            return
        try:
            ready = bool(self._web_ready.get(view, False))
        except Exception:
            ready = True
        if not ready:
            return
        queue = self._web_js_queue.get(view)
        if not queue:
            return
        page = view.page() if hasattr(view, "page") else None
        if not self._web_obj_valid(page):
            return
        payload = [str(js) for js in queue if js]
        if not payload:
            queue.clear()
            return
        queue.clear()
        for js in payload:
            self._dispatch_web_js(view, js)

    def _dispatch_web_js(self, view: QWebEngineView, js: str) -> None:
        if not js:
            return
        if getattr(self, "_web_disabled", False):
            return
        if not self._web_obj_valid(view):
            return

        def _run(v=view, script=str(js)) -> None:
            if getattr(self, "_web_disabled", False):
                return
            if not self._web_obj_valid(v):
                return
            try:
                ready = bool(self._web_ready.get(v, False))
            except Exception:
                ready = True
            if not ready:
                try:
                    self._web_js_queue.setdefault(v, []).append(script)
                except Exception:
                    pass
                return
            try:
                page = v.page() if hasattr(v, "page") else None
            except Exception:
                page = None
            if not self._web_obj_valid(page):
                return
            try:
                page.runJavaScript(script)
            except Exception:
                return

        QTimer.singleShot(0, _run)

    # -----------------------------
    # Tracks helpers / properties
    # -----------------------------
    def _ensure_audio_player_for_track(self, idx: int) -> None:
        # legacy no-op (audio handled by QMediaPlayer)
        return

    def _set_all_positions(self, pos_ms: int) -> None:
        pos_ms = int(max(0, pos_ms))
        t = pos_ms / 1000.0
        total = self._global_duration if self._global_duration > 0 else self.duration
        if total > 0:
            t = max(0.0, min(float(t), float(total)))
            pos_ms = int(t * 1000)
        self._dbg_segments(f"set_all_positions t={t:.3f} pos_ms={pos_ms} total={total:.3f}")
        self._last_pos = float(t)
        self._set_pending_seek(t)
        self._sync_video_to_global(t, force=True)
        self._sync_audio_to_global(t, force=True)
        if total > 0 and t >= (float(total) - 1e-3):
            try:
                self._play_requested = False
                self.video_player.pause()
                self.audio_player.pause()
            except Exception:
                pass

    def _set_pending_seek(self, t: float) -> None:
        self._pending_seek_target = float(t)
        self._pending_seek_ts = time.monotonic()

    def _audio_clip_for_track(self, track_idx: int) -> Optional[Clip]:
        if track_idx < 0 or track_idx >= len(self._tracks):
            return None
        clip_id = self._tracks[track_idx].audio_clip_id
        return self._find_clip(clip_id)

    def _rebuild_video_segments(self) -> None:
        segments: list[dict[str, object]] = []
        total = 0.0
        for idx, t in enumerate(self._tracks):
            if not t.video_track_id:
                continue
            vtrack = self.project.get_track(str(t.video_track_id))
            if vtrack is None:
                continue
            for clip in vtrack.sorted_clips():
                media = self.project.get_media(clip.media_id)
                path = media.path if media else None
                if not path:
                    continue
                dur = max(0.0, float(clip.timeline_out) - float(clip.timeline_in))
                if dur <= 0.0:
                    continue
                seg = {
                    "clip_id": clip.id,
                    "track_id": clip.track_id,
                    "track_state_idx": idx,
                    "path": path,
                    "start": float(clip.timeline_in),
                    "end": float(clip.timeline_out),
                    "duration": float(dur),
                    "source_in": float(clip.source_in),
                    "media_id": clip.media_id,
                }
                segments.append(seg)
                total = max(total, float(clip.timeline_out))

        segments.sort(key=lambda s: float(s.get("start", 0.0)))
        self._video_segments = segments
        self._video_concat_duration = float(total)
        if self._debug_segments:
            try:
                msg = ", ".join(
                    f"{i}:{Path(s.get('path') or '').name}@{float(s.get('start',0)):.2f}-{float(s.get('end',0)):.2f}"
                    for i, s in enumerate(segments)
                )
                self._dbg_segments(f"rebuild_video_segments n={len(segments)} [{msg}]", force=True)
            except Exception:
                pass

    def _video_layer_rank(self, track_state_idx: object | None) -> int:
        """
        Lower rank = visually higher (topmost) track.
        Adjust here if you want newer tracks on top.
        """
        try:
            return int(track_state_idx)
        except Exception:
            return 0

    def _rebuild_audio_segments(self, track_idx: int | None = None) -> None:
        # Sequential mode: build a single global audio timeline across all tracks.
        self._audio_segments_track_idx = None
        self._audio_segment_index = -1

        segments: list[dict[str, object]] = []
        for idx, tstate in enumerate(self._tracks):
            if not tstate.audio_track_id:
                continue
            atrack = self.project.get_track(str(tstate.audio_track_id))
            if atrack is None:
                continue
            for clip in atrack.sorted_clips():
                media = self.project.get_media(clip.media_id)
                path = media.path if media else None
                if not path:
                    continue
                dur = max(0.0, float(clip.timeline_out) - float(clip.timeline_in))
                if dur <= 0.0:
                    continue
                seg = {
                    "clip_id": clip.id,
                    "track_id": clip.track_id,
                    "track_state_idx": idx,
                    "path": path,
                    "start": float(clip.timeline_in),
                    "end": float(clip.timeline_out),
                    "duration": float(dur),
                    "source_in": float(clip.source_in),
                    "media_id": clip.media_id,
                }
                segments.append(seg)

        segments.sort(key=lambda s: float(s.get("start", 0.0)))
        self._audio_segments = segments

    def _map_global_to_video(self, t: float) -> Optional[tuple[int, dict[str, object], float]]:
        if not self._video_segments:
            return None
        t = max(0.0, float(t))
        best_idx = None
        best_seg = None
        best_rank = None
        best_start = None
        for i, seg in enumerate(self._video_segments):
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", 0.0) or 0.0)
            if start <= t < end:
                rank = self._video_layer_rank(seg.get("track_state_idx", None))
                if best_rank is None or rank < best_rank or (rank == best_rank and (best_start is None or start > best_start)):
                    best_rank = rank
                    best_start = start
                    best_idx = i
                    best_seg = seg
        if best_seg is None or best_idx is None:
            return None
        start = float(best_seg.get("start", 0.0) or 0.0)
        dur = float(best_seg.get("duration", 0.0) or 0.0)
        src_in = float(best_seg.get("source_in", 0.0) or 0.0)
        offset = max(0.0, min(dur, t - start))
        local = src_in + offset
        return best_idx, best_seg, local

    def _map_global_to_audio(self, t: float, track_idx: int | None = None) -> Optional[tuple[int, dict[str, object], float]]:
        # Sequential mode: use a global audio timeline.
        # On overlaps, prefer the track currently visible in video at playhead,
        # then continuity with the currently playing audio track.
        if not self._audio_segments or self._audio_segments_track_idx is not None:
            self._rebuild_audio_segments(None)
        if not self._audio_segments:
            return None
        t = max(0.0, float(t))

        requested_track = None
        if track_idx is not None:
            try:
                requested_track = int(track_idx)
            except Exception:
                requested_track = None

        current_track = None
        try:
            audio_idx = int(self._audio_segment_index)
        except Exception:
            audio_idx = -1
        if 0 <= audio_idx < len(self._audio_segments):
            try:
                current_track = int(self._audio_segments[audio_idx].get("track_state_idx"))
            except Exception:
                current_track = None

        preferred_video_track = None
        vmap = self._map_global_to_video(t)
        if vmap is not None:
            try:
                preferred_video_track = int(vmap[1].get("track_state_idx"))
            except Exception:
                preferred_video_track = None

        candidates: list[tuple[int, dict[str, object], float]] = []
        for i, seg in enumerate(self._audio_segments):
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", 0.0) or 0.0)
            if not (start <= t < end):
                continue
            if requested_track is not None:
                try:
                    seg_track = int(seg.get("track_state_idx"))
                except Exception:
                    continue
                if seg_track != requested_track:
                    continue
            candidates.append((i, seg, start))

        if not candidates:
            return None

        if len(candidates) > 1:
            def _rank(item: tuple[int, dict[str, object], float]) -> tuple[int, int, float, int, int]:
                idx, seg, start = item
                try:
                    seg_track = int(seg.get("track_state_idx"))
                except Exception:
                    seg_track = 1_000_000
                same_video = 0 if (preferred_video_track is not None and seg_track == preferred_video_track) else 1
                same_current = 0 if (current_track is not None and seg_track == current_track) else 1
                # Prefer latest-starting segment as a stable tie-break on overlaps.
                return (same_video, same_current, -float(start), seg_track, int(idx))

            candidates.sort(key=_rank)

        i, seg, start = candidates[0]
        dur = float(seg.get("duration", 0.0) or 0.0)
        src_in = float(seg.get("source_in", 0.0) or 0.0)
        offset = max(0.0, min(dur, t - start))
        local = src_in + offset
        return i, seg, local

    def _sync_video_to_global(self, t: float, force: bool = False) -> None:
        mapped = self._map_global_to_video(t)
        if mapped is None:
            self._dbg_segments(f"sync_video_to_global: no map for t={t:.3f}")
            return
        idx, seg, local = mapped
        path = str(seg.get("path") or "")
        if not path:
            return

        now = time.monotonic()
        current_path = ""
        try:
            current_path = self.video_player.source().toLocalFile()
        except Exception:
            current_path = ""
        same_source = self._same_local_path(current_path, path)
        if idx != self._video_segment_index or force:
            self._dbg_segments(
                f"switch seg idx {self._video_segment_index}->{idx} t={t:.3f} local={local:.3f} "
                f"start={float(seg.get('start',0)):.3f} src_in={float(seg.get('source_in',0)):.3f}",
                force=True,
            )
            self._video_segment_index = idx
            self._set_pending_seek(t)
            was_playing = self.video_player.playbackState() == QMediaPlayer.PlayingState
            target_ms = int(local * 1000)
            if same_source:
                # Keep the decoder on the same source and force a direct seek.
                self.video_player.setPosition(target_ms)
            else:
                self.video_player.setSource(QUrl.fromLocalFile(path))
                self.video_player.setPosition(target_ms)
            # allow decoder to settle near segment boundaries (keyframe alignment)
            self._segment_seek_guard_until = now + (0.45 if same_source else 0.8)
            try:
                self._segment_guard_src_in = float(seg.get("source_in", 0.0) or 0.0)
                self._segment_guard_start = float(seg.get("start", 0.0) or 0.0)
            except Exception:
                self._segment_guard_src_in = 0.0
                self._segment_guard_start = 0.0
            if was_playing:
                self.video_player.play()
            else:
                self.video_player.pause()
            return

        if not force and self.video_player.playbackState() == QMediaPlayer.PlayingState:
            return

        if now < self._segment_seek_guard_until:
            self._dbg_segments(f"sync_video_to_global: guard t={t:.3f} now<guard")
            return

        cur = int(self.video_player.position())
        target = int(local * 1000)
        # Avoid thrashing at segment start when keyframes are before source_in.
        try:
            src_in = float(seg.get("source_in", 0.0) or 0.0)
        except Exception:
            src_in = 0.0
        if local <= (src_in + 0.75) and cur < target:
            self._dbg_segments(
                f"sync_video_to_global: skip re-seek local={local:.3f} src_in={src_in:.3f} cur={cur} target={target}"
            )
            return
        deadband_ms = 800
        if abs(cur - target) > deadband_ms:
            self._dbg_segments(f"sync_video_to_global: seek cur={cur} target={target}")
            self.video_player.setPosition(target)

    def _sync_audio_to_global(self, t: float, force: bool = False) -> None:
        mapped = self._map_global_to_audio(t)
        if mapped is None:
            if force or (time.monotonic() < float(getattr(self, "_media_debug_sync_focus_until", 0.0) or 0.0)):
                self._media_dbg(f"sync_audio_to_global no_map t={float(t):.3f} force={bool(force)}", throttle_s=0.05)
            try:
                if self.audio_player.playbackState() == QMediaPlayer.PlayingState:
                    self.audio_player.pause()
            except Exception:
                pass
            self._audio_segment_index = -1
            return
        idx, seg, local = mapped
        path = str(seg.get("path") or "")
        if not path:
            return

        now = time.monotonic()
        current_path = ""
        try:
            current_path = self.audio_player.source().toLocalFile()
        except Exception:
            current_path = ""
        requested_path = str(getattr(self, "_audio_requested_source_path", "") or "")
        try:
            requested_ts = float(getattr(self, "_audio_requested_source_ts", 0.0) or 0.0)
        except Exception:
            requested_ts = 0.0
        try:
            media_status = self.audio_player.mediaStatus()
        except Exception:
            media_status = getattr(self, "_audio_last_media_status", None)
        loading_like_statuses = {
            getattr(QMediaPlayer, "LoadingMedia", None),
            getattr(QMediaPlayer, "LoadedMedia", None),
            getattr(QMediaPlayer, "BufferingMedia", None),
            getattr(QMediaPlayer, "BufferedMedia", None),
            getattr(QMediaPlayer, "StalledMedia", None),
        }
        same_source_pending = bool(
            self._same_local_path(requested_path, path)
            and ((now - requested_ts) < 1.2)
            and (media_status in loading_like_statuses)
            and ((not current_path) or self._same_local_path(current_path, path))
        )
        same_source = bool(self._same_local_path(current_path, path) or same_source_pending)
        segment_changed = bool(idx != self._audio_segment_index)
        source_changed = bool(not same_source)
        target_ms = int(local * 1000)
        try:
            was_playing = self.video_player.playbackState() == QMediaPlayer.PlayingState
        except Exception:
            was_playing = False

        def _hard_reload_audio_source() -> None:
            # Force decoder/buffer reset so audio never keeps playing from pre-seek data.
            try:
                self.audio_player.stop()
            except Exception:
                pass
            try:
                self._set_audio_player_source(None)
            except Exception:
                pass
            self._set_audio_player_source(path)

        if force:
            self._media_dbg(
                f"sync_audio_to_global force t={float(t):.3f} idx={idx} local={float(local):.3f} "
                f"segment_changed={segment_changed} source_changed={source_changed} "
                f"same_source={same_source} target_ms={target_ms}",
                throttle_s=0.02,
            )
            self._audio_segment_index = idx
            try:
                if was_playing and source_changed:
                    # During playback, always flush queued audio to avoid hearing the pre-jump cut area.
                    # Only reload when the file/source actually changes.
                    _hard_reload_audio_source()
                else:
                    if not was_playing:
                        self.audio_player.pause()
            except Exception:
                pass
            try:
                if was_playing:
                    if source_changed:
                        # already reloaded above
                        pass
                    else:
                        # Same file/source: direct seek is enough and avoids decoder thrash.
                        pass
                elif source_changed:
                    self._set_audio_player_source(path)
            except Exception:
                pass
            self.audio_player.setPosition(target_ms)
            self._audio_seek_guard_until = now + (0.22 if same_source else 0.35)
            if was_playing:
                self.audio_player.play()
            else:
                self.audio_player.pause()
            return

        if source_changed or segment_changed:
            if time.monotonic() < float(getattr(self, "_media_debug_sync_focus_until", 0.0) or 0.0):
                self._media_dbg(
                    f"sync_audio_to_global remap t={float(t):.3f} idx={idx} local={float(local):.3f} "
                    f"segment_changed={segment_changed} source_changed={source_changed} "
                    f"same_source={same_source} target_ms={target_ms}",
                    throttle_s=0.03,
                )
            self._audio_segment_index = idx
            try:
                if was_playing and source_changed:
                    # Flush only when changing file/source.
                    _hard_reload_audio_source()
                else:
                    if not was_playing:
                        self.audio_player.pause()
            except Exception:
                pass
            if was_playing:
                # If source_changed, source already reloaded; otherwise keep same source.
                pass
            elif source_changed:
                self._set_audio_player_source(path)
            self.audio_player.setPosition(target_ms)
            self._audio_seek_guard_until = now + (0.25 if same_source else 0.5)
            if was_playing:
                self.audio_player.play()
            else:
                self.audio_player.pause()
            return

        if same_source_pending:
            # Source request for the same file is still loading/buffering.
            # Avoid reissuing setSource()/seek every frame while video is moving.
            return

        if now < self._audio_seek_guard_until:
            return

        cur = int(self.audio_player.position())
        target = target_ms
        # Keep audio continuously aligned to the red playhead line (video clock).
        # A tighter deadband while playing recovers from missed seeks/jumps.
        deadband_ms = 140 if was_playing else 800
        if abs(cur - target) > deadband_ms:
            if time.monotonic() < float(getattr(self, "_media_debug_sync_focus_until", 0.0) or 0.0):
                self._media_dbg(
                    f"sync_audio_to_global seek cur={cur} target={target} deadband={deadband_ms} playing={was_playing}",
                    throttle_s=0.03,
                )
            self.audio_player.setPosition(target)
    def _get_active_track(self) -> TrackState:
        if not self._tracks:
            self._tracks.append(TrackState())
        if self._active_track_index is None:
            self._active_track_index = 0
        if self._active_track_index >= len(self._tracks):
            self._active_track_index = max(0, len(self._tracks) - 1)
        track = self._tracks[self._active_track_index]
        if track.audio_track_id is None:
            self._ensure_audio_track_for_state(track)
        if track.video_track_id is None:
            self._ensure_video_track_for_state(track)
        return track

    def _save_track_cfg(self, track: TrackState | None) -> None:
        if track is None:
            return
        # Ensure any in-progress edits in spinboxes are committed
        try:
            if track is self._get_active_track():
                self._commit_filter_inputs()
        except Exception:
            pass
        try:
            track.cfg = dict(self._current_preset_cfg())
        except Exception:
            track.cfg = dict(getattr(track, "cfg", {}) or {})

    def _apply_track_cfg(self, track: TrackState | None) -> None:
        if track is None:
            return
        cfg = getattr(track, "cfg", None)
        if isinstance(cfg, dict) and cfg:
            self._apply_cfg_from_dict(cfg)
        else:
            # seed with current UI values if empty
            try:
                track.cfg = dict(self._current_preset_cfg())
            except Exception:
                track.cfg = {}

    def _commit_filter_inputs(self) -> None:
        # Commit any pending edits in spinboxes before snapshotting cfg
        widgets = [
            self.threshold_pct,
            self.pre_pad_s,
            self.post_pad_s,
            self.min_cut_s,
            self.gain_db,
            self.attack_ms,
            self.release_ms,
            self.merge_pauses_ms,
            self.lufs_target,
        ]
        for w in widgets:
            try:
                w.interpretText()
            except Exception:
                pass

    @property
    def input_path(self) -> Optional[str]:
        return self._get_active_track().path

    @input_path.setter
    def input_path(self, v: Optional[str]) -> None:
        self._get_active_track().path = v

    @property
    def duration(self) -> float:
        return float(self._get_active_track().duration)

    @duration.setter
    def duration(self, v: float) -> None:
        self._get_active_track().duration = float(v)

    @property
    def rms(self) -> Optional[np.ndarray]:
        return self._get_active_track().rms

    @rms.setter
    def rms(self, v: Optional[np.ndarray]) -> None:
        self._get_active_track().rms = v

    @property
    def hop_s(self) -> float:
        return float(self._get_active_track().hop_s)

    @hop_s.setter
    def hop_s(self, v: float) -> None:
        self._get_active_track().hop_s = float(v)

    @property
    def cuts(self) -> list[Segment]:
        return self._get_active_track().cuts

    @cuts.setter
    def cuts(self, v: list[Segment]) -> None:
        self._get_active_track().cuts = v

    @property
    def keeps(self) -> list[Segment]:
        return self._get_active_track().keeps

    @keeps.setter
    def keeps(self, v: list[Segment]) -> None:
        self._get_active_track().keeps = v

    @property
    def cuts_enabled(self) -> bool:
        return bool(self._get_active_track().cuts_enabled)

    @cuts_enabled.setter
    def cuts_enabled(self, v: bool) -> None:
        self._get_active_track().cuts_enabled = bool(v)

    @property
    def manual_cuts(self) -> list[Segment]:
        return self._get_active_track().manual_cuts

    @manual_cuts.setter
    def manual_cuts(self, v: list[Segment]) -> None:
        self._get_active_track().manual_cuts = v

    @property
    def suppressed_cuts(self) -> list[Segment]:
        return self._get_active_track().suppressed_cuts

    @suppressed_cuts.setter
    def suppressed_cuts(self, v: list[Segment]) -> None:
        self._get_active_track().suppressed_cuts = v

    @property
    def _undo_stack(self) -> list[tuple[list[Segment], list[Segment]]]:
        return self._get_active_track().undo_stack

    @_undo_stack.setter
    def _undo_stack(self, v: list[tuple[list[Segment], list[Segment]]]) -> None:
        self._get_active_track().undo_stack = v

    @property
    def _redo_stack(self) -> list[tuple[list[Segment], list[Segment]]]:
        return self._get_active_track().redo_stack

    @_redo_stack.setter
    def _redo_stack(self, v: list[tuple[list[Segment], list[Segment]]]) -> None:
        self._get_active_track().redo_stack = v

    @property
    def _pending_cut_start(self) -> float | None:
        return self._get_active_track().pending_cut_start

    @_pending_cut_start.setter
    def _pending_cut_start(self, v: float | None) -> None:
        self._get_active_track().pending_cut_start = v

    @property
    def _pending_cut_end(self) -> float | None:
        return self._get_active_track().pending_cut_end

    @_pending_cut_end.setter
    def _pending_cut_end(self, v: float | None) -> None:
        self._get_active_track().pending_cut_end = v

    @property
    def _rms_min(self) -> float:
        return float(self._get_active_track().rms_min)

    @_rms_min.setter
    def _rms_min(self, v: float) -> None:
        self._get_active_track().rms_min = float(v)

    @property
    def _rms_max(self) -> float:
        return float(self._get_active_track().rms_max)

    @_rms_max.setter
    def _rms_max(self, v: float) -> None:
        self._get_active_track().rms_max = float(v)

    @property
    def _rms_eps(self) -> float:
        return float(self._get_active_track().rms_eps)

    @_rms_eps.setter
    def _rms_eps(self, v: float) -> None:
        self._get_active_track().rms_eps = float(v)

    def _track_label(self, idx: int, track: TrackState) -> str:
        if track.path:
            name = self._segment_display_name(track) or Path(track.path).name
            return f"A{idx + 1}  {name}"
        return f"A{idx + 1}"

    def _video_label(self, idx: int, track: TrackState) -> str:
        if track.path:
            name = self._segment_display_name(track) or Path(track.path).name
            return f"V{idx + 1}  {name}"
        return f"V{idx + 1}"

    def _ensure_segment_meta(self, track: TrackState) -> None:
        if track.segment_group_id is None:
            track.segment_group_id = uuid.uuid4().hex
        if not getattr(track, "segment_index", None):
            track.segment_index = 1
        if getattr(track, "segment_source_in", None) is None:
            track.segment_source_in = 0.0
        if getattr(track, "segment_source_out", None) is None:
            track.segment_source_out = 0.0

    def _segment_bounds(self, track: TrackState) -> tuple[float, float, float]:
        s_in = float(getattr(track, "segment_source_in", 0.0) or 0.0)
        s_out = float(getattr(track, "segment_source_out", 0.0) or 0.0)
        if s_out <= s_in + 1e-6:
            # fallback: try clip source bounds
            clip = None
            try:
                clip = self._find_clip(track.video_clip_id) or self._find_clip(track.audio_clip_id)
            except Exception:
                clip = None
            if clip is not None:
                s_in = float(getattr(clip, "source_in", 0.0) or 0.0)
                s_out = float(getattr(clip, "source_out", 0.0) or 0.0)
            if s_out <= s_in + 1e-6:
                s_in = 0.0
                s_out = float(track.duration or 0.0)
        seg_len = max(0.0, s_out - s_in)
        return s_in, s_out, seg_len

    def _segment_rms(self, track: TrackState) -> Optional[np.ndarray]:
        rms = track.rms
        if rms is None:
            return None
        s_in, s_out, _ = self._segment_bounds(track)
        hop = float(getattr(track, "hop_s", 0.03) or 0.03)
        if hop <= 0:
            return rms
        i0 = int(max(0.0, s_in) / hop)
        i1 = int(max(0.0, s_out) / hop)
        if i1 <= i0 or i0 >= getattr(rms, "size", 0):
            return rms
        return rms[i0:i1]

    def _update_segment_rms_stats(self, track: TrackState) -> None:
        seg_rms = self._segment_rms(track)
        if seg_rms is None or getattr(seg_rms, "size", 0) == 0:
            track.rms_min = 0.0
            track.rms_max = 0.0
            track.rms_eps = 1e-9
            return
        try:
            rmin = float(np.min(seg_rms))
            rmax = float(np.max(seg_rms))
        except Exception:
            track.rms_min = 0.0
            track.rms_max = 0.0
            track.rms_eps = 1e-9
            return
        rmin = max(0.0, rmin)
        rmax = max(rmin, rmax)
        track.rms_min = rmin
        track.rms_max = rmax
        track.rms_eps = max(1e-9, rmax * 0.001)

    def _segment_display_name(self, track: TrackState) -> str:
        if not track.path:
            return ""
        name = Path(track.path).name
        try:
            cidx = int(getattr(track, "copy_index", 0) or 0)
        except Exception:
            cidx = 0
        if cidx > 0:
            if cidx == 1:
                return f"{name} copy"
            return f"{name} copy {cidx}"
        try:
            idx = int(getattr(track, "segment_index", 1))
        except Exception:
            idx = 1
        if idx > 1:
            name = f"{name} pt {idx}"
        return name

    def _dbg_segments(self, msg: str, force: bool = False) -> None:
        if not getattr(self, "_debug_segments", False):
            return
        now = time.monotonic()
        if force or (now - getattr(self, "_debug_last_ts", 0.0)) >= float(getattr(self, "_debug_min_interval", 0.15)):
            self._debug_last_ts = now
            try:
                print(f"[segdbg] {msg}", flush=True)
            except Exception:
                pass

    def _session_logs_dir(self) -> Path:
        d = logs_root()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _prune_session_logs(self, keep: int = 10) -> None:
        try:
            files = sorted(
                self._session_logs_dir().glob("session_*.log"),
                key=lambda p: ((p.stat().st_mtime if p.exists() else 0.0), p.name),
            )
        except Exception:
            return
        extra = max(0, len(files) - max(1, int(keep)))
        for p in files[:extra]:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    def _init_session_logging(self) -> None:
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pid = os.getpid()
            path = self._session_logs_dir() / f"session_{stamp}_{pid}.log"
            self._session_log_path = path
            self._app_log(
                "app_start",
                pid=pid,
                cwd=str(Path.cwd()),
                python=sys.version.split()[0],
                platform=sys.platform,
                argv=list(sys.argv),
            )
            self._prune_session_logs(keep=10)
        except Exception:
            self._session_log_enabled = False

    def _app_log(self, event: str, **fields: object) -> None:
        if not bool(getattr(self, "_session_log_enabled", True)):
            return
        p = getattr(self, "_session_log_path", None)
        if p is None:
            return
        row: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": str(event),
        }
        for k, v in (fields or {}).items():
            try:
                json.dumps(v)
                row[str(k)] = v
            except Exception:
                row[str(k)] = str(v)
        line = redact_secrets(json.dumps(row, ensure_ascii=False))
        try:
            with self._session_log_lock:
                with p.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def _media_debug_logs_dir(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base:
            base = str(Path.home() / ".auto_cutter")
        d = Path(base) / "debug_logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _media_debug_log_path(self) -> Path:
        return self._media_debug_logs_dir() / "startup_media_debug.log"

    def _media_dbg(self, msg: str, *, throttle_s: float = 0.0) -> None:
        if not bool(getattr(self, "_media_debug_enabled", False)):
            return
        now = time.monotonic()
        if throttle_s > 0.0:
            last = float(getattr(self, "_media_debug_last_ts", 0.0) or 0.0)
            if (now - last) < float(throttle_s):
                return
            self._media_debug_last_ts = now
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[mediadbg {ts}] {msg}"
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            p = self._media_debug_log_path()
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _mp_state_name(state: object) -> str:
        try:
            if state == QMediaPlayer.PlayingState:
                return "playing"
            if state == QMediaPlayer.PausedState:
                return "paused"
            if state == QMediaPlayer.StoppedState:
                return "stopped"
        except Exception:
            pass
        return str(state)

    @staticmethod
    def _mp_status_name(status: object) -> str:
        names = {
            getattr(QMediaPlayer, "NoMedia", None): "NoMedia",
            getattr(QMediaPlayer, "LoadingMedia", None): "LoadingMedia",
            getattr(QMediaPlayer, "LoadedMedia", None): "LoadedMedia",
            getattr(QMediaPlayer, "StalledMedia", None): "StalledMedia",
            getattr(QMediaPlayer, "BufferingMedia", None): "BufferingMedia",
            getattr(QMediaPlayer, "BufferedMedia", None): "BufferedMedia",
            getattr(QMediaPlayer, "EndOfMedia", None): "EndOfMedia",
            getattr(QMediaPlayer, "InvalidMedia", None): "InvalidMedia",
        }
        try:
            name = names.get(status)
            if name:
                return name
        except Exception:
            pass
        return str(status)

    @staticmethod
    def _norm_local_path(p: object) -> str:
        try:
            s = str(p or "").strip()
        except Exception:
            return ""
        if not s:
            return ""
        try:
            return os.path.normcase(os.path.normpath(s))
        except Exception:
            return s

    @classmethod
    def _same_local_path(cls, a: object, b: object) -> bool:
        na = cls._norm_local_path(a)
        nb = cls._norm_local_path(b)
        return bool(na and nb and na == nb)

    def _media_debug_snapshot(self, tag: str) -> None:
        try:
            vsrc = self.video_player.source().toLocalFile()
        except Exception:
            vsrc = ""
        try:
            asrc = self.audio_player.source().toLocalFile()
        except Exception:
            asrc = ""
        try:
            vpos = int(self.video_player.position() or 0)
        except Exception:
            vpos = -1
        try:
            apos = int(self.audio_player.position() or 0)
        except Exception:
            apos = -1
        try:
            vstate = self._mp_state_name(self.video_player.playbackState())
        except Exception:
            vstate = "?"
        try:
            astate = self._mp_state_name(self.audio_player.playbackState())
        except Exception:
            astate = "?"
        try:
            avol = float(self.audio_output.volume())
        except Exception:
            avol = -1.0
        try:
            amute = bool(self.audio_output.isMuted())
        except Exception:
            amute = False
        self._media_dbg(
            f"{tag} vsrc='{Path(vsrc).name if vsrc else ''}' asrc='{Path(asrc).name if asrc else ''}' "
            f"vstate={vstate} astate={astate} vpos={vpos} apos={apos} avol={avol:.2f} amute={amute} "
            f"tracks={len(getattr(self, '_tracks', []) or [])} active={getattr(self, '_active_track_index', -1)}"
        )

    def _set_audio_player_source(self, path: str | None) -> None:
        req = str(path or "")
        if req:
            self.audio_player.setSource(QUrl.fromLocalFile(req))
        else:
            self.audio_player.setSource(QUrl())
        try:
            self._audio_requested_source_path = req
            self._audio_requested_source_ts = time.monotonic()
        except Exception:
            pass

    def _arm_play_guard(self, seconds: float, reason: str = "") -> None:
        try:
            until = time.monotonic() + max(0.0, float(seconds))
        except Exception:
            until = 0.0
        try:
            self._play_guard_until_ts = max(float(getattr(self, "_play_guard_until_ts", 0.0) or 0.0), until)
        except Exception:
            self._play_guard_until_ts = until
        self._play_guard_reason = str(reason or "Loading media...")
        try:
            delay_ms = max(1, int(max(0.0, self._play_guard_until_ts - time.monotonic()) * 1000.0) + 30)
            QTimer.singleShot(delay_ms, self._refresh_play_button_state)
        except Exception:
            pass
        self._refresh_play_button_state()

    def _is_play_guard_active(self) -> bool:
        try:
            return time.monotonic() < float(getattr(self, "_play_guard_until_ts", 0.0) or 0.0)
        except Exception:
            return False

    def _video_media_ready_for_play(self) -> bool:
        status = getattr(self, "_video_last_media_status", None)
        return status in (
            getattr(QMediaPlayer, "LoadedMedia", None),
            getattr(QMediaPlayer, "BufferedMedia", None),
            getattr(QMediaPlayer, "BufferingMedia", None),
            getattr(QMediaPlayer, "EndOfMedia", None),
        )

    def _can_start_playback_now(self) -> tuple[bool, str]:
        try:
            if self.video_player.playbackState() == QMediaPlayer.PlayingState:
                return True, ""
        except Exception:
            pass
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return False, "Reset in progress..."
        if bool(getattr(self, "_export_processing", False)):
            return False, "Export in progress..."
        if bool(getattr(self, "_ai_processing", False)):
            return False, "AI processing..."
        if not bool(self.input_path):
            return False, "No video loaded"
        if self._is_play_guard_active():
            try:
                rem = max(0.0, float(self._play_guard_until_ts) - time.monotonic())
            except Exception:
                rem = 0.0
            reason = str(getattr(self, "_play_guard_reason", "") or "Preparing playback...")
            if rem > 0.05:
                return False, f"{reason} ({rem:.1f}s)"
            return False, reason
        if not self._video_media_ready_for_play():
            return False, "Video still loading..."
        return True, ""

    def _refresh_play_button_state(self) -> None:
        ok, reason = self._can_start_playback_now()
        try:
            if hasattr(self, "btn_play") and self.btn_play is not None:
                self.btn_play.setEnabled(bool(ok))
                self.btn_play.setToolTip("Play / Pause" if ok else f"Play unavailable: {reason}")
        except Exception:
            pass
        try:
            self._web_js(
                self.web_transport,
                f"uiSetPlayEnabled({json.dumps(bool(ok))}, {json.dumps(reason)});",
            )
        except Exception:
            pass

    def _renumber_segment_groups(self) -> None:
        # Keep existing pt numbering stable across swaps.
        # Only assign numbers for missing/invalid or duplicate indices.
        groups: dict[str, list[int]] = {}
        for i, t in enumerate(self._tracks):
            gid = getattr(t, "segment_group_id", None)
            if not gid:
                continue
            groups.setdefault(str(gid), []).append(i)
        for _gid, indices in groups.items():
            counts: dict[int, int] = {}
            max_idx = 0
            # collect existing valid indices
            for i in indices:
                try:
                    idx = int(getattr(self._tracks[i], "segment_index", 0) or 0)
                except Exception:
                    idx = 0
                if idx > 0:
                    counts[idx] = counts.get(idx, 0) + 1
                    if idx > max_idx:
                        max_idx = idx
            # assign for missing/duplicate (keep first occurrence)
            seen: set[int] = set()
            for i in indices:
                try:
                    idx = int(getattr(self._tracks[i], "segment_index", 0) or 0)
                except Exception:
                    idx = 0
                if idx > 0 and counts.get(idx, 0) >= 1 and idx not in seen:
                    seen.add(idx)
                    continue
                # missing or duplicate: assign next
                max_idx += 1
                try:
                    self._tracks[i].segment_index = int(max_idx)
                except Exception:
                    pass
                seen.add(max_idx)

    def _next_video_color(self) -> tuple[tuple[int, int, int, int], tuple[int, int, int]]:
        palette = getattr(self, "_video_color_palette", None)
        if not palette:
            palette = [((70, 140, 90, 130), (90, 170, 110))]
        idx = int(getattr(self, "_video_color_cursor", 0)) % len(palette)
        fill, edge = palette[idx]
        self._video_color_cursor = (idx + 1) % len(palette)
        return tuple(fill), tuple(edge)

    def _assign_video_color(self, track: TrackState) -> None:
        if track is None:
            return
        if track.video_color is not None and track.video_edge is not None:
            return
        fill, edge = self._next_video_color()
        track.video_color = tuple(fill)
        track.video_edge = tuple(edge)

    def _ensure_video_track_for_state(self, track: TrackState) -> None:
        self._project_session.ensure_track_for_state(track, "video")

    def _find_clip(self, clip_id: str | None) -> Optional[Clip]:
        return self._project_session.find_clip(clip_id)

    def _track_index_for_clip_id(self, clip_id: str | None) -> Optional[int]:
        if not clip_id:
            return None
        for i, t in enumerate(self._tracks):
            if clip_id == t.audio_clip_id or clip_id == t.video_clip_id:
                return i
        return None

    def _ensure_audio_track_for_state(self, track: TrackState) -> None:
        self._project_session.ensure_track_for_state(track, "audio")

    def _add_media_to_project(self, path: str, track: TrackState) -> None:
        self._ensure_segment_meta(track)
        if float(getattr(track, "segment_source_out", 0.0) or 0.0) <= 0.0:
            track.segment_source_in = 0.0
            track.segment_source_out = float(track.duration or 0.0)
        self._assign_video_color(track)
        self._project_session.add_media_for_state(path, track)

    def _rebuild_sequential_timeline(self) -> None:
        """
        Rebuilds timeline positions so all media are concatenated in order.
        Video + audio clips for each TrackState are aligned on the same timeline.
        """
        t_cursor = 0.0
        for t in self._tracks:
            if t.media_id is None:
                continue
            self._ensure_video_track_for_state(t)
            self._ensure_audio_track_for_state(t)

            clip_list: list[Clip] = []
            for tid in (t.video_track_id, t.audio_track_id):
                if not tid:
                    continue
                tr = self.project.get_track(str(tid))
                if tr is None:
                    continue
                clip_list.extend(list(tr.clips))

            if not clip_list:
                t_cursor += max(0.0, float(t.duration or 0.0))
                continue

            cur_start = min(float(c.timeline_in) for c in clip_list)
            cur_end = max(float(c.timeline_out) for c in clip_list)
            delta = float(t_cursor) - float(cur_start)
            if abs(delta) > 1e-6:
                for c in clip_list:
                    c.timeline_in = float(c.timeline_in) + delta
                    c.timeline_out = float(c.timeline_out) + delta

            # Timeline packing must follow actual clip span, not stale saved duration.
            span = max(0.0, float(cur_end) - float(cur_start))
            if span <= 0.0:
                span = max(0.0, float(t.duration or 0.0))
            t_cursor += float(span)

    def _collect_global_keeps(self) -> list[Segment]:
        """
        Collect keeps on the real project timeline.
        This uses clip timeline positions, so it stays correct with duplicates,
        deletions and restored sessions.
        """
        keeps: list[Segment] = []
        eps = 1e-6
        total = float(self.project.timeline_duration() or self._global_duration or self.duration or 0.0)

        def _effective_track_keeps(t: TrackState) -> list[Segment]:
            # Prefer computed keeps when available.
            out: list[Segment] = []
            seg_span = 0.0
            try:
                si = float(getattr(t, "segment_source_in", 0.0) or 0.0)
                so = float(getattr(t, "segment_source_out", 0.0) or 0.0)
                if so > si + eps:
                    seg_span = max(0.0, so - si)
            except Exception:
                seg_span = 0.0
            dur = float(seg_span if seg_span > eps else max(0.0, float(getattr(t, "duration", 0.0) or 0.0)))

            raw_keeps = list(getattr(t, "keeps", None) or [])
            if raw_keeps:
                clean_keeps: list[Segment] = []
                for k in raw_keeps:
                    try:
                        a = float(k.start)
                        b = float(k.end)
                    except Exception:
                        continue
                    if b > a + eps:
                        clean_keeps.append(Segment(a, b))
                out = merge_overlaps(sorted(clean_keeps, key=lambda s: s.start))
                if out:
                    return out

            # If cuts are disabled, full clip is kept.
            if not bool(getattr(t, "cuts_enabled", False)):
                return [Segment(0.0, dur)] if dur > eps else []

            # Rebuild keeps from cuts when keeps are missing/stale (common after restore).
            cuts = merge_overlaps(list(getattr(t, "cuts", None) or []))
            if not cuts:
                manual = merge_overlaps(list(getattr(t, "manual_cuts", None) or []))
                suppressed = merge_overlaps(list(getattr(t, "suppressed_cuts", None) or []))
                if suppressed and manual:
                    filtered: list[Segment] = []
                    for c in manual:
                        if any(self._overlaps(c, s) for s in suppressed):
                            continue
                        filtered.append(c)
                    cuts = filtered
                else:
                    cuts = manual

            if not cuts:
                return [Segment(0.0, dur)] if dur > eps else []

            if dur <= eps:
                return []
            clamped_cuts: list[Segment] = []
            for c in cuts:
                try:
                    a = max(0.0, float(c.start))
                    b = max(0.0, float(c.end))
                except Exception:
                    continue
                if dur > eps:
                    a = min(a, dur)
                    b = min(b, dur)
                if b > a + eps:
                    clamped_cuts.append(Segment(a, b))
            if not clamped_cuts:
                return [Segment(0.0, dur)] if dur > eps else []
            return invert_to_keeps(float(dur), clamped_cuts, min_keep=0.0)

        def _track_keeps_domain(t: TrackState, track_keeps: list[Segment], clip_list: list[Clip]) -> str:
            # We normally store keeps in track-local time (0..track.duration).
            # Old/restored payloads may contain absolute source-domain keeps.
            if not track_keeps:
                return "local"
            try:
                ks_min = min(float(s.start) for s in track_keeps)
                ke_max = max(float(s.end) for s in track_keeps)
            except Exception:
                return "local"

            src_min = None
            src_max = None
            span_max = 0.0
            for c in clip_list:
                try:
                    s_in = float(c.source_in)
                    s_out = float(c.source_out)
                except Exception:
                    continue
                if s_out <= s_in + eps:
                    continue
                span_max = max(span_max, s_out - s_in)
                src_min = s_in if src_min is None else min(src_min, s_in)
                src_max = s_out if src_max is None else max(src_max, s_out)

            if span_max > eps and ks_min >= -eps and ke_max <= (span_max + eps):
                return "local"
            if src_min is not None and src_max is not None:
                if ks_min >= (float(src_min) - eps) and ke_max <= (float(src_max) + eps):
                    return "absolute"

            try:
                seg_in = float(getattr(t, "segment_source_in", 0.0) or 0.0)
                seg_out = float(getattr(t, "segment_source_out", 0.0) or 0.0)
            except Exception:
                seg_in = 0.0
                seg_out = 0.0
            if (seg_out > seg_in + eps) and ks_min >= (seg_in - eps) and ke_max <= (seg_out + eps):
                return "absolute"
            return "local"

        for t in self._tracks:
            vclips: list[Clip] = []
            aclips: list[Clip] = []

            if t.video_track_id:
                vtr = self.project.get_track(str(t.video_track_id))
                if vtr is not None:
                    vclips = list(vtr.sorted_clips())
            if t.audio_track_id:
                atr = self.project.get_track(str(t.audio_track_id))
                if atr is not None:
                    aclips = list(atr.sorted_clips())

            clip_list = vclips if vclips else aclips
            if not clip_list:
                continue

            cuts_on = bool(getattr(t, "cuts_enabled", False))
            track_keeps = _effective_track_keeps(t)
            keeps_domain = _track_keeps_domain(t, track_keeps, clip_list)

            for c in clip_list:
                try:
                    t_in = float(c.timeline_in)
                    t_out = float(c.timeline_out)
                    s_in = float(c.source_in)
                    s_out = float(c.source_out)
                except Exception:
                    continue

                tl_len = max(0.0, t_out - t_in)
                src_len = max(0.0, s_out - s_in)
                if tl_len <= eps:
                    continue

                # No active cuts -> keep full clip on timeline.
                if not cuts_on:
                    keeps.append(Segment(t_in, t_out))
                    continue
                # Active cuts but empty keeps -> this clip contributes no output.
                if not track_keeps:
                    continue

                # Map keeps to timeline domain.
                # Track keeps are local in normal flow; absolute support stays for old restores.
                local_len = src_len if src_len > eps else tl_len
                if local_len <= eps:
                    continue
                scale = (tl_len / local_len) if local_len > eps else 1.0
                for k in track_keeps:
                    try:
                        ks = float(k.start)
                        ke = float(k.end)
                    except Exception:
                        continue
                    if keeps_domain == "absolute":
                        os = max(ks, s_in)
                        oe = min(ke, s_out)
                        if oe <= os + eps:
                            continue
                        rel_s = os - s_in
                        rel_e = oe - s_in
                    else:
                        os = max(ks, 0.0)
                        oe = min(ke, local_len)
                        if oe <= os + eps:
                            continue
                        rel_s = os
                        rel_e = oe
                    if oe <= os + eps:
                        continue
                    a = t_in + rel_s * scale
                    b = t_in + rel_e * scale
                    if b > a + eps:
                        keeps.append(Segment(a, b))

        if not keeps:
            return []

        cleaned: list[Segment] = []
        for s in keeps:
            a = max(0.0, float(s.start))
            b = max(0.0, float(s.end))
            if total > 0.0:
                a = min(a, total)
                b = min(b, total)
            if b > a + eps:
                cleaned.append(Segment(a, b))

        return merge_overlaps(sorted(cleaned, key=lambda s: s.start))

    def _sync_project_from_track(self, track: TrackState) -> None:
        self._project_session.sync_state_to_project(track)

    def _finalize_timeline_reorder(self) -> None:
        self._rebuild_sequential_timeline()
        self._renumber_segment_groups()
        self._refresh_timeline_tracks(reset_view=False)
        total = self._global_duration if self._global_duration > 0 else self.duration
        self.seek.setRange(0, int(total * 1000))
        self._update_time_label(self.seek.value() / 1000.0)
        self._update_active_track_label()
        # Keep playback in sync with the current playhead after reordering.
        try:
            t = float(getattr(self.timeline, "playhead", 0.0) or getattr(self, "_last_pos", 0.0) or 0.0)
        except Exception:
            t = 0.0
        try:
            self._set_all_positions(int(t * 1000))
        except Exception:
            pass

    def _swap_tracks(self, idx_a: int, idx_b: int) -> None:
        if idx_a == idx_b:
            return
        if idx_a < 0 or idx_b < 0:
            return
        if idx_a >= len(self._tracks) or idx_b >= len(self._tracks):
            return
        self._tracks[idx_a], self._tracks[idx_b] = self._tracks[idx_b], self._tracks[idx_a]
        if self._active_track_index == idx_a:
            self._active_track_index = idx_b
        elif self._active_track_index == idx_b:
            self._active_track_index = idx_a
        self._finalize_timeline_reorder()

    def _move_track_to_index(self, from_idx: int, to_idx: int) -> None:
        if from_idx < 0 or from_idx >= len(self._tracks):
            return
        track = self._tracks.pop(from_idx)
        to_idx = max(0, min(int(to_idx), len(self._tracks)))
        self._tracks.insert(to_idx, track)

        if self._active_track_index == from_idx:
            self._active_track_index = to_idx
        else:
            if from_idx < self._active_track_index <= to_idx:
                self._active_track_index -= 1
            elif to_idx <= self._active_track_index < from_idx:
                self._active_track_index += 1

        self._finalize_timeline_reorder()

    def _move_track_by_time(self, track_idx: int, new_start: float) -> None:
        if track_idx < 0 or track_idx >= len(self._tracks):
            return
        order = [i for i in range(len(self._tracks)) if i != track_idx]
        cursor = 0.0
        target = len(order)
        for pos, idx in enumerate(order):
            dur = max(0.0, float(self._tracks[idx].duration))
            if new_start < (cursor + (dur * 0.5)):
                target = pos
                break
            cursor += dur
        self._move_track_to_index(track_idx, target)

    def _move_media_clips(self, media_id: str, new_start: float, ref_clip: Clip | None = None) -> None:
        idx = None
        for i, t in enumerate(self._tracks):
            if t.media_id == media_id:
                idx = i
                break
        if idx is None:
            return
        self._move_track_by_time(int(idx), float(new_start))

    def _on_clip_move_requested(self, clip_id: str, new_start: float) -> None:
        # Sequential mode: free move disabled. Only swaps are allowed.
        return

    def _on_clip_swap_requested(self, clip_id: str, target_clip_id: str) -> None:
        clip_a = self._find_clip(clip_id)
        clip_b = self._find_clip(target_clip_id)
        if clip_a is None or clip_b is None:
            return
        if clip_a.id == clip_b.id:
            return

        def _track_index_for_clip(clip: Clip) -> int | None:
            tid = str(clip.track_id)
            for i, ts in enumerate(self._tracks):
                if str(ts.video_track_id) == tid or str(ts.audio_track_id) == tid:
                    return i
            return None

        idx_a = _track_index_for_clip(clip_a)
        idx_b = _track_index_for_clip(clip_b)
        if idx_a is None or idx_b is None:
            return
        if idx_a == idx_b:
            return

        self._swap_tracks(int(idx_a), int(idx_b))

    @staticmethod
    def _clone_segments_list(segs: list[Segment] | None) -> list[Segment]:
        out: list[Segment] = []
        if not segs:
            return out
        for s in segs:
            try:
                a = float(getattr(s, "start", 0.0))
                b = float(getattr(s, "end", 0.0))
            except Exception:
                continue
            if b > a:
                out.append(Segment(a, b))
        return out

    def _clone_track_state_for_history(self, src: TrackState) -> TrackState:
        t = copy.copy(src)
        t.cuts = self._clone_segments_list(getattr(src, "cuts", []))
        t.keeps = self._clone_segments_list(getattr(src, "keeps", []))
        t.manual_cuts = self._clone_segments_list(getattr(src, "manual_cuts", []))
        t.suppressed_cuts = self._clone_segments_list(getattr(src, "suppressed_cuts", []))
        t.classic_cuts = self._clone_segments_list(getattr(src, "classic_cuts", []))
        t.classic_keeps = self._clone_segments_list(getattr(src, "classic_keeps", []))
        t.classic_manual_cuts = self._clone_segments_list(getattr(src, "classic_manual_cuts", []))
        t.classic_suppressed_cuts = self._clone_segments_list(getattr(src, "classic_suppressed_cuts", []))
        t.classic_cuts_enabled = bool(getattr(src, "classic_cuts_enabled", False))
        t.ai_cuts = self._clone_segments_list(getattr(src, "ai_cuts", []))
        t.ai_keeps = self._clone_segments_list(getattr(src, "ai_keeps", []))
        t.ai_manual_cuts = self._clone_segments_list(getattr(src, "ai_manual_cuts", []))
        t.ai_suppressed_cuts = self._clone_segments_list(getattr(src, "ai_suppressed_cuts", []))
        t.ai_cuts_enabled = bool(getattr(src, "ai_cuts_enabled", False))
        t.ai_speech = self._clone_segments_list(getattr(src, "ai_speech", []))
        t.ai_speech_raw = self._clone_segments_list(getattr(src, "ai_speech_raw", []))
        src_speakers = getattr(src, "ai_speaker_ids", None)
        t.ai_speaker_ids = list(src_speakers) if isinstance(src_speakers, list) else None
        t.cfg = dict(getattr(src, "cfg", {}) or {})
        t.undo_stack = []
        t.redo_stack = []
        # Pending cut markers are ephemeral UI state; do not carry them in timeline undo snapshots.
        t.pending_cut_start = None
        t.pending_cut_end = None
        if getattr(src, "video_color", None) is not None:
            try:
                t.video_color = tuple(src.video_color)
            except Exception:
                t.video_color = src.video_color
        if getattr(src, "video_edge", None) is not None:
            try:
                t.video_edge = tuple(src.video_edge)
            except Exception:
                t.video_edge = src.video_edge
        return t

    def _snapshot_cut_edit_state(self, track_idx: int | None = None) -> dict:
        if not self._tracks:
            return {
                "kind": "cuts",
                "track_idx": 0,
                "manual": [],
                "suppressed": [],
                "cuts": [],
                "keeps": [],
                "cuts_enabled": False,
                "pending_start": None,
                "pending_end": None,
            }
        idx = int(self._active_track_index if track_idx is None else track_idx)
        idx = max(0, min(idx, len(self._tracks) - 1))
        track = self._tracks[idx]
        # Pending cut markers are transient UI state. Do not persist them in cut undo/redo snapshots,
        # otherwise Ctrl+Z after creating a cut restores the yellow pending markers instead of fully
        # reverting the cut action.
        return {
            "kind": "cuts",
            "track_idx": int(idx),
            "manual": self._clone_segments_list(getattr(track, "manual_cuts", [])),
            "suppressed": self._clone_segments_list(getattr(track, "suppressed_cuts", [])),
            "cuts": self._clone_segments_list(getattr(track, "cuts", [])),
            "keeps": self._clone_segments_list(getattr(track, "keeps", [])),
            "cuts_enabled": bool(getattr(track, "cuts_enabled", True)),
            "pending_start": None,
            "pending_end": None,
        }

    def _snapshot_timeline_edit_state(self) -> dict:
        try:
            playhead = float(getattr(self.timeline, "playhead", 0.0) or 0.0)
        except Exception:
            playhead = float(getattr(self, "_last_pos", 0.0) or 0.0)
        return {
            "kind": "timeline",
            "tracks": [self._clone_track_state_for_history(t) for t in self._tracks],
            "project": copy.deepcopy(self.project),
            "active_track_index": int(self._active_track_index),
            "playhead": float(playhead),
        }

    def _restore_cut_edit_state(self, state: dict) -> None:
        if not self._tracks:
            return
        idx = int(state.get("track_idx", self._active_track_index))
        idx = max(0, min(idx, len(self._tracks) - 1))
        track = self._tracks[idx]
        track.manual_cuts = self._clone_segments_list(state.get("manual"))
        track.suppressed_cuts = self._clone_segments_list(state.get("suppressed"))
        track.cuts_enabled = bool(state.get("cuts_enabled", True))
        # Cut undo/redo intentionally clears pending markers (ephemeral UI state).
        track.pending_cut_start = None
        track.pending_cut_end = None

        restored_exact = False
        try:
            cuts_snap = state.get("cuts", None)
            keeps_snap = state.get("keeps", None)
            if cuts_snap is not None:
                track.cuts = self._clone_segments_list(cuts_snap)
                if keeps_snap is not None:
                    track.keeps = self._clone_segments_list(keeps_snap)
                else:
                    try:
                        track.keeps = invert_to_keeps(float(track.duration), list(track.cuts or []), min_keep=0.0)
                    except Exception:
                        track.keeps = []
                restored_exact = True
        except Exception:
            restored_exact = False

        if not restored_exact:
            try:
                self._compute_cuts_for_track(idx)
            except Exception:
                pass

        try:
            self._save_workspace_for_mode(track)
        except Exception:
            pass

        self._active_track_index = int(idx)
        self._update_active_track_label()
        self._refresh_timeline_tracks(reset_view=False)
        try:
            self.timeline.setActiveTrack(self._timeline_index_for_audio_track(self._active_track_index), emit=False)
        except Exception:
            pass
        self._set_pending_cut_visual(
            track.pending_cut_start,
            track.pending_cut_end,
            track_state_idx=idx,
        )
        try:
            seg_rms = self._segment_rms(track) if track.rms is not None else None
            self.threshold_meter.set_reference_from_rms(seg_rms)
            self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
            self._refresh_threshold_ui_state()
        except Exception:
            pass
        try:
            self._apply_track_cuts_ui(track)
        except Exception:
            pass
        self._update_ai_stats_panel(track)
        self._web_push_full_state()

    def _restore_timeline_edit_state(self, state: dict) -> None:
        tracks_snap = state.get("tracks")
        project_snap = state.get("project")
        if not isinstance(tracks_snap, list) or project_snap is None:
            return

        self.project = copy.deepcopy(project_snap)
        restored_tracks: list[TrackState] = []
        for t in tracks_snap:
            if isinstance(t, TrackState):
                restored_tracks.append(self._clone_track_state_for_history(t))
        self._tracks = restored_tracks if restored_tracks else [TrackState()]

        idx = int(state.get("active_track_index", 0))
        self._active_track_index = max(0, min(idx, len(self._tracks) - 1))
        self._update_active_track_label()
        self._refresh_timeline_tracks(reset_view=False)
        try:
            self.timeline.setActiveTrack(self._timeline_index_for_audio_track(self._active_track_index), emit=False)
        except Exception:
            pass

        track = self._get_active_track()
        self._set_pending_cut_visual(
            track.pending_cut_start,
            track.pending_cut_end,
            track_state_idx=self._active_track_index,
        )

        try:
            total = float(self._global_duration if self._global_duration > 0 else track.duration)
        except Exception:
            total = 0.0
        try:
            t = float(state.get("playhead", 0.0) or 0.0)
        except Exception:
            t = 0.0
        if total > 0.0:
            t = max(0.0, min(t, total))
        else:
            t = 0.0
        try:
            self._set_all_positions(int(t * 1000))
        except Exception:
            pass
        try:
            self.seek.blockSignals(True)
            self.seek.setRange(0, int(total * 1000))
            self.seek.setValue(int(t * 1000))
            self.seek.blockSignals(False)
        except Exception:
            pass
        try:
            self.timeline.setPlayhead(float(t))
        except Exception:
            pass
        self._update_time_label(float(t))

        try:
            seg_rms = self._segment_rms(track) if track.rms is not None else None
            self.threshold_meter.set_reference_from_rms(seg_rms)
            self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
            self._refresh_threshold_ui_state()
        except Exception:
            pass
        try:
            self._apply_track_cuts_ui(track)
        except Exception:
            pass
        self._update_ai_stats_panel(track)
        self._update_split_button_state()
        self._web_push_full_state()

    def _duplicate_track_at_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._tracks):
            return

        src = self._tracks[idx]
        if not src.path and not src.media_id:
            return

        # Pause playback while mutating timeline structure.
        try:
            self._play_requested = False
            self.video_player.pause()
        except Exception:
            pass

        src_clip = self._find_clip(src.video_clip_id) or self._find_clip(src.audio_clip_id)

        src_in = float(getattr(src, "segment_source_in", 0.0) or 0.0)
        src_out = float(getattr(src, "segment_source_out", 0.0) or 0.0)
        if src_clip is not None and src_out <= src_in + 1e-6:
            src_in = float(getattr(src_clip, "source_in", 0.0) or 0.0)
            src_out = float(getattr(src_clip, "source_out", 0.0) or 0.0)
        if src_out <= src_in + 1e-6:
            src_out = src_in + max(0.0, float(getattr(src, "duration", 0.0) or 0.0))

        dur = max(0.0, src_out - src_in)
        if dur <= 1e-6 and src_clip is not None:
            dur = max(0.0, float(src_clip.timeline_out) - float(src_clip.timeline_in))
        if dur <= 1e-6:
            return

        self._push_undo_state(kind="timeline")

        media_id = src.media_id
        if (not media_id) and src.path:
            for mid, media in self.project.media.items():
                if str(getattr(media, "path", "")) == str(src.path):
                    media_id = mid
                    break
            if not media_id:
                media = self.project.add_media(str(src.path), name=Path(str(src.path)).name)
                media.duration = float(dur)
                media_id = media.id

        dup = TrackState()
        dup.path = src.path
        dup.media_id = media_id
        dup.duration = float(dur)
        dup.rms = src.rms
        dup.hop_s = float(getattr(src, "hop_s", 0.03) or 0.03)
        dup.cuts_enabled = bool(getattr(src, "cuts_enabled", False))
        dup.cuts = self._clone_segments_list(getattr(src, "cuts", []))
        dup.keeps = self._clone_segments_list(getattr(src, "keeps", []))
        dup.manual_cuts = self._clone_segments_list(getattr(src, "manual_cuts", []))
        dup.suppressed_cuts = self._clone_segments_list(getattr(src, "suppressed_cuts", []))
        dup.rms_min = float(getattr(src, "rms_min", 0.0) or 0.0)
        dup.rms_max = float(getattr(src, "rms_max", 0.0) or 0.0)
        dup.rms_eps = float(getattr(src, "rms_eps", 1e-9) or 1e-9)
        dup.cfg = dict(getattr(src, "cfg", {}) or {})
        dup.filters_restored = bool(getattr(src, "filters_restored", False))
        dup.cuts_restored = bool(getattr(src, "cuts_restored", False))
        dup.video_color = tuple(src.video_color) if getattr(src, "video_color", None) is not None else None
        dup.video_edge = tuple(src.video_edge) if getattr(src, "video_edge", None) is not None else None
        dup.segment_source_in = float(src_in)
        dup.segment_source_out = float(src_out)
        dup.classic_cuts = self._clone_segments_list(getattr(src, "classic_cuts", []))
        dup.classic_keeps = self._clone_segments_list(getattr(src, "classic_keeps", []))
        dup.classic_manual_cuts = self._clone_segments_list(getattr(src, "classic_manual_cuts", []))
        dup.classic_suppressed_cuts = self._clone_segments_list(getattr(src, "classic_suppressed_cuts", []))
        dup.classic_cuts_enabled = bool(getattr(src, "classic_cuts_enabled", False))
        dup.ai_cuts = self._clone_segments_list(getattr(src, "ai_cuts", []))
        dup.ai_keeps = self._clone_segments_list(getattr(src, "ai_keeps", []))
        dup.ai_manual_cuts = self._clone_segments_list(getattr(src, "ai_manual_cuts", []))
        dup.ai_suppressed_cuts = self._clone_segments_list(getattr(src, "ai_suppressed_cuts", []))
        dup.ai_cuts_enabled = bool(getattr(src, "ai_cuts_enabled", False))
        dup.ai_speech = self._clone_segments_list(getattr(src, "ai_speech", []))
        dup.ai_speech_raw = self._clone_segments_list(getattr(src, "ai_speech_raw", []))
        src_speakers = getattr(src, "ai_speaker_ids", None)
        dup.ai_speaker_ids = list(src_speakers) if isinstance(src_speakers, list) else None
        dup.undo_stack.clear()
        dup.redo_stack.clear()
        dup.pending_cut_start = None
        dup.pending_cut_end = None

        # Duplicates are labeled "copy", "copy 2", ... and must not become "pt N".
        copy_source_key = (
            getattr(src, "copy_source_id", None)
            or getattr(src, "segment_group_id", None)
            or getattr(src, "media_id", None)
            or getattr(src, "path", None)
            or uuid.uuid4().hex
        )
        max_copy_idx = 0
        for t in self._tracks:
            key_t = (
                getattr(t, "copy_source_id", None)
                or getattr(t, "segment_group_id", None)
                or getattr(t, "media_id", None)
                or getattr(t, "path", None)
            )
            if str(key_t or "") != str(copy_source_key):
                continue
            try:
                ci = int(getattr(t, "copy_index", 0) or 0)
            except Exception:
                ci = 0
            max_copy_idx = max(max_copy_idx, ci)
        dup.copy_source_id = str(copy_source_key)
        dup.copy_index = int(max_copy_idx + 1 if max_copy_idx >= 1 else 1)
        dup.segment_group_id = uuid.uuid4().hex
        dup.segment_index = 1

        insert_idx = idx + 1
        self._tracks.insert(insert_idx, dup)
        self._active_track_index = int(insert_idx)

        self._ensure_video_track_for_state(dup)
        self._ensure_audio_track_for_state(dup)

        start = float(getattr(src_clip, "timeline_out", 0.0) or 0.0) if src_clip is not None else float(self.project.timeline_duration() or 0.0)
        end = float(start + dur)
        link_id = uuid.uuid4().hex
        clip_name = self._segment_display_name(dup) or (Path(dup.path).name if dup.path else f"Track {insert_idx + 1}")

        if dup.video_track_id and dup.media_id:
            vclip = self.project.add_clip(
                track_id=str(dup.video_track_id),
                media_id=str(dup.media_id),
                link_id=link_id,
                source_in=float(src_in),
                source_out=float(src_out),
                timeline_in=float(start),
                timeline_out=float(end),
                name=clip_name,
                color=dup.video_color,
                edge=dup.video_edge,
            )
            dup.video_clip_id = vclip.id

        if dup.audio_track_id and dup.media_id:
            aclip = self.project.add_clip(
                track_id=str(dup.audio_track_id),
                media_id=str(dup.media_id),
                link_id=link_id,
                source_in=float(src_in),
                source_out=float(src_out),
                timeline_in=float(start),
                timeline_out=float(end),
                name=clip_name,
            )
            dup.audio_clip_id = aclip.id

        self._finalize_timeline_reorder()
        self._web_push_full_state()

    def _remove_track_at_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._tracks):
            return

        track = self._tracks[idx]
        name = Path(track.path).name if track and track.path else f"Track {idx + 1}"
        msg = QMessageBox.question(
            self,
            "Remove video",
            f"Remove '{name}' from the project?",
            QMessageBox.Yes | QMessageBox.No
        )
        if msg != QMessageBox.Yes:
            return

        if len(self._tracks) <= 1:
            self._reset_workspace(confirm=False)
            return

        self._push_undo_state(kind="timeline")

        # stop playback if needed
        try:
            self._play_requested = False
            self.video_player.pause()
        except Exception:
            pass

        # remove project tracks and clips
        for tid in (track.audio_track_id, track.video_track_id):
            if not tid:
                continue
            tr = self.project.get_track(str(tid))
            if tr is not None:
                try:
                    tr.clips = [c for c in tr.clips if c.media_id != track.media_id]
                except Exception:
                    pass
                try:
                    self.project.tracks.remove(tr)
                except ValueError:
                    pass

        # remove media
        media_id = track.media_id
        try:
            used_elsewhere = False
            if media_id:
                for t in self._tracks:
                    if t is track:
                        continue
                    if t.media_id == media_id:
                        used_elsewhere = True
                        break
            if media_id and (not used_elsewhere) and (media_id in self.project.media):
                del self.project.media[media_id]
        except Exception:
            pass

        # remove from state list
        self._tracks.pop(idx)

        if self._active_track_index > idx:
            self._active_track_index -= 1
        elif self._active_track_index == idx:
            self._active_track_index = max(0, min(idx, len(self._tracks) - 1))

        # ensure ids for active track
        if self._tracks:
            try:
                self._ensure_audio_track_for_state(self._tracks[self._active_track_index])
                self._ensure_video_track_for_state(self._tracks[self._active_track_index])
            except Exception:
                pass

        # Re-sequence to avoid timeline holes
        self._finalize_timeline_reorder()
        self._web_push_full_state()

    def _perform_workspace_reset(self) -> None:
        self._app_log("workspace_reset_perform_begin", had_input=bool(self.input_path), tracks=len(getattr(self, "_tracks", []) or []))
        self._workspace_resetting = True
        try:
            try:
                self._cancel_warm_cache(wait_ms=1200)
            except Exception:
                pass
            try:
                self._play_requested = False
                self.video_player.stop()
            except Exception:
                try:
                    self.video_player.pause()
                except Exception:
                    pass
            try:
                self.audio_player.stop()
            except Exception:
                try:
                    self.audio_player.pause()
                except Exception:
                    pass
            try:
                self.video_player.setSource(QUrl())
            except Exception:
                pass
            try:
                self._set_audio_player_source(None)
            except Exception:
                pass
            try:
                if hasattr(self, "video_widget") and self.video_widget is not None:
                    self.video_widget.clear_frame()
            except Exception:
                pass

            self._tracks = [TrackState()]
            self._active_track_index = 0
            self._analysis_threads.clear()
            self._analysis_workers.clear()
            self._analysis_expected_path.clear()
            self._analysis_job_ids.clear()
            self._analysis_targets.clear()
            self._analysis_progress.clear()
            self._ai_threads.clear()
            self._ai_workers.clear()
            self._ai_expected_path.clear()
            self._ai_processing = False
            self._video_color_cursor = 0
            self.project = Project.create_default("Untitled")
            self._project_file_path = None
            self._offline_project_items = []
            self._timeline_track_map = []
            self._video_segments = []
            self._video_segment_index = 0
            self._video_concat_duration = 0.0
            self._audio_segments = []
            self._audio_segment_index = -1
            self._audio_segments_track_idx = None
            self._pending_restore_filters = None

            if self._tracks:
                try:
                    self._ensure_audio_track_for_state(self._tracks[0])
                    self._ensure_video_track_for_state(self._tracks[0])
                except Exception:
                    pass

            self.input_path = None
            self._reset_analysis_state()
            self._set_stage("import")
            self._update_active_track_label()
            self._update_time_label(0.0)
            self._web_push_full_state()

            # Ensure recent files are cleared after a reset
            try:
                s = QSettings("Auto Cutter", "Auto Cutter")
                s.remove("last_input_path")
                s.remove("last_session_items")
            except Exception:
                pass
            try:
                self._set_export_processing(False)
            except Exception:
                self._export_processing = False
            self._export_abort_requested = False
        finally:
            self._workspace_resetting = False
            self._pending_workspace_reset_since = 0.0
            self._pending_workspace_reset_force_applied = False
            self._app_log("workspace_reset_perform_end", input_loaded=bool(self.input_path), tracks=len(getattr(self, "_tracks", []) or []))

    def _try_finalize_pending_workspace_reset(self) -> None:
        if not bool(getattr(self, "_pending_workspace_reset", False)):
            return
        # Keep requesting cancellation without blocking UI.
        self._abort_analysis_tasks(wait_ms=0)
        try:
            self._cancel_warm_cache(wait_ms=0)
        except Exception:
            pass
        try:
            self._abort_export(wait_ms=0)
        except Exception:
            pass

        analysis_running = bool(self._analysis_in_progress())
        export_running = bool(self._export_in_progress())
        if analysis_running or export_running:
            # Keep UI responsive while waiting for workers/threads to terminate.
            since = float(getattr(self, "_pending_workspace_reset_since", 0.0) or 0.0)
            now = time.monotonic()
            if since <= 0.0:
                self._pending_workspace_reset_since = now
            elapsed = max(0.0, now - self._pending_workspace_reset_since)
            force_after = float(getattr(self, "_pending_workspace_reset_force_after_s", 0.9) or 0.9)
            if (
                analysis_running
                and not export_running
                and (elapsed >= force_after)
                and not bool(getattr(self, "_pending_workspace_reset_force_applied", False))
            ):
                self._pending_workspace_reset_force_applied = True
                self._force_detach_analysis_for_reset()
                analysis_running = bool(self._analysis_in_progress())
                export_running = bool(self._export_in_progress())
                if not analysis_running and not export_running:
                    self._pending_workspace_reset = False
                    self._perform_workspace_reset()
                    return
            elif elapsed > 1.0:
                self.statusBar().showMessage("Waiting for background tasks to stop before reset...", 1500)
            QTimer.singleShot(120, self._try_finalize_pending_workspace_reset)
            return
        self._pending_workspace_reset = False
        self._perform_workspace_reset()

    def _reset_workspace(self, confirm: bool = True) -> None:
        self._app_log("workspace_reset_requested", confirm=bool(confirm), analysis_in_progress=bool(self._analysis_in_progress()))
        if confirm:
            msg = QMessageBox.question(
                self,
                "Reset workspace",
                "Clear all videos and reset the workspace?",
                QMessageBox.Yes | QMessageBox.No
            )
            if msg != QMessageBox.Yes:
                self._app_log("workspace_reset_cancelled_confirm")
                return

        if bool(getattr(self, "_pending_workspace_reset", False)):
            self._app_log("workspace_reset_ignored_already_pending")
            return

        self._pending_workspace_reset = True
        self._pending_workspace_reset_since = time.monotonic()
        self._pending_workspace_reset_force_applied = False
        self.statusBar().showMessage("Preparing safe reset...", 1800)
        try:
            self._abort_export(wait_ms=0)
        except Exception:
            pass
        self._abort_analysis_tasks(wait_ms=0)
        QTimer.singleShot(0, self._try_finalize_pending_workspace_reset)

    def reset_workspace(self) -> None:
        self._reset_workspace(confirm=True)

    def _refresh_timeline_tracks(self, reset_view: bool = False) -> None:
        tracks: list[dict] = []
        self._timeline_track_map = []

        # Project duration (timeline)
        project_dur = float(self.project.timeline_duration() or 0.0)
        audio_max = 0.0
        for t in self._tracks:
            try:
                audio_max = max(audio_max, float(t.duration))
            except Exception:
                pass

        # Use real timeline span when available.
        # Falling back to track duration only when no clips exist avoids loading stale
        # historical durations from remembered session state.
        self._global_duration = float(project_dur if project_dur > 0.0 else audio_max)

        def _as_rms(x):
            if x is not None and not isinstance(x, np.ndarray):
                try:
                    x = np.asarray(x, dtype=np.float32)
                except Exception:
                    x = None
            return x

        # Sequential mode: single video row + single audio row.
        vclips: list[dict] = []
        aclips: list[dict] = []

        for idx, t in enumerate(self._tracks):
            if not t.media_id and not t.path:
                continue

            self._assign_video_color(t)

            # Video clips
            self._ensure_video_track_for_state(t)
            if t.video_track_id:
                vtrack = self.project.get_track(str(t.video_track_id))
                if vtrack is not None:
                    for c in vtrack.sorted_clips():
                        clip_name = self._segment_display_name(t) or c.name
                        vclips.append(
                            {
                                "start": float(c.timeline_in),
                                "end": float(c.timeline_out),
                                "source_in": float(c.source_in),
                                "source_out": float(c.source_out),
                                "name": clip_name,
                                "kind": "video",
                                "clip_id": c.id,
                                "media_id": c.media_id,
                                "track_state_idx": idx,
                                "color": getattr(c, "color", None) or t.video_color,
                                "edge": getattr(c, "edge", None) or t.video_edge,
                            }
                        )

            # Audio clips
            self._ensure_audio_track_for_state(t)
            rms = _as_rms(t.rms)
            hop_s = float(t.hop_s)
            if t.audio_track_id:
                atrack = self.project.get_track(str(t.audio_track_id))
                if atrack is not None:
                    for c in atrack.sorted_clips():
                        aclips.append(
                            {
                                "start": float(c.timeline_in),
                                "end": float(c.timeline_out),
                                "source_in": float(c.source_in),
                                "source_out": float(c.source_out),
                                "name": c.name,
                                "kind": "audio",
                                "clip_id": c.id,
                                "media_id": c.media_id,
                                "track_state_idx": idx,
                                "cuts": t.cuts,
                                "rms": rms,
                                "hop_s": hop_s,
                            }
                        )

        if vclips:
            vclips.sort(key=lambda c: float(c.get("start", 0.0)))
        if aclips:
            aclips.sort(key=lambda c: float(c.get("start", 0.0)))

        tracks.append(
            {
                "name": "Video",
                "duration": float(self._global_duration),
                "rms": None,
                "hop_s": 0.03,
                "cuts": [],
                "clips": vclips,
                "kind": "video",
                "track_state_idx": None,
            }
        )
        self._timeline_track_map.append({"kind": "video", "track_state_idx": None})

        tracks.append(
            {
                "name": "Audio",
                "duration": float(self._global_duration),
                "rms": None,
                "hop_s": 0.03,
                "cuts": [],
                "clips": aclips,
                "kind": "audio",
                "track_state_idx": None,
            }
        )
        self._timeline_track_map.append({"kind": "audio", "track_state_idx": None})

        self._rebuild_video_segments()
        self._rebuild_audio_segments(None)
        self.timeline.setTracks(tracks, reset_view=reset_view, global_duration=self._global_duration)
        self._update_timeline_scroll_policy()

        # Active track row (single audio row at index 1)
        active_timeline_idx = 1 if len(self._timeline_track_map) > 1 else 0
        self.timeline.setActiveTrack(int(active_timeline_idx), emit=False)

        if self._video_segments:
            try:
                self._sync_video_to_global(getattr(self, "_last_pos", 0.0), force=False)
            except Exception:
                pass
        try:
            self._sync_audio_to_global(getattr(self, "_last_pos", 0.0), force=False)
        except Exception:
            pass

    def _update_timeline_scroll_policy(self) -> None:
        """
        Keep timeline bars visually clean on desktop/laptop:
        - avoid horizontal scrollbar (always off)
        - keep vertical scrollbar hidden in normal 2-row mode
        """
        if not hasattr(self, "timeline_scroll") or self.timeline_scroll is None:
            return
        try:
            self.timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        except Exception:
            pass
        try:
            track_count = int(len(getattr(self, "_timeline_track_map", []) or []))
        except Exception:
            track_count = 0
        try:
            if track_count <= 2:
                self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            else:
                self.timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass

    def _update_active_track_label(self) -> None:
        if self.input_path:
            idx = (self._active_track_index or 0) + 1
            total = len(self._tracks)
            name = Path(self.input_path).name
            try:
                t = self._tracks[self._active_track_index]
                disp = self._segment_display_name(t)
                if disp:
                    name = disp
            except Exception:
                pass
            if total > 1:
                self.lbl_file.setText(f"{name}  (Track {idx}/{total})")
            else:
                self.lbl_file.setText(self.input_path)
        else:
            self.lbl_file.setText("Drop a video on the preview area or click \"Add Video\".")

    # -----------------------------
    # Edit tools (select / cut / split)
    # -----------------------------
    def _set_tool_mode(self, mode: str) -> None:
        m = str(mode).lower().strip()
        if m not in {"select", "cut", "split"}:
            m = "select"
        mode = m
        self.tool_mode = mode
        try:
            self.btn_cut_tool.blockSignals(True)
            self.btn_cut_tool.setChecked(mode == "cut")
            self.btn_cut_tool.blockSignals(False)
        except Exception:
            pass
        try:
            self.btn_split.blockSignals(True)
            self.btn_split.setChecked(mode == "split")
            self.btn_split.blockSignals(False)
        except Exception:
            pass
        # sync web transport button state
        try:
            self._web_js(self.web_transport, f"uiSetCutActive({json.dumps(mode == 'cut')});")
        except Exception:
            pass
        try:
            self._web_js(self.web_transport, f"uiSetSplitActive({json.dumps(mode == 'split')});")
        except Exception:
            pass
        if hasattr(self.timeline, "setToolMode"):
            try:
                self.timeline.setToolMode(mode)
            except Exception:
                pass

    def _toggle_split_tool(self) -> None:
        if hasattr(self, "btn_split") and self.btn_split is not None and not self.btn_split.isEnabled():
            return
        self._set_tool_mode("select" if self.tool_mode == "split" else "split")

    def _toggle_cut_tool(self) -> None:
        if hasattr(self, "btn_cut_tool") and self.btn_cut_tool is not None and not self.btn_cut_tool.isEnabled():
            return
        self._set_tool_mode("select" if self.tool_mode == "cut" else "cut")

    def _on_cut_tool_toggled(self, checked: bool) -> None:
        self._set_tool_mode("cut" if checked else "select")

    def _on_split_toggled(self, checked: bool) -> None:
        self._set_tool_mode("split" if checked else "select")

    def _split_clip_at_time(self, clip: Clip, t: float) -> bool:
        if clip is None:
            return False
        if getattr(clip, "locked", False):
            return False
        t = float(t)
        t0 = float(clip.timeline_in)
        t1 = float(clip.timeline_out)
        eps = 1e-4
        if t <= t0 + eps or t >= t1 - eps:
            return False
        split_src = float(clip.source_in) + (t - t0)
        track = self.project.get_track(clip.track_id)
        if track is None:
            return False

        self._push_undo_state(kind="timeline")

        # Resolve base color for video clips (inherit from clip or track)
        base_color = getattr(clip, "color", None)
        base_edge = getattr(clip, "edge", None)
        if track.kind == "video" and (base_color is None or base_edge is None):
            for ts in self._tracks:
                if ts.media_id == clip.media_id:
                    if base_color is None:
                        base_color = ts.video_color
                    if base_edge is None:
                        base_edge = ts.video_edge
                    break

        # remove original
        track.remove_clip(clip.id)

        a = self.project.add_clip(
            track_id=clip.track_id,
            media_id=clip.media_id,
            link_id=getattr(clip, "link_id", None),
            source_in=float(clip.source_in),
            source_out=float(split_src),
            timeline_in=float(t0),
            timeline_out=float(t),
            name=clip.name,
        )
        b = self.project.add_clip(
            track_id=clip.track_id,
            media_id=clip.media_id,
            link_id=getattr(clip, "link_id", None),
            source_in=float(split_src),
            source_out=float(clip.source_out),
            timeline_in=float(t),
            timeline_out=float(t1),
            name=clip.name,
        )
        a.enabled = clip.enabled
        a.locked = clip.locked
        b.enabled = clip.enabled
        b.locked = clip.locked

        if track.kind == "video":
            if base_color is not None and base_edge is not None:
                a.color = base_color
                a.edge = base_edge
            fill, edge = self._next_video_color()
            b.color = fill
            b.edge = edge

        # update primary clip ids if needed
        for ts in self._tracks:
            if ts.audio_clip_id == clip.id:
                ts.audio_clip_id = a.id
            if ts.video_clip_id == clip.id:
                ts.video_clip_id = a.id
        return True

    def _slice_segments(self, segs: list[Segment], start: float, end: float, shift: float) -> list[Segment]:
        out: list[Segment] = []
        if not segs:
            return out
        for s in segs:
            try:
                a = max(float(start), float(s.start))
                b = min(float(end), float(s.end))
            except Exception:
                continue
            if b > a:
                out.append(Segment(a - shift, b - shift))
        return out

    def _split_track_state_at_time(self, track_idx: int, t: float) -> bool:
        if track_idx < 0 or track_idx >= len(self._tracks):
            return False
        ts = self._tracks[track_idx]
        vclip = self._find_clip(ts.video_clip_id)
        aclip = self._find_clip(ts.audio_clip_id)
        clip = vclip or aclip
        if clip is None:
            return False

        t = float(t)
        t0 = float(clip.timeline_in)
        t1 = float(clip.timeline_out)
        eps = 1e-4
        if t <= t0 + eps or t >= t1 - eps:
            return False

        src_in = float(clip.source_in)
        src_out = float(clip.source_out)
        split_src = src_in + (t - t0)

        left_len = max(0.0, split_src - src_in)
        right_len = max(0.0, src_out - split_src)
        if left_len <= 0.0 or right_len <= 0.0:
            return False

        self._push_undo_state(kind="timeline")

        # Ensure segment metadata
        self._ensure_segment_meta(ts)
        group_id = ts.segment_group_id or uuid.uuid4().hex
        ts.segment_group_id = group_id

        # Compute next part index for this group
        max_idx = 1
        for tstate in self._tracks:
            if tstate.segment_group_id == group_id:
                try:
                    max_idx = max(max_idx, int(tstate.segment_index))
                except Exception:
                    pass
        new_idx = max_idx + 1

        # Split analysis/cut data (segment-relative)
        left_cuts = self._slice_segments(ts.cuts, 0.0, left_len, 0.0)
        right_cuts = self._slice_segments(ts.cuts, left_len, left_len + right_len, left_len)
        left_keeps = self._slice_segments(ts.keeps, 0.0, left_len, 0.0)
        right_keeps = self._slice_segments(ts.keeps, left_len, left_len + right_len, left_len)
        left_manual = self._slice_segments(ts.manual_cuts, 0.0, left_len, 0.0)
        right_manual = self._slice_segments(ts.manual_cuts, left_len, left_len + right_len, left_len)
        left_supp = self._slice_segments(ts.suppressed_cuts, 0.0, left_len, 0.0)
        right_supp = self._slice_segments(ts.suppressed_cuts, left_len, left_len + right_len, left_len)

        # Update left track state
        ts.segment_source_in = float(src_in)
        ts.segment_source_out = float(split_src)
        ts.duration = float(left_len)
        ts.cuts = left_cuts
        ts.keeps = left_keeps
        ts.manual_cuts = left_manual
        ts.suppressed_cuts = left_supp
        ts.undo_stack.clear()
        ts.redo_stack.clear()
        ts.pending_cut_start = None
        ts.pending_cut_end = None

        # Update left clips (keep on same tracks)
        left_link = uuid.uuid4().hex
        if vclip is not None:
            vclip.source_out = float(split_src)
            vclip.timeline_out = float(t)
            vclip.link_id = left_link
        if aclip is not None:
            aclip.source_out = float(split_src)
            aclip.timeline_out = float(t)
            aclip.link_id = left_link

        # Create right track state
        ts_right = TrackState()
        ts_right.path = ts.path
        ts_right.media_id = ts.media_id
        ts_right.duration = float(right_len)
        ts_right.rms = ts.rms
        ts_right.hop_s = ts.hop_s
        ts_right.rms_min = ts.rms_min
        ts_right.rms_max = ts.rms_max
        ts_right.rms_eps = ts.rms_eps
        ts_right.segment_source_in = float(split_src)
        ts_right.segment_source_out = float(src_out)
        ts_right.cuts_enabled = ts.cuts_enabled
        ts_right.cuts = right_cuts
        ts_right.keeps = right_keeps
        ts_right.manual_cuts = right_manual
        ts_right.suppressed_cuts = right_supp
        ts_right.cfg = dict(getattr(ts, "cfg", {}) or {})
        ts_right.filters_restored = bool(getattr(ts, "filters_restored", False))
        ts_right.segment_group_id = group_id
        ts_right.segment_index = int(new_idx)
        fill, edge = self._next_video_color()
        ts_right.video_color = fill
        ts_right.video_edge = edge
        ts_right.copy_source_id = getattr(ts, "copy_source_id", None)
        try:
            ts_right.copy_index = int(getattr(ts, "copy_index", 0) or 0)
        except Exception:
            ts_right.copy_index = 0

        # Insert right track after current
        self._tracks.insert(track_idx + 1, ts_right)
        if self._active_track_index > track_idx:
            self._active_track_index += 1

        # Create right-side clips on new tracks
        self._ensure_video_track_for_state(ts_right)
        self._ensure_audio_track_for_state(ts_right)
        right_link = uuid.uuid4().hex
        right_name = self._segment_display_name(ts_right)
        vclip_new = self.project.add_clip(
            track_id=str(ts_right.video_track_id),
            media_id=str(ts_right.media_id),
            link_id=right_link,
            source_in=float(split_src),
            source_out=float(src_out),
            timeline_in=float(t),
            timeline_out=float(t1),
            name=right_name,
            color=ts_right.video_color,
            edge=ts_right.video_edge,
        )
        aclip_new = self.project.add_clip(
            track_id=str(ts_right.audio_track_id),
            media_id=str(ts_right.media_id),
            link_id=right_link,
            source_in=float(split_src),
            source_out=float(src_out),
            timeline_in=float(t),
            timeline_out=float(t1),
            name=right_name,
        )
        ts_right.audio_clip_id = aclip_new.id
        ts_right.video_clip_id = vclip_new.id

        # Update segment RMS stats for both sides
        try:
            self._update_segment_rms_stats(ts)
            self._update_segment_rms_stats(ts_right)
        except Exception:
            pass

        # Re-align sequence and refresh
        self._finalize_timeline_reorder()
        return True

    def _on_clip_split_requested(self, clip_id: str, t: float) -> None:
        clip = self._find_clip(clip_id)
        if clip is None:
            return
        track_idx = self._track_index_for_clip_id(clip.id)
        changed = False
        if track_idx is not None:
            changed = self._split_track_state_at_time(int(track_idx), t)
        else:
            changed = self._split_clip_at_time(clip, t)
        if changed:
            try:
                self._refresh_timeline_tracks(reset_view=False)
            except Exception:
                pass

    def _activate_track(self, idx: int, keep_playhead: bool = True, sync_players: bool = True) -> None:
        if idx is None or idx < 0 or idx >= len(self._tracks):
            return
        if idx == self._active_track_index:
            return
        # save current track settings before switching
        try:
            cur = self._tracks[self._active_track_index] if 0 <= self._active_track_index < len(self._tracks) else None
            self._save_track_cfg(cur)
            self._save_workspace_for_mode(cur)
        except Exception:
            pass
        self._active_track_index = idx
        track = self._get_active_track()
        self._apply_track_cfg(track)
        try:
            self._restore_workspace_for_mode(track)
        except Exception:
            pass

        # update UI + timeline
        self._update_active_track_label()
        self._refresh_timeline_tracks(reset_view=False)
        self.timeline.setActiveTrack(self._timeline_index_for_audio_track(self._active_track_index), emit=False)

        # keep playhead if possible
        if keep_playhead:
            t = float(self.timeline.playhead)
        else:
            t = 0.0
        total = self._global_duration if self._global_duration > 0 else track.duration
        if total > 0:
            t = max(0.0, min(t, total))
        else:
            t = 0.0

        if sync_players:
            self._set_all_positions(int(t * 1000))
        self.seek.blockSignals(True)
        self.seek.setRange(0, int(total * 1000))
        self.seek.setValue(int(t * 1000))
        self.seek.blockSignals(False)
        self.timeline.setPlayhead(t)
        self._update_time_label(t)

        # thresholds + pending cuts (segment-aware)
        seg_rms = self._segment_rms(track) if track.rms is not None else None
        self.threshold_meter.set_reference_from_rms(seg_rms)
        self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
        self._refresh_threshold_ui_state()
        self._set_pending_cut_visual(
            track.pending_cut_start,
            track.pending_cut_end,
            track_state_idx=self._active_track_index,
        )

        # recompute cuts for this track with current settings (if analysis exists)
        if self.analysis_mode != "ai":
            if track.rms is not None and track.duration > 0:
                self.cuts_enabled = True
                self._recompute()
            else:
                self._apply_track_cuts_ui(track)
        else:
            self._apply_track_cuts_ui(track)

        self._update_ai_stats_panel(track)
        self._update_ai_options_panel(track)
        self._web_push_full_state()
        self._sync_analysis_ui_for_active_track()

    def _active_timeline_map(self) -> Optional[dict]:
        try:
            idx = int(self.timeline.active_track)
        except Exception:
            idx = 0
        if 0 <= idx < len(self._timeline_track_map):
            return self._timeline_track_map[idx]
        return None

    def _timeline_index_for_audio_track(self, track_state_idx: int) -> int:
        first_audio = None
        for i, m in enumerate(self._timeline_track_map):
            if m.get("kind") == "audio":
                if first_audio is None:
                    first_audio = i
                if m.get("track_state_idx") == track_state_idx:
                    return i
        return first_audio if first_audio is not None else 0

    def _on_timeline_track_selected(self, idx: int) -> None:
        m = self._timeline_track_map[int(idx)] if 0 <= int(idx) < len(self._timeline_track_map) else None
        if not m:
            return
        ts_idx = m.get("track_state_idx", None)
        if ts_idx is not None:
            playing = False
            try:
                playing = (self.video_player.playbackState() == QMediaPlayer.PlayingState)
            except Exception:
                playing = False
            try:
                playing = bool(playing or getattr(self, "_play_requested", False))
            except Exception:
                pass
            # While playing, do a light track switch: keep current AV stream alive.
            self._activate_track(int(ts_idx), sync_players=(not playing))
        else:
            self._web_push_full_state()

    def _fmt_time(self, seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        m = int(seconds // 60)
        s = int(seconds % 60)
        h = int(m // 60)
        m = int(m % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _web_js(self, view: QWebEngineView, js: str) -> None:
        # Defer WebEngine work onto the UI loop so updates are never fired
        # inline while Qt is still settling a heavy analysis/timeline refresh.
        if not js:
            return
        if getattr(self, "_web_disabled", False):
            return
        if not self._web_obj_valid(view):
            return
        try:
            ready = True
            if hasattr(self, "_web_ready"):
                ready = bool(self._web_ready.get(view, False))
            if not ready:
                queue = getattr(self, "_web_js_queue", None)
                if queue is None:
                    self._web_js_queue = {}
                    queue = self._web_js_queue
                queue.setdefault(view, []).append(str(js))
                return
            self._dispatch_web_js(view, str(js))
        except Exception:
            return

    def _compute_stats(self):
        total_s = float(self._global_duration or self.duration or 0.0)
        keeps: list[Segment] = []

        # Stats must reflect current project timeline (segments/deletions/duplicates),
        # not only active-track flattening.
        try:
            keeps = list(self._collect_global_keeps() or [])
        except Exception:
            keeps = []

        # Fallback for single-track projects when global keeps are not available.
        if not keeps:
            try:
                track = self._get_active_track()
                keeps = list(getattr(track, "keeps", None) or self.keeps or [])
            except Exception:
                keeps = list(self.keeps or [])

        # Clamp/merge against current global duration
        cleaned: list[Segment] = []
        for k in keeps:
            try:
                a = max(0.0, float(k.start))
                b = max(0.0, float(k.end))
            except Exception:
                continue
            if total_s > 0.0:
                a = min(a, total_s)
                b = min(b, total_s)
            if b > a:
                cleaned.append(Segment(a, b))
        keeps = merge_overlaps(sorted(cleaned, key=lambda s: s.start))
        if total_s <= 0.0 and keeps:
            total_s = max(float(k.end) for k in keeps)

        out_s = sum(getattr(seg, "dur", seg.end - seg.start) for seg in keeps)
        out_s = max(0.0, min(float(out_s), float(total_s) if total_s > 0.0 else float(out_s)))

        # Cuts are the gaps between keeps over the full timeline.
        cuts: list[Segment] = []
        cursor = 0.0
        eps = 1e-6
        for k in keeps:
            a = max(0.0, float(k.start))
            b = max(0.0, float(k.end))
            if a > cursor + eps:
                cuts.append(Segment(cursor, a))
            cursor = max(cursor, b)
        if total_s > cursor + eps:
            cuts.append(Segment(cursor, total_s))

        cuts_n = len(cuts)
        kept_pct = (out_s / total_s * 100.0) if total_s > 0 else 0.0
        saved_s = max(0.0, float(total_s) - float(out_s))
        avg_cut = 0.0
        avg_keep = 0.0
        if cuts_n > 0:
            total_cut = sum(getattr(c, "dur", c.end - c.start) for c in cuts)
            avg_cut = total_cut / float(cuts_n)
        keeps_n = len(keeps)
        if keeps_n > 0:
            avg_keep = out_s / float(keeps_n)
        cuts_per_min = (cuts_n / (total_s / 60.0)) if total_s > 0 else 0.0
        saved_pct = max(0.0, min(100.0, 100.0 - float(kept_pct)))
        return {
            "total_s": total_s,
            "cuts_n": cuts_n,
            "out_s": out_s,
            "kept_pct": kept_pct,
            "saved_s": saved_s,
            "avg_cut": avg_cut,
            "avg_keep": avg_keep,
            "cuts_per_min": cuts_per_min,
            "keeps_n": keeps_n,
            "saved_pct": saved_pct,
        }

    def _project_display_name(self) -> str:
        for t in self._tracks:
            try:
                if t.path:
                    return Path(t.path).name
            except Exception:
                continue
        if self.input_path:
            try:
                return Path(self.input_path).name
            except Exception:
                pass
        return "-"

    @staticmethod
    def _float_eq(a: Any, b: Any, tol: float = 1e-6) -> bool:
        try:
            return abs(float(a) - float(b)) <= tol
        except Exception:
            return False

    def _set_badge_label(self, label: QLabel | None, text: str, kind: str = "muted") -> None:
        if label is None:
            return
        try:
            label.setText(str(text))
            label.setProperty("kind", str(kind))
            style = label.style()
            if style is not None:
                style.unpolish(label)
                style.polish(label)
            label.update()
        except Exception:
            try:
                label.setText(str(text))
            except Exception:
                pass

    def _topbar_status_payload(self) -> dict[str, Any]:
        has_video = bool(self.input_path)
        try:
            audio_ready = any(bool(getattr(t, "audio_clip_id", None)) for t in getattr(self, "_tracks", []))
        except Exception:
            audio_ready = False
        if not audio_ready:
            audio_ready = has_video

        try:
            preset_name = str(self.preset_combo.currentText() or "Manual")
        except Exception:
            preset_name = "Manual"
        if getattr(self, "_preset_dirty", False) and getattr(self, "_preset_source_name", None):
            preset_label = "Modified"
            preset_kind = "warn"
        elif preset_name and preset_name != "Manual":
            preset_label = preset_name
            preset_kind = "ok"
        else:
            preset_label = "Manual"
            preset_kind = "muted"

        codec_label = "-"
        try:
            codec_label = str(self.codec_combo.currentText() or "-")
            if " - " in codec_label:
                codec_label = codec_label.split(" - ", 1)[0]
        except Exception:
            pass

        return {
            "video": "Loaded" if has_video else "Missing",
            "video_kind": "ok" if has_video else "muted",
            "audio": "Ready" if audio_ready else "Unavailable",
            "audio_kind": "ok" if audio_ready else "muted",
            "preset": preset_label,
            "preset_kind": preset_kind,
            "codec": codec_label,
            "codec_kind": "info",
        }

    def _push_topbar_status_chips(self) -> None:
        try:
            payload = self._topbar_status_payload()
            self._web_js(self.web_topbar, f"uiSetStatusChips({json.dumps(payload)});")
        except Exception:
            pass

    def _threshold_profile_label(self, pct: int) -> tuple[str, str]:
        p = int(max(0, min(100, pct)))
        if p < 30:
            return ("Conservative", "success")
        if p < 60:
            return ("Balanced", "info")
        return ("Aggressive", "warning")

    def _refresh_threshold_ui_state(self) -> None:
        if not hasattr(self, "threshold_pct"):
            return
        try:
            p = int(self.threshold_pct.value())
        except Exception:
            p = 0
        label, kind = self._threshold_profile_label(p)
        self._set_badge_label(getattr(self, "lbl_threshold_semantic", None), label, kind)

    def _refresh_advanced_ui_state(self) -> None:
        states: dict[str, bool] = {}
        try:
            states["padding"] = not (
                self._float_eq(self.pre_pad_s.value(), self.pre_pad_s_default)
                and self._float_eq(self.post_pad_s.value(), self.post_pad_s_default)
                and self._float_eq(self.min_cut_s.value(), self.min_cut_s_default)
            )
            states["detection"] = not (
                int(self.attack_ms.value()) == int(self.attack_ms_default)
                and int(self.release_ms.value()) == int(self.release_ms_default)
                and bool(self.gain_affects_detection.isChecked()) == bool(self.gain_affects_detection_default)
            )
            states["smoothing"] = str(self.smoothing_mode.currentText()) != str(self.smoothing_mode_default)
            states["merging"] = int(self.merge_pauses_ms.value()) != int(self.merge_pauses_ms_default)
            states["audio"] = not (
                self._float_eq(self.gain_db.value(), self.gain_db_default)
                and bool(self.normalize_lufs.isChecked()) == bool(self.normalize_lufs_default)
                and self._float_eq(self.lufs_target.value(), self.lufs_target_default, tol=0.051)
                and bool(self.limiter.isChecked()) == bool(self.limiter_default)
            )
        except Exception:
            states = {}

        badges = getattr(self, "_adv_section_badges", {}) if hasattr(self, "_adv_section_badges") else {}
        if isinstance(badges, dict):
            for key, label in badges.items():
                custom = bool(states.get(str(key), False))
                self._set_badge_label(label, "Custom" if custom else "Default", "warning" if custom else "muted")

        any_custom = any(states.values()) if states else False
        self._set_badge_label(
            getattr(self, "lbl_adv_state_badge", None),
            "Custom" if any_custom else "Default",
            "warning" if any_custom else "muted",
        )

    def _preset_profile_summary(self, cfg: dict | None = None) -> str:
        try:
            if cfg is None:
                cfg = self._current_preset_cfg()
        except Exception:
            cfg = cfg or {}
        try:
            intensity = int(cfg.get("intensity", 45))
        except Exception:
            intensity = 45
        try:
            threshold = int(cfg.get("threshold_pct", 45))
        except Exception:
            threshold = 45
        try:
            pre = float(cfg.get("pre_pad_s", 0.25))
            post = float(cfg.get("post_pad_s", 0.25))
        except Exception:
            pre, post = 0.25, 0.25

        if intensity < 30:
            pace = "Aggressive cuts"
        elif intensity < 60:
            pace = "Balanced cuts"
        else:
            pace = "Tighter keeps"

        thr_lbl, _ = self._threshold_profile_label(threshold)

        pad_avg_ms = int(round(((pre + post) * 0.5) * 1000.0))
        if pad_avg_ms <= 120:
            pad_lbl = "Short padding"
        elif pad_avg_ms <= 300:
            pad_lbl = "Balanced padding"
        else:
            pad_lbl = "Longer padding"

        return f"{pace} - {thr_lbl} threshold - {pad_lbl}"

    def _update_preset_ui_state(self) -> None:
        label = getattr(self, "lbl_preset_state", None)
        meta = getattr(self, "lbl_preset_meta", None)
        combo = getattr(self, "preset_combo", None)
        if combo is None:
            return

        try:
            current = str(combo.currentText() or "Manual")
        except Exception:
            current = "Manual"

        if getattr(self, "_preset_dirty", False) and getattr(self, "_preset_source_name", None):
            src = str(self._preset_source_name or "Preset")
            self._set_badge_label(label, "Preset modified", "warning")
            if meta is not None:
                try:
                    cfg = self.presets.get(src) if isinstance(getattr(self, "presets", None), dict) else None
                    meta.setText(f"{self._preset_profile_summary(cfg)} - Unsaved changes from {src}")
                except Exception:
                    meta.setText(f"Unsaved changes from {src}")
        elif current != "Manual":
            self._set_badge_label(label, "Saved preset", "success")
            if meta is not None:
                try:
                    cfg = self.presets.get(current) if isinstance(getattr(self, "presets", None), dict) else None
                    meta.setText(self._preset_profile_summary(cfg))
                except Exception:
                    meta.setText("Saved preset")
        else:
            self._set_badge_label(label, "Manual", "muted")
            if meta is not None:
                try:
                    meta.setText(self._preset_profile_summary())
                except Exception:
                    meta.setText("Manual tuning")

        try:
            self._push_topbar_status_chips()
        except Exception:
            pass


    def _web_push_full_state(self) -> None:
        try:
            self._web_apply_theme_vars()
        except Exception:
            pass
        # topbar
        name = self._project_display_name()
        skip = bool(self.chk_skip.isChecked())
        state = "Ready" if self.input_path else "No video loaded"
        has_video = bool(self.input_path)
        self._web_js(self.web_topbar, f"uiSetState({json.dumps(state)});")
        self._web_js(self.web_topbar, f"uiSetCrumbs({json.dumps(name)});")
        self._web_js(self.web_topbar, f"uiSetSkip({json.dumps(skip)});")
        self._web_js(self.web_topbar, "uiSetProgress(0);")
        self._web_js(self.web_topbar, f"uiSetVideoLoaded({json.dumps(has_video)});")
        self._push_topbar_status_chips()
        self._web_js(self.web_topbar, f"uiSetReadyDotState({json.dumps(getattr(self, '_ready_dot_state', 'idle'))});")
        self._web_js(self.web_topbar, f"uiSetExportDotState({json.dumps(getattr(self, '_export_dot_state', 'idle'))});")
        # timecode
        cur = self._fmt_time(getattr(self, "_last_pos", 0.0))
        st = self._compute_stats()
        dur = self._fmt_time(st.get("total_s", self._global_duration or self.duration or 0.0))
        self._web_js(self.web_transport, f"uiSetTimecode({json.dumps(cur)}, {json.dumps(dur)});")
        self._web_js(self.web_transport, f"uiSetPreviewVolume({int(getattr(self, '_preview_volume_pct', 100))});")
        self._push_zoom_to_web()

        # play/pause state
        is_playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self._web_js(self.web_transport, f"uiSetPlayState({json.dumps(is_playing)});")
        self._refresh_play_button_state()

        # enable/disable cut navigation buttons
        cuts_ready = False
        try:
            track = self._get_active_track()
            cuts_ready = bool(track and track.rms is not None and float(track.duration or 0.0) > 0.0)
        except Exception:
            cuts_ready = False
        self._web_js(self.web_transport, f"uiSetCutsReady({json.dumps(cuts_ready)});")
        # split tool state
        try:
            self._web_js(self.web_transport, f"uiSetCutActive({json.dumps(self.tool_mode == 'cut')});")
        except Exception:
            pass
        try:
            self._web_js(self.web_transport, f"uiSetSplitActive({json.dumps(self.tool_mode == 'split')});")
        except Exception:
            pass

        # stats
        out_fmt = self._fmt_time(st["out_s"])
        saved_fmt = self._fmt_time(st["saved_s"])
        avg_cut_fmt = self._fmt_time(st["avg_cut"]) if st["avg_cut"] > 0 else "0:00"
        avg_keep_fmt = self._fmt_time(st["avg_keep"]) if st["avg_keep"] > 0 else "0:00"

        try:
            self.lbl_footer.setText(
                f"Duration {dur}  -  Output {out_fmt}  -  {int(st['cuts_n'])} cuts"
            )
        except Exception:
            pass

        self._web_js(
            self.web_stats,
            f"uiSetStats({json.dumps(dur)}, {json.dumps(out_fmt)}, {st['cuts_n']}, {st['kept_pct']}, {json.dumps(saved_fmt)}, {json.dumps(avg_cut_fmt)}, {json.dumps(avg_keep_fmt)}, {st['cuts_per_min']}, {st['keeps_n']}, {st['saved_pct']});",
        )
        # Update right panel stats (legacy fallback)
        if self.stats_duration:
            self.stats_duration.setText(f"Duration: {dur}")
            self.stats_output.setText(f"Output: {out_fmt}")
            self.stats_cuts.setText(f"Cuts: {st['cuts_n']}")
            self.stats_kept.setText(f"Kept: {st['kept_pct']:.0f}%")
            preset_name = self.preset_combo.currentText() if hasattr(self, 'preset_combo') else "???"
            self.stats_preset.setText(f"Preset: {preset_name}")

    # -----------------------------
    # Layout (modular)
    # -----------------------------

    def _build_layout_modular(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # TOPBAR is now HTML
        outer.addWidget(self.web_topbar)

        # Main layout (fisso, senza splitter)
        main_container = QFrame()
        main_container.setObjectName("MainContainer")
        main_container.setAttribute(Qt.WA_StyledBackground, True)
        main_layout = QHBoxLayout(main_container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        left_container = QWidget()
        left_container.setObjectName("LeftPanel")
        left_layout = build_left_panel(self)
        left_container.setLayout(left_layout)

        right_panel = build_right_panel(self)

        # User-resizable left/right split (preview area vs inspector).
        main_splitter = QSplitter(Qt.Horizontal)
        main_splitter.setObjectName("MainPanelSplitter")
        main_splitter.setChildrenCollapsible(False)
        main_splitter.setHandleWidth(10)
        main_splitter.setOpaqueResize(False)
        try:
            main_splitter.splitterMoved.connect(self._on_main_panel_splitter_moved)
        except Exception:
            pass

        # keep references for layout swap + splitter persistence
        self._main_layout = main_layout
        self._main_splitter = main_splitter
        self._left_container = left_container
        self._right_panel = right_panel
        main_layout.addWidget(main_splitter, stretch=1)

        outer.addWidget(main_container, stretch=1)

    def _apply_layout_mode(self) -> None:
        splitter = getattr(self, "_main_splitter", None)
        if splitter is None:
            return
        try:
            while splitter.count() > 0:
                w = splitter.widget(0)
                if w is None:
                    break
                w.setParent(None)
        except Exception:
            pass

        if self._layout_mode == "right":
            splitter.addWidget(self._right_panel)
            splitter.addWidget(self._left_container)
            splitter.setStretchFactor(0, 3)
            splitter.setStretchFactor(1, 7)
        else:
            splitter.addWidget(self._left_container)
            splitter.addWidget(self._right_panel)
            splitter.setStretchFactor(0, 7)
            splitter.setStretchFactor(1, 3)
        self._apply_main_panel_ratio()

    def _toggle_layout_mode(self) -> None:
        self._layout_mode = "right" if self._layout_mode == "left" else "left"
        self._apply_layout_mode()
        self._save_layout_pref()

    def _load_layout_pref(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        mode = s.value("layout_mode", "left")
        self._layout_mode = "right" if str(mode) == "right" else "left"

    def _save_layout_pref(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        s.setValue("layout_mode", self._layout_mode)

    def _user_presets_path(self) -> Path:
        path = config_root()
        path.mkdir(parents=True, exist_ok=True)
        return path / "presets.json"

    def _load_ui_prefs(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        self._mini_stats_enabled = bool(int(s.value("mini_stats_enabled", 1)))
        self._layout_switch_enabled = bool(int(s.value("layout_switch_enabled", 1)))
        self._advanced_enabled = bool(int(s.value("advanced_enabled", 1)))
        self._recent_enabled = bool(int(s.value("recent_enabled", 0)))
        self._remember_filters_enabled = bool(int(s.value("remember_filters_enabled", 0)))
        self._remember_cuts_enabled = bool(int(s.value("remember_cuts_enabled", 0)))
        try:
            env_enabled_raw = str(os.environ.get("AUTO_CUTTER_AUTOSAVE_ENABLED", "1")).strip().lower()
            env_enabled = env_enabled_raw in {"1", "true", "yes", "on"}
        except Exception:
            env_enabled = True
        try:
            env_interval = int(str(os.environ.get("AUTO_CUTTER_AUTOSAVE_SECONDS", "45")).strip() or "45")
        except Exception:
            env_interval = 45
        env_interval = max(10, min(600, int(env_interval)))
        self._crash_autosave_enabled = bool(int(s.value("crash_autosave_enabled", int(env_enabled))))
        try:
            self._crash_autosave_interval_s = int(s.value("crash_autosave_interval_s", int(env_interval)) or env_interval)
        except Exception:
            self._crash_autosave_interval_s = int(env_interval)
        self._crash_autosave_interval_s = max(10, min(600, int(self._crash_autosave_interval_s)))
        try:
            ratio_raw = float(s.value("preview_timeline_ratio", 0.72) or 0.72)
        except Exception:
            ratio_raw = 0.72
        self._preview_timeline_ratio = self._clamp_preview_timeline_ratio(ratio_raw)
        try:
            main_ratio_raw = float(s.value("main_panel_ratio", 0.69) or 0.69)
        except Exception:
            main_ratio_raw = 0.69
        self._main_panel_ratio = self._clamp_main_panel_ratio(main_ratio_raw)
        try:
            self._window_pref_w = int(s.value("window_w", 1400) or 1400)
        except Exception:
            self._window_pref_w = 1400
        try:
            self._window_pref_h = int(s.value("window_h", 860) or 860)
        except Exception:
            self._window_pref_h = 860

    def _save_ui_prefs(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        s.setValue("mini_stats_enabled", int(self._mini_stats_enabled))
        s.setValue("layout_switch_enabled", int(self._layout_switch_enabled))
        s.setValue("advanced_enabled", int(self._advanced_enabled))
        s.setValue("recent_enabled", int(self._recent_enabled))
        s.setValue("remember_filters_enabled", int(self._remember_filters_enabled))
        s.setValue("remember_cuts_enabled", int(self._remember_cuts_enabled))
        s.setValue("crash_autosave_enabled", int(bool(self._crash_autosave_enabled)))
        s.setValue("crash_autosave_interval_s", int(self._crash_autosave_interval_s))
        s.setValue("preview_timeline_ratio", float(self._clamp_preview_timeline_ratio(getattr(self, "_preview_timeline_ratio", 0.72))))
        s.setValue("main_panel_ratio", float(self._clamp_main_panel_ratio(getattr(self, "_main_panel_ratio", 0.69))))
        if not bool(self.isMaximized()):
            try:
                s.setValue("window_w", int(self.width()))
                s.setValue("window_h", int(self.height()))
            except Exception:
                pass
        s.setValue("window_maximized", int(bool(self.isMaximized())))

    def _apply_window_size_pref(self) -> None:
        if bool(getattr(self, "_window_pref_w", 0)) <= 0 or bool(getattr(self, "_window_pref_h", 0)) <= 0:
            return
        try:
            w = int(self._window_pref_w)
            h = int(self._window_pref_h)
        except Exception:
            return
        try:
            w = max(int(self.minimumWidth()), w)
            h = max(int(self.minimumHeight()), h)
            self.resize(w, h)
        except Exception:
            pass

    def _load_theme_pref(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        raw = str(s.value("theme_name", "Dark") or "Dark").strip()
        allowed = {"Dark", "Light", "System"}
        # Keep support for older/custom values already present in configs.
        self._theme_name = raw if raw in allowed else (raw if raw else "Dark")

    def _save_theme_pref(self) -> None:
        s = QSettings("Auto Cutter", "Auto Cutter")
        s.setValue("theme_name", str(getattr(self, "_theme_name", "Dark") or "Dark"))

    def _apply_theme_pref(self) -> None:
        apply_theme(self, str(getattr(self, "_theme_name", "Dark") or "Dark"))
        self._web_apply_theme_vars()

    def _apply_ui_prefs(self) -> None:
        # Mini stats (under timeline)
        if hasattr(self, "web_stats") and self.web_stats is not None:
            self.web_stats.setVisible(bool(self._mini_stats_enabled))

        # Layout switch button (disable instead of hide)
        if hasattr(self, "btn_layout") and self.btn_layout is not None:
            self.btn_layout.setEnabled(bool(self._layout_switch_enabled))

        # Advanced section
        if hasattr(self, "adv_header") and self.adv_header is not None:
            self.adv_header.setEnabled(bool(self._advanced_enabled))
        if not self._advanced_enabled:
            self._adv_open = False
        if hasattr(self, "advanced_panel") and self.advanced_panel is not None:
            self.advanced_panel.setEnabled(bool(self._advanced_enabled))

        self._sync_adv_ui()
        self._apply_preview_timeline_ratio()

        # Export toggles are managed via the top menu only

    def _web_apply_theme_vars(self) -> None:
        # Push CSS variables to WebUI
        try:
            vars_map = {
                "--bg": self._c_bg.name(),
                "--surface": self._c_surface.name(),
                "--surface-2": self._c_surface2.name(),
                "--surface2": self._c_surface2.name(),
                "--border": self._c_border.name(),
                "--text": self._c_text.name(),
                "--text-muted": self._c_muted.name(),
                "--muted": self._c_muted.name(),
                "--subtle": self._c_subtle.name(),
                "--accent": self._c_accent.name(),
                "--accent2": self._c_accent.lighter(120).name(),
            }
            js = (
                "(() => { const r=document.documentElement.style;"
                + "".join([f"r.setProperty('{k}','{v}');" for k, v in vars_map.items()])
                + "if(document && document.body){document.body.style.color='var(--text)';}"
                + "})()"
            )
            for view in (self.web_topbar, self.web_transport, self.web_stats):
                self._web_js(view, js)
        except Exception:
            pass

    @Slot(str)

    def closeEvent(self, event):
        self._app_log(
            "app_close_begin",
            input_loaded=bool(self.input_path),
            tracks=len(getattr(self, "_tracks", []) or []),
            recent_enabled=bool(getattr(self, "_recent_enabled", False)),
            remember_filters=bool(getattr(self, "_remember_filters_enabled", False)),
            remember_cuts=bool(getattr(self, "_remember_cuts_enabled", False)),
        )
        # Ensure export resources are released on close
        try:
            self._abort_export()
        except Exception:
            pass
        try:
            advisor_thread = getattr(self, "export_advisor_thread", None)
            if advisor_thread is not None and advisor_thread.isRunning():
                advisor_thread.requestInterruption()
                advisor_thread.quit()
                advisor_thread.wait(5000)
        except Exception:
            pass
        try:
            self._abort_analysis_tasks(wait_ms=800)
        except Exception:
            pass
        # A bounded wait may end before a worker has delivered its finished
        # signal. Keep the window (and its QThread children) alive until then.
        if self._background_qthreads_running():
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        try:
            self._cancel_warm_cache(wait_ms=1200)
        except Exception:
            pass
        try:
            twitch = getattr(self, "_twitch_integration", None)
            if twitch is not None:
                twitch.shutdown()
        except Exception:
            pass
        # Bounded render/keyframe caches intentionally persist between sessions.
        # Clearing them is an explicit action in the Performance panel.
        # Persist last session info for "recent files" and "remember filters/cuts"
        try:
            s = QSettings("Auto Cutter", "Auto Cutter")
            try:
                self._save_track_cfg(self._get_active_track())
            except Exception:
                pass
            # Persist recent files as a dedicated path-only list.
            if self._recent_enabled:
                recent_paths = self._build_recent_paths()
                if recent_paths:
                    s.setValue("recent_paths_v2", json.dumps(recent_paths))
                else:
                    s.remove("recent_paths_v2")
            else:
                s.remove("recent_paths_v2")

            # Legacy fallback key (kept for compatibility while migrating).
            if self.input_path:
                s.setValue("last_input_path", str(self.input_path))

            # Persist session payload only for remember-filters / remember-cuts.
            if self._remember_filters_enabled or self._remember_cuts_enabled:
                items = self._build_session_items(
                    include_cfg=self._remember_filters_enabled,
                    include_cuts=self._remember_cuts_enabled,
                )
                if items:
                    s.setValue("last_session_items", json.dumps(items))
            elif not self._recent_enabled:
                # If neither feature needs it, clear stale complex session payload.
                s.remove("last_session_items")

            if self._remember_filters_enabled:
                # keep legacy key for compatibility
                try:
                    s.setValue("last_filters_cfg", json.dumps(self._current_preset_cfg()))
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._save_layout_pref()
            self._save_ui_prefs()
            self._save_theme_pref()
        except Exception:
            pass
        try:
            self._app_log("app_close_end")
            self._prune_session_logs(keep=10)
        except Exception:
            pass
        try:
            if self._crash_autosave_timer is not None:
                self._crash_autosave_timer.stop()
        except Exception:
            pass
        try:
            self._clear_crash_recovery_artifacts(clear_snapshot=True)
        except Exception:
            pass
        super().closeEvent(event)

    def _open_top_menu(self) -> None:
        menu = QMenu(self)
        act_theme_dark = None
        act_theme_light = None
        act_theme_system = None
        act_recovery_toggle = None
        act_recovery_custom = None
        act_reset_defaults = None
        act_language_en = None
        act_language_it = None
        act_quick_start = None
        act_diagnostics = None
        act_about = None
        act_third_party = None
        act_twitch_client_id = None
        act_twitch_connect = None
        act_twitch_disconnect = None
        act_twitch_watcher = None
        act_twitch_poll = None
        act_twitch_pending = None
        act_twitch_download_folder = None
        act_twitch_open_download_folder = None
        act_twitch_retry_download = None
        act_twitch_cancel_download = None
        recovery_interval_actions = {}

        # Theme (manual override; default is fixed Dark)
        try:
            theme_menu = menu.addMenu("Theme")
            cur_theme = str(getattr(self, "_theme_name", "Dark") or "Dark")
            act_theme_dark = theme_menu.addAction("Dark")
            act_theme_dark.setCheckable(True)
            act_theme_dark.setChecked(cur_theme == "Dark")
            act_theme_light = theme_menu.addAction("Light")
            act_theme_light.setCheckable(True)
            act_theme_light.setChecked(cur_theme == "Light")
            act_theme_system = theme_menu.addAction("System")
            act_theme_system.setCheckable(True)
            act_theme_system.setChecked(cur_theme == "System")
            menu.addSeparator()
        except Exception:
            act_theme_dark = None
            act_theme_light = None
            act_theme_system = None

        # Mini stats toggle
        if self._mini_stats_enabled:
            act_stats = menu.addAction("Disable mini stats")
        else:
            act_stats = menu.addAction("Enable mini stats")

        # Layout switch toggle
        if self._layout_switch_enabled:
            act_layout = menu.addAction("Disable layout switch")
        else:
            act_layout = menu.addAction("Enable layout switch")

        # Advanced toggle
        if self._advanced_enabled:
            act_adv = menu.addAction("Disable advanced")
        else:
            act_adv = menu.addAction("Enable advanced")

        # Crash recovery / autosave
        menu.addSeparator()
        recovery_menu = menu.addMenu("Recovery")
        act_recovery_toggle = recovery_menu.addAction("Enable autosave recovery")
        act_recovery_toggle.setCheckable(True)
        act_recovery_toggle.setChecked(bool(self._crash_autosave_enabled))
        recovery_menu.addSeparator()
        for sec in (15, 30, 45, 60, 90, 120):
            a = recovery_menu.addAction(f"Autosave every {sec}s")
            a.setCheckable(True)
            a.setChecked(int(self._crash_autosave_interval_s) == int(sec))
            a.setEnabled(bool(self._crash_autosave_enabled))
            recovery_interval_actions[a] = int(sec)
        recovery_menu.addSeparator()
        act_recovery_custom = recovery_menu.addAction("Set custom interval...")
        act_recovery_custom.setEnabled(bool(self._crash_autosave_enabled))

        # Export toggles
        menu.addSeparator()
        if self._recent_enabled:
            act_recent = menu.addAction("Disable recent files")
        else:
            act_recent = menu.addAction("Enable recent files")

        if self._remember_filters_enabled:
            act_remember = menu.addAction("Disable remember filters")
        else:
            act_remember = menu.addAction("Enable remember filters")

        if self._remember_cuts_enabled:
            act_remember_cuts = menu.addAction("Disable remember cuts")
        else:
            act_remember_cuts = menu.addAction("Enable remember cuts")

        # Twitch automation remains opt-in and never replaces the manual editor.
        menu.addSeparator()
        twitch_menu = menu.addMenu("Twitch automation")
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.is_configured:
            twitch_status = "Status: not configured"
        elif twitch.connected_login:
            twitch_status = f"Account: @{twitch.connected_login}"
        elif twitch.has_stored_token:
            twitch_status = "Account: connected (saved credentials)"
        else:
            twitch_status = "Status: ready to connect"
        act_twitch_status = twitch_menu.addAction(twitch_status)
        act_twitch_status.setEnabled(False)
        if twitch is not None and twitch.is_configured:
            watcher_label = "Watcher: running" if twitch.is_running else "Watcher: stopped"
            act_twitch_runtime = twitch_menu.addAction(watcher_label)
            act_twitch_runtime.setEnabled(False)
        if twitch is not None and twitch.is_downloading:
            act_twitch_download_runtime = twitch_menu.addAction("Download: running")
            act_twitch_download_runtime.setEnabled(False)
        if twitch is not None and twitch.is_analyzing:
            act_twitch_analysis_runtime = twitch_menu.addAction("Analysis: running")
            act_twitch_analysis_runtime.setEnabled(False)
        if twitch is not None and twitch.is_exporting:
            act_twitch_export_runtime = twitch_menu.addAction("Export: running")
            act_twitch_export_runtime.setEnabled(False)
        twitch_menu.addSeparator()
        act_twitch_client_id = twitch_menu.addAction("Set Client ID...")
        act_twitch_connect = twitch_menu.addAction("Connect Twitch account...")
        act_twitch_connect.setEnabled(
            bool(twitch is not None and twitch.is_configured and not twitch.is_authenticating)
        )
        act_twitch_disconnect = twitch_menu.addAction("Disconnect Twitch account")
        act_twitch_disconnect.setEnabled(bool(twitch is not None and twitch.has_stored_token))
        twitch_menu.addSeparator()
        act_twitch_watcher = twitch_menu.addAction("Watch for new VODs")
        act_twitch_watcher.setCheckable(True)
        act_twitch_watcher.setChecked(bool(twitch is not None and twitch.is_enabled))
        act_twitch_watcher.setEnabled(bool(twitch is not None and twitch.has_stored_token))
        act_twitch_poll = twitch_menu.addAction("Check for a new VOD now")
        act_twitch_poll.setEnabled(bool(twitch is not None and twitch.has_stored_token))
        pending_twitch_jobs = self._pending_twitch_jobs()
        act_twitch_pending = twitch_menu.addAction(
            f"Configure pending VOD... ({len(pending_twitch_jobs)})"
        )
        act_twitch_pending.setEnabled(bool(pending_twitch_jobs))
        failed_twitch_downloads = self._failed_twitch_download_jobs()
        act_twitch_retry_download = twitch_menu.addAction(
            f"Retry failed download... ({len(failed_twitch_downloads)})"
        )
        act_twitch_retry_download.setEnabled(bool(failed_twitch_downloads))
        act_twitch_cancel_download = twitch_menu.addAction("Cancel current download")
        act_twitch_cancel_download.setEnabled(bool(twitch is not None and twitch.is_downloading))
        failed_twitch_analyses = self._failed_twitch_analysis_jobs()
        act_twitch_retry_analysis = twitch_menu.addAction(
            f"Retry failed analysis... ({len(failed_twitch_analyses)})"
        )
        act_twitch_retry_analysis.setEnabled(bool(failed_twitch_analyses))
        act_twitch_cancel_analysis = twitch_menu.addAction("Cancel current analysis")
        act_twitch_cancel_analysis.setEnabled(bool(twitch is not None and twitch.is_analyzing))
        failed_twitch_exports = self._failed_twitch_export_jobs()
        act_twitch_retry_export = twitch_menu.addAction(
            f"Retry failed export... ({len(failed_twitch_exports)})"
        )
        act_twitch_retry_export.setEnabled(bool(failed_twitch_exports))
        act_twitch_cancel_export = twitch_menu.addAction("Cancel current export")
        act_twitch_cancel_export.setEnabled(bool(twitch is not None and twitch.is_exporting))
        failed_uploads = self._failed_twitch_upload_jobs()
        act_retry_upload = twitch_menu.addAction(f"Retry YouTube delivery... ({len(failed_uploads)})")
        act_retry_upload.setEnabled(bool(failed_uploads))
        act_cancel_upload = twitch_menu.addAction("Cancel YouTube upload")
        act_cancel_upload.setEnabled(bool(twitch is not None and twitch.is_uploading))
        ready_twitch_projects = self._ready_twitch_project_jobs()
        act_twitch_open_project = twitch_menu.addAction(
            f"Open ready project... ({len(ready_twitch_projects)})"
        )
        act_twitch_open_project.setEnabled(bool(ready_twitch_projects))
        ready_twitch_exports = self._ready_twitch_upload_jobs()
        act_twitch_open_export = twitch_menu.addAction(
            f"Open exported video... ({len(ready_twitch_exports)})"
        )
        act_twitch_open_export.setEnabled(bool(ready_twitch_exports))
        twitch_menu.addSeparator()
        act_twitch_download_folder = twitch_menu.addAction("Set download folder...")
        act_twitch_open_download_folder = twitch_menu.addAction("Open download folder")

        menu.addSeparator()
        language_menu = menu.addMenu("Language / Lingua")
        act_language_en = language_menu.addAction("English")
        act_language_en.setCheckable(True)
        act_language_en.setChecked(self._ui_language == "en")
        act_language_it = language_menu.addAction("Italiano")
        act_language_it.setCheckable(True)
        act_language_it.setChecked(self._ui_language == "it")

        help_menu = menu.addMenu("Help")
        act_quick_start = help_menu.addAction(ui_text("quick_start", self._ui_language))
        act_diagnostics = help_menu.addAction(ui_text("diagnostics", self._ui_language))
        act_third_party = help_menu.addAction(ui_text("third_party", self._ui_language))
        help_menu.addSeparator()
        act_about = help_menu.addAction(ui_text("about", self._ui_language))

        menu.addSeparator()
        act_reset_defaults = menu.addAction("Reset app to defaults...")

        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            self._app_log("menu_topbar_closed_no_selection")
            return

        if chosen == act_twitch_client_id:
            self._configure_twitch_client_id()
            return
        if chosen == act_twitch_connect:
            self._connect_twitch_account()
            return
        if chosen == act_twitch_disconnect:
            self._disconnect_twitch_account()
            return
        if chosen == act_twitch_watcher:
            self._set_twitch_watcher_enabled(bool(act_twitch_watcher.isChecked()))
            return
        if chosen == act_twitch_poll:
            self._poll_twitch_now()
            return
        if chosen == act_twitch_pending:
            self._configure_next_twitch_job()
            return
        if chosen == act_twitch_retry_download:
            self._retry_twitch_download()
            return
        if chosen == act_twitch_cancel_download:
            self._cancel_twitch_download()
            return
        if chosen == act_twitch_retry_analysis:
            self._retry_twitch_analysis()
            return
        if chosen == act_twitch_cancel_analysis:
            self._cancel_twitch_analysis()
            return
        if chosen == act_twitch_retry_export:
            self._retry_twitch_export()
            return
        if chosen == act_twitch_cancel_export:
            self._cancel_twitch_export()
            return
        if chosen == act_retry_upload:
            if twitch is not None and failed_uploads:
                twitch.queue_upload(failed_uploads[-1].id)
            return
        if chosen == act_cancel_upload:
            if twitch is not None:
                twitch.cancel_upload()
            return
        if chosen == act_twitch_open_project:
            self._open_latest_twitch_project()
            return
        if chosen == act_twitch_open_export:
            self._open_latest_twitch_export()
            return
        if chosen == act_twitch_download_folder:
            self._configure_twitch_download_folder()
            return
        if chosen == act_twitch_open_download_folder:
            self._open_twitch_download_folder()
            return

        if chosen in (act_language_en, act_language_it):
            self._ui_language = "it" if chosen == act_language_it else "en"
            QSettings("Auto Cutter", "Auto Cutter").setValue("ui_language", self._ui_language)
            self._apply_core_translations()
            self._apply_accessibility_metadata()
            self.statusBar().showMessage(
                "Lingua aggiornata." if self._ui_language == "it" else "Language updated.",
                2500,
            )
            return
        if chosen == act_quick_start:
            self._show_quick_start(force=True)
            return
        if chosen == act_diagnostics:
            self._create_diagnostics_bundle()
            return
        if chosen == act_third_party:
            self._open_third_party_notices()
            return
        if chosen == act_about:
            self._show_about()
            return

        if act_theme_dark is not None and chosen == act_theme_dark:
            self._theme_name = "Dark"
            self._apply_theme_pref()
            self._save_theme_pref()
            self._app_log("menu_topbar_action", action="set_theme", theme="Dark")
            return
        if act_theme_light is not None and chosen == act_theme_light:
            self._theme_name = "Light"
            self._apply_theme_pref()
            self._save_theme_pref()
            self._app_log("menu_topbar_action", action="set_theme", theme="Light")
            return
        if act_theme_system is not None and chosen == act_theme_system:
            self._theme_name = "System"
            self._apply_theme_pref()
            self._save_theme_pref()
            self._app_log("menu_topbar_action", action="set_theme", theme="System")
            return
        if chosen == act_stats:
            self._mini_stats_enabled = not self._mini_stats_enabled
            self._app_log("menu_topbar_action", action="toggle_mini_stats", enabled=self._mini_stats_enabled)
        elif chosen == act_layout:
            self._layout_switch_enabled = not self._layout_switch_enabled
            self._app_log("menu_topbar_action", action="toggle_layout_switch", enabled=self._layout_switch_enabled)
        elif chosen == act_adv:
            self._advanced_enabled = not self._advanced_enabled
            self._app_log("menu_topbar_action", action="toggle_advanced", enabled=self._advanced_enabled)
        elif chosen == act_recent:
            self._recent_enabled = not self._recent_enabled
            self._app_log("menu_topbar_action", action="toggle_recent_files", enabled=self._recent_enabled)
            if not self._recent_enabled:
                try:
                    s = QSettings("Auto Cutter", "Auto Cutter")
                    s.remove("recent_paths_v2")
                    # Legacy single-entry fallback should be removed immediately
                    # when recent files are disabled.
                    s.remove("last_input_path")
                    # Keep session payload only if it is still needed by
                    # remember-filters / remember-cuts.
                    if not self._remember_filters_enabled and not self._remember_cuts_enabled:
                        s.remove("last_session_items")
                except Exception:
                    pass
        elif chosen == act_remember:
            self._remember_filters_enabled = not self._remember_filters_enabled
            self._app_log("menu_topbar_action", action="toggle_remember_filters", enabled=self._remember_filters_enabled)
            if self._remember_filters_enabled:
                self._save_last_filters()
        elif chosen == act_remember_cuts:
            self._remember_cuts_enabled = not self._remember_cuts_enabled
            self._app_log("menu_topbar_action", action="toggle_remember_cuts", enabled=self._remember_cuts_enabled)
            if self._remember_cuts_enabled:
                self._save_last_cuts()
        elif act_recovery_toggle is not None and chosen == act_recovery_toggle:
            self._crash_autosave_enabled = not self._crash_autosave_enabled
            self._apply_crash_recovery_prefs()
            if not self._crash_autosave_enabled:
                try:
                    # Avoid prompting stale snapshots when autosave is disabled.
                    self._clear_crash_recovery_artifacts(clear_snapshot=True)
                    self._write_crash_recovery_lock()
                except Exception:
                    pass
            self._app_log(
                "menu_topbar_action",
                action="toggle_recovery_autosave",
                enabled=bool(self._crash_autosave_enabled),
                interval_seconds=int(self._crash_autosave_interval_s),
            )
        elif chosen in recovery_interval_actions:
            self._crash_autosave_interval_s = int(recovery_interval_actions.get(chosen, self._crash_autosave_interval_s))
            self._apply_crash_recovery_prefs()
            self._app_log(
                "menu_topbar_action",
                action="set_recovery_autosave_interval",
                interval_seconds=int(self._crash_autosave_interval_s),
            )
        elif act_recovery_custom is not None and chosen == act_recovery_custom:
            cur = int(self._crash_autosave_interval_s)
            val, ok = pro_get_int(
                self,
                "Recovery autosave interval",
                "Autosave every N seconds (10-600):",
                cur,
                10,
                600,
                5,
            )
            if ok:
                self._crash_autosave_interval_s = int(val)
                self._apply_crash_recovery_prefs()
                self._app_log(
                    "menu_topbar_action",
                    action="set_recovery_autosave_interval_custom",
                    interval_seconds=int(self._crash_autosave_interval_s),
                )
        elif act_reset_defaults is not None and chosen == act_reset_defaults:
            self._app_log("menu_topbar_action", action="reset_app_to_defaults")
            self._reset_app_to_defaults()
            return

        self._apply_ui_prefs()
        self._save_ui_prefs()

    def _save_last_filters(self) -> None:
        try:
            # Ensure active track settings are up-to-date
            try:
                self._save_track_cfg(self._get_active_track())
            except Exception:
                pass
            cfg = self._current_preset_cfg()
            s = QSettings("Auto Cutter", "Auto Cutter")
            s.setValue("last_filters_cfg", json.dumps(cfg))

            items = self._build_session_items(
                include_cfg=True,
                include_cuts=self._remember_cuts_enabled,
            )
            if items:
                s.setValue("last_session_items", json.dumps(items))
        except Exception:
            pass

    def _save_last_cuts(self) -> None:
        try:
            try:
                self._save_track_cfg(self._get_active_track())
            except Exception:
                pass
            s = QSettings("Auto Cutter", "Auto Cutter")
            items = self._build_session_items(
                include_cfg=self._remember_filters_enabled,
                include_cuts=True,
            )
            if items:
                s.setValue("last_session_items", json.dumps(items))
        except Exception:
            pass

    def _reset_app_to_defaults(self) -> None:
        r = QMessageBox.question(
            self,
            "Reset app to defaults",
            (
                "Reset Auto Cutter to default settings?\n\n"
                "This will:\n"
                "- restore default UI/layout preferences\n"
                "- restore default presets (custom presets removed)\n"
                "- clear saved recent/session data\n"
                "- disconnect Twitch automation\n"
                "- reset current workspace"
            ),
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return

        try:
            self._abort_export(wait_ms=0)
        except Exception:
            pass
        try:
            self._abort_analysis_tasks(wait_ms=0)
        except Exception:
            pass
        try:
            self._cancel_warm_cache(wait_ms=0)
        except Exception:
            pass
        try:
            twitch = getattr(self, "_twitch_integration", None)
            if twitch is not None:
                twitch.cancel_download()
                twitch.cancel_analysis()
                twitch.cancel_export()
                twitch.clear_configuration()
            self._twitch_watcher_enabled = False
            self._twitch_client_id = ""
            self._twitch_status = "unconfigured"
        except Exception:
            pass

        try:
            s = QSettings("Auto Cutter", "Auto Cutter")
            s.clear()
            s.sync()
        except Exception:
            pass

        # Reload in-memory defaults from empty settings store.
        try:
            self._load_layout_pref()
        except Exception:
            self._layout_mode = "left"
        try:
            self._load_ui_prefs()
        except Exception:
            self._mini_stats_enabled = True
            self._layout_switch_enabled = True
            self._advanced_enabled = True
            self._recent_enabled = False
            self._remember_filters_enabled = False
            self._remember_cuts_enabled = False
            self._preview_timeline_ratio = 0.72
            self._main_panel_ratio = 0.69
            self._crash_autosave_enabled = True
            self._crash_autosave_interval_s = 45
        try:
            self._load_theme_pref()
        except Exception:
            self._theme_name = "Dark"

        # Force default presets (custom presets removed).
        try:
            self.presets = {
                name: self._normalize_preset_cfg(cfg)
                for name, cfg in self._default_presets_catalog().items()
            }
            self._save_presets()
            self._populate_presets_combo()
            self._pick_default_preset()
            if getattr(self, "_default_preset_name", None):
                idx = self.preset_combo.findText(str(self._default_preset_name))
                if idx >= 0:
                    self.preset_combo.setCurrentIndex(idx)
        except Exception:
            pass

        try:
            self._apply_layout_mode()
        except Exception:
            pass
        try:
            self._apply_ui_prefs()
        except Exception:
            pass
        try:
            self._apply_theme_pref()
        except Exception:
            pass
        try:
            self._save_layout_pref()
            self._save_ui_prefs()
            self._save_theme_pref()
        except Exception:
            pass

        try:
            self._clear_crash_recovery_artifacts(clear_snapshot=True)
        except Exception:
            pass

        try:
            self._reset_workspace(confirm=False)
        except Exception:
            pass

        QMessageBox.information(self, "Reset app to defaults", "Default settings restored.")

    def _init_twitch_automation(self) -> None:
        self._twitch_integration = TwitchIntegration(self)
        self._twitch_client_id = ""
        self._twitch_watcher_enabled = False
        self._twitch_status = "unconfigured"
        self._twitch_poll_interval_s = 120
        self._twitch_download_log_progress: dict[str, int] = {}
        self._twitch_analysis_log_progress: dict[str, int] = {}
        self._twitch_export_log_progress: dict[str, int] = {}

        twitch = self._twitch_integration
        twitch.deviceCodeReady.connect(self._on_twitch_device_code)
        twitch.connected.connect(self._on_twitch_connected)
        twitch.disconnected.connect(self._on_twitch_disconnected)
        twitch.vodDiscovered.connect(self._on_twitch_vod_discovered)
        twitch.error.connect(self._on_twitch_error)
        twitch.statusChanged.connect(self._on_twitch_status_changed)
        twitch.enabledChanged.connect(self._on_twitch_enabled_changed)
        twitch.downloadStarted.connect(self._on_twitch_download_started)
        twitch.downloadProgress.connect(self._on_twitch_download_progress)
        twitch.downloadFinished.connect(self._on_twitch_download_finished)
        twitch.downloadFailed.connect(self._on_twitch_download_failed)
        twitch.analysisStarted.connect(self._on_twitch_analysis_started)
        twitch.analysisProgress.connect(self._on_twitch_analysis_progress)
        twitch.analysisFinished.connect(self._on_twitch_analysis_finished)
        twitch.analysisFailed.connect(self._on_twitch_analysis_failed)
        twitch.exportStarted.connect(self._on_twitch_export_started)
        twitch.exportProgress.connect(self._on_twitch_export_progress)
        twitch.exportDetail.connect(self._on_twitch_export_detail)
        twitch.exportFinished.connect(self._on_twitch_export_finished)
        twitch.exportFailed.connect(self._on_twitch_export_failed)
        twitch.uploadStarted.connect(self._on_twitch_upload_started)
        twitch.uploadProgress.connect(self._on_twitch_upload_progress)
        twitch.uploadFinished.connect(self._on_twitch_upload_finished)
        twitch.uploadFailed.connect(self._on_twitch_upload_failed)

        settings = QSettings("Auto Cutter", "Auto Cutter")
        saved_client_id = str(settings.value("automation/twitch/client_id", "") or "").strip()
        env_client_id = str(os.environ.get("AUTO_CUTTER_TWITCH_CLIENT_ID", "") or "").strip()
        requested_client_id = saved_client_id or env_client_id
        saved_download_dir = str(settings.value("automation/download_dir", "") or "").strip()
        self._twitch_download_dir = Path(saved_download_dir) if saved_download_dir else (
            self._default_twitch_download_dir()
        )
        try:
            twitch.configure_download_dir(self._twitch_download_dir)
        except Exception as exc:
            self._twitch_download_dir = self._default_twitch_download_dir()
            self._app_log("pipeline_download_dir_invalid", error=str(exc))
        try:
            self._twitch_poll_interval_s = max(
                30,
                min(3600, int(settings.value("automation/twitch/poll_interval_s", 120) or 120)),
            )
        except (TypeError, ValueError):
            self._twitch_poll_interval_s = 120
        self._twitch_watcher_enabled = str(
            settings.value("automation/twitch/watcher_enabled", "0") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

        if requested_client_id:
            try:
                self._twitch_client_id = normalize_twitch_client_id(requested_client_id)
                twitch.configure(
                    self._twitch_client_id,
                    poll_interval_s=self._twitch_poll_interval_s,
                )
            except Exception as exc:
                self._twitch_client_id = ""
                self._twitch_watcher_enabled = False
                self._app_log("twitch_configuration_invalid", error=str(exc))

        smoke_test = "--smoke-test" in sys.argv
        if not smoke_test:
            try:
                recovered = twitch.recover_pipeline()
                if recovered:
                    self._app_log("pipeline_jobs_recovered", count=len(recovered))
            except Exception as exc:
                self._app_log("pipeline_recovery_failed", error=str(exc))

        if self._twitch_watcher_enabled and not twitch.has_stored_token:
            self._twitch_watcher_enabled = False
            settings.setValue("automation/twitch/watcher_enabled", 0)
        elif self._twitch_watcher_enabled and not smoke_test:
            QTimer.singleShot(0, self._restore_twitch_watcher)

        self._app_log(
            "twitch_automation_init",
            configured=twitch.is_configured,
            credentials=twitch.has_stored_token,
            watcher_enabled=self._twitch_watcher_enabled,
            download_dir=str(self._twitch_download_dir),
        )

    def _restore_twitch_watcher(self) -> None:
        if not self._set_twitch_watcher_enabled(True):
            self.statusBar().showMessage("Twitch watcher could not be restored.", 5000)

    def _configure_twitch_client_id(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        value, ok = pro_get_text(
            self,
            "Twitch Client ID",
            "Enter the Client ID of your Twitch public application:",
            text=str(twitch.client_id or self._twitch_client_id),
        )
        if not ok:
            return
        try:
            client_id = normalize_twitch_client_id(value)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Twitch Client ID", str(exc))
            return

        if twitch.has_stored_token and client_id != twitch.client_id:
            answer = QMessageBox.question(
                self,
                "Change Twitch Client ID",
                "Changing Client ID disconnects the current Twitch account. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            twitch.disconnect_account()

        try:
            twitch.configure(client_id, poll_interval_s=self._twitch_poll_interval_s)
        except Exception as exc:
            QMessageBox.warning(self, "Twitch configuration", str(exc))
            return

        self._twitch_client_id = client_id
        self._twitch_watcher_enabled = False
        settings = QSettings("Auto Cutter", "Auto Cutter")
        settings.setValue("automation/twitch/client_id", client_id)
        settings.setValue("automation/twitch/poll_interval_s", self._twitch_poll_interval_s)
        settings.setValue("automation/twitch/watcher_enabled", 0)
        self._app_log("twitch_client_configured")
        self.statusBar().showMessage("Twitch Client ID saved. Connect the account next.", 5000)

    def _connect_twitch_account(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.is_configured:
            QMessageBox.warning(self, "Twitch", "Configure a Twitch Client ID first.")
            return
        if twitch.is_authenticating:
            self.statusBar().showMessage("Twitch connection is already in progress.", 4000)
            return
        if twitch.begin_connect():
            self._app_log("twitch_auth_started")
            self.statusBar().showMessage("Requesting a Twitch activation code...", 5000)

    def _disconnect_twitch_account(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.has_stored_token:
            return
        answer = QMessageBox.question(
            self,
            "Disconnect Twitch",
            "Disconnect Twitch and remove the encrypted credentials from this PC?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            twitch.disconnect_account()
        except Exception as exc:
            QMessageBox.warning(self, "Disconnect Twitch", str(exc))

    def _set_twitch_watcher_enabled(self, enabled: bool) -> bool:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return False
        if not twitch.set_enabled(enabled):
            return False
        self._twitch_watcher_enabled = bool(enabled)
        QSettings("Auto Cutter", "Auto Cutter").setValue(
            "automation/twitch/watcher_enabled",
            1 if enabled else 0,
        )
        self._app_log("twitch_watcher_changed", enabled=bool(enabled))
        return True

    def _poll_twitch_now(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is not None and twitch.poll_now():
            self._app_log("twitch_poll_requested")
            self.statusBar().showMessage("Checking Twitch for a new VOD...", 5000)

    def _default_twitch_download_dir(self) -> Path:
        movies = str(QStandardPaths.writableLocation(QStandardPaths.MoviesLocation) or "").strip()
        root = Path(movies) if movies else Path.home() / "Videos"
        return root / "Auto Cutter" / "Automation"

    def _configure_twitch_download_folder(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        current = Path(getattr(self, "_twitch_download_dir", self._default_twitch_download_dir()))
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Twitch download folder",
            str(current),
        )
        if not selected:
            return
        try:
            target = Path(selected).expanduser().resolve()
            twitch.configure_download_dir(target)
            self._twitch_download_dir = target
            QSettings("Auto Cutter", "Auto Cutter").setValue("automation/download_dir", str(target))
            self._app_log("pipeline_download_dir_changed", path=str(target))
            self.statusBar().showMessage(f"Twitch downloads: {target}", 6000)
        except Exception as exc:
            QMessageBox.warning(self, "Twitch download folder", str(exc))

    def _open_twitch_download_folder(self) -> None:
        target = Path(getattr(self, "_twitch_download_dir", self._default_twitch_download_dir()))
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Twitch download folder", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            QMessageBox.warning(self, "Twitch download folder", f"Could not open:\n{target}")

    @Slot(str, str, int)
    def _on_twitch_device_code(self, code: str, verification_uri: str, expires_in: int) -> None:
        try:
            QApplication.clipboard().setText(code)
        except Exception:
            pass
        opened = QDesktopServices.openUrl(QUrl(verification_uri))
        browser_note = "The Twitch page was opened in your browser." if opened else (
            f"Open this address manually: {verification_uri}"
        )
        QMessageBox.information(
            self,
            "Connect Twitch",
            (
                f"Enter this code on Twitch:\n\n{code}\n\n"
                f"{browser_note}\nThe code was copied to the clipboard and expires in {expires_in} seconds."
            ),
        )

    @Slot(str)
    def _on_twitch_connected(self, login: str) -> None:
        self._app_log("twitch_auth_connected", login=login)
        self.statusBar().showMessage(f"Twitch connected as @{login}.", 6000)
        if self._twitch_watcher_enabled:
            self._set_twitch_watcher_enabled(True)
            return
        answer = QMessageBox.question(
            self,
            "Twitch connected",
            f"Connected as @{login}. Enable automatic monitoring for new VODs?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._set_twitch_watcher_enabled(True)

    @Slot()
    def _on_twitch_disconnected(self) -> None:
        self._twitch_watcher_enabled = False
        QSettings("Auto Cutter", "Auto Cutter").setValue("automation/twitch/watcher_enabled", 0)
        self._app_log("twitch_auth_disconnected")
        self.statusBar().showMessage("Twitch account disconnected.", 5000)

    @Slot(bool)
    def _on_twitch_enabled_changed(self, enabled: bool) -> None:
        self._twitch_watcher_enabled = bool(enabled)
        QSettings("Auto Cutter", "Auto Cutter").setValue(
            "automation/twitch/watcher_enabled",
            1 if enabled else 0,
        )
        state = "enabled" if enabled else "disabled"
        self.statusBar().showMessage(f"Twitch VOD watcher {state}.", 4000)

    @Slot(str)
    def _on_twitch_status_changed(self, status: str) -> None:
        self._twitch_status = str(status or "unknown")
        self._app_log("twitch_status", status=self._twitch_status)
        if self._twitch_status == "checking":
            self.statusBar().showMessage("Checking Twitch for a new VOD...", 5000)
        elif self._twitch_status == "watching":
            self.statusBar().showMessage("Twitch VOD watcher is active.", 4000)
        elif self._twitch_status == "connected":
            self.statusBar().showMessage("Twitch check completed.", 4000)
        elif self._twitch_status == "download_failed":
            self.statusBar().showMessage("Twitch VOD download failed. Retry it from the menu.", 8000)
        elif self._twitch_status == "export_queued":
            self.statusBar().showMessage("Automatic analysis complete. Export queued...", 5000)
        elif self._twitch_status == "exporting":
            self.statusBar().showMessage("Exporting the automatic project...", 5000)
        elif self._twitch_status == "ready_upload":
            self.statusBar().showMessage("Automatic export complete. Video ready for upload.", 8000)
        elif self._twitch_status == "export_failed":
            self.statusBar().showMessage("Automatic export failed. Retry it from the menu.", 8000)

    @Slot(str)
    def _on_twitch_error(self, message: str) -> None:
        clean_message = str(message or "Unknown Twitch error.")
        self._app_log("twitch_error", error=clean_message)
        self.statusBar().showMessage(f"Twitch: {clean_message}", 10000)

    @Slot(object, object)
    def _on_twitch_vod_discovered(self, job: object, video: object) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._app_log("twitch_vod_discovered", job_id=job.id, vod_id=job.vod_id)
        duration_s = int(getattr(video, "duration_s", 0) or 0)
        duration_text = f" ({fmt_hms(duration_s)})" if duration_s > 0 else ""
        title = job.source_title or f"VOD {job.vod_id}"
        answer = QMessageBox.question(
            self,
            "New Twitch VOD",
            f"A new VOD is available{duration_text}:\n\n{title}\n\nSelect its start and end now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._configure_twitch_job(job.id)

    def _pending_twitch_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            return [
                job
                for job in twitch.manager.list_jobs(include_terminal=False)
                if job.state in {PipelineState.DISCOVERED, PipelineState.WAITING_RANGE}
            ]
        except Exception as exc:
            self._app_log("pipeline_jobs_read_failed", error=str(exc))
            return []

    def _failed_twitch_download_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            return [
                job
                for job in twitch.manager.list_jobs(include_terminal=True)
                if ((job.state == PipelineState.FAILED and job.retry_state == PipelineState.DOWNLOADING)
                    or (job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") == "downloading"))
            ]
        except Exception as exc:
            self._app_log("pipeline_failed_downloads_read_failed", error=str(exc))
            return []

    def _failed_twitch_analysis_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            return [
                job
                for job in twitch.manager.list_jobs(include_terminal=True)
                if ((job.state == PipelineState.FAILED and job.retry_state == PipelineState.ANALYZING)
                    or (job.state == PipelineState.CANCELLED and job.metadata.get("cancelled_from") == "analyzing"))
            ]
        except Exception as exc:
            self._app_log("pipeline_failed_analyses_read_failed", error=str(exc))
            return []

    def _failed_twitch_export_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            retry_states = {PipelineState.READY_EXPORT, PipelineState.EXPORTING}
            return [
                job
                for job in twitch.manager.list_jobs(include_terminal=True)
                if ((job.state == PipelineState.FAILED and job.retry_state in retry_states)
                    or (job.state == PipelineState.CANCELLED
                        and job.metadata.get("cancelled_from") in {"ready_export", "exporting"}))
            ]
        except Exception as exc:
            self._app_log("pipeline_failed_exports_read_failed", error=str(exc))
            return []

    def _ready_twitch_upload_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            jobs = [job for job in twitch.manager.list_jobs(include_terminal=True)
                    if job.state in {PipelineState.READY_UPLOAD, PipelineState.UPLOADING, PipelineState.DONE}
                    and bool(job.export_path) and Path(str(job.export_path)).is_file()]
            return sorted(jobs, key=lambda job: str(job.updated_at))
        except Exception as exc:
            self._app_log("pipeline_ready_exports_read_failed", error=str(exc))
            return []

    def _failed_twitch_upload_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        states = {PipelineState.READY_UPLOAD, PipelineState.UPLOADING}
        return [job for job in twitch.manager.list_jobs(include_terminal=True)
                if ((job.state == PipelineState.FAILED and job.retry_state in states)
                    or (job.state == PipelineState.CANCELLED
                        and job.metadata.get("cancelled_from") in {state.value for state in states}))
                and job.metadata.get("delivery", {}).get("youtube") is True]

    @Slot(str)
    def _on_twitch_upload_started(self, job_id: str) -> None:
        self._app_log("youtube_upload_started", job_id=job_id, privacy="private")
        self.statusBar().showMessage("Uploading to YouTube PRIVATE...", 5000)

    @Slot(str, int)
    def _on_twitch_upload_progress(self, job_id: str, progress: int) -> None:
        self.statusBar().showMessage(f"YouTube PRIVATE upload: {progress}%", 5000)

    @Slot(object)
    def _on_twitch_upload_finished(self, job: object) -> None:
        if isinstance(job, PipelineJob):
            self._app_log("youtube_upload_finished", job_id=job.id, video_id=job.youtube_video_id)
            self.statusBar().showMessage(f"YouTube PRIVATE video ready: {job.youtube_video_id}", 15000)

    @Slot(object, str)
    def _on_twitch_upload_failed(self, job: object, message: str) -> None:
        self._app_log("youtube_upload_failed", message=message)
        self.statusBar().showMessage(f"YouTube delivery failed: {message}", 15000)

    def _ready_twitch_project_jobs(self) -> list[PipelineJob]:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return []
        try:
            jobs = [
                job
                for job in twitch.manager.list_jobs(include_terminal=True)
                if job.state in {PipelineState.READY_EXPORT, PipelineState.EXPORTING,
                                 PipelineState.READY_UPLOAD, PipelineState.UPLOADING, PipelineState.DONE}
                and bool(job.project_path)
                and Path(str(job.project_path)).is_file()
            ]
            return sorted(jobs, key=lambda job: str(job.updated_at))
        except Exception as exc:
            self._app_log("pipeline_ready_projects_read_failed", error=str(exc))
            return []

    def _retry_twitch_download(self) -> None:
        failed = self._failed_twitch_download_jobs()
        if not failed:
            self.statusBar().showMessage("No failed Twitch download is waiting for retry.", 4000)
            return
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        job = failed[-1]
        try:
            queued = twitch.queue_download(job.id)
        except Exception as exc:
            QMessageBox.warning(self, "Retry Twitch download", str(exc))
            return
        if queued:
            self._app_log("twitch_download_retry_queued", job_id=job.id)
            self.statusBar().showMessage("Twitch VOD download queued for retry.", 5000)

    def _retry_twitch_analysis(self) -> None:
        failed = self._failed_twitch_analysis_jobs()
        if not failed:
            self.statusBar().showMessage("No failed automatic analysis is waiting for retry.", 4000)
            return
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        job = failed[-1]
        try:
            queued = twitch.queue_analysis(job.id)
        except Exception as exc:
            QMessageBox.warning(self, "Retry automatic analysis", str(exc))
            return
        if queued:
            self._app_log("twitch_analysis_retry_queued", job_id=job.id)
            self.statusBar().showMessage("Automatic analysis queued for retry.", 5000)

    def _retry_twitch_export(self) -> None:
        failed = self._failed_twitch_export_jobs()
        if not failed:
            self.statusBar().showMessage("No failed automatic export is waiting for retry.", 4000)
            return
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        job = failed[-1]
        try:
            queued = twitch.queue_export(job.id)
        except Exception as exc:
            QMessageBox.warning(self, "Retry automatic export", str(exc))
            return
        if queued:
            self._app_log("twitch_export_retry_queued", job_id=job.id)
            self.statusBar().showMessage("Automatic export queued for retry.", 5000)

    def _cancel_twitch_download(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.is_downloading:
            return
        answer = QMessageBox.question(
            self,
            "Cancel Twitch download",
            "Cancel the current download? It will remain available for retry.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes and twitch.cancel_download():
            self._app_log("twitch_download_cancel_requested", job_id=twitch.active_download_job_id)
            self.statusBar().showMessage("Cancelling Twitch VOD download...", 5000)

    def _cancel_twitch_analysis(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.is_analyzing:
            return
        answer = QMessageBox.question(
            self,
            "Cancel automatic analysis",
            "Cancel the current analysis? It will remain available for retry.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes and twitch.cancel_analysis():
            self._app_log("twitch_analysis_cancel_requested", job_id=twitch.active_analysis_job_id)
            self.statusBar().showMessage("Cancelling automatic analysis...", 5000)

    def _cancel_twitch_export(self) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None or not twitch.is_exporting:
            return
        answer = QMessageBox.question(
            self,
            "Cancel automatic export",
            "Cancel the current export? It will remain available for retry.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes and twitch.cancel_export():
            self._app_log("twitch_export_cancel_requested", job_id=twitch.active_export_job_id)
            self.statusBar().showMessage("Cancelling automatic export...", 5000)

    def _open_latest_twitch_project(self) -> None:
        ready = self._ready_twitch_project_jobs()
        if not ready:
            self.statusBar().showMessage("No automatically analyzed project is ready.", 4000)
            return
        if self._analysis_in_progress() or self._export_in_progress():
            QMessageBox.warning(
                self,
                "Open automatic project",
                "Stop the current analysis or export before opening another project.",
            )
            return
        job = ready[-1]
        project_path = Path(str(job.project_path)).resolve()
        try:
            raw = json.loads(project_path.read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.warning(self, "Open automatic project", str(exc))
            return
        if not isinstance(raw, dict):
            QMessageBox.warning(self, "Open automatic project", "The generated project has invalid structure.")
            return
        if self._load_project_payload(raw, source_path=project_path, source_label="Automatic project"):
            self._app_log("twitch_project_opened", job_id=job.id, path=str(project_path))

    def _open_latest_twitch_export(self) -> None:
        ready = self._ready_twitch_upload_jobs()
        if not ready:
            self.statusBar().showMessage("No automatic export is ready.", 4000)
            return
        job = ready[-1]
        export_path = Path(str(job.export_path)).resolve()
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(export_path))):
            QMessageBox.warning(self, "Open exported video", f"Could not open:\n{export_path}")
            return
        self._app_log("twitch_export_opened", job_id=job.id, path=str(export_path))

    @Slot(str)
    def _on_twitch_download_started(self, job_id: str) -> None:
        self._twitch_download_log_progress[job_id] = -10
        self._app_log("twitch_download_started", job_id=job_id)
        self.statusBar().showMessage("Downloading the selected Twitch VOD range: 0%", 5000)

    @Slot(str, int)
    def _on_twitch_download_progress(self, job_id: str, progress: int) -> None:
        value = max(0, min(100, int(progress)))
        self.statusBar().showMessage(f"Downloading Twitch VOD: {value}%", 5000)
        previous = int(self._twitch_download_log_progress.get(job_id, -10))
        if value >= 100 or value - previous >= 10:
            self._twitch_download_log_progress[job_id] = value
            self._app_log("twitch_download_progress", job_id=job_id, progress=value)

    @Slot(object)
    def _on_twitch_download_finished(self, job: object) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_download_log_progress.pop(job.id, None)
        self._app_log(
            "twitch_download_finished",
            job_id=job.id,
            path=str(job.local_source_path or ""),
        )
        self.statusBar().showMessage("Twitch VOD downloaded. Starting automatic analysis...", 8000)

    @Slot(object, str)
    def _on_twitch_download_failed(self, job: object, message: str) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_download_log_progress.pop(job.id, None)
        clean_message = str(message or job.error_message or "Unknown download error.")
        self._app_log(
            "twitch_download_failed",
            job_id=job.id,
            code=str(job.error_code or "download_failed"),
            error=clean_message,
        )
        QMessageBox.warning(
            self,
            "Twitch VOD download failed",
            f"{clean_message}\n\nUse Twitch automation > Retry failed download to try again.",
        )

    @Slot(str)
    def _on_twitch_analysis_started(self, job_id: str) -> None:
        self._twitch_analysis_log_progress[job_id] = -10
        self._app_log("twitch_analysis_started", job_id=job_id)
        self.statusBar().showMessage("Analyzing downloaded Twitch VOD: 0%", 5000)

    @Slot(str, int)
    def _on_twitch_analysis_progress(self, job_id: str, progress: int) -> None:
        value = max(0, min(100, int(progress)))
        self.statusBar().showMessage(f"Analyzing downloaded Twitch VOD: {value}%", 5000)
        previous = int(self._twitch_analysis_log_progress.get(job_id, -10))
        if value >= 100 or value - previous >= 10:
            self._twitch_analysis_log_progress[job_id] = value
            self._app_log("twitch_analysis_progress", job_id=job_id, progress=value)

    @Slot(object)
    def _on_twitch_analysis_finished(self, job: object) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_analysis_log_progress.pop(job.id, None)
        self._app_log(
            "twitch_analysis_finished",
            job_id=job.id,
            project_path=str(job.project_path or ""),
        )
        self.statusBar().showMessage("Automatic analysis complete. Starting export...", 8000)

    @Slot(object, str)
    def _on_twitch_analysis_failed(self, job: object, message: str) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_analysis_log_progress.pop(job.id, None)
        clean_message = str(message or job.error_message or "Unknown analysis error.")
        self._app_log(
            "twitch_analysis_failed",
            job_id=job.id,
            code=str(job.error_code or "analysis_failed"),
            error=clean_message,
        )
        QMessageBox.warning(
            self,
            "Automatic analysis failed",
            f"{clean_message}\n\nUse Twitch automation > Retry failed analysis to try again.",
        )

    @Slot(str)
    def _on_twitch_export_started(self, job_id: str) -> None:
        self._twitch_export_log_progress[job_id] = -10
        self._app_log("twitch_export_started", job_id=job_id)
        self.statusBar().showMessage("Exporting automatic project: 0%", 5000)

    @Slot(str, int)
    def _on_twitch_export_progress(self, job_id: str, progress: int) -> None:
        value = max(0, min(100, int(progress)))
        self.statusBar().showMessage(f"Exporting automatic project: {value}%", 5000)
        previous = int(self._twitch_export_log_progress.get(job_id, -10))
        if value >= 100 or value - previous >= 10:
            self._twitch_export_log_progress[job_id] = value
            self._app_log("twitch_export_progress", job_id=job_id, progress=value)

    @Slot(str, str)
    def _on_twitch_export_detail(self, job_id: str, message: str) -> None:
        self._app_log("twitch_export_detail", job_id=job_id, detail=str(message))

    @Slot(object)
    def _on_twitch_export_finished(self, job: object) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_export_log_progress.pop(job.id, None)
        self._app_log("twitch_export_finished", job_id=job.id, path=str(job.export_path or ""))
        self.statusBar().showMessage("Automatic export complete. Video ready for upload.", 10000)
        QMessageBox.information(
            self,
            "Automatic export ready",
            (
                "The selected VOD range was analyzed and exported successfully.\n\n"
                f"Video:\n{job.export_path}\n\n"
                "Open it from Twitch automation > Open exported video."
            ),
        )

    @Slot(object, str)
    def _on_twitch_export_failed(self, job: object, message: str) -> None:
        if not isinstance(job, PipelineJob):
            return
        self._twitch_export_log_progress.pop(job.id, None)
        clean_message = str(message or job.error_message or "Unknown export error.")
        self._app_log(
            "twitch_export_failed",
            job_id=job.id,
            code=str(job.error_code or "export_failed"),
            error=clean_message,
        )
        QMessageBox.warning(
            self,
            "Automatic export failed",
            f"{clean_message}\n\nUse Twitch automation > Retry failed export to try again.",
        )

    def _configure_next_twitch_job(self) -> None:
        pending = self._pending_twitch_jobs()
        if not pending:
            self.statusBar().showMessage("No Twitch VOD is waiting for a range.", 4000)
            return
        self._configure_twitch_job(pending[-1].id)

    def _configure_twitch_job(self, job_id: str) -> None:
        twitch = getattr(self, "_twitch_integration", None)
        if twitch is None:
            return
        try:
            job = twitch.manager.get(job_id)
            if job.state == PipelineState.DISCOVERED:
                job = twitch.manager.request_range(job.id)
            if job.state != PipelineState.WAITING_RANGE:
                QMessageBox.information(
                    self,
                    "Twitch VOD",
                    f"This VOD is already in pipeline state: {job.state.value}.",
                )
                return

            twitch_metadata = job.metadata.get("twitch", {})
            duration_value = twitch_metadata.get("duration_s", 0) if isinstance(twitch_metadata, dict) else 0
            duration_s = max(1, int(duration_value or 0))
            if duration_s <= 1:
                duration_s = 24 * 60 * 60
            start_s, ok = pro_get_int(
                self,
                "Twitch VOD range",
                f"Start time in seconds (VOD duration: {fmt_hms(duration_s)}):",
                0,
                0,
                duration_s - 1,
                1,
            )
            if not ok:
                return
            end_s, ok = pro_get_int(
                self,
                "Twitch VOD range",
                f"End time in seconds (after {fmt_hms(start_s)}):",
                duration_s,
                start_s + 1,
                duration_s,
                1,
            )
            if not ok:
                return

            preset_reader = getattr(self, "_current_preset_cfg", None)
            combo = getattr(self, "preset_combo", None)
            preset_name = str(combo.currentText()) if combo is not None else "Balanced (Default)"
            preset_config = (preset_reader() if callable(preset_reader)
                             else PresetRepository().resolve("Balanced (Default)"))
            export_reader = getattr(self, "_export_settings_from_ui", None)
            export_settings = export_reader() if callable(export_reader) else ExportSettings.defaults()
            twitch.manager.update_metadata(job.id, {
                "preset": {"name": preset_name, "config": normalize_preset_cfg(preset_config), "version": 1},
                "export_settings": export_settings.to_mapping(),
                "delivery": {"output_dir": str(Path(self._twitch_download_dir).expanduser().resolve()),
                             "download_dir": str(Path(self._twitch_download_dir).expanduser().resolve()),
                             "keep_source": True, "youtube": False},
            })
            queued = twitch.manager.select_range(job.id, start_s, end_s)
            self._app_log(
                "twitch_vod_range_selected",
                job_id=queued.id,
                start_s=start_s,
                end_s=end_s,
            )
            started = twitch.queue_download(queued.id)
            if started:
                QMessageBox.information(
                    self,
                    "Twitch VOD download",
                    (
                        f"Range saved: {fmt_hms(start_s)} - {fmt_hms(end_s)}.\n\n"
                        f"The download has started in:\n{self._twitch_download_dir}"
                    ),
                )
            else:
                self.statusBar().showMessage("This Twitch VOD is already queued for download.", 5000)
        except Exception as exc:
            self._app_log("twitch_vod_range_failed", job_id=job_id, error=str(exc))
            QMessageBox.warning(self, "Twitch VOD", str(exc))

    def _project_file_default_path(self) -> Path:
        if isinstance(getattr(self, "_project_file_path", None), Path):
            try:
                return Path(self._project_file_path)
            except Exception:
                pass
        for t in getattr(self, "_tracks", []) or []:
            p = str(getattr(t, "path", "") or "").strip()
            if not p:
                continue
            try:
                stem = Path(p).stem or "project"
                return Path(p).parent / f"{stem}.autocutter"
            except Exception:
                continue
        return Path.home() / "project.autocutter"

    def _crash_recovery_dir(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base:
            base = str(Path.home() / ".autocutter")
        d = Path(base) / "crash_recovery"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _crash_recovery_lock_path(self) -> Path:
        return self._crash_recovery_dir() / "running.lock"

    def _crash_recovery_snapshot_path(self) -> Path:
        return self._crash_recovery_dir() / "last_session.recovery.autocutter"

    def _write_crash_recovery_lock(self) -> None:
        p = self._crash_recovery_lock_path()
        payload = {
            "pid": int(os.getpid()),
            "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def _clear_crash_recovery_artifacts(self, *, clear_snapshot: bool) -> None:
        for p in (self._crash_recovery_lock_path(),):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        if clear_snapshot:
            try:
                sp = self._crash_recovery_snapshot_path()
                if sp.exists():
                    sp.unlink()
            except Exception:
                pass

    def _apply_crash_recovery_prefs(self) -> None:
        self._crash_autosave_interval_s = max(10, min(600, int(self._crash_autosave_interval_s or 45)))
        t = getattr(self, "_crash_autosave_timer", None)
        if t is None:
            return
        try:
            t.setInterval(int(self._crash_autosave_interval_s * 1000))
        except Exception:
            pass
        if bool(self._crash_autosave_enabled):
            if not t.isActive():
                t.start()
        else:
            if t.isActive():
                t.stop()

    def _init_crash_recovery(self) -> None:
        try:
            lock_exists = self._crash_recovery_lock_path().exists()
            snap_exists = self._crash_recovery_snapshot_path().exists()
            self._crash_recovery_offer_pending = bool(lock_exists and snap_exists)
        except Exception:
            self._crash_recovery_offer_pending = False

        try:
            self._write_crash_recovery_lock()
        except Exception as e:
            self._app_log("crash_recovery_lock_write_failed", error=str(e))

        self._crash_autosave_timer = QTimer(self)
        self._crash_autosave_timer.setSingleShot(False)
        self._crash_autosave_timer.timeout.connect(self._autosave_crash_recovery_snapshot)
        self._apply_crash_recovery_prefs()
        self._app_log(
            "crash_recovery_init",
            interval_seconds=int(self._crash_autosave_interval_s),
            enabled=bool(self._crash_autosave_enabled),
            offer_pending=bool(self._crash_recovery_offer_pending),
        )

    def _autosave_crash_recovery_snapshot(self) -> None:
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        try:
            payload = self._build_project_file_payload()
        except Exception:
            return
        tracks = payload.get("tracks") if isinstance(payload, dict) else None
        if not isinstance(tracks, list) or not tracks:
            return

        payload["recovery"] = {
            "autosaved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "autosave_interval_seconds": int(self._crash_autosave_interval_s),
            "kind": "crash_recovery",
        }

        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return
        sig = hashlib.sha1(serialized.encode("utf-8")).hexdigest()
        if sig == str(getattr(self, "_last_crash_autosave_sig", "")):
            return

        out = self._crash_recovery_snapshot_path()
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp")
            tmp.write_text(serialized, encoding="utf-8")
            tmp.replace(out)
            self._last_crash_autosave_sig = sig
            self._app_log("crash_recovery_autosave", path=str(out), tracks=len(tracks))
        except Exception as e:
            self._app_log("crash_recovery_autosave_failed", error=str(e))

    def _offer_crash_recovery_if_needed(self) -> bool:
        if not bool(getattr(self, "_crash_recovery_offer_pending", False)):
            return False
        self._crash_recovery_offer_pending = False

        snap = self._crash_recovery_snapshot_path()
        if not snap.exists():
            return False
        try:
            raw = json.loads(snap.read_text(encoding="utf-8"))
        except Exception as e:
            self._app_log("crash_recovery_read_failed", path=str(snap), error=str(e))
            return False
        if not isinstance(raw, dict):
            return False

        rec = raw.get("recovery", {}) if isinstance(raw.get("recovery", {}), dict) else {}
        ts = str(rec.get("autosaved_at", "") or raw.get("saved_at", "") or "").strip()
        msg = "A previous session did not close cleanly.\n\nRecover last autosaved session?"
        if ts:
            msg = f"{msg}\n\nAutosave: {ts}"

        answer = QMessageBox.question(
            self,
            "Recover last session",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self._app_log("crash_recovery_skipped", path=str(snap))
            try:
                snap.unlink()
            except Exception:
                pass
            return False

        loaded = self._load_project_payload(raw, source_path=snap, source_label="Crash recovery")
        if loaded:
            self._app_log("crash_recovery_loaded", path=str(snap))
        else:
            self._app_log("crash_recovery_load_failed", path=str(snap))
        return bool(loaded)

    def _build_project_file_payload(self) -> dict[str, Any]:
        try:
            self._save_track_cfg(self._get_active_track())
        except Exception:
            pass
        active = self._get_active_track()
        try:
            self._save_workspace_for_mode(active)
        except Exception:
            pass

        payload: dict[str, Any] = {
            "format": PROJECT_FORMAT,
            "version": PROJECT_VERSION,
            "saved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "project_name": str(getattr(self.project, "name", "Untitled") or "Untitled"),
            "global": {
                "analysis_mode": str(getattr(self, "analysis_mode", "classic") or "classic"),
                "active_track_index": int(getattr(self, "_active_track_index", 0) or 0),
                "skip_preview": bool(self.chk_skip.isChecked()),
                "preset_name": str(self.preset_combo.currentText() or "Manual"),
                "preset_dirty": bool(getattr(self, "_preset_dirty", False)),
                "preset_source_name": (
                    str(getattr(self, "_preset_source_name", "") or "")
                    if getattr(self, "_preset_source_name", None)
                    else None
                ),
                "codec": str(self.codec_combo.currentData() or ""),
                "export_method": str(self.export_method_combo.currentData() or ""),
                "cut_quality_index": int(self.cut_quality_combo.currentIndex()),
                "parallel_workers": int(self.parallel_workers_spin.value()),
                "chunk_count": int(self.chunk_count_spin.value()),
                "hwaccel_decode": bool(self.hwaccel_cb.isChecked()),
                "export_settings": self._export_settings_from_ui().to_mapping(),
            },
            "tracks": [
                *self._build_session_items(include_cfg=True, include_cuts=True),
                *[dict(item) for item in getattr(self, "_offline_project_items", [])],
            ],
        }
        return payload

    def _apply_project_global_state(self, global_state: dict[str, Any]) -> None:
        if not isinstance(global_state, dict):
            return

        # Export/UI settings
        try:
            export_raw = global_state.get("export_settings")
            if isinstance(export_raw, dict):
                project_export_settings = ExportSettings.from_mapping(export_raw)
                self._apply_export_settings_to_ui(project_export_settings)
                self._save_export_settings(project_export_settings)
        except Exception:
            pass
        try:
            codec = str(global_state.get("codec", "") or "").strip()
            if codec:
                idx = int(self.codec_combo.findData(codec))
                if idx >= 0:
                    self.codec_combo.setCurrentIndex(idx)
        except Exception:
            pass
        try:
            method = str(global_state.get("export_method", "") or "").strip()
            if method:
                idx = int(self.export_method_combo.findData(method))
                if idx >= 0:
                    self.export_method_combo.setCurrentIndex(idx)
        except Exception:
            pass
        try:
            qidx = int(global_state.get("cut_quality_index", 0) or 0)
            qidx = max(0, min(qidx, int(self.cut_quality_combo.count()) - 1))
            self.cut_quality_combo.setCurrentIndex(qidx)
        except Exception:
            pass
        try:
            pw = int(global_state.get("parallel_workers", int(self.parallel_workers_spin.value())))
            pw = max(int(self.parallel_workers_spin.minimum()), min(int(self.parallel_workers_spin.maximum()), pw))
            self.parallel_workers_spin.setValue(pw)
        except Exception:
            pass
        try:
            ch = int(global_state.get("chunk_count", int(self.chunk_count_spin.value())))
            ch = max(int(self.chunk_count_spin.minimum()), min(int(self.chunk_count_spin.maximum()), ch))
            self.chunk_count_spin.setValue(ch)
        except Exception:
            pass
        try:
            self.hwaccel_cb.setChecked(bool(global_state.get("hwaccel_decode", bool(self.hwaccel_cb.isChecked()))))
        except Exception:
            pass
        try:
            self.chk_skip.blockSignals(True)
            self.chk_skip.setChecked(bool(global_state.get("skip_preview", bool(self.chk_skip.isChecked()))))
        finally:
            try:
                self.chk_skip.blockSignals(False)
            except Exception:
                pass
        try:
            self._on_skip_changed()
        except Exception:
            pass

        # Preset label state (without forcing re-application)
        try:
            preset_name = str(global_state.get("preset_name", "") or "").strip()
            if preset_name:
                idx = int(self.preset_combo.findText(preset_name))
                if idx >= 0:
                    self.preset_combo.blockSignals(True)
                    self.preset_combo.setCurrentIndex(idx)
                    self.preset_combo.blockSignals(False)
        except Exception:
            pass
        try:
            self._preset_dirty = bool(global_state.get("preset_dirty", False))
            src = global_state.get("preset_source_name", None)
            self._preset_source_name = str(src) if isinstance(src, str) and src.strip() else None
            self._update_preset_ui_state()
        except Exception:
            pass

        # Active track
        try:
            idx = int(global_state.get("active_track_index", int(getattr(self, "_active_track_index", 0) or 0)))
            if 0 <= idx < len(self._tracks):
                self._activate_track(idx, sync_players=True)
        except Exception:
            pass

        # Analysis mode (restore after track activation)
        try:
            mode = str(global_state.get("analysis_mode", getattr(self, "analysis_mode", "classic")) or "classic").strip().lower()
            desired_ai = (mode == "ai")
            if bool(self.analysis_mode_toggle.isChecked()) != desired_ai:
                self.analysis_mode_toggle.setChecked(desired_ai)
        except Exception:
            pass

    def save_project_file(self) -> None:
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            self.statusBar().showMessage("Wait for reset to finish before saving project.", 2500)
            return
        payload = self._build_project_file_payload()
        tracks = payload.get("tracks") if isinstance(payload, dict) else None
        if not isinstance(tracks, list) or not tracks:
            QMessageBox.information(self, "Save project", "No loaded tracks to save yet.")
            return

        suggested = self._project_file_default_path()
        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save project",
            str(suggested),
            "Auto Cutter Project (*.autocutter)",
        )
        if not out_path:
            return
        out = Path(out_path)
        if out.suffix.lower() != ".autocutter":
            out = out.with_suffix(".autocutter")

        try:
            payload = make_payload_portable(payload, out)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(out)
            self._project_file_path = out
            try:
                self.project.name = str(out.stem or "Untitled")
            except Exception:
                pass
            self.statusBar().showMessage(f"Project saved: {out.name}", 3000)
            self._app_log("project_save_done", path=str(out), tracks=len(tracks))
        except Exception as e:
            self._app_log("project_save_failed", path=str(out), error=str(e))
            QMessageBox.critical(self, "Save project failed", str(e))

    def _load_project_payload(
        self,
        raw: dict[str, Any],
        *,
        source_path: Path | None = None,
        source_label: str = "Project",
    ) -> bool:
        try:
            raw = normalize_project_payload(raw)
        except ProjectFormatError as exc:
            QMessageBox.critical(self, f"{source_label} load failed", str(exc))
            return False
        items = raw["tracks"]

        valid_items, missing_items = resolve_project_items(items, source_path)
        if missing_items and source_path is not None:
            search = QMessageBox.question(
                self,
                "Missing media",
                (
                    f"{len(missing_items)} media file(s) are offline.\n\n"
                    "Search for them in another folder?"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if search == QMessageBox.Yes:
                search_dir = QFileDialog.getExistingDirectory(
                    self,
                    "Choose a folder containing the missing media",
                    str(source_path.parent),
                )
                if search_dir:
                    relinked, missing_items = relink_items_in_directory(missing_items, Path(search_dir))
                    valid_items.extend(relinked)

        if missing_items:
            missing_names = [
                str(item.get("media_name") or Path(str(item.get("path", "") or "")).name or "unknown")
                for item in missing_items
            ]
            preview = "\n".join(f"- {name}" for name in missing_names[:8])
            if len(missing_names) > 8:
                preview += f"\n- and {len(missing_names) - 8} more"
            keep_offline = QMessageBox.question(
                self,
                "Keep media offline",
                (
                    "These media files are still unavailable:\n\n"
                    f"{preview}\n\n"
                    "Open the project and keep their edits for a future relink?"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if keep_offline != QMessageBox.Yes:
                return False

        # Preserve session keys because _perform_workspace_reset clears them by design.
        settings_backup: dict[str, Any] = {}
        try:
            s = QSettings("Auto Cutter", "Auto Cutter")
            settings_backup["last_input_path"] = s.value("last_input_path", None)
            settings_backup["last_session_items"] = s.value("last_session_items", None)
        except Exception:
            settings_backup = {}

        self._perform_workspace_reset()
        self._offline_project_items = [dict(item) for item in missing_items]

        if settings_backup:
            try:
                s = QSettings("Auto Cutter", "Auto Cutter")
                for k, v in settings_backup.items():
                    if v is None:
                        s.remove(k)
                    else:
                        s.setValue(k, v)
            except Exception:
                pass

        self._in_auto_restore_session = True
        try:
            for entry in valid_items:
                restore_cfg = entry.get("cfg") if isinstance(entry.get("cfg"), dict) else None
                self._open_path(str(entry.get("path")), restore_cfg=restore_cfg, restore_state=entry)
            try:
                self._finalize_timeline_reorder()
            except Exception:
                pass
        finally:
            self._in_auto_restore_session = False

        if source_path is not None:
            try:
                self.project.name = str(raw.get("project_name", source_path.stem) or source_path.stem)
            except Exception:
                pass
            self._project_file_path = source_path
        else:
            try:
                self.project.name = str(raw.get("project_name", getattr(self.project, "name", "Untitled")) or "Untitled")
            except Exception:
                pass

        self._apply_project_global_state(raw.get("global", {}))
        self._web_push_full_state()
        self._push_topbar_status_chips()
        self.statusBar().showMessage(
            f"{source_label} loaded: {source_path.name if isinstance(source_path, Path) else 'session'}",
            3500,
        )
        self._app_log(
            "project_load_done",
            source=source_label,
            path=str(source_path) if isinstance(source_path, Path) else "",
            tracks_loaded=len(valid_items),
            tracks_missing=len(missing_items),
        )
        return True

    def load_project_file(self) -> None:
        if self._analysis_in_progress() or self._export_in_progress():
            QMessageBox.warning(
                self,
                "Load project",
                "Stop current analysis/export before loading a project.",
            )
            return

        suggested = self._project_file_default_path()
        in_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load project",
            str(suggested.parent if suggested.parent.exists() else Path.home()),
            "Auto Cutter Project (*.autocutter)",
        )
        if not in_path:
            return
        src = Path(in_path)

        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.critical(self, "Load project failed", f"Invalid project file:\n{e}")
            return
        self._load_project_payload(raw, source_path=src, source_label="Project")

    def _segments_to_payload(self, segs: list[Segment] | None) -> list[dict]:
        out: list[dict] = []
        if not segs:
            return out
        for s in segs:
            try:
                a = float(s.start)
                b = float(s.end)
            except Exception:
                continue
            if b > a:
                out.append({"start": a, "end": b})
        return out

    def _segments_from_payload(self, raw: object) -> list[Segment]:
        if not raw:
            return []
        out: list[Segment] = []
        if isinstance(raw, list):
            items = raw
        else:
            return out
        for item in items:
            a = b = None
            if isinstance(item, dict):
                a = item.get("start")
                b = item.get("end")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                a, b = item[0], item[1]
            try:
                fa = float(a)
                fb = float(b)
            except Exception:
                continue
            if fb > fa:
                out.append(Segment(fa, fb))
        return out

    def _apply_restore_state(self, track: TrackState, restore_state: Optional[dict]) -> bool:
        if not isinstance(restore_state, dict):
            return False
        keys = (
            "cuts",
            "keeps",
            "manual_cuts",
            "suppressed_cuts",
            "classic_cuts",
            "classic_keeps",
            "classic_manual_cuts",
            "classic_suppressed_cuts",
            "classic_cuts_enabled",
            "ai_cuts",
            "ai_keeps",
            "ai_manual_cuts",
            "ai_suppressed_cuts",
            "ai_cuts_enabled",
            "segment_source_in",
            "segment_source_out",
            "segment_group_id",
            "segment_index",
            "copy_source_id",
            "copy_index",
            "duration",
            "video_color",
            "video_edge",
            "ai_speech",
            "ai_speech_raw",
            "ai_speaker_ids",
            "cuts_enabled",
        )
        has_payload = any(k in restore_state for k in keys)
        if not has_payload:
            return False

        try:
            if restore_state.get("segment_group_id"):
                track.segment_group_id = str(restore_state.get("segment_group_id"))
        except Exception:
            pass
        try:
            if restore_state.get("segment_index") is not None:
                track.segment_index = int(restore_state.get("segment_index") or 1)
        except Exception:
            pass
        try:
            if restore_state.get("copy_source_id"):
                track.copy_source_id = str(restore_state.get("copy_source_id"))
        except Exception:
            pass
        try:
            if restore_state.get("copy_index") is not None:
                track.copy_index = int(restore_state.get("copy_index") or 0)
        except Exception:
            pass
        try:
            if restore_state.get("segment_source_in") is not None:
                track.segment_source_in = float(restore_state.get("segment_source_in") or 0.0)
            if restore_state.get("segment_source_out") is not None:
                track.segment_source_out = float(restore_state.get("segment_source_out") or 0.0)
        except Exception:
            pass
        try:
            dur = float(restore_state.get("duration") or 0.0)
            if dur > 0:
                track.duration = dur
        except Exception:
            pass
        try:
            seg_in = float(getattr(track, "segment_source_in", 0.0) or 0.0)
            seg_out = float(getattr(track, "segment_source_out", 0.0) or 0.0)
            if seg_out > seg_in + 1e-6:
                track.duration = max(0.0, seg_out - seg_in)
        except Exception:
            pass

        try:
            col = restore_state.get("video_color", None)
            if isinstance(col, (list, tuple)) and len(col) >= 4:
                track.video_color = tuple(int(x) for x in col[:4])
        except Exception:
            pass
        try:
            edge = restore_state.get("video_edge", None)
            if isinstance(edge, (list, tuple)) and len(edge) >= 3:
                track.video_edge = tuple(int(x) for x in edge[:3])
        except Exception:
            pass

        restored_cuts = self._segments_from_payload(restore_state.get("cuts"))
        restored_keeps = self._segments_from_payload(restore_state.get("keeps"))
        manual = self._segments_from_payload(restore_state.get("manual_cuts"))
        suppressed = self._segments_from_payload(restore_state.get("suppressed_cuts"))

        # Restore the full cuts snapshot when available (generated + manual + suppressed state).
        track.cuts = restored_cuts
        track.keeps = restored_keeps
        track.manual_cuts = manual
        track.suppressed_cuts = suppressed
        if (not track.keeps) and track.cuts and float(getattr(track, "duration", 0.0) or 0.0) > 0:
            try:
                track.keeps = invert_to_keeps(float(track.duration), list(track.cuts or []), min_keep=0.0)
            except Exception:
                track.keeps = []
        try:
            track.cuts_enabled = bool(
                restore_state.get("cuts_enabled", bool(track.cuts or track.keeps or manual or suppressed))
            )
        except Exception:
            track.cuts_enabled = bool(track.cuts or track.keeps or manual or suppressed)

        try:
            track.ai_speech = self._segments_from_payload(restore_state.get("ai_speech"))
        except Exception:
            track.ai_speech = []
        try:
            track.ai_speech_raw = self._segments_from_payload(restore_state.get("ai_speech_raw"))
        except Exception:
            track.ai_speech_raw = []
        try:
            speaker_ids = restore_state.get("ai_speaker_ids", None)
            if isinstance(speaker_ids, list) and speaker_ids:
                track.ai_speaker_ids = [int(x) for x in speaker_ids]
            else:
                track.ai_speaker_ids = None
        except Exception:
            track.ai_speaker_ids = None

        try:
            track.classic_cuts = self._segments_from_payload(restore_state.get("classic_cuts"))
            track.classic_keeps = self._segments_from_payload(restore_state.get("classic_keeps"))
            track.classic_manual_cuts = self._segments_from_payload(restore_state.get("classic_manual_cuts"))
            track.classic_suppressed_cuts = self._segments_from_payload(restore_state.get("classic_suppressed_cuts"))
            track.classic_cuts_enabled = bool(
                restore_state.get(
                    "classic_cuts_enabled",
                    bool(
                        track.classic_cuts
                        or track.classic_keeps
                        or track.classic_manual_cuts
                        or track.classic_suppressed_cuts
                    ),
                )
            )
        except Exception:
            track.classic_cuts = []
            track.classic_keeps = []
            track.classic_manual_cuts = []
            track.classic_suppressed_cuts = []
            track.classic_cuts_enabled = False

        try:
            track.ai_cuts = self._segments_from_payload(restore_state.get("ai_cuts"))
            track.ai_keeps = self._segments_from_payload(restore_state.get("ai_keeps"))
            track.ai_manual_cuts = self._segments_from_payload(restore_state.get("ai_manual_cuts"))
            track.ai_suppressed_cuts = self._segments_from_payload(restore_state.get("ai_suppressed_cuts"))
            track.ai_cuts_enabled = bool(
                restore_state.get(
                    "ai_cuts_enabled",
                    bool(track.ai_cuts or track.ai_keeps or track.ai_manual_cuts or track.ai_suppressed_cuts),
                )
            )
        except Exception:
            track.ai_cuts = []
            track.ai_keeps = []
            track.ai_manual_cuts = []
            track.ai_suppressed_cuts = []
            track.ai_cuts_enabled = False

        if not (track.classic_cuts or track.classic_keeps or track.classic_manual_cuts or track.classic_suppressed_cuts):
            track.classic_cuts = list(track.cuts or [])
            track.classic_keeps = list(track.keeps or [])
            track.classic_manual_cuts = list(track.manual_cuts or [])
            track.classic_suppressed_cuts = list(track.suppressed_cuts or [])
            track.classic_cuts_enabled = bool(getattr(track, "cuts_enabled", False))

        track.cuts_restored = bool(track.cuts or track.keeps or manual or suppressed)
        return True

    def _apply_track_cuts_ui(self, track: TrackState) -> None:
        out_dur = sum(k.dur for k in track.keeps) if track.keeps else 0.0
        cuts_n = len(track.cuts or [])

        self.lbl_footer.setText(
            f"Duration {fmt_hms(float(track.duration))}  -  Output {fmt_hms(out_dur)}  -  {cuts_n} cuts"
        )
        export_enabled = bool(track.keeps) and out_dur > 0.01
        self.btn_export.setEnabled(export_enabled)
        self.btn_export_edl.setEnabled(export_enabled)
        if export_enabled:
            self._set_stage("export")
        elif getattr(track, "cuts_enabled", False):
            self._set_stage("review")
        else:
            self._set_stage("analyze")

        if self.stats_duration:
            dur = self._fmt_time(float(track.duration))
            out = self._fmt_time(out_dur)
            self.stats_duration.setText(f"Duration: {dur}")
            self.stats_output.setText(f"Output: {out}")
            self.stats_cuts.setText(f"Cuts: {cuts_n}")
            kept_pct = (out_dur / float(track.duration) * 100.0) if track.duration > 0 else 0.0
            self.stats_kept.setText(f"Kept: {kept_pct:.0f}%")
        if self.analysis_mode != "ai":
            self._save_classic_cuts(track)

        self._web_push_full_state()

    def _has_classic_workspace(self, track: TrackState | None) -> bool:
        if track is None:
            return False
        return bool(
            getattr(track, "classic_cuts", None)
            or getattr(track, "classic_keeps", None)
            or getattr(track, "classic_manual_cuts", None)
            or getattr(track, "classic_suppressed_cuts", None)
        )

    def _has_ai_workspace(self, track: TrackState | None) -> bool:
        if track is None:
            return False
        return bool(
            getattr(track, "ai_cuts", None)
            or getattr(track, "ai_keeps", None)
            or getattr(track, "ai_manual_cuts", None)
            or getattr(track, "ai_suppressed_cuts", None)
            or getattr(track, "ai_speech", None)
            or getattr(track, "ai_speech_raw", None)
        )

    def _save_ai_cuts(self, track: TrackState) -> None:
        track.ai_cuts = self._clone_segments_list(getattr(track, "cuts", []))
        track.ai_keeps = self._clone_segments_list(getattr(track, "keeps", []))
        track.ai_manual_cuts = self._clone_segments_list(getattr(track, "manual_cuts", []))
        track.ai_suppressed_cuts = self._clone_segments_list(getattr(track, "suppressed_cuts", []))
        track.ai_cuts_enabled = bool(getattr(track, "cuts_enabled", False))

    def _restore_ai_cuts(self, track: TrackState) -> bool:
        if track.ai_cuts or track.ai_keeps or track.ai_manual_cuts or track.ai_suppressed_cuts:
            track.cuts = list(track.ai_cuts or [])
            track.keeps = list(track.ai_keeps or [])
            track.manual_cuts = list(track.ai_manual_cuts or [])
            track.suppressed_cuts = list(track.ai_suppressed_cuts or [])
            track.cuts_enabled = bool(
                getattr(
                    track,
                    "ai_cuts_enabled",
                    bool(track.cuts or track.keeps or track.manual_cuts or track.suppressed_cuts),
                )
            )
            return True
        return False

    def _save_workspace_for_mode(self, track: TrackState | None, mode: str | None = None) -> None:
        if track is None:
            return
        key = str(mode or getattr(self, "analysis_mode", "classic") or "classic").strip().lower()
        if key == "ai":
            self._save_ai_cuts(track)
        else:
            self._save_classic_cuts(track)

    def _finalize_manual_cut_edit_without_reanalysis(self, track: TrackState | None) -> None:
        """
        Manual add/remove cut should not rerun auto detection.
        We preserve the currently visible cuts timeline and rebuild keeps/stats/UI only.
        """
        if track is None:
            return
        try:
            track.cuts = merge_overlaps(list(getattr(track, "cuts", None) or []))
        except Exception:
            track.cuts = []

        min_keep = 0.0
        try:
            if str(getattr(self, "analysis_mode", "classic")) != "ai":
                cfg = getattr(track, "cfg", None)
                if isinstance(cfg, dict) and cfg:
                    try:
                        cfg = self._normalize_preset_cfg(dict(cfg))
                    except Exception:
                        cfg = dict(cfg)
                    intensity = int(cfg.get("intensity", self.slider_precision.value()))
                else:
                    intensity = int(self.slider_precision.value())
                if intensity >= 10:
                    _min_silence, _edge_keep, min_keep = map_intensity(intensity)
        except Exception:
            min_keep = 0.0

        try:
            track.keeps = invert_to_keeps(float(track.duration), list(track.cuts or []), min_keep=float(min_keep))
        except Exception:
            track.keeps = []

        self._save_workspace_for_mode(track)
        self._refresh_analysis_workspace_ui(track)
        self._apply_track_cuts_ui(track)

    def _restore_workspace_for_mode(self, track: TrackState | None, mode: str | None = None) -> bool:
        if track is None:
            return False
        key = str(mode or getattr(self, "analysis_mode", "classic") or "classic").strip().lower()
        if key == "ai":
            return self._restore_ai_cuts(track)
        self._restore_classic_cuts(track)
        return True

    def _refresh_analysis_workspace_ui(self, track: TrackState | None) -> None:
        if track is None:
            return
        self._refresh_timeline_tracks(reset_view=False)
        try:
            self._set_pending_cut_visual(
                track.pending_cut_start,
                track.pending_cut_end,
                track_state_idx=self._active_track_index,
            )
        except Exception:
            pass
        self._apply_track_cuts_ui(track)
        self._update_ai_stats_panel(track)
        self._update_ai_options_panel(track)

    def _build_session_items(self, include_cfg: bool, include_cuts: bool = False) -> list[dict]:
        items: list[dict] = []
        for t in self._tracks:
            path = getattr(t, "path", None)
            if not path:
                continue
            item: dict = {"path": str(path)}
            if include_cfg:
                cfg = getattr(t, "cfg", None)
                if isinstance(cfg, dict) and cfg:
                    item["cfg"] = self._normalize_preset_cfg(dict(cfg))
            if include_cuts:
                if t is self._get_active_track():
                    try:
                        self._save_workspace_for_mode(t)
                    except Exception:
                        pass
                item["cuts_enabled"] = bool(getattr(t, "cuts_enabled", False))
                item["cuts"] = self._segments_to_payload(getattr(t, "cuts", None))
                item["keeps"] = self._segments_to_payload(getattr(t, "keeps", None))
                item["manual_cuts"] = self._segments_to_payload(getattr(t, "manual_cuts", None))
                item["suppressed_cuts"] = self._segments_to_payload(getattr(t, "suppressed_cuts", None))
                item["segment_group_id"] = getattr(t, "segment_group_id", None)
                item["segment_index"] = int(getattr(t, "segment_index", 1) or 1)
                item["copy_source_id"] = getattr(t, "copy_source_id", None)
                item["copy_index"] = int(getattr(t, "copy_index", 0) or 0)
                item["segment_source_in"] = float(getattr(t, "segment_source_in", 0.0) or 0.0)
                item["segment_source_out"] = float(getattr(t, "segment_source_out", 0.0) or 0.0)
                item["duration"] = float(getattr(t, "duration", 0.0) or 0.0)
                if getattr(t, "video_color", None) is not None:
                    item["video_color"] = list(getattr(t, "video_color"))
                if getattr(t, "video_edge", None) is not None:
                    item["video_edge"] = list(getattr(t, "video_edge"))
                if getattr(t, "ai_speech", None):
                    item["ai_speech"] = self._segments_to_payload(getattr(t, "ai_speech", None))
                if getattr(t, "ai_speech_raw", None):
                    item["ai_speech_raw"] = self._segments_to_payload(getattr(t, "ai_speech_raw", None))
                if getattr(t, "ai_speaker_ids", None):
                    try:
                        item["ai_speaker_ids"] = [int(x) for x in list(getattr(t, "ai_speaker_ids") or [])]
                    except Exception:
                        pass
                item["classic_cuts_enabled"] = bool(getattr(t, "classic_cuts_enabled", False))
                item["classic_cuts"] = self._segments_to_payload(getattr(t, "classic_cuts", None))
                item["classic_keeps"] = self._segments_to_payload(getattr(t, "classic_keeps", None))
                item["classic_manual_cuts"] = self._segments_to_payload(getattr(t, "classic_manual_cuts", None))
                item["classic_suppressed_cuts"] = self._segments_to_payload(getattr(t, "classic_suppressed_cuts", None))
                item["ai_cuts_enabled"] = bool(getattr(t, "ai_cuts_enabled", False))
                item["ai_cuts"] = self._segments_to_payload(getattr(t, "ai_cuts", None))
                item["ai_keeps"] = self._segments_to_payload(getattr(t, "ai_keeps", None))
                item["ai_manual_cuts"] = self._segments_to_payload(getattr(t, "ai_manual_cuts", None))
                item["ai_suppressed_cuts"] = self._segments_to_payload(getattr(t, "ai_suppressed_cuts", None))
            items.append(item)
        return items

    def _build_recent_paths(self) -> list[str]:
        # Recent files should remember only the media paths that were opened,
        # not full track/session state (segments/copies/cuts/etc.).
        out: list[str] = []
        seen: set[str] = set()
        for t in self._tracks:
            p = str(getattr(t, "path", "") or "").strip()
            if not p:
                continue
            key = os.path.normcase(os.path.normpath(p))
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _load_recent_paths(self) -> list[str]:
        s = QSettings("Auto Cutter", "Auto Cutter")
        raw = s.value("recent_paths_v2", "")
        paths: list[str] = []
        if isinstance(raw, list):
            paths = [str(x) for x in raw if str(x).strip()]
        elif raw:
            try:
                data = json.loads(str(raw))
                if isinstance(data, list):
                    paths = [str(x) for x in data if str(x).strip()]
            except Exception:
                paths = []

        # Fallback for older versions: derive paths from session payload or legacy key.
        if not paths:
            try:
                for it in self._load_last_session_items():
                    p = str((it or {}).get("path", "")).strip()
                    if p:
                        paths.append(p)
            except Exception:
                pass
        if not paths:
            try:
                last_path = str(s.value("last_input_path", "") or "").strip()
                if last_path:
                    paths = [last_path]
            except Exception:
                paths = []

        cleaned: list[str] = []
        seen: set[str] = set()
        for p in paths:
            sp = str(p).strip()
            if not sp:
                continue
            key = os.path.normcase(os.path.normpath(sp))
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(sp)
        return cleaned

    def _load_last_session_items(self) -> list[dict]:
        s = QSettings("Auto Cutter", "Auto Cutter")
        raw = s.value("last_session_items", "")
        items = []
        if isinstance(raw, list):
            items = raw
        elif raw:
            try:
                items = json.loads(raw)
            except Exception:
                items = []

        # Fallback to legacy single-entry keys
        if not items:
            last_path = s.value("last_input_path", "")
            if last_path:
                item = {"path": str(last_path)}
                raw_cfg = s.value("last_filters_cfg", "")
                if raw_cfg:
                    try:
                        cfg = json.loads(raw_cfg)
                        if isinstance(cfg, dict) and cfg:
                            item["cfg"] = cfg
                    except Exception:
                        pass
                items = [item]

        cleaned: list[dict] = []
        for it in items:
            if isinstance(it, str):
                path = it
                cfg = None
                extra = {}
            elif isinstance(it, dict):
                path = it.get("path")
                cfg = it.get("cfg")
                extra = dict(it)
            else:
                continue
            if not path:
                continue
            entry = {"path": str(path)}
            if isinstance(cfg, dict) and cfg:
                entry["cfg"] = cfg
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k in ("path", "cfg"):
                        continue
                    entry[k] = v
            cleaned.append(entry)
        return cleaned

    def _load_last_filters_cfg(self) -> Optional[dict]:
        s = QSettings("Auto Cutter", "Auto Cutter")
        raw_cfg = s.value("last_filters_cfg", "")
        if not raw_cfg:
            return None
        try:
            cfg = json.loads(raw_cfg)
        except Exception:
            return None
        if isinstance(cfg, dict) and cfg:
            return cfg
        return None

    def _apply_cfg_from_dict(self, cfg: dict) -> None:
        if not cfg:
            return
        cfg = self._normalize_preset_cfg(cfg)
        self._applying_preset = True
        try:
            self.slider_precision.setValue(int(cfg.get("intensity", self.slider_precision.value())))
            self.threshold_pct.setValue(int(cfg.get("threshold_pct", self.threshold_pct.value())))
            self.pre_pad_s.setValue(float(cfg.get("pre_pad_s", 0.25)))
            self.post_pad_s.setValue(float(cfg.get("post_pad_s", 0.25)))
            self.min_cut_s.setValue(float(cfg.get("min_cut_s", 0.10)))
            self.gain_db.setValue(float(cfg.get("gain_db", 0.0)))
            self.gain_affects_detection.setChecked(bool(cfg.get("gain_affects_detection", False)))
            self.attack_ms.setValue(int(cfg.get("attack_ms", self.attack_ms_default)))
            self.release_ms.setValue(int(cfg.get("release_ms", self.release_ms_default)))
            self.smoothing_mode.setCurrentText(str(cfg.get("smoothing_mode", self.smoothing_mode_default)))
            self.merge_pauses_ms.setValue(int(cfg.get("merge_pauses_ms", self.merge_pauses_ms_default)))
            self.normalize_lufs.setChecked(bool(cfg.get("normalize_lufs", self.normalize_lufs_default)))
            self.lufs_target.setValue(float(cfg.get("lufs_target", self.lufs_target_default)))
            self.limiter.setChecked(bool(cfg.get("limiter", self.limiter_default)))
        finally:
            self._applying_preset = False

        # force Manual preset label
        if hasattr(self, "preset_combo"):
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(0)
            self.preset_combo.blockSignals(False)
        if hasattr(self, "btn_preset_delete"):
            self.btn_preset_delete.setEnabled(False)

    def _auto_restore_session(self) -> None:
        recent_paths = self._load_recent_paths()
        session_items = self._load_last_session_items()
        # Only the "Recent files" toggle should auto-open media on startup.
        # If remember-cuts/filters is enabled and session payload exists, prefer the
        # richer session payload so edits/cuts can be restored.
        should_open = bool(recent_paths) and bool(self._recent_enabled)
        use_session_restore = bool(should_open and session_items and (self._remember_filters_enabled or self._remember_cuts_enabled))
        self._app_log(
            "auto_restore_session_start",
            recent_paths=len(recent_paths),
            session_items=len(session_items),
            recent_enabled=bool(self._recent_enabled),
            remember_filters=bool(self._remember_filters_enabled),
            remember_cuts=bool(self._remember_cuts_enabled),
            should_open=bool(should_open),
            use_session_restore=bool(use_session_restore),
        )
        self._media_dbg(
            f"auto_restore_session start recent_paths={len(recent_paths)} recent={self._recent_enabled} "
            f"session_items={len(session_items)} remember_filters={self._remember_filters_enabled} "
            f"remember_cuts={self._remember_cuts_enabled} should_open={should_open} "
            f"use_session_restore={use_session_restore}"
        )
        self._in_auto_restore_session = True
        try:
            # Remember-filters may still preload the last tuning profile, but we do
            # not restore per-track/cut session state during recent auto-open.
            if self._remember_filters_enabled:
                self._pending_restore_filters = self._load_last_filters_cfg()
            else:
                self._pending_restore_filters = None

            if should_open:
                opened_any = False
                if use_session_restore:
                    startup_entries: list[dict] = []
                    for it in session_items:
                        if not isinstance(it, dict):
                            continue
                        p = str(it.get("path", "") or "").strip()
                        if not p:
                            continue
                        startup_entries.append(dict(it))
                else:
                    startup_entries = [{"path": str(p)} for p in recent_paths]

                for entry in startup_entries:
                    try:
                        p = Path(str(entry.get("path", "")))
                    except Exception:
                        continue
                    if not p.exists():
                        self._app_log("auto_restore_item_missing", path=str(p))
                        continue
                    restore_cfg = entry.get("cfg") if (self._remember_filters_enabled and use_session_restore) else None
                    restore_state = entry if (self._remember_cuts_enabled and use_session_restore) else None
                    self._app_log(
                        "auto_restore_item_open",
                        path=str(p),
                        restore_cfg=bool(isinstance(restore_cfg, dict) and restore_cfg),
                        restore_state=bool(isinstance(restore_state, dict)),
                    )
                    self._media_dbg(
                        f"auto_restore item path='{Path(str(p)).name}' exists=1 "
                        f"path_only={0 if use_session_restore else 1} "
                        f"restore_cfg={bool(isinstance(restore_cfg, dict) and restore_cfg)} "
                        f"restore_state={bool(isinstance(restore_state, dict))}"
                    )
                    self._open_path(str(p), restore_cfg=restore_cfg, restore_state=restore_state)
                    opened_any = True

                if opened_any:
                    if use_session_restore:
                        # Prevent a stale global pending-filters payload from leaking
                        # into later manual opens when every startup item already had cfg.
                        self._pending_restore_filters = None
                    try:
                        self._finalize_timeline_reorder()
                    except Exception:
                        pass
                    # Defer a post-startup audio source reload/resync.
                    self._startup_restore_audio_resync_pending = True
                    self._startup_restore_audio_resync_remaining = 2
                    try:
                        QTimer.singleShot(0, self._post_startup_restore_audio_resync)
                        QTimer.singleShot(220, self._post_startup_restore_audio_resync)
                    except Exception:
                        pass
                    self._media_dbg("auto_restore_session opened_any=1 finalize_reorder done")
                    self._app_log("auto_restore_session_done", opened_any=True)
                    return

            # No auto-open (or failed): keep defaults, but optionally preload last filters.
            self._media_dbg("auto_restore_session no auto-open (or failed)")
            self._app_log("auto_restore_session_done", opened_any=False)
        finally:
            self._in_auto_restore_session = False

    def _post_startup_restore_audio_resync(self) -> None:
        if not bool(getattr(self, "_startup_restore_audio_resync_pending", False)):
            return
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            self._media_dbg("startup_audio_resync skipped (workspace reset pending)")
            return
        if not self.input_path:
            self._startup_restore_audio_resync_pending = False
            self._media_dbg("startup_audio_resync skipped (no input_path)")
            return

        try:
            total = float(self._global_duration if self._global_duration > 0 else self.duration)
        except Exception:
            total = 0.0
        try:
            t = float(getattr(self, "_last_pos", 0.0) or 0.0)
        except Exception:
            t = 0.0
        if total > 0.0:
            t = max(0.0, min(float(t), float(total)))
        else:
            t = max(0.0, float(t))

        # Force a clean audio decoder/source reload after startup restore.
        self._media_dbg(
            f"startup_audio_resync begin t={t:.3f} remaining={int(getattr(self, '_startup_restore_audio_resync_remaining', 0))}"
        )
        self._media_debug_snapshot("before_startup_audio_resync")
        try:
            self.audio_player.pause()
        except Exception:
            pass
        try:
            self._set_audio_player_source(None)
        except Exception:
            pass
        try:
            self._audio_segment_index = -1
            self._audio_seek_guard_until = 0.0
        except Exception:
            pass
        try:
            self._sync_audio_to_global(t, force=True)
        except Exception:
            pass
        try:
            self._apply_preview_volume(int(getattr(self, "_preview_volume_pct", 100)))
        except Exception:
            pass
        try:
            if self.video_player.playbackState() != QMediaPlayer.PlayingState:
                self.audio_player.pause()
        except Exception:
            pass
        self._media_debug_snapshot("after_startup_audio_resync")
        try:
            remaining = int(getattr(self, "_startup_restore_audio_resync_remaining", 0))
        except Exception:
            remaining = 0
        remaining = max(0, remaining - 1)
        self._startup_restore_audio_resync_remaining = remaining
        if remaining <= 0:
            self._startup_restore_audio_resync_pending = False
        self._media_dbg(
            f"startup_audio_resync end remaining={remaining} pending={self._startup_restore_audio_resync_pending}"
        )

    def _switch_page(self, idx: int) -> None:
        self.pages.setCurrentIndex(idx)
        self.seg_main.setChecked(idx == 0)
        self.seg_export.setChecked(idx == 1)
        try:
            self._sync_adv_ui()
        except Exception:
            pass

    # -----------------------------
    # Theme + Icons
    # -----------------------------
    def _sync_icons(self) -> None:
        # theme.py sets window._c_text, _c_muted etc and then calls this method :contentReference[oaicite:5]{index=5}
        c = getattr(self, "_c_text", None)
        if c is None:
            return

        size = 18

        # Top actions
        self.btn_open.setIcon(self.icons.first_icon(
            ["actions/export.svg", "misc/info.svg"],
            size, c 
        ))

        # Transport (play/pause depends on state)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.btn_play.setIcon(self.icons.first_icon(["transport/pause.svg"], size, c))
        else:
            self.btn_play.setIcon(self.icons.first_icon(["transport/play.svg"], size, c))

        self.btn_zoom_out.setIcon(self.icons.first_icon(
            ["zoom/zoom-out.svg"],
            size, c
        ))
        self.btn_zoom_in.setIcon(self.icons.first_icon(
            ["zoom/zoom-in.svg"],
            size, c
        ))
        self.btn_zoom_reset.setIcon(self.icons.first_icon(
            ["zoom/zoom-reset.svg"],
            size, c
        ))
        if hasattr(self, "btn_cut_tool") and self.btn_cut_tool is not None:
            self.btn_cut_tool.setIcon(self.icons.first_icon(
                ["actions/cut.svg", "transport/sscissors.svg", "transport/scissors.svg"],
                size, c
            ))
        if hasattr(self, "btn_split") and self.btn_split is not None:
            self.btn_split.setIcon(self.icons.first_icon(
                ["transport/sscissors.svg", "transport/scissors.svg", "actions/cut.svg"],
                size, c
            ))

        # Advanced header control uses lock/unlock icon
        if self._adv_open:
            icon = self.icons.first_icon(["ui/unlock.svg"], size, c)
        else:
            icon = self.icons.first_icon(["ui/lock.svg"], size, c)
        self.btn_adv_toggle.setIcon(icon)

        # Preset actions
        self.btn_preset_save.setIcon(
            self.icons.first_icon(["actions/save.svg"], size, c)
        )
        if hasattr(self, "btn_preset_save_as") and self.btn_preset_save_as is not None:
            self.btn_preset_save_as.setIcon(
                self.icons.first_icon(["actions/save.svg"], size, c)
            )
        if hasattr(self, "btn_preset_manage") and self.btn_preset_manage is not None:
            self.btn_preset_manage.setIcon(
                self.icons.first_icon(["misc/info.svg", "ui/unlock.svg"], size, c)
            )
        self.btn_preset_delete.setIcon(
            self.icons.first_icon(["actions/delete.svg"], size, c)
        )

        # Reset buttons in Advanced panel
        if hasattr(self, "_reset_buttons"):
            reset_icon = self.icons.first_icon(["zoom/zoom-reset.svg"], size, c)
            for b in self._reset_buttons:
                b.setIcon(reset_icon)
                b.setText("")

    # -----------------------------
    # Advanced header behavior
    # -----------------------------
    def _toggle_advanced(self) -> None:
        self._adv_open = not self._adv_open
        self._sync_adv_ui()

    def _toggle_advanced_lock(self) -> None:
        self._adv_locked = not self._adv_locked
        self._apply_advanced_lock_state()
        self._sync_icons()

    def _apply_advanced_lock_state(self) -> None:
        """
        Lock affects ONLY detection-related controls:
        - gain_affects_detection
        - attack/release/smoothing/merge pauses
        It does NOT hide the panel.
        """
        locked = bool(self._adv_locked)

        # Detection-related controls to lock
        for w in (
            self.gain_affects_detection,
            self.attack_ms,
            self.release_ms,
            self.smoothing_mode,
            self.merge_pauses_ms,
        ):
            try:
                w.setEnabled(not locked)
            except Exception:
                pass

    def _sync_adv_ui(self) -> None:
        ai_mode = str(getattr(self, "analysis_mode", "classic")) == "ai"
        self.advanced_panel.setVisible(self._adv_open)
        focus_widgets = getattr(self, "_advanced_focus_hidden_widgets", None)
        if isinstance(focus_widgets, list):
            for w in focus_widgets:
                try:
                    visible = bool(not self._adv_open)
                    # Classic-only sections must stay hidden in AI mode.
                    if ai_mode and w in (
                        getattr(self, "section_intensity", None),
                        getattr(self, "section_threshold", None),
                    ):
                        visible = False
                    w.setVisible(visible)
                except Exception:
                    pass
        # Advanced is a Classic-only area and must remain hidden in AI mode.
        try:
            if hasattr(self, "section_advanced_root") and self.section_advanced_root is not None:
                self.section_advanced_root.setVisible((not ai_mode))
        except Exception:
            pass
        if ai_mode and self._adv_open:
            self._adv_open = False
            self.advanced_panel.setVisible(False)
        if self._adv_open:
            try:
                if hasattr(self, "advanced_scroll") and self.advanced_scroll is not None:
                    self.advanced_scroll.verticalScrollBar().setValue(0)
            except Exception:
                pass
        else:
            # Restore AI panel visibility rules when leaving Advanced focus mode.
            try:
                track = self._get_active_track() if hasattr(self, "_get_active_track") else None
            except Exception:
                track = None
            try:
                self._update_ai_options_panel(track)
            except Exception:
                pass
            try:
                self._update_ai_stats_panel(track)
            except Exception:
                pass
        if hasattr(self, "stats_panel_container") and self.stats_panel_container is not None:
            page_idx = -1
            try:
                page_idx = int(self.pages.currentIndex())
            except Exception:
                page_idx = -1
            # Export page must always keep statistics visible.
            show_stats = bool((page_idx == 1) or (not self._adv_open))
            self.stats_panel_container.setVisible(show_stats)
        self._apply_advanced_lock_state()
        self._refresh_advanced_ui_state()
        self._sync_icons()
        self._apply_core_translations()
        self._apply_accessibility_metadata()
        self._sync_icons()

  
    # -----------------------------
    # Open / playback
    # -----------------------------
    def open_file(self):
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            try:
                self.statusBar().showMessage("Cancelling analysis in progress... please wait.", 3500)
            except Exception:
                pass
            return
        # feedback immediato
        self.btn_open.setChecked(True)

        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select video",
            str(Path.home()),
            "Video Files (*.mp4 *.mov *.mkv *.m4v);;All Files (*.*)"
        )

        # torna normale anche se annulli
        QTimer.singleShot(140, lambda: self.btn_open.setChecked(False))

        if not paths:
            return
        for path in paths:
            if path:
                self._open_path(path)

    def _open_path(
        self,
        path: str,
        restore_cfg: Optional[dict] = None,
        restore_state: Optional[dict] = None,
    ):
        self._app_log(
            "open_path_begin",
            path=str(path),
            auto_restore=bool(getattr(self, "_in_auto_restore_session", False)),
            restore_cfg=bool(restore_cfg),
            restore_state=bool(restore_state),
        )
        self._media_dbg(
            f"open_path begin file='{Path(str(path)).name}' auto_restore={bool(getattr(self, '_in_auto_restore_session', False))} "
            f"restore_cfg={bool(restore_cfg)} restore_state={bool(restore_state)}"
        )
        # Guard against very-early play clicks while Qt Multimedia is still
        # initializing decoders / initial seek state for the newly opened file.
        self._arm_play_guard(
            2.0 if bool(getattr(self, "_in_auto_restore_session", False)) else 1.2,
            "Preparing playback",
        )
        if bool(getattr(self, "_in_auto_restore_session", False)):
            try:
                self._media_debug_sync_focus_until = max(
                    float(getattr(self, "_media_debug_sync_focus_until", 0.0) or 0.0),
                    time.monotonic() + 4.0,
                )
            except Exception:
                pass
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            self._app_log("open_path_aborted_reset_pending", path=str(path))
            try:
                self.statusBar().showMessage("Cancelling analysis in progress... please wait.", 3500)
            except Exception:
                pass
            self._media_dbg("open_path aborted due to pending workspace reset")
            return
        ffmpeg_ok = True
        try:
            self.ffmpeg_path, self.ffprobe_path = ensure_ffmpeg()
        except FFmpegNotFound as e:
            ffmpeg_ok = False
            self.ffmpeg_path, self.ffprobe_path = None, None
            self._app_log("ffmpeg_missing_open_path", path=str(path), error=str(e))
            QMessageBox.warning(self, "FFmpeg not found", str(e))

        # if current track already has media, create a new track
        if self.input_path:
            try:
                self._save_track_cfg(self._get_active_track())
            except Exception:
                pass
            self._tracks.append(TrackState())
            self._active_track_index = len(self._tracks) - 1
            # reset analysis state for the new track before assigning media
            self._reset_analysis_state()

        self.input_path = path
        track = self._get_active_track()
        # Preload duration so clips have visible length even before analysis completes
        if ffmpeg_ok:
            try:
                dur = float(ffprobe_duration_seconds(path))
                if dur > 0:
                    track.duration = dur
            except Exception:
                pass
        track.filters_restored = False
        track.cuts_restored = False
        restored_cfg = None
        if isinstance(restore_cfg, dict) and restore_cfg:
            try:
                restored_cfg = self._normalize_preset_cfg(dict(restore_cfg))
            except Exception:
                restored_cfg = dict(restore_cfg)
            track.cfg = dict(restored_cfg)
            track.filters_restored = True
        restored_cuts = False
        if isinstance(restore_state, dict):
            restored_cuts = self._apply_restore_state(track, restore_state)
        self._apply_track_cfg(track)
        if track.media_id is None:
            self._add_media_to_project(path, track)
        # ensure clips reflect duration and timeline shows immediately
        try:
            self._sync_project_from_track(track)
        except Exception:
            pass
        self._refresh_timeline_tracks(reset_view=True)
        total = self._global_duration if self._global_duration > 0 else track.duration
        if total > 0:
            self.seek.setRange(0, int(total * 1000))
            self._update_time_label(self.seek.value() / 1000.0)
        self._set_ready_dot_state("idle")
        self._set_export_dot_state("idle")
        self._set_ai_processing(False)
        self._web_push_full_state()
        self._update_active_track_label()
        self._media_debug_snapshot("open_path after_timeline_build")

        if restored_cuts and track.keeps:
            self._apply_track_cuts_ui(track)
            self._set_ready_dot_state("ready")
            self._update_ai_stats_panel(track)
            self._update_ai_options_panel(track)
        else:
            self._set_stage("analyze")

        # audio handled by QMediaPlayer

        # keep video preview on global timeline (concatenated order)
        if not self._video_segments:
            self._video_segment_index = 0
            self._media_dbg(f"open_path set video source direct '{Path(path).name}'")
            self.video_player.setSource(QUrl.fromLocalFile(path))
            self.video_player.pause()
        else:
            self._media_dbg("open_path sync video to global (segments present)")
            self._sync_video_to_global(getattr(self, "_last_pos", 0.0), force=True)
        try:
            self._media_dbg("open_path sync audio to global initial")
            self._sync_audio_to_global(getattr(self, "_last_pos", 0.0), force=True)
        except Exception:
            pass
        self._media_debug_snapshot("open_path after_initial_av_sync")

        # Apply default preset (if any), unless we will restore filters
        if self._default_preset_name and not (
            restored_cfg or (self._remember_filters_enabled and self._pending_restore_filters)
        ):
            try:
                self._apply_preset(self._default_preset_name)
                idx = self.preset_combo.findText(self._default_preset_name)
                if idx >= 0:
                    self.preset_combo.blockSignals(True)
                    self.preset_combo.setCurrentIndex(idx)
                    self.preset_combo.blockSignals(False)
            except Exception as e:
                print("[preset] failed:", e)  # oppure log in statusbar

        # Restore last filters after opening file (if enabled)
        if self._remember_filters_enabled and restored_cfg is None and isinstance(self._pending_restore_filters, dict):
            self._apply_cfg_from_dict(self._pending_restore_filters)
            track.filters_restored = True
            self._pending_restore_filters = None

        if ffmpeg_ok:
            self.statusBar().showMessage("Analyzing audio...")
            self._media_dbg("open_path start_analysis_thread")
            self._start_analysis_thread(path, self._active_track_index)
        else:
            self.statusBar().showMessage("FFmpeg not found: analysis disabled.", 6000)
        self._update_split_button_state()
        self._refresh_play_button_state()
        self._app_log(
            "open_path_end",
            path=str(path),
            ffmpeg_ok=bool(ffmpeg_ok),
            active_track=int(getattr(self, "_active_track_index", 0)),
            tracks=len(getattr(self, "_tracks", []) or []),
        )
        self._media_dbg("open_path end")


    @staticmethod
    def _should_warm_export_cache(path: str, duration_s: float | None = None) -> bool:
        if not path:
            return False
        try:
            max_duration_s = max(0.0, float(os.environ.get("AUTO_CUTTER_WARM_CACHE_MAX_DURATION_S", "900")))
        except Exception:
            max_duration_s = 900.0
        try:
            max_bytes = max(0, int(os.environ.get("AUTO_CUTTER_WARM_CACHE_MAX_BYTES", str(1024**3))))
        except Exception:
            max_bytes = 1024**3
        try:
            size = int(Path(path).stat().st_size)
        except OSError:
            return False
        if max_bytes <= 0 or size > max_bytes:
            return False
        if duration_s is None:
            try:
                duration_s = float(ffprobe_duration_seconds(path))
            except Exception:
                return False
        return max_duration_s > 0.0 and 0.0 < float(duration_s) <= max_duration_s

    def _enqueue_warm_export_cache(self, path: str, duration_s: float | None = None) -> None:
        if not path:
            return
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            return
        if not self._should_warm_export_cache(path, duration_s):
            self._app_log(
                "cache_warm_skipped",
                path=str(path),
                duration_s=float(duration_s or 0.0),
                reason="media_too_large_or_long",
            )
            return
        try:
            with self._warm_cache_lock:
                if self._warm_cache_stop.is_set():
                    self._warm_cache_stop.clear()
                if path in self._warm_cache_queue:
                    return
                self._warm_cache_queue.append(path)
                if self._warm_cache_thread and self._warm_cache_thread.is_alive():
                    return
                self._warm_cache_thread = threading.Thread(
                    target=self._warm_export_cache_worker, daemon=True
                )
                self._warm_cache_thread.start()
        except Exception:
            pass

    def _warm_export_cache_worker(self) -> None:
        self._warm_cache_busy = True
        try:
            while True:
                if self._warm_cache_stop.is_set():
                    return
                if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
                    return
                try:
                    with self._warm_cache_lock:
                        if not self._warm_cache_queue:
                            return
                        path = self._warm_cache_queue.pop(0)
                except Exception:
                    return

                if self._warm_cache_stop.is_set():
                    return
                if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
                    return

                def _ui_log(msg: str) -> None:
                    try:
                        QTimer.singleShot(0, lambda m=msg: self._on_export_detail(m))
                    except Exception:
                        pass

                _ui_log(f"cache_warm start path={Path(path).name}")
                t0 = time.time()
                try:
                    # Keep warm-cache probes bounded: this is optional precompute.
                    keyframes = ffprobe_keyframes(path, use_cache=True, timeout_s=45.0)
                    dt = time.time() - t0
                    _ui_log(f"cache_warm done keyframes={len(keyframes)} took={dt:.2f}s")
                except Exception as e:
                    _ui_log(f"cache_warm failed {e}")
        finally:
            self._warm_cache_busy = False
            try:
                with self._warm_cache_lock:
                    if self._warm_cache_thread is not None and not self._warm_cache_thread.is_alive():
                        self._warm_cache_thread = None
            except Exception:
                pass

    def _warm_cache_in_progress(self) -> bool:
        try:
            if bool(getattr(self, "_warm_cache_stop", None)) and self._warm_cache_stop.is_set():
                return False
            if bool(getattr(self, "_warm_cache_busy", False)):
                return True
            th = getattr(self, "_warm_cache_thread", None)
            return bool(th is not None and th.is_alive())
        except Exception:
            return bool(getattr(self, "_warm_cache_busy", False))

    def _cancel_warm_cache(self, wait_ms: int = 0) -> None:
        try:
            self._warm_cache_stop.set()
            with self._warm_cache_lock:
                self._warm_cache_queue.clear()
                th = self._warm_cache_thread
        except Exception:
            return

        try:
            wait_s = max(0.0, float(wait_ms) / 1000.0)
        except Exception:
            wait_s = 0.0
        if th is not None and th.is_alive() and wait_s > 0.0:
            try:
                th.join(wait_s)
            except Exception:
                pass
        try:
            if th is not None and (not th.is_alive()):
                with self._warm_cache_lock:
                    if self._warm_cache_thread is th:
                        self._warm_cache_thread = None
        except Exception:
            pass

    def toggle_play(self):
        can_play, reason = self._can_start_playback_now()
        if not can_play:
            self._app_log("play_blocked", reason=str(reason or "not_ready"))
            try:
                self.statusBar().showMessage(str(reason or "Playback not ready yet."), 1500)
            except Exception:
                pass
            self._refresh_play_button_state()
            return
        if self.video_player.playbackState() == QMediaPlayer.PlayingState:
            self._app_log("play_pause", pos_ms=int(self.seek.value() or 0))
            self._play_requested = False
            self.video_player.pause()
        else:
            self._app_log("play_start", pos_ms=int(self.seek.value() or 0))
            self._play_requested = True
            pos = int(self.seek.value())
            self._set_all_positions(pos)
            self.video_player.play()

    def _transport_seek_step_seconds(self) -> float:
        # Keep seek movement small and predictable for J/L transport keys.
        return 2.0

    def _transport_seek_relative(self, delta_seconds: float) -> None:
        try:
            if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
                return
            total = float(self._global_duration if self._global_duration > 0 else self.duration)
            if total <= 0.0:
                return
            try:
                cur_t = float(getattr(self.timeline, "playhead", 0.0) or 0.0)
            except Exception:
                cur_t = float(self.seek.value() or 0) / 1000.0
            target = max(0.0, min(total, float(cur_t) + float(delta_seconds)))
            self._seek_to(float(target))
        except Exception:
            pass

    def transport_seek_backward(self) -> None:
        self._transport_seek_relative(-self._transport_seek_step_seconds())

    def transport_seek_forward(self) -> None:
        self._transport_seek_relative(self._transport_seek_step_seconds())

    def _ordered_audio_tracks(self) -> list[tuple[int, Clip, float, float]]:
        items: list[tuple[int, Clip, float, float]] = []
        for idx, _t in enumerate(self._tracks):
            clip = self._audio_clip_for_track(idx)
            if clip is None:
                continue
            try:
                tin = float(clip.timeline_in)
                tout = float(clip.timeline_out)
            except Exception:
                continue
            items.append((idx, clip, tin, tout))
        items.sort(key=lambda it: float(it[2]))
        return items

    def _collect_global_cuts(self) -> list[tuple[float, float, float]]:
        cuts_global: list[tuple[float, float, float]] = []
        for idx, _clip, tin, tout in self._ordered_audio_tracks():
            if not (0 <= idx < len(self._tracks)):
                continue
            track = self._tracks[idx]
            if not getattr(track, "cuts_enabled", True):
                continue
            cuts = list(track.cuts or [])
            if not cuts:
                continue
            for c in cuts:
                try:
                    gs = float(tin) + float(c.start)
                    ge = float(tin) + float(c.end)
                except Exception:
                    continue
                cuts_global.append((gs, ge, float(tout)))
        cuts_global.sort(key=lambda it: float(it[0]))
        return cuts_global

    def _jump_to_cut(self, direction: int) -> None:
        cuts_global = self._collect_global_cuts()
        if not cuts_global:
            return

        try:
            cur_t = float(getattr(self.timeline, "playhead", 0.0))
        except Exception:
            cur_t = float(getattr(self, "_last_pos", 0.0) or 0.0)
        pending = getattr(self, "_pending_seek_target", None)
        force_av_sync = False
        if pending is not None:
            try:
                dt = time.monotonic() - float(getattr(self, "_pending_seek_ts", 0.0) or 0.0)
            except Exception:
                dt = 0.0
            try:
                timeout = float(getattr(self, "_pending_seek_timeout_s", 0.9))
            except Exception:
                timeout = 0.9
            if dt < timeout:
                cur_t = float(pending)
        eps = 1e-6
        if direction > 0:
            for gs, _ge, _tend in cuts_global:
                if gs > cur_t + eps:
                    self._seek_to(float(gs))
                    return
            return
        else:
            prev_span_s = 0.5
            target = None
            for i in range(len(cuts_global) - 1, -1, -1):
                gs, ge, t_end = cuts_global[i]
                if cur_t >= gs - eps:
                    within_track = cur_t < (t_end - 1e-6)
                    if within_track and cur_t <= ge + prev_span_s:
                        if i > 0:
                            target = cuts_global[i - 1][0]
                    else:
                        target = gs
                    break
            if target is None:
                return
            self._seek_to(float(target))

    def jump_to_next_cut(self) -> None:
        self._jump_to_cut(1)

    def jump_to_previous_cut(self) -> None:
        self._jump_to_cut(-1)

    def _on_playback_state_changed(self, state):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        self._media_dbg(
            f"video_playback_state_changed state={self._mp_state_name(state)} "
            f"play_requested={bool(getattr(self, '_play_requested', False))}"
        )
        self._media_debug_snapshot("on_playback_state_changed")
        self._refresh_play_button_state()
        c = getattr(self, "_c_text", None)
        if c is None:
            return
        size = 18
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setIcon(self.icons.first_icon(["transport/pause.svg"], size, c))
            self._web_js(self.web_transport, "uiSetPlayState(true);")
        else:
            self.btn_play.setIcon(self.icons.first_icon(["transport/play.svg"], size, c))
            self._web_js(self.web_transport, "uiSetPlayState(false);")

        # keep audio player in sync with play/pause state
        try:
            pos_ms = int(self.video_player.position() or 0)
        except Exception:
            pos_ms = 0
        t = float(pos_ms) / 1000.0
        if self._video_segments and 0 <= self._video_segment_index < len(self._video_segments):
            try:
                seg = self._video_segments[self._video_segment_index]
                seg_start = float(seg.get("start", 0.0) or 0.0)
                seg_src_in = float(seg.get("source_in", 0.0) or 0.0)
                t = t - seg_src_in + seg_start
            except Exception:
                pass
        if t <= 0.0:
            try:
                t = float(self.seek.value() or 0) / 1000.0
            except Exception:
                t = float(getattr(self, "_last_pos", 0.0) or 0.0)
        total = self._global_duration if self._global_duration > 0 else self.duration
        if total > 0:
            t = max(0.0, min(float(t), float(total)))
        else:
            t = max(0.0, float(t))
        try:
            self._sync_audio_to_global(t, force=True)
            if state == QMediaPlayer.PlayingState:
                self.audio_player.play()
            else:
                self.audio_player.pause()
        except Exception:
            pass

    def _on_skip_changed(self):
        self.skip_on = self.chk_skip.isChecked()
        self._web_js(self.web_topbar, f"uiSetSkip({json.dumps(bool(self.chk_skip.isChecked()))});")

    def _on_analysis_mode_toggled(self, checked: bool) -> None:
        prev_mode = str(getattr(self, "analysis_mode", "classic") or "classic")
        self.analysis_mode = "ai" if checked else "classic"
        try:
            self.section_intensity.setVisible(not checked)
            self.section_threshold.setVisible(not checked)
            if hasattr(self, "section_advanced_root") and self.section_advanced_root is not None:
                self.section_advanced_root.setVisible(not checked)
        except Exception:
            pass

        # Advanced is not applicable in AI mode: force-close it so it never
        # reappears unexpectedly when switching layouts/pages.
        if checked and getattr(self, "_adv_open", False):
            self._adv_open = False
        try:
            self._sync_adv_ui()
        except Exception:
            pass

        if checked:
            # do not start AI while classic analysis still running
            if getattr(self, "_analysis_threads", {}):
                QMessageBox.warning(self, "AI", "Wait for the initial analysis to finish before switching to AI.")
                try:
                    self.analysis_mode_toggle.blockSignals(True)
                    self.analysis_mode_toggle.setChecked(False)
                finally:
                    self.analysis_mode_toggle.blockSignals(False)
                self.analysis_mode = "classic"
                try:
                    self.section_intensity.setVisible(True)
                    self.section_threshold.setVisible(True)
                    if hasattr(self, "section_advanced_root") and self.section_advanced_root is not None:
                        self.section_advanced_root.setVisible(True)
                    self._sync_adv_ui()
                except Exception:
                    pass
                return
            try:
                track = self._tracks[self._active_track_index]
            except Exception:
                track = None
            if track is not None:
                self._save_workspace_for_mode(track, prev_mode)
                self._restore_workspace_for_mode(track, "ai")
                self._refresh_analysis_workspace_ui(track)
            else:
                self._update_ai_options_panel(track)
                self._update_ai_stats_panel(track)
        else:
            # restore classic cuts immediately
            if 0 <= self._active_track_index < len(self._tracks):
                track = self._tracks[self._active_track_index]
                self._save_workspace_for_mode(track, prev_mode)
                self._restore_classic_cuts(track)
                self._refresh_analysis_workspace_ui(track)
            self._set_ai_processing(False)

    # -----------------------------
    # Stage pills
    # -----------------------------
    def _set_stage(self, stage: str):
        def pill(label: str, on: bool):
            bg = "#2A2A2A" if on else "#1A1A1A"
            br = "#3A3A3A" if on else "#242424"
            fg = "#FFFFFF" if on else "#B0B0B0"
            return f"<span style='background:{bg};border:1px solid {br};padding:2px 8px;border-radius:999px;color:{fg};'>{label}</span>"

        s1 = stage in ("import", "analyze", "review", "export")
        s2 = stage in ("analyze", "review", "export")
        s3 = stage in ("review", "export")
        s4 = stage in ("export",)

        self.step_bar.setText(
            f"{pill('1 Import', s1)}&nbsp;&nbsp;"
            f"{pill('2 Analyze', s2)}&nbsp;&nbsp;"
            f"{pill('3 Review', s3)}&nbsp;&nbsp;"
            f"{pill('4 Export', s4)}"
        )

    def _set_ready_dot_state(self, state: str) -> None:
        self._ready_dot_state = str(state)
        self._web_js(self.web_topbar, f"uiSetReadyDotState({json.dumps(self._ready_dot_state)});")

    def _set_export_dot_state(self, state: str) -> None:
        self._export_dot_state = str(state)
        self._web_js(self.web_topbar, f"uiSetExportDotState({json.dumps(self._export_dot_state)});")


    def _set_user_scrubbing(self, v: bool):
        self._user_scrubbing = bool(v)

    def _next_analysis_job_id(self) -> int:
        self._analysis_job_seq = int(getattr(self, "_analysis_job_seq", 0) or 0) + 1
        return int(self._analysis_job_seq)

    def _is_current_analysis_job(self, track_idx: int, job_id: int) -> bool:
        try:
            current = self._analysis_job_ids.get(int(track_idx))
        except Exception:
            current = None
        return current is not None and int(current) == int(job_id)

    def _resolve_analysis_target_index(
        self,
        original_track_idx: int,
        job_id: int,
        target: TrackState,
        expected_path: str,
    ) -> int | None:
        if not self._is_current_analysis_job(original_track_idx, job_id):
            return None
        for idx, track in enumerate(self._tracks):
            if track is not target:
                continue
            if expected_path and track.path and not self._same_local_path(track.path, expected_path):
                return None
            return int(idx)
        return None

    def _analysis_slot_for_track(self, target: TrackState) -> int | None:
        for slot, registered in list(getattr(self, "_analysis_targets", {}).items()):
            if registered is not target:
                continue
            job_id = self._analysis_job_ids.get(int(slot))
            if job_id is None:
                continue
            thread = self._analysis_threads.get(int(slot))
            try:
                if thread is not None and thread.isRunning():
                    return int(slot)
            except Exception:
                continue
        return None

    def _cleanup_analysis_job_refs(self, track_idx: int, job_id: int, thread: QThread) -> None:
        # A late finished signal from an old worker must never remove a newer job
        # that reused the same list index.
        if not self._is_current_analysis_job(track_idx, job_id):
            return
        if self._analysis_threads.get(int(track_idx)) is not thread:
            return
        self._analysis_threads.pop(int(track_idx), None)
        self._analysis_workers.pop(int(track_idx), None)
        self._analysis_expected_path.pop(int(track_idx), None)
        self._analysis_job_ids.pop(int(track_idx), None)
        self._analysis_targets.pop(int(track_idx), None)
        self._analysis_progress.pop(int(track_idx), None)

    def _sync_analysis_ui_for_active_track(self) -> None:
        try:
            track = self._get_active_track()
        except Exception:
            return

        analyzed = bool(track.rms is not None and float(track.duration or 0.0) > 0.0)
        slot = self._analysis_slot_for_track(track)
        if analyzed:
            state = "Ready"
            progress = 0
            self._set_ready_dot_state("ready")
        elif slot is not None:
            pct = max(0, min(100, int(self._analysis_progress.get(slot, 0))))
            state = "Analyzing audio..."
            progress = 10 + int(pct * 0.8)
            self._set_ready_dot_state("idle")
        else:
            state = "Waiting for audio analysis"
            progress = 0
            self._set_ready_dot_state("idle")

        try:
            self._web_js(self.web_topbar, f"uiSetState({json.dumps(state)});")
            self._web_js(self.web_topbar, f"uiSetProgress({int(progress)});")
        except Exception:
            pass
        try:
            if analyzed:
                self.statusBar().showMessage("Audio analysis ready.", 2500)
            elif slot is not None:
                pct = max(0, min(100, int(self._analysis_progress.get(slot, 0))))
                self.statusBar().showMessage(f"Analyzing audio... {pct}%")
        except Exception:
            pass

    def _cleanup_orphan_analysis_refs(self) -> None:
        try:
            self._orphan_analysis_threads = [
                t for t in list(getattr(self, "_orphan_analysis_threads", []))
                if t is not None and t.isRunning()
            ]
        except Exception:
            self._orphan_analysis_threads = []
        try:
            self._orphan_ai_threads = [
                t for t in list(getattr(self, "_orphan_ai_threads", []))
                if t is not None and t.isRunning()
            ]
        except Exception:
            self._orphan_ai_threads = []
        # Worker wrappers are only needed to keep Python refs while orphan threads are alive.
        if not self._orphan_analysis_threads:
            self._orphan_analysis_workers = []
        if not self._orphan_ai_threads:
            self._orphan_ai_workers = []

    def _force_detach_analysis_for_reset(self) -> None:
        self._app_log(
            "workspace_reset_force_detach",
            analysis_threads=len(getattr(self, "_analysis_threads", {}) or {}),
            ai_threads=len(getattr(self, "_ai_threads", {}) or {}),
            warm_cache=bool(self._warm_cache_in_progress()),
        )
        try:
            self._cancel_warm_cache(wait_ms=0)
        except Exception:
            pass

        analysis_threads = list(getattr(self, "_analysis_threads", {}).values())
        ai_threads = list(getattr(self, "_ai_threads", {}).values())
        analysis_workers = list(getattr(self, "_analysis_workers", {}).values())
        ai_workers = list(getattr(self, "_ai_workers", {}).values())

        for w in analysis_workers + ai_workers:
            try:
                if hasattr(w, "cancel"):
                    w.cancel()
            except Exception:
                pass

        for t in analysis_threads + ai_threads:
            try:
                if t is not None and t.isRunning():
                    t.quit()
            except Exception:
                pass

        # Keep detached refs alive until threads naturally finish.
        try:
            self._orphan_analysis_threads.extend([t for t in analysis_threads if t is not None and t.isRunning()])
        except Exception:
            pass
        try:
            self._orphan_ai_threads.extend([t for t in ai_threads if t is not None and t.isRunning()])
        except Exception:
            pass
        try:
            self._orphan_analysis_workers.extend(analysis_workers)
        except Exception:
            pass
        try:
            self._orphan_ai_workers.extend(ai_workers)
        except Exception:
            pass

        self._analysis_threads.clear()
        self._analysis_workers.clear()
        self._analysis_expected_path.clear()
        self._analysis_job_ids.clear()
        self._analysis_targets.clear()
        self._analysis_progress.clear()
        self._ai_threads.clear()
        self._ai_workers.clear()
        self._ai_expected_path.clear()
        self._set_ai_processing(False)
        self._cleanup_orphan_analysis_refs()

    def _analysis_in_progress(self) -> bool:
        self._cleanup_orphan_analysis_refs()
        return (
            bool(getattr(self, "_analysis_threads", {}))
            or bool(getattr(self, "_ai_threads", {}))
            or bool(getattr(self, "_ai_processing", False))
        )

    def _export_in_progress(self) -> bool:
        try:
            th = getattr(self, "ex_thread", None)
            if th is not None and th.isRunning():
                return True
        except Exception:
            pass
        # If worker/thread are gone, heal stale busy flags.
        if bool(getattr(self, "_export_processing", False)):
            worker = getattr(self, "ex_worker", None)
            if worker is not None:
                return True
            try:
                self._set_export_processing(False)
            except Exception:
                self._export_processing = False
        return False

    @staticmethod
    def _is_cancelled_error(msg: str) -> bool:
        m = str(msg or "").strip().lower()
        return ("cancel" in m) or ("__cancelled__" in m)

    def _abort_analysis_tasks(self, wait_ms: int = 1200) -> None:
        try:
            self._cancel_warm_cache(wait_ms=wait_ms)
        except Exception:
            pass
        # Ask workers to stop first.
        for w in list(getattr(self, "_analysis_workers", {}).values()):
            try:
                if hasattr(w, "cancel"):
                    w.cancel()
            except Exception:
                pass
        for w in list(getattr(self, "_ai_workers", {}).values()):
            try:
                if hasattr(w, "cancel"):
                    w.cancel()
            except Exception:
                pass

        # Request thread shutdown.
        analysis_threads = list(getattr(self, "_analysis_threads", {}).items())
        ai_threads = list(getattr(self, "_ai_threads", {}).items())
        for _idx, t in analysis_threads + ai_threads:
            try:
                if t is not None and t.isRunning():
                    t.quit()
            except Exception:
                pass

        # Optional bounded wait; keep references for still-running threads.
        wait_s = max(0.0, float(wait_ms) / 1000.0)
        if wait_s > 0.0:
            deadline = time.monotonic() + wait_s
            for _idx, t in analysis_threads + ai_threads:
                try:
                    if t is None or (not t.isRunning()):
                        continue
                    rem = deadline - time.monotonic()
                    if rem <= 0.0:
                        break
                    t.wait(max(1, int(min(rem, 0.4) * 1000)))
                except Exception:
                    pass

        # Prune only stopped jobs; do not drop refs for running threads.
        for idx, t in list(getattr(self, "_analysis_threads", {}).items()):
            try:
                running = bool(t is not None and t.isRunning())
            except Exception:
                running = False
            if not running:
                self._analysis_threads.pop(idx, None)
                self._analysis_workers.pop(idx, None)
                self._analysis_expected_path.pop(idx, None)
                self._analysis_job_ids.pop(idx, None)
                self._analysis_targets.pop(idx, None)
                self._analysis_progress.pop(idx, None)
        for idx, t in list(getattr(self, "_ai_threads", {}).items()):
            try:
                running = bool(t is not None and t.isRunning())
            except Exception:
                running = False
            if not running:
                self._ai_threads.pop(idx, None)
                self._ai_workers.pop(idx, None)
                self._ai_expected_path.pop(idx, None)
        if not self._ai_threads:
            self._set_ai_processing(False)

    def _update_split_button_state(self) -> None:
        enabled = False
        try:
            enabled = any(bool(getattr(t, "path", None)) for t in self._tracks)
        except Exception:
            enabled = False
        if self._analysis_in_progress():
            enabled = False
        if hasattr(self, "btn_cut_tool") and self.btn_cut_tool is not None:
            self.btn_cut_tool.setEnabled(bool(enabled))
        if hasattr(self, "btn_split") and self.btn_split is not None:
            self.btn_split.setEnabled(bool(enabled))
        if not enabled and self.tool_mode in {"split", "cut"}:
            self._set_tool_mode("select")

    def _set_ai_processing(self, active: bool) -> None:
        active = bool(active)
        prev = bool(getattr(self, "_ai_processing", False))
        self._ai_processing = active
        if prev != active:
            self._app_log("ai_processing_state", active=active)
        try:
            if hasattr(self, "analysis_mode_toggle") and self.analysis_mode_toggle is not None:
                self.analysis_mode_toggle.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "btn_play") and self.btn_play is not None:
                self.btn_play.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "seek") and self.seek is not None:
                self.seek.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "timeline") and self.timeline is not None:
                self.timeline.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "video_widget") and self.video_widget is not None:
                if active:
                    self.video_widget.set_overlay("AI PROCESSING\nPLEASE WAIT")
                else:
                    self.video_widget.set_overlay(None)
        except Exception:
            pass
        self._update_split_button_state()
        self._refresh_play_button_state()

    def _set_export_processing(self, active: bool) -> None:
        active = bool(active)
        prev = bool(getattr(self, "_export_processing", False))
        self._export_processing = active
        if prev != active:
            self._app_log("export_processing_state", active=active)
        try:
            if hasattr(self, "btn_export") and self.btn_export is not None:
                self.btn_export.setText("Exporting..." if active else "Export MP4")
        except Exception:
            pass
        try:
            if hasattr(self, "btn_play") and self.btn_play is not None:
                self.btn_play.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "seek") and self.seek is not None:
                self.seek.setEnabled(not active)
        except Exception:
            pass
        try:
            if hasattr(self, "timeline") and self.timeline is not None:
                self.timeline.setEnabled(not active)
        except Exception:
            pass
        if active:
            try:
                self.video_player.pause()
            except Exception:
                pass
            try:
                self.audio_player.pause()
            except Exception:
                pass
        self._refresh_play_button_state()

    def _update_ai_options_panel(self, track: TrackState | None) -> None:
        if not hasattr(self, "ai_options_panel"):
            return
        if self.analysis_mode != "ai":
            self.ai_options_panel.setVisible(False)
            return
        if self._ai_processing:
            self.ai_options_panel.setVisible(False)
            return
        has_ai = track is not None and bool(track.ai_speech or track.ai_speech_raw)
        self.ai_options_panel.setVisible(True)
        try:
            self.btn_ai_process.setVisible(not has_ai)
            self.btn_ai_reprocess.setVisible(has_ai)
        except Exception:
            pass

    def _update_ai_stats_panel(self, track: TrackState | None) -> None:
        if not hasattr(self, "ai_stats_panel"):
            return
        if self.analysis_mode != "ai":
            self.ai_stats_panel.setVisible(False)
            return
        if not self._ai_processing and (track is None or (not track.ai_speech and not track.ai_speech_raw)):
            self.ai_stats_panel.setVisible(False)
            return
        self.ai_stats_panel.setVisible(True)
        if track is None or (not track.ai_speech and not track.ai_speech_raw):
            self.ai_stats_speakers.setText("-")
            self.ai_stats_voice.setText("-")
            self.ai_stats_noise.setText("-")
            self.ai_stats_total.setText("-")
            self.ai_stats_speaker_list.setText("No AI data yet.")
            try:
                self.ai_stats_timeline.set_data(0.0, [])
            except Exception:
                pass
            return

        duration = float(track.duration or 0.0)
        speech = list(track.ai_speech or [])
        merged = merge_overlaps(speech)
        total_speech = sum(float(s.end) - float(s.start) for s in merged)
        voice_pct = (total_speech / duration * 100.0) if duration > 0 else 0.0
        noise_pct = max(0.0, 100.0 - voice_pct)

        raw_speech = list(track.ai_speech_raw or [])
        speaker_ids = track.ai_speaker_ids if (track.ai_speaker_ids and len(track.ai_speaker_ids) == len(raw_speech)) else None
        spk_totals: dict[int, float] = {}
        if speaker_ids and raw_speech:
            for seg, sid in zip(raw_speech, speaker_ids):
                spk_totals[int(sid)] = spk_totals.get(int(sid), 0.0) + max(0.0, float(seg.end) - float(seg.start))
        else:
            spk_totals[0] = total_speech

        n_spk = len(spk_totals)
        self.ai_stats_speakers.setText(str(n_spk))
        self.ai_stats_voice.setText(f"{voice_pct:.0f}%")
        self.ai_stats_noise.setText(f"{noise_pct:.0f}%")
        self.ai_stats_total.setText(fmt_hms(total_speech))

        lines = []
        for sid in sorted(spk_totals.keys()):
            lines.append(f"Speaker {sid + 1}: {fmt_hms(spk_totals[sid])}")
        self.ai_stats_speaker_list.setText("\n".join(lines))
        try:
            if speaker_ids and raw_speech:
                self.ai_stats_timeline.set_data(duration, raw_speech, speaker_ids)
            else:
                self.ai_stats_timeline.set_data(duration, speech, None)
        except Exception:
            pass

    def _save_classic_cuts(self, track: TrackState) -> None:
        track.classic_cuts = self._clone_segments_list(getattr(track, "cuts", []))
        track.classic_keeps = self._clone_segments_list(getattr(track, "keeps", []))
        track.classic_manual_cuts = self._clone_segments_list(getattr(track, "manual_cuts", []))
        track.classic_suppressed_cuts = self._clone_segments_list(getattr(track, "suppressed_cuts", []))
        track.classic_cuts_enabled = bool(getattr(track, "cuts_enabled", False))

    def _restore_classic_cuts(self, track: TrackState) -> None:
        if track.classic_cuts or track.classic_keeps:
            track.cuts = list(track.classic_cuts or [])
            track.keeps = list(track.classic_keeps or [])
            track.manual_cuts = list(track.classic_manual_cuts or [])
            track.suppressed_cuts = list(track.classic_suppressed_cuts or [])
            track.cuts_enabled = bool(
                getattr(
                    track,
                    "classic_cuts_enabled",
                    bool(track.cuts or track.keeps or track.manual_cuts or track.suppressed_cuts),
                )
            )
        else:
            # fallback: recompute from RMS if available
            if track.rms is not None and track.duration > 0:
                try:
                    idx = self._tracks.index(track)
                except Exception:
                    idx = self._active_track_index
                try:
                    self._compute_cuts_for_track(int(idx))
                except Exception:
                    pass

    def _clear_track_cuts(self, track: TrackState) -> None:
        track.cuts = []
        track.keeps = []
        track.manual_cuts = []
        track.suppressed_cuts = []
        track.undo_stack.clear()
        track.redo_stack.clear()
        track.pending_cut_start = None
        track.pending_cut_end = None
        track.cuts_enabled = True

    def _ai_postprocess_speech(self, speech: list[Segment], duration: float) -> list[Segment]:
        duration = float(duration or 0.0)
        segs = []
        for s in speech:
            try:
                a = max(0.0, float(s.start))
                b = min(float(duration), float(s.end))
            except Exception:
                continue
            if b > a:
                segs.append(Segment(a, b))

        try:
            min_speech = float(self.ai_min_speech_ms.value()) / 1000.0
        except Exception:
            min_speech = 0.0
        if min_speech > 0:
            segs = [s for s in segs if (s.end - s.start) >= min_speech]

        try:
            merge_gap = float(self.ai_merge_gap_ms.value()) / 1000.0
        except Exception:
            merge_gap = 0.0

        segs.sort(key=lambda s: float(s.start))
        merged: list[Segment] = []
        for s in segs:
            if not merged:
                merged.append(Segment(s.start, s.end))
                continue
            last = merged[-1]
            if (s.start - last.end) <= merge_gap:
                last.end = max(last.end, s.end)
            else:
                merged.append(Segment(s.start, s.end))

        try:
            pre = float(self.pre_pad_s.value())
        except Exception:
            pre = 0.0
        try:
            post = float(self.post_pad_s.value())
        except Exception:
            post = 0.0

        padded: list[Segment] = []
        for s in merged:
            a = max(0.0, s.start - pre)
            b = min(duration, s.end + post)
            if b > a:
                padded.append(Segment(a, b))
        return merge_overlaps(padded)

    def _invert_keeps_to_cuts(self, duration: float, keeps: list[Segment]) -> list[Segment]:
        if duration <= 0:
            return []
        if not keeps:
            return [Segment(0.0, duration)]
        keeps = merge_overlaps([Segment(max(0.0, float(s.start)), min(float(duration), float(s.end))) for s in keeps])
        cuts: list[Segment] = []
        cursor = 0.0
        for k in keeps:
            if k.start > cursor:
                cuts.append(Segment(cursor, k.start))
            cursor = max(cursor, k.end)
        if cursor < duration:
            cuts.append(Segment(cursor, duration))
        # apply min_cut_s to avoid micro-cuts
        try:
            min_cut = float(self.min_cut_s.value())
        except Exception:
            min_cut = 0.0
        if min_cut > 0:
            cuts = [c for c in cuts if (c.end - c.start) >= min_cut]
        return cuts

    def _start_ai_analysis(self, track_idx: int) -> None:
        if not (0 <= track_idx < len(self._tracks)):
            return
        track = self._tracks[track_idx]
        if not track.path:
            return
        deps_ok, reason = ai_dependency_status()
        if not deps_ok:
            msg = "AI dependencies not available (Spleeter/Silero)."
            if reason:
                msg += f"\n\n{reason}"
            QMessageBox.warning(self, "AI not available", msg)
            try:
                self.analysis_mode_toggle.blockSignals(True)
                self.analysis_mode_toggle.setChecked(False)
            finally:
                self.analysis_mode_toggle.blockSignals(False)
            self.analysis_mode = "classic"
            try:
                self.section_intensity.setVisible(True)
                self.section_threshold.setVisible(True)
            except Exception:
                pass
            self._update_ai_options_panel(None)
            self._update_ai_stats_panel(None)
            return
        if track_idx in self._ai_threads:
            return

        # Preserve current visible workspace before replacing it with a fresh AI run.
        if self._has_ai_workspace(track):
            self._save_ai_cuts(track)
        else:
            self._save_classic_cuts(track)
        # clear current cuts immediately
        self._clear_track_cuts(track)
        track.ai_speech = []
        track.ai_speech_raw = []
        track.ai_speaker_ids = None
        self._refresh_timeline_tracks(reset_view=False)
        self._update_ai_stats_panel(track)
        self._update_ai_options_panel(track)

        cfg = AiPipelineConfig()
        try:
            aggr = float(self.ai_aggr_slider.value())
        except Exception:
            aggr = 50.0
        vad_thr = 0.3 + (aggr / 100.0) * 0.4
        cfg.vad_threshold = float(max(0.1, min(0.9, vad_thr)))
        try:
            cfg.min_speech_s = float(self.ai_min_speech_ms.value()) / 1000.0
        except Exception:
            cfg.min_speech_s = 0.3
        try:
            cfg.merge_gap_s = float(self.ai_merge_gap_ms.value()) / 1000.0
        except Exception:
            cfg.merge_gap_s = 0.2
        try:
            idx = int(self.ai_expected_speakers.currentIndex())
            cfg.expected_speakers = idx if idx > 0 else 0
        except Exception:
            cfg.expected_speakers = 0
        worker = AiAnalyzeWorker(str(track.path), int(track_idx), cfg)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_ai_done, Qt.QueuedConnection)
        worker.error.connect(self._on_ai_error, Qt.QueuedConnection)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        worker.error.connect(thread.quit)
        worker.error.connect(worker.deleteLater)

        self._ai_threads[track_idx] = thread
        self._ai_workers[track_idx] = worker
        self._ai_expected_path[track_idx] = str(track.path)
        thread.finished.connect(lambda idx=track_idx: self._ai_threads.pop(idx, None))
        thread.finished.connect(lambda idx=track_idx: self._ai_workers.pop(idx, None))
        thread.finished.connect(lambda idx=track_idx: self._ai_expected_path.pop(idx, None))
        thread.finished.connect(self._update_split_button_state)

        self._set_ai_processing(True)
        self.statusBar().showMessage("AI processing...")
        try:
            self._web_js(self.web_topbar, f"uiSetState({json.dumps('AI processing...')});")
        except Exception:
            pass
        self._update_ai_options_panel(track)
        self._update_ai_stats_panel(track)
        thread.start()

    def _on_ai_process_clicked(self) -> None:
        if self.analysis_mode != "ai":
            return
        self._start_ai_analysis(self._active_track_index)

    def _on_ai_reprocess_clicked(self) -> None:
        if self.analysis_mode != "ai":
            return
        if not (0 <= self._active_track_index < len(self._tracks)):
            return
        track = self._tracks[self._active_track_index]
        track.ai_speech = []
        track.ai_speech_raw = []
        track.ai_speaker_ids = None
        self._update_ai_stats_panel(track)
        self._update_ai_options_panel(track)
        self._start_ai_analysis(self._active_track_index)
    def _on_ai_done(self, track_idx: int, duration: float, payload: object) -> None:
        if bool(getattr(self, "_pending_workspace_reset", False)):
            QTimer.singleShot(0, self._try_finalize_pending_workspace_reset)
            return
        if bool(getattr(self, "_workspace_resetting", False)):
            return
        if not (0 <= track_idx < len(self._tracks)):
            return
        track = self._tracks[track_idx]
        expected = self._ai_expected_path.get(track_idx)
        if expected and track.path and str(track.path) != str(expected):
            return
        if self.analysis_mode != "ai":
            return

        speech = []
        try:
            speech = list(payload.get("speech", []))  # type: ignore[union-attr]
        except Exception:
            speech = []
        try:
            speaker_ids = payload.get("speaker_ids", None)  # type: ignore[union-attr]
        except Exception:
            speaker_ids = None

        # If this track is a segment, map AI speech to segment-local time.
        seg_in = float(getattr(track, "segment_source_in", 0.0) or 0.0)
        seg_out = float(getattr(track, "segment_source_out", 0.0) or 0.0)
        is_segment = seg_out > seg_in + 1e-6

        local_raw: list[Segment] = []
        local_speakers: list[int] | None = [] if speaker_ids and isinstance(speaker_ids, list) else None

        if is_segment:
            seg_len = max(0.0, seg_out - seg_in)
            for i, s in enumerate(speech):
                try:
                    a = max(seg_in, float(s.start))
                    b = min(seg_out, float(s.end))
                except Exception:
                    continue
                if b > a:
                    local_raw.append(Segment(a - seg_in, b - seg_in))
                    if local_speakers is not None and i < len(speaker_ids):
                        local_speakers.append(int(speaker_ids[i]))
            track.duration = float(seg_len)
        else:
            local_raw = list(speech or [])
            if local_speakers is not None and speaker_ids:
                local_speakers = [int(x) for x in speaker_ids]
            try:
                track.duration = float(duration)
            except Exception:
                pass

        track.ai_speaker_ids = local_speakers if local_speakers is not None and local_speakers else None
        track.ai_speech_raw = list(local_raw)
        track.ai_speech = self._ai_postprocess_speech(track.ai_speech_raw, float(track.duration or 0.0))

        keeps = merge_overlaps(track.ai_speech)
        if not keeps:
            keeps = [Segment(0.0, float(track.duration or 0.0))]
        cuts = self._invert_keeps_to_cuts(float(track.duration or 0.0), keeps)

        track.keeps = keeps
        track.cuts = cuts
        track.manual_cuts = []
        track.suppressed_cuts = []
        track.cuts_enabled = True
        self._save_ai_cuts(track)

        self._set_ai_processing(False)
        self._refresh_analysis_workspace_ui(track)
        self.statusBar().showMessage("AI processing complete.", 4000)
        self._web_js(self.web_topbar, f"uiSetState({json.dumps('Ready')});")
        self._set_ready_dot_state("ready")

    def _on_ai_error(self, track_idx: int, msg: str) -> None:
        if bool(getattr(self, "_pending_workspace_reset", False)):
            QTimer.singleShot(0, self._try_finalize_pending_workspace_reset)
            return
        if bool(getattr(self, "_workspace_resetting", False)):
            return
        if self._is_cancelled_error(msg):
            self._set_ai_processing(False)
            self.statusBar().showMessage("AI analysis canceled.", 2500)
            return
        if not (0 <= track_idx < len(self._tracks)):
            return
        track = self._tracks[track_idx]
        if not track.path:
            return
        name = Path(track.path).name if track and track.path else "Track"
        self._set_ai_processing(False)
        QMessageBox.critical(self, "AI analysis failed", f"{name}: {msg}")
        track.ai_speech = []
        track.ai_speech_raw = []
        track.ai_speaker_ids = None
        # restore classic
        self._restore_classic_cuts(track)
        self._refresh_analysis_workspace_ui(track)
        try:
            self.analysis_mode_toggle.blockSignals(True)
            self.analysis_mode_toggle.setChecked(False)
        finally:
            self.analysis_mode_toggle.blockSignals(False)
        self.analysis_mode = "classic"

    # -----------------------------
    # Presets (FULL, forward-compatible)
    # -----------------------------
    def _normalize_preset_cfg(self, cfg: dict) -> dict:
        defaults = {name: getattr(self, name + "_default") for name in (
            "attack_ms", "release_ms", "smoothing_mode", "merge_pauses_ms",
            "normalize_lufs", "lufs_target", "limiter",
        )}
        return normalize_preset_cfg(cfg, defaults)

    def _current_preset_cfg(self) -> dict:
        """
        Full preset dict from current UI values + hidden advanced defaults.
        Later, when you add UI widgets for the new params, replace defaults with widget values.
        """
        cfg = {
            "intensity": int(self.slider_precision.value()),
            "threshold_pct": int(self.threshold_pct.value()),
            "pre_pad_s": float(self.pre_pad_s.value()),
            "post_pad_s": float(self.post_pad_s.value()),
            "min_cut_s": float(self.min_cut_s.value()),
            "gain_db": float(self.gain_db.value()),
            "gain_affects_detection": bool(self.gain_affects_detection.isChecked()),
            # new advanced (currently defaults until UI is wired)
            "attack_ms": int(self.attack_ms.value()),
            "release_ms": int(self.release_ms.value()),
            "smoothing_mode": str(self.smoothing_mode.currentText()),
            "merge_pauses_ms": int(self.merge_pauses_ms.value()),
            "normalize_lufs": bool(self.normalize_lufs.isChecked()),
            "lufs_target": float(self.lufs_target.value()),
            "limiter": bool(self.limiter.isChecked()),
        }
        return cfg

    def _load_presets(self):
        try:
            repository = PresetRepository()
            self.presets = repository.load()
            if not repository.path.is_file():
                repository.save(self.presets)
        except Exception as e:
            QMessageBox.warning(self, "Presets", f"Failed to read presets.json:\n{e}")
            self.presets = {}

    def _default_presets_catalog(self) -> dict[str, dict]:
        return default_presets_catalog()

    def _reset_presets_to_defaults(self) -> None:
        r = QMessageBox.question(
            self,
            "Reset presets",
            "Replace all saved presets with the 3 default presets?\n\nThis will remove your custom presets.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return

        self.presets = {
            name: self._normalize_preset_cfg(cfg)
            for name, cfg in self._default_presets_catalog().items()
        }
        self._save_presets()
        self._populate_presets_combo()
        self._pick_default_preset()
        if getattr(self, "_default_preset_name", None):
            idx = self.preset_combo.findText(str(self._default_preset_name))
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self._preset_dirty = False
        self._preset_source_name = None
        self._update_preset_ui_state()
        QMessageBox.information(self, "Reset presets", "Default presets restored.")

    def _save_presets(self):
        try:
            PresetRepository(self.presets_path).save(self.presets)
        except Exception as e:
            QMessageBox.critical(self, "Presets", f"Failed to save presets.json:\n{e}")

    def _populate_presets_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Manual")
        for name in sorted(self.presets.keys(), key=lambda s: s.lower()):
            self.preset_combo.addItem(name)
        self.preset_combo.setCurrentIndex(0)
        self.preset_combo.blockSignals(False)
        self.btn_preset_delete.setEnabled(False)
        self._preset_dirty = False
        self._preset_source_name = None
        self._update_preset_ui_state()

    def _pick_default_preset(self):
        if self.presets:
            # Prefer an explicit default if present
            for prefer in ("Balanced (Default)", "Gameplay (Default)", "Default"):
                for k in self.presets.keys():
                    if k.lower() == prefer.lower():
                        self._default_preset_name = k
                        return
            for k in self.presets.keys():
                if "(default)" in k.lower():
                    self._default_preset_name = k
                    return
            self._default_preset_name = sorted(self.presets.keys(), key=lambda s: s.lower())[0]
        else:
            self._default_preset_name = None

    def _apply_preset(self, name: str):
        cfg = self.presets.get(name)
        if not cfg:
            return
        cfg = self._normalize_preset_cfg(cfg)

        # Store advanced defaults (until UI exists)
        self.attack_ms_default = int(cfg.get("attack_ms", self.attack_ms_default))
        self.release_ms_default = int(cfg.get("release_ms", self.release_ms_default))
        self.smoothing_mode_default = str(cfg.get("smoothing_mode", self.smoothing_mode_default))
        self.merge_pauses_ms_default = int(cfg.get("merge_pauses_ms", self.merge_pauses_ms_default))
        self.normalize_lufs_default = bool(cfg.get("normalize_lufs", self.normalize_lufs_default))
        self.lufs_target_default = float(cfg.get("lufs_target", self.lufs_target_default))
        self.limiter_default = bool(cfg.get("limiter", self.limiter_default))

        self._applying_preset = True
        try:
            self.slider_precision.setValue(int(cfg["intensity"]))
            self.threshold_pct.setValue(int(cfg["threshold_pct"]))
            self.pre_pad_s.setValue(float(cfg.get("pre_pad_s", 0.25)))
            self.post_pad_s.setValue(float(cfg.get("post_pad_s", 0.25)))
            self.min_cut_s.setValue(float(cfg.get("min_cut_s", 0.10)))
            self.gain_db.setValue(float(cfg.get("gain_db", 0.0)))
            self.gain_affects_detection.setChecked(bool(cfg.get("gain_affects_detection", False)))
            self.attack_ms.setValue(int(cfg.get("attack_ms", self.attack_ms_default)))
            self.release_ms.setValue(int(cfg.get("release_ms", self.release_ms_default)))
            self.smoothing_mode.setCurrentText(str(cfg.get("smoothing_mode", self.smoothing_mode_default)))
            self.merge_pauses_ms.setValue(int(cfg.get("merge_pauses_ms", self.merge_pauses_ms_default)))

            self.normalize_lufs.setChecked(bool(cfg.get("normalize_lufs", self.normalize_lufs_default)))
            self.lufs_target.setValue(float(cfg.get("lufs_target", self.lufs_target_default)))
            self.limiter.setChecked(bool(cfg.get("limiter", self.limiter_default)))
        finally:
            self._applying_preset = False

        self._preset_dirty = False
        self._preset_source_name = name
        self._refresh_precision_label()
        self._refresh_threshold_ui_state()
        self._refresh_advanced_ui_state()
        self._update_preset_ui_state()
        self.btn_preset_delete.setEnabled(True)

        # preview gain should reflect preset immediately

        if self.analysis_mode != "ai":
            if self.rms is not None and self.duration > 0 and self.cuts_enabled:
                self._recompute()

    def _on_preset_changed(self, _index: int):
        name = self.preset_combo.currentText()
        if name == "Manual":
            self.btn_preset_delete.setEnabled(False)
            if not getattr(self, "_preset_dirty", False):
                self._preset_source_name = None
        else:
            self.btn_preset_delete.setEnabled(True)
            self._apply_preset(name)
            self._preset_dirty = False
            self._preset_source_name = name

        # Update stats preset name
        if self.stats_preset:
            self.stats_preset.setText(f"Preset: {name}")
        self._update_preset_ui_state()

    def _on_preset_save(self):
        current = self.preset_combo.currentText()
        new_cfg = self._current_preset_cfg()

        # Preserve unknown keys when overwriting
        if current != "Manual" and current in self.presets and isinstance(self.presets[current], dict):
            merged = dict(self.presets[current])
            merged.update(new_cfg)
            new_cfg = merged

        if current == "Manual":
            name, ok = pro_get_text(self, "Save preset", "Preset name:")
            if not ok:
                return
            name = name.strip()
            if not name:
                return
            if name.lower() == "manual":
                QMessageBox.warning(self, "Save preset", "The name 'Manual' is reserved.")
                return

            self.presets[name] = self._normalize_preset_cfg(new_cfg)
            self._save_presets()
            self._populate_presets_combo()
            idx = self.preset_combo.findText(name)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
            self._pick_default_preset()
            self._preset_dirty = False
            self._preset_source_name = name
            self._update_preset_ui_state()
            return

        self.presets[current] = self._normalize_preset_cfg(new_cfg)
        self._save_presets()
        self._preset_dirty = False
        self._preset_source_name = current
        self._update_preset_ui_state()
        QMessageBox.information(self, "Save preset", f"Preset '{current}' saved.")

    def _on_preset_save_as(self):
        base_name = self.preset_combo.currentText() if hasattr(self, "preset_combo") else "Preset"
        if base_name == "Manual":
            base_name = ""
        name, ok = pro_get_text(
            self,
            "Save preset as",
            "Preset name:",
            text=str(base_name).strip(),
        )
        if not ok:
            return
        name = str(name or "").strip()
        if not name:
            return
        if name.lower() == "manual":
            QMessageBox.warning(self, "Save preset", "The name 'Manual' is reserved.")
            return
        self.presets[name] = self._normalize_preset_cfg(self._current_preset_cfg())
        self._save_presets()
        self._populate_presets_combo()
        idx = self.preset_combo.findText(name)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self._pick_default_preset()
        self._preset_dirty = False
        self._preset_source_name = name
        self._update_preset_ui_state()

    def _open_preset_manage_menu(self):
        menu = QMenu(self)
        current = self.preset_combo.currentText()
        act_rename = None
        act_delete = None
        act_save = None
        act_reset_defaults = None
        if current != "Manual" and current in self.presets:
            act_rename = menu.addAction("Rename preset...")
            act_delete = menu.addAction("Delete preset")
        else:
            act_save = menu.addAction("Save as preset...")
        if self.presets:
            menu.addSeparator()
        act_reset_defaults = menu.addAction("Reset presets to defaults...")
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        if act_rename is not None and chosen == act_rename:
            self._rename_preset(current)
        elif act_delete is not None and chosen == act_delete:
            self._on_preset_delete()
        elif current == "Manual" and act_save is not None and chosen == act_save:
            self._on_preset_save_as()
        elif act_reset_defaults is not None and chosen == act_reset_defaults:
            self._reset_presets_to_defaults()

    def _rename_preset(self, current: str):
        if current == "Manual" or current not in self.presets:
            return
        name, ok = pro_get_text(self, "Rename preset", "New preset name:", text=current)
        if not ok:
            return
        name = str(name or "").strip()
        if not name or name == current:
            return
        if name.lower() == "manual":
            QMessageBox.warning(self, "Rename preset", "The name 'Manual' is reserved.")
            return
        if name in self.presets:
            QMessageBox.warning(self, "Rename preset", f"A preset named '{name}' already exists.")
            return
        self.presets[name] = self.presets.pop(current)
        self._save_presets()
        self._populate_presets_combo()
        idx = self.preset_combo.findText(name)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)
        self._pick_default_preset()
        self._preset_dirty = False
        self._preset_source_name = name
        self._update_preset_ui_state()

    def _on_preset_delete(self):
        current = self.preset_combo.currentText()
        if current == "Manual":
            return
        if current not in self.presets:
            return

        r = QMessageBox.question(
            self, "Delete preset",
            f"Delete preset '{current}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if r != QMessageBox.Yes:
            return

        del self.presets[current]
        self._save_presets()
        self._populate_presets_combo()
        self._pick_default_preset()
        self._preset_dirty = False
        self._preset_source_name = None
        self._update_preset_ui_state()

    # -----------------------------
    # Params change => recompute
    # -----------------------------
    def _on_params_changed(self):
        if not getattr(self, "_applying_preset", False):
            if self.preset_combo.currentText() != "Manual":
                self._preset_dirty = True
                self._preset_source_name = self.preset_combo.currentText()
                self.preset_combo.blockSignals(True)
                self.preset_combo.setCurrentIndex(0)
                self.preset_combo.blockSignals(False)
                self.btn_preset_delete.setEnabled(False)

        self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
        self._refresh_precision_label()
        self._refresh_threshold_ui_state()
        self._refresh_advanced_ui_state()
        self._update_preset_ui_state()

        if self.rms is not None and self.duration > 0 and self.cuts_enabled:
            self._recompute()

        # persist per-track settings
        try:
            self._save_track_cfg(self._get_active_track())
        except Exception:
            pass

    def _refresh_precision_label(self):
        v = self.slider_precision.value()
        if v < 10:
            name = "No cuts"
        elif v < 40:
            name = "Natural"
        elif v < 70:
            name = "Fast"
        else:
            name = "Tight"
        self.lbl_precision.setText(name)
        kind = "success" if v < 30 else ("info" if v < 70 else "warning")
        self._set_badge_label(getattr(self, "lbl_intensity_value", None), f"{int(v)}%", kind)

    # -----------------------------
    # Seek bar sync
    # -----------------------------
    def _on_seek_press(self):
        self._user_scrubbing = True

    def _on_seek_release(self):
        self._user_scrubbing = False
        try:
            self._app_log("seek_release", pos_ms=int(self.seek.value() or 0))
        except Exception:
            pass
        self._set_all_positions(self.seek.value())
        self.timeline.setPlayhead(self.seek.value() / 1000.0)

    def _on_seek_value_changed(self, v: int):
        if self._user_scrubbing:
            t = v / 1000.0
            self._sync_video_to_global(t, force=True)
            self._sync_audio_to_global(t, force=True)
            self.timeline.setPlayhead(t)
            self._update_time_label(t)

    def _apply_preview_volume(self, pct: int) -> None:
        try:
            p = max(0, min(100, int(pct)))
        except Exception:
            p = 100
        self._preview_volume_pct = p
        vol = float(p) / 100.0
        try:
            self.audio_output.setVolume(vol)
        except Exception:
            pass
        try:
            self.video_audio.setVolume(vol)
        except Exception:
            pass
        try:
            self.preview_volume_value.setText(f"{p}%")
        except Exception:
            pass
        try:
            if hasattr(self, "preview_volume_slider") and self.preview_volume_slider is not None:
                if int(self.preview_volume_slider.value()) != int(p):
                    self.preview_volume_slider.blockSignals(True)
                    self.preview_volume_slider.setValue(int(p))
                    self.preview_volume_slider.blockSignals(False)
        except Exception:
            pass
        try:
            self._web_js(self.web_transport, f"uiSetPreviewVolume({int(p)});")
        except Exception:
            pass

    def _on_preview_volume_changed(self, v: int) -> None:
        try:
            self._app_log("preview_volume_changed", value=int(v))
        except Exception:
            pass
        self._apply_preview_volume(v)

    def _set_preview_volume_from_web(self, v: int) -> None:
        try:
            self._app_log("preview_volume_changed_web", value=int(v))
        except Exception:
            pass
        self._apply_preview_volume(v)

    def _seek_to(self, t: float):
        t = float(t)
        total = self._global_duration if self._global_duration > 0 else self.duration
        if total > 0:
            t = max(0.0, min(t, float(total)))
        pos_ms = int(t * 1000)
        self._set_pending_seek(t)
        self._last_pos = t
        self._set_all_positions(pos_ms)

        self.seek.blockSignals(True)
        self.seek.setValue(pos_ms)
        self.seek.blockSignals(False)

        self.timeline.setPlayhead(float(t))
        self._update_time_label(float(t))

    # -----------------------------
    # Analysis thread
    # -----------------------------
    def _reset_analysis_state(self):
        self.duration = 0.0
        self.rms = None
        self.cuts = []
        self.keeps = []
        self.cuts_enabled = False
        self._rms_min = 0.0
        self._rms_max = 0.0
        self._rms_eps = 1e-9

        self.btn_export.setEnabled(False)
        self.btn_export_edl.setEnabled(False)
        self.export_progress.setValue(0)
        self.export_status.setText("")
        self.lbl_footer.setText("Duration - Output - Cuts")

        self._refresh_timeline_tracks(reset_view=True)
        self._web_zoom_steps = 0
        self._web_zoom_steps_max = self._calc_zoom_steps_max()
        self.seek.setRange(0, 0)
        self.lbl_time.setText("0:00 / 0:00")

        self._undo_stack.clear()
        self._redo_stack.clear()
        self._edit_undo_stack.clear()
        self._edit_redo_stack.clear()

        self.manual_cuts.clear()
        self.suppressed_cuts.clear()

        self.threshold_meter.set_reference_from_rms(None)
        self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
        self._refresh_threshold_ui_state()

        self._set_ready_dot_state("idle")
        self._set_export_dot_state("idle")

        self._pending_cut_start = None
        self._pending_cut_end = None
        self._set_pending_cut_visual(None, None)

        self._update_split_button_state()
        try:
            track = self._get_active_track()
            track.ai_speech = []
            track.ai_speech_raw = []
            track.ai_speaker_ids = None
        except Exception:
            pass
        self._update_ai_stats_panel(None)
        self._web_push_full_state()

    def _start_analysis_thread(self, path: str, track_idx: int):
        self._app_log("analysis_start", path=str(path), track_idx=int(track_idx))
        if not (0 <= int(track_idx) < len(self._tracks)):
            return
        target_track = self._tracks[int(track_idx)]
        old_t = self._analysis_threads.get(track_idx)
        if old_t is not None:
            old_w = self._analysis_workers.get(track_idx)
            try:
                if old_w is not None and hasattr(old_w, "cancel"):
                    old_w.cancel()
            except Exception:
                pass
            try:
                if old_t.isRunning():
                    old_t.quit()
                    old_t.wait(500)
            except Exception:
                pass
            try:
                if old_t is None or (not old_t.isRunning()):
                    self._analysis_threads.pop(track_idx, None)
                    self._analysis_workers.pop(track_idx, None)
                    self._analysis_expected_path.pop(track_idx, None)
                    self._analysis_job_ids.pop(track_idx, None)
                    self._analysis_targets.pop(track_idx, None)
                    self._analysis_progress.pop(track_idx, None)
            except Exception:
                pass
            if old_t is not None:
                try:
                    if old_t.isRunning():
                        # Avoid overlapping workers on same track index.
                        return
                except Exception:
                    return

        an_thread = QThread(self)
        an_worker = AnalyzeWorker(path, hop_s=0.03, sr=16000, track_idx=int(track_idx))
        job_id = self._next_analysis_job_id()
        an_worker.moveToThread(an_thread)

        an_thread.started.connect(an_worker.run)
        an_worker.done.connect(
            lambda idx, duration, rms_np, hop_s, auto_thr, jid=job_id, target=target_track, expected=str(path): self._on_analysis_done_guard(
                jid, idx, target, expected, duration, rms_np, hop_s, auto_thr
            ),
            Qt.QueuedConnection,
        )
        an_worker.error.connect(
            lambda idx, msg, jid=job_id, target=target_track, expected=str(path): self._on_analysis_error_guard(
                jid, idx, target, expected, msg
            ),
            Qt.QueuedConnection,
        )
        an_worker.progress.connect(
            lambda idx, pct, jid=job_id, target=target_track, expected=str(path): self._on_analysis_progress_guard(
                jid, idx, target, expected, pct
            ),
            Qt.QueuedConnection,
        )

        an_worker.done.connect(an_thread.quit)
        an_worker.done.connect(an_worker.deleteLater)
        an_thread.finished.connect(an_thread.deleteLater)

        an_worker.error.connect(an_thread.quit)
        an_worker.error.connect(an_worker.deleteLater)

        # keep refs while running
        self._analysis_threads[track_idx] = an_thread
        self._analysis_workers[track_idx] = an_worker
        self._analysis_expected_path[track_idx] = str(path)
        self._analysis_job_ids[track_idx] = int(job_id)
        self._analysis_targets[track_idx] = target_track
        self._analysis_progress[track_idx] = 0
        an_thread.finished.connect(
            lambda idx=track_idx, jid=job_id, thread=an_thread: self._cleanup_analysis_job_refs(idx, jid, thread)
        )
        an_thread.finished.connect(self._update_split_button_state)

        an_thread.start()
        # --- WebUI: show analyzing state ---
        self._web_js(self.web_topbar, f"uiSetState({json.dumps('Analyzing audio...')});")
        self._web_js(self.web_topbar, "uiSetProgress(12);")  # placeholder value (0-100)
        self._update_split_button_state()

    def _on_analysis_progress_guard(
        self,
        job_id: int,
        original_track_idx: int,
        target: TrackState,
        expected_path: str,
        percentage: int,
    ) -> None:
        if not self._is_current_analysis_job(original_track_idx, job_id):
            return
        self._analysis_progress[int(original_track_idx)] = max(0, min(100, int(percentage)))
        track_idx = self._resolve_analysis_target_index(
            original_track_idx, job_id, target, expected_path
        )
        if track_idx is None:
            return
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            return
        if track_idx != self._active_track_index:
            return
        pct = max(0, min(100, int(percentage)))
        self.statusBar().showMessage(f"Analyzing audio... {pct}%")
        self._web_js(self.web_topbar, f"uiSetProgress({10 + int(pct * 0.8)});")

    def _on_analysis_done_guard(
        self,
        job_id: int,
        original_track_idx: int,
        target: TrackState,
        expected_path: str,
        duration: float,
        rms_np: object,
        hop_s: float,
        auto_thr: float,
    ) -> None:
        track_idx = self._resolve_analysis_target_index(
            original_track_idx, job_id, target, expected_path
        )
        if track_idx is None:
            self._app_log(
                "analysis_done_stale_ignored",
                track_idx=int(original_track_idx),
                job_id=int(job_id),
                current=int(self._analysis_job_ids.get(original_track_idx, -1)),
            )
            return
        try:
            self._on_analysis_done(
                track_idx,
                duration,
                rms_np,
                hop_s,
                auto_thr,
                expected_path=expected_path,
            )
        except Exception as exc:
            self._app_log(
                "analysis_apply_error",
                track_idx=int(track_idx),
                job_id=int(job_id),
                message=str(exc),
            )
            self._on_analysis_error(track_idx, f"Failed to apply audio analysis: {exc}")

    def _on_analysis_error_guard(
        self,
        job_id: int,
        original_track_idx: int,
        target: TrackState,
        expected_path: str,
        msg: str,
    ) -> None:
        track_idx = self._resolve_analysis_target_index(
            original_track_idx, job_id, target, expected_path
        )
        if track_idx is None:
            self._app_log(
                "analysis_error_stale_ignored",
                track_idx=int(original_track_idx),
                job_id=int(job_id),
                current=int(self._analysis_job_ids.get(original_track_idx, -1)),
            )
            return
        self._on_analysis_error(track_idx, msg)


    def _on_analysis_done(
        self,
        track_idx: int,
        duration: float,
        rms_np: object,
        hop_s: float,
        auto_thr: float,
        expected_path: str | None = None,
    ):
        try:
            rms_len = int(len(rms_np)) if rms_np is not None else 0
        except Exception:
            rms_len = 0
        self._app_log(
            "analysis_done",
            track_idx=int(track_idx),
            duration_s=float(duration),
            hop_s=float(hop_s),
            rms_len=rms_len,
            auto_thr=float(auto_thr),
        )
        if bool(getattr(self, "_pending_workspace_reset", False)):
            QTimer.singleShot(0, self._try_finalize_pending_workspace_reset)
            return
        if bool(getattr(self, "_workspace_resetting", False)):
            return
        if track_idx < 0 or track_idx >= len(self._tracks):
            return
        track = self._tracks[track_idx]
        # Ignore stale analysis results (e.g., after reset or track replaced)
        expected = expected_path or self._analysis_expected_path.get(track_idx)
        if expected and track.path and not self._same_local_path(track.path, expected):
            return
        if track.path is None:
            return
        track.duration = float(duration)
        if rms_np is not None and not isinstance(rms_np, np.ndarray):
            try:
                rms_np = np.asarray(rms_np, dtype=np.float32)
            except Exception:
                rms_np = None
        track.rms = rms_np  # numpy array
        track.hop_s = float(hop_s)

        # ensure segment bounds are set for full tracks
        if float(getattr(track, "segment_source_out", 0.0) or 0.0) <= float(getattr(track, "segment_source_in", 0.0) or 0.0):
            track.segment_source_in = 0.0
            track.segment_source_out = float(track.duration or 0.0)

        # calibration for threshold meter mapping (per track, segment-aware)
        self._update_segment_rms_stats(track)
        track.cuts_enabled = True
        self._sync_project_from_track(track)
        has_restored_cuts_snapshot = bool(
            getattr(track, "cuts_restored", False)
            and (track.cuts or track.keeps)
        )

        # auto threshold percent for this track
        lo = track.rms_min
        hi = track.rms_max + track.rms_eps
        if hi <= lo + 1e-12:
            auto_pct = 45
        else:
            x = float(auto_thr)
            auto_pct = int(max(0, min(100, round((x - lo) / (hi - lo) * 100.0))))

        if track_idx != self._active_track_index:
            if not has_restored_cuts_snapshot:
                self._compute_cuts_for_track(track_idx)
            self._refresh_timeline_tracks(reset_view=False)
            total = self._global_duration if self._global_duration > 0 else self.duration
            self.seek.setRange(0, int(total * 1000))
            self._update_time_label(self.seek.value() / 1000.0)
            self._update_split_button_state()
            self._sync_analysis_ui_for_active_track()
            self._app_log(
                "analysis_applied",
                track_idx=int(track_idx),
                active=False,
                cuts=len(track.cuts or []),
            )
            return

        self._refresh_timeline_tracks(reset_view=True)
        total = self._global_duration if self._global_duration > 0 else self.duration
        self.seek.setRange(0, int(total * 1000))

        apply_auto_threshold = True
        if self._remember_filters_enabled and getattr(track, "filters_restored", False):
            apply_auto_threshold = False
        if apply_auto_threshold:
            self.threshold_pct.blockSignals(True)
            self.threshold_pct.setValue(int(auto_pct))
            self.threshold_pct.blockSignals(False)

        # already refreshed above
        self._web_zoom_steps = 0
        self._web_zoom_steps_max = self._calc_zoom_steps_max()
        self.threshold_meter.set_reference_from_rms(self._segment_rms(track))
        self.threshold_meter.set_threshold_pct(float(self.threshold_pct.value()))
        self._refresh_threshold_ui_state()

        self._update_time_label(0.0)

        self.cuts_enabled = True
        if has_restored_cuts_snapshot:
            self._apply_track_cuts_ui(track)
        else:
            self._recompute()

        # --- WebUI: update state + stats after analysis ---
        self._web_js(self.web_topbar, f"uiSetState({json.dumps('Ready')});")
        self._web_js(self.web_topbar, f"uiSetCrumbs({json.dumps(self._project_display_name())});")
        self._set_ready_dot_state("ready")
        self._web_push_full_state()

        self._set_stage("review")
        if has_restored_cuts_snapshot:
            self.statusBar().showMessage("Analysis complete. Restored remembered cuts.", 5000)
        else:
            self.statusBar().showMessage("Analysis complete. Cuts generated automatically.", 5000)
        self._update_split_button_state()
        self._app_log(
            "analysis_applied",
            track_idx=int(track_idx),
            active=True,
            cuts=len(track.cuts or []),
        )
        if not bool(getattr(self, "_in_auto_restore_session", False)):
            analyzed_path = str(track.path or "")
            analyzed_duration = float(track.duration or 0.0)
            QTimer.singleShot(
                250,
                lambda p=analyzed_path, d=analyzed_duration: self._enqueue_warm_export_cache(p, d),
            )

    def _on_analysis_error(self, track_idx: int, msg: str):
        self._app_log(
            "analysis_error",
            track_idx=int(track_idx),
            cancelled=bool(self._is_cancelled_error(msg)),
            message=str(msg),
        )
        if bool(getattr(self, "_pending_workspace_reset", False)):
            QTimer.singleShot(0, self._try_finalize_pending_workspace_reset)
            return
        if bool(getattr(self, "_workspace_resetting", False)):
            return
        if self._is_cancelled_error(msg):
            self.statusBar().showMessage("Analysis canceled.", 2500)
            self._update_split_button_state()
            self._sync_analysis_ui_for_active_track()
            return
        if track_idx != self._active_track_index:
            track = self._tracks[track_idx] if 0 <= track_idx < len(self._tracks) else None
            if not track or not track.path:
                return
            name = Path(track.path).name if track and track.path else "Track"
            QMessageBox.critical(self, "Analysis failed", f"{name}: {msg}")
            self._update_split_button_state()
            self._sync_analysis_ui_for_active_track()
            return

        if not self.input_path:
            return

        self.statusBar().clearMessage()
        self._set_stage("import")

        # Fallback UI: se il player ha gi? la durata, almeno mostri la timeline "vuota"
        try:
            dur_ms = int(self.video_player.duration() or 0)
            if dur_ms > 0 and (self.duration <= 0.0):
                self.duration = dur_ms / 1000.0
                self.seek.setRange(0, dur_ms)
                try:
                    track = self._get_active_track()
                    self._sync_project_from_track(track)
                    self._update_segment_rms_stats(track)
                except Exception:
                    pass
                self._refresh_timeline_tracks(reset_view=False)
                self._update_time_label(self.seek.value() / 1000.0)
        except Exception:
            pass

        QMessageBox.critical(self, "Analysis failed", msg)
        self._update_split_button_state()

    def _auto_thr_to_pct(self, auto_thr_amp: float) -> int:
        return threshold_amp_to_pct(
            auto_thr_amp, float(getattr(self, "_rms_min", 0.0)),
            float(getattr(self, "_rms_max", 0.0)), getattr(self, "_rms_eps", None),
        )

    def _threshold_pct_to_amp(self, pct: float) -> float:
        return threshold_pct_to_amp(
            pct, float(getattr(self, "_rms_min", 0.0)), float(getattr(self, "_rms_max", 0.0)),
            getattr(self, "_rms_eps", None), float(self.gain_db.value()),
            self.gain_affects_detection.isChecked(),
        )

    def _threshold_pct_to_amp_for_track(
        self, track: TrackState, pct: float, gain_db: float, gain_affects_detection: bool,
    ) -> float:
        return threshold_pct_to_amp(
            pct, float(track.rms_min or 0.0), float(track.rms_max or 0.0),
            track.rms_eps or None, gain_db, gain_affects_detection,
        )

    def _compute_cuts_for_track(self, track_idx: int) -> None:
        if track_idx < 0 or track_idx >= len(self._tracks):
            return
        track = self._tracks[track_idx]
        if track.path is None or track.rms is None or track.duration <= 0:
            return

        cfg = {}
        try:
            if isinstance(track.cfg, dict):
                cfg = dict(track.cfg)
        except Exception:
            cfg = {}
        if cfg:
            cfg = self._normalize_preset_cfg(cfg)
        else:
            cfg = self._normalize_preset_cfg(self._current_preset_cfg())

        seg_rms = self._segment_rms(track)
        if int(cfg["intensity"]) >= 10 and (seg_rms is None or getattr(seg_rms, "size", 0) == 0):
            return
        track.suppressed_cuts = merge_overlaps(list(track.suppressed_cuts or []))
        track.cuts, track.keeps = compute_classic_cuts(
            seg_rms if seg_rms is not None else np.array([]), float(track.duration), float(track.hop_s), cfg,
            manual_cuts=track.manual_cuts or [], suppressed_cuts=track.suppressed_cuts,
            rms_min=float(track.rms_min or 0.0), rms_max=float(track.rms_max or 0.0),
            rms_eps=track.rms_eps or None,
        )
        self._save_classic_cuts(track)

    # -----------------------------
    # Cuts recompute (supports new engine kwargs if present)
    # -----------------------------
    @staticmethod
    def _overlaps(a: Segment, b: Segment, eps: float = 1e-6) -> bool:
        return (min(a.end, b.end) - max(a.start, b.start)) > eps

    @staticmethod
    def _subtract_segments(base: list[Segment] | None, masks: list[Segment] | None, eps: float = 1e-6) -> list[Segment]:
        """
        Return base minus masks, preserving remaining fragments.
        Example: [0,10] - [3,4] => [0,3],[4,10]
        """
        if not base:
            return []
        base_m = merge_overlaps(list(base))
        if not masks:
            return [Segment(float(s.start), float(s.end)) for s in base_m]

        mask_m = merge_overlaps(list(masks))
        out: list[Segment] = []
        j = 0
        for seg in base_m:
            try:
                seg_start = float(seg.start)
                seg_end = float(seg.end)
            except Exception:
                continue
            if seg_end <= seg_start + eps:
                continue

            cur = seg_start
            while j < len(mask_m):
                try:
                    mj_end = float(mask_m[j].end)
                except Exception:
                    j += 1
                    continue
                if mj_end <= cur + eps:
                    j += 1
                    continue
                break

            k = j
            while k < len(mask_m):
                try:
                    ms = float(mask_m[k].start)
                    me = float(mask_m[k].end)
                except Exception:
                    k += 1
                    continue
                if ms >= seg_end - eps:
                    break
                if me <= cur + eps:
                    k += 1
                    continue
                if ms > cur + eps:
                    out.append(Segment(cur, min(ms, seg_end)))
                cur = max(cur, me)
                if cur >= seg_end - eps:
                    break
                k += 1

            if cur < seg_end - eps:
                out.append(Segment(cur, seg_end))

        return merge_overlaps(out)

    def _recompute(self):
        if self.input_path is None:
            return

        track = self._get_active_track()
        if track.rms is None or track.duration <= 0:
            return

        rms = self._segment_rms(track)
        if rms is None or getattr(rms, "size", 0) == 0:
            return

        track.manual_cuts = merge_overlaps(list(track.manual_cuts or []))
        track.suppressed_cuts = merge_overlaps(list(track.suppressed_cuts or []))
        track.cuts, track.keeps = compute_classic_cuts(
            rms, float(track.duration), float(track.hop_s), self._current_preset_cfg(),
            manual_cuts=track.manual_cuts, suppressed_cuts=track.suppressed_cuts,
            threshold_amp=self._threshold_pct_to_amp(float(self.threshold_pct.value())),
        )

        out_dur = sum(k.dur for k in track.keeps) if track.keeps else 0.0
        cuts_n = len(track.cuts)

        self.lbl_footer.setText(
            f"Duration {fmt_hms(float(track.duration))}  -  Output {fmt_hms(out_dur)}  -  {cuts_n} cuts"
        )

        self._refresh_timeline_tracks(reset_view=False)
        self._set_pending_cut_visual(
            self._pending_cut_start,
            self._pending_cut_end,
            track_state_idx=self._active_track_index,
        )

        export_enabled = bool(track.keeps) and out_dur > 0.01
        self.btn_export.setEnabled(export_enabled)
        self.btn_export_edl.setEnabled(export_enabled)
        if export_enabled:
            self._set_stage("export")
        # --- WebUI: refresh stats + inspector after recompute ---
        self._web_push_full_state()

        # Update right panel stats
        if self.stats_duration:
            dur = self._fmt_time(float(track.duration))
            out = self._fmt_time(out_dur)
            self.stats_duration.setText(f"Duration: {dur}")
            self.stats_output.setText(f"Output: {out}")
            self.stats_cuts.setText(f"Cuts: {cuts_n}")
            kept_pct = (out_dur / float(track.duration) * 100.0) if track.duration > 0 else 0.0
            self.stats_kept.setText(f"Kept: {kept_pct:.0f}%")

    # -----------------------------
    # Player updates
    # -----------------------------
    def _update_time_label(self, t: float):
        total = self._global_duration if self._global_duration > 0 else self.duration
        self.lbl_time.setText(f"{fmt_hms(t)} / {fmt_hms(total)}")

    def _on_player_duration_changed(self, dur_ms: int):
        if dur_ms is None or dur_ms <= 0:
            return
        dur_s = dur_ms / 1000.0
        duration_changed = False

        # update track duration if missing (based on current video source)
        track_idx = None
        try:
            src = self.video_player.source().toLocalFile()
        except Exception:
            src = ""
        if src:
            for i, t in enumerate(self._tracks):
                if t.path == src:
                    track_idx = i
                    break
        if track_idx is None and self._video_segments:
            seg = self._video_segments[self._video_segment_index]
            try:
                track_idx = int(seg.get("track_state_idx"))
            except Exception:
                track_idx = None
        if track_idx is None:
            track_idx = self._active_track_index

        if 0 <= track_idx < len(self._tracks):
            track = self._tracks[track_idx]
            if float(track.duration or 0.0) <= 0.0:
                track.duration = float(dur_s)
                duration_changed = True

        if duration_changed:
            try:
                self._sync_project_from_track(track)
            except Exception:
                pass
            self._rebuild_sequential_timeline()
            try:
                self._refresh_timeline_tracks(reset_view=False)
            except Exception:
                pass

        if self.rms is None and track_idx == self._active_track_index:
            self.duration = float(dur_s)
            self._refresh_timeline_tracks(reset_view=False)
            total = self._global_duration if self._global_duration > 0 else self.duration
            self.seek.setRange(0, int(total * 1000))
            self._update_time_label(self.seek.value() / 1000.0)

    def _on_video_status_changed(self, status):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        self._video_last_media_status = status
        self._media_dbg(f"video_status_changed status={self._mp_status_name(status)}")
        self._refresh_play_button_state()
        if status in (
            getattr(QMediaPlayer, "LoadedMedia", None),
            getattr(QMediaPlayer, "BufferedMedia", None),
            getattr(QMediaPlayer, "InvalidMedia", None),
            getattr(QMediaPlayer, "StalledMedia", None),
        ):
            self._media_debug_snapshot("on_video_status_changed")
        try:
            if status != QMediaPlayer.EndOfMedia:
                return
        except Exception:
            return
        # when a clip ends, switch to the next visible video at the same global time
        if not self._play_requested:
            return
        # Advance based on global time (supports overlapping/topmost video)
        if not self._video_segments:
            return
        try:
            cur_seg = None
            if 0 <= self._video_segment_index < len(self._video_segments):
                cur_seg = self._video_segments[self._video_segment_index]
            if cur_seg is not None:
                seg_start = float(cur_seg.get("start", 0.0) or 0.0)
                seg_dur = float(cur_seg.get("duration", 0.0) or 0.0)
                t = seg_start + seg_dur
            else:
                t = float(getattr(self, "_last_pos", 0.0) or 0.0)
        except Exception:
            t = float(getattr(self, "_last_pos", 0.0) or 0.0)

        mapped = self._map_global_to_video(t)
        if mapped is None:
            self._dbg_segments(f"video_status_end: no map for t={t:.3f}", force=True)
            self._play_requested = False
            return
        self._dbg_segments(f"video_status_end: advance to t={t:.3f}", force=True)
        try:
            self._sync_video_to_global(t, force=True)
            self._sync_audio_to_global(t, force=True)
            self.video_player.play()
        except Exception:
            pass

    def _on_audio_status_changed(self, status):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        self._audio_last_media_status = status
        self._media_dbg(f"audio_status_changed status={self._mp_status_name(status)}")
        self._refresh_play_button_state()
        if status in (
            getattr(QMediaPlayer, "LoadedMedia", None),
            getattr(QMediaPlayer, "BufferedMedia", None),
            getattr(QMediaPlayer, "InvalidMedia", None),
            getattr(QMediaPlayer, "StalledMedia", None),
        ):
            self._media_debug_snapshot("on_audio_status_changed")
        # Qt Multimedia may leave the dedicated audio player in StoppedState
        # after an async source load while the video player is already running.
        # Kick playback once media is loaded/buffered, but only in active play.
        try:
            v_playing = (self.video_player.playbackState() == QMediaPlayer.PlayingState)
        except Exception:
            v_playing = False
        if v_playing and status in (
            getattr(QMediaPlayer, "LoadedMedia", None),
            getattr(QMediaPlayer, "BufferedMedia", None),
        ):
            try:
                if self.audio_player.playbackState() != QMediaPlayer.PlayingState:
                    self._media_dbg("audio_status_changed auto_play_kick")
                    self.audio_player.play()
            except Exception:
                pass

    def _on_video_error(self, err, err_str: str | None = None):
        msg = str(err_str or "")
        self._app_log("video_error", err=str(err), message=msg)
        self._media_dbg(f"video_error err={err} msg={msg!r}")
        self._media_debug_snapshot("on_video_error")

    def _on_audio_error(self, err, err_str: str | None = None):
        msg = str(err_str or "")
        self._app_log("audio_error", err=str(err), message=msg)
        self._media_dbg(f"audio_error err={err} msg={msg!r}")
        self._media_debug_snapshot("on_audio_error")

    def _on_position_changed(self, pos_ms: int):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        force_av_sync = False
        seg_start = 0.0
        seg_src_in = 0.0
        seg_dur = 0.0
        if self._video_segments and 0 <= self._video_segment_index < len(self._video_segments):
            try:
                seg = self._video_segments[self._video_segment_index]
                seg_start = float(seg.get("start", 0.0) or 0.0)
                seg_src_in = float(seg.get("source_in", 0.0) or 0.0)
                seg_dur = float(seg.get("duration", 0.0) or 0.0)
            except Exception:
                seg_start = 0.0
        # Position is in source time; convert to global timeline time.
        t = (pos_ms / 1000.0) - seg_src_in + seg_start
        # Guard: when switching segments, QMediaPlayer may report 0 before it seeks.
        try:
            if time.monotonic() < self._segment_seek_guard_until:
                if (pos_ms / 1000.0) < (seg_src_in - 0.25):
                    self._dbg_segments(
                        f"pos_changed guard pos={pos_ms/1000.0:.3f} src_in={seg_src_in:.3f} seg_start={seg_start:.3f}",
                        force=True,
                    )
                    return
        except Exception:
            pass
        # Clamp only small lower drift to avoid jumping to previous segment due to keyframe jitter.
        if seg_dur > 0.0 and t < seg_start:
            if (seg_start - t) <= 2.0:
                self._dbg_segments(
                    f"pos_changed clamp t={t:.3f} -> {seg_start:.3f} seg_dur={seg_dur:.3f}"
                )
                t = seg_start
        global_ms = int(t * 1000)

        pending = getattr(self, "_pending_seek_target", None)
        if pending is not None:
            try:
                dt = time.monotonic() - float(getattr(self, "_pending_seek_ts", 0.0) or 0.0)
            except Exception:
                dt = 0.0
            try:
                tol = float(getattr(self, "_pending_seek_tolerance_s", 0.6))
            except Exception:
                tol = 0.6
            try:
                timeout = float(getattr(self, "_pending_seek_timeout_s", 0.9))
            except Exception:
                timeout = 0.9
            if abs(float(t) - float(pending)) > tol:
                if dt < timeout:
                    return
                # Timeout with stale position: force players back to pending target.
                try:
                    self._sync_video_to_global(float(pending), force=True)
                except Exception:
                    pass
                try:
                    self._sync_audio_to_global(float(pending), force=True)
                except Exception:
                    pass
                self._pending_seek_ts = time.monotonic()
                return
            # Accept position (close enough or timed out)
            self._pending_seek_target = None
            force_av_sync = True

        # keep video/audio aligned to global time (supports topmost video + active audio)
        try:
            self._sync_video_to_global(t, force=False)
        except Exception:
            pass
        try:
            self._sync_audio_to_global(t, force=force_av_sync)
        except Exception:
            pass

        self._last_pos = t
        # Throttle heavy UI updates while playing to keep playback smooth.
        now = time.monotonic()
        do_ui = True
        if (not self._user_scrubbing) and self.video_player.playbackState() == QMediaPlayer.PlayingState:
            if (now - self._last_ui_update_s) < self._ui_update_interval_s:
                do_ui = False
            else:
                self._last_ui_update_s = now
        else:
            self._last_ui_update_s = now

        if do_ui:
            # WebUI transport timecode (avoid full refresh)
            cur = self._fmt_time(t)
            dur = self._fmt_time(self._global_duration or self.duration or 0.0)
            self._web_js(self.web_transport, f"uiSetTimecode({json.dumps(cur)}, {json.dumps(dur)});")

            self.timeline.setPlayhead(t)
            # QMediaPlayer is the single clock; no per-track sync needed

            if not self._user_scrubbing:
                self.seek.blockSignals(True)
                self.seek.setValue(global_ms)
                self.seek.blockSignals(False)

            self._update_time_label(t)

        if not self.skip_on or self._skip_guard:
            return

        # Drive cuts/skip by the track that owns the current global time.
        amap = self._map_global_to_audio(t, None)
        if amap is None:
            return
        _aidx, _aseg, _local_src_t = amap
        try:
            track_idx = int(_aseg.get("track_state_idx"))
        except Exception:
            track_idx = None
        if track_idx is None or not (0 <= track_idx < len(self._tracks)):
            return

        track = self._tracks[track_idx]
        cuts = track.cuts or []
        if not track.cuts_enabled or not cuts:
            return

        try:
            seg_start = float(_aseg.get("start", 0.0) or 0.0)
        except Exception:
            seg_start = 0.0
        # cuts are segment-relative (0..segment duration)
        local_t = max(0.0, float(t) - seg_start)

        for c in cuts:
            if local_t < c.start:
                break
            if c.start <= local_t < c.end:
                self._skip_guard = True
                try:
                    new_global = seg_start + max(0.0, float(c.end))
                    # clamp to segment end / global duration
                    try:
                        seg_dur = float(_aseg.get("duration", 0.0) or 0.0)
                    except Exception:
                        seg_dur = 0.0
                    if seg_dur > 0.0:
                        # If skip lands at segment end, move slightly past it to continue to next segment.
                        if new_global >= (seg_start + seg_dur - 1e-3):
                            new_global = seg_start + seg_dur + 1e-3
                        else:
                            new_global = min(new_global, seg_start + seg_dur)
                    total = self._global_duration if self._global_duration > 0 else self.duration
                    if total > 0:
                        new_global = min(new_global, float(total))
                except Exception:
                    new_global = seg_start
                self._set_all_positions(int(new_global * 1000))
                self._skip_guard = False
                break

    # -----------------------------
    # Timeline: manual cuts / removal
    # -----------------------------
    def _snapshot_edit_state(self, kind: str = "cuts", track_idx: int | None = None) -> dict:
        k = str(kind or "cuts").strip().lower()
        if k == "timeline":
            return self._snapshot_timeline_edit_state()
        return self._snapshot_cut_edit_state(track_idx)

    def _push_undo_state(self, kind: str = "cuts", track_idx: int | None = None):
        snap = self._snapshot_edit_state(kind=kind, track_idx=track_idx)
        self._edit_undo_stack.append(snap)
        if len(self._edit_undo_stack) > int(self._edit_history_limit):
            self._edit_undo_stack = self._edit_undo_stack[-int(self._edit_history_limit):]
        self._edit_redo_stack.clear()

    def _undo_cuts(self):
        # Pending cut markers are ephemeral UI actions.
        # Undo should clear them first before touching persisted cut history.
        if self._pending_cut_start is not None or self._pending_cut_end is not None:
            self._cancel_pending_cut()
            try:
                self.statusBar().showMessage("Pending cut cleared.", 1200)
            except Exception:
                pass
            return

        if self._edit_undo_stack:
            state = self._edit_undo_stack.pop()
            kind = str(state.get("kind", "cuts"))
            if kind == "timeline":
                self._edit_redo_stack.append(self._snapshot_timeline_edit_state())
                self._restore_timeline_edit_state(state)
            else:
                self._edit_redo_stack.append(self._snapshot_cut_edit_state(state.get("track_idx")))
                self._restore_cut_edit_state(state)
            return

        # legacy fallback (older per-track stack)
        if not self._undo_stack:
            return
        self._redo_stack.append((
            self._clone_segments_list(self.manual_cuts),
            self._clone_segments_list(self.suppressed_cuts),
        ))
        manual, suppressed = self._undo_stack.pop()
        self.manual_cuts = manual
        self.suppressed_cuts = suppressed
        self._pending_cut_start = None
        self._pending_cut_end = None
        self._set_pending_cut_visual(None, None)
        self._web_push_full_state()
        self._recompute()

    def _redo_cuts(self):
        if self._edit_redo_stack:
            state = self._edit_redo_stack.pop()
            kind = str(state.get("kind", "cuts"))
            if kind == "timeline":
                self._edit_undo_stack.append(self._snapshot_timeline_edit_state())
                self._restore_timeline_edit_state(state)
            else:
                self._edit_undo_stack.append(self._snapshot_cut_edit_state(state.get("track_idx")))
                self._restore_cut_edit_state(state)
            return

        # legacy fallback (older per-track stack)
        if not self._redo_stack:
            return
        self._undo_stack.append((
            self._clone_segments_list(self.manual_cuts),
            self._clone_segments_list(self.suppressed_cuts),
        ))
        manual, suppressed = self._redo_stack.pop()
        self.manual_cuts = manual
        self.suppressed_cuts = suppressed
        self._pending_cut_start = None
        self._pending_cut_end = None
        self._set_pending_cut_visual(None, None)
        self._web_push_full_state()
        self._recompute()

    def _on_cut_clicked(self, idx: int, _global_pos):
        m = self._active_timeline_map()
        if not m or m.get("kind") != "audio":
            return
        if idx < 0 or idx >= len(self.cuts):
            return
        if QMessageBox.question(
            self,
            "Cut",
            "Remove this cut?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes:
            self._remove_cuts({idx})

    def _on_cuts_selected(self, indices: list[int], _global_pos):
        m = self._active_timeline_map()
        if not m or m.get("kind") != "audio":
            return
        if not indices:
            return
        if QMessageBox.question(
            self,
            "Cuts",
            f"Remove {len(indices)} selected cuts?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes:
            self._remove_cuts(set(indices))

    def _remove_cuts(self, to_remove: set[int]):
        if not to_remove or not self.cuts:
            return

        self._push_undo_state()
        visible_cuts = self._clone_segments_list(list(self.cuts or []))
        eps = 1e-3

        for idx in sorted(to_remove, reverse=True):
            if idx < 0 or idx >= len(self.cuts):
                continue
            c = self.cuts[idx]
            try:
                if idx < len(visible_cuts):
                    visible_cuts.pop(idx)
            except Exception:
                pass

            manual_all = list(self.manual_cuts or [])
            overlapping_manual: list[Segment] = []
            kept_manual: list[Segment] = []

            for mc in manual_all:
                try:
                    exact_match = (abs(float(mc.start) - float(c.start)) < eps) and (abs(float(mc.end) - float(c.end)) < eps)
                except Exception:
                    exact_match = False
                if exact_match or self._overlaps(mc, c):
                    overlapping_manual.append(Segment(float(mc.start), float(mc.end)))
                else:
                    kept_manual.append(mc)

            if overlapping_manual:
                self.manual_cuts = kept_manual
                suppress_parts = self._subtract_segments([Segment(float(c.start), float(c.end))], overlapping_manual)
                if suppress_parts:
                    self.suppressed_cuts.extend(suppress_parts)
            else:
                self.suppressed_cuts.append(Segment(float(c.start), float(c.end)))

        self.manual_cuts = merge_overlaps(self.manual_cuts)
        self.suppressed_cuts = merge_overlaps(self.suppressed_cuts)
        try:
            track = self._get_active_track()
            track.cuts = merge_overlaps(list(visible_cuts or []))
        except Exception:
            track = self._get_active_track()
        self._finalize_manual_cut_edit_without_reanalysis(track)

    def _apply_pending_cut_click(self, edit_t: float, *, auto_commit: bool = True) -> None:
        try:
            t = max(0.0, min(float(edit_t), float(self.duration)))
        except Exception:
            return

        # If a full pending range already exists (or a stale state), restart from a fresh start point.
        if (
            self._pending_cut_start is None
            or (
                self._pending_cut_end is not None
                and abs(float(self._pending_cut_end) - float(self._pending_cut_start or 0.0)) > 1e-6
            )
        ):
            self._pending_cut_start = float(t)
            self._pending_cut_end = None
            self._set_pending_cut_visual(self._pending_cut_start, self._pending_cut_end)
            try:
                self.statusBar().showMessage(f"Cut start set at {fmt_hms(t)}. Click end point.", 1500)
            except Exception:
                pass
            return

        self._pending_cut_end = float(t)
        self._set_pending_cut_visual(self._pending_cut_start, self._pending_cut_end)
        if abs(float(self._pending_cut_end) - float(self._pending_cut_start or 0.0)) <= 1e-6:
            # Same point clicked twice: keep the start marker only.
            self._pending_cut_end = None
            self._set_pending_cut_visual(self._pending_cut_start, self._pending_cut_end)
            return

        # Fast workflow: 2 clicks = create cut (no extra confirmation dialog).
        if auto_commit:
            self._commit_pending_cut(confirm=False)

    def _on_cut_tool_timeline_click(self, edit_t: float) -> None:
        if str(getattr(self, "tool_mode", "select")) != "cut":
            return
        self._apply_pending_cut_click(edit_t, auto_commit=True)


    def _on_timeline_context_menu(self, t_sec: float, global_pos, cut_meta):
        cut_idx = None
        row_kind = None
        track_state_idx = None
        source_t = None
        local_t = None
        cut_tool_click = False
        if isinstance(cut_meta, dict):
            cut_idx = cut_meta.get("cut_idx")
            row_kind = cut_meta.get("row_kind")
            track_state_idx = cut_meta.get("track_state_idx")
            source_t = cut_meta.get("source_t")
            local_t = cut_meta.get("local_t")
            cut_tool_click = bool(cut_meta.get("cut_tool_click"))
        else:
            cut_idx = cut_meta

        if track_state_idx is not None:
            try:
                ts_idx_i = int(track_state_idx)
            except Exception:
                ts_idx_i = None
            if ts_idx_i is not None and 0 <= ts_idx_i < len(self._tracks):
                if ts_idx_i != self._active_track_index:
                    playing = False
                    try:
                        playing = (self.video_player.playbackState() == QMediaPlayer.PlayingState)
                    except Exception:
                        playing = False
                    try:
                        playing = bool(playing or getattr(self, "_play_requested", False))
                    except Exception:
                        pass
                    self._activate_track(ts_idx_i, sync_players=(not playing))

        try:
            if local_t is not None:
                edit_t = float(local_t)
            elif source_t is not None:
                active_track = self._get_active_track()
                seg_in, _seg_out, _seg_len = self._segment_bounds(active_track)
                edit_t = float(source_t) - float(seg_in)
            else:
                edit_t = float(t_sec)
        except Exception:
            edit_t = float(t_sec)
        try:
            edit_t = max(0.0, min(float(edit_t), float(self.duration)))
        except Exception:
            edit_t = 0.0

        menu = QMenu(self)

        is_audio_row = (row_kind == "audio")
        can_edit_cuts = bool(is_audio_row or track_state_idx is not None)

        if cut_tool_click:
            if can_edit_cuts:
                self._on_cut_tool_timeline_click(float(edit_t))
            return

        act_mark_cut = None
        act_trim_start = None
        act_trim_end = None
        act_cancel = None
        act_remove_cut = None

        if can_edit_cuts:
            if cut_idx is None and isinstance(self.cuts, list):
                for i, c in enumerate(self.cuts):
                    try:
                        if float(c.start) <= float(edit_t) <= float(c.end):
                            cut_idx = i
                            break
                    except Exception:
                        continue
            # Single-step context action:
            # first invocation sets start, second invocation sets end and creates cut immediately.
            should_set_start = (
                self._pending_cut_start is None
                or (
                    self._pending_cut_end is not None
                    and abs(float(self._pending_cut_end) - float(self._pending_cut_start or 0.0)) > 1e-6
                )
            )
            if should_set_start:
                act_mark_cut = menu.addAction("Set cut start here")
            else:
                act_mark_cut = menu.addAction("Set cut end here (create immediately)")
            act_trim_start = menu.addAction("Cut from 0 to here")
            act_trim_end = menu.addAction("Cut from here to end")

            menu.addSeparator()
            if self._pending_cut_start is not None or self._pending_cut_end is not None:
                act_cancel = menu.addAction("Cancel pending cut")

            if cut_idx is not None and isinstance(cut_idx, int) and 0 <= cut_idx < len(self.cuts):
                menu.addSeparator()
                act_remove_cut = menu.addAction("Remove this cut")

        act_remove_track = None
        act_duplicate_track = None
        if track_state_idx is not None:
            menu.addSeparator()
            act_duplicate_track = menu.addAction("Duplicate this video")
            act_remove_track = menu.addAction("Remove this video")

        chosen = menu.exec(global_pos.toPoint())
        if chosen is None:
            return

        if act_remove_track is not None and chosen == act_remove_track:
            try:
                self._remove_track_at_index(int(track_state_idx))
            except Exception:
                pass
            return

        if act_duplicate_track is not None and chosen == act_duplicate_track:
            try:
                self._duplicate_track_at_index(int(track_state_idx))
            except Exception:
                pass
            return

        if not can_edit_cuts:
            return

        if chosen == act_trim_start:
            self._create_manual_cut_range(0.0, float(edit_t), confirm=True, clear_pending=True)
            return

        if chosen == act_trim_end:
            self._create_manual_cut_range(float(edit_t), float(self.duration), confirm=True, clear_pending=True)
            return

        if chosen == act_mark_cut:
            self._apply_pending_cut_click(float(edit_t), auto_commit=True)
            return

        if act_cancel is not None and chosen == act_cancel:
            self._cancel_pending_cut()
            return

        if act_remove_cut is not None and chosen == act_remove_cut:
            if isinstance(cut_idx, int) and 0 <= cut_idx < len(self.cuts):
                self._remove_cuts({cut_idx})
            return

    def _set_pending_cut_visual(
        self,
        start_s: float | None,
        end_s: float | None,
        track_state_idx: int | None = None,
    ) -> None:
        if not hasattr(self.timeline, "set_pending_cut"):
            return
        try:
            ts_idx = int(self._active_track_index if track_state_idx is None else track_state_idx)
            row_idx = int(self._timeline_index_for_audio_track(ts_idx))
            self.timeline.set_pending_cut(start_s, end_s, track_index=row_idx)
        except Exception:
            try:
                self.timeline.set_pending_cut(start_s, end_s)
            except Exception:
                pass

    def _cancel_pending_cut(self):
        self._pending_cut_start = None
        self._pending_cut_end = None
        self._set_pending_cut_visual(None, None)

        self._web_push_full_state()

    def _commit_pending_cut(self, confirm: bool = True):
        if self._pending_cut_start is None or self._pending_cut_end is None:
            return
        a = float(self._pending_cut_start)
        b = float(self._pending_cut_end)
        if abs(b - a) < 1e-6:
            return
        s = min(a, b)
        e = max(a, b)

        self._create_manual_cut_range(s, e, confirm=confirm, clear_pending=True)

    def _create_manual_cut_range(
        self,
        start_s: float,
        end_s: float,
        confirm: bool = True,
        clear_pending: bool = True
    ):
        start_s = max(0.0, min(float(start_s), float(self.duration)))
        end_s = max(0.0, min(float(end_s), float(self.duration)))
        if end_s <= start_s + 1e-6:
            return

        if confirm:
            if QMessageBox.question(
                self,
                "Create cut",
                f"Create cut from {fmt_hms(start_s)} to {fmt_hms(end_s)}?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            ) != QMessageBox.Yes:
                return

        self._push_undo_state()
        new_manual = Segment(start_s, end_s)
        self.manual_cuts.append(new_manual)
        self.manual_cuts = merge_overlaps(self.manual_cuts)
        if self.suppressed_cuts:
            # Explicit manual cut must override previously suppressed auto regions.
            self.suppressed_cuts = self._subtract_segments(list(self.suppressed_cuts or []), [new_manual])

        if clear_pending:
            self._cancel_pending_cut()

        track = self._get_active_track()
        track.cuts = merge_overlaps(list(getattr(track, "cuts", None) or []) + [new_manual])
        self._finalize_manual_cut_edit_without_reanalysis(track)

    # -----------------------------
    # Drag & Drop (also works on black preview via FrameVideoWidget)
    # -----------------------------
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md and md.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self._app_log("drop_event_received")
        if bool(getattr(self, "_pending_workspace_reset", False)) or bool(getattr(self, "_workspace_resetting", False)):
            try:
                self.statusBar().showMessage("Cancelling analysis in progress... please wait.", 3500)
            except Exception:
                pass
            return
        md = event.mimeData()
        if not md or not md.hasUrls():
            return
        urls = md.urls()
        if not urls:
            return
        for url in urls:
            local = url.toLocalFile()
            if local:
                self._app_log("drop_open_path", path=str(local))
                self._open_path(local)

    # -----------------------------
    # Export
    # -----------------------------
    def _edl_fps(self) -> int:
        try:
            fps = float(getattr(self.project, "fps", 0.0) or 0.0)
        except Exception:
            fps = 0.0
        if fps <= 0.0:
            fps = 30.0
        fps_i = int(round(fps))
        return max(1, min(fps_i, 120))

    @staticmethod
    def _edl_frames_from_seconds(sec: float, fps: int) -> int:
        return int(round(max(0.0, float(sec)) * float(fps)))

    @staticmethod
    def _edl_timecode_from_frames(frames: int, fps: int) -> str:
        frames = max(0, int(frames))
        fps = max(1, int(fps))
        ff = frames % fps
        total_s = frames // fps
        ss = total_s % 60
        mm = (total_s // 60) % 60
        hh = total_s // 3600
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    @staticmethod
    def _edl_reel_name(path: str, used: dict[str, str]) -> str:
        try:
            stem = Path(path).stem
        except Exception:
            stem = ""
        clean = "".join(c for c in stem if c.isalnum()).upper()
        if not clean:
            clean = "CLIP"
        base = clean[:8]
        if base not in used:
            used[base] = path
            return base
        if used.get(base) == path:
            return base
        for i in range(1, 1000):
            suffix = str(i)
            cand = (clean[: max(1, 8 - len(suffix))] + suffix)[:8]
            if cand not in used:
                used[cand] = path
                return cand
        return base

    def _build_edl_text(self, keeps: list[Segment]) -> str:
        self._rebuild_video_segments()
        if not self._video_segments:
            raise RuntimeError("No video segments available for EDL export.")

        fps = self._edl_fps()
        keeps_sorted = sorted(keeps, key=lambda s: float(s.start))
        reel_map: dict[str, str] = {}
        events: list[dict[str, object]] = []
        rec_frames = 0

        for k in keeps_sorted:
            ks = max(0.0, float(k.start))
            ke = max(0.0, float(k.end))
            if ke <= ks:
                continue

            for seg in self._video_segments:
                seg_start = float(seg.get("start", 0.0) or 0.0)
                seg_end = float(seg.get("end", 0.0) or 0.0)
                if seg_end <= ks:
                    continue
                if seg_start >= ke:
                    break

                sub_start = max(ks, seg_start)
                sub_end = min(ke, seg_end)
                if sub_end <= sub_start:
                    continue

                dur_s = sub_end - sub_start
                dur_frames = self._edl_frames_from_seconds(dur_s, fps)
                if dur_frames <= 0:
                    continue

                src_in_s = (sub_start - seg_start) + float(seg.get("source_in", 0.0) or 0.0)
                src_in_frames = self._edl_frames_from_seconds(src_in_s, fps)
                src_out_frames = src_in_frames + dur_frames

                rec_in_frames = rec_frames
                rec_out_frames = rec_in_frames + dur_frames
                rec_frames = rec_out_frames

                path = str(seg.get("path") or "")
                reel = self._edl_reel_name(path, reel_map)
                clip_name = Path(path).name if path else reel

                events.append(
                    {
                        "reel": reel,
                        "src_in": src_in_frames,
                        "src_out": src_out_frames,
                        "rec_in": rec_in_frames,
                        "rec_out": rec_out_frames,
                        "clip_name": clip_name,
                        "path": path,
                    }
                )

        if not events:
            raise RuntimeError("No EDL events generated (keeps may be empty).")

        title = self._project_display_name()
        if not title or title == "-":
            title = "Auto Cutter"

        lines: list[str] = []
        lines.append(f"TITLE: {title}")
        lines.append("FCM: NON-DROP FRAME")
        lines.append("")

        audio_track = "A"
        video_track = "V"

        for i, ev in enumerate(events, start=1):
            reel = str(ev["reel"])
            src_in = self._edl_timecode_from_frames(int(ev["src_in"]), fps)
            src_out = self._edl_timecode_from_frames(int(ev["src_out"]), fps)
            rec_in = self._edl_timecode_from_frames(int(ev["rec_in"]), fps)
            rec_out = self._edl_timecode_from_frames(int(ev["rec_out"]), fps)

            line_v = f"{i:03d}  {reel:<8} {video_track:<4} C        {src_in} {src_out} {rec_in} {rec_out}"
            line_a = f"{i:03d}  {reel:<8} {audio_track:<4} C        {src_in} {src_out} {rec_in} {rec_out}"
            lines.append(line_v)
            lines.append(line_a)
            lines.append(f"* FROM CLIP NAME: {ev['clip_name']}")
            if ev.get("path"):
                lines.append(f"* SOURCE FILE: {ev['path']}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def export_edl(self):
        self._app_log("export_edl_request_begin")
        keeps_for_export = self._collect_global_keeps()
        if not keeps_for_export:
            QMessageBox.warning(self, "Export EDL", "Nothing to export (keeps is empty).")
            self._app_log("export_edl_request_rejected", reason="empty_keeps")
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export EDL",
            "auto_cutter.edl",
            "EDL (*.edl)"
        )
        if not out_path:
            self._app_log("export_edl_request_cancelled_save_dialog")
            return

        try:
            edl_text = self._build_edl_text(keeps_for_export)
            Path(out_path).write_text(edl_text, encoding="utf-8", newline="\n")
        except Exception as e:
            self._app_log("export_edl_error", message=str(e))
            QMessageBox.critical(self, "Export EDL failed", str(e))
            return

        self._app_log("export_edl_done", output_path=str(out_path))
        QMessageBox.information(self, "Export EDL", "EDL exported successfully.")

    def _active_audio_clips(self) -> list[Clip]:
        idx = int(self._active_track_index) if self._active_track_index is not None else -1
        if idx < 0 or idx >= len(self._tracks):
            return []
        tstate = self._tracks[idx]
        self._ensure_audio_track_for_state(tstate)
        if not tstate.audio_track_id:
            return []
        atrack = self.project.get_track(str(tstate.audio_track_id))
        if atrack is None:
            return []
        return list(atrack.sorted_clips())

    def _all_audio_clips_with_state(self) -> list[dict]:
        clips: list[dict] = []
        for idx, tstate in enumerate(self._tracks):
            if not tstate.audio_track_id:
                continue
            atrack = self.project.get_track(str(tstate.audio_track_id))
            if atrack is None:
                continue
            for c in atrack.sorted_clips():
                clips.append({"clip": c, "track_state_idx": idx})
        return clips

    def _video_clips_with_state(self) -> list[dict]:
        clips: list[dict] = []
        for idx, tstate in enumerate(self._tracks):
            if not tstate.video_track_id:
                continue
            vtrack = self.project.get_track(str(tstate.video_track_id))
            if vtrack is None:
                continue
            for c in vtrack.sorted_clips():
                clips.append({"clip": c, "track_state_idx": idx})
        return clips

    def _active_audio_timeline_keeps(self, audio_clips: list[Clip]) -> list[Segment]:
        if not audio_clips:
            return []
        idx = int(self._active_track_index) if self._active_track_index is not None else -1
        if idx < 0 or idx >= len(self._tracks):
            return []
        track = self._tracks[idx]
        if not getattr(track, "cuts_enabled", True):
            return []
        keeps = list(track.keeps or [])
        if not keeps:
            return []

        out: list[Segment] = []
        for clip in audio_clips:
            try:
                s_in = float(clip.source_in)
                s_out = float(clip.source_out)
                t_in = float(clip.timeline_in)
            except Exception:
                continue
            for k in keeps:
                try:
                    ks = float(k.start)
                    ke = float(k.end)
                except Exception:
                    continue
                os = max(ks, s_in)
                oe = min(ke, s_out)
                if oe > os:
                    out.append(Segment(t_in + (os - s_in), t_in + (oe - s_in)))
        if not out:
            return []
        return merge_overlaps(out)

    def _build_flatten_export_segments(self) -> tuple[list[str], list[dict]]:
        video_items = self._video_clips_with_state()
        audio_items = self._all_audio_clips_with_state()

        edges = set()
        for item in video_items:
            c = item["clip"]
            edges.add(float(c.timeline_in))
            edges.add(float(c.timeline_out))
        for item in audio_items:
            c = item["clip"]
            edges.add(float(c.timeline_in))
            edges.add(float(c.timeline_out))

        if not edges:
            return [], []

        edges = sorted({t for t in edges if t is not None})
        if len(edges) < 2:
            return [], []

        def pick_video(t: float):
            best = None
            best_rank = None
            for item in video_items:
                c = item["clip"]
                try:
                    if float(c.timeline_in) <= t < float(c.timeline_out):
                        rank = self._video_layer_rank(item.get("track_state_idx"))
                        if best_rank is None or rank < best_rank:
                            best = item
                            best_rank = rank
                except Exception:
                    continue
            return best

        def pick_audio(t: float):
            best = None
            best_rank = None
            for item in audio_items:
                c = item["clip"]
                try:
                    if float(c.timeline_in) <= t < float(c.timeline_out):
                        rank = self._video_layer_rank(item.get("track_state_idx"))
                        if best_rank is None or rank < best_rank:
                            best = item
                            best_rank = rank
                except Exception:
                    continue
            return best

        base_segments: list[dict] = []
        for i in range(len(edges) - 1):
            t0 = float(edges[i])
            t1 = float(edges[i + 1])
            if t1 <= t0:
                continue
            mid = (t0 + t1) * 0.5
            v_item = pick_video(mid)
            a_item = pick_audio(mid)
            if v_item is None and a_item is None:
                continue
            base_segments.append(
                {
                    "start": t0,
                    "end": t1,
                    "v_item": v_item,
                    "a_item": a_item,
                }
            )

        if not base_segments:
            return [], []

        # Apply global keeps from the whole timeline (all tracks/segments/duplicates).
        keeps = self._collect_global_keeps()
        if keeps:
            keeps = sorted(keeps, key=lambda s: s.start)
            filtered: list[dict] = []
            ki = 0
            for seg in base_segments:
                s0 = float(seg["start"])
                s1 = float(seg["end"])
                while ki < len(keeps) and keeps[ki].end <= s0:
                    ki += 1
                kj = ki
                while kj < len(keeps) and keeps[kj].start < s1:
                    ks = max(s0, keeps[kj].start)
                    ke = min(s1, keeps[kj].end)
                    if ke > ks:
                        filtered.append(
                            {
                                "start": ks,
                                "end": ke,
                                "v_item": seg["v_item"],
                                "a_item": seg.get("a_item", None),
                            }
                        )
                    if keeps[kj].end <= s1:
                        kj += 1
                    else:
                        break
                ki = kj
            base_segments = filtered

        # Build export segments with input indices
        input_paths: list[str] = []
        path_to_idx: dict[str, int] = {}

        def _path_idx(path: str) -> int:
            if path in path_to_idx:
                return path_to_idx[path]
            idx = len(input_paths)
            input_paths.append(path)
            path_to_idx[path] = idx
            return idx

        out_segments: list[dict] = []
        eps = 1e-6
        for seg in base_segments:
            t0 = float(seg["start"])
            t1 = float(seg["end"])
            dur = t1 - t0
            if dur <= eps:
                continue

            v_idx = None
            v_in = None
            v_out = None
            v_item = seg.get("v_item", None)
            if v_item and isinstance(v_item, dict):
                v_clip = v_item.get("clip")
                if v_clip is not None:
                    media = self.project.get_media(v_clip.media_id)
                    v_path = media.path if media else None
                    if v_path:
                        v_idx = _path_idx(str(v_path))
                        try:
                            v_in = float(v_clip.source_in) + (t0 - float(v_clip.timeline_in))
                        except Exception:
                            v_in = float(v_clip.source_in)
                        v_out = float(v_in) + dur

            a_idx = None
            a_in = None
            a_out = None
            a_clip = None
            a_item = seg.get("a_item", None)
            if a_item and isinstance(a_item, dict):
                a_clip = a_item.get("clip", None)
            if a_clip is not None:
                media = self.project.get_media(a_clip.media_id)
                a_path = media.path if media else None
                if a_path:
                    a_idx = _path_idx(str(a_path))
                    try:
                        a_in = float(a_clip.source_in) + (t0 - float(a_clip.timeline_in))
                    except Exception:
                        a_in = float(a_clip.source_in)
                    a_out = float(a_in) + dur

            if v_idx is None and a_idx is None:
                continue

            out_segments.append(
                {
                    "start": float(t0),
                    "end": float(t1),
                    "duration": float(dur),
                    "v_idx": v_idx,
                    "v_in": v_in,
                    "v_out": v_out,
                    "a_idx": a_idx,
                    "a_in": a_in,
                    "a_out": a_out,
                }
            )

        return input_paths, out_segments

    def _export_setting_controls(self) -> tuple[QWidget, ...]:
        return (
            self.codec_combo,
            self.export_method_combo,
            self.cut_quality_combo,
            self.container_combo,
            self.output_mode_combo,
            self.resolution_combo,
            self.aspect_combo,
            self.no_upscale_cb,
            self.fps_combo,
            self.fps_mode_combo,
            self.video_quality_combo,
            self.rate_control_combo,
            self.video_bitrate_spin,
            self.target_size_spin,
            self.custom_quality_spin,
            self.two_pass_cb,
            self.audio_codec_combo,
            self.audio_bitrate_combo,
            self.sample_rate_combo,
            self.channels_combo,
            self.pixel_depth_combo,
            self.color_mode_combo,
            self.range_start_spin,
            self.range_end_spin,
            self.parallel_workers_spin,
            self.chunk_count_spin,
            self.hwaccel_cb,
        )

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        idx = int(combo.findData(value))
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _export_settings_from_ui(self) -> ExportSettings:
        cut_keys = ("balanced", "higher", "faster", "maximum_speed")
        cut_idx = max(0, min(int(self.cut_quality_combo.currentIndex()), len(cut_keys) - 1))
        return ExportSettings(
            preset=str(self.export_preset_combo.currentData() or "custom"),
            codec=str(self.codec_combo.currentData() or "auto"),
            method=str(self.export_method_combo.currentData() or "auto"),
            container=str(self.container_combo.currentData() or "mp4"),
            output_mode=str(self.output_mode_combo.currentData() or "single"),
            resolution=str(self.resolution_combo.currentData() or "source"),
            aspect=str(self.aspect_combo.currentData() or "source"),
            no_upscale=bool(self.no_upscale_cb.isChecked()),
            fps=str(self.fps_combo.currentData() or "source"),
            fps_mode=str(self.fps_mode_combo.currentData() or "cfr"),
            quality=str(self.video_quality_combo.currentData() or "very_high"),
            rate_control=str(self.rate_control_combo.currentData() or "quality"),
            video_bitrate_mbps=float(self.video_bitrate_spin.value()),
            target_size_mb=int(self.target_size_spin.value()),
            custom_quality=int(self.custom_quality_spin.value()),
            two_pass=bool(self.two_pass_cb.isChecked()),
            audio_codec=str(self.audio_codec_combo.currentData() or "auto"),
            audio_bitrate_kbps=int(self.audio_bitrate_combo.currentData() or 320),
            sample_rate=str(self.sample_rate_combo.currentData() or "source"),
            channels=str(self.channels_combo.currentData() or "source"),
            pixel_depth=str(self.pixel_depth_combo.currentData() or "source"),
            color_mode=str(self.color_mode_combo.currentData() or "preserve"),
            cut_quality=cut_keys[cut_idx],
            parallel_workers=int(self.parallel_workers_spin.value()),
            chunk_count=int(self.chunk_count_spin.value()),
            hwaccel_decode=bool(self.hwaccel_cb.isChecked()),
            range_start=float(self.range_start_spin.value()),
            range_end=float(self.range_end_spin.value()),
        ).normalized()

    def _apply_export_settings_to_ui(self, settings: ExportSettings) -> None:
        value = settings.normalized()
        self._applying_export_settings = True
        try:
            self._set_combo_data(self.export_preset_combo, value.preset)
            self._set_combo_data(self.codec_combo, value.codec)
            self._set_combo_data(self.export_method_combo, value.method)
            self._set_combo_data(self.container_combo, value.container)
            self._set_combo_data(self.output_mode_combo, value.output_mode)
            self._set_combo_data(self.resolution_combo, value.resolution)
            self._set_combo_data(self.aspect_combo, value.aspect)
            self.no_upscale_cb.setChecked(value.no_upscale)
            self._set_combo_data(self.fps_combo, value.fps)
            self._set_combo_data(self.fps_mode_combo, value.fps_mode)
            self._set_combo_data(self.video_quality_combo, value.quality)
            self._set_combo_data(self.rate_control_combo, value.rate_control)
            self.video_bitrate_spin.setValue(value.video_bitrate_mbps)
            self.target_size_spin.setValue(value.target_size_mb)
            self.custom_quality_spin.setValue(value.custom_quality)
            self.two_pass_cb.setChecked(value.two_pass)
            self._set_combo_data(self.audio_codec_combo, value.audio_codec)
            self._set_combo_data(self.audio_bitrate_combo, value.audio_bitrate_kbps)
            self._set_combo_data(self.sample_rate_combo, value.sample_rate)
            self._set_combo_data(self.channels_combo, value.channels)
            self._set_combo_data(self.pixel_depth_combo, value.pixel_depth)
            self._set_combo_data(self.color_mode_combo, value.color_mode)
            cut_index = {
                "balanced": 0,
                "higher": 1,
                "faster": 2,
                "maximum_speed": 3,
            }.get(value.cut_quality, 0)
            self.cut_quality_combo.setCurrentIndex(cut_index)
            self.parallel_workers_spin.setValue(value.parallel_workers)
            self.chunk_count_spin.setValue(value.chunk_count)
            self.hwaccel_cb.setChecked(value.hwaccel_decode)
            self.range_start_spin.setValue(value.range_start)
            self.range_end_spin.setValue(value.range_end)
        finally:
            self._applying_export_settings = False
        self._sync_export_settings_ui(value)

    def _load_export_settings(self) -> None:
        settings = ExportSettings.defaults()
        try:
            raw = str(QSettings("Auto Cutter", "Auto Cutter").value("export/settings_v1", "") or "")
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    settings = ExportSettings.from_mapping(parsed)
        except Exception:
            settings = ExportSettings.defaults()
        self._apply_export_settings_to_ui(settings)

    def _save_export_settings(self, settings: ExportSettings | None = None) -> None:
        value = (settings or self._export_settings_from_ui()).normalized()
        QSettings("Auto Cutter", "Auto Cutter").setValue(
            "export/settings_v1",
            json.dumps(value.to_mapping(), ensure_ascii=True, sort_keys=True),
        )

    def _sync_export_settings_ui(self, settings: ExportSettings | None = None) -> None:
        value = (settings or self._export_settings_from_ui()).normalized()
        rate = value.rate_control
        bitrate_visible = rate == "bitrate"
        target_visible = rate == "target_size"
        custom_visible = value.quality == "custom" and rate == "quality"
        getattr(self, "export_bitrate_wrap", self.video_bitrate_spin).setVisible(bitrate_visible)
        getattr(self, "export_target_size_wrap", self.target_size_spin).setVisible(target_visible)
        getattr(self, "export_custom_quality_wrap", self.custom_quality_spin).setVisible(custom_visible)
        self.two_pass_cb.setEnabled(value.codec == "libx264" and rate in {"bitrate", "target_size"})
        self.audio_bitrate_combo.setEnabled(value.audio_codec not in {"copy", "pcm_s24le"})
        selected_range = value.output_mode == "selected_range"
        getattr(self, "export_range_wrap", self.range_start_spin).setVisible(selected_range)
        self.fps_mode_combo.setEnabled(value.fps == "source")
        messages = value.compatibility_messages()
        self.export_compatibility_label.setText(" ".join(messages))
        self.export_compatibility_label.setProperty(
            "status",
            "warning" if value.requires_accurate_pipeline() else "ok",
        )
        self.export_compatibility_label.style().unpolish(self.export_compatibility_label)
        self.export_compatibility_label.style().polish(self.export_compatibility_label)
        self.btn_export.setText(
            "Export audio" if value.output_mode == "audio_only" else
            "Export clips" if value.output_mode == "per_clip" else
            "Export video"
        )
        try:
            self._push_topbar_status_chips()
        except Exception:
            pass

    @Slot()
    def _on_export_preset_changed(self) -> None:
        if self._applying_export_settings:
            return
        preset = str(self.export_preset_combo.currentData() or "custom")
        current = self._export_settings_from_ui()
        updated = current.with_preset(preset)
        self._apply_export_settings_to_ui(updated)
        self._save_export_settings(updated)

    @Slot()
    def _on_export_setting_changed(self) -> None:
        if self._applying_export_settings:
            return
        value = self._export_settings_from_ui()
        if value.preset != "custom":
            value = replace(value, preset="custom")
        self._apply_export_settings_to_ui(value)
        self._save_export_settings(value)

    @Slot()
    def _reset_export_settings(self) -> None:
        defaults = ExportSettings.defaults()
        self._apply_export_settings_to_ui(defaults)
        self._save_export_settings(defaults)
        self.export_advisor_result.setText("Export settings restored to the recommended defaults.")

    @Slot()
    def _apply_export_recommendation(self) -> None:
        recommendation = getattr(self, "_last_export_recommendation", None)
        if recommendation is None:
            return
        current = self._export_settings_from_ui()
        updated = replace(
            current,
            preset="custom",
            codec=str(recommendation.codec),
            method="auto",
            parallel_workers=int(recommendation.workers),
            chunk_count=int(recommendation.chunks),
            quality="very_high",
            rate_control="quality",
            custom_quality=16,
        ).normalized()
        self._apply_export_settings_to_ui(updated)
        self._save_export_settings(updated)
        self.btn_export_advisor_apply.setEnabled(False)
        self.export_advisor_result.setText(
            "Recommendation applied. You can still review and change every value before export."
        )
        self._app_log(
            "export_advisor_applied",
            codec=updated.codec,
            workers=updated.parallel_workers,
            chunks=updated.chunk_count,
        )

    @Slot()
    def _clear_export_cache(self) -> None:
        if bool(getattr(self, "_export_processing", False)):
            self.statusBar().showMessage("Stop the active export before clearing its cache.", 5000)
            return
        try:
            removed_chunks = int(ExportWorker.clear_persistent_chunk_cache() or 0)
        except Exception:
            removed_chunks = 0
        try:
            removed_keyframes = int(clear_keyframe_cache() or 0)
        except Exception:
            removed_keyframes = 0
        self.export_advisor_result.setText(
            f"Render cache cleared: {removed_chunks} chunk entries, "
            f"{removed_keyframes} keyframe entries."
        )
        self._app_log(
            "export_cache_cleared",
            chunks=removed_chunks,
            keyframes=removed_keyframes,
        )

    @Slot()
    def _start_export_advisor(self) -> None:
        if not self.input_path or not self.ffmpeg_path:
            self.export_advisor_result.setText("Load a video before running the settings advisor.")
            return
        current_thread = getattr(self, "export_advisor_thread", None)
        if current_thread is not None and current_thread.isRunning():
            return

        try:
            input_paths, flat_segments = self._build_flatten_export_segments()
        except Exception:
            input_paths, flat_segments = [], []
        if not input_paths:
            input_paths = [track.path for track in self._tracks if track.path]
        if not input_paths:
            input_paths = [self.input_path]

        if flat_segments:
            total_seconds = sum(float(seg.get("duration", 0.0) or 0.0) for seg in flat_segments)
            segment_count = len(flat_segments)
        else:
            keeps = self._collect_global_keeps()
            total_seconds = sum(max(0.0, float(seg.end) - float(seg.start)) for seg in keeps)
            segment_count = len(keeps)

        self.btn_export_advisor.setEnabled(False)
        self.btn_export_advisor_apply.setEnabled(False)
        self.btn_export_advisor_apply.setVisible(False)
        self._last_export_recommendation = None
        self.export_advisor_result.setText(
            "Benchmark in progress. Current controls remain unchanged..."
        )
        thread = QThread(self)
        worker = ExportAdvisorWorker(
            ffmpeg_path=self.ffmpeg_path,
            input_path=input_paths[0],
            requested_codec=str(self.codec_combo.currentData() or "auto"),
            total_seconds=float(total_seconds),
            segment_count=max(1, int(segment_count)),
            input_count=max(1, len(input_paths)),
        )
        self.export_advisor_thread = thread
        self.export_advisor_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_export_advisor_done, Qt.QueuedConnection)
        worker.error.connect(self._on_export_advisor_error, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_export_advisor_thread_finished)
        self._app_log(
            "export_advisor_start",
            inputs=len(input_paths),
            segments=segment_count,
            total_seconds=float(total_seconds),
            requested_codec=str(self.codec_combo.currentData() or "auto"),
        )
        thread.start()

    @Slot(object)
    def _on_export_advisor_done(self, recommendation: ExportRecommendation) -> None:
        self._last_export_recommendation = recommendation
        current_codec = str(self.codec_combo.currentData() or "auto")
        current_method = str(self.export_method_combo.currentData() or "auto")
        current_workers = int(self.parallel_workers_spin.value())
        current_chunks = int(self.chunk_count_spin.value())
        measured = ", ".join(
            f"{item.codec} {item.speed:.2f}x" for item in recommendation.benchmarks
        )
        self.export_advisor_result.setText(
            "Recommended (not applied): "
            f"codec={recommendation.codec}, method={recommendation.method}, "
            f"workers={recommendation.workers}, chunks={recommendation.chunks}.\n"
            f"Measured: {measured}.\n"
            f"Current: codec={current_codec}, method={current_method}, "
            f"workers={current_workers or 'Auto'}, chunks={current_chunks or 'Auto'}.\n"
            f"{recommendation.note}"
        )
        self.btn_export_advisor_apply.setVisible(True)
        self.btn_export_advisor_apply.setEnabled(True)
        self._app_log(
            "export_advisor_done",
            recommended_codec=recommendation.codec,
            recommended_workers=recommendation.workers,
            recommended_chunks=recommendation.chunks,
        )

    @Slot(str)
    def _on_export_advisor_error(self, message: str) -> None:
        self._last_export_recommendation = None
        self.btn_export_advisor_apply.setEnabled(False)
        self.btn_export_advisor_apply.setVisible(False)
        self.export_advisor_result.setText(f"Recommendation unavailable: {message}")
        self._app_log("export_advisor_error", message=str(message))

    @Slot()
    def _on_export_advisor_thread_finished(self) -> None:
        self.btn_export_advisor.setEnabled(True)
        self.export_advisor_thread = None
        self.export_advisor_worker = None

    def export_mp4(self):
        self._app_log("export_request_begin", has_input=bool(self.input_path), has_ffmpeg=bool(self.ffmpeg_path))
        if not self.input_path or not self.ffmpeg_path:
            QMessageBox.warning(self, "Export", "No input video loaded.")
            self._app_log("export_request_rejected", reason="no_input_or_ffmpeg")
            return

        settings = self._export_settings_from_ui()
        self._save_export_settings(settings)
        requested_settings = settings
        extension = settings.output_extension()
        if settings.output_mode == "per_clip":
            out_path = QFileDialog.getExistingDirectory(
                self,
                "Choose folder for exported clips",
                str(Path(self.input_path).parent),
            )
        else:
            format_filters = {
                ".mp4": "MP4 (*.mp4)",
                ".mkv": "Matroska (*.mkv)",
                ".mov": "QuickTime (*.mov)",
                ".webm": "WebM (*.webm)",
                ".m4a": "MPEG-4 Audio (*.m4a)",
                ".opus": "Opus Audio (*.opus)",
                ".wav": "Wave Audio (*.wav)",
            }
            out_path, _ = QFileDialog.getSaveFileName(
                self,
                "Export media",
                str(Path(self.input_path).with_name(f"{Path(self.input_path).stem}.cutted{extension}")),
                format_filters.get(extension, "Media files (*.*)"),
            )
        if not out_path:
            self._app_log("export_request_cancelled_save_dialog")
            return
        if settings.output_mode != "per_clip" and not str(out_path).lower().endswith(extension):
            out_path = f"{out_path}{extension}"

        self._export_abort_requested = False
        requested_codec = settings.codec
        codec_fallback_warning = ""
        try:
            codec_selection = resolve_video_codec(self.ffmpeg_path, requested_codec)
            codec = codec_selection.resolved
            if codec_selection.used_fallback:
                codec_fallback_warning = (
                    f"{requested_codec} is not usable on this computer; "
                    f"the export will use {codec}. {codec_selection.fallback_reason}"
                )
            self._app_log(
                "export_codec_resolved",
                requested=requested_codec,
                resolved=str(codec),
                fallback=bool(codec_selection.used_fallback),
            )
        except Exception as e:
            QMessageBox.critical(self, "Export codec unavailable", str(e))
            self._app_log("export_request_rejected", reason="no_usable_video_encoder", error=str(e))
            return
        if settings.container == "webm" and codec not in {
            "av1_amf", "av1_nvenc", "av1_qsv", "libaom-av1"
        }:
            QMessageBox.critical(
                self,
                "WebM requires AV1",
                "Select an AV1 encoder for WebM export. The current encoder resolved to "
                f"{codec}.",
            )
            self._app_log("export_request_rejected", reason="webm_requires_av1", codec=str(codec))
            return
        settings = replace(settings, codec=str(codec)).normalized()
        export_method = settings.method
        self._app_log(
            "export_start",
            output_path=str(out_path),
            codec=str(codec),
            export_method=str(export_method),
        )

        # "Auto" -> 0 (let ExportWorker decide based on CPU/GPU)
        pw = int(settings.parallel_workers)
        cc = int(settings.chunk_count)
        if cc < 0:
            cc = 0

        # Timeline loudness controls are combined with the delivery audio settings.
        normalize_lufs = bool(self.normalize_lufs.isChecked())
        lufs_target = float(self.lufs_target.value())
        limiter_on = bool(self.limiter.isChecked())
        cut_cfg = self.cut_quality_combo.currentData()
        if not isinstance(cut_cfg, dict):
            cut_cfg = {"enabled": self.cut_hq_enabled_default, "max_seconds": self.cut_hq_max_seconds_default}
        cut_hq_enabled = bool(cut_cfg.get("enabled", self.cut_hq_enabled_default))
        try:
            cut_hq_max_seconds = float(cut_cfg.get("max_seconds", self.cut_hq_max_seconds_default))
        except Exception:
            cut_hq_max_seconds = float(self.cut_hq_max_seconds_default)

        input_paths, flat_segments = self._build_flatten_export_segments()
        keeps_for_export: list[Segment] = []

        selected_range = settings.output_mode == "selected_range"
        if selected_range:
            if settings.range_end <= settings.range_start:
                QMessageBox.warning(self, "Export", "Set a valid timeline start and end range.")
                self._app_log("export_request_rejected", reason="invalid_selected_range")
                return
            if flat_segments:
                flat_segments = self._clip_flat_segments_to_range(
                    flat_segments,
                    settings.range_start,
                    settings.range_end,
                )
            settings = replace(settings, output_mode="single").normalized()

        # Fast-path: if flattened segments map linearly to a single input,
        # convert to legacy keeps (faster, uses preseek).
        if flat_segments:
            if settings.output_mode != "per_clip":
                fast_ok, fast_keeps = self._segments_to_legacy_keeps(input_paths, flat_segments)
                if fast_ok:
                    flat_segments = []
                    input_paths = [input_paths[0]] if input_paths else []
                    keeps_for_export = fast_keeps
                else:
                    keeps_for_export = self.keeps  # unused when segments provided
            else:
                keeps_for_export = self.keeps  # unused when segments provided
        else:
            input_paths = [t.path for t in self._tracks if t.path]
            if len(input_paths) > 1:
                if export_method == "chunked_parallel":
                    export_method = "filter_concat"
                keeps_for_export = self._collect_global_keeps()
            else:
                # Single-input projects can still have multiple timeline clips (split/duplicate/delete).
                # Export must use global timeline keeps, not only active-track keeps.
                keeps_for_export = self._collect_global_keeps()

        if selected_range and not flat_segments:
            keeps_for_export = self._clip_keeps_to_output_range(
                keeps_for_export,
                self.range_start_spin.value(),
                self.range_end_spin.value(),
            )

        if not flat_segments and not keeps_for_export:
            QMessageBox.warning(self, "Export", "Nothing to export (keeps is empty).")
            self._app_log("export_request_rejected", reason="empty_keeps")
            return

        expected_duration_s = 0.0
        try:
            if flat_segments:
                expected_duration_s = float(
                    sum(float(s.get("duration", 0.0) or 0.0) for s in flat_segments)
                )
            else:
                expected_duration_s = float(
                    sum(max(0.0, float(k.end) - float(k.start)) for k in (keeps_for_export or []))
                )
        except Exception:
            expected_duration_s = 0.0

        if not self._run_export_preflight(
            output_path=str(out_path),
            codec=str(codec),
            expected_duration_s=float(expected_duration_s),
            input_paths=list(input_paths or []),
            initial_warnings=[codec_fallback_warning] if codec_fallback_warning else None,
        ):
            self._app_log("export_request_rejected", reason="preflight_failed")
            return

        try:
            keeps_total = float(sum(max(0.0, float(k.end) - float(k.start)) for k in (keeps_for_export or [])))
        except Exception:
            keeps_total = 0.0
        try:
            seg_total = float(sum(float(s.get("duration", 0.0) or 0.0) for s in (flat_segments or [])))
        except Exception:
            seg_total = 0.0
        try:
            self._on_export_detail(
                f"ui_export_plan tracks={len(self._tracks)} active_track={int(self._active_track_index) + 1} "
                f"inputs={len(input_paths)} segments={len(flat_segments)} segments_total={seg_total:.3f}s "
                f"keeps={len(keeps_for_export)} keeps_total={keeps_total:.3f}s "
                f"cut_hq={'on' if cut_hq_enabled else 'off'} cut_hq_max={cut_hq_max_seconds:.1f}s"
            )
        except Exception:
            pass
        self._export_start_monotonic = time.monotonic()
        self._export_last_detail_ts = float(self._export_start_monotonic)

        self.btn_export.setEnabled(False)
        self.export_progress.setValue(0)
        self.export_progress.setFormat("0%")
        self.export_status.setText("Starting export...")
        try:
            self.export_details.clear()
            self.btn_export_details.setChecked(False)
            self.export_details.setVisible(False)
            self.btn_export_details.setText("Show logs")
            if hasattr(self, "export_stats_block") and self.export_stats_block is not None:
                self.export_stats_block.setVisible(True)
            self._last_export_detail_hint = ""
        except Exception:
            pass
        self.statusBar().showMessage("Exporting...")
        self._set_stage("export")
        self._set_export_dot_state("export")
        self._set_export_processing(True)
        self._web_js(self.web_topbar, "uiSetProgress(0);")

        try:
            self.ex_thread = QThread(self)
            self.ex_worker = ExportWorker(
                ffmpeg_path=self.ffmpeg_path,
                input_path=self.input_path,
                output_path=out_path,
                keeps=keeps_for_export,
                codec=str(codec),
                use_hwaccel=bool(self.hwaccel_cb.isChecked()),
                export_method=str(export_method),
                parallel_workers=int(pw),
                chunk_count=int(cc),
                audio_gain_db=float(self.gain_db.value()),
                normalize_lufs=normalize_lufs,
                lufs_target=lufs_target,
                apply_limiter=limiter_on,
                cut_hq_enabled=cut_hq_enabled,
                cut_hq_max_seconds=cut_hq_max_seconds,
                input_paths=input_paths if len(input_paths) > 0 else None,
                segments=flat_segments if flat_segments else None,
                export_settings=settings,
                requested_export_settings=requested_settings,
            )
            self.ex_worker.moveToThread(self.ex_thread)

            self.ex_thread.started.connect(self.ex_worker.run)
            self.ex_worker.progress.connect(self._on_export_progress)
            self.ex_worker.detail.connect(self._on_export_detail)
            self.ex_worker.finished.connect(self._on_export_done)
            self.ex_worker.error.connect(self._on_export_error)
            self.ex_worker.finished.connect(self.ex_thread.quit)
            self.ex_worker.finished.connect(self.ex_worker.deleteLater)
            self.ex_thread.finished.connect(self.ex_thread.deleteLater)
            self.ex_worker.error.connect(self.ex_thread.quit)
            self.ex_worker.error.connect(self.ex_worker.deleteLater)
            self.ex_thread.started.connect(lambda: self._on_export_detail("ui_export_thread_started_signal"))
            self.ex_thread.finished.connect(lambda: self._on_export_detail("ui_export_thread_finished_signal"))

            self._on_export_detail("ui_export_worker_setup ok")
            self._on_export_detail("ui_export_thread_start")
            self.ex_thread.start()
            QTimer.singleShot(1800, self._check_export_thread_started)
            QTimer.singleShot(12000, self._check_export_liveness)
        except Exception as e:
            self._app_log("export_thread_setup_failed", error=str(e))
            self._on_export_error(f"Export failed to start: {e}")
            return

    @staticmethod
    def _clip_flat_segments_to_range(
        segments: list[dict],
        range_start: float,
        range_end: float,
    ) -> list[dict]:
        start = max(0.0, float(range_start))
        end = max(start, float(range_end))
        clipped: list[dict] = []
        cursor = 0.0
        for source in segments:
            seg_start = float(source.get("start", 0.0) or 0.0)
            seg_end = float(source.get("end", seg_start) or seg_start)
            left = max(start, seg_start)
            right = min(end, seg_end)
            if right <= left + 1e-9:
                continue
            head = left - seg_start
            duration = right - left
            item = dict(source)
            for prefix in ("v", "a"):
                in_key = f"{prefix}_in"
                out_key = f"{prefix}_out"
                if item.get(f"{prefix}_idx", None) is not None:
                    source_in = float(item.get(in_key, 0.0) or 0.0) + head
                    item[in_key] = source_in
                    item[out_key] = source_in + duration
            item["start"] = cursor
            item["end"] = cursor + duration
            item["duration"] = duration
            clipped.append(item)
            cursor += duration
        return clipped

    @staticmethod
    def _clip_keeps_to_output_range(
        keeps: list[Segment],
        range_start: float,
        range_end: float,
    ) -> list[Segment]:
        start = max(0.0, float(range_start))
        end = max(start, float(range_end))
        clipped: list[Segment] = []
        cursor = 0.0
        for keep in keeps:
            duration = max(0.0, float(keep.end) - float(keep.start))
            timeline_end = cursor + duration
            left = max(start, cursor)
            right = min(end, timeline_end)
            if right > left + 1e-9:
                source_start = float(keep.start) + (left - cursor)
                clipped.append(Segment(source_start, source_start + (right - left)))
            cursor = timeline_end
        return clipped

    def _segments_to_legacy_keeps(
        self,
        input_paths: list[str],
        segments: list[dict],
    ) -> tuple[bool, list[Segment]]:
        """
        Return (ok, keeps) if flattened segments can be reduced to a single-input
        linear keep list (fast legacy path). Otherwise (False, []).
        """
        if not segments:
            return False, []
        if len(input_paths) != 1:
            return False, []

        eps = 1e-3
        base_offset = None
        last_v_in = None
        keeps: list[Segment] = []

        for s in segments:
            try:
                dur = float(s.get("duration", 0.0) or 0.0)
            except Exception:
                dur = 0.0
            if dur <= eps:
                continue

            v_idx = s.get("v_idx", None)
            a_idx = s.get("a_idx", None)
            if v_idx is None or a_idx is None:
                return False, []
            try:
                v_idx = int(v_idx)
                a_idx = int(a_idx)
            except Exception:
                return False, []
            if v_idx != 0 or a_idx != 0:
                return False, []

            try:
                v_in = float(s.get("v_in", 0.0) or 0.0)
                v_out = float(s.get("v_out", 0.0) or 0.0)
                a_in = float(s.get("a_in", 0.0) or 0.0)
                a_out = float(s.get("a_out", 0.0) or 0.0)
                t0 = float(s.get("start", 0.0) or 0.0)
            except Exception:
                return False, []

            if v_out <= v_in + eps or a_out <= a_in + eps:
                return False, []
            if abs((v_out - v_in) - dur) > 5e-3:
                return False, []
            if abs((a_out - a_in) - dur) > 5e-3:
                return False, []

            offset = v_in - t0
            if base_offset is None:
                base_offset = offset
            elif abs(offset - base_offset) > 5e-3:
                return False, []

            if last_v_in is not None and v_in < last_v_in - 5e-3:
                return False, []
            last_v_in = v_in

            keeps.append(Segment(v_in, v_out))

        if not keeps:
            return False, []

        # Merge overlaps to keep a clean list
        keeps = merge_overlaps(sorted(keeps, key=lambda k: k.start))
        return True, keeps

    def _selected_video_encoder(self, codec_value: str) -> str:
        c = str(codec_value or "").strip().lower()
        if c in {
            "h264_amf", "h264_nvenc", "h264_qsv",
            "hevc_amf", "hevc_nvenc", "hevc_qsv",
            "av1_amf", "av1_nvenc", "av1_qsv",
            "libx264", "libx265", "libaom-av1",
        }:
            return c
        return "libx264"

    def _ffmpeg_has_encoder(self, encoder: str) -> bool:
        ffmpeg = str(getattr(self, "ffmpeg_path", "") or "").strip()
        if not ffmpeg or not Path(ffmpeg).exists():
            return False
        try:
            p = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-h", f"encoder={encoder}"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return int(p.returncode) == 0
        except Exception:
            return False

    def _estimate_export_size_bytes(self, *, expected_duration_s: float, input_paths: list[str]) -> int:
        paths = [str(p) for p in (input_paths or []) if str(p).strip()]
        if not paths and self.input_path:
            paths = [str(self.input_path)]

        total_input_bytes = 0
        total_input_duration = 0.0
        for p in paths:
            try:
                fp = Path(p)
                if not fp.exists():
                    continue
                total_input_bytes += int(fp.stat().st_size)
                d = float(ffprobe_duration_seconds(str(fp)) or 0.0)
                if d > 0.0:
                    total_input_duration += d
            except Exception:
                continue

        if total_input_bytes <= 0 or total_input_duration <= 0.5 or expected_duration_s <= 0.5:
            return int(1.2 * 1024 * 1024 * 1024)  # conservative fallback: ~1.2 GB

        avg_bps = float(total_input_bytes) / max(1.0, float(total_input_duration))
        estimated = avg_bps * max(1.0, float(expected_duration_s))
        # headroom for muxing/temp/write amplification
        estimated = estimated * 1.45 + (220 * 1024 * 1024)
        return int(max(300 * 1024 * 1024, estimated))

    def _run_export_preflight(
        self,
        *,
        output_path: str,
        codec: str,
        expected_duration_s: float,
        input_paths: list[str],
        initial_warnings: list[str] | None = None,
    ) -> bool:
        errors: list[str] = []
        warnings: list[str] = [str(item) for item in (initial_warnings or []) if str(item).strip()]

        # 1) ffmpeg / ffprobe
        ffmpeg = str(getattr(self, "ffmpeg_path", "") or "").strip()
        ffprobe = str(getattr(self, "ffprobe_path", "") or "").strip()
        if not ffmpeg or not Path(ffmpeg).exists():
            try:
                self.ffmpeg_path, self.ffprobe_path = ensure_ffmpeg()
                ffmpeg = str(self.ffmpeg_path or "")
                ffprobe = str(self.ffprobe_path or "")
            except Exception as e:
                errors.append(f"FFmpeg is missing: {e}")
        if not ffprobe or not Path(ffprobe).exists():
            errors.append("FFprobe is missing.")

        # 2) selected codec availability
        encoder = self._selected_video_encoder(codec)
        if ffmpeg and not self._ffmpeg_has_encoder(encoder):
            errors.append(f"Selected codec is not available in FFmpeg: {encoder}")

        # 3) disk space
        try:
            out_dir = Path(output_path).resolve().parent
            out_dir.mkdir(parents=True, exist_ok=True)
            free_bytes = int(shutil.disk_usage(str(out_dir)).free)
            required_bytes = self._estimate_export_size_bytes(
                expected_duration_s=float(expected_duration_s),
                input_paths=input_paths,
            )
            if free_bytes < required_bytes:
                errors.append(
                    "Low disk space for export "
                    f"(required ~{required_bytes / (1024**3):.2f} GB, free {free_bytes / (1024**3):.2f} GB)."
                )
            elif free_bytes < int(required_bytes * 1.35):
                warnings.append(
                    "Disk space is tight for this export "
                    f"(estimated ~{required_bytes / (1024**3):.2f} GB, free {free_bytes / (1024**3):.2f} GB)."
                )
        except Exception as e:
            warnings.append(f"Disk space check skipped: {e}")

        # 4) AI dependencies (relevant when AI mode is active)
        if str(getattr(self, "analysis_mode", "classic") or "classic").strip().lower() == "ai":
            deps_ok, reason = ai_dependency_status()
            if not deps_ok:
                warnings.append(f"AI dependencies are not fully available: {reason}")

        if errors:
            text = "Export preflight failed:\n\n- " + "\n- ".join(errors)
            if warnings:
                text += "\n\nWarnings:\n- " + "\n- ".join(warnings)
            QMessageBox.critical(self, "Export preflight failed", text)
            self._app_log("export_preflight_failed", errors=" | ".join(errors), warnings=" | ".join(warnings))
            return False

        if warnings:
            text = "Export preflight warnings:\n\n- " + "\n- ".join(warnings) + "\n\nContinue export?"
            choice = QMessageBox.question(
                self,
                "Export preflight warnings",
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if choice != QMessageBox.Yes:
                self._app_log("export_preflight_cancelled_on_warning", warnings=" | ".join(warnings))
                return False

        self._app_log(
            "export_preflight_ok",
            codec=str(codec),
            encoder=str(encoder),
            expected_duration=float(expected_duration_s),
            inputs=len(input_paths or []),
        )
        return True

    def _on_export_progress(self, pct: int, text: str):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        try:
            pct = int(pct)
        except Exception:
            pct = 0
        pct = max(0, min(100, pct))
        self.export_progress.setValue(pct)
        try:
            self.export_progress.setFormat(f"{pct}%")
        except Exception:
            pass
        self.export_status.setText(text)
        self._web_js(self.web_topbar, f"uiSetProgress({int(pct)});")

    def _toggle_export_details(self) -> None:
        try:
            visible = bool(self.btn_export_details.isChecked())
        except Exception:
            visible = False
        self._app_log("export_logs_visibility", visible=visible)
        self.export_details.setVisible(visible)
        try:
            if hasattr(self, "export_stats_block") and self.export_stats_block is not None:
                self.export_stats_block.setVisible(not visible)
        except Exception:
            pass
        try:
            self.export_details.raise_()
        except Exception:
            pass
        self.btn_export_details.setText("Hide logs" if visible else "Show logs")

    def _export_logs_dir(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base:
            base = str(Path.home() / ".auto_cutter")
        d = Path(base) / "export_logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _copy_export_logs(self) -> None:
        self._app_log("export_logs_copy_click")
        try:
            txt = self.export_details.toPlainText().strip()
        except Exception:
            txt = ""
        if not txt:
            self.statusBar().showMessage("No export logs to copy.", 2500)
            return
        try:
            QApplication.clipboard().setText(txt)
            self.statusBar().showMessage("Export logs copied.", 2500)
        except Exception:
            self.statusBar().showMessage("Failed to copy export logs.", 2500)

    def _open_export_logs_folder(self) -> None:
        self._app_log("export_logs_open_folder_click")
        try:
            d = self._export_logs_dir()
            txt = self.export_details.toPlainText().strip()
            if txt:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                (d / f"export_{stamp}.log").write_text(txt, encoding="utf-8")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
            self.statusBar().showMessage("Opened export logs folder.", 2500)
        except Exception as e:
            QMessageBox.warning(self, "Export logs", f"Failed to open export logs folder:\n{e}")

    def _export_detail_hint(self, line: str) -> str | None:
        s = (line or "").strip().lower()
        if not s:
            return None
        if s.startswith("checklist context="):
            return line
        if s.startswith("checklist decision"):
            return line
        if s.startswith("checklist step="):
            return line
        if s.startswith("keyframes_scan start"):
            return "step=keyframes_scan_start"
        if s.startswith("keyframes_scan done"):
            return "step=keyframes_scan_done"
        if s.startswith("chunk_plan "):
            return line
        if s.startswith("chunk_start "):
            return line
        if s.startswith("final_concat start"):
            return line
        if s.startswith("final_concat done"):
            return line
        if s.startswith("remux_audio_policy "):
            return line
        if s.startswith("export_summary "):
            return line
        return None

    def _on_export_detail(self, msg: str) -> None:
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            return
        if msg is None:
            return
        line = str(msg).strip()
        if not line:
            return
        try:
            self._export_last_detail_ts = float(time.monotonic())
        except Exception:
            pass
        try:
            ts = time.strftime("%H:%M:%S")
            self.export_details.appendPlainText(f"[{ts}] {line}")
            hint = self._export_detail_hint(line)
            if hint and hint != self._last_export_detail_hint:
                self._last_export_detail_hint = hint
                self.export_details.appendPlainText(f"[{ts}] >>> {hint}")
                try:
                    self._app_log("export_step", hint=str(hint))
                except Exception:
                    pass
        except Exception:
            try:
                self.export_details.appendPlainText(line)
            except Exception:
                pass

    def _on_export_done(self):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            self._set_export_processing(False)
            self._export_abort_requested = False
            self._export_last_detail_ts = None
            self._export_start_monotonic = None
            self.ex_thread = None
            self.ex_worker = None
            return
        self._app_log("export_done")
        self.export_progress.setValue(100)
        self.export_progress.setFormat("100%")
        self.export_status.setText("Export complete.")
        self._web_js(self.web_topbar, "uiSetProgress(100);")
        self.btn_export.setEnabled(True)
        self.btn_export_edl.setEnabled(True)
        self.statusBar().showMessage("Export complete.", 5000)
        self._set_stage("review")
        self._set_export_dot_state("done")
        self._set_export_processing(False)
        self._export_abort_requested = False
        self._export_last_detail_ts = None
        self._export_start_monotonic = None
        self.ex_thread = None
        self.ex_worker = None

    def _on_export_error(self, msg: str):
        if bool(getattr(self, "_workspace_resetting", False)) or bool(getattr(self, "_pending_workspace_reset", False)):
            self._set_export_processing(False)
            self._export_abort_requested = False
            self._export_last_detail_ts = None
            self._export_start_monotonic = None
            self.ex_thread = None
            self.ex_worker = None
            return
        self._app_log(
            "export_error",
            aborted=bool(getattr(self, "_export_abort_requested", False)),
            message=str(msg),
        )
        if getattr(self, "_export_abort_requested", False) or "cancel" in str(msg).lower():
            self.export_status.setText("Export cancelled.")
        else:
            QMessageBox.critical(self, "Export failed", msg)
            self.export_status.setText("Export failed.")
        self._web_js(self.web_topbar, "uiSetProgress(0);")
        self.btn_export.setEnabled(True)
        self.btn_export_edl.setEnabled(True)
        self.statusBar().clearMessage()
        self._set_stage("review")
        self._set_export_dot_state("idle")
        self._set_export_processing(False)
        self._export_abort_requested = False
        self._export_last_detail_ts = None
        self._export_start_monotonic = None
        self.ex_thread = None
        self.ex_worker = None

    def _check_export_thread_started(self) -> None:
        """
        Guard against silent startup failures where UI stays at 0% with no worker logs.
        """
        if not bool(getattr(self, "_export_processing", False)):
            return
        try:
            th = getattr(self, "ex_thread", None)
            running = bool(th is not None and th.isRunning())
        except Exception:
            running = False
        if running:
            return
        self._app_log("export_thread_not_running_after_start")
        self._on_export_error("Export failed to start (worker thread did not start).")

    def _check_export_liveness(self) -> None:
        """
        Runtime liveness watchdog:
        - if export thread is alive but no details are received for too long, abort gracefully
        - avoids infinite 0% stalls on problematic systems/drivers
        """
        if not bool(getattr(self, "_export_processing", False)):
            return
        try:
            th = getattr(self, "ex_thread", None)
            running = bool(th is not None and th.isRunning())
        except Exception:
            running = False
        if not running:
            return

        try:
            last_ts = float(getattr(self, "_export_last_detail_ts", time.monotonic()) or time.monotonic())
        except Exception:
            last_ts = time.monotonic()
        idle_s = max(0.0, float(time.monotonic() - last_ts))
        try:
            pct = int(self.export_progress.value())
        except Exception:
            pct = 0

        if idle_s >= 45.0 and pct <= 0:
            self._on_export_detail(
                f"ui_export_watchdog notice idle={idle_s:.1f}s pct={pct}% "
                "waiting for worker/ffmpeg progress"
            )

        # Hard timeout is intentionally conservative; if we reach it, the process
        # is very likely stalled on this machine.
        if idle_s >= 300.0:
            self._app_log("export_watchdog_abort", idle_seconds=idle_s, pct=pct)
            self._abort_export(wait_ms=1200)
            self._on_export_error(
                "Export appears stalled (no progress logs for over 5 minutes). "
                "Please retry export."
            )
            return

        QTimer.singleShot(12000, self._check_export_liveness)

    def _background_qthreads_running(self) -> bool:
        threads = [getattr(self, "ex_thread", None), getattr(self, "export_advisor_thread", None)]
        for name in ("_analysis_threads", "_ai_threads"):
            threads.extend(getattr(self, name, {}).values())
        for name in ("_orphan_analysis_threads", "_orphan_ai_threads"):
            threads.extend(getattr(self, name, []))
        for thread in threads:
            try:
                if thread is not None and thread.isRunning():
                    return True
            except RuntimeError:
                # Qt may already have deleted a finished thread.
                continue
        return False

    def _abort_export(self, wait_ms: int = 1500) -> None:
        self._app_log("export_abort_requested")
        try:
            if getattr(self, "ex_worker", None) is not None:
                self._export_abort_requested = True
                try:
                    self.ex_worker.cancel()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if getattr(self, "ex_thread", None) is not None and self.ex_thread.isRunning():
                self.ex_thread.quit()
                try:
                    ms = max(0, int(wait_ms))
                except Exception:
                    ms = 0
                if ms > 0:
                    self.ex_thread.wait(ms)
        except Exception:
            pass
        try:
            th = getattr(self, "ex_thread", None)
            if th is None or (not th.isRunning()):
                self.ex_thread = None
                self.ex_worker = None
                if bool(getattr(self, "_export_processing", False)):
                    self._set_export_processing(False)
        except Exception:
            pass
