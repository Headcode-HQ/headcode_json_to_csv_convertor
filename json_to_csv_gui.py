#!/usr/bin/env python3
"""
JSON -> CSV GUI converter for Headcode reference data (and generic JSON files).

Features:
- Pick input and output folders in a desktop GUI.
- Converts all .json files in the input folder to .csv files.
- Optional recursive conversion for subfolders.
- Live progress and activity log so users can see what is happening.
- Preserves folder structure when recursive mode is enabled.
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import traceback
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except Exception as exc:  # pragma: no cover - import guard for non-GUI envs
    raise SystemExit(
        "Tkinter is required to run this tool. "
        "Install Python with Tk support and run again."
    ) from exc


def _flatten_dict(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, nested in value.items():
        child_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(nested, dict):
            flat.update(_flatten_dict(nested, child_key))
        elif isinstance(nested, list):
            flat[child_key] = json.dumps(nested, ensure_ascii=False)
        else:
            flat[child_key] = nested
    return flat


def _ordered_union_keys(rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def _to_csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_json_to_rows(data: Any) -> tuple[list[str], list[dict[str, Any]]]:
    # Most Headcode reference files are list[object].
    if isinstance(data, list):
        if not data:
            return (["value"], [])

        if all(isinstance(item, dict) for item in data):
            rows = [_flatten_dict(item) for item in data]
            return (_ordered_union_keys(rows), rows)

        rows = [{"value": _to_csv_cell(item)} for item in data]
        return (["value"], rows)

    if isinstance(data, dict):
        # Common generic pattern: {"items": [ ... ]} or similar.
        if len(data) == 1:
            _, only_value = next(iter(data.items()))
            if isinstance(only_value, list):
                return _normalize_json_to_rows(only_value)

        row = _flatten_dict(data)
        return (_ordered_union_keys([row]), [row])

    # Scalar JSON root (string/number/bool/null)
    return (["value"], [{"value": _to_csv_cell(data)}])


def convert_json_file_to_csv(json_path: Path, csv_path: Path) -> int:
    raw = json_path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    headers, rows = _normalize_json_to_rows(data)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _to_csv_cell(row.get(key)) for key in headers})

    return len(rows)


def collect_json_files(input_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*.json" if recursive else "*.json"
    return sorted(p for p in input_dir.glob(pattern) if p.is_file())


class JsonToCsvGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Headcode JSON to CSV Converter")
        self.root.geometry("900x640")
        self.root.minsize(760, 520)

        self.input_dir_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Select folders and click Convert.")

        self._queue: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._is_running = False

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill=tk.BOTH, expand=True)

        path_frame = ttk.LabelFrame(container, text="Folders", padding=12)
        path_frame.pack(fill=tk.X)

        ttk.Label(path_frame, text="JSON input folder").grid(row=0, column=0, sticky="w")
        self.input_entry = ttk.Entry(path_frame, textvariable=self.input_dir_var, width=82)
        self.input_entry.grid(row=1, column=0, padx=(0, 8), pady=(4, 10), sticky="ew")
        self.input_button = ttk.Button(path_frame, text="Browse...", command=self._choose_input_dir)
        self.input_button.grid(row=1, column=1, pady=(4, 10), sticky="ew")

        ttk.Label(path_frame, text="CSV output folder").grid(row=2, column=0, sticky="w")
        self.output_entry = ttk.Entry(path_frame, textvariable=self.output_dir_var, width=82)
        self.output_entry.grid(row=3, column=0, padx=(0, 8), pady=(4, 10), sticky="ew")
        self.output_button = ttk.Button(path_frame, text="Browse...", command=self._choose_output_dir)
        self.output_button.grid(row=3, column=1, pady=(4, 10), sticky="ew")

        self.recursive_check = ttk.Checkbutton(
            path_frame,
            text="Include subfolders (recursive)",
            variable=self.recursive_var,
        )
        self.recursive_check.grid(row=4, column=0, sticky="w")
        path_frame.columnconfigure(0, weight=1)

        controls = ttk.Frame(container)
        controls.pack(fill=tk.X, pady=(10, 8))
        self.convert_button = ttk.Button(controls, text="Convert JSON to CSV", command=self._start_conversion)
        self.convert_button.pack(side=tk.LEFT)
        self.clear_button = ttk.Button(controls, text="Clear Log", command=self._clear_log)
        self.clear_button.pack(side=tk.LEFT, padx=(8, 0))

        progress_frame = ttk.LabelFrame(container, text="Progress", padding=12)
        progress_frame.pack(fill=tk.X)
        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.status_label = ttk.Label(progress_frame, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill=tk.X)

        log_frame = ttk.LabelFrame(container, text="Activity Log", padding=12)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log_text = ScrolledText(log_frame, wrap=tk.WORD, height=16, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _choose_input_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose folder containing JSON files")
        if path:
            self.input_dir_var.set(path)

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="Choose folder to write CSV files")
        if path:
            self.output_dir_var.set(path)

    def _set_running_state(self, running: bool) -> None:
        self._is_running = running
        state_convert = tk.DISABLED if running else tk.NORMAL
        state_paths = tk.DISABLED if running else tk.NORMAL
        self.convert_button.configure(state=state_convert)
        self.input_button.configure(state=state_paths)
        self.output_button.configure(state=state_paths)
        self.input_entry.configure(state=state_paths)
        self.output_entry.configure(state=state_paths)
        self.recursive_check.configure(state=state_paths)

    def _log(self, text: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _start_conversion(self) -> None:
        if self._is_running:
            return

        input_dir_raw = self.input_dir_var.get().strip()
        output_dir_raw = self.output_dir_var.get().strip()
        recursive = self.recursive_var.get()

        if not input_dir_raw:
            messagebox.showerror("Missing Input Folder", "Select a JSON input folder first.")
            return
        if not output_dir_raw:
            messagebox.showerror("Missing Output Folder", "Select a CSV output folder first.")
            return

        input_dir = Path(input_dir_raw)
        output_dir = Path(output_dir_raw)

        if not input_dir.exists() or not input_dir.is_dir():
            messagebox.showerror("Invalid Input Folder", f"Input folder does not exist:\n{input_dir}")
            return

        json_files = collect_json_files(input_dir, recursive)
        if not json_files:
            messagebox.showinfo("No JSON Files", "No .json files were found in the selected input folder.")
            return

        output_dir.mkdir(parents=True, exist_ok=True)
        self.progress.configure(value=0, maximum=len(json_files))
        self.status_var.set(f"Starting conversion of {len(json_files)} file(s)...")
        self._log("=" * 72)
        self._log(f"Input:  {input_dir}")
        self._log(f"Output: {output_dir}")
        self._log(f"Recursive: {recursive}")
        self._log(f"Found {len(json_files)} JSON file(s).")
        self._set_running_state(True)

        self._worker_thread = threading.Thread(
            target=self._worker_convert,
            args=(input_dir, output_dir, json_files),
            daemon=True,
        )
        self._worker_thread.start()

    def _worker_convert(self, input_dir: Path, output_dir: Path, json_files: list[Path]) -> None:
        success = 0
        failed = 0
        total = len(json_files)

        for index, json_path in enumerate(json_files, start=1):
            rel_path = json_path.relative_to(input_dir)
            csv_path = output_dir / rel_path.with_suffix(".csv")

            try:
                row_count = convert_json_file_to_csv(json_path, csv_path)
                success += 1
                self._queue.put(
                    (
                        "file_ok",
                        (index, total, str(json_path), str(csv_path), row_count),
                    )
                )
            except Exception as exc:
                failed += 1
                self._queue.put(
                    (
                        "file_error",
                        (index, total, str(json_path), str(exc)),
                    )
                )

        self._queue.put(("done", (total, success, failed)))

    def _poll_queue(self) -> None:
        try:
            while True:
                event, payload = self._queue.get_nowait()

                if event == "file_ok":
                    index, total, src, dst, rows = payload
                    self.progress.configure(value=index)
                    self.status_var.set(f"[{index}/{total}] Converted: {Path(src).name}")
                    self._log(f"[{index}/{total}] OK   {src} -> {dst} ({rows} row(s))")
                elif event == "file_error":
                    index, total, src, err = payload
                    self.progress.configure(value=index)
                    self.status_var.set(f"[{index}/{total}] Failed: {Path(src).name}")
                    self._log(f"[{index}/{total}] FAIL {src}")
                    self._log(f"            {err}")
                elif event == "done":
                    total, success, failed = payload
                    self._set_running_state(False)
                    self.status_var.set(
                        f"Finished. Converted {success}/{total} file(s), {failed} failure(s)."
                    )
                    self._log("-" * 72)
                    self._log(f"Finished. Success: {success}, Failed: {failed}, Total: {total}")
                    if failed == 0:
                        messagebox.showinfo(
                            "Conversion Complete",
                            f"Converted {success} file(s) successfully.",
                        )
                    else:
                        messagebox.showwarning(
                            "Conversion Completed With Errors",
                            f"Converted {success} file(s), {failed} failed.\nCheck the activity log.",
                        )
        except queue.Empty:
            pass
        except Exception:  # pragma: no cover - safety for unexpected UI issues
            self._log("Unexpected UI error:")
            self._log(traceback.format_exc())
            self._set_running_state(False)
            self.status_var.set("Stopped because of an unexpected UI error.")

        self.root.after(100, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    JsonToCsvGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
