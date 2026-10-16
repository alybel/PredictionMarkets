"""Stock universe: the 100 largest US stocks (S&P 100 constituents), Yahoo ticker notation."""

TOP_US_STOCKS: dict[str, str] = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA", "AMZN": "Amazon", "GOOGL": "Alphabet",
    "META": "Meta Platforms", "BRK-B": "Berkshire Hathaway", "AVGO": "Broadcom", "TSLA": "Tesla", "LLY": "Eli Lilly",
    "JPM": "JPMorgan Chase", "V": "Visa", "WMT": "Walmart", "ORCL": "Oracle", "MA": "Mastercard",
    "XOM": "Exxon Mobil", "UNH": "UnitedHealth", "COST": "Costco", "NFLX": "Netflix", "HD": "Home Depot",
    "PG": "Procter & Gamble", "JNJ": "Johnson & Johnson", "ABBV": "AbbVie", "BAC": "Bank of America", "CRM": "Salesforce",
    "KO": "Coca-Cola", "CVX": "Chevron", "AMD": "AMD", "TMUS": "T-Mobile US", "PLTR": "Palantir",
    "WFC": "Wells Fargo", "CSCO": "Cisco", "IBM": "IBM", "MRK": "Merck", "ABT": "Abbott",
    "PM": "Philip Morris", "LIN": "Linde", "GE": "GE Aerospace", "MCD": "McDonald's", "ACN": "Accenture",
    "PEP": "PepsiCo", "ISRG": "Intuitive Surgical", "DIS": "Disney", "NOW": "ServiceNow", "INTU": "Intuit",
    "TMO": "Thermo Fisher", "AXP": "American Express", "MS": "Morgan Stanley", "GS": "Goldman Sachs", "T": "AT&T",
    "VZ": "Verizon", "CAT": "Caterpillar", "RTX": "RTX", "QCOM": "Qualcomm", "TXN": "Texas Instruments",
    "ADBE": "Adobe", "BKNG": "Booking", "SPGI": "S&P Global", "PFE": "Pfizer", "AMGN": "Amgen",
    "BLK": "BlackRock", "SCHW": "Charles Schwab", "LOW": "Lowe's", "UNP": "Union Pacific", "NEE": "NextEra Energy",
    "HON": "Honeywell", "C": "Citigroup", "DHR": "Danaher", "BA": "Boeing", "GILD": "Gilead",
    "AMAT": "Applied Materials", "BMY": "Bristol-Myers Squibb", "MDT": "Medtronic", "UBER": "Uber", "COP": "ConocoPhillips",
    "DE": "Deere", "LMT": "Lockheed Martin", "SBUX": "Starbucks", "MO": "Altria", "CMCSA": "Comcast",
    "INTC": "Intel", "MU": "Micron", "ADP": "ADP", "PYPL": "PayPal", "CVS": "CVS Health",
    "UPS": "UPS", "MDLZ": "Mondelez", "MMM": "3M", "SO": "Southern Company", "DUK": "Duke Energy",
    "CL": "Colgate-Palmolive", "USB": "U.S. Bancorp", "GD": "General Dynamics", "COF": "Capital One", "EMR": "Emerson",
    "FDX": "FedEx", "TGT": "Target", "NKE": "Nike", "BK": "BNY Mellon", "GM": "General Motors",
}
