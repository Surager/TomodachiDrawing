import argparse
import atexit
import json
import logging
import multiprocessing
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template_string, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler
from werkzeug.utils import secure_filename

from .main import generate_pixel_commands
from .main_brush import generate_commands as generate_brush_commands
from .nxbt_path import import_nxbt
from .sequence_preview import render_macro_preview
from .tomodachi_common import (
    build_color_layers,
    collapse_macro_loop_blocks,
    color_key_to_rgb,
    dump_point_layers,
    flatten_macro_lines,
    fmt_seconds,
    load_quantized_image,
    save_quantized_preview,
)


# Jobs and uploads stay under the repository root (`output/webui/`), not inside the installed package.
_REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI_ROOT = _REPO_ROOT / "output" / "webui"
JOBS_ROOT = WEBUI_ROOT / "jobs"
LOGGER = logging.getLogger("tomodachi_webui")
VALID_BUTTONS = (
    "Y",
    "X",
    "B",
    "A",
    "JCL_SR",
    "JCL_SL",
    "R",
    "ZR",
    "MINUS",
    "PLUS",
    "R_STICK_PRESS",
    "L_STICK_PRESS",
    "HOME",
    "CAPTURE",
    "DPAD_DOWN",
    "DPAD_UP",
    "DPAD_RIGHT",
    "DPAD_LEFT",
    "JCR_SR",
    "JCR_SL",
    "L",
    "ZL",
)
MACRO_POLL_SECONDS = 1 / 120
MACRO_STOP_TIMEOUT_SECONDS = 1.0
MACRO_CHUNK_SIZE = 5000
STABILITY_MAX_PAIRS = 50_000


def macro_line_holds_physical_input(line):
    """True if a flattened macro line drives buttons or sticks (not wait-only / comment)."""
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    first = s.split()[0]
    if first in VALID_BUTTONS:
        return True
    if first.startswith("L_STICK@") or first.startswith("R_STICK@"):
        return True
    return False


def chunk_slice_end(lines, chunk_start, hard_end):
    """Shrink hard_end so the last line is not input-holding when possible (nxbt chunk gap safety)."""
    chunk_end = hard_end
    while chunk_end > chunk_start and macro_line_holds_physical_input(lines[chunk_end - 1]):
        chunk_end -= 1
    if chunk_end == chunk_start:
        chunk_end = min(chunk_start + 1, hard_end)
    return chunk_end


def resolve_job_directory(sequence_id):
    """Return resolved job directory path, or None if ID is unsafe or folder missing."""
    if not sequence_id or not isinstance(sequence_id, str):
        return None
    if sequence_id.strip() != sequence_id:
        return None
    if any(sep in sequence_id for sep in ("/", "\\", os.sep)):
        return None
    root = JOBS_ROOT.resolve()
    candidate = (JOBS_ROOT / sequence_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


class WerkzeugStatusPollFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return "/api/status" not in message


class QuietStatusRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        path = self.path.split("?", 1)[0]
        if path == "/api/status" and str(code) == "200":
            return
        super().log_request(code, size)


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("werkzeug").addFilter(WerkzeugStatusPollFilter())



def configure_multiprocessing():
    if os.name != "posix":
        return
    if "fork" not in multiprocessing.get_all_start_methods():
        return

    current = multiprocessing.get_start_method(allow_none=True)
    if current == "fork":
        return

    multiprocessing.set_start_method("fork", force=True)
    if current is None:
        LOGGER.info("Configured multiprocessing start method: fork")
    else:
        LOGGER.warning(
            "Changed multiprocessing start method from %s to fork for nxbt compatibility",
            current,
        )


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_exception(context, exc):
    LOGGER.exception("%s: %s", context, exc)


class ControllerService:
    def __init__(self):
        self.lock = threading.RLock()
        self.macro_lock = threading.Lock()
        self.nx = None
        self.controller_index = None
        self.status = "idle"
        self.message = "未创建虚拟手柄"
        self.error = None
        self.connected_at = None
        self.shutdown_done = False
        self.connection_generation = 0

    def snapshot(self):
        with self.lock:
            return {
                "status": self.status,
                "message": self.message,
                "error": self.error,
                "connected_at": self.connected_at,
                "controller_index": self.controller_index,
            }

    def start_connect(self):
        with self.lock:
            if self.status in ("starting", "waiting", "connected"):
                return self.snapshot()
            self.shutdown_done = False
            self.connection_generation += 1
            generation = self.connection_generation
            self.status = "starting"
            self.message = "正在创建虚拟 Pro Controller"
            self.error = None
            self.connected_at = None

        thread = threading.Thread(target=self._connect_worker, args=(generation,), daemon=True)
        thread.start()
        return self.snapshot()

    def reconnect(self):
        self.shutdown(message="正在重新连接，已请求清理旧虚拟手柄")
        return self.start_connect()

    def is_active_generation(self, generation):
        with self.lock:
            return generation == self.connection_generation and not self.shutdown_done

    def _connect_worker(self, generation):
        nx = None
        controller_index = None
        try:
            LOGGER.info("Starting virtual Pro Controller")
            configure_multiprocessing()
            nxbt = import_nxbt()
            nx = nxbt.Nxbt()
            controller_index = nx.create_controller(nxbt.PRO_CONTROLLER)
            if not self.is_active_generation(generation):
                self.cleanup_nx(nx, controller_index, "stale connection worker")
                return
            with self.lock:
                if generation != self.connection_generation or self.shutdown_done:
                    self.cleanup_nx(nx, controller_index, "stale connection worker")
                    return
                self.nx = nx
                self.controller_index = controller_index
                self.status = "waiting"
                self.message = "请在 Switch 的更改握法/顺序页面连接这个虚拟手柄"
            LOGGER.info("Virtual controller created, waiting for Switch connection")
            nx.wait_for_connection(controller_index)
            with self.lock:
                if generation != self.connection_generation or self.shutdown_done:
                    return
                self.status = "connected"
                self.message = "虚拟手柄已连接"
                self.connected_at = now_text()
            LOGGER.info("Virtual controller connected")
        except Exception as exc:
            log_exception("Virtual controller connection failed", exc)
            with self.lock:
                if generation == self.connection_generation and not self.shutdown_done:
                    self.status = "error"
                    self.error = str(exc)
                    self.message = "虚拟手柄连接失败"

    def require_connected(self):
        with self.lock:
            if self.status != "connected" or self.nx is None or self.controller_index is None:
                raise RuntimeError("虚拟手柄尚未连接")
            return self.nx, self.controller_index

    def run_macro(self, macro):
        nx, controller_index = self.require_connected()
        with self.macro_lock:
            nx.macro(controller_index, macro)

    def cleanup_nx(self, nx, controller_index, context):
        if nx is None:
            return

        LOGGER.info("Cleaning up nxbt controller resources: %s", context)
        if controller_index is not None:
            try:
                nx.clear_macros(controller_index)
            except Exception as exc:
                LOGGER.warning("Failed to clear nxbt macros during cleanup: %s", exc)
            try:
                nx.remove_controller(controller_index)
            except Exception as exc:
                LOGGER.warning("Failed to remove nxbt controller during cleanup: %s", exc)

        try:
            exit_handler = getattr(nx, "_on_exit", None)
            if exit_handler is not None:
                exit_handler()
                try:
                    atexit.unregister(exit_handler)
                except Exception:
                    pass
            elif hasattr(nx, "controllers") and nx.controllers.is_alive():
                nx.controllers.terminate()
                nx.controllers.join(timeout=2)
        except Exception as exc:
            LOGGER.warning("Failed to run nxbt exit cleanup: %s", exc)

        try:
            resource_manager = getattr(nx, "resource_manager", None)
            if resource_manager is not None:
                resource_manager.shutdown()
        except Exception as exc:
            LOGGER.warning("Failed to shutdown nxbt resource manager: %s", exc)

        LOGGER.info("nxbt cleanup finished")

    def shutdown(self, message="WebUI 正在退出，已请求清理虚拟手柄"):
        with self.lock:
            if self.shutdown_done:
                return
            self.shutdown_done = True
            self.connection_generation += 1
            nx = self.nx
            controller_index = self.controller_index
            self.status = "idle"
            self.message = message
            self.nx = None
            self.controller_index = None
            self.connected_at = None

        self.cleanup_nx(nx, controller_index, "shutdown")


class DrawState:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = {
            "status": "idle",
            "sequence_id": None,
            "sent_lines": 0,
            "total_lines": 0,
            "percent": 0,
            "lines_per_second": 0.0,
            "eta_seconds": None,
            "message": "未开始绘画",
            "error": None,
            "started_at": None,
            "finished_at": None,
            "pause_requested": False,
            "cancel_requested": False,
            "benchmark": None,
        }

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def update(self, **kwargs):
        with self.lock:
            self.state.update(kwargs)

    def is_running(self):
        with self.lock:
            return self.state["status"] == "running"

    def is_busy(self):
        with self.lock:
            return self.state["status"] in ("running", "paused", "stopping")

    def is_paused(self):
        with self.lock:
            return self.state["pause_requested"]

    def is_pause_requested(self):
        with self.lock:
            return self.state["pause_requested"]

    def is_cancel_requested(self):
        with self.lock:
            return self.state["cancel_requested"]

    def request_pause(self):
        with self.lock:
            if self.state["status"] != "running":
                return False
            self.state["pause_requested"] = True
            self.state["status"] = "paused"
            self.state["message"] = "已暂停，后续按键未发送"
            return True

    def resume(self):
        with self.lock:
            if self.state["status"] != "paused":
                return False
            self.state["pause_requested"] = False
            self.state["status"] = "running"
            self.state["message"] = "继续发送绘画按键序列"
            return True

    def request_cancel(self):
        with self.lock:
            if self.state["status"] not in ("running", "paused"):
                return False
            self.state["cancel_requested"] = True
            self.state["pause_requested"] = False
            self.state["status"] = "stopping"
            self.state["message"] = "正在终止绘画，停止当前按键"
            return True

    def wait_while_paused(self):
        waited = False
        while self.is_pause_requested() and not self.is_cancel_requested():
            waited = True
            time.sleep(0.05)
        return waited


controller = ControllerService()
draw_state = DrawState()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

# Web UI strings (zh default + en). Embedded in the page for instant switching.
I18N_CATALOG = {
    "zh": {
        "lang_label": "语言",
        "page_title": "TomodachiDrawing WebUI",
        "header_title": "TomodachiDrawing WebUI",
        "seq_count_label": "已生成按键序列：",
        "section_controller": "虚拟手柄",
        "section_buttons": "发送 Switch 按键",
        "section_upload": "上传图片并生成序列",
        "section_stability": "稳定性测试序列",
        "section_sequences": "按键序列",
        "detail_pick": "请选择一个条目",
        "h_layers": "颜色层列表",
        "status_loading": "读取中",
        "btn_connect": "等待主机连接虚拟手柄",
        "btn_reconnect": "重新连接手柄",
        "btn_lr": "发送 L+R",
        "lbl_image": "图片文件，尺寸必须为 256x256",
        "lbl_mode": "生成模式",
        "mode_brush": "brush：优先使用大笔刷",
        "mode_pixel": "pixel：逐像素绘制",
        "lbl_press": "按下时长",
        "lbl_wait": "等待时长",
        "lbl_min_gain": "brush 最小收益",
        "lbl_merge": "合并相近颜色阈值",
        "label_return_home": "每个颜色层结束后回到 (0,0) 并把颜色归到底部",
        "btn_generate": "生成按键序列",
        "stability_intro": "生成大量 <code>DPAD_RIGHT</code> 与 <code>A</code>（按下时长 + 间隔与绘画宏相同），用于在真机上摸索最合适的间隔。生成后选中该条目，使用「绘制整图」发送到 Switch。",
        "lbl_stability_pairs": "循环次数（一次 = 先 RIGHT 再 A）",
        "lbl_stability_wait": "间隔时长",
        "btn_stability": "生成稳定性测试",
        "btn_draw_whole": "绘制整图",
        "btn_draw_layer": "绘制当前颜色层",
        "btn_draw_from_layer": "从当前颜色层开始绘制",
        "btn_delete": "删除此序列",
        "btn_pause": "暂停",
        "btn_resume": "继续",
        "btn_stop": "终止",
        "link_macro": "查看 macro.txt",
        "link_layer_macro": "查看颜色层 macro.txt",
        "cap_upload": "上传图片",
        "cap_quantized": "量化预览",
        "cap_sequence": "按键序列回放预览",
        "cap_layer": "当前颜色层预览",
        "alt_upload": "上传图片预览",
        "alt_quantized": "量化预览",
        "alt_sequence": "按键序列回放预览",
        "alt_layer": "当前颜色层预览",
        "list_empty": "还没有生成按键序列",
        "detail_empty": "请选择一个条目",
        "layer_none": "该条目暂无颜色层拆分数据",
        "lines_unit": "行",
        "pixels_unit": "像素",
        "lines_per_sec": "行/秒",
        "eta_label": "剩余",
        "status_idle": "未创建虚拟手柄",
        "status_starting": "正在创建虚拟手柄",
        "status_waiting": "等待 Switch 连接",
        "status_connected": "已连接",
        "status_error": "连接错误",
        "poll_failed": "状态读取失败",
        "meta_mode": "模式",
        "meta_pairs": "循环次数",
        "meta_press": "按下时长",
        "meta_wait": "间隔时长",
        "meta_macro_lines": "宏行数",
        "meta_colors": "颜色层",
        "meta_merge": "合并阈值",
        "meta_return_home": "层后归零点",
        "meta_gen_total": "生成总耗时",
        "on": "开启",
        "off": "关闭",
        "mode_stability": "稳定性测试",
        "bench_stab": "宏写入 {mw}s · 占位图 {ph}s",
        "bench_full": "读图 {ing}s · 量化分层 {qu}s · 侧车 {du}s · 整图宏 {fmg}s · 整图预览 {fpv}s · 按层 {ll}s（宏 {lmg}s / 写文件与层预览 {lo}s）",
        "draw_bench": "绘画 {outcome} · 总耗时 {total}s · {prep}读取宏 {read}s · 等待手柄 {cw}s · 宏发送 {macro}s · 暂停 {pause}s · 分块 {chunks} · 实际 {sent}/{tot} 行 · 平均 {lps} 行/秒",
        "bench_prep": "生成增量宏 {s}s · ",
        "bench_prep_empty": "",
        "outcome_done": "完成",
        "outcome_cancelled": "已终止",
        "outcome_error": "出错",
        "upload_generating": "正在生成，请稍候",
        "upload_done_lines": "已生成 {n} 行按键序列",
        "upload_done_bench": "已生成 {n} 行按键序列（总耗时 {t}s）",
        "stab_generating": "正在生成…",
        "stab_done": "已生成 {n} 行",
        "stab_done_bench": "已生成 {n} 行（总耗时 {t}s），可选中后「绘制整图」发送",
        "api_fallback": "请求失败",
        "gen_fallback": "生成失败",
        "confirm_from_layer": "将从颜色层 {layer} 开始绘制至最后（共 {n} 层）。\n首次发起需要重新生成宏，可能耗时几秒到十几秒。继续？",
        "confirm_delete": "确定删除序列「{id}」？本地文件夹将一并删除，不可恢复。",
        "draw_msg_sending_line": "正在发送第 {a}/{b} 行",
        "draw_stop_near": "已终止在第 {a}/{b} 行附近，后续按键未发送",
        "draw_pause_near": "已暂停在第 {a}/{b} 行附近，后续按键未发送",
        "draw_from_layer_hsv": "正在从颜色层 {layer} (H{h} S{s} V{v}) 起绘制至最后",
        "draw_from_layer_plain": "正在从颜色层 {layer} 起绘制至最后",
        "draw_layer_hsv": "正在发送颜色层 {layer} (H{h} S{s} V{v}) 绘画按键序列",
        "draw_layer_plain": "正在发送颜色层 {layer} 绘画按键序列",
    },
    "en": {
        "lang_label": "Language",
        "page_title": "TomodachiDrawing WebUI",
        "header_title": "TomodachiDrawing WebUI",
        "seq_count_label": "Saved sequences: ",
        "section_controller": "Virtual controller",
        "section_buttons": "Send Switch buttons",
        "section_upload": "Upload image & generate macro",
        "section_stability": "Stability test sequence",
        "section_sequences": "Sequences",
        "detail_pick": "Select an item",
        "h_layers": "Color layers",
        "status_loading": "Loading…",
        "btn_connect": "Create & wait for host",
        "btn_reconnect": "Reconnect controller",
        "btn_lr": "Send L+R",
        "lbl_image": "Image file (must be 256×256)",
        "lbl_mode": "Generation mode",
        "mode_brush": "brush: prefer large brush strokes",
        "mode_pixel": "pixel: per-pixel drawing",
        "lbl_press": "Press duration",
        "lbl_wait": "Wait duration",
        "lbl_min_gain": "brush min gain",
        "lbl_merge": "Merge similar colors (threshold)",
        "label_return_home": "After each color layer, return to (0,0) and reset color to bottom",
        "btn_generate": "Generate macro",
        "stability_intro": "Generates many <code>DPAD_RIGHT</code> and <code>A</code> lines (same timing as drawing macros) to tune intervals on hardware. Then select the entry and use <strong>Draw whole image</strong> to send to the Switch.",
        "lbl_stability_pairs": "Loop count (one loop = RIGHT then A)",
        "lbl_stability_wait": "Gap duration",
        "btn_stability": "Generate stability test",
        "btn_draw_whole": "Draw whole image",
        "btn_draw_layer": "Draw current layer",
        "btn_draw_from_layer": "Draw from current layer",
        "btn_delete": "Delete sequence",
        "btn_pause": "Pause",
        "btn_resume": "Resume",
        "btn_stop": "Stop",
        "link_macro": "Open macro.txt",
        "link_layer_macro": "Open layer macro.txt",
        "cap_upload": "Uploaded image",
        "cap_quantized": "Quantized preview",
        "cap_sequence": "Macro playback preview",
        "cap_layer": "Current layer preview",
        "alt_upload": "Uploaded image preview",
        "alt_quantized": "Quantized preview",
        "alt_sequence": "Sequence preview",
        "alt_layer": "Layer preview",
        "list_empty": "No sequences yet",
        "detail_empty": "Select an item",
        "layer_none": "No per-layer data for this entry",
        "lines_unit": "lines",
        "pixels_unit": "px",
        "lines_per_sec": "lines/s",
        "eta_label": "ETA",
        "status_idle": "Virtual controller not created",
        "status_starting": "Creating virtual controller",
        "status_waiting": "Waiting for Switch",
        "status_connected": "Connected",
        "status_error": "Connection error",
        "poll_failed": "Failed to read status",
        "meta_mode": "Mode",
        "meta_pairs": "Loops",
        "meta_press": "Press",
        "meta_wait": "Wait / gap",
        "meta_macro_lines": "Macro lines",
        "meta_colors": "Color layers",
        "meta_merge": "Merge threshold",
        "meta_return_home": "Return home per layer",
        "meta_gen_total": "Generation total",
        "on": "On",
        "off": "Off",
        "mode_stability": "Stability test",
        "bench_stab": "macro write {mw}s · placeholders {ph}s",
        "bench_full": "ingest {ing}s · quantize {qu}s · dump {du}s · full macro {fmg}s · full preview {fpv}s · layers {ll}s (macro {lmg}s / IO & previews {lo}s)",
        "draw_bench": "Draw {outcome} · total {total}s · {prep}read {read}s · wait ctrl {cw}s · macro active {macro}s · paused {pause}s · chunks {chunks} · sent {sent}/{tot} lines · avg {lps} lines/s",
        "bench_prep": "partial macro {s}s · ",
        "bench_prep_empty": "",
        "outcome_done": "completed",
        "outcome_cancelled": "stopped",
        "outcome_error": "error",
        "upload_generating": "Generating…",
        "upload_done_lines": "Generated {n} macro lines",
        "upload_done_bench": "Generated {n} macro lines ({t}s total)",
        "stab_generating": "Generating…",
        "stab_done": "Generated {n} lines",
        "stab_done_bench": "Generated {n} lines ({t}s total). Select it and use Draw whole image.",
        "api_fallback": "Request failed",
        "gen_fallback": "Generation failed",
        "confirm_from_layer": "Draw from layer {layer} through the end ({n} layers).\nThe partial macro may regenerate first (a few seconds). Continue?",
        "confirm_delete": "Delete sequence “{id}”? The job folder will be removed permanently.",
        "draw_msg_sending_line": "Sending line {a}/{b}",
        "draw_stop_near": "Stopped around line {a}/{b}; remaining lines not sent",
        "draw_pause_near": "Paused around line {a}/{b}; remaining lines not sent",
        "draw_from_layer_hsv": "Drawing from layer {layer} (H{h} S{s} V{v}) to the end",
        "draw_from_layer_plain": "Drawing from layer {layer} to the end",
        "draw_layer_hsv": "Sending layer {layer} (H{h} S{s} V{v}) macro",
        "draw_layer_plain": "Sending layer {layer} macro",
    },
}

# Chinese UI/server strings → English (when UI language is English).
ZH_TO_EN_UI = {
    "未创建虚拟手柄": "Virtual controller not created",
    "正在创建虚拟 Pro Controller": "Creating virtual Pro Controller",
    "正在重新连接，已请求清理旧虚拟手柄": "Reconnecting; old controller cleanup requested",
    "请在 Switch 的更改握法/顺序页面连接这个虚拟手柄": "On Switch, connect this controller from the Change Grip/Order screen",
    "虚拟手柄已连接": "Virtual controller connected",
    "虚拟手柄连接失败": "Virtual controller connection failed",
    "虚拟手柄尚未连接": "Virtual controller not connected",
    "未开始绘画": "Drawing not started",
    "已暂停，后续按键未发送": "Paused; remaining inputs not sent",
    "继续发送绘画按键序列": "Resuming macro playback",
    "正在终止绘画，停止当前按键": "Stopping drawing; halting current macro",
    "找不到按键序列": "Sequence not found",
    "正在发送绘画按键序列": "Sending drawing macro",
    "正在发送整图绘画按键序列": "Sending whole-image macro",
    "绘画已终止，后续按键未发送": "Drawing stopped; remaining lines not sent",
    "绘画按键序列发送完成": "Macro playback finished",
    "绘画发送失败": "Drawing send failed",
    "绘画进行中，请先终止或等待结束后再重新连接手柄": "Drawing in progress; stop or wait before reconnecting",
    "绘画进行中，暂时不能发送单独按键": "Drawing in progress; single buttons disabled",
    "不支持的按键": "Unsupported button",
    "请选择图片": "Choose an image",
    "mode 只能是 pixel 或 brush": "mode must be pixel or brush",
    "参数必须为正数，合并阈值可以为 0": "Parameters must be positive; merge threshold may be 0",
    "pairs / press / wait 参数无效": "Invalid pairs / press / wait",
    "该序列正在绘制或终止中，请先停止绘画后再删除": "Sequence is drawing or stopping; stop drawing first",
    "已有绘画任务正在进行": "A drawing task is already running",
    "找不到颜色层": "Layer not found",
    "该颜色层缺少宏文件": "This layer has no macro file",
    "当前没有正在运行的绘画任务": "No drawing task is running",
    "当前没有已暂停的绘画任务": "No paused drawing task",
    "当前没有可以终止的绘画任务": "Nothing to stop",
    "找不到文件": "File not found",
    "第一层等价于绘制整图，请改用「绘制整图」": "First layer equals whole image; use Draw whole image",
    "缺少 source.png，无法重新生成": "Missing source.png; cannot regenerate",
    "按下时长与间隔须为正数": "Press and gap durations must be positive",
}
ZH_TO_EN_UI[f"循环次数须在 1～{STABILITY_MAX_PAIRS} 之间"] = (
    f"Loop count must be between 1 and {STABILITY_MAX_PAIRS}"
)


INDEX_HTML = r"""
<!doctype html>
<html lang="{{ html_lang }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ i18n_catalog[ui_lang]['page_title'] }}</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --line: #d8dee4;
      --text: #17202a;
      --muted: #687785;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: 0; }
    main {
      width: min(1420px, 100%);
      margin: 0 auto;
      padding: 20px;
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }
    h2 { margin: 0 0 12px; font-size: 16px; }
    label { display: block; font-size: 13px; color: var(--muted); margin: 10px 0 6px; }
    input, select {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--text);
    }
    button {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 11px;
      background: #fff;
      color: var(--text);
      cursor: pointer;
      font-weight: 600;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.blue { background: var(--accent-2); border-color: var(--accent-2); color: #fff; }
    button.danger { background: var(--danger); border-color: var(--danger); color: #fff; }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .stack { display: grid; gap: 14px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .status {
      display: grid;
      gap: 5px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f9fafb;
    }
    .status strong { font-size: 14px; }
    .muted { color: var(--muted); font-size: 13px; }
    .error { color: var(--danger); }
    .controller-pad {
      display: grid;
      grid-template-columns: 1fr 74px 1fr;
      grid-template-areas:
        "shoulder-left center shoulder-right"
        "dpad center face"
        "stick-left bottom stick-right"
        "extra extra extra";
      gap: 12px;
      align-items: center;
    }
    .controller-pad button {
      min-width: 0;
      min-height: 36px;
      padding: 6px;
      font-size: 12px;
      line-height: 1;
    }
    .pad-cluster {
      display: grid;
      grid-template-columns: repeat(3, 36px);
      grid-template-rows: repeat(3, 36px);
      gap: 4px;
      justify-content: center;
      align-items: center;
    }
    .pad-center {
      grid-area: center;
      display: grid;
      gap: 7px;
      justify-items: center;
    }
    .pad-bottom {
      grid-area: bottom;
      display: grid;
      grid-template-columns: 1fr;
      gap: 7px;
    }
    .pad-extra {
      grid-area: extra;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 7px;
    }
    .pad-shoulder-left { grid-area: shoulder-left; display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .pad-shoulder-right { grid-area: shoulder-right; display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .pad-dpad { grid-area: dpad; }
    .pad-face { grid-area: face; }
    .pad-stick-left { grid-area: stick-left; justify-self: center; width: 92px; }
    .pad-stick-right { grid-area: stick-right; justify-self: center; width: 92px; }
    .pad-spacer { visibility: hidden; pointer-events: none; }
    .round-button {
      border-radius: 999px;
      aspect-ratio: 1;
      width: 36px;
      min-height: 36px;
      padding: 0;
    }
    .wide-button {
      width: 100%;
      border-radius: 999px;
    }
    .layout {
      display: grid;
      grid-template-columns: 300px 1fr;
      gap: 16px;
    }
    .list {
      display: grid;
      gap: 8px;
      max-height: 640px;
      overflow: auto;
      padding-right: 4px;
    }
    .item {
      width: 100%;
      text-align: left;
      display: grid;
      gap: 4px;
      border-color: var(--line);
    }
    .item.active { border-color: var(--accent-2); box-shadow: inset 3px 0 0 var(--accent-2); }
    .previews {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    figure { margin: 0; }
    figcaption { margin-bottom: 6px; color: var(--muted); font-size: 13px; }
    img.preview {
      width: 100%;
      max-height: 420px;
      aspect-ratio: 1;
      object-fit: contain;
      image-rendering: pixelated;
      background:
        linear-gradient(45deg, #e7ecf0 25%, transparent 25%),
        linear-gradient(-45deg, #e7ecf0 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #e7ecf0 75%),
        linear-gradient(-45deg, transparent 75%, #e7ecf0 75%);
      background-size: 20px 20px;
      background-position: 0 0, 0 10px, 10px -10px, -10px 0;
      border: 1px solid var(--line);
      border-radius: 6px;
    }
    progress { width: 100%; height: 18px; }
    .meta {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }
    .meta div { border: 1px solid var(--line); border-radius: 6px; padding: 9px; background: #fbfcfd; }
    .meta b { display: block; font-size: 18px; }
    @media (max-width: 980px) {
      main, .layout, .previews { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; }
    }
    header .header-right {
      display: flex;
      align-items: center;
      gap: 16px;
      flex-wrap: wrap;
    }
    .lang-select {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--muted);
    }
    .lang-select select {
      width: auto;
      min-width: 110px;
      min-height: 32px;
    }
  </style>
</head>
<body>
  <header>
    <h1 data-i18n="header_title">TomodachiDrawing WebUI</h1>
    <div class="header-right">
      <label class="lang-select">
        <span data-i18n="lang_label">语言</span>
        <select id="langSelect" aria-label="Language">
          <option value="zh">中文</option>
          <option value="en">English</option>
        </select>
      </label>
      <div class="muted"><span data-i18n="seq_count_label">已生成按键序列：</span><strong id="sequenceCount">0</strong></div>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2 data-i18n="section_controller">虚拟手柄</h2>
        <div class="status">
          <strong id="controllerText" data-i18n="status_loading">读取中</strong>
          <span id="controllerMessage" class="muted"></span>
          <span id="controllerError" class="muted error"></span>
        </div>
        <div class="row" style="margin-top: 12px">
          <button class="primary" id="connectBtn" data-i18n="btn_connect">等待主机连接虚拟手柄</button>
          <button id="reconnectBtn" data-i18n="btn_reconnect">重新连接手柄</button>
          <button id="lrBtn" data-i18n="btn_lr">发送 L+R</button>
        </div>
      </section>

      <section>
        <h2 data-i18n="section_buttons">发送 Switch 按键</h2>
        <div class="controller-pad" id="controllerPad">
          <div class="pad-shoulder-left">
            <button data-button="ZL">ZL</button>
            <button data-button="L">L</button>
          </div>
          <div class="pad-shoulder-right">
            <button data-button="R">R</button>
            <button data-button="ZR">ZR</button>
          </div>
          <div class="pad-cluster pad-dpad">
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="DPAD_UP">↑</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="DPAD_LEFT">←</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="DPAD_RIGHT">→</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="DPAD_DOWN">↓</button>
            <span class="pad-spacer"></span>
          </div>
          <div class="pad-center">
            <button class="round-button" data-button="PLUS">+</button>
            <button class="round-button" data-button="HOME">⌂</button>
            <button class="round-button" data-button="MINUS">−</button>
          </div>
          <div class="pad-cluster pad-face">
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="X">X</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="Y">Y</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="A">A</button>
            <span class="pad-spacer"></span>
            <button class="round-button" data-button="B">B</button>
            <span class="pad-spacer"></span>
          </div>
          <button class="wide-button pad-stick-left" data-button="L_STICK_PRESS">L Stick</button>
          <div class="pad-bottom">
            <button data-button="CAPTURE">Capture</button>
          </div>
          <button class="wide-button pad-stick-right" data-button="R_STICK_PRESS">R Stick</button>
          <div class="pad-extra">
            <button data-button="JCL_SL">L SL</button>
            <button data-button="JCL_SR">L SR</button>
            <button data-button="JCR_SL">R SL</button>
            <button data-button="JCR_SR">R SR</button>
          </div>
        </div>
      </section>

      <section>
        <h2 data-i18n="section_upload">上传图片并生成序列</h2>
        <form id="uploadForm">
          <label for="imageInput" data-i18n="lbl_image">图片文件，尺寸必须为 256x256</label>
          <input id="imageInput" name="image" type="file" accept="image/*" required>
          <label for="modeInput" data-i18n="lbl_mode">生成模式</label>
          <select id="modeInput" name="mode">
            <option value="brush" data-i18n="mode_brush">brush：优先使用大笔刷</option>
            <option value="pixel" data-i18n="mode_pixel">pixel：逐像素绘制</option>
          </select>
          <div class="row">
            <div style="flex:1">
              <label for="pressInput" data-i18n="lbl_press">按下时长</label>
              <input id="pressInput" name="press" type="number" step="0.001" min="0.001" value="0.075">
            </div>
            <div style="flex:1">
              <label for="waitInput" data-i18n="lbl_wait">等待时长</label>
              <input id="waitInput" name="wait" type="number" step="0.001" min="0.001" value="0.075">
            </div>
          </div>
          <label for="minGainInput" data-i18n="lbl_min_gain">brush 最小收益</label>
          <input id="minGainInput" name="min_gain" type="number" step="1" min="1" value="1">
          <label for="mergeThresholdInput" data-i18n="lbl_merge">合并相近颜色阈值</label>
          <input id="mergeThresholdInput" name="merge_threshold" type="number" step="1" min="0" value="0">
          <label style="display:flex; gap:8px; align-items:center; color: var(--text); margin-top: 12px;">
            <input id="returnHomePerLayerInput" name="return_home_per_layer" type="checkbox" checked style="width:auto; min-height:auto;">
            <span data-i18n="label_return_home">每个颜色层结束后回到 (0,0) 并把颜色归到底部</span>
          </label>
          <div class="row" style="margin-top: 12px">
            <button class="blue" type="submit" data-i18n="btn_generate">生成按键序列</button>
          </div>
        </form>
        <p id="uploadMessage" class="muted"></p>
      </section>

      <section>
        <h2 data-i18n="section_stability">稳定性测试序列</h2>
        <p class="muted" data-i18n-html="stability_intro">生成大量 <code>DPAD_RIGHT</code> 与 <code>A</code>（按下时长 + 间隔与绘画宏相同），用于在真机上摸索最合适的间隔。生成后选中该条目，使用「绘制整图」发送到 Switch。</p>
        <form id="stabilityForm">
          <label for="stabilityPairsInput" data-i18n="lbl_stability_pairs">循环次数（一次 = 先 RIGHT 再 A）</label>
          <input id="stabilityPairsInput" name="pairs" type="number" step="1" min="1" max="50000" value="800" required>
          <div class="row">
            <div style="flex:1">
              <label for="stabilityPressInput" data-i18n="lbl_press">按下时长</label>
              <input id="stabilityPressInput" name="press" type="number" step="0.001" min="0.001" value="0.075">
            </div>
            <div style="flex:1">
              <label for="stabilityWaitInput" data-i18n="lbl_stability_wait">间隔时长</label>
              <input id="stabilityWaitInput" name="wait" type="number" step="0.001" min="0.001" value="0.075">
            </div>
          </div>
          <div class="row" style="margin-top: 12px">
            <button class="blue" type="submit" data-i18n="btn_stability">生成稳定性测试</button>
          </div>
        </form>
        <p id="stabilityMessage" class="muted"></p>
      </section>
    </div>

    <section>
      <div class="layout">
        <div>
          <h2 data-i18n="section_sequences">按键序列</h2>
          <div id="sequenceList" class="list"></div>
        </div>
        <div>
          <h2 id="detailTitle">请选择一个条目</h2>
          <div id="detailMeta" class="meta"></div>
          <div class="row">
            <button class="primary" id="drawBtn" data-i18n="btn_draw_whole">绘制整图</button>
            <button class="blue" id="drawLayerBtn" data-i18n="btn_draw_layer">绘制当前颜色层</button>
            <button class="blue" id="drawFromLayerBtn" data-i18n="btn_draw_from_layer">从当前颜色层开始绘制</button>
            <button class="danger" id="deleteSeqBtn" type="button" data-i18n="btn_delete">删除此序列</button>
            <button id="pauseBtn" data-i18n="btn_pause">暂停</button>
            <button class="danger" id="stopBtn" data-i18n="btn_stop">终止</button>
            <a id="macroLink" class="muted" href="#" target="_blank" rel="noreferrer" data-i18n="link_macro">查看 macro.txt</a>
            <a id="layerMacroLink" class="muted" href="#" target="_blank" rel="noreferrer" data-i18n="link_layer_macro">查看颜色层 macro.txt</a>
          </div>
          <div style="margin: 12px 0">
            <progress id="drawProgress" max="100" value="0"></progress>
            <div id="drawText" class="muted"></div>
            <div id="drawBench" class="muted" style="font-size: 0.85em; line-height: 1.45; margin-top: 4px;"></div>
          </div>
          <div class="previews">
            <figure>
              <figcaption data-i18n="cap_upload">上传图片</figcaption>
              <img id="sourcePreview" class="preview" alt="" data-i18n-alt="alt_upload">
            </figure>
            <figure>
              <figcaption data-i18n="cap_quantized">量化预览</figcaption>
              <img id="quantizedPreview" class="preview" alt="" data-i18n-alt="alt_quantized">
            </figure>
            <figure>
              <figcaption data-i18n="cap_sequence">按键序列回放预览</figcaption>
              <img id="sequencePreview" class="preview" alt="" data-i18n-alt="alt_sequence">
            </figure>
            <figure>
              <figcaption data-i18n="cap_layer">当前颜色层预览</figcaption>
              <img id="layerPreview" class="preview" alt="" data-i18n-alt="alt_layer">
            </figure>
          </div>
          <div style="margin-top: 12px">
            <h2 style="margin-bottom: 8px" data-i18n="h_layers">颜色层列表</h2>
            <div id="layerList" class="list" style="max-height: 260px"></div>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const buttons = {{ buttons|tojson }};
    const I18N = {{ i18n_catalog|tojson }};
    const ZH_TO_EN = {{ zh_to_en_ui|tojson }};
    const LS_KEY = 'tomodachi_lang';
    const COOKIE = 'tomodachi_lang';

    let lang = localStorage.getItem(LS_KEY) || '{{ ui_lang }}';
    if (lang !== 'zh' && lang !== 'en') lang = 'zh';

    let selectedId = null;
    let selectedLayerId = null;
    let entries = [];

    const el = id => document.getElementById(id);

    function t(key) {
      const pack = I18N[lang] || I18N.zh;
      if (pack && Object.prototype.hasOwnProperty.call(pack, key)) return pack[key];
      return (I18N.zh && I18N.zh[key]) || key;
    }

    function fmt(str, obj) {
      return str.replace(/\{(\w+)\}/g, (_, k) => (obj[k] !== undefined && obj[k] !== null) ? String(obj[k]) : '');
    }

    function translateKnownZh(s) {
      if (!s || lang === 'zh') return s;
      return ZH_TO_EN[s] || s;
    }

    function translateDrawMessage(msg) {
      if (!msg || lang === 'zh') return msg;
      const direct = ZH_TO_EN[msg];
      if (direct) return direct;
      let m;
      if ((m = msg.match(/^正在发送第 (\d+)\/(\d+) 行$/)))
        return fmt(t('draw_msg_sending_line'), { a: m[1], b: m[2] });
      if ((m = msg.match(/^已终止在第 (\d+)\/(\d+) 行附近，后续按键未发送$/)))
        return fmt(t('draw_stop_near'), { a: m[1], b: m[2] });
      if ((m = msg.match(/^已暂停在第 (\d+)\/(\d+) 行附近，后续按键未发送$/)))
        return fmt(t('draw_pause_near'), { a: m[1], b: m[2] });
      if ((m = msg.match(/^正在从颜色层 (\S+) \(H(\d+) S(\d+) V(\d+)\) 起绘制至最后$/)))
        return fmt(t('draw_from_layer_hsv'), { layer: m[1], h: m[2], s: m[3], v: m[4] });
      if ((m = msg.match(/^正在从颜色层 (\S+) 起绘制至最后$/)))
        return fmt(t('draw_from_layer_plain'), { layer: m[1] });
      if ((m = msg.match(/^正在发送颜色层 (\S+) \(H(\d+) S(\d+) V(\d+)\) 绘画按键序列$/)))
        return fmt(t('draw_layer_hsv'), { layer: m[1], h: m[2], s: m[3], v: m[4] });
      if ((m = msg.match(/^正在发送颜色层 (\S+) 绘画按键序列$/)))
        return fmt(t('draw_layer_plain'), { layer: m[1] });
      return msg;
    }

    function userFacingErrorMessage(s) {
      return translateKnownZh(s);
    }

    function applyI18n() {
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
      document.title = t('page_title');
      for (const node of document.querySelectorAll('[data-i18n]')) {
        const key = node.getAttribute('data-i18n');
        if (key) node.textContent = t(key);
      }
      for (const node of document.querySelectorAll('[data-i18n-html]')) {
        const key = node.getAttribute('data-i18n-html');
        if (key) node.innerHTML = t(key);
      }
      for (const node of document.querySelectorAll('[data-i18n-alt]')) {
        const key = node.getAttribute('data-i18n-alt');
        if (key) node.setAttribute('alt', t(key));
      }
      el('langSelect').value = lang;
    }

    function persistLang(next) {
      lang = next;
      localStorage.setItem(LS_KEY, lang);
      document.cookie = `${COOKIE}=${encodeURIComponent(lang)}; path=/; max-age=31536000; SameSite=Lax`;
      applyI18n();
      renderList();
      renderDetail();
    }

    el('langSelect').onchange = () => persistLang(el('langSelect').value);

    function statusText(status) {
      const key = { idle: 'status_idle', starting: 'status_starting', waiting: 'status_waiting', connected: 'status_connected', error: 'status_error' }[status];
      return key ? t(key) : status;
    }

    function formatEta(seconds) {
      if (seconds === null || seconds === undefined) return '--';
      const value = Math.max(0, Math.round(seconds));
      const h = Math.floor(value / 3600);
      const m = Math.floor((value % 3600) / 60);
      const s = value % 60;
      if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
      return `${m}:${String(s).padStart(2, '0')}`;
    }

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) {
        const raw = data.error || data.message || t('api_fallback');
        throw new Error(userFacingErrorMessage(raw));
      }
      return data;
    }

    async function pollStatus() {
      try {
        const data = await api('/api/status');
        const c = data.controller;
        const item = entries.find(entry => entry.id === selectedId);
        const hasLayer = !!(item && item.layers && item.layers.some(layer => layer.id === selectedLayerId));
        el('controllerText').textContent = statusText(c.status);
        el('controllerMessage').textContent = translateKnownZh(c.message || '');
        el('controllerError').textContent = translateKnownZh(c.error || '');
        el('sequenceCount').textContent = data.sequence_count;
        const drawBusy = data.draw.status === 'running' || data.draw.status === 'paused' || data.draw.status === 'stopping';
        el('connectBtn').disabled = drawBusy || c.status === 'starting' || c.status === 'waiting' || c.status === 'connected';
        el('reconnectBtn').disabled = drawBusy || c.status === 'starting';
        el('lrBtn').disabled = c.status !== 'connected' || drawBusy;
        el('drawBtn').disabled = !selectedId || c.status !== 'connected' || drawBusy;
        el('drawLayerBtn').disabled = !selectedId || !hasLayer || c.status !== 'connected' || drawBusy;
        const item2 = entries.find(entry => entry.id === selectedId);
        const layerIdx = item2 && item2.layers ? item2.layers.findIndex(layer => layer.id === selectedLayerId) : -1;
        el('drawFromLayerBtn').disabled = !selectedId || !hasLayer || layerIdx <= 0 || c.status !== 'connected' || drawBusy;
        el('pauseBtn').disabled = data.draw.status !== 'running' && data.draw.status !== 'paused';
        el('pauseBtn').textContent = data.draw.status === 'paused' ? t('btn_resume') : t('btn_pause');
        el('stopBtn').disabled = data.draw.status !== 'running' && data.draw.status !== 'paused';
        const drawingSeq = data.draw.sequence_id;
        el('deleteSeqBtn').disabled = !selectedId || (drawBusy && drawingSeq === selectedId);
        el('drawProgress').value = data.draw.percent || 0;
        const speed = Number(data.draw.lines_per_second || 0).toFixed(2);
        const eta = formatEta(data.draw.eta_seconds);
        const dm = translateDrawMessage(data.draw.message || '');
        el('drawText').textContent = `${dm} ${data.draw.percent || 0}% · ${data.draw.sent_lines || 0}/${data.draw.total_lines || 0} ${t('lines_unit')} · ${speed} ${t('lines_per_sec')} · ${t('eta_label')} ${eta}`;
        const bench = data.draw.benchmark;
        if (bench) {
          const outcomeKey = { completed: 'outcome_done', cancelled: 'outcome_cancelled', error: 'outcome_error' }[bench.outcome] || '';
          const outcomeLabel = outcomeKey ? t(outcomeKey) : bench.outcome;
          const prepText = bench.prep_seconds > 0 ? fmt(t('bench_prep'), { s: bench.prep_seconds }) : t('bench_prep_empty');
          el('drawBench').textContent = fmt(t('draw_bench'), {
            outcome: outcomeLabel,
            total: bench.total_seconds,
            prep: prepText,
            read: bench.read_seconds,
            cw: bench.controller_wait_seconds,
            macro: bench.macro_active_seconds,
            pause: bench.paused_seconds,
            chunks: bench.chunks_count,
            sent: bench.sent_lines,
            tot: bench.total_lines,
            lps: bench.lines_per_second_overall,
          });
        } else {
          el('drawBench').textContent = '';
        }
      } catch (err) {
        el('controllerText').textContent = t('poll_failed');
        el('controllerError').textContent = userFacingErrorMessage(err.message);
      }
    }

    async function loadEntries(selectNewest = false) {
      const data = await api('/api/sequences');
      entries = data.entries;
      el('sequenceCount').textContent = entries.length;
      if (selectNewest && entries.length) selectedId = entries[0].id;
      renderList();
      renderDetail();
    }

    function renderList() {
      const list = el('sequenceList');
      list.innerHTML = '';
      if (!entries.length) {
        list.innerHTML = `<p class="muted">${t('list_empty')}</p>`;
        return;
      }
      for (const item of entries) {
        const button = document.createElement('button');
        button.className = 'item' + (item.id === selectedId ? ' active' : '');
        button.innerHTML = `<strong>${item.source_name}</strong><span class="muted">${item.mode} · ${item.macro_lines} ${t('lines_unit')} · ${item.created_at}</span>`;
        button.onclick = () => {
          selectedId = item.id;
          renderList();
          renderDetail();
          pollStatus();
        };
        list.appendChild(button);
      }
    }

    function renderDetail() {
      const item = entries.find(entry => entry.id === selectedId);
      if (!item) {
        el('detailTitle').textContent = t('detail_pick');
        el('detailMeta').innerHTML = '';
        for (const id of ['sourcePreview', 'quantizedPreview', 'sequencePreview', 'layerPreview']) el(id).removeAttribute('src');
        el('macroLink').href = '#';
        el('layerMacroLink').href = '#';
        el('layerList').innerHTML = `<p class="muted">${t('detail_empty')}</p>`;
        selectedLayerId = null;
        return;
      }
      const layers = item.layers || [];
      if (!layers.some(layer => layer.id === selectedLayerId)) {
        selectedLayerId = layers.length ? layers[0].id : null;
      }
      const selectedLayer = layers.find(layer => layer.id === selectedLayerId) || null;
      el('detailTitle').textContent = item.source_name;
      const b = item.benchmark;
      const isStability = item.mode === 'stability_test';
      let benchRow = '';
      if (b) {
        const benchLines = isStability
          ? fmt(t('bench_stab'), { mw: b.macro_write_seconds, ph: b.placeholder_images_seconds })
          : fmt(t('bench_full'), {
              ing: b.ingest_seconds, qu: b.quantize_seconds, du: b.dump_seconds,
              fmg: b.full_macro_generate_seconds, fpv: b.full_preview_seconds,
              ll: b.layers_loop_seconds, lmg: b.layers_macro_generate_seconds, lo: b.layers_other_seconds,
            });
        benchRow = `
        <div><span class="muted">${t('meta_gen_total')}</span><b>${b.total_seconds}s</b></div>
        <div class="muted" style="font-size:0.85em; grid-column: 1 / -1; line-height:1.45;">
          ${benchLines}
        </div>`;
      }
      if (isStability) {
        el('detailMeta').innerHTML = `
        <div><span class="muted">${t('meta_mode')}</span><b>${t('mode_stability')}</b></div>
        <div><span class="muted">${t('meta_pairs')}</span><b>${item.pairs}</b></div>
        <div><span class="muted">${t('meta_press')}</span><b>${item.press}</b></div>
        <div><span class="muted">${t('meta_wait')}</span><b>${item.wait}</b></div>
        <div><span class="muted">${t('meta_macro_lines')}</span><b>${item.macro_lines}</b></div>${benchRow}`;
      } else {
        el('detailMeta').innerHTML = `
        <div><span class="muted">${t('meta_mode')}</span><b>${item.mode}</b></div>
        <div><span class="muted">${t('meta_colors')}</span><b>${item.colors}</b></div>
        <div><span class="muted">${t('meta_macro_lines')}</span><b>${item.macro_lines}</b></div>
        <div><span class="muted">${t('meta_merge')}</span><b>${item.merge_threshold || 0}</b></div>
        <div><span class="muted">${t('meta_return_home')}</span><b>${item.return_home_per_layer ? t('on') : t('off')}</b></div>${benchRow}`;
      }
      el('sourcePreview').src = item.source_url;
      el('quantizedPreview').src = item.quantized_preview_url;
      el('sequencePreview').src = item.sequence_preview_url;
      el('macroLink').href = item.macro_url;
      if (selectedLayer && selectedLayer.preview_url) {
        el('layerPreview').src = selectedLayer.preview_url;
      } else {
        el('layerPreview').removeAttribute('src');
      }
      el('layerMacroLink').href = selectedLayer && selectedLayer.macro_url ? selectedLayer.macro_url : '#';
      renderLayerList(item, selectedLayerId);
    }

    function renderLayerList(item, activeLayerId) {
      const list = el('layerList');
      const layers = item.layers || [];
      list.innerHTML = '';
      if (!layers.length) {
        list.innerHTML = `<p class="muted">${t('layer_none')}</p>`;
        return;
      }
      for (const layer of layers) {
        const color = layer.color || ['?', '?', '?'];
        const button = document.createElement('button');
        button.className = 'item' + (layer.id === activeLayerId ? ' active' : '');
        button.innerHTML = `<strong>${layer.id} · H${color[0]} S${color[1]} V${color[2]}</strong><span class="muted">${layer.count || 0} ${t('pixels_unit')} · ${layer.macro_lines || 0} ${t('lines_unit')}</span>`;
        button.onclick = () => {
          selectedLayerId = layer.id;
          renderDetail();
          pollStatus();
        };
        list.appendChild(button);
      }
    }

    function initButtons() {
      for (const button of document.querySelectorAll('[data-button]')) {
        const buttonName = button.dataset.button;
        if (!buttons.includes(buttonName)) continue;
        button.onclick = async () => {
          try {
            await api('/api/controller/button', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({button: buttonName})
            });
          } catch (err) {
            alert(userFacingErrorMessage(err.message));
          }
        };
      }
    }

    el('connectBtn').onclick = async () => {
      try { await api('/api/controller/connect', {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('reconnectBtn').onclick = async () => {
      try { await api('/api/controller/reconnect', {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('lrBtn').onclick = async () => {
      try { await api('/api/controller/lr', {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('drawBtn').onclick = async () => {
      if (!selectedId) return;
      try { await api(`/api/draw/${selectedId}`, {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('drawLayerBtn').onclick = async () => {
      if (!selectedId || !selectedLayerId) return;
      try { await api(`/api/draw/${selectedId}/layer/${selectedLayerId}`, {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('drawFromLayerBtn').onclick = async () => {
      if (!selectedId || !selectedLayerId) return;
      const item = entries.find(entry => entry.id === selectedId);
      const layers = (item && item.layers) || [];
      const idx = layers.findIndex(layer => layer.id === selectedLayerId);
      if (idx <= 0) return;
      const remaining = layers.length - idx;
      if (!confirm(fmt(t('confirm_from_layer'), { layer: selectedLayerId, n: remaining }))) return;
      try { await api(`/api/draw/${selectedId}/from-layer/${selectedLayerId}`, {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('deleteSeqBtn').onclick = async () => {
      if (!selectedId) return;
      if (!confirm(fmt(t('confirm_delete'), { id: selectedId }))) return;
      try {
        await api(`/api/sequences/${encodeURIComponent(selectedId)}`, {method: 'DELETE'});
        selectedId = null;
        selectedLayerId = null;
        await loadEntries();
      } catch (err) {
        alert(userFacingErrorMessage(err.message));
      }
      pollStatus();
    };

    el('pauseBtn').onclick = async () => {
      try {
        const status = (await api('/api/status')).draw.status;
        const path = status === 'paused' ? '/api/draw/resume' : '/api/draw/pause';
        await api(path, {method: 'POST'});
      } catch (err) {
        alert(userFacingErrorMessage(err.message));
      }
      pollStatus();
    };

    el('stopBtn').onclick = async () => {
      try { await api('/api/draw/stop', {method: 'POST'}); }
      catch (err) { alert(userFacingErrorMessage(err.message)); }
      pollStatus();
    };

    el('uploadForm').onsubmit = async event => {
      event.preventDefault();
      el('uploadMessage').textContent = t('upload_generating');
      try {
        const form = new FormData(event.currentTarget);
        const res = await fetch('/api/sequences', {method: 'POST', body: form});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || t('gen_fallback'));
        selectedId = data.entry.id;
        const bt = data.entry.benchmark && data.entry.benchmark.total_seconds;
        el('uploadMessage').textContent = bt != null
          ? fmt(t('upload_done_bench'), { n: data.entry.macro_lines, t: Number(bt).toFixed(2) })
          : fmt(t('upload_done_lines'), { n: data.entry.macro_lines });
        await loadEntries();
      } catch (err) {
        el('uploadMessage').textContent = userFacingErrorMessage(err.message);
      }
    };

    el('stabilityForm').onsubmit = async event => {
      event.preventDefault();
      el('stabilityMessage').textContent = t('stab_generating');
      try {
        const fd = new FormData(event.currentTarget);
        const body = {
          pairs: parseInt(fd.get('pairs'), 10),
          press: parseFloat(fd.get('press')),
          wait: parseFloat(fd.get('wait')),
        };
        const res = await fetch('/api/sequences/stability-test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || t('gen_fallback'));
        selectedId = data.entry.id;
        const bt = data.entry.benchmark && data.entry.benchmark.total_seconds;
        el('stabilityMessage').textContent = bt != null
          ? fmt(t('stab_done_bench'), { n: data.entry.macro_lines, t: Number(bt).toFixed(2) })
          : fmt(t('stab_done'), { n: data.entry.macro_lines });
        await loadEntries(true);
      } catch (err) {
        el('stabilityMessage').textContent = userFacingErrorMessage(err.message);
      }
    };

    applyI18n();
    initButtons();
    loadEntries();
    pollStatus();
    setInterval(pollStatus, 1000);
  </script>
</body>
</html>
"""


def public_entry(meta):
    job_id = meta["id"]
    layers = []
    for layer in meta.get("layers", []):
        layer_copy = dict(layer)
        macro_file = layer_copy.get("macro_file")
        preview_file = layer_copy.get("preview_file")
        if macro_file:
            layer_copy["macro_url"] = f"/files/{job_id}/{macro_file}"
        if preview_file:
            layer_copy["preview_url"] = f"/files/{job_id}/{preview_file}"
        layers.append(layer_copy)
    return {
        **meta,
        "layers": layers,
        "source_url": f"/files/{job_id}/source.png",
        "quantized_preview_url": f"/files/{job_id}/quantized_preview.png",
        "sequence_preview_url": f"/files/{job_id}/sequence_preview.png",
        "macro_url": f"/files/{job_id}/macro.txt",
    }


def finalize_job_benchmark(segments, job_started_perf):
    """Round segment timings and append wall-clock total (seconds, perf_counter)."""
    total = time.perf_counter() - job_started_perf
    out = {key: round(val, 4) for key, val in segments.items()}
    out["total_seconds"] = round(total, 4)
    return out


def finalize_draw_benchmark(segments, draw_started_perf, sent_lines, total_lines, outcome):
    """Round drawing-phase timings and append totals/throughput for a draw run."""
    total = time.perf_counter() - draw_started_perf
    out = {
        key: (round(val, 4) if isinstance(val, float) else val)
        for key, val in segments.items()
    }
    out["total_seconds"] = round(total, 4)
    out["sent_lines"] = int(sent_lines)
    out["total_lines"] = int(total_lines)
    out["lines_per_second_overall"] = (
        round(sent_lines / total, 2) if total > 0 and sent_lines > 0 else 0.0
    )
    out["outcome"] = outcome
    return out


def log_draw_benchmark(sequence_id, benchmark):
    """Emit the draw-phase benchmark to the logger using the same shape as job benchmarks."""
    LOGGER.info(
        "Draw %s benchmark outcome=%s total=%.3fs prep=%.3fs read=%.3fs controller_wait=%.3fs "
        "macro_active=%.3fs paused=%.3fs chunks=%d sent=%d/%d (%.2f lines/s)",
        sequence_id,
        benchmark["outcome"],
        benchmark["total_seconds"],
        benchmark["prep_seconds"],
        benchmark["read_seconds"],
        benchmark["controller_wait_seconds"],
        benchmark["macro_active_seconds"],
        benchmark["paused_seconds"],
        benchmark["chunks_count"],
        benchmark["sent_lines"],
        benchmark["total_lines"],
        benchmark["lines_per_second_overall"],
    )


def build_stability_test_macro_lines(pairs, press, wait):
    """Alternating DPAD_RIGHT and A with the same timing shape as drawing macros."""
    pt = fmt_seconds(press)
    wt = fmt_seconds(wait)
    unit = [f"DPAD_RIGHT {pt}", wt, f"A {pt}", wt]
    return unit * pairs


def create_stability_test_job(pairs, press, wait):
    """Write a job folder with macro.txt only useful for timing experiments on hardware."""
    if pairs < 1 or pairs > STABILITY_MAX_PAIRS:
        raise ValueError(f"循环次数须在 1～{STABILITY_MAX_PAIRS} 之间")
    if press <= 0 or wait <= 0:
        raise ValueError("按下时长与间隔须为正数")

    job_started_perf = time.perf_counter()
    bench_segments = {}

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    t0 = time.perf_counter()
    lines = collapse_macro_loop_blocks(build_stability_test_macro_lines(pairs, press, wait))
    (job_dir / "macro.txt").write_text("\n".join(lines), encoding="utf-8")
    bench_segments["macro_write_seconds"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    from PIL import Image

    placeholder = Image.new("RGBA", (256, 256), (248, 248, 250, 255))
    placeholder.save(job_dir / "source.png")
    placeholder.save(job_dir / "quantized_preview.png")
    placeholder.save(job_dir / "sequence_preview.png")
    bench_segments["placeholder_images_seconds"] = time.perf_counter() - t0

    (job_dir / "points.json").write_text(
        json.dumps({"size": [256, 256], "layers": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    macro_lines = len(lines)
    benchmark = finalize_job_benchmark(bench_segments, job_started_perf)
    meta = {
        "id": job_id,
        "source_name": "稳定性测试",
        "created_at": now_text(),
        "mode": "stability_test",
        "kind": "stability_test",
        "press": press,
        "wait": wait,
        "pairs": pairs,
        "merge_threshold": 0,
        "return_home_per_layer": False,
        "min_gain": 1,
        "colors": 0,
        "macro_lines": macro_lines,
        "layers": [],
        "benchmark": benchmark,
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    LOGGER.info(
        "Created stability test job %s pairs=%s macro_lines=%s press=%s wait=%s",
        job_id,
        pairs,
        macro_lines,
        press,
        wait,
    )
    return public_entry(meta)


def layer_id_from_index(index):
    return f"L{index:03d}"


def build_layer_image(layer):
    rgba = np.zeros((256, 256, 4), dtype=np.uint8)
    rgb = color_key_to_rgb(layer.key)
    rgba[layer.mask, 0] = rgb[0]
    rgba[layer.mask, 1] = rgb[1]
    rgba[layer.mask, 2] = rgb[2]
    rgba[layer.mask, 3] = 255
    from PIL import Image

    return Image.fromarray(rgba, mode="RGBA")


def load_entries():
    entries = []
    if not JOBS_ROOT.exists():
        return entries
    for meta_path in JOBS_ROOT.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            entries.append(public_entry(meta))
        except Exception as exc:
            LOGGER.warning("Skipping invalid sequence metadata %s: %s", meta_path, exc)
            continue
    entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return entries


def find_entry(sequence_id):
    for entry in load_entries():
        if entry["id"] == sequence_id:
            return entry
    return None


def generate_sequence(
    image_storage,
    mode,
    press,
    wait,
    min_gain,
    merge_threshold,
    return_home_per_layer,
):
    job_started_perf = time.perf_counter()
    bench_segments = {}

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    source_name = secure_filename(image_storage.filename) or "image.png"
    raw_source_path = job_dir / f"upload_{source_name}"
    source_path = job_dir / "source.png"
    t0 = time.perf_counter()
    image_storage.save(raw_source_path)

    from PIL import Image

    with Image.open(raw_source_path) as image:
        image.convert("RGBA").save(source_path)
    bench_segments["ingest_seconds"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    keys, opaque = load_quantized_image(source_path, merge_threshold=merge_threshold)
    layers = build_color_layers(keys, opaque)
    bench_segments["quantize_seconds"] = time.perf_counter() - t0

    quantized_preview_path = job_dir / "quantized_preview.png"
    point_dump_path = job_dir / "points.json"
    macro_path = job_dir / "macro.txt"
    sequence_preview_path = job_dir / "sequence_preview.png"
    layer_dir = job_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    save_quantized_preview(keys, opaque, quantized_preview_path)
    dump_point_layers(layers, point_dump_path)
    bench_segments["dump_seconds"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    if mode == "pixel":
        commands, color_count = generate_pixel_commands(
            source_path,
            press,
            wait,
            merge_threshold,
            return_home_per_layer=False,
        )
    else:
        commands, color_count = generate_brush_commands(
            source_path,
            press,
            wait,
            min_gain,
            merge_threshold,
            return_home_per_layer=False,
        )
    bench_segments["full_macro_generate_seconds"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    macro_path.write_text("\n".join(commands), encoding="utf-8")
    render_macro_preview(macro_path, sequence_preview_path)
    bench_segments["full_preview_seconds"] = time.perf_counter() - t0

    layer_entries = []
    layers_macro_generate_seconds = 0.0
    t_layers = time.perf_counter()
    for index, layer in enumerate(layers):
        layer_id = layer_id_from_index(index)
        h, s, v = layer.key
        stem = f"{layer_id}_h{h:03d}_s{s:03d}_v{v:03d}"
        layer_source_path = layer_dir / f"{stem}_source.png"
        layer_macro_path = layer_dir / f"{stem}.txt"
        layer_preview_path = layer_dir / f"{stem}_preview.png"

        build_layer_image(layer).save(layer_source_path)
        t_gen = time.perf_counter()
        if mode == "pixel":
            layer_commands, _ = generate_pixel_commands(
                layer_source_path,
                press,
                wait,
                merge_threshold,
                return_home_per_layer,
            )
        else:
            layer_commands, _ = generate_brush_commands(
                layer_source_path,
                press,
                wait,
                min_gain,
                merge_threshold,
                return_home_per_layer,
            )
        layers_macro_generate_seconds += time.perf_counter() - t_gen
        layer_macro_path.write_text("\n".join(layer_commands), encoding="utf-8")
        render_macro_preview(layer_macro_path, layer_preview_path)
        layer_entries.append(
            {
                "id": layer_id,
                "index": index,
                "color": [int(h), int(s), int(v)],
                "count": int(layer.count),
                "macro_lines": len(layer_commands),
                "source_file": f"layers/{layer_source_path.name}",
                "macro_file": f"layers/{layer_macro_path.name}",
                "preview_file": f"layers/{layer_preview_path.name}",
            }
        )
    bench_segments["layers_loop_seconds"] = time.perf_counter() - t_layers
    bench_segments["layers_macro_generate_seconds"] = layers_macro_generate_seconds
    bench_segments["layers_other_seconds"] = max(
        0.0,
        bench_segments["layers_loop_seconds"] - layers_macro_generate_seconds,
    )

    benchmark = finalize_job_benchmark(bench_segments, job_started_perf)
    LOGGER.info(
        "Job %s benchmark total=%.3fs quantize=%.3fs full_macro=%.3fs "
        "full_preview=%.3fs layers_loop=%.3fs (layers_macro=%.3fs)",
        job_id,
        benchmark["total_seconds"],
        benchmark["quantize_seconds"],
        benchmark["full_macro_generate_seconds"],
        benchmark["full_preview_seconds"],
        benchmark["layers_loop_seconds"],
        benchmark["layers_macro_generate_seconds"],
    )

    meta = {
        "id": job_id,
        "source_name": source_name,
        "created_at": now_text(),
        "mode": mode,
        "press": press,
        "wait": wait,
        "min_gain": min_gain,
        "merge_threshold": merge_threshold,
        "return_home_per_layer": bool(return_home_per_layer),
        "colors": color_count,
        "macro_lines": len(commands),
        "layers": layer_entries,
        "benchmark": benchmark,
    }
    (job_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return public_entry(meta)


def draw_speed_stats(start_monotonic, sent, total):
    elapsed = max(time.monotonic() - start_monotonic, 0.001)
    speed = sent / elapsed if sent > 0 else 0.0
    remaining = max(total - sent, 0)
    eta = remaining / speed if speed > 0 else None
    return round(speed, 2), int(round(eta)) if eta is not None else None


def is_macro_finished(nx, controller_index, macro_id):
    finished = nx.manager_state[controller_index]["finished_macros"]
    return macro_id in finished


def stop_macro_and_wait(nx, controller_index, macro_id):
    try:
        nx.stop_macro(controller_index, macro_id, block=False)
    except Exception as exc:
        LOGGER.warning("Failed to request macro stop: %s", exc)
        return

    deadline = time.monotonic() + MACRO_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_macro_finished(nx, controller_index, macro_id):
            return
        time.sleep(MACRO_POLL_SECONDS)


def run_macro_chunk_until_done_or_interrupted(nx, controller_index, chunk_text):
    macro_id = nx.macro(controller_index, chunk_text, block=False)
    while not is_macro_finished(nx, controller_index, macro_id):
        if draw_state.is_cancel_requested():
            stop_macro_and_wait(nx, controller_index, macro_id)
            return "cancelled"
        if draw_state.is_pause_requested():
            stop_macro_and_wait(nx, controller_index, macro_id)
            return "paused"
        time.sleep(MACRO_POLL_SECONDS)
    return "completed"


def update_draw_progress(start_monotonic, sent, total, message=None):
    percent = int(sent * 100 / total) if total else 100
    speed, eta = draw_speed_stats(start_monotonic, sent, total)
    update = {
        "sent_lines": sent,
        "percent": percent,
        "lines_per_second": speed,
        "eta_seconds": eta,
    }
    if message is not None:
        update["message"] = message
    draw_state.update(**update)


def find_layer(entry, layer_id):
    for layer in entry.get("layers", []):
        if layer.get("id") == layer_id:
            return layer
    return None


def find_layer_index(entry, layer_id):
    for layer in entry.get("layers", []):
        if layer.get("id") == layer_id:
            idx = layer.get("index")
            return int(idx) if idx is not None else None
    return None


def partial_macro_filename(layer_id):
    return f"macro_from_{layer_id}.txt"


def ensure_partial_macro(sequence_id, layer_id):
    """Generate (and cache) a macro that draws from `layer_id` to the last layer.

    Re-uses the same generator the original job used so that the colour /
    brush / canvas state is reset to home at the start, identical to picking
    up a fresh job whose first layer happens to be `layer_id`.
    """
    job_dir = resolve_job_directory(sequence_id)
    if not job_dir:
        raise FileNotFoundError("找不到按键序列")

    entry = find_entry(sequence_id)
    if not entry:
        raise FileNotFoundError("找不到按键序列")

    layer_index = find_layer_index(entry, layer_id)
    if layer_index is None:
        raise ValueError("找不到颜色层")
    if layer_index <= 0:
        raise ValueError("第一层等价于绘制整图，请改用「绘制整图」")

    out_path = job_dir / partial_macro_filename(layer_id)
    if out_path.is_file():
        return out_path, len(out_path.read_text(encoding="utf-8").splitlines()), True

    source_path = job_dir / "source.png"
    if not source_path.is_file():
        raise FileNotFoundError("缺少 source.png，无法重新生成")

    mode = entry.get("mode", "brush")
    press = float(entry.get("press", 0.075))
    wait = float(entry.get("wait", 0.075))
    min_gain = int(entry.get("min_gain", 1) or 1)
    merge_threshold = float(entry.get("merge_threshold", 0.0) or 0.0)
    return_home_per_layer = bool(entry.get("return_home_per_layer", False))

    if mode == "pixel":
        commands, _ = generate_pixel_commands(
            source_path,
            press,
            wait,
            merge_threshold,
            return_home_per_layer=return_home_per_layer,
            start_layer_index=layer_index,
        )
    else:
        commands, _ = generate_brush_commands(
            source_path,
            press,
            wait,
            min_gain,
            merge_threshold,
            return_home_per_layer=return_home_per_layer,
            start_layer_index=layer_index,
        )

    out_path.write_text("\n".join(commands), encoding="utf-8")
    return out_path, len(commands), False


def draw_worker(
    sequence_id,
    macro_file="macro.txt",
    start_message="正在发送绘画按键序列",
    prep_seconds=0.0,
):
    entry = find_entry(sequence_id)
    if not entry:
        draw_state.update(
            status="error",
            message="找不到按键序列",
            error="sequence not found",
            finished_at=now_text(),
            benchmark=None,
        )
        return

    draw_started_perf = time.perf_counter()
    bench_segments = {
        "prep_seconds": float(prep_seconds or 0.0),
        "read_seconds": 0.0,
        "controller_wait_seconds": 0.0,
        "macro_active_seconds": 0.0,
        "paused_seconds": 0.0,
        "chunks_count": 0,
    }

    macro_path = JOBS_ROOT / sequence_id / macro_file
    t_read = time.perf_counter()
    lines = flatten_macro_lines(macro_path.read_text(encoding="utf-8").splitlines())
    bench_segments["read_seconds"] = time.perf_counter() - t_read

    total = len(lines)
    sent_lines = 0
    started_monotonic = time.monotonic()
    draw_state.update(
        status="running",
        sequence_id=sequence_id,
        sent_lines=0,
        total_lines=total,
        percent=0,
        lines_per_second=0.0,
        eta_seconds=None,
        message=start_message,
        error=None,
        started_at=now_text(),
        finished_at=None,
        pause_requested=False,
        cancel_requested=False,
        benchmark=None,
    )
    try:
        t_ctrl = time.perf_counter()
        nx, controller_index = controller.require_connected()
        with controller.macro_lock:
            bench_segments["controller_wait_seconds"] = time.perf_counter() - t_ctrl
            chunk_start = 0
            while chunk_start < total:
                t_pause = time.perf_counter()
                if draw_state.wait_while_paused():
                    bench_segments["paused_seconds"] += time.perf_counter() - t_pause
                    draw_state.update(message=start_message)
                if draw_state.is_cancel_requested():
                    break

                hard_end = min(chunk_start + MACRO_CHUNK_SIZE, total)
                chunk_end = chunk_slice_end(lines, chunk_start, hard_end)
                chunk_lines = lines[chunk_start:chunk_end]
                chunk_text = "\n".join(chunk_lines)

                if not chunk_text.strip():
                    update_draw_progress(started_monotonic, chunk_end, total)
                    chunk_start = chunk_end
                    sent_lines = chunk_end
                    continue

                t_chunk = time.perf_counter()
                result = run_macro_chunk_until_done_or_interrupted(nx, controller_index, chunk_text)
                bench_segments["macro_active_seconds"] += time.perf_counter() - t_chunk
                bench_segments["chunks_count"] += 1
                if result == "completed":
                    update_draw_progress(
                        started_monotonic,
                        chunk_end,
                        total,
                        message=f"正在发送第 {chunk_end}/{total} 行",
                    )
                    chunk_start = chunk_end
                    sent_lines = chunk_end
                    continue

                update_draw_progress(
                    started_monotonic,
                    chunk_end,
                    total,
                    message=(
                        f"已终止在第 {chunk_end}/{total} 行附近，后续按键未发送"
                        if result == "cancelled"
                        else f"已暂停在第 {chunk_end}/{total} 行附近，后续按键未发送"
                    ),
                )
                chunk_start = chunk_end
                sent_lines = chunk_end
                if result == "cancelled":
                    break
                t_pause = time.perf_counter()
                draw_state.wait_while_paused()
                bench_segments["paused_seconds"] += time.perf_counter() - t_pause
                draw_state.update(message=start_message)
                if draw_state.is_cancel_requested():
                    break
        if draw_state.is_cancel_requested():
            sent_lines = draw_state.snapshot().get("sent_lines", sent_lines) or sent_lines
            speed, eta = draw_speed_stats(started_monotonic, sent_lines, total)
            benchmark = finalize_draw_benchmark(
                bench_segments, draw_started_perf, sent_lines, total, "cancelled"
            )
            log_draw_benchmark(sequence_id, benchmark)
            draw_state.update(
                status="cancelled",
                lines_per_second=speed,
                eta_seconds=eta,
                message="绘画已终止，后续按键未发送",
                finished_at=now_text(),
                pause_requested=False,
                cancel_requested=False,
                benchmark=benchmark,
            )
            return
        sent_lines = total
        benchmark = finalize_draw_benchmark(
            bench_segments, draw_started_perf, sent_lines, total, "completed"
        )
        log_draw_benchmark(sequence_id, benchmark)
        draw_state.update(
            status="done",
            percent=100,
            lines_per_second=draw_speed_stats(started_monotonic, total, total)[0],
            eta_seconds=0,
            message="绘画按键序列发送完成",
            finished_at=now_text(),
            pause_requested=False,
            cancel_requested=False,
            benchmark=benchmark,
        )
    except Exception as exc:
        log_exception(f"Drawing macro send failed for sequence {sequence_id}", exc)
        benchmark = finalize_draw_benchmark(
            bench_segments, draw_started_perf, sent_lines, total, "error"
        )
        log_draw_benchmark(sequence_id, benchmark)
        draw_state.update(
            status="error",
            message="绘画发送失败",
            error=str(exc),
            finished_at=now_text(),
            pause_requested=False,
            cancel_requested=False,
            benchmark=benchmark,
        )


@app.route("/", methods=["GET"])
def index():
    lang = request.cookies.get("tomodachi_lang", "") or ""
    if lang not in ("zh", "en"):
        lang = "zh"
    html_lang = "zh-CN" if lang == "zh" else "en"
    return render_template_string(
        INDEX_HTML,
        buttons=VALID_BUTTONS,
        i18n_catalog=I18N_CATALOG,
        html_lang=html_lang,
        ui_lang=lang,
        zh_to_en_ui=ZH_TO_EN_UI,
    )


@app.errorhandler(Exception)
def api_unhandled_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    log_exception("Unhandled WebUI request error", exc)
    return jsonify({"error": str(exc)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    entries = load_entries()
    return jsonify(
        {
            "controller": controller.snapshot(),
            "draw": draw_state.snapshot(),
            "sequence_count": len(entries),
        }
    )


@app.route("/api/controller/connect", methods=["POST"])
def api_connect():
    return jsonify({"controller": controller.start_connect()})


@app.route("/api/controller/reconnect", methods=["POST"])
def api_reconnect():
    if draw_state.is_busy():
        return jsonify({"error": "绘画进行中，请先终止或等待结束后再重新连接手柄"}), 409
    return jsonify({"controller": controller.reconnect()})


@app.route("/api/controller/lr", methods=["POST"])
def api_lr():
    if draw_state.is_busy():
        return jsonify({"error": "绘画进行中，暂时不能发送单独按键"}), 409
    try:
        controller.run_macro("L R 0.1s\n0.2s")
        return jsonify({"ok": True})
    except Exception as exc:
        log_exception("Failed to send L+R", exc)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/controller/button", methods=["POST"])
def api_button():
    if draw_state.is_busy():
        return jsonify({"error": "绘画进行中，暂时不能发送单独按键"}), 409
    data = request.get_json(silent=True) or {}
    button = str(data.get("button", "")).upper()
    if button not in VALID_BUTTONS:
        return jsonify({"error": "不支持的按键"}), 400
    try:
        controller.run_macro(f"{button} 0.1s\n0.1s")
        return jsonify({"ok": True})
    except Exception as exc:
        log_exception(f"Failed to send button {button}", exc)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sequences", methods=["GET"])
def api_sequences():
    return jsonify({"entries": load_entries()})


@app.route("/api/sequences", methods=["POST"])
def api_create_sequence():
    image = request.files.get("image")
    if not image:
        return jsonify({"error": "请选择图片"}), 400

    mode = request.form.get("mode", "brush")
    if mode not in ("pixel", "brush"):
        return jsonify({"error": "mode 只能是 pixel 或 brush"}), 400

    try:
        press = float(request.form.get("press", "0.1"))
        wait = float(request.form.get("wait", "0.1"))
        min_gain = int(request.form.get("min_gain", "1"))
        merge_threshold = float(request.form.get("merge_threshold", "0"))
        return_home_per_layer = str(request.form.get("return_home_per_layer", "")).lower() in (
            "1",
            "true",
            "on",
            "yes",
        )
        if press <= 0 or wait <= 0 or min_gain < 1 or merge_threshold < 0:
            raise ValueError
    except ValueError:
        return jsonify({"error": "参数必须为正数，合并阈值可以为 0"}), 400

    try:
        entry = generate_sequence(
            image,
            mode,
            press,
            wait,
            min_gain,
            merge_threshold,
            return_home_per_layer,
        )
        return jsonify({"entry": entry})
    except Exception as exc:
        log_exception("Failed to generate sequence", exc)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sequences/stability-test", methods=["POST"])
def api_create_stability_sequence():
    data = request.get_json(silent=True) or {}
    try:
        pairs = int(data.get("pairs", 0))
        press = float(data.get("press", 0))
        wait = float(data.get("wait", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "pairs / press / wait 参数无效"}), 400
    try:
        entry = create_stability_test_job(pairs, press, wait)
        return jsonify({"entry": entry})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log_exception("Failed to create stability test job", exc)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sequences/<sequence_id>", methods=["GET", "DELETE"])
def api_sequence(sequence_id):
    if request.method == "DELETE":
        job_dir = resolve_job_directory(sequence_id)
        if not job_dir:
            return jsonify({"error": "找不到按键序列"}), 404
        snap = draw_state.snapshot()
        if draw_state.is_busy() and snap.get("sequence_id") == sequence_id:
            return jsonify({"error": "该序列正在绘制或终止中，请先停止绘画后再删除"}), 409
        try:
            shutil.rmtree(job_dir)
        except OSError as exc:
            LOGGER.warning("Failed to delete job %s: %s", sequence_id, exc)
            return jsonify({"error": str(exc)}), 500
        LOGGER.info("Deleted job directory %s", sequence_id)
        return jsonify({"ok": True})

    entry = find_entry(sequence_id)
    if not entry:
        return jsonify({"error": "找不到按键序列"}), 404
    return jsonify({"entry": entry})


@app.route("/api/sequences/<sequence_id>/layers", methods=["GET"])
def api_sequence_layers(sequence_id):
    entry = find_entry(sequence_id)
    if not entry:
        return jsonify({"error": "找不到按键序列"}), 404
    return jsonify({"layers": entry.get("layers", [])})


@app.route("/api/draw/<sequence_id>", methods=["POST"])
def api_draw(sequence_id):
    if draw_state.is_busy():
        return jsonify({"error": "已有绘画任务正在进行"}), 409
    if not find_entry(sequence_id):
        return jsonify({"error": "找不到按键序列"}), 404
    try:
        controller.require_connected()
    except Exception as exc:
        log_exception("Cannot start drawing because controller is not ready", exc)
        return jsonify({"error": str(exc)}), 400
    thread = threading.Thread(
        target=draw_worker,
        args=(sequence_id, "macro.txt", "正在发送整图绘画按键序列"),
        daemon=True,
    )
    thread.start()
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/api/draw/<sequence_id>/from-layer/<layer_id>", methods=["POST"])
def api_draw_from_layer(sequence_id, layer_id):
    if draw_state.is_busy():
        return jsonify({"error": "已有绘画任务正在进行"}), 409
    entry = find_entry(sequence_id)
    if not entry:
        return jsonify({"error": "找不到按键序列"}), 404
    layer = find_layer(entry, layer_id)
    if not layer:
        return jsonify({"error": "找不到颜色层"}), 404
    try:
        controller.require_connected()
    except Exception as exc:
        log_exception("Cannot start partial drawing because controller is not ready", exc)
        return jsonify({"error": str(exc)}), 400
    t_prep = time.perf_counter()
    try:
        macro_path, _line_count, was_cached = ensure_partial_macro(sequence_id, layer_id)
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log_exception("Failed to generate partial macro", exc)
        return jsonify({"error": str(exc)}), 500
    prep_seconds = 0.0 if was_cached else time.perf_counter() - t_prep
    color = layer.get("color") or []
    if len(color) == 3:
        message = (
            f"正在从颜色层 {layer_id} (H{color[0]} S{color[1]} V{color[2]}) 起绘制至最后"
        )
    else:
        message = f"正在从颜色层 {layer_id} 起绘制至最后"
    thread = threading.Thread(
        target=draw_worker,
        args=(sequence_id, macro_path.name, message),
        kwargs={"prep_seconds": prep_seconds},
        daemon=True,
    )
    thread.start()
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/api/draw/<sequence_id>/layer/<layer_id>", methods=["POST"])
def api_draw_layer(sequence_id, layer_id):
    if draw_state.is_busy():
        return jsonify({"error": "已有绘画任务正在进行"}), 409
    entry = find_entry(sequence_id)
    if not entry:
        return jsonify({"error": "找不到按键序列"}), 404
    layer = find_layer(entry, layer_id)
    if not layer:
        return jsonify({"error": "找不到颜色层"}), 404
    macro_file = layer.get("macro_file")
    if not macro_file:
        return jsonify({"error": "该颜色层缺少宏文件"}), 400
    try:
        controller.require_connected()
    except Exception as exc:
        log_exception("Cannot start layer drawing because controller is not ready", exc)
        return jsonify({"error": str(exc)}), 400
    color = layer.get("color") or []
    message = f"正在发送颜色层 {layer_id} 绘画按键序列"
    if len(color) == 3:
        message = f"正在发送颜色层 {layer_id} (H{color[0]} S{color[1]} V{color[2]}) 绘画按键序列"
    thread = threading.Thread(
        target=draw_worker,
        args=(sequence_id, macro_file, message),
        daemon=True,
    )
    thread.start()
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/api/draw/pause", methods=["POST"])
def api_draw_pause():
    if not draw_state.request_pause():
        return jsonify({"error": "当前没有正在运行的绘画任务"}), 409
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/api/draw/resume", methods=["POST"])
def api_draw_resume():
    if not draw_state.resume():
        return jsonify({"error": "当前没有已暂停的绘画任务"}), 409
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/api/draw/stop", methods=["POST"])
def api_draw_stop():
    if not draw_state.request_cancel():
        return jsonify({"error": "当前没有可以终止的绘画任务"}), 409
    return jsonify({"draw": draw_state.snapshot()})


@app.route("/files/<sequence_id>/<path:filename>", methods=["GET"])
def files(sequence_id, filename):
    job_dir = resolve_job_directory(sequence_id)
    if not job_dir:
        return jsonify({"error": "找不到文件"}), 404
    target = (job_dir / filename).resolve()
    try:
        target.relative_to(job_dir.resolve())
    except ValueError:
        return jsonify({"error": "找不到文件"}), 404
    if not target.is_file():
        return jsonify({"error": "找不到文件"}), 404
    return send_file(target)


def parse_args():
    parser = argparse.ArgumentParser(description="Run the TomodachiDrawing WebUI.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=50000)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def cleanup():
    controller.shutdown()


def handle_shutdown_signal(signum, _frame):
    LOGGER.info("Received signal %s, shutting down WebUI", signum)
    cleanup()
    raise SystemExit(128 + signum)


def install_shutdown_handlers():
    atexit.register(cleanup)
    if threading.current_thread() is not threading.main_thread():
        return
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, handle_shutdown_signal)


def main():
    configure_logging()
    configure_multiprocessing()
    install_shutdown_handlers()
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    if args.debug:
        LOGGER.info("Debug mode enabled; Flask reloader is disabled for nxbt compatibility")
    try:
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            threaded=True,
            use_reloader=False,
            request_handler=QuietStatusRequestHandler,
        )
    finally:
        cleanup()


if __name__ == "__main__":
    main()
