"""
Local web UI for MFJ tax curve: x = gig deductible, y = net federal.

Run from this directory:

  pip install -r requirements.txt
  python web_app.py

Then open http://127.0.0.1:5000/
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from mfj_tax import compute_mfj_2025, net_federal_vs_gig_deduction_curve

app = Flask(__name__)


def _parse_num(name: str, default: float = 0.0) -> float:
    v = request.args.get(name)
    if v is None or v == "":
        return float(default)
    return float(str(v).replace(",", ""))


def _parse_int(name: str, default: int = 0) -> int:
    v = request.args.get(name)
    if v is None or v == "":
        return int(default)
    return int(v)


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/curve")
def api_curve():
    if not request.is_json:
        return jsonify({"error": "JSON body expected"}), 400
    body = request.get_json(silent=True) or {}
    try:
        w2 = float(str(body.get("w2", 0)).replace(",", ""))
        gig_gross = float(str(body.get("gig_gross", 0)).replace(",", ""))
        gig_deduction = float(str(body.get("gig_deduction", 0)).replace(",", ""))
        kids = int(body.get("kids", 0))
        inv = float(str(body.get("investment_income", 0)).replace(",", ""))
        age_head = int(body.get("age_head", 35))
        age_spouse = int(body.get("age_spouse", 35))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid numeric input"}), 400

    w2 = max(0.0, w2)
    gig_gross = max(0.0, gig_gross)
    gig_deduction = max(0.0, gig_deduction)
    kids = max(0, kids)
    inv = max(0.0, inv)

    snapshot = compute_mfj_2025(
        w2,
        gig_gross,
        kids,
        gig_deduction,
        investment_income=inv,
        age_head=age_head,
        age_spouse=age_spouse,
    )

    if gig_gross <= 0:
        points = [
            {
                "deductible": 0.0,
                "net_federal": snapshot.net_federal_after_refundable_credits,
            }
        ]
    else:
        points = net_federal_vs_gig_deduction_curve(
            w2,
            gig_gross,
            kids,
            investment_income=inv,
            age_head=age_head,
            age_spouse=age_spouse,
        )

    return jsonify(
        {
            "points": points,
            "snapshot_net_federal": snapshot.net_federal_after_refundable_credits,
            "snapshot_summary": snapshot.summary(),
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
