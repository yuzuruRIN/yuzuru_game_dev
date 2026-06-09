import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path


COMMENT_LOCATION_RE = re.compile(r'^\s*#\s*(game/.+?:\d+)\s*$')
TRANSLATE_BLOCK_RE = re.compile(r'^\s*translate\s+(\w+)\s+(.+?)\s*:\s*$')
COMMENTED_DIALOGUE_RE = re.compile(r'^\s*#\s*([A-Za-z_]\w*)?\s*"((?:[^"\\]|\\.)*)"\s*$')
DIALOGUE_LINE_RE = re.compile(r'^\s*([A-Za-z_]\w*)?\s*"((?:[^"\\]|\\.)*)"\s*$')
OLD_LINE_RE = re.compile(r'^\s*old\s*"((?:[^"\\]|\\.)*)"\s*$')
NEW_LINE_RE = re.compile(r'^\s*new\s*"((?:[^"\\]|\\.)*)"\s*$')

LANGUAGE_CHAR_PATTERNS = {
    "None": None,
    "Thai": re.compile(r'[\u0E00-\u0E7F]'),
    "English": re.compile(r'[A-Za-z]'),
    "Japanese": re.compile(r'[\u3040-\u30FF\u4E00-\u9FFF]'),
    "Russian": re.compile(r'[\u0400-\u04FF]'),
    "Arabic": re.compile(r'[\u0600-\u06FF]'),
    "Chinese": re.compile(r'[\u4E00-\u9FFF]'),
    "Vietnamese": re.compile(r'[A-Za-zÀ-ỹĐđ]'),
    "Indonesian": re.compile(r'[A-Za-z]'),
}


def unescape_renpy_text(text: str) -> str:
    text = text.replace(r'\"', '"')
    text = text.replace(r"\'", "'")
    text = text.replace(r'\\', '\\')
    text = text.replace(r'\n', '\n')
    text = text.replace(r'\t', '\t')
    return text


def normalize_text(text: str) -> str:
    return unescape_renpy_text(text).strip()


def is_untranslated(source_text: str, translated_text: str) -> bool:
    source = normalize_text(source_text)
    target = normalize_text(translated_text)

    if target == "":
        return True

    if source == target:
        return True

    return False


def contains_language_chars(text: str, language_name: str) -> bool:
    pattern = LANGUAGE_CHAR_PATTERNS.get(language_name)
    if pattern is None:
        return False
    return bool(pattern.search(text))


def read_text_safely(file_path: Path) -> str:
    encodings = ["utf-8", "utf-8-sig", "cp1252"]
    for enc in encodings:
        try:
            return file_path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def parse_file(file_path: Path, block_language_filter="All", leftover_language="None"):
    results = []

    text = read_text_safely(file_path)
    lines = text.splitlines()

    current_location = None
    current_block_language = None
    in_strings_block = False

    pending_old_text = None
    pending_old_location = None

    i = 0
    total_lines = len(lines)

    while i < total_lines:
        line = lines[i]

        loc_match = COMMENT_LOCATION_RE.match(line)
        if loc_match:
            current_location = loc_match.group(1)

        block_match = TRANSLATE_BLOCK_RE.match(line)
        if block_match:
            current_block_language = block_match.group(1)
            block_name = block_match.group(2).strip()

            if block_name == "strings":
                in_strings_block = True
                pending_old_text = None
                pending_old_location = None
            else:
                in_strings_block = False

            i += 1
            continue

        should_check_this_block = (
            block_language_filter == "All"
            or current_block_language == block_language_filter
        )

        if in_strings_block:
            old_match = OLD_LINE_RE.match(line)
            if old_match:
                pending_old_text = old_match.group(1)
                pending_old_location = current_location
                i += 1
                continue

            new_match = NEW_LINE_RE.match(line)
            if new_match and pending_old_text is not None:
                new_text = new_match.group(1)

                reason = None
                if should_check_this_block:
                    if is_untranslated(pending_old_text, new_text):
                        reason = "ยังไม่แปล/เหมือนต้นฉบับ"
                    elif leftover_language != "None" and contains_language_chars(normalize_text(new_text), leftover_language):
                        reason = f"ยังมีอักษร {leftover_language} ค้าง"

                if reason:
                    results.append({
                        "type": "strings",
                        "location": pending_old_location or "(ไม่พบตำแหน่ง)",
                        "source": pending_old_text,
                        "target": new_text,
                        "reason": reason,
                        "block_language": current_block_language or "",
                    })

                pending_old_text = None
                pending_old_location = None
                i += 1
                continue

            i += 1
            continue

        commented_dialogue_match = COMMENTED_DIALOGUE_RE.match(line)
        if commented_dialogue_match:
            source_speaker = commented_dialogue_match.group(1) or ""
            source_text = commented_dialogue_match.group(2)

            j = i + 1
            while j < total_lines and lines[j].strip() == "":
                j += 1

            if j < total_lines:
                dialogue_match = DIALOGUE_LINE_RE.match(lines[j])
                if dialogue_match:
                    target_speaker = dialogue_match.group(1) or ""
                    target_text = dialogue_match.group(2)

                    if source_speaker == target_speaker and should_check_this_block:
                        reason = None

                        if is_untranslated(source_text, target_text):
                            reason = "ยังไม่แปล/เหมือนต้นฉบับ"
                        elif leftover_language != "None" and contains_language_chars(normalize_text(target_text), leftover_language):
                            reason = f"ยังมีอักษร {leftover_language} ค้าง"

                        if reason:
                            results.append({
                                "type": "dialogue",
                                "location": current_location or "(ไม่พบตำแหน่ง)",
                                "source": source_text,
                                "target": target_text,
                                "reason": reason,
                                "block_language": current_block_language or "",
                            })

            i = j if j > i else i + 1
            continue

        i += 1

    return results


def collect_rpy_files(paths):
    files = []
    for path_str in paths:
        path = Path(path_str)

        if not path.exists():
            continue

        if path.is_file() and path.suffix.lower() == ".rpy":
            files.append(path)
        elif path.is_dir():
            files.extend(path.rglob("*.rpy"))

    return sorted(set(files))


class RenPyTranslationCheckerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Ren'Py Translation Checker")
        self.root.geometry("1280x780")

        self.selected_paths = []
        self.results = []

        self.build_ui()

    def build_ui(self):
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill="x")

        btn_frame = ttk.Frame(top_frame)
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="เพิ่มไฟล์ .rpy", command=self.add_files).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="เพิ่มโฟลเดอร์", command=self.add_folder).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="ล้างรายการ", command=self.clear_paths).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="เริ่มตรวจสอบ", command=self.run_check).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="บันทึกผลลัพธ์ .txt", command=self.save_results).pack(side="left", padx=5)

        filter_frame = ttk.Frame(top_frame)
        filter_frame.pack(fill="x", pady=(10, 0))

        ttk.Label(filter_frame, text="เช็ค block ภาษา:").pack(side="left", padx=(0, 5))

        self.block_language_var = tk.StringVar(value="All")
        self.block_language_combo = ttk.Combobox(
            filter_frame,
            textvariable=self.block_language_var,
            state="readonly",
            width=20,
            values=[
                "All",
                "English",
                "Japanese",
                "Vietnamese",
                "Russian",
                "ChineseSimplified",
                "ChineseTraditional",
                "Arabic",
                "Indonesian",
            ]
        )
        self.block_language_combo.pack(side="left", padx=(0, 15))

        ttk.Label(filter_frame, text="ตรวจภาษาที่ยังค้าง:").pack(side="left", padx=(0, 5))

        self.leftover_language_var = tk.StringVar(value="None")
        self.leftover_language_combo = ttk.Combobox(
            filter_frame,
            textvariable=self.leftover_language_var,
            state="readonly",
            width=20,
            values=[
                "None",
                "Thai",
                "English",
                "Japanese",
                "Russian",
                "Arabic",
                "Chinese",
                "Vietnamese",
                "Indonesian",
            ]
        )
        self.leftover_language_combo.pack(side="left")

        path_label = ttk.Label(top_frame, text="รายการไฟล์/โฟลเดอร์ที่เลือก:")
        path_label.pack(anchor="w", pady=(10, 5))

        self.path_listbox = tk.Listbox(top_frame, height=7)
        self.path_listbox.pack(fill="x", expand=False)

        mid_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        mid_frame.pack(fill="both", expand=True)

        summary_frame = ttk.Frame(mid_frame)
        summary_frame.pack(fill="x", pady=(5, 10))

        self.summary_var = tk.StringVar(value="ยังไม่ได้ตรวจสอบ")
        ttk.Label(summary_frame, textvariable=self.summary_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")

        columns = ("location", "type", "block_language", "reason", "file")
        self.tree = ttk.Treeview(mid_frame, columns=columns, show="headings")

        self.tree.heading("location", text="ตำแหน่ง")
        self.tree.heading("type", text="ประเภท")
        self.tree.heading("block_language", text="ภาษา block")
        self.tree.heading("reason", text="สาเหตุ")
        self.tree.heading("file", text="ไฟล์แปล")

        self.tree.column("location", width=240, anchor="w")
        self.tree.column("type", width=100, anchor="center")
        self.tree.column("block_language", width=130, anchor="center")
        self.tree.column("reason", width=220, anchor="w")
        self.tree.column("file", width=520, anchor="w")

        scrollbar_y = ttk.Scrollbar(mid_frame, orient="vertical", command=self.tree.yview)
        scrollbar_x = ttk.Scrollbar(mid_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar_y.pack(side="right", fill="y")
        scrollbar_x.pack(side="bottom", fill="x")

        self.tree.bind("<<TreeviewSelect>>", self.show_detail)

        bottom_frame = ttk.LabelFrame(self.root, text="รายละเอียด", padding=10)
        bottom_frame.pack(fill="both", expand=False, padx=10, pady=(0, 10))

        ttk.Label(bottom_frame, text="ต้นฉบับ:").pack(anchor="w")
        self.source_text = tk.Text(bottom_frame, height=5, wrap="word")
        self.source_text.pack(fill="x", pady=(0, 8))

        ttk.Label(bottom_frame, text="ข้อความปัจจุบันในไฟล์แปล:").pack(anchor="w")
        self.target_text = tk.Text(bottom_frame, height=5, wrap="word")
        self.target_text.pack(fill="x")

        self.source_text.configure(state="disabled")
        self.target_text.configure(state="disabled")

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="เลือกไฟล์ .rpy",
            filetypes=[("Ren'Py files", "*.rpy"), ("All files", "*.*")]
        )
        if files:
            for f in files:
                if f not in self.selected_paths:
                    self.selected_paths.append(f)
            self.refresh_path_list()

    def add_folder(self):
        folder = filedialog.askdirectory(title="เลือกโฟลเดอร์")
        if folder:
            if folder not in self.selected_paths:
                self.selected_paths.append(folder)
            self.refresh_path_list()

    def clear_paths(self):
        self.selected_paths.clear()
        self.results.clear()
        self.refresh_path_list()
        self.clear_tree()
        self.summary_var.set("ยังไม่ได้ตรวจสอบ")
        self.set_text(self.source_text, "")
        self.set_text(self.target_text, "")

    def refresh_path_list(self):
        self.path_listbox.delete(0, tk.END)
        for path in self.selected_paths:
            self.path_listbox.insert(tk.END, path)

    def clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def run_check(self):
        if not self.selected_paths:
            messagebox.showwarning("ยังไม่ได้เลือกไฟล์", "กรุณาเลือกไฟล์ .rpy หรือโฟลเดอร์ก่อน")
            return

        files = collect_rpy_files(self.selected_paths)
        if not files:
            messagebox.showwarning("ไม่พบไฟล์", "ไม่พบไฟล์ .rpy ในรายการที่เลือก")
            return

        selected_block_language = self.block_language_var.get()
        selected_leftover_language = self.leftover_language_var.get()

        self.root.config(cursor="watch")
        self.root.update_idletasks()

        self.results.clear()
        self.clear_tree()

        checked_count = 0
        found_count = 0

        for file_path in files:
            checked_count += 1
            try:
                file_results = parse_file(
                    file_path,
                    block_language_filter=selected_block_language,
                    leftover_language=selected_leftover_language,
                )

                for entry in file_results:
                    entry["file"] = str(file_path)
                    self.results.append(entry)

                    self.tree.insert(
                        "",
                        "end",
                        values=(
                            entry["location"],
                            entry["type"],
                            entry.get("block_language", ""),
                            entry.get("reason", ""),
                            str(file_path),
                        )
                    )
                    found_count += 1

            except Exception as e:
                error_entry = {
                    "type": "error",
                    "location": "(อ่านไฟล์ไม่ได้)",
                    "source": "",
                    "target": str(e),
                    "reason": "error",
                    "block_language": "",
                    "file": str(file_path),
                }
                self.results.append(error_entry)

                self.tree.insert(
                    "",
                    "end",
                    values=(
                        error_entry["location"],
                        error_entry["type"],
                        error_entry["block_language"],
                        error_entry["reason"],
                        error_entry["file"],
                    )
                )

        self.root.config(cursor="")
        self.summary_var.set(
            f"ตรวจสอบแล้ว {checked_count} ไฟล์ | พบบรรทัดที่มีปัญหา {found_count} รายการ"
        )

        if found_count == 0:
            messagebox.showinfo("เสร็จสิ้น", "ไม่พบบรรทัดที่มีปัญหา")

    def show_detail(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return

        item_id = selected[0]
        values = self.tree.item(item_id, "values")
        if not values:
            return

        location, entry_type, block_language, reason, file_path = values

        matched_entry = None
        for entry in self.results:
            if (
                entry.get("location") == location
                and entry.get("type") == entry_type
                and entry.get("file") == file_path
                and entry.get("reason", "") == reason
                and entry.get("block_language", "") == block_language
            ):
                matched_entry = entry
                break

        if matched_entry:
            self.set_text(self.source_text, matched_entry.get("source", ""))
            self.set_text(self.target_text, matched_entry.get("target", ""))

    def set_text(self, widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def save_results(self):
        if not self.results:
            messagebox.showwarning("ยังไม่มีผลลัพธ์", "กรุณาตรวจสอบไฟล์ก่อน แล้วค่อยบันทึกผลลัพธ์")
            return

        save_path = filedialog.asksaveasfilename(
            title="บันทึกผลลัพธ์",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )

        if not save_path:
            return

        try:
            lines = []
            lines.append("ผลการตรวจสอบไฟล์แปล Ren'Py")
            lines.append("=" * 70)
            lines.append("")

            normal_results = [r for r in self.results if r.get("type") != "error"]
            error_results = [r for r in self.results if r.get("type") == "error"]

            lines.append(f"พบบรรทัดที่มีปัญหาทั้งหมด: {len(normal_results)}")
            lines.append("")

            current_file = None
            for entry in normal_results:
                if current_file != entry["file"]:
                    current_file = entry["file"]
                    lines.append(f"ไฟล์: {current_file}")
                    lines.append("-" * 70)

                lines.append(f"# {entry['location']}")
                lines.append(f"ประเภท: {entry['type']}")
                lines.append(f"ภาษา block: {entry.get('block_language', '')}")
                lines.append(f"สาเหตุ: {entry.get('reason', '')}")
                lines.append(f"ต้นฉบับ: {normalize_text(entry['source'])}")
                lines.append(f"ปัจจุบัน: {normalize_text(entry['target'])}")
                lines.append("")

            if error_results:
                lines.append("")
                lines.append("ไฟล์ที่เกิดข้อผิดพลาด")
                lines.append("-" * 70)
                for entry in error_results:
                    lines.append(f"{entry['file']}: {entry['target']}")

            Path(save_path).write_text("\n".join(lines), encoding="utf-8-sig")
            messagebox.showinfo("บันทึกสำเร็จ", f"บันทึกผลลัพธ์แล้ว:\n{save_path}")
        except Exception as e:
            messagebox.showerror("เกิดข้อผิดพลาด", f"บันทึกไฟล์ไม่สำเร็จ\n\n{e}")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass

    app = RenPyTranslationCheckerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()