"""Budget-Tool (sehr einfaches Grundgerüst) für SuS (Gymnasium Sek II)

Was kann das Script?
- Liest Einnahmen & Ausgaben aus einer CSV-Datei.
- Berechnet Totals und Ausgaben nach Kategorie.
- Gruppiert Ausgaben in Fixkosten / Wünsche / Sparen und zeigt diese in %.

So startest du (im Terminal):
- Standard-Datei: `transactions.csv` im gleichen Ordner wie dieses Script
  `python3 budget.py`
- Eigene Datei angeben:
  `python3 budget.py mein_budget.csv`
- Ohne Diagramme (nur Zahlen):
  `python3 budget.py --no-plots`

CSV-Spalten (mindestens):
- date, kind, amount, category
  kind: income/expense (oder Einnahmen/Ausgaben)
  amount: z.B. 12.50 oder 12,50

Optional:
- group: fix / want / save (wenn du die Gruppe direkt setzen willst)
- note: Text

Hinweis zu Plots in VS Code:
- Plotly-Plots öffnen sich beim normalen Script-Run meist im Browser.
- In einem Notebook/Interactive Window erscheinen sie oft direkt in VS Code.
"""

import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime


# -----------------------------------------------------------------------------
# 1) Anpassungsbereich: Kategorien -> Gruppe
# -----------------------------------------------------------------------------

# Wenn in der CSV in der Spalte `group` nichts steht, wird diese Tabelle benutzt.
CATEGORY_TO_GROUP = {
	# Fixkosten
	"Miete": "fix",
	"Krankenkasse": "fix",
	"ÖV": "fix",
	"Handy": "fix",
	"Internet": "fix",
	"Strom": "fix",
	"Abo": "fix",
	# Persönliche Wünsche
	"Restaurant": "want",
	"Kino": "want",
	"Games": "want",
	"Kleidung": "want",
	"Hobby": "want",
	# Sparen
	"Sparen": "save",
	"Investieren": "save",
}

GROUP_LABEL = {
	"fix": "Fixkosten",
	"want": "Persönliche Wünsche",
	"save": "Sparen",
	"other": "Andere",
}


# -----------------------------------------------------------------------------
# 2) Hilfsfunktionen
# -----------------------------------------------------------------------------


def parse_amount(text):
	"""Zahl aus Text lesen (12,50 oder 12.50)."""
	text = (text or "").strip()
	if text == "":
		raise ValueError("amount ist leer")
	text = text.replace("'", "")
	text = text.replace(" ", "")
	text = text.replace(",", ".")
	return float(text)


def parse_date(text):
	"""Datum robust lesen.

	Unterstützt z.B.:
	- 2026-01-11
	- 11.01.2026
	- 11/01/2026
	"""
	value = (text or "").strip()
	if value == "":
		raise ValueError("date ist leer")

	formats = [
		"%Y-%m-%d",
		"%d.%m.%Y",
		"%d/%m/%Y",
		"%d-%m-%Y",
	]
	for fmt in formats:
		try:
			return datetime.strptime(value, fmt).date()
		except ValueError:
			pass

	# Fallback: falls ISO mit Zeitstempel kommt
	try:
		return datetime.fromisoformat(value).date()
	except Exception:
		raise ValueError("Unbekanntes Datumsformat: " + value)


def normalize_kind(text):
	"""Erlaubt auch deutsche Begriffe."""
	value = (text or "").strip().lower()
	if value in ["income", "ein", "einnahme", "einnahmen"]:
		return "income"
	if value in ["expense", "aus", "ausgabe", "ausgaben"]:
		return "expense"
	raise ValueError("kind muss income oder expense sein")


def normalize_group(text):
	value = (text or "").strip().lower()
	if value in ["fix", "fixkosten"]:
		return "fix"
	if value in ["want", "wunsch", "wünsche", "wuensche"]:
		return "want"
	if value in ["save", "sparen"]:
		return "save"
	return "other"


def group_for_category(category, group_text=""):
	"""Bestimmt die Gruppe einer Ausgabe.

	- Wenn group_text gesetzt ist: normalisieren.
	- Sonst: via CATEGORY_TO_GROUP nachschlagen.
	"""
	group_text = (group_text or "").strip()
	if group_text == "":
		return CATEGORY_TO_GROUP.get(category, "other")
	return normalize_group(group_text)


def default_csv_path():
	"""transactions.csv im gleichen Ordner wie dieses Script."""
	folder = os.path.dirname(os.path.abspath(__file__))
	return os.path.join(folder, "transactions.csv")


def ensure_template_csv(filename):
	"""Erstellt eine Beispiel-CSV, falls sie noch nicht existiert."""
	if os.path.exists(filename):
		return

	rows = [
		{
			"date": str(date.today()),
			"kind": "income",
			"amount": "500",
			"category": "Lohn",
			"group": "",
			"note": "Beispiel: Nebenjob",
		},
		{
			"date": str(date.today()),
			"kind": "expense",
			"amount": "120",
			"category": "Miete",
			"group": "fix",
			"note": "Beispiel: Anteil WG",
		},
		{
			"date": str(date.today()),
			"kind": "expense",
			"amount": "25.5",
			"category": "Restaurant",
			"group": "want",
			"note": "Beispiel: Pizza",
		},
		{
			"date": str(date.today()),
			"kind": "expense",
			"amount": "50",
			"category": "Sparen",
			"group": "save",
			"note": "Beispiel: Sparkonto",
		},
	]

	with open(filename, "w", encoding="utf-8", newline="") as f:
		writer = csv.DictWriter(
			f, fieldnames=["date", "kind", "amount", "category", "group", "note"]
		)
		writer.writeheader()
		writer.writerows(rows)


# -----------------------------------------------------------------------------
# 3) Daten einlesen
# -----------------------------------------------------------------------------


def load_transactions(filename):
	"""Liest eine CSV und gibt eine Liste von Transaktionen (dicts) zurück."""
	required = ["date", "kind", "amount", "category"]

	with open(filename, "r", encoding="utf-8", newline="") as f:
		reader = csv.DictReader(f)
		if reader.fieldnames is None:
			raise ValueError("CSV hat keine Kopfzeile")

		for col in required:
			if col not in reader.fieldnames:
				raise ValueError("Spalte fehlt: " + col)

		transactions = []
		line_nr = 1
		for row in reader:
			line_nr += 1
			try:
				kind = normalize_kind(row.get("kind"))
				amount = parse_amount(row.get("amount"))
				category = (row.get("category") or "").strip()
				if category == "":
					raise ValueError("category ist leer")

				group = group_for_category(category, row.get("group"))

				transactions.append(
					{
						"date": (row.get("date") or "").strip(),
						"date_obj": parse_date(row.get("date")),
						"kind": kind,
						"amount": amount,
						"category": category,
						"group": group,
						"note": (row.get("note") or "").strip(),
					}
				)
			except Exception as e:
				raise ValueError("Fehler in Zeile " + str(line_nr) + ": " + str(e))

	return transactions


# -----------------------------------------------------------------------------
# 4) Auswerten
# -----------------------------------------------------------------------------


def compute_totals(transactions):
	income = 0.0
	expense = 0.0
	for tx in transactions:
		if tx["kind"] == "income":
			income += tx["amount"]
		else:
			expense += tx["amount"]
	return income, expense, income - expense


def sum_expenses_by_category(transactions):
	sums = defaultdict(float)
	for tx in transactions:
		if tx["kind"] == "expense":
			sums[tx["category"]] += tx["amount"]
	return dict(sums)


def sum_expenses_by_group(transactions):
	sums = defaultdict(float)
	for tx in transactions:
		if tx["kind"] == "expense":
			sums[tx["group"]] += tx["amount"]
	return dict(sums)


# -----------------------------------------------------------------------------
# 5) Plotten (optional)
# -----------------------------------------------------------------------------


def create_plots(expenses_by_category, expenses_by_group, renderer="browser"):
	try:
		import plotly.express as px
		import plotly.io as pio
	except ImportError:
		print("Plotly ist nicht installiert. Installiere es mit: pip install plotly")
		return None, None

	# Für normale .py-Runs ist Browser oft am zuverlässigsten.
	# In Notebooks kann man z.B. auch "notebook" verwenden.
	pio.renderers.default = renderer

	fig_expenses_by_category = None
	fig_expenses_by_group = None

	# Balkendiagramm: Ausgaben nach Kategorie
	if len(expenses_by_category) > 0:
		items = sorted(expenses_by_category.items(), key=lambda x: x[1], reverse=True)
		categories = [k for k, _ in items]
		values = [v for _, v in items]

		fig_expenses_by_category = px.bar(
			x=categories,
			y=values,
			title="Ausgaben nach Kategorie",
			labels={"x": "Kategorie", "y": "CHF"},
			text=values,
		)
		fig_expenses_by_category.update_layout(xaxis_tickangle=-30)
	else:
		print("Keine Ausgaben (Kategorie-Plot).")

	# Kreisdiagramm: Gruppen in %
	total = sum(expenses_by_group.values())
	if total > 0:
		items = sorted(expenses_by_group.items(), key=lambda x: x[1], reverse=True)
		labels = [GROUP_LABEL.get(k, k) for k, _ in items]
		values = [v for _, v in items]

		fig_expenses_by_group = px.pie(
			names=labels,
			values=values,
			title="Ausgaben nach Gruppe (in %)",
			hole=0.35,
		)
	else:
		print("Keine Ausgaben (Gruppen-Plot).")
	return fig_expenses_by_category, fig_expenses_by_group


def show_plots(expenses_by_category, expenses_by_group, renderer="browser"):
	fig1, fig2 = create_plots(expenses_by_category, expenses_by_group, renderer=renderer)
	if fig1 is not None:
		fig1.show()
	if fig2 is not None:
		fig2.show()


# -----------------------------------------------------------------------------
# 6) Main
# -----------------------------------------------------------------------------


def parse_args(argv):
	"""Sehr einfache Argumente:
	- optional: Dateiname
	- optional: --no-plots
	"""
	no_plots = False
	filename = None

	for a in argv[1:]:
		if a == "--no-plots":
			no_plots = True
		else:
			filename = a

	if filename is None:
		filename = default_csv_path()

	return filename, no_plots


def main():
	filename, no_plots = parse_args(sys.argv)
	ensure_template_csv(filename)

	transactions = load_transactions(filename)
	income, expense, net = compute_totals(transactions)

	print("Datei:", filename)
	print("Einnahmen total: %.2f" % income)
	print("Ausgaben total:  %.2f" % expense)
	print("Saldo (Netto):   %.2f" % net)

	exp_cat = sum_expenses_by_category(transactions)
	exp_grp = sum_expenses_by_group(transactions)

	if no_plots:
		return

	show_plots(exp_cat, exp_grp)


if __name__ == "__main__":
	main()
