"""
Modeled deal metrics for every listing, using industry averages when the seller
does not publish their own figures. Rules (per CJ):

  SDE:
    - if the listing shows its own SDE / cash flow, use that
    - else if revenue < 250k  -> SDE = revenue * 90%   (small firms run very lean)
    - else                    -> SDE = revenue * 45%

  Debt service = revenue * 15.5%   (annual)

  Owner comp add-back:
    - revenue < 250k -> 25k   (a small firm cannot carry a full 75k salary)
    - revenue >= 250k -> 75k

  DSCR = (SDE - owner_comp) / debt_service     (both tiers net owner comp)
  Monthly cash flow = (SDE - debt_service - owner_comp) / 12
  DSCR pass threshold = 1.5

All outputs are modeled estimates unless SDE came straight from the listing.
"""
from typing import Optional, Dict

SMALL_FIRM_CEILING = 250_000
SMALL_MARGIN = 0.90
LARGE_MARGIN = 0.45
DEBT_SERVICE_RATE = 0.155
SMALL_OWNER_COMP = 25_000
LARGE_OWNER_COMP = 75_000
DSCR_THRESHOLD = 1.5


def compute_metrics(revenue: Optional[float],
                    sde: Optional[float] = None) -> Dict:
    """
    Return modeled metrics. Needs a revenue figure (or a given SDE) to do anything.
    Returns a dict with sde, sde_is_modeled, debt_service, owner_comp, dscr,
    monthly_cash_flow, dscr_pass, and metrics_basis. Empty dict if nothing to model.
    """
    rev = float(revenue) if revenue else None
    given_sde = float(sde) if sde else None

    if rev is None and given_sde is None:
        return {}

    small = (rev is not None and rev < SMALL_FIRM_CEILING)
    owner_comp = SMALL_OWNER_COMP if small else LARGE_OWNER_COMP

    # SDE: prefer the listing's own number; otherwise model it off revenue.
    if given_sde is not None:
        model_sde = given_sde
        sde_is_modeled = False
    elif rev is not None:
        model_sde = rev * (SMALL_MARGIN if small else LARGE_MARGIN)
        sde_is_modeled = True
    else:
        return {}

    # Debt service needs a revenue figure. If we only have a given SDE and no
    # revenue, we cannot size debt service, so report SDE only.
    if rev is None:
        return {
            "sde": round(model_sde),
            "sde_is_modeled": sde_is_modeled,
            "debt_service": None,
            "owner_comp": owner_comp,
            "dscr": None,
            "monthly_cash_flow": None,
            "dscr_pass": None,
            "metrics_basis": "sde_only_no_revenue",
        }

    debt_service = rev * DEBT_SERVICE_RATE
    dscr = (model_sde - owner_comp) / debt_service if debt_service else None
    monthly_cf = (model_sde - debt_service - owner_comp) / 12.0

    return {
        "sde": round(model_sde),
        "sde_is_modeled": sde_is_modeled,
        "debt_service": round(debt_service),
        "owner_comp": owner_comp,
        "dscr": round(dscr, 2) if dscr is not None else None,
        "monthly_cash_flow": round(monthly_cf),
        "dscr_pass": (dscr >= DSCR_THRESHOLD) if dscr is not None else None,
        "metrics_basis": ("listing_sde" if not sde_is_modeled
                          else "modeled_small" if small else "modeled_large"),
    }
