"""Calculators tab: transmission lines, IPC-7351 pads, copper and RF helpers."""
from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from . import calcs

if TYPE_CHECKING:
    from .gui import App


@dataclass
class Field:
    key: str
    label: str
    unit: str = ""
    default: object = ""
    choices: list | None = None
    optional: bool = False  # may be left blank


@dataclass
class Calc:
    name: str
    group: str
    fields: list[Field]
    compute: Callable[[dict], list[tuple[str, str]]]
    about: str = ""
    solve: tuple[str, str] | None = None  # (field to find, label of the target) for "Find width"
    solve_fn: Callable[[dict, float], float] | None = None
    on_change: Callable[["CalculatorTab", str], None] | None = None


def _f(v: float, digits: int = 3) -> str:
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:.4g}"


def _line_rows(line: calcs.Line, f_ghz) -> list[tuple[str, str]]:
    rows = [("Characteristic impedance Z0", f"{line.z0:.2f} Ω"),
            ("Effective dielectric constant εeff", f"{line.eeff:.3f}"),
            ("Propagation delay", f"{line.delay_ps_per_mm():.2f} ps/mm   ({line.delay_ps_per_mm() * 25.4:.1f} ps/inch)"),
            ("Velocity factor", f"{line.velocity_factor():.3f}")]
    if f_ghz:
        rows.append((f"Quarter wavelength at {_f(f_ghz)} GHz", f"{line.quarter_wave_mm(f_ghz):.2f} mm"))
    return rows + [("Note", n) for n in line.notes]


def _num(v, name: str, optional: bool = False):
    if v in ("", None):
        if optional:
            return None
        raise calcs.CalcError(f"enter a value for {name}")
    try:
        return float(v)
    except ValueError as e:
        raise calcs.CalcError(f"{name} must be a number") from e


# ---------------------------------------------------------------- transmission lines
_EPS = Field("er", "Relative dielectric constant εr", "", 4.3)
_T = Field("t", "Copper thickness t", "mm  (1 oz = 0.035)", 0.035)
_FREQ = Field("f", "Frequency (for λ/4, optional)", "GHz", "", optional=True)
_TARGET = Field("target", "Target impedance (for Find width)", "Ω", 50, optional=True)


def _ms(v):
    line = calcs.microstrip(v["w"], v["h"], v["er"], v["t"])
    rows = _line_rows(line, v["f"])
    try:
        rows.insert(1, ("IPC-2141 estimate (for comparison)", f"{calcs.microstrip_ipc2141(v['w'], v['h'], v['er'], v['t']):.2f} Ω"))
    except calcs.CalcError as e:
        rows.insert(1, ("IPC-2141 estimate", str(e)))
    return rows


def _gcpw(v):
    return _line_rows(calcs.cpw_grounded(v["w"], v["s"], v["h"], v["er"], v["t"]), v["f"])


def _cpw(v):
    return _line_rows(calcs.cpw(v["w"], v["s"], v["h"], v["er"], v["t"]), v["f"])


def _sl(v):
    return _line_rows(calcs.stripline(v["w"], v["b"], v["er"], v["t"]), v["f"])


def _dms(v):
    zd, single = calcs.diff_microstrip(v["w"], v["s"], v["h"], v["er"], v["t"])
    return [("Differential impedance Zdiff", f"{zd:.2f} Ω"), ("Odd-mode impedance (Zdiff / 2)", f"{zd / 2:.2f} Ω"),
            ("Single trace Z0 (uncoupled)", f"{single.z0:.2f} Ω")] + _line_rows(single, v["f"])[1:]


def _dsl(v):
    zd, single = calcs.diff_stripline(v["w"], v["s"], v["b"], v["er"], v["t"])
    return [("Differential impedance Zdiff", f"{zd:.2f} Ω"), ("Odd-mode impedance (Zdiff / 2)", f"{zd / 2:.2f} Ω"),
            ("Single trace Z0 (uncoupled)", f"{single.z0:.2f} Ω")] + _line_rows(single, v["f"])[1:]


def _width_solver(z_of: Callable[[dict, float], float], hi_factor_key: str = "h"):
    def solve(v, target):
        top = max(50 * v.get(hi_factor_key, 1.0), 20.0)
        return calcs.solve_width(lambda w: z_of(v, w), target, 0.005, top)
    return solve


# ---------------------------------------------------------------- copper / RF
def _current(v):
    r = calcs.trace_width_for_current(v["i"], v["dt"], v["oz"], v["layer"] == "Internal")
    rows = [("Minimum trace width", f"{r['width_mm']:.3f} mm   ({r['width_mil']:.1f} mil)"),
            ("Copper cross-section", f"{r['area_mil2']:.1f} mil²")]
    if v["len"]:
        ohms = calcs.trace_resistance(r["width_mm"], v["len"], v["oz"], 25 + v["dt"])
        rows += [(f"Resistance of {_f(v['len'])} mm at that width", f"{ohms * 1e3:.2f} mΩ"),
                 ("Voltage drop", f"{ohms * v['i'] * 1e3:.1f} mV"), ("Power lost in the trace", f"{ohms * v['i'] ** 2 * 1e3:.1f} mW")]
    return rows


def _resistance(v):
    ohms = calcs.trace_resistance(v["w"], v["len"], v["oz"], v["temp"])
    rows = [("Resistance", f"{ohms * 1e3:.3f} mΩ")]
    if v["i"]:
        rows += [("Voltage drop", f"{ohms * v['i'] * 1e3:.2f} mV"), ("Power", f"{ohms * v['i'] ** 2 * 1e3:.2f} mW")]
    return rows


def _skin(v):
    d = calcs.skin_depth_mm(v["f"] * 1e6, calcs.RHO_CU, v["mur"])
    return [("Skin depth δ", f"{d * 1e3:.2f} µm   ({d / 0.0254:.3f} mil)"),
            ("Share of 1 oz copper (35 µm) carrying current", f"{min(1.0, 2 * d / 0.035) * 100:.0f}%  (both faces)")]


def _atten(v):
    r = calcs.attenuator(v["db"], v["z0"])
    return [("Pi: shunt resistors (2)", f"{r['pi_shunt']:.2f} Ω"), ("Pi: series resistor", f"{r['pi_series']:.2f} Ω"),
            ("T: series resistors (2)", f"{r['t_series']:.2f} Ω"), ("T: shunt resistor", f"{r['t_shunt']:.2f} Ω")]


def _match(v):
    m = calcs.mismatch(calcs.gamma_from(v["kind"], v["val"], v["z0"]))
    rl = "∞" if math.isinf(m["return_loss_db"]) else f"{m['return_loss_db']:.2f} dB"
    return [("|Reflection coefficient| Γ", f"{m['gamma']:.4f}"), ("Return loss", rl), ("VSWR", f"{m['vswr']:.3f} : 1"),
            ("Mismatch loss", f"{m['mismatch_loss_db']:.3f} dB"), ("Power reflected", f"{m['reflected_pct']:.2f}%")]


def _lc(v):
    f = calcs.resonance_hz(v["l"] * 1e-9, v["c"] * 1e-12)
    return [("Resonant frequency", f"{f / 1e6:.4f} MHz"), ("Reactance of L (= C) at resonance",
                                                           f"{2 * math.pi * f * v['l'] * 1e-9:.2f} Ω")]


# ---------------------------------------------------------------- IPC-7351
LEVELS = [("A", "A - Most (largest pads, hand soldering / rework)"), ("B", "B - Nominal (most designs)"),
          ("C", "C - Least (high density)")]


def _ipc(v):
    level = v["level"][0]
    goals = None
    if any(v.get(k) is not None for k in ("jt", "jh", "js")):
        d = calcs.FILLETS[v["kind"]][level]
        goals = (v["jt"] if v["jt"] is not None else d[0], v["jh"] if v["jh"] is not None else d[1],
                 v["js"] if v["js"] is not None else d[2], d[3])
    lp = calcs.land_pattern(v["kind"], level, (v["lmin"], v["lmax"]), (v["tmin"], v["tmax"]), (v["wmin"], v["wmax"]),
                            v["fab"], v["place"], float(v["round"]), v["pitch"], goals)
    rows = [("Pad size (length × width)", f"{_f(lp.pad_length)} × {_f(lp.x)} mm"),
            ("Pad centre to pad centre", f"{_f(lp.centre)} mm   (each pad at ±{_f(lp.centre / 2)} mm)"),
            ("Z (outer edge to outer edge)", f"{_f(lp.z)} mm"), ("G (inner edge to inner edge)", f"{_f(lp.g)} mm"),
            ("How much bigger than the terminal", ""),
            ("    toe (outward, per pad)", f"+{_f(lp.toe)} mm"),
            ("    heel (inward, per pad)", f"{'+' if lp.heel >= 0 else ''}{_f(lp.heel)} mm"),
            ("    side (each side)", f"{'+' if lp.side >= 0 else ''}{_f(lp.side)} mm")]
    if lp.gap_to_next is not None:
        rows.append(("Copper gap between neighbouring pads", f"{_f(lp.gap_to_next)} mm"))
    rows += [("Courtyard length (pads + excess)", f"{_f(lp.courtyard)} mm"),
             ("Fillet goals used (toe / heel / side)", " / ".join(_f(g) for g in lp.goals[:3]) +
              f" mm, courtyard excess {_f(lp.goals[3])} mm")]
    return rows + [("Note", n) for n in lp.notes]


def _ipc_preset(tab: "CalculatorTab", key: str) -> None:
    if key != "preset":
        return
    p = calcs.PRESETS.get(tab.vars["preset"].get())
    if not p:
        return
    tab.vars["kind"].set(p["type"])
    for k, (lo, hi) in (("l", p["L"]), ("t", p["T"]), ("w", p["W"])):
        tab.vars[k + "min"].set(_f(lo))
        tab.vars[k + "max"].set(_f(hi))
    tab.vars["pitch"].set(_f(p["pitch"]) if p.get("pitch") else "")


CALCS: list[Calc] = [
    Calc("Microstrip", "Transmission lines",
         [Field("w", "Track width W", "mm", 0.3), Field("h", "Dielectric height h (to ground)", "mm", 0.2), _EPS, _T,
          _FREQ, _TARGET], _ms,
         "Outer-layer track over a ground plane. Hammerstad & Jensen with thickness correction (Wadell); the IPC-2141 "
         "estimate is shown for comparison.",
         ("w", "target"), _width_solver(lambda v, w: calcs.microstrip(w, v["h"], v["er"], v["t"]).z0)),
    Calc("Coplanar waveguide with ground", "Transmission lines",
         [Field("w", "Track width", "mm", 0.35), Field("s", "Gap to the side ground (each side)", "mm", 0.15),
          Field("h", "Dielectric height h (to ground below)", "mm", 0.2), _EPS, _T, _FREQ, _TARGET], _gcpw,
         "Grounded (conductor-backed) coplanar waveguide: track with ground copper on both sides and a ground plane "
         "below - Wadell p.79, as used by chemandy.com. Stitch the side grounds to the plane with vias.",
         ("w", "target"), _width_solver(lambda v, w: calcs.cpw_grounded(w, v["s"], v["h"], v["er"], v["t"]).z0)),
    Calc("Coplanar waveguide (no ground plane)", "Transmission lines",
         [Field("w", "Track width", "mm", 0.5), Field("s", "Gap to the side ground (each side)", "mm", 0.2),
          Field("h", "Substrate thickness", "mm", 1.6), _EPS, _T, _FREQ, _TARGET], _cpw,
         "Coplanar waveguide with no copper under it, on a substrate of finite thickness (Simons; thickness: Gupta).",
         ("w", "target"), _width_solver(lambda v, w: calcs.cpw(w, v["s"], v["h"], v["er"], v["t"]).z0)),
    Calc("Stripline", "Transmission lines",
         [Field("w", "Track width W", "mm", 0.15), Field("b", "Plane-to-plane spacing b (total dielectric)", "mm", 0.5),
          _EPS, _T, _FREQ, _TARGET], _sl,
         "Inner-layer track centred between two ground planes (Wheeler, as given by Wadell).",
         ("w", "target"), _width_solver(lambda v, w: calcs.stripline(w, v["b"], v["er"], v["t"]).z0, "b")),
    Calc("Differential microstrip", "Transmission lines",
         [Field("w", "Track width W", "mm", 0.2), Field("s", "Gap between the two tracks", "mm", 0.2),
          Field("h", "Dielectric height h", "mm", 0.2), _EPS, _T, _FREQ, Field("target", "Target Zdiff (for Find width)", "Ω", 90, optional=True)],
         _dms, "Edge-coupled pair on an outer layer (IPC-2141 approximation - good to a few percent for s/h 0.2 - 3).",
         ("w", "target"), _width_solver(lambda v, w: calcs.diff_microstrip(w, v["s"], v["h"], v["er"], v["t"])[0])),
    Calc("Differential stripline", "Transmission lines",
         [Field("w", "Track width W", "mm", 0.12), Field("s", "Gap between the two tracks", "mm", 0.15),
          Field("b", "Plane-to-plane spacing b", "mm", 0.5), _EPS, _T, _FREQ, Field("target", "Target Zdiff (for Find width)", "Ω", 100, optional=True)],
         _dsl, "Edge-coupled pair centred between two planes (IPC-2141 approximation).",
         ("w", "target"), _width_solver(lambda v, w: calcs.diff_stripline(w, v["s"], v["b"], v["er"], v["t"])[0], "b")),
    Calc("SMD pad size (IPC-7351)", "Footprints",
         [Field("preset", "Start from", "", "0603 (1608 metric)", list(calcs.PRESETS), optional=True),
          Field("kind", "Terminal type", "", "Chip resistor / capacitor (0603 and larger)", list(calcs.FILLETS)),
          Field("level", "Density level", "", LEVELS[1][1], [l for _, l in LEVELS]),
          Field("lmin", "L min - overall length, terminal tip to tip", "mm", 1.5),
          Field("lmax", "L max", "mm", 1.7),
          Field("tmin", "T min - terminal (lead foot) length", "mm", 0.2), Field("tmax", "T max", "mm", 0.5),
          Field("wmin", "W min - terminal width", "mm", 0.7), Field("wmax", "W max", "mm", 0.9),
          Field("pitch", "Pitch (leaded parts, optional)", "mm", "", optional=True),
          Field("fab", "PCB fabrication tolerance F", "mm", 0.05), Field("place", "Placement tolerance P", "mm", 0.025),
          Field("round", "Round pads to", "mm", "0.05", ["0.01", "0.05"]),
          Field("jt", "Override toe goal (blank = IPC)", "mm", "", optional=True),
          Field("jh", "Override heel goal (blank = IPC)", "mm", "", optional=True),
          Field("js", "Override side goal (blank = IPC)", "mm", "", optional=True)],
         _ipc,
         "Pad size for a part from its datasheet dimensions, per the IPC-7351B land pattern equations "
         "(Z = Lmin + 2·Jt + √(CL² + F² + P²), G = Smax - 2·Jh - √(CS² + F² + P²), X = Wmin + 2·Js + √(CW² + F² + P²)). "
         "For leaded / QFN parts give the dimensions of one row: L = lead tip to lead tip across the package.",
         on_change=_ipc_preset),
    Calc("Trace width for current (IPC-2221)", "Copper",
         [Field("i", "Current", "A", 1.0), Field("dt", "Allowed temperature rise", "°C", 10),
          Field("oz", "Copper weight", "oz/ft²", 1.0), Field("layer", "Layer", "", "External", ["External", "Internal"]),
          Field("len", "Trace length (optional)", "mm", "", optional=True)], _current,
         "Minimum width to carry a DC current with a given temperature rise (IPC-2221 charts as a formula). "
         "Internal layers need about twice the width."),
    Calc("Trace resistance", "Copper",
         [Field("w", "Trace width", "mm", 0.25), Field("len", "Length", "mm", 100), Field("oz", "Copper weight", "oz/ft²", 1.0),
          Field("temp", "Copper temperature", "°C", 25), Field("i", "Current (optional)", "A", "", optional=True)],
         _resistance, "DC resistance of a copper trace, corrected for temperature."),
    Calc("Skin depth", "Copper",
         [Field("f", "Frequency", "MHz", 100), Field("mur", "Relative permeability µr", "", 1.0)], _skin,
         "Depth at which current density in copper falls to 1/e."),
    Calc("Attenuator (Pi and T)", "RF",
         [Field("db", "Attenuation", "dB", 6), Field("z0", "System impedance", "Ω", 50)], _atten,
         "Resistor values for matched Pi and T attenuators."),
    Calc("Return loss / VSWR / mismatch", "RF",
         [Field("kind", "I know the", "", "VSWR", ["VSWR", "Return loss (dB)", "|Reflection coefficient|", "Load resistance (ohm)"]),
          Field("val", "Value", "", 1.5), Field("z0", "System impedance (for a load resistance)", "Ω", 50)], _match,
         "Converts between VSWR, return loss, reflection coefficient and mismatch loss."),
    Calc("LC resonance", "RF",
         [Field("l", "Inductance", "nH", 100), Field("c", "Capacitance", "pF", 10)], _lc, "Resonant frequency of an L and C."),
]


class CalculatorTab(ttk.Frame):
    def __init__(self, parent, app: "App"):
        super().__init__(parent, padding=10)
        self.app = app
        self.calc: Calc | None = None
        self.vars: dict[str, tk.StringVar] = {}
        self._pending = None
        left = ttk.Frame(self)
        left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse", height=22)
        self.tree.column("#0", width=260)
        self.tree.pack(fill="y", expand=True)
        groups: dict[str, str] = {}
        for i, c in enumerate(CALCS):
            if c.group not in groups:
                groups[c.group] = self.tree.insert("", "end", text=c.group, open=True)
            self.tree.insert(groups[c.group], "end", iid=str(i), text=c.name)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selected())
        right = ttk.Frame(self, padding=(14, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.title = tk.StringVar()
        ttk.Label(right, textvariable=self.title, style="Bold.TLabel").pack(anchor="w")
        self.about = tk.StringVar()
        ttk.Label(right, textvariable=self.about, style="Muted.TLabel", wraplength=860, justify="left").pack(
            anchor="w", pady=(2, 8))
        body = ttk.Frame(right)
        body.pack(fill="both", expand=True)
        self.form = ttk.LabelFrame(body, text="Inputs", padding=8)
        self.form.pack(side="left", fill="y", anchor="n")
        self.results = ttk.LabelFrame(body, text="Results", padding=8)
        self.results.pack(side="left", fill="both", expand=True, padx=(10, 0), anchor="n")
        self.tree.selection_set("0")

    # ------------------------------------------------------------ building the form
    def _selected(self) -> None:
        sel = self.tree.selection()
        if not sel or not sel[0].isdigit():
            return
        self.calc = CALCS[int(sel[0])]
        self.title.set(self.calc.name)
        self.about.set(self.calc.about)
        for w in self.form.winfo_children():
            w.destroy()
        self.vars = {}
        for r, f in enumerate(self.calc.fields):
            ttk.Label(self.form, text=f.label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            v = tk.StringVar(value="" if f.default is None else str(f.default))
            self.vars[f.key] = v
            if f.choices:
                w = ttk.Combobox(self.form, textvariable=v, values=f.choices, state="readonly",
                                 width=max(12, min(46, max(len(str(c)) for c in f.choices) + 2)))
            else:
                w = ttk.Entry(self.form, textvariable=v, width=12)
            w.grid(row=r, column=1, sticky="w", pady=2)
            ttk.Label(self.form, text=f.unit, style="Muted.TLabel").grid(row=r, column=2, sticky="w", padx=(6, 0))
            v.trace_add("write", lambda *a, k=f.key: self._changed(k))
        if self.calc.solve:
            ttk.Button(self.form, text="Find width for the target", command=self._solve).grid(
                row=len(self.calc.fields), column=1, sticky="w", pady=(8, 0))
        if self.calc.on_change:
            self.calc.on_change(self, "preset")
        self._compute()

    def _changed(self, key: str) -> None:
        if self.calc and self.calc.on_change:
            self.calc.on_change(self, key)
        if self._pending:
            self.after_cancel(self._pending)
        self._pending = self.after(150, self._compute)

    def _values(self) -> dict:
        out: dict = {}
        for f in self.calc.fields:
            raw = self.vars[f.key].get().strip()
            if f.choices or f.key == "round":
                out[f.key] = raw
            else:
                out[f.key] = _num(raw, f.label.split(" (")[0], f.optional)
        return out

    def _show(self, rows: list[tuple[str, str]]) -> None:
        for w in self.results.winfo_children():
            w.destroy()
        for r, (label, value) in enumerate(rows):
            ttk.Label(self.results, text=label, style="Muted.TLabel" if label == "Note" else "TLabel").grid(
                row=r, column=0, sticky="nw", padx=(0, 14), pady=2)
            e = ttk.Entry(self.results, width=56)  # selectable, so results can be copied
            e.insert(0, value)
            e.configure(state="readonly")
            if value:
                e.grid(row=r, column=1, sticky="w", pady=2)

    def _compute(self) -> None:
        self._pending = None
        if not self.calc:
            return
        try:
            self._show(self.calc.compute(self._values()))
        except (calcs.CalcError, ValueError, ZeroDivisionError, OverflowError) as e:
            self._show([("Cannot calculate", str(e))])

    def _solve(self) -> None:
        key, target_key = self.calc.solve
        try:
            v = self._values()
            if v.get(target_key) is None:
                raise calcs.CalcError("enter a target impedance")
            w = self.calc.solve_fn(v, v[target_key])
        except (calcs.CalcError, ValueError, ZeroDivisionError, OverflowError) as e:
            self._show([("Cannot find a width", str(e))])
            return
        self.vars[key].set(f"{w:.4f}")
