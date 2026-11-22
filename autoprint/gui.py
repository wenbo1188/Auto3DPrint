"""Windows desktop window for step 1: connect, watch status, upload and print."""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from autoprint import __version__
from autoprint.config import Settings
from autoprint.ftps import list_files, upload_file
from autoprint.printer import BambuPrinter, PrinterStatus
from autoprint.threemf import sliced_plates

URL_PREFIXES = ["ftp://", "file:///sdcard/", "file:///"]


def fmt(value, unit="", digits=0):
    if value is None:
        return "-"
    return f"{value:.{digits}f}{unit}" if isinstance(value, float) else f"{value}{unit}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.printer: BambuPrinter | None = None
        self.events: queue.Queue = queue.Queue()

        root.title(f"Auto3DPrint {__version__} - P2S 控制")
        root.minsize(620, 560)
        self._build()
        self._set_connected(False)
        root.after(100, self._drain_events)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout -------------------------------------------------------
    def _build(self):
        pad = {"padx": 6, "pady": 3}
        s = self.settings

        conn = ttk.LabelFrame(self.root, text="打印机连接（需先开启局域网模式 + 开发者模式）")
        conn.pack(fill="x", padx=8, pady=6)
        self.var_host = tk.StringVar(value=s.host)
        self.var_serial = tk.StringVar(value=s.serial)
        self.var_code = tk.StringVar(value=s.access_code)
        self.var_prefix = tk.StringVar(value=s.url_prefix)
        self.var_ams = tk.BooleanVar(value=s.use_ams)
        self.var_level = tk.BooleanVar(value=s.bed_leveling)

        rows = [
            ("IP 地址", ttk.Entry(conn, textvariable=self.var_host, width=22)),
            ("序列号", ttk.Entry(conn, textvariable=self.var_serial, width=22)),
            ("访问码", ttk.Entry(conn, textvariable=self.var_code, width=22, show="*")),
        ]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(conn, text=label).grid(row=i, column=0, sticky="w", **pad)
            widget.grid(row=i, column=1, sticky="w", **pad)

        self.btn_connect = ttk.Button(conn, text="连接", command=self._toggle_connect)
        self.btn_connect.grid(row=0, column=2, sticky="ew", **pad)
        self.btn_ftp_test = ttk.Button(conn, text="测试文件传输", command=self._ftp_test)
        self.btn_ftp_test.grid(row=1, column=2, sticky="ew", **pad)
        self.lbl_conn = ttk.Label(conn, text="未连接", foreground="gray")
        self.lbl_conn.grid(row=2, column=2, sticky="w", **pad)

        # ---- status ----
        st = ttk.LabelFrame(self.root, text="打印状态")
        st.pack(fill="x", padx=8, pady=6)
        self.status_vars = {}
        labels = [
            ("state", "状态"), ("file", "当前文件"), ("progress", "进度"),
            ("remaining", "剩余时间"), ("layer", "层"), ("nozzle", "喷嘴"), ("bed", "热床"),
            ("error", "错误码"),
        ]
        for i, (key, text) in enumerate(labels):
            r, c = divmod(i, 2)
            ttk.Label(st, text=text + "：").grid(row=r, column=c * 2, sticky="w", **pad)
            var = tk.StringVar(value="-")
            ttk.Label(st, textvariable=var, width=24).grid(row=r, column=c * 2 + 1, sticky="w", **pad)
            self.status_vars[key] = var
        self.progress = ttk.Progressbar(st, maximum=100)
        self.progress.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)
        st.columnconfigure(3, weight=1)

        # ---- print ----
        pr = ttk.LabelFrame(self.root, text="打印文件（Bambu Studio 里“导出切片文件”得到的 .3mf）")
        pr.pack(fill="x", padx=8, pady=6)
        self.var_file = tk.StringVar()
        ttk.Entry(pr, textvariable=self.var_file).grid(row=0, column=0, columnspan=3, sticky="ew", **pad)
        ttk.Button(pr, text="选择…", command=self._choose_file).grid(row=0, column=3, **pad)
        pr.columnconfigure(2, weight=1)

        ttk.Label(pr, text="盘号").grid(row=1, column=0, sticky="w", **pad)
        self.var_plate = tk.IntVar(value=1)
        self.cmb_plate = ttk.Combobox(pr, textvariable=self.var_plate, values=[1], width=5, state="readonly")
        self.cmb_plate.grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(pr, text="使用 AMS", variable=self.var_ams).grid(row=1, column=2, sticky="w", **pad)
        ttk.Checkbutton(pr, text="自动调平", variable=self.var_level).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(pr, text="启动地址前缀").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(pr, textvariable=self.var_prefix, values=URL_PREFIXES, width=16).grid(
            row=2, column=1, columnspan=2, sticky="w", **pad
        )

        btns = ttk.Frame(pr)
        btns.grid(row=3, column=0, columnspan=4, sticky="ew", **pad)
        self.btn_print = ttk.Button(btns, text="上传并开始打印", command=self._upload_and_print)
        self.btn_upload = ttk.Button(btns, text="只上传", command=lambda: self._upload_and_print(start=False))
        self.btn_pause = ttk.Button(btns, text="暂停", command=lambda: self._cmd("pause"))
        self.btn_resume = ttk.Button(btns, text="继续", command=lambda: self._cmd("resume"))
        self.btn_stop = ttk.Button(btns, text="停止", command=self._stop)
        for b in (self.btn_print, self.btn_upload, self.btn_pause, self.btn_resume, self.btn_stop):
            b.pack(side="left", padx=3)
        self.upload_bar = ttk.Progressbar(pr, maximum=100)
        self.upload_bar.grid(row=4, column=0, columnspan=4, sticky="ew", **pad)

        # ---- log ----
        lg = ttk.LabelFrame(self.root, text="日志")
        lg.pack(fill="both", expand=True, padx=8, pady=6)
        self.log_box = tk.Text(lg, height=8, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

    # ---- helpers ------------------------------------------------------
    def log(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {text}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def post(self, kind: str, *args):
        """Called from worker threads; handled on the Tk thread."""
        self.events.put((kind, args))

    def _drain_events(self):
        try:
            while True:
                kind, args = self.events.get_nowait()
                if kind == "status":
                    self._show_status(*args)
                elif kind == "conn":
                    ok, msg = args
                    self._set_connected(ok, msg)
                    self.log(msg)
                elif kind == "log":
                    self.log(*args)
                elif kind == "upload":
                    sent, total = args
                    self.upload_bar["value"] = 100 * sent / max(total, 1)
                elif kind == "error":
                    self.log("错误：" + args[0])
                    messagebox.showerror("出错了", args[0])
                elif kind == "busy":
                    self._set_busy(*args)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _collect_settings(self) -> Settings:
        s = self.settings
        s.host = self.var_host.get().strip()
        s.serial = self.var_serial.get().strip()
        s.access_code = self.var_code.get().strip()
        s.url_prefix = self.var_prefix.get().strip() or "ftp://"
        s.use_ams = self.var_ams.get()
        s.bed_leveling = self.var_level.get()
        s.save()
        return s

    def _set_connected(self, ok: bool, msg: str = "未连接"):
        self.lbl_conn.configure(text=msg if ok else (msg or "未连接"), foreground="green" if ok else "gray")
        self.btn_connect.configure(text="断开" if ok else "连接")
        state = "normal" if ok else "disabled"
        for b in (self.btn_print, self.btn_pause, self.btn_resume, self.btn_stop):
            b.configure(state=state)

    def _set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for b in (self.btn_print, self.btn_upload, self.btn_ftp_test):
            b.configure(state=state)
        if not busy and not (self.printer and self.printer.is_connected):
            self.btn_print.configure(state="disabled")

    def _show_status(self, st: PrinterStatus):
        v = self.status_vars
        v["state"].set(st.state_label)
        v["file"].set(st.file or "-")
        v["progress"].set(fmt(st.percent, "%"))
        v["remaining"].set(fmt(st.remaining_min, " 分钟"))
        v["layer"].set(f"{fmt(st.layer)} / {fmt(st.total_layers)}")
        v["nozzle"].set(f"{fmt(st.nozzle_temp, '°C')} → {fmt(st.nozzle_target, '°C')}")
        v["bed"].set(f"{fmt(st.bed_temp, '°C')} → {fmt(st.bed_target, '°C')}")
        v["error"].set(str(st.error_code) if st.error_code else "无")
        self.progress["value"] = st.percent or 0

    # ---- actions ------------------------------------------------------
    def _toggle_connect(self):
        if self.printer and self.printer.is_connected:
            self.printer.on_connection = None
            self.printer.disconnect()
            self.printer = None
            self._set_connected(False, "已断开")
            self.log("已断开")
            return
        s = self._collect_settings()
        if not (s.host and s.serial and s.access_code):
            messagebox.showwarning("缺少信息", "请填写 IP 地址、序列号和访问码。")
            return
        self.log(f"正在连接 {s.host} …")
        self.btn_connect.configure(state="disabled")

        def work():
            p = BambuPrinter(s.host, s.serial, s.access_code, port=s.mqtt_port)
            p.on_status = lambda st: self.post("status", st)
            p.on_connection = lambda ok, msg: self.post("conn", ok, msg)
            try:
                p.connect()
                self.printer = p
                if not p.wait_for_report(10):
                    self.post("log", "已连接但没收到状态，检查序列号是否正确")
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.post("error", f"连接失败：{e}")
            finally:
                self.root.after(0, lambda: self.btn_connect.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def _ftp_test(self):
        s = self._collect_settings()
        if not (s.host and s.access_code):
            messagebox.showwarning("缺少信息", "请填写 IP 地址和访问码。")
            return
        self.log("测试文件传输（FTPS 990 端口）…")

        def work():
            self.post("busy", True)
            try:
                names = list_files(s.host, s.access_code, port=s.ftp_port)
                shown = ", ".join(names[:10]) + (" …" if len(names) > 10 else "")
                self.post("log", f"文件传输正常，存储里有 {len(names)} 项：{shown}")
            except Exception as e:  # noqa: BLE001
                self.post("error", f"文件传输失败：{e}（检查是否插了存储卡、访问码是否正确）")
            finally:
                self.post("busy", False)

        threading.Thread(target=work, daemon=True).start()

    def _choose_file(self):
        path = filedialog.askopenfilename(filetypes=[("切片后的 3MF", "*.3mf"), ("所有文件", "*.*")])
        if not path:
            return
        self.var_file.set(path)
        plates = sliced_plates(path)
        if plates:
            self.cmb_plate.configure(values=plates)
            self.var_plate.set(plates[0])
            self.log(f"已选择 {os.path.basename(path)}，包含切片好的盘：{plates}")
        else:
            self.cmb_plate.configure(values=[1])
            self.var_plate.set(1)
            self.log("这个 3mf 里没找到切片 G-code。请在 Bambu Studio 里用“导出切片文件”重新导出。")

    def _upload_and_print(self, start: bool = True):
        s = self._collect_settings()
        path = self.var_file.get().strip()
        if not os.path.isfile(path):
            messagebox.showwarning("没有文件", "请先选择一个 .3mf 文件。")
            return
        if not sliced_plates(path):
            if not messagebox.askyesno("可能没切片", "没在文件里找到切片 G-code，打印机可能无法执行。仍然继续吗？"):
                return
        if start and not (self.printer and self.printer.is_connected):
            messagebox.showwarning("未连接", "请先连接打印机。")
            return
        plate = int(self.var_plate.get() or 1)

        def work():
            self.post("busy", True)
            try:
                self.post("log", f"上传 {os.path.basename(path)} …")
                name = upload_file(
                    s.host, s.access_code, path, port=s.ftp_port,
                    progress=lambda a, b: self.post("upload", a, b),
                )
                self.post("log", "上传完成")
                if start:
                    self.printer.start_print(
                        name, plate=plate, url_prefix=s.url_prefix,
                        use_ams=s.use_ams, bed_leveling=s.bed_leveling,
                    )
                    self.post("log", f"已发送开始打印命令（第 {plate} 盘，地址 {s.url_prefix}{name}）")
            except Exception as e:  # noqa: BLE001
                self.post("error", f"上传或启动失败：{e}")
            finally:
                self.post("busy", False)

        threading.Thread(target=work, daemon=True).start()

    def _cmd(self, name: str):
        try:
            getattr(self.printer, name)()
            self.log(f"已发送：{name}")
        except Exception as e:  # noqa: BLE001
            self.log(f"命令失败：{e}")

    def _stop(self):
        if messagebox.askyesno("确认停止", "确定要停止当前打印吗？停止后无法继续。"):
            self._cmd("stop")

    def _on_close(self):
        try:
            self._collect_settings()
            if self.printer:
                self.printer.on_connection = None
                self.printer.disconnect()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    try:
        # Sharper text on high-DPI Windows screens.
        from ctypes import windll  # type: ignore[attr-defined]

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
