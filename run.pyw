"""Double-click launcher for KiCad Part Importer (runs without a console window)."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from partimporter.gui import main
    main()
except Exception:
    err = traceback.format_exc()
    try:
        from partimporter.config import app_data_dir
        (app_data_dir() / "crash.txt").write_text(err, encoding="utf-8")
    except Exception:
        pass
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("KiCad Part Importer", "The app crashed:\n\n" + err[-1500:] +
                         "\n\nIf a module is missing, run Install.bat again.")
