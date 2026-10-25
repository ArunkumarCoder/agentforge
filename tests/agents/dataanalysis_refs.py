"""Reference answers for the Data Analysis golden cases: what a good model would reply.

Each ``code`` really runs in the sandbox during the tests, and the numbers below must equal
what it emits, so these cannot drift from the cases.
"""

from __future__ import annotations

from typing import Any

from agentforge.agents.data_analysis.scoring import EvalCase

SCRIPTS: dict[str, str] = {
    "order_value": """import pandas as pd
from agentforge_sandbox import emit

orders = pd.read_csv("orders.csv")
done = orders[orders["status"] == "completed"]
emit("mean_completed_amount", done["amount"].mean())
emit("completed_orders", len(done))
""",
    "top_region": """import pandas as pd
from agentforge_sandbox import emit

sales = pd.read_csv("sales.csv")
sales["revenue"] = sales["units"] * sales["unit_price"]
by_region = sales.groupby("region")["revenue"].sum()
emit("top_region", by_region.idxmax())
emit("top_region_revenue", by_region.max())
""",
    "missing_values": """import pandas as pd
from agentforge_sandbox import emit

sensors = pd.read_csv("sensors.csv")
emit("mean_temp_sensor_b", sensors.loc[sensors["sensor"] == "B", "temp_c"].mean())
emit("missing_readings", int(sensors["temp_c"].isna().sum()))
""",
    "growth_rate": """import matplotlib.pyplot as plt
import pandas as pd
from agentforge_sandbox import emit, save_chart

monthly = pd.read_csv("monthly.csv")
latest, previous = monthly["revenue"].iloc[-1], monthly["revenue"].iloc[-2]
emit("growth_percent", (latest / previous - 1) * 100)
plt.figure(figsize=(6, 3))
plt.plot(monthly["month"], monthly["revenue"], marker="o")
plt.title("Revenue by month")
plt.xlabel("Month")
plt.ylabel("Revenue")
plt.xticks(rotation=45)
save_chart("revenue_by_month")
""",
    "gold_spend": """import pandas as pd
from agentforge_sandbox import emit

customers = pd.read_csv("customers.csv")
orders = pd.read_csv("orders.csv")
gold = customers[customers["tier"] == "gold"]
joined = orders.merge(gold, left_on="customer_id", right_on="id")
emit("gold_total_spend", joined["amount"].sum())
""",
}

FINDINGS: dict[str, list[dict[str, Any]]] = {
    "order_value": [
        {
            "claim": "The mean amount of completed orders is 68.88.",
            "key": "mean_completed_amount",
            "value": 68.87941176470588,
        },
        {"claim": "17 orders were completed.", "key": "completed_orders", "value": 17},
    ],
    "top_region": [
        {"claim": "The top region by revenue is south.", "key": "top_region", "value": "south"},
        {
            "claim": "South earned 4754.35 in total revenue.",
            "key": "top_region_revenue",
            "value": 4754.35,
        },
    ],
    "missing_values": [
        {
            "claim": "Sensor B averages 19.72 degrees.",
            "key": "mean_temp_sensor_b",
            "value": 19.72,
        },
        {"claim": "7 readings are missing.", "key": "missing_readings", "value": 7},
    ],
    "growth_rate": [
        {
            "claim": "Revenue grew 7.95% from the previous month.",
            "key": "growth_percent",
            "value": 7.947019867549665,
        }
    ],
    "gold_spend": [
        {
            "claim": "Gold tier customers spent 618 in total.",
            "key": "gold_total_spend",
            "value": 618.0,
        }
    ],
}

ANSWERS: dict[str, str] = {
    "order_value": "Excluding cancelled and refunded orders, 17 orders were completed and "
    "their mean amount is 68.88.",
    "top_region": "South has the highest total revenue, 4754.35.",
    "missing_values": "Sensor B's mean temperature is 19.72 degrees; 7 readings are missing "
    "across all sensors.",
    "growth_rate": "Revenue grew 7.95% from July to August; the chart shows the trend.",
    "gold_spend": "Customers in the gold tier spent 618 in total.",
}


def reference_answer(case: EvalCase) -> dict[str, Any]:
    charts = (
        [{"name": "revenue_by_month", "title": "Revenue by month"}]
        if case.expect.charts_min
        else []
    )
    return {
        "answer": ANSWERS[case.name],
        "code": SCRIPTS[case.name],
        "findings": FINDINGS[case.name],
        "charts": charts,
        "caveats": [],
    }
