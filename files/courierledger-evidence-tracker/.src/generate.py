#!/usr/bin/env python3
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from copy import copy

OUT = "/home/hatch/workspace/goals/courierledger-planning-and-market-validation/files/courierledger-evidence-tracker/courierledger-evidence-tracker.xlsx"

NAVY = "14324F"; TEAL = "0F766E"; LIGHT = "E6F0ED"; GREY = "F2F2F2"
YELLOW = "FFF7CC"; BLUE = "1D4ED8"; GOLD = "FEF3C7"
thin = Side(style="thin", color="B0BEC5")
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
TITLE = Font(name="Calibri", size=16, bold=True, color=NAVY)
SUB = Font(name="Calibri", size=10, color="475569")
HDR = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
BODY = Font(name="Calibri", size=10)
BOLD = Font(name="Calibri", size=10, bold=True)
INPUTF = Font(name="Calibri", size=10, color=BLUE)
FORMF = Font(name="Calibri", size=10, color="000000")
SMALL = Font(name="Calibri", size=9, color="475569", italic=True)

wb = openpyxl.Workbook()
wb.properties.title = "CourierLedger Evidence Tracker and Decision Scorecard"
wb.properties.creator = "CourierLedger validation"

def setup(ws, title, subtitle, widths, freeze="A6"):
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1; ws.page_setup.fitToHeight = 0; ws.page_setup.orientation = "landscape"
    ws.print_title_rows = "1:5"
    for i, w in enumerate(widths, 1): ws.column_dimensions[get_column_letter(i)].width = w
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(widths))
    c = ws.cell(row=1, column=1, value=title); c.font = TITLE
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(widths))
    c = ws.cell(row=2, column=1, value=subtitle); c.font = SUB; c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 28
    if freeze: ws.freeze_panes = freeze

def header_row(ws, row, headers):
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=i, value=h); c.font = HDR; c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center"); c.border = BORDER
    ws.row_dimensions[row].height = 36
    ws.auto_filter.ref = f"{get_column_letter(1)}{row}:{get_column_letter(len(headers))}{row}"

def style_range(ws, r1, r2, ncols, input_cols, formula_cols=()):
    for r in range(r1, r2+1):
        for col in range(1, ncols+1):
            c = ws.cell(row=r, column=col); c.font = FORMF; c.border = BORDER
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if col in input_cols: c.fill = PatternFill("solid", fgColor=YELLOW); c.font = INPUTF
            if col in formula_cols: c.fill = PatternFill("solid", fgColor=GREY)

def note(ws, row, text, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text); c.font = SMALL; c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = 26

def dv(ws, formula, rng, prompt):
    d = DataValidation(type="list", formula1=formula, allow_blank=True)
    d.prompt = prompt; d.promptTitle = "Choose"; ws.add_data_validation(d); d.add(rng); return d

# ---------- README ----------
ws = wb.active; ws.title = "README"
setup(ws, "CourierLedger — Evidence Tracker & Decision Scorecard",
      "14-day market validation in Bangladesh. Empty template: no interviews have been done and no results are recorded. Fill only yellow cells; grey cells are formulas.",
      [28, 95], freeze=None)
rows = [
 ("Purpose", "Track what sellers actually did and said about COD reconciliation — recent behaviour, time, differences and workarounds — so the Day 7 and Day 14 decisions rest on evidence, not enthusiasm or positive comments."),
 ("How to use", "1) Screen a seller in Screening_Log.  2) Log the interview in Interview_Log the same day.  3) Add each distinct finding in Evidence_Log.  4) Log any prototype demo (synthetic data only) in Demo_Log.  5) Check competitors in Competitor_Log.  6) Read Metrics_Dashboard, then complete Scorecard_Day7 / Scorecard_Day14."),
 ("Colour legend", "Yellow fill + blue text = you type here.  Grey fill = formula — do not overwrite.  White = reference / pre-set threshold set BEFORE interviews."),
 ("Privacy rule", "Use seller codes (e.g. S-001), not names, phone numbers or bank details. Do not collect unnecessary personal or financial information. If a seller offers an example, accept only consented, redacted column headers, rule documentation, or a synthetic reproduction. Never upload real merchant data into the P0 prototype."),
 ("Money rule", "Record amounts in BDT as the seller reported them. If a seller does not know an amount, leave it blank — blank means unknown, never zero. Do not treat a public courier pricing page as proof of that seller's contractual rates."),
 ("Status", "P0 prototype: 47 tests passed, 0 failed (reported and verified in the prior phase). That verifies the synthetic-data logic only — it is not market evidence."),
 ("Thresholds", "The thresholds on the two scorecards were set before any interviews, as the plan requires. If you change one mid-validation, note the date and reason in the scorecard's Change log row — do not silently move a gate."),
 ("Example row (synthetic only — not a real interview)", "S-EX | Channel: Facebook page | Shipments/month: 320 | Couriers: 2 (Steadfast, Pathao) | Minutes per settlement cycle: 90 | Cycles/month: 4 | Unexplained differences in last 3 cycles: 2 | Method: Spreadsheet | Willing to test prototype: Yes.  This example shows the shape of a completed row only; it is NOT entered in Interview_Log and is NOT counted in any metric."),
]
r = 4
for a, b in rows:
    ws.cell(row=r, column=1, value=a).font = BOLD
    c = ws.cell(row=r, column=2, value=b); c.font = BODY; c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 42; r += 1
ws.sheet_properties.pageSetUpPr.fitToPage = True

# ---------- Screening_Log ----------
ws = wb.create_sheet("Screening_Log")
setup(ws, "Screening Log — who is eligible to interview",
      "Target: Bangladesh-based online sellers using COD couriers (Steadfast, Pathao, Paperfly, RedX, eCourier). Eligibility is a formula from the Yes/No answers — do not override it.",
      [12,12,18,14,13,13,14,13,30,15,13,16])
note(ws, 3, "Yellow = type here. Grey = formula. A seller is eligible only if based in Bangladesh, selling online, using a COD courier, and reconciling (or receiving) courier settlements.", 12)
note(ws, 4, "Do not screen in sellers on hypothetical interest in a product. Screen on current behaviour only.", 12)
headers = ["Screening ID","Date","Seller code","Bangladesh based? (Yes/No)","Sells online? (Yes/No)","Uses COD courier? (Yes/No)","Shipments / month","No. of couriers","Couriers used (text)","Reconciles settlements? (Yes/No)","Eligible (formula)","Interview status"]
header_row(ws, 5, headers)
for r in range(6, 36):
    ws.cell(row=r, column=11, value=f'=IF(COUNTBLANK(D{r}:F{r})=3,"",IF(AND(D{r}="Yes",E{r}="Yes",F{r}="Yes"),"Eligible","Not eligible"))')
style_range(ws, 6, 35, 12, input_cols=set(range(1,11))|{12}, formula_cols={11})
for col in "DEFG": dv(ws, '"Yes,No"', f"{col}6:{col}35", "Yes or No")
dv(ws, '"Invited,Interviewed,Declined,No response,Not invited"', "L6:L35", "Status")
for r in range(6, 36): ws.cell(row=r, column=7).number_format = '#,##0'; ws.cell(row=r, column=8).number_format = '0'

# ---------- Interview_Log ----------
ws = wb.create_sheet("Interview_Log")
setup(ws, "Interview Log — one row per completed interview",
      "Ask about the seller's most recent settlement cycles, not hypothetical enthusiasm. Leave an amount blank if the seller does not know it — blank is unknown, never zero.",
      [12,12,11,16,14,13,11,26,13,13,11,13,14,15,11,22,14,13,13,30])
note(ws, 3, "Yellow = type here. Grey = formula (Monthly minutes = minutes per cycle x cycles per month). Record the seller's own words and recent examples in Evidence_Log, not just scores here.", 20)
note(ws, 4, "Couriers in scope: Steadfast, Pathao, Paperfly, RedX, eCourier — do not assume they settle the same way. Record each seller's couriers as they state them.", 20)
headers = ["Interview ID","Date","Seller code","Status","Sales channel","Shipments / month","No. of couriers","Couriers used (text)","Minutes per settlement cycle","Cycles / month","Monthly minutes (formula)","Unexplained diffs in last 3 cycles (count)","Last-cycle unexplained amount (BDT, blank = unknown)","Problem recurrence (1-5)","Problem severity (1-5)","Current reconciliation method","Willing to test prototype?","Follow-up consent?","Evidence strength","Recent example summary (their words, brief)"]
header_row(ws, 5, headers)
for r in range(6, 46):
    ws.cell(row=r, column=11, value=f'=IF(OR(I{r}="",J{r}=""),"",I{r}*J{r})')
style_range(ws, 6, 45, 20, input_cols=set(range(1,21))-{11}, formula_cols={11})
dv(ws, '"Completed,Scheduled,No-show,Declined"', "D6:D45", "Status")
dv(ws, '"Facebook page,Shopify,Own website,WooCommerce,Mixed,Other"', "E6:E45", "Channel")
dv(ws, '"Spreadsheet,Courier dashboard only,Notebook / manual,Accounting tool,Platform tool (named in Evidence Log),No regular check,Other"', "P6:P45", "Method")
dv(ws, '"Yes,No,Undecided"', "Q6:Q45", "Willing to test")
dv(ws, '"Yes,No"', "R6:R45", "Consent")
dv(ws, '"Strong,Moderate,Weak,None"', "S6:S45", "Evidence strength")
for r in range(6, 46):
    ws.cell(row=r, column=6).number_format = '#,##0'; ws.cell(row=r, column=13).number_format = '#,##0'
    ws.cell(row=r, column=9).number_format = '#,##0'; ws.cell(row=r, column=11).number_format = '#,##0'

# ---------- Evidence_Log ----------
ws = wb.create_sheet("Evidence_Log")
setup(ws, "Evidence Log — one row per distinct finding",
      "Separate what a seller reported, what you observed, and what a document showed. A positive comment is not demand evidence.",
      [12,12,12,20,46,16,13,18,42,18])
note(ws, 3, "Yellow = type here. Topic covers the full reconciliation chain: COD tracking, delivery/return fees, partial & returned shipments, payout differences, and bank settlement verification.", 10)
note(ws, 4, "Consent basis: record what the seller agreed to share. Redacted headers / rule documents / synthetic reproductions only — never real merchant financial rows.", 10)
headers = ["Evidence ID","Date","Interview ID","Evidence type","Finding (what actually happened, most recent example)","Supports hypothesis?","Strength","Topic","Detail / quote summary","Consent basis"]
header_row(ws, 5, headers)
style_range(ws, 6, 55, 10, input_cols=set(range(1,11)))
dv(ws, '"Seller report (recent example),Seller report (general),Observed workflow,Document shared (redacted),Demo reaction,Competitor observation"', "D6:D55", "Type")
dv(ws, '"Supports,Contradicts,Mixed,Unclear"', "F6:F55", "Direction")
dv(ws, '"Strong,Moderate,Weak"', "G6:G55", "Strength")
dv(ws, '"COD tracking,Statement reconciliation,Delivery fees,Return fees,Partial shipments,Returned shipments,Payout differences,Bank settlement,Unexplained differences,Workaround cost,Other"', "H6:H55", "Topic")

# ---------- Demo_Log ----------
ws = wb.create_sheet("Demo_Log")
setup(ws, "Prototype Demo Log — P0 with synthetic data only",
      "Demo steps: 1 CSV upload, 2 column mapping, 3 reconciliation results, 4 under-collection, 5 fee discrepancies, 6 statement inconsistency, 7 data-quality warnings. Never load real merchant data.",
      [12,12,12,13,13,13,13,13,13,13,15,15,40])
note(ws, 3, "Yellow = type here. Grey = formula (steps shown counts the Yes answers). Judge recognition by whether the seller matches a step to their own recent example — not by politeness.", 13)
note(ws, 4, "If a seller offers real data during a demo, stop and switch back to the synthetic dataset. Note the offer in Notes; do not accept the file.", 13)
headers = ["Demo ID","Date","Interview ID","Synthetic data only confirmed?","Step 1 Upload","Step 2 Mapping","Step 3 Results","Step 4 Under-collection","Step 5 Fee discrepancy","Step 6 Statement inconsistency","Step 7 Data-quality warnings","Steps shown (formula)","Seller matched a step to their own recent example?","Notes: questions raised / gaps named"]
header_row(ws, 5, headers)
# fix: header count is 14, adjust widths handled generically
for r in range(6, 26):
    ws.cell(row=r, column=12, value=f'=IF(COUNTBLANK(E{r}:K{r})=7,"",COUNTIF(E{r}:K{r},"Yes"))')
style_range(ws, 6, 25, 14, input_cols=set(range(1,15))-{12}, formula_cols={12})
for col in "DEFGHIJKLM": dv(ws, '"Yes,No"', f"{col}6:{col}25", "Yes or No")
dv(ws, '"Yes - named their own example,Partly,No"', "M6:M25", "Recognition")
ws.column_dimensions['N'].width = 40; ws.column_dimensions['M'].width = 22

# ---------- Competitor_Log ----------
ws = wb.create_sheet("Competitor_Log")
setup(ws, "Competitor & Alternative-Workflow Log",
      "Vendor wording below is a claim to verify, not a verified capability. Hands-on testing or a seller's own account is stronger evidence than a marketing page. Prices shown are the vendor's listed price — never a merchant's courier contract rate.",
      [26,20,20,26,44,18,34,13,16,46])
ws.column_dimensions['N'].width = 40
note(ws, 3, "Yellow = update here as you verify. Pre-filled rows are research leads from public vendor pages checked for this plan; re-check the source URL and date before relying on any row.", 10)
note(ws, 4, "Key gap to test for every tool: does it work for non-Shopify / Facebook (F-commerce) sellers, handle partial & returned shipments, keep bank settlement separate from courier-reported payout, and use merchant-confirmed rates rather than public pricing?", 10)
headers = ["Competitor / alternative","Type","Channel fit (as claimed)","Couriers (as claimed)","COD reconciliation claim (vendor wording, paraphrased closely)","Evidence status","How verified / how to verify","Date checked","Listed price (as shown)","Gap / question to check"]
header_row(ws, 5, headers)
comp = [
 ("ParcelPilotBD (Shopify app)","Direct COD reconciliation tool","Shopify only","Steadfast, Pathao, RedX","Upload a courier payout statement; each line matched to Shopify orders and re-checked as COD collected minus courier fees; lines that do not add up or never appear are flagged","Vendor claim only","Vendor page read; verify hands-on with a synthetic statement and ask a Shopify seller who uses it","2026-10-31","$5 / month (as listed)","Does it serve Facebook / non-Shopify sellers at all? How are partial, returned and bank-settlement differences handled?","https://apps.shopify.com/parcelpilotbd"),
 ("SmartParcel (Shopify app)","Courier booking + COD tool","Shopify only","Steadfast, Pathao, Carrybee","Track parcels live and reconcile COD automatically; auto COD reconciliation on one dashboard","Vendor claim only","Vendor page read; verify what 'automatically' means — statement upload, API, or status only?","2026-10-31","$1.99 / month (as listed)","Is reconciliation line-by-line against merchant-confirmed fees, or only totals? Paperfly / RedX / eCourier covered?","https://apps.shopify.com/smartparcel-bd?search_id=311f32c1-6e1f-4ad5-bcb4-6c1ed6d7a437&surface_detail=orders-and-shipping-orders-orders-other&surface_inter_position=1&surface_intra_position=18&surface_type=category&surface_version=redesign"),
 ("ecomsbd","Seller operations platform","Online & F-commerce sellers in Bangladesh","Steadfast, Pathao, RedX (own accounts)","Orders, COD, inventory and profit together; COD owed / paid / overdue tracked; payout matched against expected, line by line","Vendor claim only","Vendor page read; verify with a trial / seller interview — especially fee and return handling","2026-10-31","","If it already reconciles line by line for F-commerce, what is left unserved — partials, bank settlement, multi-courier?","https://ecomsbd.com/"),
 ("OrderPilot (Cognifyx)","Order / courier / COD platform","Facebook, Instagram & web sellers","Steadfast, Pathao, RedX; others recorded by hand","Upload the courier payout statement; each parcel matched and shortfalls flagged, not hidden; profit after courier, COD, return and ad costs","Vendor claim only","Vendor page read; verify statement formats supported and whether rates are merchant-confirmed","2026-10-31","","Closest F-commerce comparator — test its handling of partial delivery, returns and statement arithmetic","https://orderpilot.cognifyx.net/"),
 ("NexaStore","Commerce platform","Bangladesh online sellers","Pathao, Steadfast","Track orders, delivery charges, returns and the COD balance the courier still owes; reconcile collected cash, delivery fees and returns from settlement records","Vendor claim only","Vendor page read; verify whether reconciliation is line-level or a balance view","2026-10-31","","Balance-owed view vs line-by-line fee checking — which do sellers actually need?","https://nexastorebd.com/"),
 ("AmarPilot","Order / stock / COD dashboard","F-commerce & WhatsApp sellers","Pathao, Steadfast, RedX","COD reconciled per courier; courier COD cash and real profit in one dashboard; mark orders paid and track what each courier still owes","Vendor claim only","Vendor page read; verify depth of reconciliation in a demo / seller account","2026-10-31","","Does 'reconciled' mean statement-line matching, or marking paid on delivery?","https://amarpilot.com/"),
 ("Courier's own dashboard & statement (each courier separately)","Incumbent alternative","All sellers with that courier","Steadfast / Pathao / Paperfly / RedX / eCourier — log one row per courier as you learn","Seller checks the courier portal / statement itself; no independent re-check of fees against their own contract","To verify in interviews","Ask each seller to walk through their last check (screen or redacted headers only); note per courier, do not generalise","", "", "What can the portal not show — cross-courier totals, expected-vs-reported fees, bank arrival?",""),
 ("Spreadsheet (Excel / Google Sheets)","Manual alternative","All channels","All — one sheet per courier is common","Seller matches statement lines to orders by hand / lookup formulas and flags differences themselves","To verify in interviews","Ask to see the structure (not the data): columns, formulas, time per cycle, where it breaks","",  "Seller's own time cost","Where does it fail — partials, returns, duplicate tracking IDs, missing fee lines?",""),
 ("Notebook / bKash-bank balance check / no regular check","Manual alternative","All channels","All","Seller checks that money arrived roughly as expected, or does not reconcile line by line","To verify in interviews","Ask what triggered the last check and what a missed difference would look like","",  "Unpriced time / missed differences","Is the absence of checking a sign of no problem, or of no workable method? Distinguish in interviews.",""),
]
# Competitor_Log has 10 display cols + hidden source URL in col K
ws.cell(row=5, column=11, value="Source URL (verbatim)").font = HDR
ws.cell(row=5, column=11).fill = PatternFill("solid", fgColor=NAVY)
ws.column_dimensions['K'].width = 44
r = 6
for row in comp:
    for i, v in enumerate(row[:10], 1): ws.cell(row=r, column=i, value=v)
    ws.cell(row=r, column=11, value=row[10]); r += 1
style_range(ws, 6, 25, 11, input_cols={6,7,8,9,10})
dv(ws, '"Vendor claim only,Verified hands-on,Seller-reported,Assumption,Contradicted,To verify in interviews"', "F6:F25", "Evidence status")
for rr in range(6, 26): ws.row_dimensions[rr].height = 52

# ---------- Metrics_Dashboard ----------
ws = wb.create_sheet("Metrics_Dashboard")
setup(ws, "Metrics Dashboard — calculated from the logs (do not type here)",
      "All cells below are formulas over Interview_Log, Screening_Log, Evidence_Log and Demo_Log. With no interviews entered, counts are 0 and averages show 'No data yet' — that is the honest empty state, not a result.",
      [52,18,18,62], freeze=None)
header_row(ws, 5, ["Metric","Value","Unit","How to read it / source"])
IL = "Interview_Log"; rng = lambda col: f"{IL}!${col}$6:${col}$45"
metrics = [
 ("Screened sellers", f"=COUNTA(Screening_Log!A6:A35)", "sellers", "Screening_Log rows entered."),
 ("Eligible sellers", f'=COUNTIF(Screening_Log!K6:K35,"Eligible")', "sellers", "Screening_Log formula column."),
 ("Interviews completed", f'=COUNTIFS({rng("D")},"Completed")', "interviews", "Interview_Log Status = Completed. All metrics below use completed interviews only."),
 ("Avg. shipments / month (completed)", f'=IFERROR(AVERAGEIFS({rng("F")},{rng("D")},"Completed"),"No data yet")', "shipments", "Average, not a market size. Report the range in your notes as well."),
 ("Avg. couriers per seller", f'=IFERROR(AVERAGEIFS({rng("G")},{rng("D")},"Completed"),"No data yet")', "couriers", "Multi-courier use is a core hypothesis — one dashboard per courier is the pain it implies."),
 ("Avg. minutes per settlement cycle", f'=IFERROR(AVERAGEIFS({rng("I")},{rng("D")},"Completed"),"No data yet")', "minutes", "Seller-reported time for their most recent cycle."),
 ("Avg. monthly reconciliation minutes", f'=IFERROR(AVERAGEIFS({rng("K")},{rng("D")},"Completed"),"No data yet")', "minutes / month", "Formula column K in Interview_Log (minutes x cycles). This is the workaround time cost."),
 ("Sellers with >=1 unexplained diff. in last 3 cycles", f'=COUNTIFS({rng("D")},"Completed",{rng("L")},">=1")', "sellers", "Count, Interview_Log column L."),
 ("Share with >=1 unexplained diff.", f'=IFERROR(COUNTIFS({rng("D")},"Completed",{rng("L")},">=1")/COUNTIFS({rng("D")},"Completed"),"No data yet")', "% of completed", "Share of completed interviews. Blank L (not asked / unknown) is not counted as zero."),
 ("Sellers using spreadsheet as main method", f'=COUNTIFS({rng("D")},"Completed",{rng("P")},"Spreadsheet")', "sellers", "The main alternative workflow to beat."),
 ("Sellers with no regular check", f'=COUNTIFS({rng("D")},"Completed",{rng("P")},"No regular check")', "sellers", "Distinguish 'no problem' from 'no method' using Evidence_Log notes."),
 ("Willing to test prototype (Yes)", f'=COUNTIFS({rng("D")},"Completed",{rng("Q")},"Yes")', "sellers", "Willingness to test with synthetic data. Stronger than a positive comment; weaker than paying."),
 ("Share willing to test prototype", f'=IFERROR(COUNTIFS({rng("D")},"Completed",{rng("Q")},"Yes")/COUNTIFS({rng("D")},"Completed"),"No data yet")', "% of completed", "See scorecard thresholds."),
 ("Evidence items logged", "=COUNTA(Evidence_Log!A6:A55)", "items", "Evidence_Log rows."),
 ("Evidence items supporting hypothesis", f'=COUNTIF(Evidence_Log!F6:F55,"Supports")', "items", "Also read the Contradicts rows — do not report support alone."),
 ("Evidence items contradicting hypothesis", f'=COUNTIF(Evidence_Log!F6:F55,"Contradicts")', "items", "Contradicting evidence must appear in the Day 7 / Day 14 decision note."),
 ("Prototype demos run (synthetic only)", f'=COUNTIFS(Demo_Log!D6:D25,"Yes")', "demos", "Demo_Log rows where synthetic-only was confirmed. Any other demo does not count."),
 ("Demos where seller named their own example", f'=COUNTIFS(Demo_Log!M6:M25,"Yes - named their own example")', "demos", "Problem recognition grounded in their own recent case."),
 ("Competitors / alternatives logged", "=COUNTA(Competitor_Log!A6:A25)", "rows", "Pre-filled research leads count as logged, not as verified."),
 ("Competitors verified hands-on", f'=COUNTIF(Competitor_Log!F6:F25,"Verified hands-on")', "rows", "Vendor claims alone do not close the competitor question."),
]
r = 6
for name, formula, unit, how in metrics:
    ws.cell(row=r, column=1, value=name).font = BODY
    c = ws.cell(row=r, column=2, value=formula); c.font = FORMF
    ws.cell(row=r, column=3, value=unit).font = BODY
    ws.cell(row=r, column=4, value=how).font = BODY
    for col in range(1, 5): ws.cell(row=r, column=col).border = BORDER; ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="center")
    ws.cell(row=r, column=2).fill = PatternFill("solid", fgColor=GREY)
    ws.row_dimensions[r].height = 30; r += 1
for rr in (13, 18): ws.cell(row=rr, column=2).number_format = '0.0%'
note(ws, r+1, "Do not copy these numbers into a report without the completed-interview count beside them. Averages over 2-3 interviews are anecdotes, not market evidence.", 4)

ws.cell(row=13, column=2).number_format = '#,##0'
ws.cell(row=14, column=2).number_format = '0.0%'

# ---------- Scorecards ----------
def scorecard(sheet, title, subtitle, criteria, decision_rows):
    ws = wb.create_sheet(sheet)
    setup(ws, title, subtitle, [6,52,16,16,14,58], freeze=None)
    note(ws, 3, "Thresholds were set BEFORE interviews. Actual = formula from Metrics_Dashboard. Met? is a formula. The decision at the bottom is a human judgement recorded in words — the formula only summarises the criteria.", 6)
    note(ws, 4, "Options at each gate: Continue / Revise the problem hypothesis / Narrow the target market / Stop. Record which one, why, and what contradicting evidence was weighed.", 6)
    header_row(ws, 5, ["#","Criterion","Threshold (pre-set)","Actual (formula)","Met? (formula)","Evidence note (type here: which interviews / evidence IDs support this)"])
    r = 6
    for i, (crit, thr, actual) in enumerate(criteria, 1):
        ws.cell(row=r, column=1, value=i).font = BODY
        ws.cell(row=r, column=2, value=crit).font = BODY
        c = ws.cell(row=r, column=3, value=thr); c.font = INPUTF; c.fill = PatternFill("solid", fgColor=GOLD)
        c = ws.cell(row=r, column=4, value=actual); c.font = FORMF; c.fill = PatternFill("solid", fgColor=GREY)
        ws.cell(row=r, column=5, value=f'=IF(D{r}="","Not yet",IF(D{r}="No data yet","No data yet",IF(D{r}>=C{r},"Met","Not met")))').font = FORMF
        ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor=GREY)
        for col in range(1, 7):
            ws.cell(row=r, column=col).border = BORDER; ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="center")
        ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor=YELLOW); ws.cell(row=r, column=6).font = INPUTF
        ws.row_dimensions[r].height = 34; r += 1
    last = r - 1
    ws.cell(row=r, column=2, value="Criteria met (formula)").font = BOLD
    ws.cell(row=r, column=4, value=f'=COUNTIF(E6:E{last},"Met")&" of "&COUNTA(B6:B{last})').font = FORMF
    ws.cell(row=r, column=4).fill = PatternFill("solid", fgColor=GREY); r += 2
    for label, hint in decision_rows:
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        ws.cell(row=r, column=1, value=label).font = BOLD
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=6)
        c = ws.cell(row=r, column=3, value=""); c.font = INPUTF; c.fill = PatternFill("solid", fgColor=YELLOW)
        ws.cell(row=r, column=3).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 36; r += 1
    note(ws, r+1, "Change log: if any threshold above was changed after interviews began, record the date, old value, new value and reason in the Decision note — an unlogged change invalidates the gate.", 6)
    return ws

M = "Metrics_Dashboard"
day7 = [
 ("Interviews completed by Day 7", 8, f"={M}!B8"),
 ("Sellers with >=1 unexplained difference in last 3 cycles", 4, f"={M}!B13"),
 ("Share of completed interviews with >=1 unexplained difference", 0.5, f"={M}!B14"),
 ("Average monthly reconciliation minutes per seller", 60, f"={M}!B12"),
 ("Sellers willing to test the prototype with synthetic data", 3, f"={M}!B17"),
 ("Evidence items logged", 16, f"={M}!B19"),
 ("Competitor / alternative rows with verification attempted (hands-on or seller-reported)", 2, f'=COUNTIF(Competitor_Log!F6:F25,"Verified hands-on")+COUNTIF(Competitor_Log!F6:F25,"Seller-reported")'),
]
scorecard("Scorecard_Day7", "Day 7 Decision Scorecard — is the problem real and recurring?",
  "Gate at the end of Day 7. If interview volume or problem evidence is well below threshold, fix recruitment or revise the hypothesis in week 2 — do not lower the gate silently.",
  day7, [("Decision (Continue / Revise hypothesis / Narrow market / Stop)", ""), ("Reason — evidence for and against", ""), ("What changes in Days 8-14 as a result", "")])
for rr in range(6, 13): wb["Scorecard_Day7"].cell(row=rr, column=3).number_format = '0.0%' if rr == 8 else '#,##0'
wb["Scorecard_Day7"].cell(row=8, column=4).number_format = '0.0%'

day14 = [
 ("Interviews completed by Day 14", 16, f"={M}!B8"),
 ("Sellers with >=1 unexplained difference in last 3 cycles", 8, f"={M}!B13"),
 ("Share of completed interviews with >=1 unexplained difference", 0.5, f"={M}!B14"),
 ("Average monthly reconciliation minutes per seller", 60, f"={M}!B12"),
 ("Sellers using a spreadsheet as their main method", 5, f"={M}!B15"),
 ("Sellers willing to test the prototype with synthetic data", 6, f"={M}!B17"),
 ("Share willing to test the prototype", 0.35, f"={M}!B18"),
 ("Prototype demos run (synthetic data only)", 4, f"={M}!B22"),
 ("Demos where the seller named their own matching example", 3, f"={M}!B23"),
 ("Competitors verified hands-on", 1, f"={M}!B25"),
 ("Evidence items contradicting the hypothesis (record count; decision note must show they were weighed)", 0, f"={M}!B21"),
]
scorecard("Scorecard_Day14", "Day 14 Decision Scorecard — continue, revise, narrow, or stop?",
  "Gate at the end of Day 14. 'Continue' needs the problem, the time cost and the willingness-to-test criteria together — problem evidence alone, or enthusiasm alone, is not enough. Willingness to pay was explored through current workaround cost and past paying behaviour, never by asking 'would you pay for this?' in the abstract.",
  day14, [("Decision (Continue / Revise hypothesis / Narrow market / Stop)", ""), ("Strongest evidence FOR continuing", ""), ("Strongest evidence AGAINST / contradicting", ""), ("If Narrow: which segment, and what evidence singles it out?", ""), ("If Continue: what is the single next validation step (not M1 build)?", "")])
for rr in range(6, 17): wb["Scorecard_Day14"].cell(row=rr, column=3).number_format = '#,##0'
wb["Scorecard_Day14"].cell(row=8, column=3).number_format = '0.0'; wb["Scorecard_Day14"].cell(row=8, column=4).number_format = '0.0%'
wb["Scorecard_Day14"].cell(row=12, column=3).number_format = '0.0'; wb["Scorecard_Day14"].cell(row=12, column=4).number_format = '0.0%'

# ---------- Lists ----------
ws = wb.create_sheet("Lists & Definitions")
setup(ws, "Lists, Definitions & Metric Notes", "Reference only. Dropdown values used in the logs, and what each key term means in this validation.", [30,90], freeze=None)
defs = [
 ("Sales channels", "Facebook page, Shopify, Own website, WooCommerce, Mixed, Other — channel matters because several competitors found are Shopify-only."),
 ("Couriers in scope", "Steadfast, Pathao, Paperfly, RedX, eCourier. Never assume identical settlement rules; log per courier."),
 ("Evidence strength — Strong", "A specific recent example with amounts / time the seller could point to, an observed workflow, or a redacted document."),
 ("Evidence strength — Moderate", "A specific recent example without documents, consistent across the interview."),
 ("Evidence strength — Weak", "General impressions, old memories, or opinions about a hypothetical product. Weak evidence cannot carry a gate criterion alone."),
 ("Unexplained difference", "A gap between what the seller expected and what the courier reported / paid that the seller could not account for from fees they knew about. Never record it as a confirmed loss — it is unexplained until the fee basis is checked."),
 ("Blank amount", "Unknown. Never convert a blank or 'don't know' into 0 in any log."),
 ("Willingness to pay — how it is investigated", "Through current workaround cost (time, staff, tools they already pay for) and past paying behaviour for similar problems, recorded in Evidence_Log. Not through a direct hypothetical price question."),
 ("Decision options", "Continue / Revise the problem hypothesis / Narrow the target market / Stop — used identically at Day 7 and Day 14."),
]
r = 4
for a, b in defs:
    ws.cell(row=r, column=1, value=a).font = BOLD
    c = ws.cell(row=r, column=2, value=b); c.font = BODY; c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 34; r += 1

order = ["README","Screening_Log","Interview_Log","Evidence_Log","Demo_Log","Competitor_Log","Metrics_Dashboard","Scorecard_Day7","Scorecard_Day14","Lists & Definitions"]
wb._sheets = [wb[s] for s in order]
for s in wb.worksheets: s.sheet_properties.tabColor = NAVY
wb.active = wb["README"]
wb.save(OUT)
print("saved", OUT)
