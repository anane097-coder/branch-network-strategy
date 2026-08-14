# 05 — Risks, Limitations, and Data-Access Problems

## 1. The federal open-data environment is materially less stable than it was

This is the most important finding in the research and it changes how the projects should be built.

Throughout 2025 and into 2026, federal datasets were removed, altered, or discontinued at a scale that has been difficult even for professional researchers to track. Reporting in early 2026 put the estimate at **well over 3,000 datasets removed from public access** ([NOTUS, Feb 2026](https://www.notus.org/trump-white-house/federal-data-is-disappearing); [Marketplace](https://www.marketplace.org/story/2026/02/06/over-3000-government-data-sets-removed-from-public-access)). A Congressional Research Service report (R48889, March 2026, [congress.gov](https://www.congress.gov/crs-product/R48889)) found that while statute governs how data is *added* to Data.gov, it does not address whether or how data may be *removed*, that agencies have broad discretion over what to list, and that Data.gov catalog accuracy is unreliable — it is a catalog pointing at agency-hosted files, not an archive. Specific casualties include the Drug Abuse Warning Network, some HHS survey breakdowns, and reduced access to restricted-use research data at several agencies. Separately, the 43-day shutdown beginning October 1, 2025 halted collection and publication at most statistical agencies ([Richmond Fed, Feb 2026](https://www.richmondfed.org/publications/research/economic_brief/2026/eb_26-04)), which is why the 2020–2024 ACS 5-year release slipped from its usual December slot to January 29, 2026.

**What this means practically:**

- **Prefer datasets with a statutory filing requirement behind them.** FDIC SOD, HMDA, CMS PBJ, and hospital MRFs exist because institutions are legally required to file them. They are far more durable than discretionary agency publications.
- **Download and archive the raw extracts on day one**, commit them (or a documented subset) to the repo, and pin the vintage in the README. A portfolio project whose data source 404s in six months is worse than no project.
- **Never build the flagship on a single agency's discretionary publication.**
- Verified as actively publishing during this research (August 2026): FDIC SOD and API, HMDA 2025 data (published March 31, 2026; national snapshot June 23, 2026), ACS 2020–2024 5-year and the Census API, BLS JOLTS (June 2026 release published Aug 4, 2026), USAspending API, CMS hospital price transparency program, Chicago Data Portal 311, and data.milwaukee.gov (datasets refreshed Aug 12, 2026).

## 2. Access mechanics that will cost time if not anticipated

| Source | Issue |
|---|---|
| **Census API** | All data queries now require an API key. Free, instant, but a hard stop if not obtained first. |
| **HMDA Modified LAR** | The public file is *modified* to protect privacy — certain fields are binned or withheld (age, some amounts). The separate Snapshot National file has a different structure. She must read the field documentation and state in the case study which file she used and what it omits. Getting this wrong is the classic HMDA rookie error. |
| **Hospital MRFs (P3)** | There is **no central repository**. Each hospital publishes its own file at its own URL, sizes range from megabytes to many gigabytes, and formats vary despite the schema. This is the largest single execution risk in the shortlist. Mitigation: fix the hospital list to 15–25 in one metro, download once, archive, and document any that could not be retrieved — the ones that fail *are themselves a compliance finding*. |
| **Socrata portals** (Chicago, others) | Anonymous API access is throttled. Register a free app token. Bulk CSV export of a multi-million-row dataset can time out; paginate. |
| **FDIC SOD** | Institution and branch identifiers change across years through mergers. Longitudinal analysis requires handling the structure-change history, not just joining on name. |
| **CMS PBJ** | Quarterly files, large; facility identifiers must be joined to the Provider Information file. Straightforward but not small. |
| **Geography vintages** | ACS 2020–2024 uses 2020 census tract boundaries; HMDA uses the tract vintage in effect for the filing year; older city datasets may use 2010 tracts. Mixing vintages silently produces wrong joins. Pin one geography vintage and document it. |

## 3. Tooling and publishing risks

**Power BI publishing is the trap most portfolio guides don't mention.** Publish to web requires a Power BI license and, in practice, a work or school account with the tenant's "publish to web" setting enabled — a personal Microsoft account will not do it. Microsoft's own documentation confirms a license is needed even from My Workspace, and the community Q&A on exactly this portfolio use case ends with "buy a month of Pro and expect the link to break when it lapses."

Recommended approach:
1. **Build in Power BI Desktop** (free) because that's what the job postings ask for — Milwaukee County, Connect for Health, and Trustmark all name it.
2. **Publish the interactive version to Tableau Public** (free, permanent, no account gymnastics) *or* accept a Pro month timed to the active job search.
3. Regardless: embed a short screen-recorded walkthrough (30–60 seconds) and high-quality screenshots directly in the case-study page, and offer the .pbix as a download. Assume a recruiter will never click through to an external dashboard.

**Cloud warehouse temptation.** Several postings name Snowflake, Fabric, and Databricks. It is tempting to build on one. I'd advise against it for the flagship: free tiers expire, and a broken link during a job search is worse than the marginal keyword. DuckDB or Postgres locally, with the SQL clearly published, demonstrates the same skill. If she wants the keyword, do a small, separate, clearly-scoped exercise on a free tier.

## 4. Analytical and ethical risks — read this before P1 or P2

Both P1 and P2 use HMDA and census-tract demographics. These are the exact data sources used in fair-lending enforcement, and the analysis sits close to a genuinely serious subject.

- **A "market opportunity score" that systematically deprioritizes lower-income or majority-minority tracts is, in substance, a redlining model** — regardless of intent, and regardless of whether race was an input. If income, home value, and existing-branch proximity are weighted heavily, that is a plausible output.
- Guardrails for the flagship: state the CRA context explicitly; include an equity check as a named acceptance criterion (the recommended set must be evaluated for its distribution across low- and moderate-income tracts and the result reported, not hidden); frame the unmet-mortgage-demand KPI as *identifying underserved demand the bank is missing*, which is both the more defensible framing and the more commercially interesting one.
- For P2, disparities in denial rates **are not evidence of discrimination** on their own — HMDA's public file lacks credit score, DTI, and LTV, the very factors that drive underwriting. The correct output is "here are the gaps that remain unexplained by the factors we can observe, and here is what we'd need to pull internally to close the question." Saying that precisely is a competence signal. Saying it imprecisely is a red flag to any compliance interviewer.
- **Do this well and it becomes an asset**, not a liability: handling a sensitive analysis with visible care is exactly what a bank hiring for a compliance or strategy analyst wants to see.

Related, smaller: don't publish an analysis framed as insider strategy for a real named company. Use a realistic composite institution.

> **Superseded — 2026-08-14.** The composite-institution recommendation in the paragraph above was reversed by `08` §0 and subsequently locked. The project uses a **real, named institution analyzed outside-in from public filings only**. The concern behind the original wording still stands and is handled by the guardrails in `08` §0: state on page one that only public data was used and that there is no relationship with the institution, and frame closures as *"candidates for review"* rather than "close these." The rest of this section — the redlining risk, the CRA framing, the limits of the public HMDA file — is **not** superseded and remains the governing guidance.

## 5. Scope and completion risk

The brief already identifies this as the main danger and it's correct. Specific tripwires for this set of projects:

- **P3 will try to become an engineering project.** The temptation is to build a general-purpose MRF parser for all hospitals. Don't. Fixed hospital list, fixed service line, validator that checks a documented subset of the v3.0 requirements.
- **P1 will try to become a spatial-analysis project.** Drive-time isochrones, gravity models, and trade-area estimation are a rabbit hole. A documented radius assumption with a stated limitation is entirely acceptable and is what many real teams use.
- **Feature creep in the portfolio site itself.** The site is a container. It should be simple, fast, mobile-legible, and finished in a weekend.
- Set a hard rule: **no second project starts until the first is deployed publicly and readable end to end.**

## 6. Things I could not verify and that we should check at build time

- The current published vintage of County Business Patterns (P1's small-business input). If the latest vintage is stale, the project is unaffected — swap in ACS business/employment measures — but confirm before designing around it.
- BTS TranStats availability and current coverage (only relevant if P12 is revived, which I don't recommend).
- Whether Milwaukee's open data portal exposes a full historical 311/service-request dataset comparable to Chicago's, or only the aggregate comparison views visible on the landing page. Relevant only if P4 or P14 moves up the list.
