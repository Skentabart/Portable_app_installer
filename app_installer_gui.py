import json
import os
import platform
import queue
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from pathlib import Path
from tkinter import (
    Tk,
    Toplevel,
    StringVar,
    BooleanVar,
    END,
    W,
    E,
    N,
    S,
    filedialog,
    messagebox,
    ttk,
)

CONFIG_FILE = Path("installer_catalog.json")
DEFAULT_MAX_WORKERS = 3

SILENT_FLAGS = {
    ".exe": ["/S", "/silent", "/verysilent", "/qn", "/quiet", "-s", "-silent"],
    ".msi": ["/qn", "/quiet", "/norestart"],
    ".msix": ["/quiet"],
    ".deb": [],
    ".rpm": [],
    ".pkg": [],
    ".sh": [],
}


@dataclass
class InstallerItem:
    name: str
    path: str
    args: str = ""
    enabled: bool = True


class InstallerCatalog:
    def __init__(self, config_file: Path):
        self.config_file = config_file
        self.items: list[InstallerItem] = []
        self.load()

    def load(self):
        if not self.config_file.exists():
            self.items = []
            return
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
            self.items = [InstallerItem(**item) for item in data]
        except Exception:
            self.items = []

    def save(self):
        payload = [asdict(item) for item in self.items]
        self.config_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class InstallerApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Portable App Installer")
        self.root.geometry("1100x620")

        self.catalog = InstallerCatalog(CONFIG_FILE)
        self.progress_vars: dict[int, ttk.Progressbar] = {}
        self.status_vars: dict[int, StringVar] = {}
        self.selection_vars: dict[int, BooleanVar] = {}
        self.message_queue: queue.Queue[tuple[str, dict]] = queue.Queue()

        self.password: str | None = None
        self.total_tasks = 0
        self.completed_tasks = 0
        self.install_lock = threading.Lock()

        self._build_ui()
        self._refresh_table()
        self.root.after(150, self._poll_queue)

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=10)
        header.grid(row=0, column=0, sticky=(W, E))

        ttk.Button(header, text="Добавить установщик", command=self._add_installer_dialog).grid(
            row=0, column=0, padx=5
        )
        ttk.Button(header, text="Импортировать папку", command=self._import_folder).grid(
            row=0, column=1, padx=5
        )
        ttk.Button(header, text="Удалить выбранные", command=self._remove_selected).grid(
            row=0, column=2, padx=5
        )
        ttk.Button(header, text="Установить выбранные", command=self._install_selected).grid(
            row=0, column=3, padx=5
        )
        ttk.Button(header, text="Установить все", command=self._install_all).grid(
            row=0, column=4, padx=5
        )

        ttk.Label(header, text="Параллельных задач:").grid(row=0, column=5, padx=(20, 5))
        self.workers_var = StringVar(value=str(DEFAULT_MAX_WORKERS))
        workers_box = ttk.Spinbox(header, from_=1, to=8, textvariable=self.workers_var, width=5)
        workers_box.grid(row=0, column=6)

        self.overall_progress = ttk.Progressbar(self.root, maximum=100)
        self.overall_progress.grid(row=1, column=0, sticky=(W, E), padx=10, pady=5)
        self.overall_status = StringVar(value="Ожидание")
        ttk.Label(self.root, textvariable=self.overall_status, padding=(10, 0)).grid(
            row=2, column=0, sticky=W
        )

        table_frame = ttk.Frame(self.root, padding=10)
        table_frame.grid(row=3, column=0, sticky=(N, S, W, E))

        columns = ("select", "name", "path", "args", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)
        self.tree.heading("select", text="Выбор")
        self.tree.heading("name", text="Программа")
        self.tree.heading("path", text="Файл")
        self.tree.heading("args", text="Аргументы")
        self.tree.heading("status", text="Статус")

        self.tree.column("select", width=80, anchor="center")
        self.tree.column("name", width=220)
        self.tree.column("path", width=430)
        self.tree.column("args", width=160)
        self.tree.column("status", width=180)

        self.tree.grid(row=0, column=0, sticky=(N, S, W, E))
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=scroll.set)
        scroll.grid(row=0, column=1, sticky=(N, S))

        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        self.root.columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._on_double_click)

    def _refresh_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.selection_vars.clear()
        self.status_vars.clear()

        for idx, item in enumerate(self.catalog.items):
            selected = "Да" if item.enabled else "Нет"
            row_id = self.tree.insert(
                "",
                END,
                iid=str(idx),
                values=(selected, item.name, item.path, item.args, "Готово"),
            )
            self.selection_vars[idx] = BooleanVar(value=item.enabled)
            self.status_vars[idx] = StringVar(value="Готово")
            self.tree.item(row_id, values=(selected, item.name, item.path, item.args, "Готово"))

    def _on_double_click(self, _event):
        selected = self.tree.selection()
        if not selected:
            return
        idx = int(selected[0])
        item = self.catalog.items[idx]

        dlg = Toplevel(self.root)
        dlg.title("Редактирование")
        dlg.transient(self.root)
        dlg.grab_set()

        name_var = StringVar(value=item.name)
        path_var = StringVar(value=item.path)
        args_var = StringVar(value=item.args)
        enabled_var = BooleanVar(value=item.enabled)

        ttk.Label(dlg, text="Название").grid(row=0, column=0, sticky=W, padx=8, pady=4)
        ttk.Entry(dlg, textvariable=name_var, width=50).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(dlg, text="Путь").grid(row=1, column=0, sticky=W, padx=8, pady=4)
        ttk.Entry(dlg, textvariable=path_var, width=50).grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(dlg, text="Аргументы").grid(row=2, column=0, sticky=W, padx=8, pady=4)
        ttk.Entry(dlg, textvariable=args_var, width=50).grid(row=2, column=1, padx=8, pady=4)

        ttk.Checkbutton(dlg, text="Включено", variable=enabled_var).grid(
            row=3, column=1, sticky=W, padx=8, pady=4
        )

        def save_changes():
            item.name = name_var.get().strip() or item.name
            item.path = path_var.get().strip() or item.path
            item.args = args_var.get().strip()
            item.enabled = enabled_var.get()
            self.catalog.save()
            self._refresh_table()
            dlg.destroy()

        ttk.Button(dlg, text="Сохранить", command=save_changes).grid(
            row=4, column=1, sticky=E, padx=8, pady=8
        )

    def _add_installer_dialog(self):
        file_path = filedialog.askopenfilename(title="Выберите установщик")
        if not file_path:
            return
        file = Path(file_path)
        default_args = self._default_args(file)

        self.catalog.items.append(
            InstallerItem(
                name=file.stem,
                path=str(file),
                args=default_args,
                enabled=True,
            )
        )
        self.catalog.save()
        self._refresh_table()

    def _import_folder(self):
        folder = filedialog.askdirectory(title="Папка с установщиками")
        if not folder:
            return

        folder_path = Path(folder)
        installer_files = [
            f
            for f in folder_path.iterdir()
            if f.is_file() and f.suffix.lower() in SILENT_FLAGS
        ]

        if not installer_files:
            messagebox.showinfo("Импорт", "В папке не найдено поддерживаемых установщиков.")
            return

        known_paths = {item.path for item in self.catalog.items}
        added = 0
        for file in installer_files:
            if str(file) in known_paths:
                continue
            self.catalog.items.append(
                InstallerItem(
                    name=file.stem,
                    path=str(file),
                    args=self._default_args(file),
                    enabled=True,
                )
            )
            added += 1

        self.catalog.save()
        self._refresh_table()
        messagebox.showinfo("Импорт", f"Добавлено установщиков: {added}")

    def _remove_selected(self):
        selected = sorted((int(i) for i in self.tree.selection()), reverse=True)
        if not selected:
            messagebox.showwarning("Удаление", "Сначала выделите строки.")
            return

        for idx in selected:
            self.catalog.items.pop(idx)

        self.catalog.save()
        self._refresh_table()

    def _install_selected(self):
        selected = [int(i) for i in self.tree.selection()]
        if not selected:
            selected = [i for i, item in enumerate(self.catalog.items) if item.enabled]
        self._run_installation(selected)

    def _install_all(self):
        self._run_installation(list(range(len(self.catalog.items))))

    def _run_installation(self, indexes: list[int]):
        indexes = [i for i in indexes if 0 <= i < len(self.catalog.items)]
        if not indexes:
            messagebox.showwarning("Установка", "Нет выбранных программ для установки.")
            return

        if platform.system() != "Windows":
            if not self._ensure_admin_password():
                return

        self.total_tasks = len(indexes)
        self.completed_tasks = 0
        self.overall_progress["value"] = 0
        self.overall_status.set(f"Запуск: 0/{self.total_tasks}")

        max_workers = self._safe_workers()
        thread = threading.Thread(target=self._install_worker, args=(indexes, max_workers), daemon=True)
        thread.start()

    def _install_worker(self, indexes: list[int], max_workers: int):
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(self._install_one, idx) for idx in indexes]
            for future in futures:
                future.result()

    def _install_one(self, idx: int):
        item = self.catalog.items[idx]
        self.message_queue.put(("status", {"idx": idx, "status": "Установка..."}))

        cmd = self._build_command(item)
        try:
            process = subprocess.run(
                cmd,
                input=(self.password + "\n") if self.password and cmd[0] == "sudo" else None,
                capture_output=True,
                text=True,
                check=False,
            )
            ok = process.returncode == 0
            details = process.stdout.strip() or process.stderr.strip()
            status = "Успешно" if ok else f"Ошибка ({process.returncode})"
            self.message_queue.put(
                (
                    "status",
                    {
                        "idx": idx,
                        "status": status,
                        "details": details[:300],
                    },
                )
            )
        except Exception as exc:
            self.message_queue.put(
                (
                    "status",
                    {
                        "idx": idx,
                        "status": "Ошибка запуска",
                        "details": str(exc),
                    },
                )
            )
        finally:
            self.message_queue.put(("done", {}))

    def _poll_queue(self):
        while True:
            try:
                msg_type, payload = self.message_queue.get_nowait()
            except queue.Empty:
                break

            if msg_type == "status":
                idx = payload["idx"]
                status = payload["status"]
                self._set_row_status(idx, status)
                if payload.get("details"):
                    self.overall_status.set(payload["details"])
            elif msg_type == "done":
                with self.install_lock:
                    self.completed_tasks += 1
                    if self.total_tasks:
                        ratio = (self.completed_tasks / self.total_tasks) * 100
                        self.overall_progress["value"] = ratio
                        self.overall_status.set(
                            f"Выполнено: {self.completed_tasks}/{self.total_tasks}"
                        )

        self.root.after(150, self._poll_queue)

    def _set_row_status(self, idx: int, status: str):
        if idx >= len(self.catalog.items):
            return
        item = self.catalog.items[idx]
        selected = "Да" if item.enabled else "Нет"
        self.tree.item(str(idx), values=(selected, item.name, item.path, item.args, status))

    def _build_command(self, item: InstallerItem):
        installer = Path(item.path)
        ext = installer.suffix.lower()
        args = item.args.split() if item.args else []

        if platform.system() == "Windows":
            if ext == ".msi":
                return ["msiexec", "/i", str(installer), *args]
            return [str(installer), *args]

        if ext == ".deb":
            base = ["dpkg", "-i", str(installer)]
        elif ext == ".rpm":
            base = ["rpm", "-i", str(installer)]
        elif ext == ".pkg" and platform.system() == "Darwin":
            base = ["installer", "-pkg", str(installer), "-target", "/"]
        elif ext == ".sh":
            base = ["bash", str(installer), *args]
            args = []
        else:
            base = [str(installer)]

        return ["sudo", "-S", *base, *args]

    def _default_args(self, file: Path) -> str:
        options = SILENT_FLAGS.get(file.suffix.lower(), [])
        return options[0] if options else ""

    def _safe_workers(self) -> int:
        try:
            workers = int(self.workers_var.get())
            return max(1, min(8, workers))
        except ValueError:
            return DEFAULT_MAX_WORKERS

    def _ensure_admin_password(self) -> bool:
        dlg = Toplevel(self.root)
        dlg.title("Права администратора")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.resizable(False, False)

        ttk.Label(dlg, text="Введите пароль sudo (один раз перед запуском):").grid(
            row=0, column=0, padx=10, pady=(10, 6), sticky=W
        )
        pass_var = StringVar()
        entry = ttk.Entry(dlg, textvariable=pass_var, show="*", width=36)
        entry.grid(row=1, column=0, padx=10, pady=6, sticky=(W, E))
        entry.focus_set()

        accepted = {"ok": False}

        def verify():
            pwd = pass_var.get()
            if not pwd:
                messagebox.showwarning("Пароль", "Пароль не может быть пустым.")
                return
            check = subprocess.run(
                ["sudo", "-S", "-v"],
                input=pwd + "\n",
                capture_output=True,
                text=True,
                check=False,
            )
            if check.returncode == 0:
                self.password = pwd
                accepted["ok"] = True
                dlg.destroy()
            else:
                messagebox.showerror("Пароль", "Не удалось подтвердить пароль sudo.")

        ttk.Button(dlg, text="Проверить", command=verify).grid(
            row=2, column=0, padx=10, pady=(6, 10), sticky=E
        )

        self.root.wait_window(dlg)
        return accepted["ok"]


def main():
    root = Tk()
    ttk.Style().theme_use("clam")
    app = InstallerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
