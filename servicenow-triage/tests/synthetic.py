"""Fake ServiceNow incidents with the same columns as the real export, used by
the tests and to try the pipeline without real data.

    python tests/synthetic.py data/synthetic.csv 4000
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

CIS = {
    "S.A.MS-OUTLOOK": ["outlook not opening", "cannot send email", "mailbox full", "calendar not syncing"],
    "S.A.MY-LEARNING-ZONE": ["my learning zone course not loading", "mlz certificate missing", "training module stuck"],
    "S.A.ASCONNECT-3.0": ["asconnect login failed", "as connect page error", "asconnect report blank"],
    "S.A.RESCONNECT": ["resconnect booking screen frozen", "res connect timeout", "resconnect pnr display issue"],
    "S.A.MYBC": ["mybc payslip not visible", "mybc leave request error"],
    "S.A.SITA-WORLDTRACER": ["worldtracer bag file not found"],
}
GROUPS = {
    "S.A.MS-OUTLOOK": ["IMG-SMC-TCC-ITSC", "IMG-SMC-TCC-L1-Engineers"],
    "S.A.MY-LEARNING-ZONE": ["IMG-SMC-TCC-ITSC", "LMS-Support"],
    "S.A.ASCONNECT-3.0": ["BSO-ENG&ERP"],
    "S.A.RESCONNECT": ["RES-Apps"],
    "S.A.MYBC": ["BSO-ENG&ERP", "IMG-SMC-TCC-ITSC"],
    "S.A.SITA-WORLDTRACER": ["Airport-Apps"],
}
WEIGHTS = np.array([0.35, 0.18, 0.17, 0.15, 0.12, 0.03])
NOISE = [
    "", " Please help urgently.", " Kind regards,\nJohn\nEmployee 4512345",
    " Contact me on +971 4 123 4567 or john.doe@example.com", " <p>Screenshot attached</p>",
    "\n-----Original Message-----\nFrom: someone else about another topic",
]


def make(n: int = 4000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2025-01-01")
    opened = start + pd.to_timedelta(np.sort(rng.uniform(0, 590, n)), unit="D")
    cis = rng.choice(list(CIS), size=n, p=WEIGHTS)
    rows = []
    for i, (ci, ts) in enumerate(zip(cis, opened)):
        short = rng.choice(CIS[ci])
        urgent = rng.random() < 0.06
        if urgent:
            short = f"{short} - all users down outage"
        group = rng.choice(GROUPS[ci]) if rng.random() < 0.98 else "Tiny-Team-" + str(rng.integers(0, 5))
        impact = "1 - High" if urgent else rng.choice(["2 - Medium", "3 - Low"], p=[0.3, 0.7])
        prio = rng.choice(["1 - Critical", "2 - High"]) if urgent else rng.choice(["3 - Moderate", "4 - Low", "5 - Planning"], p=[0.4, 0.5, 0.1])
        rows.append({
            "number": f"INC{1000000 + i}",
            "opened_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "short_description": short,
            "description": f"User reports {short}.{rng.choice(NOISE)} Ref PNR AB12CD.",
            "contact_type": rng.choice(["Email", "Phone", "Self-service"]),
            "caller_id": f"user{rng.integers(0, 300)}",
            "sys_created_by": rng.choice(["svc_email", "agent1", "agent2"]),
            "impact": impact,
            "cmdb_ci": ci,
            "assignment_group": group,
            "category": "Software",
            "subcategory": rng.choice(["Email", "Application", "Access"]),
            "priority": prio,
            "state": rng.choice(["Closed", "Resolved", "Cancelled"], p=[0.7, 0.28, 0.02]),
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/synthetic.csv"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4000
    from pathlib import Path
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    make(n).to_csv(out, index=False)
    print(f"{n} synthetic incidents -> {out}")
