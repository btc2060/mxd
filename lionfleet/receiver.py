from __future__ import annotations

import argparse
import socket
import threading
from pathlib import Path

from .config import load_receiver
from .protocol import MAX_PACKET, decode, encode, receipt
from .speech import Announcer, play_notice
from .store import ReceiverStore


class ReceiverServer:
    def __init__(self, config, root, store=None):
        self.store = store or ReceiverStore(config, root)
        self.c = config
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((config.bind_ip, config.port))
            self.socket.settimeout(.2)
        except Exception:
            self.socket.close()
            raise
        self.address = self.socket.getsockname()
        self.error = ""
        self.thread = threading.Thread(target=self._run, name="fleet-receiver", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                data, address = self.socket.recvfrom(MAX_PACKET + 1)
                msg = decode(data)
                if msg["type"] != "status":
                    continue
                ok, error = self.store.apply(msg, address)
                self.socket.sendto(encode(receipt(msg, ok, error)), address)
                self.error = error
            except socket.timeout:
                continue
            except ValueError:
                continue  # Invalid/unregistered traffic cannot create commands or UI actions.
            except Exception as error:
                self.error = f"接收线程异常：{error}"
                if not self.stop_event.is_set():
                    print(self.error, flush=True)
                    self.stop_event.wait(.1)

    def stop(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=1)
        self.socket.close()


def show_panel(server, announcer):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Garden 多机报警接收端 — 静音不会恢复挂机")
    root.geometry("1100x600")
    ttk.Label(root, text=f"监听 UDP {server.c.port}  |  收到报警后按机号切 KVM／UU，手动处理，再在该机按 F8。", padding=10).pack(anchor="w")
    columns = ("name", "port", "status", "last", "duration", "address")
    table = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
    for key, title, width in zip(columns, ("机号", "KVM 口", "当前状态", "最后心跳", "报警时长", "来源 IP"), (110, 65, 470, 85, 85, 130)):
        table.heading(key, text=title)
        table.column(key, width=width, minwidth=50)
    table.tag_configure("alarm", foreground="#bf1111")
    table.tag_configure("offline", foreground="#777777")
    table.pack(fill="both", expand=True, padx=10)
    for m in server.c.machines:
        table.insert("", "end", iid=m.machine_id, values=(m.machine_name, m.kvm_port, "尚未连接", "—", "—", ""))
    if server.c.machines:
        table.selection_set(server.c.machines[0].machine_id)
    buttons = ttk.Frame(root, padding=10)
    buttons.pack(fill="x")

    def selected():
        items = table.selection()
        return items[0] if items else None

    def mute():
        if selected():
            server.store.mute_machine(selected())

    def unmute():
        if selected():
            server.store.unmute_machine(selected())

    def test():
        if selected():
            announcer.test(selected())

    ttk.Button(buttons, text="静音选中机（仅本次报警）", command=mute).pack(side="left", padx=4)
    ttk.Button(buttons, text="恢复选中机声音", command=unmute).pack(side="left", padx=4)
    ttk.Button(buttons, text="测试选中机号播报", command=test).pack(side="left", padx=4)
    detail = tk.StringVar()
    ttk.Label(root, textvariable=detail, wraplength=1050, padding=10).pack(anchor="w")
    warning = tk.StringVar()
    ttk.Label(root, textvariable=warning, foreground="#bf1111", wraplength=1050, padding=10).pack(anchor="w")

    def refresh():
        for row in server.store.rows():
            tag = "alarm" if row["alarms"] else "" if row["online"] else "offline"
            table.item(row["id"], values=(row["name"], row["port"], row["status"], row["last"],
                                          f'{row["alarm_seconds"]} 秒' if row["alarms"] else "—", row["address"]), tags=(tag,))
            if row["id"] == selected():
                detail.set("；".join(a["reason"] for a in row["alarms"]) or "该机当前没有待处理报警。")
        warning.set(server.error or server.store.persistence_error or announcer.error)
        root.after(400, refresh)

    refresh()
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Garden 多机 UDP 接收、机号面板与语音报警；仅需 Python 标准库")
    parser.add_argument("--config", type=Path, default=Path("config.receiver.local.json"))
    parser.add_argument("--test-sound", action="store_true", help="只测试本机声音和中文机号语音")
    parser.add_argument("--console", action="store_true", help="无界面接收，便于排错")
    args = parser.parse_args(argv)
    server = announcer = None
    try:
        if args.test_sound:
            play_notice("一号机测试报警，接收端声音正常")
            return 0
        path = args.config.resolve()
        config = load_receiver(path)
        server = ReceiverServer(config, path.parent)
        announcer = Announcer(server.store, path.parent)
        server.start()
        announcer.start()
        print(f"接收端已监听 {config.bind_ip}:{config.port}。机号必须与 machines 登记一致。", flush=True)
        if args.console:
            while not server.stop_event.wait(2):
                for row in server.store.rows():
                    print(f'{row["id"]} {row["name"]} KVM={row["port"]} {row["status"]} last={row["last"]}', flush=True)
        else:
            show_panel(server, announcer)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"接收端未能运行：{error}。首次使用请复制 config.receiver.example.json 为 config.receiver.local.json。", flush=True)
        return 1
    finally:
        if server:
            server.stop()
        if announcer:
            announcer.stop()


if __name__ == "__main__":
    raise SystemExit(main())
