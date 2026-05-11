"""
U.S. federal tax estimate for Married Filing Jointly (MFJ), tax year 2025.

Ordinary income tax uses official MFJ rate *buckets* (progressive brackets).
Child Tax Credit (CTC) follows MAGI phase-out rules, then non-refundable vs
refundable Additional CTC (ACTC) using the standard 15% × (earned income − $2,500)
formula capped per child (Schedule 8812 style, simplified).

Earned Income Credit (EITC) for MFJ uses the IRS Publication 596 (2025) EIC
Table MFJ columns when the lookup amount falls on a published $50 row; in the
few gaps in the scraped table, falls back to the Tax-Calculator piecewise
parameters for tax year 2025 (same maximum credits and phase-in/out structure
as current law). Lookup amount is min(earned income, AGI), matching Form 1040
EIC worksheets when those lines differ.

Investment income is collected because Pub. 596 Rule 6 denies any EITC when
investment income exceeds the annual threshold (2025: $11,950), even if earned
income is low. Enter a rough total of items that count for that test (e.g.
taxable interest, ordinary dividends); the model does not reconstruct every
Form 1040 line.

Not tax advice: no state tax, AMT, QBI, premium tax credits, Form 8812 line caps
from SS/Medicare taxes, prior-year earned income election, etc.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass

if sys.version_info < (3, 7):
    sys.exit("Python 3.7+ required (dataclasses). Try: /usr/bin/python3 mfj_tax.py …")

try:
    from eitc_mfj_2025_rows import EITC_MFJ_ROWS
except ImportError:  # pragma: no cover
    EITC_MFJ_ROWS = []

# ----- Tax year 2025 (Rev. Proc. 2024-40 / Pub. 501 / Pub. 596) -----

STANDARD_DEDUCTION_MFJ = 31_500.0

BRACKETS_MFJ: list[tuple[float, float]] = [
    (0.10, 23_850.0),
    (0.12, 96_950.0),
    (0.22, 206_700.0),
    (0.24, 394_600.0),
    (0.32, 501_050.0),
    (0.35, 751_600.0),
    (0.37, float("inf")),
]

SOCIAL_SECURITY_WAGE_BASE = 176_100.0
SE_NET_PERCENT = 0.9235
SOCIAL_SECURITY_RATE = 0.124
MEDICARE_RATE = 0.029
ADDITIONAL_MEDICARE_RATE = 0.009
ADDITIONAL_MEDICARE_MFJ_THRESHOLD = 250_000.0

# Child tax credit (per qualifying child under 17); MAGI phase-out §24(h)(5)
CTC_PER_CHILD = 2_000.0
CTC_MFSI_PHASEOUT_START_MFJ = 400_000.0
CTC_MFSI_PHASEOUT_PER_1K = 50.0

# Additional child tax credit (refundable slice of CTC)
ACTC_PER_CHILD_CAP = 1_700.0
ACTC_EARNED_INCOME_EXCESS_RATE = 0.15
ACTC_EARNED_INCOME_THRESHOLD = 2_500.0

# EITC — MFJ "less than" AGI / earned caps (IRS EITC tables, TY 2025)
EITC_MFJ_MAX_AGI_OR_EARNED = (26_214, 57_554, 64_430, 68_675)
EITC_INVESTMENT_INCOME_LIMIT = 11_950.0

# Max grid points when scanning gig deduction for min/max net federal
_NET_SCAN_MAX_POINTS = 25_001

# Tax-Calculator policy_current_law.json TY 2025 (fallback / gap fill)
_EITC_C = (649.0, 4_328.0, 7_152.0, 8_046.0)
_EITC_RT = (0.0765, 0.34, 0.4, 0.45)
_EITC_PS = (10_620.0, 23_350.0, 23_350.0, 23_350.0)
_EITC_PS_ADDON_MFJ = (7_110.0, 7_120.0, 7_120.0, 7_120.0)
_EITC_PRT = (0.0765, 0.1598, 0.2106, 0.2106)


@dataclass(frozen=True)
class OrdinaryTaxBracketSlice:
    rate: float
    taxable_from: float
    taxable_to: float
    taxable_in_bracket: float
    tax_from_bracket: float


def _ordinary_income_tax_detail(
    taxable: float, brackets: list[tuple[float, float]]
) -> tuple[float, tuple[OrdinaryTaxBracketSlice, ...]]:
    """Progressive ordinary tax; returns (total, per-bracket slices)."""
    if taxable <= 0:
        return 0.0, ()
    tax = 0.0
    lower = 0.0
    slices: list[OrdinaryTaxBracketSlice] = []
    for rate, upper in brackets:
        if taxable <= lower:
            break
        slice_top = min(taxable, upper)
        width = slice_top - lower
        piece = round(rate * width, 2)
        tax += piece
        slices.append(
            OrdinaryTaxBracketSlice(
                rate=rate,
                taxable_from=lower,
                taxable_to=slice_top,
                taxable_in_bracket=width,
                tax_from_bracket=piece,
            )
        )
        lower = upper
    return round(tax, 2), tuple(slices)


def _tax_from_brackets(taxable: float, brackets: list[tuple[float, float]]) -> float:
    t, _ = _ordinary_income_tax_detail(taxable, brackets)
    return t


def _additional_medicare_se(
    w2_wages: float, schedule_c_net: float, se_base: float
) -> float:
    combined = max(0.0, w2_wages) + max(0.0, schedule_c_net)
    excess = max(0.0, combined - ADDITIONAL_MEDICARE_MFJ_THRESHOLD)
    if excess <= 0:
        return 0.0
    base = min(se_base, excess)
    return round(base * ADDITIONAL_MEDICARE_RATE, 2)


def _self_employment_tax(
    w2_wages: float, schedule_c_net: float
) -> tuple[float, float]:
    net = max(0.0, schedule_c_net)
    se_base = net * SE_NET_PERCENT
    ss_cap_room = max(0.0, SOCIAL_SECURITY_WAGE_BASE - max(0.0, w2_wages))
    ss_part = min(se_base, ss_cap_room) * SOCIAL_SECURITY_RATE
    medicare_part = se_base * MEDICARE_RATE
    add_medicare = _additional_medicare_se(w2_wages, net, se_base)
    se_tax = round(ss_part + medicare_part + add_medicare, 2)
    return se_tax, round(0.5 * se_tax, 2)


def _ctc_tentative_after_magi_phaseout(magi: float, num_children: int) -> float:
    if num_children <= 0:
        return 0.0
    raw = num_children * CTC_PER_CHILD
    if magi <= CTC_MFSI_PHASEOUT_START_MFJ:
        return raw
    over = magi - CTC_MFSI_PHASEOUT_START_MFJ
    reduction = math.ceil(over / 1000.0) * CTC_MFSI_PHASEOUT_PER_1K
    return max(0.0, raw - reduction)


def _eitc_parametric_mfj(lookup: float, n_eitc: int) -> float:
    """Piecewise EITC (Tax-Calculator style) for MFJ; lookup = min(EI, AGI)."""
    i = min(3, max(0, n_eitc))
    rt, prt, mx = _EITC_RT[i], _EITC_PRT[i], _EITC_C[i]
    po = _EITC_PS[i] + _EITC_PS_ADDON_MFJ[i]
    pre = min(rt * lookup, mx)
    if lookup > po:
        phased = max(0.0, mx - prt * (lookup - po))
        return min(pre, phased)
    return pre


def _eitc_from_pub596_table(lookup: int, n_eitc: int) -> float | None:
    if not EITC_MFJ_ROWS:
        return None
    col = min(3, max(0, n_eitc))
    for lo, hi, m0, m1, m2, m3 in EITC_MFJ_ROWS:
        if lo <= lookup < hi:
            return float((m0, m1, m2, m3)[col])
    return None


def _eitc_mfj(
    earned_income: float,
    agi: float,
    n_eitc: int,
    investment_income: float,
    age_head: int,
    age_spouse: int,
) -> float:
    if n_eitc < 0 or n_eitc > 3:
        return 0.0
    if investment_income > EITC_INVESTMENT_INCOME_LIMIT + 1e-9:
        return 0.0
    lim = EITC_MFJ_MAX_AGI_OR_EARNED[min(3, n_eitc)]
    if earned_income >= lim or agi >= lim:
        return 0.0
    if earned_income <= 0:
        return 0.0
    if n_eitc == 0:
        if not ((25 <= age_head < 65) or (25 <= age_spouse < 65)):
            return 0.0

    lu = int(math.floor(min(earned_income, agi)))
    tbl = _eitc_from_pub596_table(lu, n_eitc)
    if tbl is not None:
        return tbl
    return float(round(_eitc_parametric_mfj(float(lu), n_eitc), 0))


def _actc(
    tentative_ctc: float,
    income_tax_before_nonrefundable: float,
    earned_income: float,
    num_children: int,
) -> tuple[float, float]:
    """
    Returns (non-refundable CTC applied against income tax, ACTC refundable).
    Simplified: no other non-refundable credits competing on Form 1040.
    """
    if num_children <= 0 or tentative_ctc <= 0:
        return 0.0, 0.0
    nonref = min(tentative_ctc, income_tax_before_nonrefundable)
    unused = max(0.0, tentative_ctc - nonref)
    if unused <= 0:
        return nonref, 0.0
    earned_pool = ACTC_EARNED_INCOME_EXCESS_RATE * max(
        0.0, earned_income - ACTC_EARNED_INCOME_THRESHOLD
    )
    cap = num_children * ACTC_PER_CHILD_CAP
    actc = min(unused, earned_pool, cap)
    return nonref, round(max(0.0, actc), 2)


@dataclass
class MFJTaxResult:
    schedule_c_net: float
    earned_income: float
    self_employment_tax: float
    half_se_tax_deduction: float
    agi: float
    taxable_income: float
    ordinary_income_tax_before_credits: float
    ordinary_tax_brackets: tuple[OrdinaryTaxBracketSlice, ...]
    ctc_tentative: float
    ctc_nonrefundable: float
    additional_child_tax_credit: float
    eitc: float
    investment_income: float
    income_tax_after_ctc_nonrefundable: float
    net_federal_after_refundable_credits: float

    def summary(self) -> str:
        lines = [
            "Tax year: 2025 (federal MFJ)",
            f"Schedule C net (gig gross − gig deduction): ${self.schedule_c_net:,.2f}",
            f"Earned income (W-2 + Sch. C net, for EITC/ACTC): ${self.earned_income:,.2f}",
            f"Investment income (input): ${self.investment_income:,.2f}",
            f"Self-employment tax: ${self.self_employment_tax:,.2f}",
            f"½ SE tax (deductible for AGI): ${self.half_se_tax_deduction:,.2f}",
            f"AGI (approx.): ${self.agi:,.2f}",
            f"Taxable income (std. deduction MFJ): ${self.taxable_income:,.2f}",
            "",
            "Ordinary income tax by bracket (MFJ 2025):",
        ]
        for s in self.ordinary_tax_brackets:
            pct = int(round(100 * s.rate))
            lines.append(
                f"  {pct}% on ${s.taxable_in_bracket:,.2f} "
                f"(taxable ${s.taxable_from:,.2f}–${s.taxable_to:,.2f}): "
                f"${s.tax_from_bracket:,.2f}"
            )
        if not self.ordinary_tax_brackets:
            lines.append("  (no positive taxable income in a taxed bracket)")
        lines += [
            "",
            f"Ordinary income tax (sum of brackets): ${self.ordinary_income_tax_before_credits:,.2f}",
            f"CTC tentative (after MAGI phase-out): ${self.ctc_tentative:,.2f}",
            f"CTC non-refundable (applied to income tax): ${self.ctc_nonrefundable:,.2f}",
            f"ACTC (refundable remainder of CTC): ${self.additional_child_tax_credit:,.2f}",
            f"EITC (MFJ; Pub.596 table + gap fill): ${self.eitc:,.2f}",
            f"Income tax after non-refundable CTC: ${self.income_tax_after_ctc_nonrefundable:,.2f}",
            "",
            "Net federal (income tax after non-refundable CTC + SE tax "
            "− ACTC − EITC; negative means net refundable): "
            f"${self.net_federal_after_refundable_credits:,.2f}",
        ]
        return "\n".join(lines)


def compute_mfj_2025(
    w2: float,
    gig_gross: float,
    qualifying_children: int,
    gig_deduction: float,
    *,
    investment_income: float = 0.0,
    age_head: int = 35,
    age_spouse: int = 35,
) -> MFJTaxResult:
    w2 = max(0.0, float(w2))
    gig_gross = max(0.0, float(gig_gross))
    gig_deduction = max(0.0, float(gig_deduction))
    qc = max(0, int(qualifying_children))
    inv = max(0.0, float(investment_income))

    schedule_c_net = max(0.0, gig_gross - gig_deduction)
    earned_income = w2 + schedule_c_net

    se_tax, half_se = _self_employment_tax(w2, schedule_c_net)
    agi = w2 + schedule_c_net - half_se
    taxable = max(0.0, agi - STANDARD_DEDUCTION_MFJ)

    ord_tax, bracket_slices = _ordinary_income_tax_detail(taxable, BRACKETS_MFJ)

    ctc_tent = _ctc_tentative_after_magi_phaseout(agi, qc)
    ctc_nonref, actc = _actc(ctc_tent, ord_tax, earned_income, qc)

    n_eitc = min(3, qc)
    eitc_amt = _eitc_mfj(earned_income, agi, n_eitc, inv, age_head, age_spouse)

    inc_after_nonref = max(0.0, round(ord_tax - ctc_nonref, 2))
    net = round(inc_after_nonref + se_tax - actc - eitc_amt, 2)

    return MFJTaxResult(
        schedule_c_net=schedule_c_net,
        earned_income=earned_income,
        self_employment_tax=se_tax,
        half_se_tax_deduction=half_se,
        agi=agi,
        taxable_income=taxable,
        ordinary_income_tax_before_credits=ord_tax,
        ordinary_tax_brackets=bracket_slices,
        ctc_tentative=round(ctc_tent, 2),
        ctc_nonrefundable=round(ctc_nonref, 2),
        additional_child_tax_credit=actc,
        eitc=round(eitc_amt, 2),
        investment_income=inv,
        income_tax_after_ctc_nonrefundable=inc_after_nonref,
        net_federal_after_refundable_credits=net,
    )


@dataclass(frozen=True)
class NetFederalGigDeductionSweep:
    """Min/max net federal as gig_deduction runs from 0 through gig gross (Schedule C net ≥ 0)."""

    gig_gross: float
    min_net_federal: float
    max_net_federal: float
    gig_deduction_at_min: float
    gig_deduction_at_max: float
    sample_count: int


def _gig_deduction_sample_points(gig_gross: float) -> list[float]:
    """Deductions from $0 through gross (whole-dollar grid; includes exact gross if non-integer)."""
    g = max(0.0, float(gig_gross))
    if g <= 0:
        return [0.0]
    whole = int(math.floor(g + 1e-9))
    n_needed = whole + 1
    if n_needed <= _NET_SCAN_MAX_POINTS:
        pts = [float(i) for i in range(0, whole + 1)]
    else:
        step = max(1, int(math.ceil(n_needed / _NET_SCAN_MAX_POINTS)))
        pts = [float(p) for p in sorted(set(range(0, whole + 1, step)) | {whole})]
    if g > whole + 1e-6:
        pts.append(g)
        pts.sort()
    return pts


def print_net_federal_gig_deduction_sweep(
    w2: float,
    gig_gross: float,
    qualifying_children: int,
    *,
    investment_income: float = 0.0,
    age_head: int = 35,
    age_spouse: int = 35,
) -> NetFederalGigDeductionSweep:
    """Prints min/max net federal over gig deductions and two endpoint summaries."""
    sw = net_federal_range_for_gig_deduction(
        w2,
        gig_gross,
        qualifying_children,
        investment_income=investment_income,
        age_head=age_head,
        age_spouse=age_spouse,
    )
    print(
        "—— 在「Gig 可扣除费用」从 $0 到 "
        f"${sw.gig_gross:,.2f}（Schedule C 净利从毛利降到 $0）时 ——\n"
        f"Net federal 最低: ${sw.min_net_federal:,.2f}  "
        f"（约在可扣除费用 = ${sw.gig_deduction_at_min:,.2f} 时）\n"
        f"Net federal 最高: ${sw.max_net_federal:,.2f}  "
        f"（约在可扣除费用 = ${sw.gig_deduction_at_max:,.2f} 时）\n"
        f"（扫描了 {sw.sample_count} 个扣除额样本；"
        "EITC 等会使净额不一定随扣除单调变化。）\n"
    )
    whole = int(math.floor(max(0.0, gig_gross) + 1e-9))
    if sw.sample_count < whole + 1:
        print(
            "提示：毛利较大时为控制耗时使用了稀疏网格，极值可能与逐元扫描差几美元；"
            "需要更细可把毛利拆小多次估算。\n"
        )
    print("—— 端点明细：扣除 = $0 ——\n")
    print(
        compute_mfj_2025(
            w2,
            gig_gross,
            qualifying_children,
            0.0,
            investment_income=investment_income,
            age_head=age_head,
            age_spouse=age_spouse,
        ).summary()
    )
    print("\n—— 端点明细：扣除 = 毛利（净利 $0）——\n")
    print(
        compute_mfj_2025(
            w2,
            gig_gross,
            qualifying_children,
            sw.gig_gross,
            investment_income=investment_income,
            age_head=age_head,
            age_spouse=age_spouse,
        ).summary()
    )
    print(
        "\n—— 极小值明细（Net federal min）——\n"
        f"扣除≈${sw.gig_deduction_at_min:,.2f}，Net federal={sw.min_net_federal:,.2f}\n"
    )
    print(
        compute_mfj_2025(
            w2,
            gig_gross,
            qualifying_children,
            sw.gig_deduction_at_min,
            investment_income=investment_income,
            age_head=age_head,
            age_spouse=age_spouse,
        ).summary()
    )
    print(
        "\n—— 极大值明细（Net federal max）——\n"
        f"扣除≈${sw.gig_deduction_at_max:,.2f}，Net federal={sw.max_net_federal:,.2f}\n"
    )
    print(
        compute_mfj_2025(
            w2,
            gig_gross,
            qualifying_children,
            sw.gig_deduction_at_max,
            investment_income=investment_income,
            age_head=age_head,
            age_spouse=age_spouse,
        ).summary()
    )
    print(
        "\n区间汇总（Net federal）："
        f" 最低 ${sw.min_net_federal:,.2f} "
        f"（扣除≈${sw.gig_deduction_at_min:,.2f}），"
        f" 最高 ${sw.max_net_federal:,.2f} "
        f"（扣除≈${sw.gig_deduction_at_max:,.2f}）。"
    )
    return sw


def net_federal_range_for_gig_deduction(
    w2: float,
    gig_gross: float,
    qualifying_children: int,
    *,
    investment_income: float = 0.0,
    age_head: int = 35,
    age_spouse: int = 35,
) -> NetFederalGigDeductionSweep:
    """
    For fixed W-2, gig gross, and dependents, sweep gig_deduction from $0 up to
    gross (so Schedule C net runs from gross down to 0). Returns min/max net
    federal; EITC can make the relationship non-monotonic, so we scan samples.
    """
    g = max(0.0, float(gig_gross))
    pts = _gig_deduction_sample_points(g)
    min_net = float("inf")
    max_net = float("-inf")
    d_min = 0.0
    d_max = 0.0
    for d in pts:
        r = compute_mfj_2025(
            w2,
            g,
            qualifying_children,
            d,
            investment_income=investment_income,
            age_head=age_head,
            age_spouse=age_spouse,
        )
        n = r.net_federal_after_refundable_credits
        if n < min_net:
            min_net = n
            d_min = d
        if n > max_net:
            max_net = n
            d_max = d
    return NetFederalGigDeductionSweep(
        gig_gross=g,
        min_net_federal=round(min_net, 2),
        max_net_federal=round(max_net, 2),
        gig_deduction_at_min=d_min,
        gig_deduction_at_max=d_max,
        sample_count=len(pts),
    )


def _prompt_float(label: str, default: float = 0.0) -> float:
    suf = f"（直接回车 = {default:g}）" if default != 0.0 else "（直接回车 = 0）"
    s = input(f"{label}{suf}: ").strip().replace(",", "")
    if not s:
        return float(default)
    return float(s)


def _prompt_int(label: str, default: int = 0) -> int:
    suf = f"（直接回车 = {default}）"
    s = input(f"{label}{suf}: ").strip()
    if not s:
        return int(default)
    return int(s)


def _prompt_gig_deduction_or_range(gig_gross: float) -> float | None:
    """
    None = user skipped → show net federal range over deductions.
    Otherwise a non-negative float.
    """
    print(
        "Gig / 自雇 可扣除费用：输入数字；若还不确定，请直接回车。\n"
        "  （回车将扫描「扣除从 $0 到 $毛利」并给出 Net federal 的最低～最高；\n"
        "   毛利为 0 时回车与填 0 相同。）"
    )
    s = input("> ").strip().replace(",", "")
    if not s:
        return None
    v = float(s)
    return max(0.0, v)


def _run_interactive() -> None:
    print("2025 联邦税 MFJ 估算 — 按提示输入数字，回车使用括号内默认值。\n")
    w2 = _prompt_float("W-2 工资（约等于 Box 1）", 0.0)
    gig_gross = _prompt_float("Gig / 自雇 总收入（毛利）", 0.0)
    gig_deduction = _prompt_gig_deduction_or_range(gig_gross)
    kids = _prompt_int("符合 CTC/EITC 的子女数量（0–多名，EITC 按 3 名以上封顶档）", 0)
    print(
        "\n投资所得（与 EITC 有关）：若全年「投资所得」超过 "
        f"${EITC_INVESTMENT_INCOME_LIMIT:,.0f}（2025 Pub.596 Rule 6），"
        "则完全不能拿 EITC。此处可填利息、普通股息等简化合计；没有就回车 0。\n"
    )
    inv = _prompt_float("投资所得", 0.0)
    age_h = _prompt_int("本人年龄（无子女 EITC 需 25–64；有子女可随意填）", 35)
    age_s = _prompt_int("配偶年龄", 35)
    print()

    if gig_deduction is None:
        if gig_gross <= 0:
            gig_deduction = 0.0
            r = compute_mfj_2025(
                w2,
                gig_gross,
                kids,
                gig_deduction,
                investment_income=inv,
                age_head=age_h,
                age_spouse=age_s,
            )
            print(r.summary())
            return
        print_net_federal_gig_deduction_sweep(
            w2, gig_gross, kids, investment_income=inv, age_head=age_h, age_spouse=age_s
        )
        return

    r = compute_mfj_2025(
        w2,
        gig_gross,
        kids,
        gig_deduction,
        investment_income=inv,
        age_head=age_h,
        age_spouse=age_s,
    )
    print(r.summary())


if __name__ == "__main__":
    import argparse

    if len(sys.argv) <= 1:
        _run_interactive()
    else:
        p = argparse.ArgumentParser(description="MFJ 2025 federal tax estimate.")
        p.add_argument("--w2", type=float, required=True, help="W-2 wages (approx. box 1)")
        p.add_argument("--gig-gross", type=float, default=0.0, dest="gig_gross")
        p.add_argument(
            "--gig-deduction", type=float, default=0.0, dest="gig_deduction"
        )
        p.add_argument("--kids", type=int, default=0, dest="qualifying_children")
        p.add_argument(
            "--investment-income",
            type=float,
            default=0.0,
            dest="investment_income",
            help="Investment income for EITC limit (Form 1040 definition simplified)",
        )
        p.add_argument("--age-head", type=int, default=35, dest="age_head")
        p.add_argument("--age-spouse", type=int, default=35, dest="age_spouse")
        p.add_argument(
            "--gig-deduction-range",
            action="store_true",
            dest="gig_deduction_range",
            help="Sweep gig deduction from 0 to gross; print net federal min/max and endpoints",
        )
        args = p.parse_args()

        if args.gig_deduction_range:
            if args.gig_gross <= 0:
                r = compute_mfj_2025(
                    args.w2,
                    args.gig_gross,
                    args.qualifying_children,
                    0.0,
                    investment_income=args.investment_income,
                    age_head=args.age_head,
                    age_spouse=args.age_spouse,
                )
                print(r.summary())
            else:
                print_net_federal_gig_deduction_sweep(
                    args.w2,
                    args.gig_gross,
                    args.qualifying_children,
                    investment_income=args.investment_income,
                    age_head=args.age_head,
                    age_spouse=args.age_spouse,
                )
        else:
            r = compute_mfj_2025(
                args.w2,
                args.gig_gross,
                args.qualifying_children,
                args.gig_deduction,
                investment_income=args.investment_income,
                age_head=args.age_head,
                age_spouse=args.age_spouse,
            )
            print(r.summary())
