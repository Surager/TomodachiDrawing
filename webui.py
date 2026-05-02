import argparse
import atexit
import importlib
import json
import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, render_template_string, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler
from werkzeug.utils import secure_filename

from main import generate_pixel_commands
from main_brush import generate_commands as generate_brush_commands
from sequence_preview import render_macro_preview
from tomodachi_common import (
    build_color_layers,
    color_key_to_rgb,
    dump_point_layers,
    load_quantized_image,
    save_quantized_preview,
)


ROOT = Path(__file__).resolve().parent
WEBUI_ROOT = ROOT / "output" / "webui"
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


def import_nxbt():
    try:
        return importlib.import_module("nxbt")
    except ModuleNotFoundError:
        for candidate in (os.environ.get("NXBT_SOURCE_DIR"), r"D:\aaWorkspace\git\github\nxbt"):
            if not candidate:
                continue
            source_dir = Path(candidate)
            if source_dir.exists():
                sys.path.insert(0, str(source_dir))
                return importlib.import_module("nxbt")
        raise


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


INDEX_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TomodachiDrawing WebUI</title>
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
  </style>
</head>
<body>
  <header>
    <h1>TomodachiDrawing WebUI</h1>
    <div class="muted">已生成按键序列：<strong id="sequenceCount">0</strong></div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>虚拟手柄</h2>
        <div class="status">
          <strong id="controllerText">读取中</strong>
          <span id="controllerMessage" class="muted"></span>
          <span id="controllerError" class="muted error"></span>
        </div>
        <div class="row" style="margin-top: 12px">
          <button class="primary" id="connectBtn">等待主机连接虚拟手柄</button>
          <button id="reconnectBtn">重新连接手柄</button>
          <button id="lrBtn">发送 L+R</button>
        </div>
      </section>

      <section>
        <h2>发送 Switch 按键</h2>
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
        <h2>上传图片并生成序列</h2>
        <form id="uploadForm">
          <label for="imageInput">图片文件，尺寸必须为 256x256</label>
          <input id="imageInput" name="image" type="file" accept="image/*" required>
          <label for="modeInput">生成模式</label>
          <select id="modeInput" name="mode">
            <option value="brush">brush：优先使用大笔刷</option>
            <option value="pixel">pixel：逐像素绘制</option>
          </select>
          <div class="row">
            <div style="flex:1">
              <label for="pressInput">按下时长</label>
              <input id="pressInput" name="press" type="number" step="0.001" min="0.001" value="0.075">
            </div>
            <div style="flex:1">
              <label for="waitInput">等待时长</label>
              <input id="waitInput" name="wait" type="number" step="0.001" min="0.001" value="0.075">
            </div>
          </div>
          <label for="minGainInput">brush 最小收益</label>
          <input id="minGainInput" name="min_gain" type="number" step="1" min="1" value="1">
          <label for="mergeThresholdInput">合并相近颜色阈值</label>
          <input id="mergeThresholdInput" name="merge_threshold" type="number" step="1" min="0" value="0">
          <label style="display:flex; gap:8px; align-items:center; color: var(--text); margin-top: 12px;">
            <input id="returnHomePerLayerInput" name="return_home_per_layer" type="checkbox" checked style="width:auto; min-height:auto;">
            每个颜色层结束后回到 (0,0) 并把颜色归到底部
          </label>
          <div class="row" style="margin-top: 12px">
            <button class="blue" type="submit">生成按键序列</button>
          </div>
        </form>
        <p id="uploadMessage" class="muted"></p>
      </section>
    </div>

    <section>
      <div class="layout">
        <div>
          <h2>按键序列</h2>
          <div id="sequenceList" class="list"></div>
        </div>
        <div>
          <h2 id="detailTitle">请选择一个条目</h2>
          <div id="detailMeta" class="meta"></div>
          <div class="row">
            <button class="primary" id="drawBtn">绘制整图</button>
            <button class="blue" id="drawLayerBtn">绘制当前颜色层</button>
            <button id="pauseBtn">暂停</button>
            <button class="danger" id="stopBtn">终止</button>
            <a id="macroLink" class="muted" href="#" target="_blank" rel="noreferrer">查看 macro.txt</a>
            <a id="layerMacroLink" class="muted" href="#" target="_blank" rel="noreferrer">查看颜色层 macro.txt</a>
          </div>
          <div style="margin: 12px 0">
            <progress id="drawProgress" max="100" value="0"></progress>
            <div id="drawText" class="muted"></div>
          </div>
          <div class="previews">
            <figure>
              <figcaption>上传图片</figcaption>
              <img id="sourcePreview" class="preview" alt="上传图片预览">
            </figure>
            <figure>
              <figcaption>量化预览</figcaption>
              <img id="quantizedPreview" class="preview" alt="量化预览">
            </figure>
            <figure>
              <figcaption>按键序列回放预览</figcaption>
              <img id="sequencePreview" class="preview" alt="按键序列回放预览">
            </figure>
            <figure>
              <figcaption>当前颜色层预览</figcaption>
              <img id="layerPreview" class="preview" alt="当前颜色层预览">
            </figure>
          </div>
          <div style="margin-top: 12px">
            <h2 style="margin-bottom: 8px">颜色层列表</h2>
            <div id="layerList" class="list" style="max-height: 260px"></div>
          </div>
        </div>
      </div>
    </section>
  </main>

  <script>
    const buttons = {{ buttons|tojson }};
    let selectedId = null;
    let selectedLayerId = null;
    let entries = [];

    const el = id => document.getElementById(id);

    function statusText(status) {
      return {
        idle: '未创建虚拟手柄',
        starting: '正在创建虚拟手柄',
        waiting: '等待 Switch 连接',
        connected: '已连接',
        error: '连接错误'
      }[status] || status;
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
      if (!res.ok) throw new Error(data.error || data.message || '请求失败');
      return data;
    }

    async function pollStatus() {
      try {
        const data = await api('/api/status');
        const c = data.controller;
        const item = entries.find(entry => entry.id === selectedId);
        const hasLayer = !!(item && item.layers && item.layers.some(layer => layer.id === selectedLayerId));
        el('controllerText').textContent = statusText(c.status);
        el('controllerMessage').textContent = c.message || '';
        el('controllerError').textContent = c.error || '';
        el('sequenceCount').textContent = data.sequence_count;
        const drawBusy = data.draw.status === 'running' || data.draw.status === 'paused' || data.draw.status === 'stopping';
        el('connectBtn').disabled = drawBusy || c.status === 'starting' || c.status === 'waiting' || c.status === 'connected';
        el('reconnectBtn').disabled = drawBusy || c.status === 'starting';
        el('lrBtn').disabled = c.status !== 'connected' || drawBusy;
        el('drawBtn').disabled = !selectedId || c.status !== 'connected' || drawBusy;
        el('drawLayerBtn').disabled = !selectedId || !hasLayer || c.status !== 'connected' || drawBusy;
        el('pauseBtn').disabled = data.draw.status !== 'running' && data.draw.status !== 'paused';
        el('pauseBtn').textContent = data.draw.status === 'paused' ? '继续' : '暂停';
        el('stopBtn').disabled = data.draw.status !== 'running' && data.draw.status !== 'paused';
        el('drawProgress').value = data.draw.percent || 0;
        const speed = Number(data.draw.lines_per_second || 0).toFixed(2);
        const eta = formatEta(data.draw.eta_seconds);
        el('drawText').textContent = `${data.draw.message || ''} ${data.draw.percent || 0}% · ${data.draw.sent_lines || 0}/${data.draw.total_lines || 0} 行 · ${speed} 行/秒 · 剩余 ${eta}`;
      } catch (err) {
        el('controllerText').textContent = '状态读取失败';
        el('controllerError').textContent = err.message;
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
        list.innerHTML = '<p class="muted">还没有生成按键序列</p>';
        return;
      }
      for (const item of entries) {
        const button = document.createElement('button');
        button.className = 'item' + (item.id === selectedId ? ' active' : '');
        button.innerHTML = `<strong>${item.source_name}</strong><span class="muted">${item.mode} · ${item.macro_lines} 行 · ${item.created_at}</span>`;
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
        el('detailTitle').textContent = '请选择一个条目';
        el('detailMeta').innerHTML = '';
        for (const id of ['sourcePreview', 'quantizedPreview', 'sequencePreview', 'layerPreview']) el(id).removeAttribute('src');
        el('macroLink').href = '#';
        el('layerMacroLink').href = '#';
        el('layerList').innerHTML = '<p class="muted">请选择一个条目</p>';
        selectedLayerId = null;
        return;
      }
      const layers = item.layers || [];
      if (!layers.some(layer => layer.id === selectedLayerId)) {
        selectedLayerId = layers.length ? layers[0].id : null;
      }
      const selectedLayer = layers.find(layer => layer.id === selectedLayerId) || null;
      el('detailTitle').textContent = item.source_name;
      el('detailMeta').innerHTML = `
        <div><span class="muted">模式</span><b>${item.mode}</b></div>
        <div><span class="muted">颜色层</span><b>${item.colors}</b></div>
        <div><span class="muted">宏行数</span><b>${item.macro_lines}</b></div>
        <div><span class="muted">合并阈值</span><b>${item.merge_threshold || 0}</b></div>
        <div><span class="muted">层后归零点</span><b>${item.return_home_per_layer ? '开启' : '关闭'}</b></div>`;
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
        list.innerHTML = '<p class="muted">该条目暂无颜色层拆分数据</p>';
        return;
      }
      for (const layer of layers) {
        const color = layer.color || ['?', '?', '?'];
        const button = document.createElement('button');
        button.className = 'item' + (layer.id === activeLayerId ? ' active' : '');
        button.innerHTML = `<strong>${layer.id} · H${color[0]} S${color[1]} V${color[2]}</strong><span class="muted">${layer.count || 0} 像素 · ${layer.macro_lines || 0} 行</span>`;
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
            alert(err.message);
          }
        };
      }
    }

    el('connectBtn').onclick = async () => {
      try { await api('/api/controller/connect', {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('reconnectBtn').onclick = async () => {
      try { await api('/api/controller/reconnect', {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('lrBtn').onclick = async () => {
      try { await api('/api/controller/lr', {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('drawBtn').onclick = async () => {
      if (!selectedId) return;
      try { await api(`/api/draw/${selectedId}`, {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('drawLayerBtn').onclick = async () => {
      if (!selectedId || !selectedLayerId) return;
      try { await api(`/api/draw/${selectedId}/layer/${selectedLayerId}`, {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('pauseBtn').onclick = async () => {
      try {
        const status = (await api('/api/status')).draw.status;
        const path = status === 'paused' ? '/api/draw/resume' : '/api/draw/pause';
        await api(path, {method: 'POST'});
      } catch (err) {
        alert(err.message);
      }
      pollStatus();
    };

    el('stopBtn').onclick = async () => {
      try { await api('/api/draw/stop', {method: 'POST'}); }
      catch (err) { alert(err.message); }
      pollStatus();
    };

    el('uploadForm').onsubmit = async event => {
      event.preventDefault();
      el('uploadMessage').textContent = '正在生成，请稍候';
      try {
        const form = new FormData(event.currentTarget);
        const res = await fetch('/api/sequences', {method: 'POST', body: form});
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || '生成失败');
        selectedId = data.entry.id;
        el('uploadMessage').textContent = `已生成 ${data.entry.macro_lines} 行按键序列`;
        await loadEntries();
      } catch (err) {
        el('uploadMessage').textContent = err.message;
      }
    };

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
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    source_name = secure_filename(image_storage.filename) or "image.png"
    raw_source_path = job_dir / f"upload_{source_name}"
    source_path = job_dir / "source.png"
    image_storage.save(raw_source_path)

    from PIL import Image

    with Image.open(raw_source_path) as image:
        image.convert("RGBA").save(source_path)
    keys, opaque = load_quantized_image(source_path, merge_threshold=merge_threshold)
    layers = build_color_layers(keys, opaque)

    quantized_preview_path = job_dir / "quantized_preview.png"
    point_dump_path = job_dir / "points.json"
    macro_path = job_dir / "macro.txt"
    sequence_preview_path = job_dir / "sequence_preview.png"
    layer_dir = job_dir / "layers"
    layer_dir.mkdir(parents=True, exist_ok=True)

    save_quantized_preview(keys, opaque, quantized_preview_path)
    dump_point_layers(layers, point_dump_path)

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

    macro_path.write_text("\n".join(commands), encoding="utf-8")
    render_macro_preview(macro_path, sequence_preview_path)

    layer_entries = []
    for index, layer in enumerate(layers):
        layer_id = layer_id_from_index(index)
        h, s, v = layer.key
        stem = f"{layer_id}_h{h:03d}_s{s:03d}_v{v:03d}"
        layer_source_path = layer_dir / f"{stem}_source.png"
        layer_macro_path = layer_dir / f"{stem}.txt"
        layer_preview_path = layer_dir / f"{stem}_preview.png"

        build_layer_image(layer).save(layer_source_path)
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


def draw_worker(sequence_id, macro_file="macro.txt", start_message="正在发送绘画按键序列"):
    entry = find_entry(sequence_id)
    if not entry:
        draw_state.update(
            status="error",
            message="找不到按键序列",
            error="sequence not found",
            finished_at=now_text(),
        )
        return

    macro_path = JOBS_ROOT / sequence_id / macro_file
    lines = macro_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
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
    )
    try:
        nx, controller_index = controller.require_connected()
        with controller.macro_lock:
            for chunk_start in range(0, total, MACRO_CHUNK_SIZE):
                if draw_state.wait_while_paused():
                    draw_state.update(message=start_message)
                if draw_state.is_cancel_requested():
                    break

                chunk_end = min(chunk_start + MACRO_CHUNK_SIZE, total)
                chunk_lines = lines[chunk_start:chunk_end]
                chunk_text = "\n".join(chunk_lines)

                if not chunk_text.strip():
                    update_draw_progress(started_monotonic, chunk_end, total)
                    continue

                result = run_macro_chunk_until_done_or_interrupted(nx, controller_index, chunk_text)
                if result == "completed":
                    update_draw_progress(
                        started_monotonic,
                        chunk_end,
                        total,
                        message=f"正在发送第 {chunk_end}/{total} 行",
                    )
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
                if result == "cancelled":
                    break
                draw_state.wait_while_paused()
                draw_state.update(message=start_message)
                if draw_state.is_cancel_requested():
                    break
        if draw_state.is_cancel_requested():
            speed, eta = draw_speed_stats(
                started_monotonic,
                draw_state.snapshot()["sent_lines"],
                total,
            )
            draw_state.update(
                status="cancelled",
                lines_per_second=speed,
                eta_seconds=eta,
                message="绘画已终止，后续按键未发送",
                finished_at=now_text(),
                pause_requested=False,
                cancel_requested=False,
            )
            return
        draw_state.update(
            status="done",
            percent=100,
            lines_per_second=draw_speed_stats(started_monotonic, total, total)[0],
            eta_seconds=0,
            message="绘画按键序列发送完成",
            finished_at=now_text(),
            pause_requested=False,
            cancel_requested=False,
        )
    except Exception as exc:
        log_exception(f"Drawing macro send failed for sequence {sequence_id}", exc)
        draw_state.update(
            status="error",
            message="绘画发送失败",
            error=str(exc),
            finished_at=now_text(),
            pause_requested=False,
            cancel_requested=False,
        )


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, buttons=VALID_BUTTONS)


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


@app.route("/api/sequences/<sequence_id>", methods=["GET"])
def api_sequence(sequence_id):
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
    if not find_entry(sequence_id):
        return jsonify({"error": "找不到文件"}), 404
    job_dir = JOBS_ROOT / sequence_id
    return send_from_directory(job_dir, filename)


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
