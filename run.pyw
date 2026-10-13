"""Double-click launcher for Kiporter (runs without a console window)."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from kiporter.gui import main
    main()
except Exception:
    err = traceback.format_exc()
    try:
        from kiporter.config import app_data_dir
        (app_data_dir() / "crash.txt").write_text(err, encoding="utf-8")
    except Exception:
        pass
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Kiporter", "The app crashed:\n\n" + err[-1500:] +
                         "\n\nIf a module is missing, run Install.bat again.")
