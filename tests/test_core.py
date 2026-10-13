"""Offline tests (no network, no KiCad needed) - run on Windows, macOS and Linux in CI."""
from __future__ import annotations

from pathlib import Path

import pytest

from kiporter import bom, fields, kicad_env, library, providers, render, sexpr

ASSETS = Path(__file__).resolve().parent.parent / "kiporter" / "assets"

SYM_LIB = """(kicad_symbol_lib (version 20241209) (generator "test")
  (symbol "OPAMP" (pin_names (offset 0.508))
    (property "Reference" "U" (at 0 5 0) (effects (font (size 1.27 1.27))))
    (property "Value" "OPAMP" (at 0 -5 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "old:SOIC-8" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "MFGR" "TI" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Mouser Price/Stock" "https://x" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "OPAMP_0_1" (rectangle (start -5 5) (end 5 -5) (stroke (width 0.25)) (fill (type background))))
    (symbol "OPAMP_1_1"
      (pin input line (at -7.62 2.54 0) (length 2.54) (name "IN+" (effects (font (size 1 1)))) (number "3" (effects (font (size 1 1)))))
      (pin output line (at 7.62 0 180) (length 2.54) (name "OUT" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
      (arc (start 0 1) (mid 1 0) (end 0 -1))
    )
  )
)
"""

FOOTPRINT = """(footprint "SOIC-8" (layer "F.Cu")
  (fp_line (start -2 -2.5) (end 2 -2.5) (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  (fp_arc (start -1 0) (mid 0 1) (end 1 0) (stroke (width 0.1) (type solid)) (layer "F.Fab"))
  (pad "1" smd roundrect (at -2.7 -1.905) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (pad "2" thru_hole circle (at 2.7 1.905 90) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask"))
  (model "${KICAD9_3DMODEL_DIR}/Package_SO.3dshapes/SOIC-8.step" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
)
"""


def test_sexpr_roundtrip():
    node = sexpr.parse(SYM_LIB)
    again = sexpr.parse(sexpr.dump(node))
    assert again == node
    assert sexpr.unquote('"a\\"b"') == 'a"b'
    assert sexpr.quote('a"b') == '"a\\"b"'


def test_field_normalisation_and_order():
    lib = sexpr.parse(SYM_LIB)
    sym = sexpr.children(lib, "symbol")[0]
    names = fields.field_names()
    changes = fields.normalize_symbol(sym, names)
    assert any("MFGR" in c for c in changes)
    fields.apply_part_data(sym, {"mpn": "LM358", "mouser": "595-LM358", "lcsc": "C7950"}, names)
    props = [sexpr.unquote(p[1]) for p in sexpr.children(sym, "property")]
    assert "Mouser Price/Stock" not in props and "MFGR" not in props
    assert props.index("Manufacturer") < props.index("MPN") < props.index("LCSC Part") < props.index("Mouser PN")
    assert fields.get_property(sym, "Manufacturer") == "TI"


def test_install_links_symbol_footprint_and_model(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "OPAMP.kicad_sym").write_text(SYM_LIB, encoding="utf-8")
    (src / "SOIC-8.kicad_mod").write_text(FOOTPRINT, encoding="utf-8")
    (src / "SOIC-8.step").write_text("ISO-10303-21;", encoding="utf-8")
    target = library._make("project", "Proj", tmp_path / "lib", "${KIPRJMOD}/lib", tmp_path)
    part = library.StagedPart([src / "OPAMP.kicad_sym"], [src / "SOIC-8.kicad_mod"], [src / "SOIC-8.step"],
                              {"mpn": "LM358", "manufacturer": "TI"})
    res = library.install(target, part)
    assert res.symbols == ["Proj:OPAMP"] and res.footprints == ["Proj:SOIC-8"]
    sym = sexpr.children(sexpr.parse(target.sym_file.read_text(encoding="utf-8")), "symbol")[0]
    assert fields.get_property(sym, "Footprint") == "Proj:SOIC-8"
    fp = sexpr.parse((target.fp_dir / "SOIC-8.kicad_mod").read_text(encoding="utf-8"))
    assert sexpr.unquote(sexpr.child(fp, "model")[1]) == "${KIPRJMOD}/lib/Proj.3dshapes/SOIC-8.step"
    assert kicad_env.lib_table_has(tmp_path / "sym-lib-table", "Proj")
    assert library.install(target, part).skipped  # second time: already there


def test_lib_table_edit(tmp_path):
    t = tmp_path / "fp-lib-table"
    assert kicad_env.ensure_lib_table_entry(t, "fp", "A", "${KIPRJMOD}/A.pretty")
    assert not kicad_env.ensure_lib_table_entry(t, "fp", "A", "${KIPRJMOD}/A.pretty")
    assert kicad_env.remove_lib_table_entry(t, "A")
    assert not kicad_env.lib_table_has(t, "A")


def test_env_expand():
    env = {"KIPRJMOD": "/p", "X": "/x"}
    assert kicad_env.expand("${KIPRJMOD}/a/$(X)/b", env) == "/p/a//x/b"
    assert kicad_env.resolve("${NOPE}/a", env) is None


def test_provider_helpers():
    assert providers._float("$1,234.50") == 1234.5
    assert providers._float("0,52 €") == 0.52
    assert providers._int("1,234 In Stock") == 1234
    assert providers._lead_days("12 Weeks") == 84
    assert providers.normalize_package('8-SOIC (0.154", 3.90mm Width)') == "SOIC-8"
    r = providers.PartResult("Mouser", "LM358DT", "ST", supplier_pn="511-LM358DT", stock=5000, lead_days=70,
                             price=0.5, currency="USD")
    assert r.part_data()["mouser"] == "511-LM358DT"
    assert r.stock_str == "5,000" and r.lead_str == "10 wk" and r.price_str == "$0.5"
    best = providers.best_match([providers.PartResult("X", "LM358DT", stock=1), r], "lm358dt")
    assert best is r


def test_renderers_build_geometry():
    lib = sexpr.parse(SYM_LIB)
    d = render.symbol_drawing(lib, "OPAMP")
    assert d.items and d.bbox()[0] < -7
    fp = render.footprint_drawing(sexpr.parse(FOOTPRINT))
    assert fp.items
    pts = render.arc_points((1, 0), (0, 1), (-1, 0))
    assert abs(pts[len(pts) // 2][1] - 1) < 1e-6


def test_signature_logo_footprint_renders():
    fp = sexpr.parse((ASSETS / "signature.kicad_mod").read_text(encoding="utf-8"))
    d = render.footprint_drawing(fp)
    assert len([i for i in d.items if i[0] == "poly"]) >= 3
    assert (ASSETS / "logo.png").exists()


def _lines():
    return [bom.BomLine(["R1", "R2"], 2, "10k", "Lib:R_0603", data={"mpn": "RC0603FR-0710KL", "lcsc": "C25804",
                                                                     "manufacturer": "YAGEO"}, mount="SMD"),
            bom.BomLine(["J1"], 1, "CONN", "Lib:Conn", data={}, mount="THT"),
            bom.BomLine(["U9"], 1, "NC", "Lib:SOT23", dnp=True)]


@pytest.mark.parametrize("fmt", bom.FORMATS, ids=lambda f: f.name)
def test_bom_formats(fmt, tmp_path):
    rows = bom.bom_rows(_lines(), fmt, boards=3)
    assert len(rows) == 3  # header + 2 populated lines
    assert all(len(r) == len(fmt.columns) for r in rows)
    out = bom.export(_lines(), fmt, tmp_path, "proj", 3, file_type="csv",
                     places=[bom.Placement("R1", "10k", "R_0603", 1.0, -2.0, 90, "top")])
    assert all(p.exists() for p in out)


def test_bom_quantities_and_cpl():
    jlc = bom.get_format("JLCPCB")
    assert bom.bom_rows(_lines(), jlc)[1] == ["10k", "R1,R2", "R_0603", "C25804"]
    dk = bom.get_format("Digi-Key BOM Manager")
    assert bom.bom_rows(_lines(), dk, boards=5)[1][0] == 10
    cpl = bom.cpl_rows([bom.Placement("R1", "10k", "R_0603", 1.5, -2.25, 90, "bottom")], "JLCPCB")
    assert cpl[1] == ["R1", "1.5000mm", "-2.2500mm", "Bottom", "90"]
    assert bom.missing_fields(_lines(), jlc) == ["J1: no lcsc"]


def test_platform_detection_does_not_crash():
    installs = kicad_env.find_installs()
    assert isinstance(installs, list)
    ki = kicad_env.detect_kicad()
    assert ki.version and ki.major >= 5
    assert isinstance(kicad_env.kicad_window_titles(), list)
    assert kicad_env.window_project_names("Power [Board 1.1/Power] — Schematic Editor") >= {"Board 1.1"}


@pytest.mark.parametrize("mod", ["gui", "settings_gui", "tools_gui", "fab_gui", "preview_gui", "fab", "cad"])
def test_modules_import(mod):
    import importlib
    assert importlib.import_module(f"kiporter.{mod}")


def test_fab_profiles_and_custom(tmp_path):
    from kiporter import fab
    from kiporter.config import Settings
    jlc = fab.get_profile("JLCPCB")
    assert "--no-x2" in jlc.gerber_flags and "--excellon-units" in jlc.drill_flags
    s = Settings()
    s.custom_fab_profiles = []
    s.custom_fab_profiles = [{"name": "Mine", "layers": ["F.Cu", fab.INNER, "B.Cu"], "drill_separate_th": True,
                              "drill_map": "none", "x2": False}]
    m = fab.get_profile("Mine", s)
    assert m.custom and m.layers('(layers (2 "In1.Cu" signal)) (setup') == ["F.Cu", "In1.Cu", "B.Cu"]
    assert "--excellon-separate-th" in m.drill_flags and "--generate-map" not in m.drill_flags
    assert fab.get_profile("Use the board's own plot settings").gerber_flags == ["--board-plot-params"]


def test_kicad_bom_templates_from_project(tmp_path):
    import json
    from kiporter import bom, kicad_env
    pro = tmp_path / "p.kicad_pro"
    pro.write_text(json.dumps({"schematic": {
        "bom_settings": {"fields_ordered": [{"name": "Reference", "label": "Ref", "show": True, "group_by": False},
                                            {"name": "Value", "label": "Value", "show": True, "group_by": True}]},
        "bom_presets": [{"name": "Mine", "fields_ordered": [{"name": "MPN", "label": "MPN", "show": True}]}],
        "bom_fmt_presets": [{"name": "Pipes", "field_delimiter": "|"}]}}), encoding="utf-8")
    ki = kicad_env.KiCadInstall("10.0", tmp_path)
    names = [t.name for t in bom.kicad_templates(pro, ki)]
    assert names[0].startswith(bom.KICAD_PREFIX + bom.USER_MARK + "Project BOM settings")
    assert any("Mine" in n for n in names) and any("Grouped By Value" in n for n in names)
    assert "Pipes (preset in this project)" in bom.kicad_formats(pro)


def test_user_bom_plugin_is_listed_first(tmp_path):
    from kiporter import bom, kicad_env
    from kiporter.config import Settings
    script = tmp_path / "My_BOM.py"
    script.write_text("import kicad_netlist_reader\n", encoding="utf-8")
    pro = tmp_path / "p.kicad_pro"
    pro.write_text("{}", encoding="utf-8")
    s = Settings()
    s.bom_plugin_paths = {"My_BOM": str(script)}
    ts = bom.kicad_templates(pro, kicad_env.KiCadInstall("10.0", tmp_path), s)
    assert bom.USER_MARK in ts[0].name and ts[0].source == "user" and ts[0].command[0] == str(script)


def test_lcsc_keyword_variants():
    from kiporter.providers import PartResult, _best_keyword_matches, _keyword_variants
    strict, relaxed = _keyword_variants("header 2.54 mm")
    assert strict[0] == "header 2.54 mm" and "2.54mm header" in strict
    assert "2.54mm headers" in relaxed
    assert _keyword_variants("LM358") == (["LM358"], [])
    hits = [PartResult(source="LCSC", mpn="2.54-1*4P", description="Pin Header"),
            PartResult(source="LCSC", mpn="XH-3A", description="Header")]
    assert [r.mpn for r in _best_keyword_matches("header 2.54 mm 1x4", hits)] == ["2.54-1*4P"]


def test_resistor_symbol_style():
    from kiporter import sexpr, symstyle
    src = ('(symbol "R10K" (in_bom yes) (on_board yes) (property "Reference" "R" (id 0) (at 0 5.08 0)) '
           '(property "Value" "R10K" (id 1) (at 0 -5.08 0)) (property "ki_keywords" "{kw}" (id 2) (at 0 0 0)) '
           '(symbol "R10K_0_1" (rectangle (start -2.54 1.02) (end 2.54 -1.02)) '
           '(pin input line (at 5.08 0 180) (length 2.54) (name "2") (number "2")) '
           '(pin input line (at -5.08 0 0) (length 2.54) (name "1") (number "1"))))')
    sym = sexpr.parse(src.format(kw="Resistors"))
    assert symstyle.restyle_symbol(sym, "ansi")
    text = sexpr.dump_inline(sym)
    assert "rectangle" not in text and text.count("polyline") == 5 and text.count("(pin passive") == 2
    assert '(number "1"' in text and '(number "2"' in text and '"R10K_1_1"' in text
    assert symstyle.restyle_symbol(sym, "iec") and "rectangle" in sexpr.dump_inline(sym)
    assert not symstyle.restyle_symbol(sexpr.parse(src.format(kw="Resistors")), "keep")
    assert not symstyle.restyle_symbol(sexpr.parse(src.format(kw="NTC Thermistors")), "ansi")
    assert not symstyle.restyle_symbol(sexpr.parse(src.replace('"Reference" "R"', '"Reference" "C"')), "ansi")


def test_lib_table_wrong_header_is_repaired(tmp_path):
    from kiporter import kicad_env
    t = tmp_path / "fp-lib-table"
    t.write_text("(sym_lib_table\n)\n", encoding="utf-8")
    assert not kicad_env.lib_table_header_ok(t, "fp")
    assert kicad_env.ensure_lib_table_entry(t, "fp", "Parts", "${KIPRJMOD}/libs/Parts.pretty")
    text = t.read_text(encoding="utf-8")
    assert text.startswith("(fp_lib_table") and '(name "Parts")' in text
    assert not kicad_env.ensure_lib_table_entry(t, "fp", "Parts", "${KIPRJMOD}/libs/Parts.pretty")
    t.write_text("(sym_lib_table\n)\n", encoding="utf-8")
    assert kicad_env.fix_lib_table_header(t, "fp")
    assert t.read_text(encoding="utf-8") == "(fp_lib_table\n\t(version 7)\n)\n"


def test_bom_alternatives():
    from kiporter import alternatives as alt
    from kiporter.bom import BomLine
    from kiporter.providers import PartResult
    assert alt.schematic_value("4k7", "resistor") == 4700 and alt.schematic_value("100R", "resistor") == 100
    assert alt.schematic_value("10k 1%", "resistor") == 10000 and alt.schematic_value("0603WAF1002T5E", "resistor") is None
    assert abs(alt.schematic_value("100n", "capacitor") - 1e-7) < 1e-12 and alt.schematic_value("100", "capacitor") is None
    assert abs(alt.schematic_value("4u7", "capacitor") - 4.7e-6) < 1e-12
    assert alt.values_in("Thick Film Resistors - SMD 10 kOhms 1%", "resistor") == [10000.0]
    assert abs(alt.values_in("MLCC 0.1uF 50V X7R", "capacitor")[0] - 1e-7) < 1e-12
    assert alt.kind_of(["R1", "R22"]) == "resistor" and alt.kind_of(["RN1"]) is None and alt.kind_of(["R1", "C1"]) is None

    ln = BomLine(refs=["R1", "R2"], qty=2, value="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    assert alt.keyword(ln) == "10k 0603 resistor"
    hits = [PartResult(source="LCSC", mpn="A", description="10kΩ ±1% 100mW", package="0603", stock=500, price=0.01),
            PartResult(source="LCSC", mpn="B", description="10kΩ ±1% 125mW", package="0805", stock=9000),
            PartResult(source="LCSC", mpn="C", description="1kΩ ±1% 100mW", package="0603", stock=9000),
            PartResult(source="LCSC", mpn="D", description="10kΩ ±5% 100mW", package="0603", stock=0)]
    assert alt.passive_pick(ln, hits, 10).part.mpn == "A"

    ic = BomLine(refs=["U1"], qty=1, value="LM358", footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                 data={"mpn": "LM358DR"})
    fam = [PartResult(source="Mouser", mpn="LM358DR", stock=0),
           PartResult(source="Mouser", mpn="LM358DR2G", package="SOIC-8", stock=700),
           PartResult(source="Mouser", mpn="LM358PWR", package="TSSOP-8", stock=9000),
           PartResult(source="Mouser", mpn="LM324DR", package="SOIC-14", stock=9000)]
    assert alt.family_pick(ic, fam, 5).part.mpn == "LM358DR2G"
    assert alt.keyword(ic) is None


def test_bom_switch_to_lcsc_suggestion():
    from kiporter import alternatives as alt
    from kiporter.bom import BomLine
    from kiporter.providers import PartResult
    ic = BomLine(refs=["U1"], qty=1, value="LM358", footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                 data={"mpn": "LM358DR", "manufacturer": "TI"}, offers={"Mouser": None})
    res = [PartResult(source="LCSC", mpn="LM358DR", supplier_pn="C5423", package="SOIC-8", stock=900),
           PartResult(source="LCSC", mpn="LM358DR2G", supplier_pn="C7950", package="SOIC-8", stock=99999)]
    a = alt.suggest(ic, res, 10)
    assert a.exact and a.part.supplier_pn == "C5423"
    alt.switch(ic, a)
    assert ic.lcsc == "C5423" and ic.mpn == "LM358DR" and ic.offers["LCSC"].stock == 900 and ic.offers["Mouser"] is None
    assert alt.switch_back(ic) and ic.lcsc == "" and "LCSC" not in ic.offers and not alt.switch_back(ic)

    b = alt.suggest(ic, res, 5000)  # LCSC lists the part, so it is suggested even though it is short
    assert b.exact and b.part.supplier_pn == "C5423"
    ic.data["lcsc"] = "C5423"
    assert alt.suggest(ic, res, 5000) is None  # already an LCSC part: nothing to recommend
    ic.data["lcsc"] = ""
    fam = alt.suggest(ic, res[1:], 10)  # LCSC does not list it: the family member is offered
    assert not fam.exact and fam.part.supplier_pn == "C7950"
    alt.switch(ic, fam)
    assert ic.mpn == "LM358DR2G" and ic.lcsc == "C7950" and ic.offers["Mouser"] == "re-check"

    r = BomLine(refs=["R1"], qty=1, value="10k", footprint="Resistor_SMD:R_0603_1608Metric")
    hits = [PartResult(source="LCSC", mpn="RC0603", supplier_pn="C25804", description="10kΩ ±1%", package="0603", stock=5000)]
    c = alt.suggest(r, [], 1, lambda kw: hits if kw == "10k 0603 resistor" else [])
    assert c.part.supplier_pn == "C25804" and not c.exact


def test_bom_value_used_as_part_number():
    from kiporter import alternatives as alt
    from kiporter.bom import BomLine, stock_status
    from kiporter.providers import PartResult
    q = BomLine(refs=["Q8", "Q9"], qty=2, value="MMBT2222A", footprint="Project_Parts:SOT-23")
    assert alt.value_part_number(q) == "MMBT2222A"
    assert alt.value_part_number(BomLine(refs=["R1"], qty=1, value="4k7", footprint="x:R_0603")) == ""
    assert alt.value_part_number(BomLine(refs=["J1"], qty=1, value="Conn", footprint="x:y")) == ""
    assert alt.value_part_number(BomLine(refs=["U1"], qty=1, value="LM358DR", footprint="x:y", data={"mpn": "LM358DR"})) == ""
    res = [PartResult(source="LCSC", mpn="MMBT2222A", supplier_pn="C83939", package="SOT-23", stock=182289),
           PartResult(source="LCSC", mpn="MMBT2222ALT1G", supplier_pn="C82460", package="SOT-23", stock=1434482),
           PartResult(source="LCSC", mpn="MMBT2222AWT1G", supplier_pn="C28273", package="SC-70(SOT-323)", stock=9999999),
           PartResult(source="LCSC", mpn="MMBT3904", supplier_pn="C20526", package="SOT-23", stock=9999999)]
    a = alt.suggest(q, res, 6)
    assert a.part.mpn == "MMBT2222ALT1G" and not a.exact
    assert stock_status(q, 6) == "no MPN"
    q.offers = {"LCSC": a.part}
    assert stock_status(q, 6) == "ok"
    alt.switch(q, a)
    assert q.lcsc == "C82460" and q.mpn == "MMBT2222ALT1G" and q.value == "MMBT2222ALT1G"
    assert alt.switch_back(q) and q.value == "MMBT2222A" and q.lcsc == ""


def test_schematic_fields_are_written_in_place(tmp_path):
    from kiporter import fields, schedit
    sub_sheet = '''(kicad_sch (version 20250114) (generator "eeschema")
  (symbol (lib_id "Device:Q_NPN") (at 10 10 0) (unit 1)
    (property "Reference" "Q1" (at 12 9 0) (effects (font (size 1.27 1.27))))
    (property "Value" "MMBT2222A" (at 12 11 0) (effects (font (size 1.27 1.27))))
    (property "Datasheet" "~" (at 10 10 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "LCSC" "" (at 10 10 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances (project "p" (path "/a/b" (reference "Q8") (unit 1))))))'''
    root = '''(kicad_sch (version 20250114) (generator "eeschema")
  (lib_symbols (symbol "Device:R" (property "Reference" "R" (at 0 0 0))))
  (symbol (lib_id "Device:R") (at 5 5 0) (unit 1)
    (property "Reference" "R1" (at 6 5 0) (effects (font (size 1.27 1.27))))
    (property "Datasheet" "~" (at 5 5 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (instances (project "p" (path "/a" (reference "R1") (unit 1)))))
  (sheet (at 0 0) (size 10 10) (property "Sheetname" "S") (property "Sheetfile" "sub.kicad_sch")))'''
    (tmp_path / "root.kicad_sch").write_text(root, encoding="utf-8")
    (tmp_path / "sub.kicad_sch").write_text(sub_sheet, encoding="utf-8")
    names = fields.field_names()
    upd = {"Q8": {"lcsc": "C82460", "mpn": "MMBT2222ALT1G", "value": "MMBT2222ALT1G"}, "R1": {"lcsc": "C25804"}}
    count, files, texts = schedit.set_fields(tmp_path / "root.kicad_sch", upd, names)
    assert count == 2 and len(files) == 2
    schedit.write(texts)
    sub_text = (tmp_path / "sub.kicad_sch").read_text(encoding="utf-8")
    assert '(property "LCSC" "C82460"' in sub_text and '"MPN" "MMBT2222ALT1G"' in sub_text  # alias reused, MPN added
    assert '(property "Value" "MMBT2222ALT1G"' in sub_text and "MMBT2222A\"" not in sub_text
    root_text = (tmp_path / "root.kicad_sch").read_text(encoding="utf-8")
    assert '"LCSC Part" "C25804"' in root_text and '(symbol "Device:R"' in root_text
    assert root_text.count("C25804") == 1  # the library copy of the symbol is left alone
    assert schedit.set_fields(tmp_path / "root.kicad_sch", upd, names)[0] == 0


def test_lcsc_number_search_keeps_the_exact_part():
    from kiporter.providers import PartResult, _best_keyword_matches
    hits = [PartResult(source="LCSC", mpn="1N4148WS", supplier_pn="C2128", description="Switching diode"),
            PartResult(source="LCSC", mpn="XC2128-ABC", supplier_pn="C3826377", description="Something else")]
    assert "C2128" in [r.supplier_pn for r in _best_keyword_matches("C2128", hits)]
