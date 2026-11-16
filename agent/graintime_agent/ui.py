"""The pairing window (what someone sees when they open GrainTimeAgent.exe)."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from . import VERSION, config, installer, winsetup
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
        ttk.Label(frm, text="To install a newer version, open its GrainTimeAgent.exe and click Update (the pairing is "
                            "kept). To move this PC to another site, create a new pairing code and pair again.",
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
            site = installer.pair_and_install(url, code)
        except installer.InstallError as exc:
            messagebox.showerror("GrainTime Agent", exc.message)
            return
        messagebox.showinfo("GrainTime Agent",
                            f"Paired with {site['name']}.\n\nThe agent is running now and starts "
                            f"automatically whenever {windows_user()} signs in. Turn on polling for the site "
                            "in GrainTime to start collecting.")
        root.destroy()

    def do_update():
        """Replace the installed agent with this copy; keep the pairing."""
        try:
            installer.update_in_place(lambda m: None)
        except (OSError, installer.InstallError) as exc:
            messagebox.showerror("GrainTime Agent", f"Could not update the agent:\n{exc}")
            return
        messagebox.showinfo("GrainTime Agent", f"Updated to version {VERSION}. The agent is running again, "
                                               f"still paired with {state['site']['name']}.")
        root.destroy()

    def do_remove():
        if not messagebox.askyesno("GrainTime Agent", "Stop the agent on this PC and forget its pairing?"):
            return
        installer.uninstall(lambda m: None)
        messagebox.showinfo("GrainTime Agent", "Removed. Revoke the agent on the site's page in GrainTime too.")
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(column=0, row=8, columnspan=2, sticky="e")
    if state:
        ttk.Button(btns, text="Remove from this PC", command=do_remove).grid(column=0, row=0, padx=4)
        if winsetup.is_frozen() and not winsetup.running_from_install():
            ttk.Button(btns, text=f"Update this PC to {VERSION}", command=do_update).grid(column=2, row=0, padx=4)
    ttk.Button(btns, text="Pair again" if state else "Pair", command=do_pair).grid(column=1, row=0)
    root.mainloop()
