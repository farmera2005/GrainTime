"""The pairing window (what someone sees when they open GrainTimeAgent.exe)."""

from __future__ import annotations

import platform
import tkinter as tk
from tkinter import messagebox, ttk

from . import VERSION, config, winsetup
from .client import Server, ServerError
from .localdb import find_running_instance
from .worker import windows_user


def run_setup_window() -> None:
    state = config.load()
    root = tk.Tk()
    root.title("GrainTime Agent")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=18)
    frm.grid()

    ttk.Label(frm, text="GrainTime Agent", font=("Segoe UI", 14, "bold")).grid(column=0, row=0, columnspan=2, sticky="w")
    ttk.Label(frm, text=f"Version {VERSION} · Windows account {windows_user()}", foreground="#555").grid(
        column=0, row=1, columnspan=2, sticky="w", pady=(0, 10))
    running = find_running_instance()
    ttk.Label(frm, text=("The scale software's database is open." if running else
                         "The scale software's database is not open right now (that's fine; the agent "
                         "waits for it)."), wraplength=420).grid(column=0, row=2, columnspan=2, sticky="w", pady=(0, 10))

    if state:
        site = state.get("site", {})
        ttk.Label(frm, text=f"Paired with {site.get('name')} ({site.get('code')})\nGrainTime: {state['server']}\n"
                            f"Starts at sign-in: {'yes' if winsetup.autostart_enabled() else 'no'}",
                  justify="left").grid(column=0, row=3, columnspan=2, sticky="w")
        ttk.Label(frm, text="To move this PC to another site, create a new pairing code in GrainTime and pair again.",
                  wraplength=420, foreground="#555").grid(column=0, row=4, columnspan=2, sticky="w", pady=(6, 10))

    ttk.Label(frm, text="GrainTime address").grid(column=0, row=5, sticky="w")
    server_var = tk.StringVar(value=(state or {}).get("server") or "http://")
    ttk.Entry(frm, textvariable=server_var, width=40).grid(column=1, row=5, sticky="we", pady=3)
    ttk.Label(frm, text="Pairing code").grid(column=0, row=6, sticky="w")
    code_var = tk.StringVar()
    ttk.Entry(frm, textvariable=code_var, width=20).grid(column=1, row=6, sticky="w", pady=3)
    ttk.Label(frm, text="Get a pairing code in GrainTime: Configuration → Sites → this site → Agent.",
              foreground="#555", wraplength=420).grid(column=0, row=7, columnspan=2, sticky="w", pady=(0, 10))

    def do_pair():
        url, code = server_var.get().strip().rstrip("/"), code_var.get().strip()
        if not url.startswith(("http://", "https://")) or not code:
            messagebox.showerror("GrainTime Agent", "Enter the GrainTime address (http://…) and the pairing code.")
            return
        try:
            got = Server(url).pair(code, platform.node(), windows_user())
        except ServerError as exc:
            messagebox.showerror("GrainTime Agent", exc.detail)
            return
        except OSError as exc:
            messagebox.showerror("GrainTime Agent", f"Could not reach GrainTime at {url}:\n{exc}")
            return
        winsetup.stop_running_agents()
        config.save(url, got["key_id"], got["secret"], got["site"])
        exe = winsetup.install_copy()
        winsetup.enable_autostart(exe)
        winsetup.start_background(exe)
        messagebox.showinfo("GrainTime Agent",
                            f"Paired with {got['site']['name']}.\n\nThe agent is running now and starts "
                            f"automatically whenever {windows_user()} signs in. Turn on polling for the site "
                            "in GrainTime to start collecting.")
        root.destroy()

    def do_remove():
        if not messagebox.askyesno("GrainTime Agent", "Stop the agent on this PC and forget its pairing?"):
            return
        winsetup.stop_running_agents()
        winsetup.disable_autostart()
        config.forget()
        messagebox.showinfo("GrainTime Agent", "Removed. Revoke the agent on the site's page in GrainTime too.")
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=8, columnspan=2, sticky="e")
    if state:
        ttk.Button(btns, text="Remove from this PC", command=do_remove).grid(column=0, row=0, padx=4)
    ttk.Button(btns, text="Pair again" if state else "Pair", command=do_pair).grid(column=1, row=0)
    root.mainloop()
