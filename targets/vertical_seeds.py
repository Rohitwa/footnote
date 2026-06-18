"""Structured business-vertical seeds — per-vertical revenue / PAT / users,
nested competitor benchmarks, and department-wise headcount.

Idempotent: seeded once per company (skipped if verticals already exist).
All figures sourced from the long-form research in research_seeds.py — no
invented numbers. Where a competitor figure is not reliably public, their_value
is left None and renders as "—".

Shape:
    VERTICAL_SEEDS = {
        "<company_id>": {
            "verticals": [
                {name, revenue, pat, active_users, note, status,
                 benchmarks: [{competitor_name, metric, our_value, their_value}],
                 headcount:  [{department, headcount, entity}]},
                ...
            ],
            "group_headcount": [{department, headcount, entity}],  # vertical_id = NULL
        }
    }
"""

VERTICAL_SEEDS = {
    "ht-media": {
        "verticals": [
            {
                "name": "English Print (HT + Mint)",
                "revenue": "₹1,500 cr (group print, +8%)",
                "pat": "Profitable (standalone parent)",
                "active_users": "HT north-India circ. leader; Mint Premium paywall",
                "note": "Cash cow — funds everything. Circulation in ~2%/yr slow decline.",
                "status": "healthy",
                "benchmarks": [
                    {"competitor_name": "DB Corp (Dainik Bhaskar)", "metric": "English print ad rev FY26", "our_value": "₹644 cr (+8%)", "their_value": None},
                    {"competitor_name": "DB Corp (Dainik Bhaskar)", "metric": "Group circulation rev", "our_value": "₹208 cr (−1.4%)", "their_value": None},
                ],
            },
            {
                "name": "Hindi Print (Hindustan / HMVL)",
                "revenue": "₹739.64 cr (+9.9%)",
                "pat": "₹48.69 cr (−37%, OTTplay drag)",
                "active_users": "2nd-largest Hindi daily (Bihar, JH, UP, UK, NCR)",
                "note": "Operationally healthy — Q4 OPM 30.32% is an 8-quarter high. PAT fell only because of OTTplay.",
                "status": "healthy",
                "benchmarks": [
                    {"competitor_name": "Jagran Prakashan (Dainik Jagran)", "metric": "Revenue FY26", "our_value": "₹739.64 cr", "their_value": None},
                    {"competitor_name": "Jagran Prakashan (Dainik Jagran)", "metric": "Q4 op margin", "our_value": "30.32% (8Q high)", "their_value": None},
                    {"competitor_name": "Jagran Prakashan (Dainik Jagran)", "metric": "Hindi print ad rev FY26", "our_value": "₹504 cr (+8%)", "their_value": None},
                ],
            },
            {
                "name": "Radio (Fever / Radio Nasha / Radio One)",
                "revenue": "₹140 cr (−32%)",
                "pat": "EBITDA −₹22 cr",
                "active_users": "Tier-2/3 footprint retained; 4 metro licenses surrendered",
                "note": "Cost rationalisation by exit, not turnaround. 4 metro licenses surrendered eff. Jun 15 2026.",
                "status": "declining",
                "benchmarks": [
                    {"competitor_name": "ENIL (Radio Mirchi)", "metric": "Radio revenue FY26", "our_value": "₹140 cr (−32%)", "their_value": None},
                    {"competitor_name": "Music Broadcast (Radio City)", "metric": "Radio EBITDA", "our_value": "−₹22 cr", "their_value": None},
                ],
            },
            {
                "name": "Digital (HT / Mint / VCCircle / Smartcast)",
                "revenue": "₹155 cr (+2%)",
                "pat": "EBITDA −₹8 cr (−5% margin)",
                "active_users": "hindustantimes.com top-3 India English news; livemint.com biz leader",
                "note": "The strategic puzzle — ₹945 cr net cash idle, no announced capital-deployment thesis. Paywall not yet scaling.",
                "status": "loss",
                "benchmarks": [
                    {"competitor_name": "Network18 / Times Internet", "metric": "Digital revenue FY26", "our_value": "₹155 cr (+2%)", "their_value": None},
                    {"competitor_name": "Network18 / Times Internet", "metric": "Digital EBITDA margin", "our_value": "−5%", "their_value": None},
                ],
            },
            {
                "name": "Shine.com (Jobs portal)",
                "revenue": "Not broken out (₹70–120 cr range, unverified)",
                "pat": "Undisclosed",
                "active_users": "1,068 employees (Apr 30 2026, LinkedIn-confirmed)",
                "note": "Under-monetized vs Naukri/LinkedIn. Possible standalone listing candidate. Un-discussed in mgmt commentary.",
                "status": "loss",
                "benchmarks": [
                    {"competitor_name": "Info Edge (Naukri)", "metric": "Headcount", "our_value": "1,068", "their_value": None},
                    {"competitor_name": "Info Edge (Naukri)", "metric": "Segment revenue disclosed?", "our_value": "No (bundled)", "their_value": "Yes"},
                ],
                "headcount": [
                    {"department": "Shine.com", "headcount": 1068, "entity": "self"},
                ],
            },
            {
                "name": "OTTplay (killed Mar 2026)",
                "revenue": "₹96.64 cr (discontinued)",
                "pat": "−₹100.96 cr loss before tax",
                "active_users": "Aggregator model; net worth −₹38.09 cr at shutdown",
                "note": "Single biggest drag on HMVL consol PAT. Board: profitability timeline missed criteria. Honest write-off.",
                "status": "killed",
                "benchmarks": [],
            },
        ],
        # Group-level department (business-line) distribution — HT Media only.
        # Competitor dept splits not reliably public, so seeded as 'self'.
        "group_headcount": [
            {"department": "Print (HT+Mint+Hindustan)", "headcount": 4800, "entity": "self"},
            {"department": "Shine.com / HT Education", "headcount": 1068, "entity": "self"},
            {"department": "Digital", "headcount": 700, "entity": "self"},
            {"department": "Radio (pre-exit)", "headcount": 500, "entity": "self"},
            {"department": "Corporate / shared", "headcount": 400, "entity": "self"},
            {"department": "Bridge School", "headcount": 90, "entity": "self"},
        ],
    },
}
