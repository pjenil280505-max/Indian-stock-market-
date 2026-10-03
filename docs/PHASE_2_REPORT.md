# Phase 2 — Fundamentals + News Evidence Layer

**Status:** design approved (2026-10-03). Step **2.0 (read-only source probe) complete**.
Nothing is deployed; no database, Worker or `main` change. Production stays at `a153eb1`.

Scope: collect, normalise, validate and store *evidence* (filings, announcements,
fundamentals) point-in-time. No scores, signals, trading or AI decisions.

---

## 2.0 Source probe — measured, not assumed

Four read-only runs from a GitHub runner (NSE blocks other egress), with no
database, no secrets, nothing stored, and metadata-only output. In total 43
requests: 42 returned 200, **none were refused**, and **no cookies were needed**.
Every call used the honest User-Agent; nothing was evaded.

| Run | What it measured |
|---|---|
| [37096123196](https://github.com/pjenil280505-max/Indian-stock-market-/actions/runs/37096123196) | Reachability, fields, announcement volume and depth, corporate actions with dates |
| [37096311575](https://github.com/pjenil280505-max/Indian-stock-market-/actions/runs/37096311575) | Results cut-off, old XBRL links, Q2 and bank XBRL, shareholding history |
| [37096477743](https://github.com/pjenil280505-max/Indian-stock-market-/actions/runs/37096477743) | Year XBRL starts; the integrated-filing page's API references |
| [37096608630](https://github.com/pjenil280505-max/Indian-stock-market-/actions/runs/37096608630) | Whether post-2025 results arrive as announcements |

### Source table (measured)

| Source (NSE `/api/…`) | Depth measured | Exchange timestamp | Identity | Volume | Backtest suitability |
|---|---|---|---|---|---|
| `corporate-announcements` | **Back to at least 2008-10-01** | `an_dt` in every year; `exchdisstime` (to the second) from 2021; `seq_id` from 2016 (absent in 2008 and 2011) | `sm_isin` on 100% of records in every sample | 805–1,649 a day in 2026 (629 in 2025; 252 in 2016; 136 in 2008); about 700 bytes of JSON per record | **High** |
| `corporates-financial-results` | Aug 2012 → **Feb 2025 only**. Returns `[]` for Aug 2025 and Aug 2026 | `broadCastDate` in every year; `exchdisstime` from 2016 | `isin` on 100% | 1,430–1,900 filings in a peak week | **High, 2020 to Feb 2025** |
| …its XBRL (`nsearchives`) | **None in 2016–2017; about 70% of filings in 2018–2019; 100% from 2020** | XBRL files carry `Last-Modified` | ISIN is *absent* from the general (Ind AS) XBRL, so use the JSON `isin` | 21–93 KB per file | High |
| `corporate-share-holdings-master` | Per-company history starts **around Dec 2021** (RELIANCE 22 records, TCS 20) | `broadcastDate` and `systemDate`, about 5 s apart | ISIN in JSON (97%) and in the XBRL | 1,668 filings in the deadline week | **Medium: revisions appear to replace the original** (98 of 1,668 had a `revisionDate`) |
| …its XBRL | Same | — | ISIN present | 195–523 KB per file | Medium |
| `corporates-corporateActions` with `from_date` / `to_date` | **History works**: 846 actions in Sep 2016, 897 in Sep 2021, 485 in Sep 2026. **Resolves open question U8** | **None**: `caBroadcastDate` is always empty | `isin` on 100% | — | Medium. The ex-date is reliable; the date it was announced is unknown |

### Facts the design depends on

1. **No structured results source has been found for Mar 2025 onwards.** The classic endpoint stops after Feb 2025. The integrated-filing page names no API in its HTML or its two static scripts, and I did not guess endpoint names. Results-like announcements on the peak days of 13–14 Aug 2026 were 12–14 "Integrated Filing- Financial" filings a day, **all PDF**. This is the largest open gap.
2. **Results XBRL uses four templates,** identifiable from the file name: `INDAS`, `NBFC_INDAS`, `BANKING`, `NONINDAS`. The JSON `bank` flag takes the values N, F and B.
   - Banks use different element names: `ProfitLossForThePeriod` instead of `ProfitLossForPeriod`, plus `InterestEarned`, `OperatingProfitBeforeProvisionAndContingencies` and `GrossNonPerformingAssets`.
   - So the mapping must be done per template.
3. **Units:** values are in INR with `decimals = -5`, meaning rounded to the lakh. EPS uses `INRPerShare`.
4. **Quarterly vs half-yearly content:** a Q1 XBRL had profit-and-loss items only. A Q2 XBRL had the balance sheet (`Assets`, `Equity`, `BorrowingsCurrent`, `BorrowingsNoncurrent`, `CashAndCashEquivalents`) and the cash flow statement, including `PurchaseOfPropertyPlantAndEquipment…` (capex). So ROE, debt-to-equity and free cash flow are **half-yearly at best**.
5. **Standalone vs consolidated:** the JSON `consolidated` field is "Consolidated" or "Non-Consolidated", and the XBRL `NatureOfReportStandaloneConsolidated` field is present in the samples.
6. **Shareholding XBRL** carries promoter, public and institutional breakdowns as dimensions.
   - **Pledge appears only as yes/no flags.** No pledged-share count was found, so pledge % is *not* available from this source.
   - It **contains the PAN numbers of named shareholders, which is personal data.** Raw copies must be stored privately and never put into Neon or logs.
7. **Announcement categories (`desc`) on 30 Sep 2026:** "Shareholders meeting" 401 and "Trading Window" 210, out of 1,217. Roughly half the volume has low evidentiary value, which matters for the storage budget.
8. **`hasXbrl` is True on every announcement** and carries no information.

### What this changes in the approved design

| Area | Change |
|---|---|
| Fundamentals backtest window | **2020-Q1 to Feb 2025 at full coverage** (2018–2019 at about 70%). Nothing structured before 2018. |
| Current results | **Blocked until a structured source is found.** See decision D1. |
| Shareholding history | Usable from around Dec 2021, with **latest-version semantics**. Original versions must be captured going forward, because a later revision replaces them. |
| Corporate actions | Historical backfill is possible. Announcement time comes from matching announcements, otherwise the record is marked `knowledge_time_quality = 'inferred'`. |
| Announcement storage | About 260k a year [estimate]. Every record goes to R2 as gzipped JSON; Neon indexes the material categories. |
| What is time-critical | NSE announcements **can be backfilled to 2008**, so they are not urgent. The things that are lost if not captured now: third-party news (Upstox keeps 7 days), **original shareholding versions**, and XBRL files that NSE replaces silently. |

### Decisions needed before step 2.1

- **D1, the current-results source.** Options:
  - (a) **[Recommended]** You open NSE's "Integrated Filing – Financials" page in a normal browser, copy the request URL shown in DevTools → Network, and I probe it once. This is your own browser's request, so nothing is evaded.
  - (b) Probe BSE's results XBRL.
  - (c) Use Upstox fundamentals as forward-only snapshots (needs the token).
  - (d) Accept the gap.
- **D2:** Create the Cloudflare R2 bucket and an API token limited to that bucket. I will show the exact secret names first.
- **D3:** Confirm the Neon project's storage cap: 0.5 GB or 1 GB.
- **D4:** Decide whether to generate the Upstox Analytics Token (for news collection).

Probe code: `scripts/probe_phase2_sources.py` and `.github/workflows/phase2-source-probe.yml`. The workflow runs only on a push to this branch that changes the probe. Tests: `tests/test_phase2_probe.py`.

---

## 2.1 Issuer identity + evidence store — built, NOT deployed (commit `405263c`)

- Migration `0003` adds four tables and changes no existing table; tests prove Phase 1 data and
  columns are identical before and after. `issuers` / `symbol_issuer` key companies on the ISIN
  issuer code (partly-paid/DVR map to the same company; funds and invalid ISINs are flagged,
  never guessed). `symbol_aliases` records ticker history from first observation onward.
  `source_documents` registers raw evidence; a trigger refuses UPDATE of evidence columns and DELETE.
- R2 client: stdlib SigV4 signer verified against AWS's published example; pinned to the
  account endpoint; HEAD/GET/conditional PUT only.
- `scripts/phase2_update.py` + `phase2-update.yml`: identity sync + storage canary; refuses to
  run 10:30–15:45 UTC on weekdays; scheduled 00:31 UTC Tue–Sat.
- 44 new tests, 9 mutations all caught. One existing assertion generalised (the hard-coded
  migration list in `test_migrations.py`).
- **Blocked on you:** R2 is not enabled on the Cloudflare account (API: "Please enable R2
  through the Cloudflare Dashboard").

**Deploy order (when approved) — order matters:** Phase 1's daily job stops if a migration is
pending, so 0003 must be applied *before* the code reaches `main`'s schedule:
1. Enable R2, create bucket `isr-evidence`, create an R2 API token scoped to that bucket
   (Object Read & Write), add secrets `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
2. Run *Database migrations* `status`, then `apply` from this branch (0003 only; fingerprint
   guard checks Phase 1 data).
3. Merge to `main`; dispatch *Phase 2 evidence update* once; verify identity counts and
   `storage OK (written, verified)`.
Rollback: the code is additive; reverting the merge stops the job. The four tables can stay.

## Recent-results source — FOUND (runs 37097971685, 37098067076, 37098157958, 37098246667)

Method: read NSE's own page code (`/dist/js/sections/corporate-filings.js`) for the endpoint and
the exact URL it builds; no endpoint name or parameter was guessed.

| Item | Measured |
|---|---|
| Endpoint | `/api/integrated-filing-results?&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&type=Integrated Filing- Financials&page=N&size=M` → `{data, totalCount}` |
| Coverage | Aug 2026 week **2,221**; May 2025 week **921** (quarter ending 31-Mar-2025); Feb 2025 **0** — it begins exactly where the classic endpoint ends |
| Fields | `seq_Id`, `symbol`, `smName`, `qe_Date`, `consolidated` (Standalone/Consolidated), `audited`, `broadcast_Date` (44/50 filled), `creation_Date`, **`type_Sub` (Original/Revision)**, `revised_Date`, `revision_Remark`, `xbrl`, `ixbrl`. **No ISIN in the JSON** |
| XBRL | `INTEGRATED_FILING_{INDAS,NONINDAS,NBFC_INDAS}_*_WEB.xml`; taxonomy **`in-capmkt`** (new), same core element names as before; **ISIN present**; unit INR; `decimals` varies (−3/−4/−5) |
| RSS | `nsearchives…/content/RSS/Integrated_Filing_Financials.xml` — latest 20 only (insufficient alone) |

So structured, point-in-time results exist **from 2018 (partial) / 2020 (full) to today**, across
two endpoints that meet at the quarter ending Mar 2025. Knowledge time: `broadcast_Date`, else
`creation_Date` (quality flagged). Also seen in NSE's code, not yet probed:
`/api/corporate-pledgedata` (could fill the pledge-% gap), `/api/annual-reports-xbrl`,
`/api/corporate-credit-rating`, `/api/corporate-board-meetings`.
