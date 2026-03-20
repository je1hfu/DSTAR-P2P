from __future__ import annotations

import queue
import random
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

import serial
from serial.tools import list_ports

try:
    from dotenv import dotenv_values
except ModuleNotFoundError:
    dotenv_values = None


APP_TITLE = "DSTAR-P2P GUI Alpha"
ENV_PATH = Path(__file__).resolve().with_name(".env")
DB_PATH = Path(__file__).resolve().with_name("stations.db")
RESPONSE_DELAYS = [3, 6, 9, 12, 15]
BEACON_INTERVAL_RANGE = (30, 90)
QRV_DELAYS = [2, 4, 6, 8, 10]
STALE_AFTER = timedelta(minutes=5)


@dataclass
class AppSettings:
    callsign: str
    port: str
    gl: str
    baud_rate: int


def load_env_defaults(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}

    if dotenv_values is not None:
        values = dotenv_values(env_path)
        return {key: (value or "").strip() for key, value in values.items()}

    defaults: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        defaults[key.strip()] = value.strip().strip('"').strip("'")
    return defaults


def read_station_rows(db_path: Path) -> list[dict[str, str]]:
    if not db_path.exists():
        return []

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT callsign, last_seen, status, gl, gl_updated_at, query_count
                FROM stations
                ORDER BY last_seen DESC
                """
            )
            rows = cursor.fetchall()
    except sqlite3.Error:
        return []

    return [
        {
            "callsign": row[0] or "",
            "last_seen": row[1] or "",
            "status": row[2] or "",
            "gl": row[3] or "",
            "gl_updated_at": row[4] or "",
            "query_count": str(row[5] or 0),
        }
        for row in rows
    ]


class DStarBackend(threading.Thread):
    def __init__(self, settings: AppSettings, event_queue: queue.Queue, db_path: Path) -> None:
        super().__init__(daemon=True)
        self.settings = settings
        self.event_queue = event_queue
        self.db_path = db_path
        self.stop_event = threading.Event()
        self.db_lock = threading.Lock()
        self.serial_lock = threading.Lock()
        self.responded_callsigns: set[str] = set()
        self.conn: Optional[sqlite3.Connection] = None
        self.ser: Optional[serial.Serial] = None
        self.worker_threads: list[threading.Thread] = []

    def run(self) -> None:
        try:
            self._open_serial()
            self._open_database()
            self._ensure_schema()
            self._emit_connection_state(True, f"{self.settings.port} に接続しました")
            self._log("INFO", f"Using callsign {self.settings.callsign} / GL {self.settings.gl}")
            self._emit_stations_snapshot()

            self.worker_threads = [
                threading.Thread(target=self._auto_beacon_loop, daemon=True),
                threading.Thread(target=self._listen_loop, daemon=True),
                threading.Thread(target=self._query_loop, daemon=True),
            ]
            for thread in self.worker_threads:
                thread.start()

            while not self.stop_event.is_set():
                self.stop_event.wait(0.25)
        except Exception as exc:
            self._log("ERROR", f"接続開始に失敗しました: {exc}")
        finally:
            self.stop_event.set()
            self._close_resources()
            self._emit_connection_state(False, "通信を停止しました")

    def stop(self) -> None:
        self.stop_event.set()

    def _open_serial(self) -> None:
        self.ser = serial.Serial(self.settings.port, self.settings.baud_rate, timeout=1)
        self._log("INFO", f"Serial open: {self.settings.port} @ {self.settings.baud_rate}")

    def _open_database(self) -> None:
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._log("INFO", f"Database ready: {self.db_path.name}")

    def _ensure_schema(self) -> None:
        if self.conn is None:
            raise RuntimeError("Database is not available.")

        with self.db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS stations (
                    callsign TEXT PRIMARY KEY,
                    last_seen TEXT,
                    status TEXT,
                    gl TEXT,
                    gl_updated_at TEXT,
                    query_count INTEGER DEFAULT 0
                )
                """
            )
            self.conn.commit()

    def _close_resources(self) -> None:
        if self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
                    self._log("INFO", "Serial port closed")
            except serial.SerialException:
                pass

        if self.conn is not None:
            try:
                self.conn.close()
                self._log("INFO", "Database closed")
            except sqlite3.Error:
                pass

    def _log(self, level: str, message: str) -> None:
        self.event_queue.put(
            {
                "type": "log",
                "level": level,
                "message": message,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def _emit_connection_state(self, connected: bool, message: str) -> None:
        self.event_queue.put(
            {
                "type": "connection",
                "connected": connected,
                "message": message,
            }
        )

    def _emit_stations_snapshot(self) -> None:
        self.event_queue.put(
            {
                "type": "stations",
                "rows": self._fetch_station_rows(),
            }
        )

    def _fetch_station_rows(self) -> list[dict[str, str]]:
        if self.conn is None:
            return []

        with self.db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                SELECT callsign, last_seen, status, gl, gl_updated_at, query_count
                FROM stations
                ORDER BY last_seen DESC
                """
            )
            rows = cursor.fetchall()

        return [
            {
                "callsign": row[0] or "",
                "last_seen": row[1] or "",
                "status": row[2] or "",
                "gl": row[3] or "",
                "gl_updated_at": row[4] or "",
                "query_count": str(row[5] or 0),
            }
            for row in rows
        ]

    def _add_crc(self, message: str) -> str:
        stripped_message = message.strip()
        crc = zlib.crc32(stripped_message.encode()) & 0xFFFFFFFF
        return f"{stripped_message} CRC={crc:08X}\n"

    def _verify_crc(self, line: str) -> tuple[bool, str]:
        if "CRC=" not in line:
            return True, line

        try:
            parts = line.rsplit("CRC=", 1)
            content = parts[0].strip()
            received_crc = parts[1].split()[0].strip()
            calculated_crc = f"{zlib.crc32(content.encode()) & 0xFFFFFFFF:08X}"
            return calculated_crc == received_crc, content
        except Exception:
            return False, ""

    def _send_message(self, message: str) -> None:
        if self.ser is None:
            raise RuntimeError("Serial connection is not available.")

        final_message = self._add_crc(message)
        with self.serial_lock:
            self.ser.write(final_message.encode())
        self._log("TX", final_message.strip())

    def _upsert_station_seen(self, sender: str) -> None:
        if self.conn is None:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO stations (callsign, last_seen, status, gl, gl_updated_at, query_count)
                VALUES (?, ?, ?, NULL, NULL, 0)
                ON CONFLICT(callsign) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    status = excluded.status
                """,
                (sender, now, "active"),
            )
            self.conn.commit()

        if sender not in self.responded_callsigns:
            self.responded_callsigns.add(sender)
            self._log("INFO", f"応答局を記録: {sender}")
        self._emit_stations_snapshot()

    def _upsert_station_gl(self, from_callsign: str, gl: str) -> None:
        if self.conn is None:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO stations (callsign, last_seen, status, gl, gl_updated_at, query_count)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(callsign) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    status = excluded.status,
                    gl = excluded.gl,
                    gl_updated_at = excluded.gl_updated_at,
                    query_count = 0
                """,
                (from_callsign, now, "active", gl, now),
            )
            self.conn.commit()

        self._log("INFO", f"GL情報を更新: {from_callsign} -> {gl}")
        self._emit_stations_snapshot()

    def _increment_query_count(self, callsign: str) -> None:
        if self.conn is None:
            return

        with self.db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE stations SET query_count = query_count + 1 WHERE callsign = ?",
                (callsign,),
            )
            self.conn.commit()
        self._emit_stations_snapshot()

    def _auto_beacon_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._send_message(f"CQ de {self.settings.callsign}\n")
                wait_seconds = random.randint(*BEACON_INTERVAL_RANGE)
                self._log("INFO", f"次回ビーコンまで {wait_seconds} 秒")
                if self.stop_event.wait(wait_seconds):
                    break
            except Exception as exc:
                self._log("ERROR", f"ビーコン送信エラー: {exc}")
                if self.stop_event.wait(1):
                    break

    def _listen_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.ser is None:
                    break

                with self.serial_lock:
                    raw = self.ser.readline().decode(errors="ignore").strip()

                if not raw:
                    continue

                self._log("RX", raw)
                ok, line = self._verify_crc(raw)
                if not ok:
                    self._log("ERROR", f"CRC検証失敗: {raw}")
                    continue

                self._handle_received_line(line)
            except serial.SerialException as exc:
                self._log("ERROR", f"シリアル受信エラー: {exc}")
                self.stop_event.set()
            except Exception as exc:
                self._log("ERROR", f"受信処理エラー: {exc}")

    def _handle_received_line(self, line: str) -> None:
        if line.startswith("CQ de ") and self.settings.callsign not in line:
            sender = line.split("CQ de ", 1)[1].strip()
            delay = random.choice(RESPONSE_DELAYS)
            self._log("INFO", f"{sender} へ CQ 応答を {delay} 秒後に送信")
            if self.stop_event.wait(delay):
                return
            self._send_message(f"{sender} de {self.settings.callsign}\n")

        if line.startswith(f"QRV? {self.settings.callsign} de "):
            from_call = line.split(f"QRV? {self.settings.callsign} de ", 1)[1].strip()
            self._log("INFO", f"{from_call} から QRV? を受信")
            self._send_message(f"{from_call} de {self.settings.callsign} GL={self.settings.gl} K\n")

        if line.startswith(f"{self.settings.callsign} de "):
            sender = line.split("de ", 1)[1].split()[0].strip()
            self._upsert_station_seen(sender)

        if "GL=" in line and line.endswith("K"):
            header, gl_part = line.split("GL=", 1)
            gl = gl_part.split()[0].strip()
            parts = header.strip().split("de")
            if len(parts) == 2:
                to_callsign = parts[0].strip()
                from_callsign = parts[1].strip()
                if to_callsign == self.settings.callsign:
                    self._upsert_station_gl(from_callsign, gl)
                else:
                    self._log("INFO", f"自局宛てではない GL 応答を受信: {line}")

    def _query_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.conn is None:
                    break

                five_min_ago = (datetime.now() - STALE_AFTER).strftime("%Y-%m-%d %H:%M:%S")
                with self.db_lock:
                    cursor = self.conn.cursor()
                    cursor.execute(
                        """
                        SELECT callsign, query_count
                        FROM stations
                        WHERE (gl IS NULL OR gl_updated_at IS NULL OR gl_updated_at < ?)
                        """,
                        (five_min_ago,),
                    )
                    rows = cursor.fetchall()

                for callsign, query_count in rows:
                    if self.stop_event.is_set():
                        return
                    if query_count >= 2:
                        continue
                    delay = random.choice(QRV_DELAYS)
                    self._log("INFO", f"{callsign} へ QRV? を {delay} 秒後に送信")
                    if self.stop_event.wait(delay):
                        return
                    self._send_message(f"QRV? {callsign} de {self.settings.callsign}\n")
                    self._increment_query_count(callsign)

                if self.stop_event.wait(20):
                    break
            except Exception as exc:
                self._log("ERROR", f"詳細問い合わせエラー: {exc}")
                if self.stop_event.wait(2):
                    break


class DStarGuiApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1220x760")
        self.root.minsize(1040, 640)
        self.root.configure(bg="#d8e6f4")

        self.event_queue: queue.Queue = queue.Queue()
        self.backend: Optional[DStarBackend] = None
        self.last_snapshot_signature: tuple[tuple[str, ...], ...] = ()

        env_defaults = load_env_defaults(ENV_PATH)
        self.callsign_var = tk.StringVar(value=env_defaults.get("DSTAR_CALLSIGN", ""))
        self.port_var = tk.StringVar(value=env_defaults.get("DSTAR_PORT", ""))
        self.gl_var = tk.StringVar(value=env_defaults.get("DSTAR_MY_GL", ""))
        self.baud_var = tk.StringVar(value=env_defaults.get("DSTAR_BAUD_RATE", "9600"))
        self.connection_state_var = tk.StringVar(value="待機中")

        self.entry_widgets: list[tk.Widget] = []

        self._configure_style()
        self._build_layout()
        self._refresh_ports()
        self._refresh_station_list_from_db(force=True)
        self._poll_events()
        self._schedule_db_refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        style.configure("App.TFrame", background="#d8e6f4")
        style.configure(
            "Card.TLabelframe",
            background="#f4f8fc",
            bordercolor="#9db7d1",
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background="#f4f8fc",
            foreground="#244767",
            font=("Yu Gothic UI", 10, "bold"),
        )
        style.configure("Form.TLabel", background="#f4f8fc", foreground="#27445f")
        style.configure("State.TLabel", background="#d8e6f4", foreground="#17324d", font=("Yu Gothic UI", 10, "bold"))
        style.configure("Connect.TButton", font=("Yu Gothic UI", 11, "bold"), padding=(18, 12))
        style.configure("Treeview", rowheight=28, font=("Consolas", 10), background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Yu Gothic UI", 10, "bold"), background="#c4d9ed", foreground="#17324d")
        style.map("Treeview", background=[("selected", "#9fd2ff")], foreground=[("selected", "#102235")])

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=1)

        top_frame = ttk.LabelFrame(self.root, text="設定", style="Card.TLabelframe", padding=16)
        top_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        top_frame.columnconfigure(1, weight=1)
        top_frame.columnconfigure(3, weight=1)
        top_frame.columnconfigure(5, weight=1)
        top_frame.columnconfigure(7, weight=1)

        self._add_form_field(top_frame, "Callsign", self.callsign_var, 0, 0)
        self.port_combo = ttk.Combobox(top_frame, textvariable=self.port_var, state="normal")
        self._place_custom_field(top_frame, "COM Port", self.port_combo, 0, 2)
        self.entry_widgets.append(self.port_combo)

        refresh_button = ttk.Button(top_frame, text="ポート更新", command=self._refresh_ports)
        refresh_button.grid(row=0, column=4, sticky="w", padx=(8, 0), pady=4)
        self.refresh_ports_button = refresh_button

        self._add_form_field(top_frame, "GL", self.gl_var, 1, 0)
        self._add_form_field(top_frame, "Baud Rate", self.baud_var, 1, 2)

        state_label = ttk.Label(
            top_frame,
            textvariable=self.connection_state_var,
            style="State.TLabel",
            anchor="e",
        )
        state_label.grid(row=1, column=5, columnspan=3, sticky="e", padx=(8, 0), pady=4)

        middle_frame = ttk.LabelFrame(self.root, text="Online Stations", style="Card.TLabelframe", padding=12)
        middle_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        middle_frame.columnconfigure(0, weight=1)
        middle_frame.rowconfigure(0, weight=1)

        columns = ("indicator", "callsign", "gl", "last_seen", "status")
        self.station_tree = ttk.Treeview(middle_frame, columns=columns, show="headings")
        self.station_tree.heading("indicator", text="")
        self.station_tree.heading("callsign", text="Callsign")
        self.station_tree.heading("gl", text="GL")
        self.station_tree.heading("last_seen", text="Last Seen")
        self.station_tree.heading("status", text="Status")
        self.station_tree.column("indicator", width=42, anchor="center", stretch=False)
        self.station_tree.column("callsign", width=220, anchor="w")
        self.station_tree.column("gl", width=140, anchor="center")
        self.station_tree.column("last_seen", width=220, anchor="center")
        self.station_tree.column("status", width=140, anchor="center")
        self.station_tree.tag_configure("active", foreground="#0c6e39")
        self.station_tree.tag_configure("stale", foreground="#6a7a89")
        self.station_tree.tag_configure("unknown", foreground="#6f5b14")

        tree_scrollbar = ttk.Scrollbar(middle_frame, orient="vertical", command=self.station_tree.yview)
        self.station_tree.configure(yscrollcommand=tree_scrollbar.set)
        self.station_tree.grid(row=0, column=0, sticky="nsew")
        tree_scrollbar.grid(row=0, column=1, sticky="ns")

        bottom_frame = ttk.LabelFrame(self.root, text="Live Log", style="Card.TLabelframe", padding=12)
        bottom_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 16))
        bottom_frame.columnconfigure(0, weight=1)
        bottom_frame.columnconfigure(1, weight=0)
        bottom_frame.rowconfigure(0, weight=1)

        self.log_text = ScrolledText(
            bottom_frame,
            wrap="word",
            font=("Consolas", 10),
            background="#102235",
            foreground="#f2f7fb",
            insertbackground="#f2f7fb",
            padx=12,
            pady=10,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.log_text.configure(state="disabled")
        self.log_text.tag_configure("TX", foreground="#7ec8ff")
        self.log_text.tag_configure("RX", foreground="#8af0a1")
        self.log_text.tag_configure("INFO", foreground="#d7e2ef")
        self.log_text.tag_configure("ERROR", foreground="#ff8f8f")

        button_panel = ttk.Frame(bottom_frame, style="App.TFrame")
        button_panel.grid(row=0, column=1, sticky="ns")
        button_panel.rowconfigure(0, weight=1)

        self.connect_button = ttk.Button(
            button_panel,
            text="接続",
            style="Connect.TButton",
            command=self._toggle_connection,
        )
        self.connect_button.grid(row=1, column=0, sticky="se", pady=(0, 4))

    def _add_form_field(
        self,
        parent: ttk.LabelFrame,
        label_text: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        entry = ttk.Entry(parent, textvariable=variable)
        self._place_custom_field(parent, label_text, entry, row, column)
        self.entry_widgets.append(entry)

    def _place_custom_field(
        self,
        parent: ttk.LabelFrame,
        label_text: str,
        widget: tk.Widget,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label_text, style="Form.TLabel").grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 8),
            pady=4,
        )
        widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=4)

    def _toggle_connection(self) -> None:
        if self.backend is not None and self.backend.is_alive():
            self.connection_state_var.set("停止処理中...")
            self.backend.stop()
            return

        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.backend = DStarBackend(settings=settings, event_queue=self.event_queue, db_path=DB_PATH)
        self._set_form_enabled(False)
        self.connect_button.configure(text="停止")
        self.connection_state_var.set("接続中...")
        self._append_log("INFO", f"接続開始: {settings.port} / {settings.callsign} / {settings.gl}")
        self.backend.start()

    def _collect_settings(self) -> AppSettings:
        callsign = self.callsign_var.get().strip().upper()
        port = self.port_var.get().strip()
        gl = self.gl_var.get().strip()
        baud_text = self.baud_var.get().strip()

        if not callsign:
            raise ValueError("Callsign を入力してください。")
        if not port:
            raise ValueError("COM Port を入力または選択してください。")
        if not gl:
            raise ValueError("GL を入力してください。")
        if not baud_text:
            raise ValueError("Baud Rate を入力してください。")

        try:
            baud_rate = int(baud_text)
        except ValueError as exc:
            raise ValueError("Baud Rate は整数で入力してください。") from exc

        return AppSettings(callsign=callsign, port=port, gl=gl, baud_rate=baud_rate)

    def _set_form_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        combo_state = "normal" if enabled else "disabled"
        for widget in self.entry_widgets:
            if widget is self.port_combo:
                widget.configure(state=combo_state)
            else:
                widget.configure(state=state)
        self.refresh_ports_button.configure(state=state)

    def _refresh_ports(self) -> None:
        ports = sorted(port.device for port in list_ports.comports())
        self.port_combo["values"] = ports
        if not self.port_var.get() and ports:
            self.port_var.set(ports[0])

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(150, self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        if event_type == "log":
            self._append_log(str(event["level"]), f"{event['timestamp']} {event['message']}")
        elif event_type == "connection":
            connected = bool(event["connected"])
            self.connection_state_var.set(str(event["message"]))
            if connected:
                self.connect_button.configure(text="停止")
            else:
                self.connect_button.configure(text="接続")
                self._set_form_enabled(True)
        elif event_type == "stations":
            rows = event.get("rows", [])
            if isinstance(rows, list):
                self._render_station_rows(rows)

    def _append_log(self, level: str, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{level}] {message}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _render_station_rows(self, rows: list[dict[str, str]]) -> None:
        signature = tuple(
            (
                row.get("callsign", ""),
                row.get("gl", ""),
                row.get("last_seen", ""),
                row.get("status", ""),
            )
            for row in rows
        )
        if signature == self.last_snapshot_signature:
            return

        self.last_snapshot_signature = signature
        for item in self.station_tree.get_children():
            self.station_tree.delete(item)

        for row in rows:
            indicator, display_status, tag = self._format_station_status(row)
            self.station_tree.insert(
                "",
                "end",
                values=(
                    indicator,
                    row.get("callsign", ""),
                    row.get("gl", "") or "-",
                    row.get("last_seen", "") or "-",
                    display_status,
                ),
                tags=(tag,),
            )

    def _format_station_status(self, row: dict[str, str]) -> tuple[str, str, str]:
        last_seen_text = row.get("last_seen", "")
        base_status = row.get("status", "") or "unknown"

        try:
            last_seen = datetime.strptime(last_seen_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "○", base_status, "unknown"

        if datetime.now() - last_seen <= STALE_AFTER:
            return "●", base_status, "active"
        return "○", "stale", "stale"

    def _refresh_station_list_from_db(self, force: bool = False) -> None:
        rows = read_station_rows(DB_PATH)
        if force or rows or self.last_snapshot_signature:
            self._render_station_rows(rows)

    def _schedule_db_refresh(self) -> None:
        self._refresh_station_list_from_db()
        self.root.after(3000, self._schedule_db_refresh)

    def _on_close(self) -> None:
        if self.backend is not None and self.backend.is_alive():
            self.backend.stop()
            self.root.after(250, self.root.destroy)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = DStarGuiApp(root)
    app._append_log("INFO", "GUI alpha ready")
    if ENV_PATH.exists():
        app._append_log("INFO", f".env defaults loaded from {ENV_PATH.name}")
    else:
        app._append_log("INFO", "No .env found. Fill settings manually.")
    root.mainloop()


if __name__ == "__main__":
    main()
