"""Engineering calculators: transmission-line impedance, IPC-7351 land patterns, trace current and RF helpers.

All lengths are in millimetres unless a name says otherwise. Formulas and their sources:

* Microstrip - Hammerstad & Jensen (1980), with their conductor-thickness correction (as in Wadell,
  Transmission Line Design Handbook, 1991). IPC-2141 is given alongside for comparison.
* Coplanar waveguide with ground (conductor-backed CPW) - Wadell p.79, the formula chemandy.com uses.
* Coplanar waveguide without ground, on a substrate of finite thickness - Simons, Coplanar Waveguide Circuits.
  Conductor thickness (both CPW types) - Gupta, Microstrip Lines and Slotlines.
* Symmetric stripline - Wheeler (1978) as given by Wadell.
* Edge-coupled differential pairs - IPC-2141 approximations.
* Trace current - IPC-2221 (I = k * dT^0.44 * A^0.725).
* Land patterns - IPC-7351B equations (Z, G, X with RMS tolerances).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

ETA0 = 376.730313668  # free-space impedance, ohms
C0 = 299792458.0  # m/s
MU0 = 4e-7 * math.pi
RHO_CU = 1.72e-8  # ohm*m at 20 C (annealed copper)
OZ_MM = 0.0347  # 1 oz/ft2 copper, mm


class CalcError(ValueError):
    pass


# ==================================================================== helpers
def ellip_k(k: float) -> float:
    """Complete elliptic integral of the first kind K(k), modulus k (exact, by the arithmetic-geometric mean)."""
    if not 0 <= k < 1:
        raise CalcError("elliptic modulus out of range")
    a, b = 1.0, math.sqrt(1 - k * k)
    for _ in range(60):
        if abs(a - b) < 1e-15 * a:
            break
        a, b = (a + b) / 2, math.sqrt(a * b)
    return math.pi / (2 * a)


def k_ratio(k: float) -> float:
    """K(k) / K(k'). k is kept just inside 0..1, where very wide or very narrow geometry would round it off."""
    k = min(max(k, 1e-12), 1 - 1e-15)
    return ellip_k(k) / ellip_k(math.sqrt(1 - k * k))


def _positive(**vals: float) -> None:
    for name, v in vals.items():
        if v is None or v <= 0:
            raise CalcError(f"{name} must be greater than 0")


@dataclass
class Line:
    """Result of a transmission-line calculation."""
    z0: float
    eeff: float
    notes: list[str] = field(default_factory=list)

    def delay_ps_per_mm(self) -> float:
        return math.sqrt(self.eeff) / C0 * 1e12 / 1e3

    def velocity_factor(self) -> float:
        return 1 / math.sqrt(self.eeff)

    def quarter_wave_mm(self, f_ghz: float) -> float:
        return C0 / (f_ghz * 1e9) / math.sqrt(self.eeff) / 4 * 1e3


# ==================================================================== transmission lines
def _hj_z01(u: float) -> float:
    f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))


def _hj_eeff(u: float, er: float) -> float:
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def microstrip(w: float, h: float, er: float, t: float = 0.0) -> Line:
    """Hammerstad & Jensen microstrip, with conductor thickness t."""
    _positive(width=w, height=h, er=er)
    u = w / h
    if t > 0:
        th = t / h
        coth2 = (1 / math.tanh(math.sqrt(6.517 * u))) ** 2
        du1 = th / math.pi * math.log(1 + 4 * math.e / (th * coth2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * du1 if er > 1 else du1
        u1, ur = u + du1, u + dur
        eeff = _hj_eeff(ur, er) * (_hj_z01(u1) / _hj_z01(ur)) ** 2
        z0 = _hj_z01(ur) / math.sqrt(_hj_eeff(ur, er))
    else:
        eeff = _hj_eeff(u, er)
        z0 = _hj_z01(u) / math.sqrt(eeff)
    out = Line(z0, eeff)
    if not 0.01 <= u <= 100:
        out.notes.append("W/h is outside 0.01 - 100, where the formula is accurate to about 1%.")
    return out


def microstrip_ipc2141(w: float, h: float, er: float, t: float = 0.0) -> float:
    _positive(width=w, height=h, er=er)
    arg = 5.98 * h / (0.8 * w + t)
    if arg <= 1:
        raise CalcError("IPC-2141 is not valid for a trace this wide")
    return 87 / math.sqrt(er + 1.41) * math.log(arg)


def _gupta(a: float, b: float, t: float) -> tuple[float, float]:
    """Effective track (a) and slot edge (b) for conductor thickness t (Gupta)."""
    if t <= 0:
        return a, b
    d = 1.25 * t / math.pi * (1 + math.log(4 * math.pi * a / t))
    return a + d, b - d


def cpw_grounded(w: float, s: float, h: float, er: float, t: float = 0.0) -> Line:
    """Conductor-backed coplanar waveguide (Wadell p.79): track w, gaps s each side, substrate h to ground."""
    _positive(width=w, gap=s, height=h, er=er)
    a, b = _gupta(w, w + 2 * s, t)
    if b <= a:
        raise CalcError("the copper is too thick for this gap")
    k = a / b
    k3 = math.tanh(math.pi * a / (4 * h)) / math.tanh(math.pi * b / (4 * h))
    r0, r3 = k_ratio(k), k_ratio(k3)  # K(k)/K(k'), K(k3)/K(k3')
    q = r3 / r0
    eeff = (1 + er * q) / (1 + q)
    if t > 0:  # thick copper also lowers the effective dielectric constant (as in Qucs / KiCad)
        eeff -= 0.7 * (eeff - 1) * t / s / (k_ratio(w / (w + 2 * s)) + 0.7 * t / s)
    z0 = 60 * math.pi / math.sqrt(eeff) / (r0 + r3)
    out = Line(z0, eeff)
    if h < s:
        out.notes.append("The substrate is thinner than the gap: most of the field goes to the ground plane below, "
                         "so the line behaves more like a microstrip.")
    return out


def cpw(w: float, s: float, h: float, er: float, t: float = 0.0) -> Line:
    """Coplanar waveguide with no ground plane under it, on a substrate of thickness h (Simons)."""
    _positive(width=w, gap=s, height=h, er=er)
    a0, b0 = w, w + 2 * s
    k0 = a0 / b0
    k1 = math.sinh(math.pi * a0 / (4 * h)) / math.sinh(math.pi * b0 / (4 * h))
    eeff = 1 + (er - 1) / 2 * k_ratio(k1) / k_ratio(k0)
    a, b = _gupta(a0, b0, t)
    if b <= a:
        raise CalcError("the copper is too thick for this gap")
    kt = a / b
    if t > 0:  # Gupta: thickness lowers the effective dielectric constant a little
        eeff = eeff - (eeff - 1) / ((b0 - a0) / 2 / (0.7 * t) * k_ratio(k0) + 1)
    z0 = 30 * math.pi / math.sqrt(eeff) / k_ratio(kt)
    return Line(z0, eeff)


def stripline(w: float, b: float, er: float, t: float = 0.0) -> Line:
    """Symmetric stripline (Wheeler/Wadell): track w centred between planes b apart (b = total dielectric)."""
    _positive(width=w, plane_spacing=b, er=er)
    if t >= b:
        raise CalcError("the copper is thicker than the dielectric")
    x = t / b
    if x > 0:
        m = 2 / (1 + 2 / 3 * x / (1 - x))
        dw = (b - t) * x / (math.pi * (1 - x)) * (
            1 - 0.5 * math.log((x / (2 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
    else:
        dw = 0.0
    m_ = (b - t) / (w + dw)
    z0 = 30 / math.sqrt(er) * math.log(1 + 4 / math.pi * m_ * (8 / math.pi * m_ + math.sqrt((8 / math.pi * m_) ** 2 + 6.27)))
    out = Line(z0, er)
    if w / (b - t) < 0.35 and x > 0.25:
        out.notes.append("Very thick copper for this spacing: the result is less accurate.")
    return out


def diff_microstrip(w: float, s: float, h: float, er: float, t: float = 0.0) -> tuple[float, Line]:
    """Edge-coupled microstrip pair (IPC-2141 approximation). Returns (Zdiff, single-ended line)."""
    _positive(spacing=s)
    single = microstrip(w, h, er, t)
    return 2 * single.z0 * (1 - 0.48 * math.exp(-0.96 * s / h)), single


def diff_stripline(w: float, s: float, b: float, er: float, t: float = 0.0) -> tuple[float, Line]:
    """Edge-coupled stripline pair (IPC-2141 approximation). Returns (Zdiff, single-ended line)."""
    _positive(spacing=s)
    single = stripline(w, b, er, t)
    return 2 * single.z0 * (1 - 0.374 * math.exp(-2.9 * s / b)), single


def solve_width(z_of_w: Callable[[float], float], target: float, lo: float, hi: float) -> float:
    """Track width giving impedance `target` (impedance falls as the track gets wider)."""
    z_lo, z_hi = z_of_w(lo), z_of_w(hi)
    if not (z_hi <= target <= z_lo):
        raise CalcError(f"{target:g} ohm is out of reach: widths {lo:g}-{hi:g} mm give {z_hi:.1f}-{z_lo:.1f} ohm")
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if z_of_w(mid) > target:
            lo = mid
        else:
            hi = mid
        if hi / lo < 1 + 1e-7:
            break
    return math.sqrt(lo * hi)


# ==================================================================== copper
def trace_width_for_current(amps: float, rise_c: float, oz: float, internal: bool) -> dict:
    """IPC-2221 trace width for a current and temperature rise."""
    _positive(current=amps, temperature_rise=rise_c, copper_weight=oz)
    k = 0.024 if internal else 0.048
    area_mil2 = (amps / (k * rise_c ** 0.44)) ** (1 / 0.725)
    thick_mil = oz * OZ_MM / 0.0254
    w_mil = area_mil2 / thick_mil
    return {"width_mm": w_mil * 0.0254, "width_mil": w_mil, "area_mil2": area_mil2}


def trace_resistance(w_mm: float, length_mm: float, oz: float, temp_c: float = 25.0) -> float:
    _positive(width=w_mm, length=length_mm, copper_weight=oz)
    rho = RHO_CU * (1 + 0.00393 * (temp_c - 20))
    return rho * (length_mm / 1e3) / ((w_mm / 1e3) * (oz * OZ_MM / 1e3))


def skin_depth_mm(f_hz: float, rho: float = RHO_CU, mu_r: float = 1.0) -> float:
    _positive(frequency=f_hz)
    return math.sqrt(rho / (math.pi * f_hz * MU0 * mu_r)) * 1e3


# ==================================================================== RF helpers
def attenuator(db: float, z0: float) -> dict:
    """Matched pi and T attenuator resistor values."""
    _positive(attenuation=db, impedance=z0)
    k = 10 ** (db / 20)
    return {"pi_shunt": z0 * (k + 1) / (k - 1), "pi_series": z0 * (k * k - 1) / (2 * k),
            "t_series": z0 * (k - 1) / (k + 1), "t_shunt": z0 * 2 * k / (k * k - 1)}


def mismatch(gamma: float) -> dict:
    if not 0 <= gamma < 1:
        raise CalcError("|reflection coefficient| must be between 0 and 1")
    return {"gamma": gamma,
            "return_loss_db": float("inf") if gamma == 0 else -20 * math.log10(gamma),
            "vswr": (1 + gamma) / (1 - gamma),
            "mismatch_loss_db": -10 * math.log10(1 - gamma * gamma),
            "reflected_pct": gamma * gamma * 100}


def gamma_from(kind: str, value: float, z0: float = 50.0) -> float:
    if kind == "VSWR":
        if value < 1:
            raise CalcError("VSWR is at least 1")
        return (value - 1) / (value + 1)
    if kind == "Return loss (dB)":
        _positive(return_loss=value)
        return 10 ** (-value / 20)
    if kind == "Load resistance (ohm)":
        _positive(load=value, impedance=z0)
        return abs((value - z0) / (value + z0))
    return value


def resonance_hz(l_h: float, c_f: float) -> float:
    _positive(inductance=l_h, capacitance=c_f)
    return 1 / (2 * math.pi * math.sqrt(l_h * c_f))


# ==================================================================== IPC-7351B land patterns
# solder fillet goals (toe, heel, side) and courtyard excess, per density level A (most), B (nominal), C (least)
FILLETS: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "Chip resistor / capacitor (0603 and larger)": {
        "A": (0.55, 0.00, 0.05, 0.50), "B": (0.35, 0.00, 0.00, 0.25), "C": (0.15, 0.00, -0.05, 0.10)},
    "Chip resistor / capacitor (smaller than 0603)": {
        "A": (0.20, 0.00, 0.05, 0.20), "B": (0.10, 0.00, 0.00, 0.15), "C": (0.02, 0.00, -0.05, 0.10)},
    "Gull-wing leads (pitch over 0.625 mm)": {
        "A": (0.55, 0.45, 0.05, 0.50), "B": (0.35, 0.35, 0.03, 0.25), "C": (0.15, 0.25, 0.01, 0.10)},
    "Gull-wing leads (pitch 0.625 mm or less)": {
        "A": (0.55, 0.45, 0.01, 0.50), "B": (0.35, 0.35, -0.02, 0.25), "C": (0.15, 0.25, -0.04, 0.10)},
    "QFN / DFN / SON (no-lead, bottom terminations)": {
        "A": (0.40, 0.00, -0.04, 0.50), "B": (0.30, 0.00, -0.04, 0.25), "C": (0.20, 0.00, -0.04, 0.10)},
}

# typical datasheet dimensions (min, max) in mm: overall length L, terminal length T, terminal width W, pitch, pins
PRESETS: dict[str, dict] = {
    "0201 (0603 metric)": {"type": "Chip resistor / capacitor (smaller than 0603)", "L": (0.57, 0.63),
                           "T": (0.10, 0.20), "W": (0.27, 0.33)},
    "0402 (1005 metric)": {"type": "Chip resistor / capacitor (smaller than 0603)", "L": (0.95, 1.05),
                           "T": (0.15, 0.35), "W": (0.45, 0.55)},
    "0603 (1608 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (1.50, 1.70),
                           "T": (0.20, 0.50), "W": (0.70, 0.90)},
    "0805 (2012 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (1.80, 2.20),
                           "T": (0.25, 0.65), "W": (1.10, 1.40)},
    "1206 (3216 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (3.00, 3.40),
                           "T": (0.25, 0.75), "W": (1.40, 1.80)},
    "1210 (3225 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (3.00, 3.40),
                           "T": (0.25, 0.75), "W": (2.30, 2.70)},
    "2010 (5025 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (4.80, 5.20),
                           "T": (0.35, 0.85), "W": (2.30, 2.70)},
    "2512 (6332 metric)": {"type": "Chip resistor / capacitor (0603 and larger)", "L": (6.15, 6.55),
                           "T": (0.35, 0.85), "W": (3.00, 3.40)},
    "SOIC-8 (1.27 mm)": {"type": "Gull-wing leads (pitch over 0.625 mm)", "L": (5.80, 6.20), "T": (0.40, 1.27),
                         "W": (0.31, 0.51), "pitch": 1.27},
    "SOT-23 (0.95 mm)": {"type": "Gull-wing leads (pitch over 0.625 mm)", "L": (2.10, 2.64), "T": (0.30, 0.60),
                         "W": (0.30, 0.50), "pitch": 0.95},
    "TSSOP (0.65 mm)": {"type": "Gull-wing leads (pitch over 0.625 mm)", "L": (6.20, 6.60), "T": (0.45, 0.75),
                        "W": (0.19, 0.30), "pitch": 0.65},
    "SSOP / QFP (0.5 mm)": {"type": "Gull-wing leads (pitch 0.625 mm or less)", "L": (8.80, 9.20),
                            "T": (0.45, 0.75), "W": (0.17, 0.27), "pitch": 0.5},
    "QFN 0.5 mm (4x4)": {"type": "QFN / DFN / SON (no-lead, bottom terminations)", "L": (3.90, 4.10),
                         "T": (0.30, 0.50), "W": (0.18, 0.30), "pitch": 0.5},
}


@dataclass
class LandPattern:
    z: float  # outer edge to outer edge of the two pads
    g: float  # inner edge to inner edge
    x: float  # pad width
    pad_length: float
    centre: float  # pad centre to pad centre
    toe: float  # how far the pad reaches past the terminal, per pad (from the nominal part)
    heel: float
    side: float  # per side
    courtyard: float  # courtyard length (along the pads)
    gap_to_next: float | None  # copper gap between neighbouring pads, for leaded parts
    goals: tuple
    notes: list[str] = field(default_factory=list)


def _round_up(v: float, step: float) -> float:
    return math.ceil(v / step - 1e-9) * step if step else v


def _round_down(v: float, step: float) -> float:
    return math.floor(v / step + 1e-9) * step if step else v


def land_pattern(kind: str, level: str, l: tuple, t: tuple, w: tuple, f: float = 0.05, p: float = 0.025,
                 round_to: float = 0.05, pitch: float | None = None, goals: tuple | None = None) -> LandPattern:
    """IPC-7351B land pattern for a two-terminal part or one row of a leaded / no-lead package.

    l: (Lmin, Lmax) terminal-to-terminal (lead tip to lead tip) length; t: (Tmin, Tmax) terminal length;
    w: (Wmin, Wmax) terminal width; f: PCB fabrication tolerance; p: placement tolerance."""
    lmin, lmax = sorted(l)
    tmin, tmax = sorted(t)
    wmin, wmax = sorted(w)
    _positive(Lmin=lmin, Tmin=tmin, Wmin=wmin)
    jt, jh, js, cy = goals if goals else FILLETS[kind][level][:3] + (FILLETS[kind][level][3],)
    # inner spacing between terminals, with its tolerance reduced the IPC way (RMS of the part tolerances)
    smin, smax = lmin - 2 * tmax, lmax - 2 * tmin
    s_rms = math.sqrt((lmax - lmin) ** 2 + 2 * (tmax - tmin) ** 2)
    trim = ((smax - smin) - s_rms) / 2
    smin_rms, smax_rms = smin + trim, smax - trim
    if smin_rms <= 0:
        raise CalcError("the terminals overlap: check L and T")
    cl, cs, cw = lmax - lmin, smax_rms - smin_rms, wmax - wmin
    z = lmin + 2 * jt + math.sqrt(cl ** 2 + f ** 2 + p ** 2)
    g = smax_rms - 2 * jh - math.sqrt(cs ** 2 + f ** 2 + p ** 2)
    x = wmin + 2 * js + math.sqrt(cw ** 2 + f ** 2 + p ** 2)
    z, g, x = _round_up(z, round_to), _round_down(g, round_to), _round_up(x, round_to)
    notes = []
    if g <= 0:
        notes.append("The pads would touch: G came out at 0 or less.")
        g = max(g, 0.0)
    if pitch and x > pitch - 0.1:
        notes.append(f"Pad width {x:.2f} mm leaves less than 0.1 mm between pads at {pitch:g} mm pitch - "
                     "consider density level C or narrower pads.")
    lnom, tnom, wnom = (lmin + lmax) / 2, (tmin + tmax) / 2, (wmin + wmax) / 2
    pad_len = (z - g) / 2
    return LandPattern(
        z=z, g=g, x=x, pad_length=pad_len, centre=(z + g) / 2,
        toe=(z - lnom) / 2, heel=(lnom - 2 * tnom - g) / 2, side=(x - wnom) / 2,
        courtyard=_round_up(z + 2 * cy, 0.01), gap_to_next=(pitch - x) if pitch else None,
        goals=(jt, jh, js, cy), notes=notes)
