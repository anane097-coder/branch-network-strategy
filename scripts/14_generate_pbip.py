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
import re
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
DISCONNECTED = {"lmi_coverage", "recommendation_sets", "ref_index_weights",
                "fact_county_deposit_growth", "market_share"}

RELATIONSHIPS = [
    # (from_table, from_col, to_table, to_col)
    ("fact_branch_deposits", "uninumbr", "dim_branch", "uninumbr"),
    ("fact_branch_deposits", "cert", "dim_institution", "cert"),
    ("fact_branch_deposits", "year", "dim_year", "year"),
    ("bridge_branch_catchment", "uninumbr", "dim_branch", "uninumbr"),
    ("bridge_branch_catchment", "tract_geoid", "dim_tract", "tract_geoid"),
    ("fact_tract_competition", "tract_geoid", "dim_tract", "tract_geoid"),
    ("tract_capture_rate", "tract_geoid", "dim_tract", "tract_geoid"),
    ("unmet_demand", "tract_geoid", "dim_tract", "tract_geoid"),
    ("opportunity_index", "tract_geoid", "dim_tract", "tract_geoid"),
    ("index_components", "tract_geoid", "dim_tract", "tract_geoid"),
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
    ("index_components", "lmi_flag"),
    ("index_components", "growth_is_estimated"),
    ("dim_tract", "in_catchment"),
    ("recommendation_sets", "lmi_flag"),
    ("recommendation_sets", "growth_is_estimated"),
    ("dim_branch", "is_subject_bank"),
    ("dim_institution", "is_subject_bank"),
    ("unmet_demand", "lmi_flag"),
    ("tract_capture_rate", "lmi_flag"),
}
BOOL_LITERALS = {True, False, "True", "False", "true", "false"}

# THE DATE TABLE. DATEADD, and every other time-intelligence function, needs a
# real date column on a table MARKED as a date table. Against anything else it
# does not error - it returns blank, and a year-over-year measure then reads as
# "no change", which is the most convincing wrong answer available.
#
# Marking it takes two things and both are required: dataCategory Time on the
# table, and isKey on the date column. One without the other silently does
# nothing.
DATE_TABLE, DATE_COLUMN = "dim_year", "date"

# GEOGRAPHIC DATA CATEGORIES. Without these Power BI guesses from the column
# name, and it guesses badly: a filled map keyed on an uncategorised
# "county_name" geocoded the string against every state in the union and
# shaded counties from Washington to Florida. Latitude and longitude need no
# geocoding at all, which is why the point layers use them.
DATA_CATEGORY = {
    ("dim_tract", "county_full"): "Place",
    ("dim_tract", "state_name"): "StateOrProvince",
    ("dim_tract", "centroid_lat"): "Latitude",
    ("dim_tract", "centroid_lon"): "Longitude",
    ("index_components", "centroid_lat"): "Latitude",
    ("index_components", "centroid_lon"): "Longitude",
    ("recommendation_sets", "centroid_lat"): "Latitude",
    ("recommendation_sets", "centroid_lon"): "Longitude",
    ("dim_branch", "latitude"): "Latitude",
    ("dim_branch", "longitude"): "Longitude",
    ("dim_branch", "state"): "StateOrProvince",
    ("dim_branch", "city"): "City",
}


def pbi_types(df: pd.DataFrame, table: str):
    """(tmdl dataType, M type) per column, identifiers forced to text."""
    out = {}
    for col in df.columns:
        if table == DATE_TABLE and col == DATE_COLUMN:
            out[col] = ("dateTime", "type date")
            continue
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
    lines = [f"table {table}", f"\tlineageTag: {tag('table', table)}"]
    if table == DATE_TABLE:
        lines.append("\tdataCategory: Time")
    lines.append("")
    for col, (dt, _) in types.items():
        is_date_key = table == DATE_TABLE and col == DATE_COLUMN
        lines += [f"\tcolumn {col}", f"\t\tdataType: {dt}"]
        if is_date_key:
            lines += ["\t\tisKey", "\t\tformatString: General Date"]
        cat = DATA_CATEGORY.get((table, col))
        if cat:
            lines.append(f"\t\tdataCategory: {cat}")
        lines += [
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


# DAX reserves more identifiers than the documented function list, and a
# collision fails at parse time in Desktop rather than here - which is the
# worst place to find it, because the model half-loads and the error names the
# measure rather than the word. `Weight` was the one that bit; the rest are
# words plausible enough to reach for. NOT EXHAUSTIVE, which is why VAR names
# in this file are all prefixed rather than merely checked.
DAX_RESERVED = {
    "weight", "measure", "column", "table", "var", "return", "define",
    "evaluate", "order", "by", "asc", "desc", "start", "at", "in", "not",
    "and", "or", "true", "false", "blank", "value", "values", "all", "filter",
    "row", "rank", "level", "scale", "format", "path", "union", "distinct",
    "sample", "generate", "summarize", "calculate", "calendar", "currency",
    "date", "time", "year", "month", "day", "hour", "minute", "second",
    "now", "today", "min", "max", "sum", "count", "average", "divide",
    "if", "switch", "rate", "type", "name", "index", "sort", "group",
}


def check_var_names(name: str, expr: str):
    """Reject a measure whose VAR names could collide before it is written."""
    bad = [v for v in re.findall(r"\bVAR\s+([A-Za-z_][A-Za-z0-9_]*)", expr)
           if v.lower() in DAX_RESERVED]
    if bad:
        raise SystemExit(
            f"Measure {name!r} uses reserved VAR name(s) {bad}. "
            "DAX will refuse to parse it in Power BI Desktop.")


def measure(name: str, expr: str, table: str, fmt: str | None = None) -> str:
    check_var_names(name, expr)
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
    # Spec 11. DATEADD requires a REAL date column on a table marked as a
    # date table - against an integer year it does not error, it returns
    # blank, and a year-over-year measure then reads as "no change".
    ("Deposits PY",
     "CALCULATE ( [Total Deposits], DATEADD ( dim_year[date], -1, YEAR ) )",
     "#,0"),
    ("Deposit CAGR 3yr", """VAR CurrentDeposits = [Total Deposits]
VAR BaseDeposits = CALCULATE ( [Total Deposits], DATEADD ( dim_year[date], -3, YEAR ) )
RETURN
    IF ( BaseDeposits > 0, ( CurrentDeposits / BaseDeposits ) ^ ( 1 / 3 ) - 1 )""",
     "0.0%"),
    ("Deposit CAGR %", """VAR CagrFirstYear = MIN ( dim_year[year] )
VAR CagrLastYear = MAX ( dim_year[year] )
VAR CagrYearSpan = CagrLastYear - CagrFirstYear
VAR CagrStartDeposits = CALCULATE ( [Total Deposits], dim_year[year] = CagrFirstYear )
VAR CagrEndDeposits = CALCULATE ( [Total Deposits], dim_year[year] = CagrLastYear )
RETURN
    IF (
        CagrYearSpan > 0
            && NOT ISBLANK ( CagrStartDeposits ) && CagrStartDeposits > 0
            && NOT ISBLANK ( CagrEndDeposits ),
        ( ( CagrEndDeposits / CagrStartDeposits ) ^ ( 1 / CagrYearSpan ) - 1 ) * 100
    )""", "#,0.00"),
]

PERF_MEASURES = [
    # Spec 11 names. Both ship; size-adjusted is the default on every visual
    # and raw is reachable through the page-2 field parameter, so the
    # market-position effect stays visible rather than normalised out of sight.
    ("Index (size-adjusted)",
     "AVERAGE ( dim_branch[index_size_adjusted] )", "#,0.000"),
    ("Index (raw)", "AVERAGE ( dim_branch[index_raw] )", "#,0.000"),
    # No service-type filter: SQL-08 already restricts to types 11 and 12
    # before the index exists. Limited-service facilities structurally book no
    # deposits, so a zero is a fact about the facility type rather than a
    # measurement of it - left in, they score 0.0000 and sort to the top of
    # any review list.
    ("Underperforming Branches",
     "CALCULATE ( COUNTROWS ( dim_branch ), dim_branch[index_size_adjusted] < 1 )",
     "#,0"),
]

COVERAGE_MEASURES = [
    ("LMI Coverage %",
     'CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )',
     "#,0.00"),
    ("LMI Coverage % (recommended)",
     'CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "B_constrained" )',
     "#,0.00"),
    ("Coverage Delta Gap (pp)", """VAR SelectedRule = SELECTEDVALUE ( lmi_coverage[rule], "B_constrained" )
VAR CurrentLmiCoverage = CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )
VAR CurrentNonLmiCoverage = CALCULATE ( MAX ( lmi_coverage[non_lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = "current" )
VAR RecommendedLmiCoverage = CALCULATE ( MAX ( lmi_coverage[lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = SelectedRule )
VAR RecommendedNonLmiCoverage = CALCULATE ( MAX ( lmi_coverage[non_lmi_coverage_pct] ), ALL ( lmi_coverage ), lmi_coverage[rule] = SelectedRule )
RETURN
    ( RecommendedLmiCoverage - CurrentLmiCoverage )
        - ( RecommendedNonLmiCoverage - CurrentNonLmiCoverage )""", "#,0.000"),
    ("Binding Test Result", """IF (
    ISBLANK ( [Coverage Delta Gap (pp)] ),
    "NOT MEASURED",
    IF ( [Coverage Delta Gap (pp)] >= 0, "PASS - expansion is proportional", "FAIL - non-LMI coverage grows faster" )
)""", None),
    ("Constraint Cost (index points)", """VAR CommercialScore = CALCULATE ( SUM ( recommendation_sets[opportunity_score] ), ALL ( recommendation_sets ), recommendation_sets[rule] = "A_commercial" )
VAR ConstrainedScore = CALCULATE ( SUM ( recommendation_sets[opportunity_score] ), ALL ( recommendation_sets ), recommendation_sets[rule] = "B_constrained" )
RETURN CommercialScore - ConstrainedScore""", "#,0.000"),
    ("Constraint Cost %", """VAR CommercialScore = CALCULATE ( SUM ( recommendation_sets[opportunity_score] ), ALL ( recommendation_sets ), recommendation_sets[rule] = "A_commercial" )
RETURN DIVIDE ( [Constraint Cost (index points)], CommercialScore ) * 100""", "#,0.0"),
    ("LMI Sites in Shortlist", """CALCULATE (
    COUNTROWS ( recommendation_sets ),
    recommendation_sets[lmi_flag] = TRUE,
    recommendation_sets[rule] = "B_constrained"
) + 0""", "#,0"),
]

TRACT_MEASURES = [
    # SUBJECT-PREFIXED, per spec 11's own naming rule: in_catchment derives
    # from bridge_branch_catchment, which covers the subject only, so every
    # measure reading it inherits that scope. The spec names this measure
    # "LMI Coverage"; the prefix is applied because the rule it states two
    # paragraphs earlier is the more important of the two.
    ("Subject LMI Coverage", """DIVIDE (
    CALCULATE ( SUM ( dim_tract[households] ),
                dim_tract[lmi_flag] = TRUE, dim_tract[in_catchment] = TRUE ),
    CALCULATE ( SUM ( dim_tract[households] ), dim_tract[lmi_flag] = TRUE )
)""", "0.0%"),
    # Households, never tracts - the tract basis inflated the over-index from
    # +1.9pp to +3.3pp. The 31 no-basis tracts leave BOTH sides via the
    # lmi_flag NULL, which is why neither filter above coalesces it to FALSE.
    ("Subject Non-LMI Coverage", """DIVIDE (
    CALCULATE ( SUM ( dim_tract[households] ),
                dim_tract[lmi_flag] = FALSE, dim_tract[in_catchment] = TRUE ),
    CALCULATE ( SUM ( dim_tract[households] ), dim_tract[lmi_flag] = FALSE )
)""", "0.0%"),
    ("Subject Tract Coverage %", """DIVIDE (
    CALCULATE ( COUNTROWS ( dim_tract ), dim_tract[in_catchment] = TRUE ),
    COUNTROWS ( ALL ( dim_tract ) )
) * 100""", "#,0.0"),
    ("LMI Share of Selection %", """VAR TractsDetermined = CALCULATE ( COUNTROWS ( dim_tract ), NOT ISBLANK ( dim_tract[lmi_flag] ) )
VAR TractsLmi = CALCULATE ( COUNTROWS ( dim_tract ), dim_tract[lmi_flag] = TRUE )
RETURN DIVIDE ( TractsLmi, TractsDetermined ) * 100""", "#,0.0"),
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
    # Spec 11's Weighted Score: a plain SUMX over five named columns, which
    # is why index_components ships wide. The long form needed a SUMX over
    # VALUES() with a SWITCH inside - correct, but harder to read, and this is
    # the measure a reviewer is most likely to actually read.
    #
    # VAR names are long on purpose: `Weight` is RESERVED in DAX and will not
    # parse, and so are enough other obvious words that guessing is not worth
    # it. check_var_names() rejects a collision before anything is written.
    ("Weighted Score", """VAR wHG = [w Household Growth Value]
VAR wMI = [w Median Income Value]
VAR wDG = [w Deposit Growth Value]
VAR wCS = [w Competitor Saturation Value]
VAR wUD = [w Unmet Demand Value]
VAR wTotal = wHG + wMI + wDG + wCS + wUD
RETURN
    IF (
        wTotal = 0,
        BLANK (),
        DIVIDE (
            SUMX (
                index_components,
                index_components[household_growth_z] * wHG
                    + index_components[median_income_z] * wMI
                    + index_components[deposit_growth_z] * wDG
                    + index_components[competitor_saturation_z] * wCS
                    + index_components[unmet_demand_z] * wUD
            ),
            wTotal
        )
    )""", "#,0.000"),
    # The card that sits beside the sliders. Moving unmet demand down visibly
    # raises it - the sensitivity finding made interactive, and the single
    # most useful thing on the page.
    ("Top 50 LMI Share %", """VAR Top50 =
    TOPN ( 50, ALL ( index_components ), [Weighted Score], DESC )
VAR Determined = COUNTROWS ( FILTER ( Top50, NOT ISBLANK ( index_components[lmi_flag] ) ) )
VAR IsLmi = COUNTROWS ( FILTER ( Top50, index_components[lmi_flag] = TRUE ) )
RETURN DIVIDE ( IsLmi, Determined ) * 100""", "#,0.0"),
    ("Top 50 Cluster-Measured Growth %", """VAR Top50 =
    TOPN ( 50, ALL ( index_components ), [Weighted Score], DESC )
RETURN
    DIVIDE (
        COUNTROWS ( FILTER ( Top50, index_components[growth_is_estimated] = TRUE ) ),
        COUNTROWS ( Top50 )
    ) * 100""", "#,0.0"),
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
        "dim_branch": PERF_MEASURES,
        "lmi_coverage": COVERAGE_MEASURES,
        "dim_tract": TRACT_MEASURES,
        "tract_capture_rate": CAPTURE_MEASURES,
        "opportunity_index": INDEX_MEASURES,
        "index_components": WHATIF_MEASURES,
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
                  ("dim_branch", PERF_MEASURES),
                  ("lmi_coverage", COVERAGE_MEASURES),
                  ("dim_tract", TRACT_MEASURES),
                  ("tract_capture_rate", CAPTURE_MEASURES),
                  ("opportunity_index", INDEX_MEASURES),
                  ("index_components", WHATIF_MEASURES)):
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


# Rationale carried into the emitted measures.dax. Kept here rather than in
# that file because a second hand-maintained copy has now drifted twice - it
# referenced branch_performance[index_vs_market] and [service_type], neither
# of which existed, and then survived a table rename it knew nothing about.
# One source, generated.
MEASURE_NOTES = {
    "Total Deposits ($bn)":
        "Deposits are stored in WHOLE DOLLARS. SOD publishes thousands and the\n"
        "conversion happens once, in script 10. A second conversion here would\n"
        "be invisible and would surface as a 1000x error only where somebody\n"
        "compared two visuals.",
    "Market Deposits":
        "REMOVEFILTERS on the institution ONLY, leaving geography and time\n"
        "intact. ALL() over the model would compare a county's subject deposits\n"
        "against the whole footprint; no removal at all returns 100%. Both look\n"
        "entirely plausible on a card, which is what makes this the classic\n"
        "filter-context error.",
    "Deposits PY":
        "DATEADD needs a REAL date column on a table marked as a date table.\n"
        "Against an integer year it does not error - it returns blank, and the\n"
        "measure reads as no change.",
    "Deposit CAGR 3yr":
        "BLANK, never zero, when the base is missing. Zero would place an\n"
        "unmeasured branch in the middle of every ranking as though it had been\n"
        "measured. Refuse before computing.",
    "Deposit CAGR %":
        "Endpoints come from the SELECTION, not hardcoded to 2019 and 2025, so\n"
        "the measure stays correct when a slicer narrows the range. A CAGR\n"
        "against a fixed endpoint under a filtered context keeps returning a\n"
        "plausible number, which is why it survives review.",
    "Index (size-adjusted)":
        "THE DEFAULT ON EVERY VISUAL. The raw index fell mechanically as\n"
        "catchment size rose - median 1.518 in the smallest catchment quartile\n"
        "against 0.416 in the largest - and inverted the ranking of every\n"
        "market. Chicago read weakest at 0.564 and is strongest at 1.357 once\n"
        "adjusted.",
    "Index (raw)":
        "Audit only, reachable through the page-2 field parameter so the\n"
        "market-position effect stays visible rather than normalised out of\n"
        "sight. Never rank on it.",
    "Subject LMI Coverage":
        "SUBJECT-PREFIXED because in_catchment derives from\n"
        "bridge_branch_catchment, which covers the subject only - every measure\n"
        "reading it inherits that scope, and the field list should say so.\n"
        "Households, never tracts: the tract basis inflated the over-index from\n"
        "+1.9pp to +3.3pp. The 31 no-basis tracts leave BOTH numerator and\n"
        "denominator through the lmi_flag NULL, which is why neither filter\n"
        "coalesces it to FALSE.",
    "Coverage Delta Gap (pp)":
        "THE BINDING TEST. Both deltas are non-negative by construction - a\n"
        "branch adds catchment area and removes none - so this compares their\n"
        "SIZES, not their signs. AC-06 as written (both values present) cannot\n"
        "fail; this can, and did, for the unconstrained commercial set, where\n"
        "non-LMI coverage grew 8.4x faster than LMI.",
    "Capture Rate %":
        "Originations only, on BOTH sides of the ratio. Purchased loans\n"
        "(action_taken 6) were bought on the secondary market and say nothing\n"
        "about serving a tract; several lenders here exceed 88% purchased\n"
        "against the subject's 0.8%, so including them would inflate\n"
        "competitors far more than the subject.",
    "Weighted Score":
        "FR-03 demonstrated rather than claimed. Sliders cannot be constrained\n"
        "to sum to 1, so the measure normalises across whatever they do sum to\n"
        "rather than asking the user to do arithmetic. index_components ships\n"
        "wide precisely so this is a plain SUMX over five named columns.\n"
        "VAR names are long because `Weight` is RESERVED in DAX and will not\n"
        "parse - check_var_names() rejects a collision before writing.",
    "Top 50 LMI Share %":
        "The card beside the sliders. Moving unmet demand down visibly raises\n"
        "it, which is the sensitivity finding made interactive and the single\n"
        "most useful thing on the page.",
    "LMI Share of Selection %":
        "Tracts with no LMI determination leave BOTH numerator and denominator.\n"
        "Counting them as non-LMI would inflate the denominator and quietly\n"
        "improve every equity figure on the page.",
    "Underperforming Branches":
        "No service-type filter is needed: SQL-08 already restricts to types 11\n"
        "and 12. Limited-service facilities structurally book no deposits, so a\n"
        "zero is a fact about the facility type rather than a measurement of\n"
        "it - left in, they score 0.0000 and sort to the top of any review list.",
}

MEASURE_SECTIONS = [
    ("Deposits, share and trend", "fact_branch_deposits", "DEPOSIT_MEASURES"),
    ("Branch performance", "dim_branch", "PERF_MEASURES"),
    ("Equity and the binding test", "lmi_coverage", "COVERAGE_MEASURES"),
    ("Tract composition", "dim_tract", "TRACT_MEASURES"),
    ("Capture rate", "tract_capture_rate", "CAPTURE_MEASURES"),
    ("Opportunity index", "opportunity_index", "INDEX_MEASURES"),
    ("What-if weighting", "index_components", "WHATIF_MEASURES"),
]


def write_measures_dax():
    """Emit powerbi/measures.dax from the same lists the model is built from.

    Not a second copy - a rendering. The previous hand-maintained file drifted
    twice, and both drifts were only caught because the validator reads it.
    """
    lists = {"DEPOSIT_MEASURES": DEPOSIT_MEASURES, "PERF_MEASURES": PERF_MEASURES,
             "COVERAGE_MEASURES": COVERAGE_MEASURES, "TRACT_MEASURES": TRACT_MEASURES,
             "CAPTURE_MEASURES": CAPTURE_MEASURES, "INDEX_MEASURES": INDEX_MEASURES,
             "WHATIF_MEASURES": WHATIF_MEASURES}
    out = [
        "// " + "=" * 73,
        "// Measures - branch-network-strategy",
        "// " + "=" * 73,
        "// GENERATED by scripts/14_generate_pbip.py. Do not edit: the model and",
        "// this file are rendered from one list, so they cannot disagree.",
        "//",
        "// Written to be read. Every measure that could be computed more than one",
        "// way says which way it computes and why, because the definitional",
        "// choices in this project are the analysis, not an implementation detail.",
        "// " + "=" * 73,
        "",
    ]
    for title, home, key in MEASURE_SECTIONS:
        out += ["", "// " + "-" * 73,
                f"// {title}   [table: {home}]",
                "// " + "-" * 73, ""]
        for name, expr, *rest in lists[key]:
            note = MEASURE_NOTES.get(name)
            if note:
                out += ["// " + line for line in note.split("\n")]
            out.append(f"{name} =")
            out.append(expr.strip())
            out.append("")
    for pname, default in WHATIF:
        out += [f"// What-if parameter: {pname}, 0 to 0.50 step 0.05,"
                f" default {default}",
                f"{pname} = GENERATESERIES ( 0, 0.5, 0.05 )",
                f"{pname} Value = SELECTEDVALUE ( '{pname}'[{pname}], {default} )",
                ""]
    (BASE / "measures.dax").write_text("\n".join(out), encoding="utf-8")


def build_report():
    """Pages per spec 11, built in the spec build order - page 4 first,
    because it carries the finding and must not be the unfinished one.
    File order is 1,2,3,4 with page 3 hidden from the navigator."""

    # ---- Page 4: recommendation and equity, three bands ------------------
    p4 = [
        textbox(16, 10, 1248, 40, [
            run("Recommendation and equity check", "20pt", True, INK)]),
        # BAND 1 - both site sets, over the existing footprint.
        visual("map", 16, 54, 620, 250,
               {"Latitude": ["recommendation_sets:centroid_lat"],
                "Longitude": ["recommendation_sets:centroid_lon"],
                "Series": ["recommendation_sets:rule"],
                "Size": ["recommendation_sets:opportunity_score"]},
               "Rule A (commercial) and Rule B (constrained), both shown"),
        visual("tableEx", 648, 54, 616, 250, {"Values": [
            "recommendation_sets:rule", "recommendation_sets:county_name",
            "recommendation_sets:tract_geoid", "recommendation_sets:tier",
            "recommendation_sets:opportunity_score",
            "recommendation_sets:lmi_flag",
            "recommendation_sets:growth_is_estimated"]},
            "The three sites, both rules"),
        # BAND 2 - the comparison table. The centrepiece of the page.
        visual("tableEx", 16, 314, 900, 178, {"Values": [
            "lmi_coverage:rule", "lmi_coverage:lmi_coverage_pct",
            "lmi_coverage:non_lmi_coverage_pct", "lmi_coverage:delta_lmi_pp",
            "lmi_coverage:delta_non_lmi_pp",
            "lmi_coverage:non_lmi_growth_multiple",
            "lmi_coverage:binding_test"]},
            "Current, Rule A, Rule B - the test that can fail"),
        visual("card", 928, 314, 168, 88,
               {"Values": ["measure:Subject LMI Coverage"]}, "LMI coverage now"),
        visual("card", 1104, 314, 160, 88,
               {"Values": ["measure:Subject Non-LMI Coverage"]}, "non-LMI now"),
        visual("card", 928, 410, 168, 82,
               {"Values": ["measure:Constraint Cost %"]}, "Constraint cost %"),
        visual("card", 1104, 410, 160, 82,
               {"Values": ["measure:LMI Sites in Shortlist"]}, "LMI sites of 3"),
        # BAND 3 - three text blocks, on canvas, not in tooltips.
        textbox(16, 500, 410, 104, [
            run("Cost of the constraint. ", "10pt", True, INK),
            run("0.6712 index points, 9.4% of the unconstrained score. "
                "26,617 new catchment households against 37,413.",
                "10pt", False, INK)]),
        textbox(436, 500, 410, 104, [
            run("0 of 3. ", "11pt", True, WARN),
            run("Neither set places a branch in an LMI tract. Rule B improves "
                "catchment composition; it does not site among LMI "
                "households. Anyone reading this as a CRA response needs that "
                "distinction - the ceiling is upstream, in a shortlist that "
                "is 4.0% LMI.", "10pt", False, INK)]),
        textbox(856, 500, 408, 104, [
            run("Correlated exposure. ", "10pt", True, WARN),
            run("Both sets concentrate in the Chicago-Naperville-Elgin "
                "corridor, where 22 of 29 top-50 tracts carry cluster-"
                "estimated growth. One regional growth assumption, mostly "
                "estimated.", "10pt", False, INK)]),
        # What-if weighting. FR-03 demonstrated rather than claimed.
        textbox(16, 612, 300, 92, [
            run("Weights are a judgement, not a fact.", "10pt", True, INK),
            run(" Under a growth-led weighting 39.7% of tracts move 500+ "
                "places and only 31 of the top 50 survive.", "9pt", False, INK)]),
        visual("card", 1004, 612, 128, 92,
               {"Values": ["measure:Top 50 LMI Share %"]}, "Top-50 LMI %"),
        visual("card", 1140, 612, 124, 92,
               {"Values": ["measure:Top 50 Cluster-Measured Growth %"]},
               "Top-50 est. growth %"),
    ]
    x = 324
    for pname, _ in WHATIF:
        p4.append(visual("slicer", x, 612, 132, 92,
                         {"Values": [f"{pname}:{pname}"]},
                         pname.replace("w ", "")))
        x += 136

    # ---- Page 1: market opportunity --------------------------------------
    p1 = [
        textbox(16, 10, 900, 40, [run("Market opportunity", "20pt", True, INK)]),
        # Five cards. Five is the ceiling the spec sets, not a target.
        visual("card", 16, 52, 240, 92,
               {"Values": ["measure:Total Deposits ($bn)"]},
               "Footprint deposits ($bn)"),
        visual("card", 264, 52, 200, 92, {"Values": ["measure:Branch Count"]},
               "Branches"),
        visual("card", 472, 52, 200, 92,
               {"Values": ["measure:Deposit CAGR 3yr"]}, "3yr deposit CAGR"),
        visual("card", 680, 52, 200, 92, {"Values": ["measure:Market Share %"]},
               "Market share %"),
        visual("card", 888, 52, 208, 92,
               {"Values": ["measure:Subject Tract Coverage %"]},
               "Tract coverage %"),
        # County choropleth rather than 4,807 tract polygons: 174 counties
        # render fast and read clearly, which is the entire reason for it.
        # county_full is "Adams County, Illinois", categorised as Place. The
        # bare county name geocodes against every state that has one.
        visual("filledMap", 16, 152, 300, 320,
               {"Category": ["dim_tract:county_full"],
                "Y": ["measure:Published Opportunity Score"]},
               "Counties by mean opportunity score"),
        # Coordinates, not names: exact, and needs no geocoding. Beside the
        # choropleth rather than stacked on it - two map visuals at the same
        # position simply hide one another.
        visual("map", 324, 152, 292, 320,
               {"Latitude": ["index_components:centroid_lat"],
                "Longitude": ["index_components:centroid_lon"],
                "Size": ["measure:Weighted Score"]},
               "Top tracts by weighted score"),
        # The composition flags ARE the point of this table: a reader should
        # see 4.0% LMI and 62% estimated growth without being told.
        visual("tableEx", 628, 152, 636, 320, {"Values": [
            "index_components:tract_geoid", "index_components:county_name",
            "index_components:opportunity_score", "index_components:lmi_flag",
            "index_components:growth_is_estimated"]},
            "Top 50 tracts - LMI and growth-basis flags",
            sort=("index_components:opportunity_score", "desc")),
        visual("columnChart", 16, 482, 600, 150,
               {"Category": ["ref_index_weights:component"],
                "Y": ["ref_index_weights:weight"]},
               "Component weights - unmet demand carries 39.0% of top-50 contribution"),
        visual("slicer", 628, 482, 200, 150, {"Values": ["dim_branch:state"]},
               "State"),
        visual("slicer", 840, 482, 200, 150, {"Values": ["dim_tract:tier"]},
               "Tier"),
        visual("slicer", 1052, 482, 212, 150,
               {"Values": ["dim_tract:cbsa_title"]}, "CBSA"),
        textbox(16, 640, 1248, 64, [
            run("Shortlist is 4.0% LMI against a 29.5% baseline; 62% of "
                "top-50 tracts carry estimated rather than observed growth.",
                "11pt", True, WARN)]),
    ]

    # ---- Page 2: branch performance --------------------------------------
    p2 = [
        textbox(16, 10, 900, 40, [run("Branch performance", "20pt", True, INK)]),
        visual("scatterChart", 16, 52, 740, 420,
               {"Category": ["dim_branch:uninumbr"],
                "X": ["dim_branch:catchment_households"],
                "Y": ["dim_branch:actual_deposits"],
                "Size": ["dim_branch:catchment_households"],
                "Series": ["dim_branch:diagnosis"]},
               "Catchment potential against actual deposits"),
        visual("card", 768, 52, 240, 92,
               {"Values": ["measure:Index (size-adjusted)"]},
               "Index (size-adjusted)"),
        visual("card", 1016, 52, 248, 92, {"Values": ["measure:Index (raw)"]},
               "Index (raw) - audit only"),
        # Five diagnosis categories, cross-filtering the scatter. Level and
        # trajectory stay apart here: no composite rank.
        visual("barChart", 768, 152, 496, 150,
               {"Category": ["dim_branch:diagnosis"],
                "Y": ["measure:Underperforming Branches"]}, "Diagnosis"),
        # The flags carry the formatting, not the index - they are what a
        # reader needs to spot.
        visual("tableEx", 768, 310, 496, 162, {"Values": [
            "dim_branch:city", "dim_branch:index_size_adjusted",
            "dim_branch:cagr_3y_pct", "dim_branch:booking_concentration",
            "dim_branch:catchment_partly_unmeasured",
            "dim_branch:position_drift_miles", "dim_branch:county_agrees"]},
            "Ranked, with the four flags",
            sort=("dim_branch:index_size_adjusted", "asc")),
        textbox(16, 482, 1248, 76, [
            run("Brown County holds 18.6% of branches and 51.7% of deposits. ",
                "10pt", True, WARN),
            run("Booking-concentration branches are flagged and excluded from "
                "the index. Rank on the size-adjusted index, never the raw "
                "one: raw fell mechanically as catchment size rose - median "
                "1.518 in the smallest quartile against 0.416 in the largest "
                "- and inverted the ranking of every market.",
                "10pt", False, INK)]),
    ]

    # ---- Page 3: branch detail, drillthrough only ------------------------
    p3 = [
        textbox(16, 10, 900, 40, [run("Branch detail", "20pt", True, INK)]),
        visual("slicer", 16, 52, 260, 200, {"Values": ["dim_branch:city"]},
               "Branch"),
        visual("tableEx", 16, 260, 260, 212, {"Values": [
            "dim_branch:institution_name", "dim_branch:address",
            "dim_branch:market", "dim_branch:first_year",
            "dim_branch:last_year"]}, "Header"),
        visual("lineChart", 288, 52, 480, 220,
               {"Category": ["dim_year:year"], "Y": ["measure:Total Deposits"]},
               "Deposits 2019-2025"),
        visual("tableEx", 288, 280, 480, 192, {"Values": [
            "dim_branch:catchment_households", "dim_branch:predicted_deposits",
            "dim_branch:actual_deposits", "dim_branch:index_size_adjusted"]},
            "Index breakdown - potential, predicted, actual"),
        visual("tableEx", 780, 52, 484, 220, {"Values": [
            "dim_tract:tract_geoid", "dim_tract:households",
            "dim_tract:median_hh_income", "dim_tract:lmi_flag",
            "bridge_branch_catchment:distance_miles",
            "bridge_branch_catchment:is_primary"]},
            "Catchment tracts - distance and whether contested"),
        visual("tableEx", 780, 280, 484, 192, {"Values": [
            "fact_tract_competition:competitor_branches",
            "fact_tract_competition:radius_miles",
            "fact_tract_competition:competitor_per_10k_catchment_hh"]},
            "Competitors within tier radius"),
        textbox(16, 482, 1248, 76, [
            run("Flags. ", "10pt", True, WARN),
            run("position_drift_miles above threshold means the branch moved "
                "and its catchment was recomputed; catchment_partly_unmeasured "
                "means one or more tracts had suppressed ACS values. Either "
                "explains why an index reads as it does. fact_tract_competition "
                "is not a catchment bridge - it carries no branch identifier, "
                "because competitor branches have no catchments here.",
                "10pt", False, INK)]),
    ]

    pages = [("Market opportunity", p1), ("Branch performance", p2),
             ("Branch detail", p3), ("Recommendation and equity", p4)]

    sections = []
    for i, (name, visuals) in enumerate(pages):
        # Page 3 is a drillthrough target and must not appear in the page
        # navigator. visibility 1 = HiddenInViewMode.
        hidden = name == "Branch detail"
        sections.append({
            "config": json.dumps({"visibility": 1}) if hidden else "{}",
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
    known_measures = set(MEASURE_HOME)

    # Every table[column] reference in every generated measure, and in the
    # hand-written measures.dax reference file. A DAX measure naming a column
    # that is not there fails at load and names the MEASURE, not the column -
    # so it is cheaper to catch here. measures.dax is checked too because it
    # is a second copy of the same logic and second copies drift: it carried
    # branch_performance[index_vs_market] and [service_type], neither of which
    # exists on that table.
    dax_sources = {"generated": "\n".join(
        e for _tbl, ms in (("", DEPOSIT_MEASURES), ("", PERF_MEASURES),
                           ("", COVERAGE_MEASURES), ("", TRACT_MEASURES),
                           ("", CAPTURE_MEASURES), ("", INDEX_MEASURES),
                           ("", WHATIF_MEASURES))
        for _n, e, *_ in ms)}
    ref = BASE / "measures.dax"
    if ref.exists():
        dax_sources["measures.dax"] = re.sub(
            r"//[^\n]*", "", ref.read_text(encoding="utf-8"))

    for label, body in dax_sources.items():
        for tbl, col in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\[([^\]]+)\]", body):
            if tbl in tables:
                if col not in tables[tbl].columns and col not in known_measures:
                    problems.append(f"[{label}] {tbl} has no column {col!r}")
            elif tbl not in {p for p, _ in WHATIF}:
                problems.append(f"[{label}] unknown table {tbl!r}")

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
    write_measures_dax()
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
