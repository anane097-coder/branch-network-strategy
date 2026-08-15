"""Generate an openable Power BI project (.pbip) from the exported model.

    python scripts/14_generate_pbip.py

PBIP is the text-authorable Power BI format: a TMDL semantic model and a JSON
report definition, both of which Power BI Desktop opens and saves like any
.pbix. Generating it rather than clicking it built has the same justification
as everything else here - it re-runs against a new vintage, and the diff of a
dashboard change is readable.

lineageTag GUIDs are derived with uuid5 from a fixed namespace, so
regenerating produces byte-identical files rather than a diff of new random
identifiers every run.

WHAT IS NOT DECLARED HERE IS THE COLUMN TYPES - THEY COME FROM THE CSVs, WITH
ONE OVERRIDE THAT MATTERS. Identifier columns are forced to text. A tract
GEOID or county FIPS read as a number loses nothing visible in this footprint
(Illinois begins 17, Wisconsin 55, so no leading zero is dropped) and then
silently fails every join. That failure mode is why the type list is explicit
rather than left to Power Query's inference.
"""
import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "powerbi" / "data"
PROJECT = "branch-network-strategy"
BASE = ROOT / "powerbi"
NS = uuid.UUID("6f1d4f7a-2c3b-4e58-9a10-1f2c3d4e5f60")   # fixed: stable GUIDs

# Identifiers. Never numeric, whatever they look like.
TEXT_COLUMNS = {
    "tract_geoid", "county_fips", "cbsa", "uninumbr", "cert", "lei",
    "service_type", "is_main_office", "state", "year_str", "action_taken",
    "size_quartile",
}

# Tables that must NOT participate in relationships. Each is a footprint-level
# constant or a parameter set; connecting them would let a page filter change
# a number that is not supposed to vary by page filter.
DISCONNECTED = {"lmi_coverage", "recommended_sites", "ref_index_weights",
                "fact_county_deposit_growth", "market_share"}

RELATIONSHIPS = [
    # (from_table, from_col, to_table, to_col)
    ("fact_branch_deposits", "uninumbr", "dim_branch", "uninumbr"),
    ("fact_branch_deposits", "cert", "dim_institution", "cert"),
    ("fact_branch_deposits", "year", "dim_year", "year"),
    ("bridge_branch_catchment", "uninumbr", "dim_branch", "uninumbr"),
    ("bridge_branch_catchment", "tract_geoid", "dim_tract", "tract_geoid"),
    ("branch_performance", "uninumbr", "dim_branch", "uninumbr"),
    ("fact_tract_competition", "tract_geoid", "dim_tract", "tract_geoid"),
    ("tract_capture_rate", "tract_geoid", "dim_tract", "tract_geoid"),
    ("unmet_demand", "tract_geoid", "dim_tract", "tract_geoid"),
    ("opportunity_index", "tract_geoid", "dim_tract", "tract_geoid"),
    ("fact_index_components", "tract_geoid", "dim_tract", "tract_geoid"),
    ("recommended_coverage", "tract_geoid", "dim_tract", "tract_geoid"),
]
# NOTE: dim_branch[tract_geoid] -> dim_tract is deliberately ABSENT. With the
# bridge in place it would close a loop (branch -> bridge -> tract and
# branch -> tract), and Power BI would silently deactivate one of them. The
# branch's home tract stays available as a column.
# fact_county_deposit_growth joins on county_fips, which is not unique in
# dim_tract; it is left disconnected rather than modelled many-to-many, where
# the filter direction would be a guess.

WHATIF = [
    ("w Household Growth", 0.20),
    ("w Median Income", 0.15),
    ("w Deposit Growth", 0.25),
    ("w Competitor Saturation", 0.20),
    ("w Unmet Demand", 0.20),
]

COMPONENT_OF = {
    "w Household Growth": "household_growth",
    "w Median Income": "median_income",
    "w Deposit Growth": "deposit_market_growth",
    "w Competitor Saturation": "competitor_saturation",
    "w Unmet Demand": "unmet_mortgage_demand",
}


def tag(*parts) -> str:
    return str(uuid.uuid5(NS, "|".join(parts)))


# Columns that MUST arrive as booleans. Every one is three-valued in this
# project - true, false, and not-determined - and every one is compared
# against TRUE in DAX. A three-valued column reads as object dtype in pandas,
# so naive inference types it as text, and `lmi_flag = TRUE` then matches
# nothing at all: no error, an empty card, and an equity figure of zero that
# looks like a finding. Asserted rather than hoped for.
MUST_BE_BOOLEAN = {
    ("dim_tract", "lmi_flag"),
    ("opportunity_index", "lmi_flag"),
    ("opportunity_index", "growth_is_estimated"),
    ("recommended_sites", "lmi_flag"),
    ("recommended_sites", "growth_is_estimated"),
    ("dim_branch", "is_subject_bank"),
    ("dim_institution", "is_subject_bank"),
    ("unmet_demand", "lmi_flag"),
    ("tract_capture_rate", "lmi_flag"),
}
BOOL_LITERALS = {True, False, "True", "False", "true", "false"}


def pbi_types(df: pd.DataFrame, table: str):
    """(tmdl dataType, M type) per column, identifiers forced to text."""
    out = {}
    for col in df.columns:
        if col in TEXT_COLUMNS:
            out[col] = ("string", "type text")
            continue
        k = df[col].dtype.kind
        if k == "b":
            out[col] = ("boolean", "type logical")
        elif k == "i":
            out[col] = ("int64", "Int64.Type")
        elif k == "f":
            out[col] = ("double", "type number")
        else:
            # A three-valued boolean arrives here, as object dtype, because
            # of the nulls. Recover it on VALUES rather than on dtype.
            vals = set(df[col].dropna().unique())
            if vals and vals <= BOOL_LITERALS:
                out[col] = ("boolean", "type logical")
            else:
                out[col] = ("string", "type text")
    for tbl, col in MUST_BE_BOOLEAN:
        if tbl == table and col in out and out[col][0] != "boolean":
            raise SystemExit(
                f"{table}[{col}] must be boolean for the DAX to work and was "
                f"typed {out[col][0]}. Values seen: "
                f"{sorted(set(df[col].dropna().unique()))[:6]}")
    return out


def m_partition(table: str, types: dict) -> str:
    casts = ", ".join(f'{{"{c}", {m}}}' for c, (_, m) in types.items())
    return (
        "\t\t\tlet\n"
        f'\t\t\t    Source = Csv.Document(File.Contents(DataFolder & "\\{table}.csv"), '
        "[Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "\t\t\t    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n"
        f"\t\t\t    Typed = Table.TransformColumnTypes(Promoted, {{{casts}}})\n"
        "\t\t\tin\n"
        "\t\t\t    Typed"
    )


def table_tmdl(table: str, df: pd.DataFrame, measures: str = "") -> str:
    types = pbi_types(df, table)
    lines = [f"table {table}", f"\tlineageTag: {tag('table', table)}", ""]
    for col, (dt, _) in types.items():
        lines += [
            f"\tcolumn {col}",
            f"\t\tdataType: {dt}",
            f"\t\tlineageTag: {tag('col', table, col)}",
            "\t\tsummarizeBy: none",
            f"\t\tsourceColumn: {col}",
            "",
            "\t\tannotation SummarizationSetBy = Automatic",
            "",
        ]
    if measures:
        lines.append(measures)
    lines += [
        f"\tpartition {table} = m",
        "\t\tmode: import",
        "\t\tsource =",
        m_partition(table, types),
        "",
        "\tannotation PBI_ResultType = Table",
        "",
    ]
    return "\n".join(lines)


def measure(name: str, expr: str, table: str, fmt: str | None = None) -> str:
    body = "\n".join(f"\t\t\t{l}" if l.strip() else "" for l in expr.strip().split("\n"))
    out = [f"\tmeasure '{name}' =", body, f"\t\tlineageTag: {tag('m', table, name)}"]
    if fmt:
        out.append(f'\t\tformatString: {fmt}')
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# The measures. Kept in sync with powerbi/measures.dax by being generated from
# the same list - one definition, not two files that drift.
# --------------------------------------------------------------------------
DEPOSIT_MEASURES = [
    ("Total Deposits", "SUM ( fact_branch_deposits[deposits] )", "#,0"),
    ("Total Deposits ($bn)", "DIVIDE ( [Total Deposits], 1000000000 )", "#,0.00"),
    ("Branch Count", "DISTINCTCOUNT ( fact_branch_deposits[uninumbr] )", "#,0"),
    ("Subject Deposits",
     'CALCULATE ( [Total Deposits], dim_institution[is_subject_bank] = TRUE )', "#,0"),
    ("Market Deposits",
     "CALCULATE ( [Total Deposits], REMOVEFILTERS ( dim_institution ) )", "#,0"),
    ("Market Share %",
     "DIVIDE ( [Subject Deposits], [Market Deposits] ) * 100", "#,0.00"),
    ("Deposit CAGR %", """VAR FirstYear = MIN ( dim_year[year] )
VAR LastYear = MAX ( dim_year[year] )
VAR Span = LastYear - FirstYear
VAR StartDep = CALCULATE ( [Total Deposits], dim_year[year] = FirstYear )
VAR EndDep = CALCULATE ( [Total Deposits], dim_year[year] = LastYear )
RETURN
    IF (
        Span > 0 && NOT ISBLANK ( StartDep ) && StartDep > 0 && NOT ISBLANK ( EndDep ),
        ( ( EndDep / StartDep ) ^ ( 1 / Span ) - 1 ) * 100
    )""", "#,0.00"),
]

PERF_MEASURES = [
    ("Performance Index", "AVERAGE ( branch_performance[index_size_adjusted] )", "#,0.000"),
    ("Performance Index (raw, do not rank)",
     "AVERAGE ( branch_performance[performance_index] )", "#,0.000"),
    ("Underperforming Branches",
     "CALCULATE ( COUNTROWS ( branch_performance ), branch_performance[index_size_adjusted] < 1 )",
     "#,0"),
]

COVERAGE_MEASURES = [
    ("LMI Coverage %",
     'CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )',
     "#,0.00"),
    ("LMI Coverage % (recommended)",
     'CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "B_constrained" )',
     "#,0.00"),
    ("Coverage Delta Gap (pp)", """VAR Rule = SELECTEDVALUE ( lmi_coverage[rule], "B_constrained" )
VAR CurLMI = CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )
VAR CurNon = CALCULATE ( MAX ( lmi_coverage[non_lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )
VAR NewLMI = CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = Rule )
VAR NewNon = CALCULATE ( MAX ( lmi_coverage[non_lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = Rule )
RETURN ( NewLMI - CurLMI ) - ( NewNon - CurNon )""", "#,0.000"),
    ("Binding Test Result", """IF (
    ISBLANK ( [Coverage Delta Gap (pp)] ),
    "NOT MEASURED",
    IF ( [Coverage Delta Gap (pp)] >= 0, "PASS - expansion is proportional", "FAIL - non-LMI coverage grows faster" )
)""", None),
    ("Constraint Cost (index points)", """VAR A = CALCULATE ( SUM ( recommended_sites[opportunity_score] ), ALL ( recommended_sites ), recommended_sites[rule] = "A_commercial" )
VAR B = CALCULATE ( SUM ( recommended_sites[opportunity_score] ), ALL ( recommended_sites ), recommended_sites[rule] = "B_constrained" )
RETURN A - B""", "#,0.000"),
    ("Constraint Cost %", """VAR A = CALCULATE ( SUM ( recommended_sites[opportunity_score] ), ALL ( recommended_sites ), recommended_sites[rule] = "A_commercial" )
RETURN DIVIDE ( [Constraint Cost (index points)], A ) * 100""", "#,0.0"),
    ("LMI Sites in Shortlist", """CALCULATE (
    COUNTROWS ( recommended_sites ),
    recommended_sites[lmi_flag] = TRUE,
    recommended_sites[rule] = "B_constrained"
) + 0""", "#,0"),
]

TRACT_MEASURES = [
    ("LMI Share of Selection %", """VAR Determined = CALCULATE ( COUNTROWS ( dim_tract ), NOT ISBLANK ( dim_tract[lmi_flag] ) )
VAR IsLMI = CALCULATE ( COUNTROWS ( dim_tract ), dim_tract[lmi_flag] = TRUE )
RETURN DIVIDE ( IsLMI, Determined ) * 100""", "#,0.0"),
    ("Cluster-Measured Growth %", """DIVIDE (
    CALCULATE ( COUNTROWS ( dim_tract ), dim_tract[growth_basis] <> "direct" ),
    CALCULATE ( COUNTROWS ( dim_tract ), NOT ISBLANK ( dim_tract[household_growth_pct] ) )
) * 100""", "#,0.0"),
    ("Shortlist Caveat", """"LMI " & FORMAT ( [LMI Share of Selection %], "0.0" ) & "% vs 29.5% footprint  |  growth estimated for "
    & FORMAT ( [Cluster-Measured Growth %], "0.0" ) & "% of these tracts\"""", None),
]

CAPTURE_MEASURES = [
    ("Capture Rate %",
     "DIVIDE ( SUM ( tract_capture_rate[subject_originations] ), SUM ( tract_capture_rate[all_originations] ) ) * 100",
     "#,0.00"),
    ("Unmet Originations", "SUM ( tract_capture_rate[competitor_originations] )", "#,0"),
]

INDEX_MEASURES = [
    ("Published Opportunity Score",
     "AVERAGE ( opportunity_index[opportunity_score] )", "#,0.000"),
]

WHATIF_MEASURES = [
    ("Weight Total",
     " + ".join(f"[{n} Value]" for n, _ in WHATIF), "#,0.00"),
    ("Selected Opportunity Score", """VAR W = [Weight Total]
VAR Contribution =
    SUMX (
        VALUES ( fact_index_components[component] ),
        VAR C = fact_index_components[component]
        VAR Z = CALCULATE ( AVERAGE ( fact_index_components[z_score] ) )
        VAR Weight =
            SWITCH (
                C,
""" + ",\n".join(
        f'                "{COMPONENT_OF[n]}", [{n} Value]' for n, _ in WHATIF
    ) + """,
                0
            )
        RETURN Z * Weight
    )
RETURN DIVIDE ( Contribution, W )""", "#,0.000"),
]


def build_model():
    tables = {}
    for csv in sorted(DATA.glob("*.csv")):
        if csv.stem.startswith("_"):
            continue
        tables[csv.stem] = pd.read_csv(
            csv, dtype={c: "string" for c in TEXT_COLUMNS}, low_memory=False)

    home = {
        "fact_branch_deposits": DEPOSIT_MEASURES,
        "branch_performance": PERF_MEASURES,
        "lmi_coverage": COVERAGE_MEASURES,
        "dim_tract": TRACT_MEASURES,
        "tract_capture_rate": CAPTURE_MEASURES,
        "opportunity_index": INDEX_MEASURES,
        "fact_index_components": WHATIF_MEASURES,
    }

    sm = BASE / f"{PROJECT}.SemanticModel"
    defn = sm / "definition"
    if sm.exists():
        shutil.rmtree(sm)
    (defn / "tables").mkdir(parents=True)

    for name, df in tables.items():
        ms = "".join(measure(n, e, name, f) for n, e, f in home.get(name, []))
        (defn / "tables" / f"{name}.tmdl").write_text(
            table_tmdl(name, df, ms), encoding="utf-8")

    # What-if parameter tables: calculated, disconnected, one per component.
    for pname, default in WHATIF:
        col = pname
        body = [
            f"table '{pname}'",
            f"\tlineageTag: {tag('table', pname)}",
            "",
            f"\tcolumn '{col}'",
            "\t\tdataType: double",
            f"\t\tlineageTag: {tag('col', pname, col)}",
            "\t\tsummarizeBy: none",
            "\t\tsourceColumn: [Value]",
            "\t\tsortByColumn: ",
            "",
            "\t\tannotation SummarizationSetBy = Automatic",
            "",
            f"\tmeasure '{pname} Value' = SELECTEDVALUE ( '{pname}'[{col}], {default} )",
            f"\t\tlineageTag: {tag('m', pname, 'value')}",
            "\t\tformatString: #,0.00",
            "",
            f"\tpartition '{pname}' = calculated",
            "\t\tmode: import",
            "\t\tsource = GENERATESERIES ( 0, 0.5, 0.05 )",
            "",
            "\tannotation PBI_Id = ParameterTable",
            "",
        ]
        txt = "\n".join(body).replace("\t\tsortByColumn: \n", "")
        (defn / "tables" / f"{pname}.tmdl").write_text(txt, encoding="utf-8")

    # Relationships
    rels = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        if ft not in tables or tt not in tables:
            raise SystemExit(f"Relationship references a missing table: {ft}/{tt}")
        if fc not in tables[ft].columns or tc not in tables[tt].columns:
            raise SystemExit(f"Relationship references a missing column: "
                             f"{ft}[{fc}] -> {tt}[{tc}]")
        rels += [
            f"relationship {tag('rel', ft, fc, tt, tc)}",
            f"\tfromColumn: {ft}.{fc}",
            f"\ttoColumn: {tt}.{tc}",
            "",
        ]
    (defn / "relationships.tmdl").write_text("\n".join(rels), encoding="utf-8")

    # The data folder parameter. Absolute, because Power Query has no notion of
    # "next to the project file" - stated in the README so it is changed once.
    folder = str((DATA).resolve()).replace("\\", "\\")
    (defn / "expressions.tmdl").write_text(
        "expression DataFolder = "
        f'"{folder}" meta [IsParameterQuery=true, Type="Text", '
        "IsParameterQueryRequired=true]\n"
        f"\tlineageTag: {tag('expr', 'DataFolder')}\n\n"
        "\tannotation PBI_NavigationStepName = Navigation\n\n"
        "\tannotation PBI_ResultType = Text\n", encoding="utf-8")

    # en-US, not the machine locale. The CSVs use "." decimals and "," field
    # separators; a culture that reads them the other way round would not
    # error, it would silently reinterpret every number in the model.
    order = sorted(tables) + [p for p, _ in WHATIF]
    (defn / "model.tmdl").write_text(
        "model Model\n"
        "\tculture: en-US\n"
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tdiscourageImplicitMeasures\n"
        "\tsourceQueryCulture: en-US\n"
        "\tdataAccessOptions\n"
        "\t\tlegacyRedirects\n"
        "\t\treturnErrorValuesAsNull\n"
        "\n"
        # Model-level annotations are INDENTED under the model block. At
        # column 0 they parse as a sibling declaration and the model fails
        # to load - the sort of whitespace dependency that is invisible in a
        # diff and total in effect.
        "\tannotation PBI_QueryOrder = " + json.dumps(order) + "\n"
        "\n"
        "\tannotation __PBI_TimeIntelligenceEnabled = 0\n"
        "\n"
        + "\n".join(f"ref table {chr(39)}{t}{chr(39)}" if " " in t
                    else f"ref table {t}" for t in order)
        + "\n\nref cultureInfo en-US\n", encoding="utf-8")

    (defn / "cultures").mkdir(exist_ok=True)
    (defn / "cultures" / "en-US.tmdl").write_text(
        "cultureInfo en-US\n\n\tlinguisticMetadata =\n\t\t\t{\n"
        '\t\t\t  "Version": "1.0.0",\n\t\t\t  "Language": "en-US"\n\t\t\t}\n'
        "\t\tcontentType: json\n", encoding="utf-8")

    (sm / "definition.pbism").write_text(json.dumps({
        "version": "4.2",
        "settings": {},
    }, indent=2), encoding="utf-8")
    (sm / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": PROJECT},
        "config": {"version": "2.0", "logicalId": tag("logical", "model")},
    }, indent=2), encoding="utf-8")
    return tables


# ==========================================================================
# Report definition
# ==========================================================================
# Where a measure lives, so a visual's query names the right entity.
MEASURE_HOME = {}
for _tbl, _ms in (("fact_branch_deposits", DEPOSIT_MEASURES),
                  ("branch_performance", PERF_MEASURES),
                  ("lmi_coverage", COVERAGE_MEASURES),
                  ("dim_tract", TRACT_MEASURES),
                  ("tract_capture_rate", CAPTURE_MEASURES),
                  ("opportunity_index", INDEX_MEASURES),
                  ("fact_index_components", WHATIF_MEASURES)):
    for _n, *_ in _ms:
        MEASURE_HOME[_n] = _tbl
for _p, _ in WHATIF:
    MEASURE_HOME[f"{_p} Value"] = _p


def _src(alias, entity):
    return {"Name": alias, "Entity": entity, "Type": 0}


def field(spec):
    """'table:column' for a column, 'measure:Name' for a measure."""
    kind, ref = spec.split(":", 1)
    if kind == "measure":
        entity = MEASURE_HOME[ref]
        alias = entity[0].lower()
        return (entity, alias,
                {"Measure": {"Expression": {"SourceRef": {"Source": alias}},
                             "Property": ref},
                 "Name": f"{entity}.{ref}", "NativeReferenceName": ref},
                f"{entity}.{ref}")
    entity, col = kind, ref
    alias = entity[0].lower()
    return (entity, alias,
            {"Column": {"Expression": {"SourceRef": {"Source": alias}},
                        "Property": col},
             "Name": f"{entity}.{col}", "NativeReferenceName": col},
            f"{entity}.{col}")


def visual(vtype, x, y, w, h, roles, title=None, z=0, objects=None,
           sort=None):
    froms, selects, projections = {}, [], {}
    for role, specs in roles.items():
        projections[role] = []
        for s in specs:
            entity, alias, sel, qref = field(s)
            froms[alias] = entity
            if sel not in selects:
                selects.append(sel)
            projections[role].append({"queryRef": qref})
    proto = {
        "Version": 2,
        "From": [_src(a, e) for a, e in froms.items()],
        "Select": selects,
    }
    if sort:
        _, alias, _, qref = field(sort[0])
        entity = froms[alias]
        expr = ({"Measure": {"Expression": {"SourceRef": {"Source": alias}},
                             "Property": sort[0].split(":", 1)[1]}}
                if sort[0].startswith("measure:") else
                {"Column": {"Expression": {"SourceRef": {"Source": alias}},
                            "Property": sort[0].split(":", 1)[1]}})
        proto["OrderBy"] = [{"Direction": 2 if sort[1] == "desc" else 1,
                             "Expression": expr}]
    sv = {
        "visualType": vtype,
        "projections": projections,
        "prototypeQuery": proto,
        "drillFilterOtherVisuals": True,
    }
    if objects:
        sv["objects"] = objects
    if title:
        sv["vcObjects"] = {"title": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
        }}]}
    cfg = {"name": tag("vis", vtype, title or "", str(x), str(y)),
           "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                              "width": w, "height": h}}],
           "singleVisual": sv}
    return {"config": json.dumps(cfg), "filters": "[]",
            "height": h, "width": w, "x": x, "y": y, "z": z}


def textbox(x, y, w, h, runs, z=0):
    cfg = {"name": tag("txt", str(x), str(y), runs[0]["value"][:24]),
           "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                              "width": w, "height": h}}],
           "singleVisual": {
               "visualType": "textbox",
               "drillFilterOtherVisuals": True,
               "objects": {"general": [{"properties": {
                   "paragraphs": [{"textRuns": runs}]}}]}}}
    return {"config": json.dumps(cfg), "filters": "[]",
            "height": h, "width": w, "x": x, "y": y, "z": z}


def run(text, size="12pt", bold=False, colour=None):
    style = {"fontSize": size, "fontFamily": "Segoe UI"}
    if bold:
        style["fontWeight"] = "bold"
    if colour:
        style["color"] = colour
    return {"value": text, "textStyle": style}


INK, WARN, GOOD = "#1F2933", "#B23A22", "#1B6B4A"


def build_report():
    pages = []

    # ---- Page 1: Market opportunity -------------------------------------
    p1 = [
        textbox(16, 12, 900, 44, [run("Market opportunity", "20pt", True, INK)]),
        visual("card", 16, 68, 240, 110, {"Values": ["measure:Total Deposits ($bn)"]},
               "Footprint deposits ($bn)"),
        visual("card", 268, 68, 240, 110, {"Values": ["measure:Deposit CAGR %"]},
               "Deposit CAGR % (selected years)"),
        visual("card", 520, 68, 240, 110, {"Values": ["measure:Market Share %"]},
               "Subject market share %"),
        visual("card", 772, 68, 240, 110, {"Values": ["measure:Branch Count"]},
               "Branches in view"),
        visual("slicer", 1024, 68, 240, 110, {"Values": ["dim_tract:tier"]}, "Tier"),
        visual("tableEx", 16, 192, 620, 300, {"Values": [
            "dim_tract:county_name", "measure:Published Opportunity Score",
            "measure:LMI Share of Selection %",
            "measure:Cluster-Measured Growth %"]},
            "Markets by opportunity score",
            sort=("measure:Published Opportunity Score", "desc")),
        visual("columnChart", 648, 192, 616, 300,
               {"Category": ["dim_tract:cbsa_title"],
                "Y": ["measure:Published Opportunity Score"]},
               "Opportunity by CBSA",
               sort=("measure:Published Opportunity Score", "desc")),
        textbox(16, 504, 1248, 60, [
            run("The index is z-score normalised. min-max was abandoned because "
                "17 tracts report the ACS top-code of $250,001 — a censored "
                "bound, not a measurement — and min-max anchors the whole scale "
                "on it.", "10pt", False, INK)]),
        visual("slicer", 16, 576, 240, 120, {"Values": ["dim_branch:state"]},
               "State"),
        visual("tableEx", 268, 576, 996, 120, {"Values": [
            "fact_county_deposit_growth:county_fips",
            "fact_county_deposit_growth:cagr_pct_total",
            "fact_county_deposit_growth:cagr_pct_retail",
            "fact_county_deposit_growth:excluded_deposit_share"]},
            "County deposit growth — total vs retail basis (booking centres removed)"),
    ]
    pages.append(("Market opportunity", p1))

    # ---- Page 2: Branch performance -------------------------------------
    p2 = [
        textbox(16, 12, 900, 44, [run("Branch performance", "20pt", True, INK)]),
        visual("scatterChart", 16, 68, 760, 420,
               {"Category": ["branch_performance:uninumbr"],
                "X": ["branch_performance:households"],
                "Y": ["branch_performance:actual_deposits"]},
               "Catchment households against actual deposits"),
        visual("card", 792, 68, 236, 110,
               {"Values": ["measure:Performance Index"]},
               "Performance index (size-adjusted)"),
        visual("card", 1036, 68, 228, 110,
               {"Values": ["measure:Underperforming Branches"]},
               "Branches below 1.0"),
        visual("tableEx", 792, 192, 472, 296, {"Values": [
            "branch_performance:city", "branch_performance:index_size_adjusted",
            "branch_performance:diagnosis"]},
            "Ranked by size-adjusted index",
            sort=("branch_performance:index_size_adjusted", "asc")),
        textbox(16, 500, 1248, 76, [
            run("Rank on index_size_adjusted, never on the raw index. ", "10pt", True, WARN),
            run("The raw index fell mechanically as catchment size rose — median "
                "1.518 in the smallest catchment quartile against 0.416 in the "
                "largest — which inverted the ranking of every market. Chicago "
                "read as weakest at 0.564 and is strongest at 1.357 once adjusted.",
                "10pt", False, INK)]),
    ]
    pages.append(("Branch performance", p2))

    # ---- Page 3: Branch detail ------------------------------------------
    p3 = [
        textbox(16, 12, 900, 44, [run("Branch detail", "20pt", True, INK)]),
        visual("slicer", 16, 68, 260, 420, {"Values": ["dim_branch:city"]}, "Branch city"),
        visual("columnChart", 288, 68, 500, 220,
               {"Category": ["dim_year:year"], "Y": ["measure:Total Deposits"]},
               "Deposits by year"),
        visual("card", 800, 68, 224, 110, {"Values": ["measure:Capture Rate %"]},
               "Mortgage capture rate %"),
        visual("card", 1040, 68, 224, 110, {"Values": ["measure:Unmet Originations"]},
               "Unmet originations"),
        visual("tableEx", 800, 192, 464, 296, {"Values": [
            "fact_tract_competition:tract_geoid",
            "fact_tract_competition:competitor_branches",
            "fact_tract_competition:competitor_per_10k_catchment_hh"]},
            "Competitor branches near these tracts"),
        visual("tableEx", 288, 300, 500, 188, {"Values": [
            "dim_tract:tract_geoid", "dim_tract:households",
            "dim_tract:lmi_flag"]}, "Catchment tracts"),
        textbox(16, 500, 1248, 76, [
            run("fact_tract_competition is not a catchment bridge. ", "10pt", True, WARN),
            run("It counts competitor branches near a tract and carries no branch "
                "identifier, because competitor branches have no catchments here — "
                "their tract assignments were never computed and their coordinates "
                "never validated. Do not relate it to dim_branch.", "10pt", False, INK)]),
    ]
    pages.append(("Branch detail", p3))

    # ---- Page 4: Recommendation and equity ------------------------------
    p4 = [
        textbox(16, 12, 1248, 52, [
            run("Recommendation and equity check", "20pt", True, INK)]),
        textbox(16, 64, 1248, 52, [
            run("Rule B ships. ", "12pt", True, INK),
            run("The commercial set fails the binding test: non-LMI coverage "
                "grows 8.4× faster than LMI. AC-06 as written would have passed "
                "it, because AC-06 tests that two numbers exist.", "12pt", False, INK)]),
        visual("tableEx", 16, 124, 760, 240, {"Values": [
            "recommended_sites:rule", "recommended_sites:tract_geoid",
            "recommended_sites:county_name", "recommended_sites:opportunity_score",
            "recommended_sites:lmi_flag", "recommended_sites:growth_is_estimated"]},
            "The three sites — constrained (B) and commercial (A)"),
        visual("card", 792, 124, 232, 106, {"Values": ["measure:LMI Coverage %"]},
               "LMI coverage now %"),
        visual("card", 1032, 124, 232, 106,
               {"Values": ["measure:LMI Coverage % (recommended)"]},
               "LMI coverage recommended %"),
        visual("card", 792, 238, 232, 126,
               {"Values": ["measure:Binding Test Result"]}, "Binding test"),
        visual("card", 1032, 238, 232, 126,
               {"Values": ["measure:Constraint Cost %"]},
               "Cost of the constraint %"),
        visual("tableEx", 16, 376, 760, 150, {"Values": [
            "lmi_coverage:rule", "lmi_coverage:lmi_coverage_pct",
            "lmi_coverage:non_lmi_coverage_pct", "lmi_coverage:delta_lmi_pp",
            "lmi_coverage:delta_non_lmi_pp", "lmi_coverage:binding_test"]},
            "AC-06 and the test that can fail"),
        visual("card", 792, 376, 232, 150,
               {"Values": ["measure:LMI Sites in Shortlist"]},
               "LMI tracts among the 3 sites"),
        textbox(1032, 376, 232, 150, [
            run("0 of 3.", "14pt", True, WARN),
            run(" The constraint improves catchment composition; it does not "
                "site among LMI communities. It selects from a shortlist that "
                "is 4.0% LMI, so the ceiling is upstream in the opportunity "
                "model, not in the constraint.", "9pt", False, INK)]),
        textbox(16, 538, 1248, 74, [
            run("One named risk, not three caveats. ", "10pt", True, WARN),
            run("29 of the top 50 sit in one Chicago-area growth corridor, and "
                "growth there is disproportionately cluster-estimated rather "
                "than observed (22 of 29 against 9 of 21 elsewhere, phi +0.336). "
                "One growth assumption, mostly estimated, in one corridor.",
                "10pt", False, INK)],),
        textbox(16, 620, 620, 84, [
            run("Weights are a judgement, not a fact.", "10pt", True, INK),
            run(" Drag to re-weight. Under a growth-led weighting 39.7% of "
                "tracts move 500+ places and only 31 of the top 50 survive.",
                "10pt", False, INK)]),
    ]
    x = 648
    for pname, _ in WHATIF:
        p4.append(visual("slicer", x, 620, 122, 84, {"Values": [f"{pname}:{pname}"]},
                         pname.replace("w ", "")))
        x += 124
    pages.append(("Recommendation and equity", p4))

    sections = []
    for i, (name, visuals) in enumerate(pages):
        sections.append({
            "config": "{}",
            "displayName": name,
            "displayOption": 1,
            "filters": "[]",
            "height": 720.0,
            "name": f"page{i+1}",
            "ordinal": i,
            "visualContainers": visuals,
            "width": 1280.0,
        })

    report = {
        "config": json.dumps({
            "version": "5.55",
            "themeCollection": {"baseTheme": {
                "name": "CY24SU10", "version": "5.55", "type": 2}},
            "activeSectionIndex": 0,
            "defaultDrillFilterOtherVisuals": True,
            "settings": {"useStylableVisualContainerHeader": True},
        }),
        "layoutOptimization": 0,
        "resourcePackages": [{"resourcePackage": {
            "disabled": False,
            "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json",
                       "type": 202}],
            "name": "SharedResources", "type": 2}}],
        "sections": sections,
    }

    rp = BASE / f"{PROJECT}.Report"
    if rp.exists():
        shutil.rmtree(rp)
    rp.mkdir(parents=True)
    (rp / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (rp / "definition.pbir").write_text(json.dumps({
        "version": "4.0",
        "datasetReference": {"byPath": {
            "path": f"../{PROJECT}.SemanticModel"}},
    }, indent=2), encoding="utf-8")
    (rp / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": PROJECT},
        "config": {"version": "2.0", "logicalId": tag("logical", "report")},
    }, indent=2), encoding="utf-8")

    (BASE / f"{PROJECT}.pbip").write_text(json.dumps({
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{PROJECT}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2), encoding="utf-8")
    return pages


def validate(tables, pages):
    """Every field a visual names must exist. Checked here, not in Desktop.

    A visual referencing a column that is not there does not error - Power BI
    renders it empty, and an empty card on a dashboard reads as a zero. This
    is the same reason the warehouse asserts on joins rather than on columns
    alone: the damage shows up as a plausible absence, not as a crash.
    """
    problems = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        for t in (ft, tt):
            if t in DISCONNECTED:
                problems.append(f"{t} is marked disconnected but has a relationship")
    for name, visuals in pages:
        for v in visuals:
            cfg = json.loads(v["config"])
            sv = cfg["singleVisual"]
            for sel in sv.get("prototypeQuery", {}).get("Select", []):
                if "Measure" in sel:
                    m = sel["Measure"]["Property"]
                    if m not in MEASURE_HOME:
                        problems.append(f"[{name}] unknown measure {m!r}")
                else:
                    entity = sel["Name"].rsplit(".", 1)[0]
                    col = sel["Column"]["Property"]
                    if entity in tables:
                        if col not in tables[entity].columns:
                            problems.append(
                                f"[{name}] {entity} has no column {col!r}")
                    elif entity not in {p for p, _ in WHATIF}:
                        problems.append(f"[{name}] unknown table {entity!r}")
    return problems


if __name__ == "__main__":
    tables = build_model()
    print(f"semantic model: {len(tables)} tables + {len(WHATIF)} parameters")
    print(f"  relationships: {len(RELATIONSHIPS)}")
    n_meas = sum(len(v) for v in (DEPOSIT_MEASURES, PERF_MEASURES,
                                  COVERAGE_MEASURES, TRACT_MEASURES,
                                  CAPTURE_MEASURES, INDEX_MEASURES,
                                  WHATIF_MEASURES))
    print(f"  measures: {n_meas} + {len(WHATIF)} parameter values")
    pages = build_report()
    for name, vis in pages:
        print(f"  page: {name:32s} {len(vis)} visuals")

    problems = validate(tables, pages)
    if problems:
        print("\nVALIDATION FAILED:")
        for p in problems:
            print(f"  {p}")
        raise SystemExit(1)
    print("\nvalidation: every field reference resolves to a real "
          "column or measure")
    print(f"Open {BASE.relative_to(ROOT)}/{PROJECT}.pbip in Power BI Desktop.")
