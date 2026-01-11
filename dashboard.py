"""Dash Budget Dashboard (für SuS, Gymnasium Sek II)

Start:
  python3 dashboard.py

Dann im Browser öffnen:
  http://127.0.0.1:8050

CSV-Spalten (mindestens):
  - date, kind, amount, category
    kind: income/expense (oder Einnahmen/Ausgaben)
    amount: z.B. 12.50 oder 12,50

Optional:
  - group: fix / want / save (wenn du die Gruppe direkt setzen willst)
  - note

Hinweis:
  Wenn group leer ist, wird die Zuordnung aus budget.py verwendet.
"""

from __future__ import annotations

import base64
import csv
import io
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from werkzeug.exceptions import RequestEntityTooLarge

import plotly.express as px
import plotly.graph_objects as go

from dash import Dash, Input, Output, State, dcc, html, no_update


# Damit "import budget" sicher funktioniert, egal von wo gestartet.
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
	sys.path.insert(0, str(THIS_DIR))

import budget  # noqa: E402


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _decode_contents(contents: str) -> bytes:
	"""Decode base64 payload from dcc.Upload."""
	try:
		_, b64 = contents.split(",", 1)
	except ValueError:
		# already raw?
		b64 = contents
	return base64.b64decode(b64)


def _bytes_to_text(data: bytes) -> str:
	"""Try common encodings (UTF-8, UTF-8-SIG, Latin-1)."""
	for enc in ("utf-8-sig", "utf-8", "latin-1"):
		try:
			return data.decode(enc)
		except UnicodeDecodeError:
			pass
	# worst-case fallback
	return data.decode("utf-8", errors="replace")


def _sniff_dialect(sample: str) -> csv.Dialect:
	class _ExcelSemicolon(csv.Dialect):
		delimiter = ";"
		quotechar = '"'
		doublequote = True
		skipinitialspace = False
		lineterminator = "\r\n"
		quoting = csv.QUOTE_MINIMAL

	try:
		sniffer = csv.Sniffer()
		return sniffer.sniff(sample, delimiters=";,\t,")
	except Exception:
		# very common in German-speaking regions
		if sample.count(";") > sample.count(","):
			return _ExcelSemicolon
		return csv.excel


def _empty_fig(title: str) -> go.Figure:
	fig = go.Figure()
	fig.update_layout(
		title=title,
		# separators: <decimal><thousands>
		# Swiss style: decimal '.' and thousands apostrophe '\''
		separators=".'",
		annotations=[
			dict(
				text="Noch keine Daten. Bitte CSV hochladen.",
				x=0.5,
				y=0.5,
				xref="paper",
				yref="paper",
				showarrow=False,
			)
		],
	)
	return fig


def _apply_swiss_separators(fig: go.Figure) -> go.Figure:
	"""Use Swiss-style number separators (thousands apostrophe) in axes/hover."""
	fig.update_layout(separators=".'")
	return fig


@dataclass
class ParsedData:
	transactions: list[dict[str, Any]]
	warnings: list[str]


def parse_transactions_from_upload(contents: str, filename: str | None) -> ParsedData:
	data = _decode_contents(contents)
	text = _bytes_to_text(data)

	# Read CSV
	sample = text[:4096]
	dialect = _sniff_dialect(sample)
	stream = io.StringIO(text)
	reader = csv.DictReader(stream, dialect=dialect)
	if reader.fieldnames is None:
		raise ValueError("CSV hat keine Kopfzeile")

	required = {"date", "kind", "amount", "category"}
	missing = required.difference(set(reader.fieldnames))
	if missing:
		raise ValueError("Spalten fehlen: " + ", ".join(sorted(missing)))

	transactions: list[dict[str, Any]] = []
	warnings: list[str] = []

	line_nr = 1
	for row in reader:
		line_nr += 1
		try:
			kind = budget.normalize_kind(row.get("kind"))
			amount = budget.parse_amount(row.get("amount"))
			category = (row.get("category") or "").strip()
			if category == "":
				raise ValueError("category ist leer")

			date_obj = budget.parse_date(row.get("date"))
			group = budget.group_for_category(category, row.get("group"))

			transactions.append(
				{
					"date": (row.get("date") or "").strip(),
					"date_obj": date_obj,
					"kind": kind,
					"amount": amount,
					"category": category,
					"group": group,
					"note": (row.get("note") or "").strip(),
				}
			)
		except Exception as e:
			raise ValueError(
				f"Fehler in Zeile {line_nr}: {e} (Datei: {filename or 'upload'})"
			)

	if len(transactions) == 0:
		warnings.append("CSV enthält keine Datenzeilen (nur Header?)")

	return ParsedData(transactions=transactions, warnings=warnings)


def month_key(d) -> str:
	return d.strftime("%Y-%m")


def compute_aggregates(transactions: list[dict[str, Any]]):
	monthly_cat = defaultdict(lambda: defaultdict(float))
	monthly_group = defaultdict(lambda: defaultdict(float))
	monthly_income = defaultdict(float)
	monthly_expense = defaultdict(float)

	totals_by_category = defaultdict(float)
	totals_by_group = defaultdict(float)

	for tx in transactions:
		m = month_key(tx["date_obj"])
		if tx["kind"] == "income":
			monthly_income[m] += tx["amount"]
			continue

		monthly_expense[m] += tx["amount"]
		monthly_cat[m][tx["category"]] += tx["amount"]
		monthly_group[m][tx["group"]] += tx["amount"]
		totals_by_category[tx["category"]] += tx["amount"]
		totals_by_group[tx["group"]] += tx["amount"]

	months = sorted(set(monthly_income.keys()) | set(monthly_expense.keys()))
	return (
		months,
		monthly_income,
		monthly_expense,
		monthly_cat,
		monthly_group,
		dict(totals_by_category),
		dict(totals_by_group),
	)


def future_value_monthly_contrib(pmt: float, years: int, annual_real_rate: float) -> float:
	"""Future value of monthly contributions in *real* terms (today's CHF)."""
	if years <= 0 or pmt <= 0:
		return 0.0
	r_m = (1.0 + annual_real_rate) ** (1.0 / 12.0) - 1.0
	n = years * 12
	if abs(r_m) < 1e-12:
		return pmt * n
	return pmt * (((1.0 + r_m) ** n - 1.0) / r_m)


def fig_monthly_expenses_by_category(months, monthly_cat) -> go.Figure:
	rows = []
	for m in months:
		for cat, amt in monthly_cat.get(m, {}).items():
			rows.append({"month": m, "category": cat, "amount": amt})
	if not rows:
		return _empty_fig("Ausgaben pro Monat (nach Kategorie)")
	fig = px.bar(rows, x="month", y="amount", color="category", title="Ausgaben pro Monat (nach Kategorie)")
	fig.update_traces(
		hovertemplate="<b>%{fullData.name}</b><br>Monat %{x}<br>CHF %{y:,.0f}.-<extra></extra>"
	)
	fig.update_layout(barmode="stack", xaxis_title="Monat", yaxis_title="CHF")
	return _apply_swiss_separators(fig)


def fig_monthly_expenses_by_group(months, monthly_group) -> go.Figure:
	rows = []
	for m in months:
		for grp, amt in monthly_group.get(m, {}).items():
			rows.append({"month": m, "group": budget.GROUP_LABEL.get(grp, grp), "amount": amt})
	if not rows:
		return _empty_fig("Ausgaben pro Monat (nach Gruppe)")
	fig = px.bar(rows, x="month", y="amount", color="group", title="Ausgaben pro Monat (nach Gruppe)")
	fig.update_traces(
		hovertemplate="<b>%{fullData.name}</b><br>Monat %{x}<br>CHF %{y:,.0f}.-<extra></extra>"
	)
	fig.update_layout(barmode="stack", xaxis_title="Monat", yaxis_title="CHF")
	return _apply_swiss_separators(fig)


def fig_totals_by_category(totals_by_category) -> go.Figure:
	if not totals_by_category:
		return _empty_fig("Ausgaben total (nach Kategorie)")
	items = sorted(totals_by_category.items(), key=lambda x: x[1], reverse=True)
	rows = [{"category": k, "amount": v} for k, v in items]
	fig = px.bar(rows, x="category", y="amount", title="Ausgaben total (nach Kategorie)", text="amount")
	fig.update_traces(
		hovertemplate="Kategorie %{x}<br>CHF %{y:,.0f}.-<extra></extra>",
		texttemplate="CHF %{y:,.0f}.-",
		textposition="auto",
	)
	fig.update_layout(xaxis_tickangle=-30, xaxis_title="Kategorie", yaxis_title="CHF")
	return _apply_swiss_separators(fig)


def fig_group_pie(totals_by_group) -> go.Figure:
	if not totals_by_group:
		return _empty_fig("Ausgaben total (nach Gruppe)")
	items = sorted(totals_by_group.items(), key=lambda x: x[1], reverse=True)
	rows = [{"group": budget.GROUP_LABEL.get(k, k), "amount": v} for k, v in items]
	fig = px.pie(rows, names="group", values="amount", title="Ausgaben total (nach Gruppe)", hole=0.35)
	fig.update_traces(hovertemplate="%{label}<br>CHF %{value:,.0f}.-<extra></extra>")
	return _apply_swiss_separators(fig)


def fig_monthly_cashflow(months, monthly_income, monthly_expense, show_income: bool) -> go.Figure:
	if not months:
		return _empty_fig("Cashflow pro Monat")

	income = [monthly_income.get(m, 0.0) for m in months]
	expense = [monthly_expense.get(m, 0.0) for m in months]
	net = [i - e for i, e in zip(income, expense)]

	fig = go.Figure()
	if show_income:
		fig.add_bar(name="Einnahmen", x=months, y=income)
	fig.add_bar(name="Ausgaben", x=months, y=expense)
	fig.add_scatter(name="Saldo (Netto)", x=months, y=net, mode="lines+markers")
	fig.update_layout(
		title="Cashflow pro Monat",
		xaxis_title="Monat",
		yaxis_title="CHF",
		barmode="group",
	)
	fig.update_traces(
		hovertemplate="Monat %{x}<br>CHF %{y:,.0f}.-<extra>%{fullData.name}</extra>",
		selector=dict(type="bar"),
	)
	fig.update_traces(
		hovertemplate="Monat %{x}<br>CHF %{y:,.0f}.-<extra>%{fullData.name}</extra>",
		selector=dict(type="scatter"),
	)
	return _apply_swiss_separators(fig)


def fig_savings_projection(monthly_saving: float) -> go.Figure:
	# Vorgaben aus Auftrag:
	# - Aktienmarkt: 8% Rendite - 1% Inflation = 7% real
	# - Bank: Inflation -1% real (Kaufkraft sinkt)
	stock_real = 0.07
	bank_real = -0.01
	no_infl_no_investing_real = 0.0

	years = list(range(0, 41))
	stock = [future_value_monthly_contrib(monthly_saving, y, stock_real) for y in years]
	bank = [future_value_monthly_contrib(monthly_saving, y, bank_real) for y in years]
	baseline = [
		future_value_monthly_contrib(monthly_saving, y, no_infl_no_investing_real) for y in years
	]

	fig = go.Figure()
	fig.add_scatter(
		x=years,
		y=baseline,
		mode="lines",
		name="Ohne Inflation & ohne Investieren (0% real/Jahr)",
	)
	fig.add_scatter(x=years, y=stock, mode="lines", name="Aktien (≈ +7% real/Jahr)")
	fig.add_scatter(x=years, y=bank, mode="lines", name="Bank (≈ -1% real/Jahr)")
	fig.update_layout(
		title="Vermögensentwicklung des Spar-Betrags (in heutiger Kaufkraft)",
		xaxis_title="Jahre",
		yaxis_title="CHF (real)",
	)
	fig.update_traces(
		hovertemplate="Jahr %{x}<br>CHF %{y:,.0f}.-<extra>%{fullData.name}</extra>",
		selector=dict(type="scatter"),
	)

	# Marker bei 10/20/30/40
	for y in (10, 20, 30, 40):
		fig.add_vline(x=y, line_width=1, line_dash="dot", line_color="rgba(0,0,0,0.25)")
	return _apply_swiss_separators(fig)


# -----------------------------------------------------------------------------
# Dash App
# -----------------------------------------------------------------------------


app = Dash(__name__)
app.title = "Budget Dashboard"

# Posit Connect serves the app via WSGI. Expose the Flask server.
server = app.server

# Uploads are posted to the Dash callback endpoint; on servers this often hits
# a request body limit. Make it configurable.
_max_upload_mb = int(os.getenv("DASH_MAX_UPLOAD_MB", "25"))
server.config["MAX_CONTENT_LENGTH"] = _max_upload_mb * 1024 * 1024


@server.errorhandler(RequestEntityTooLarge)
def _handle_upload_too_large(e):
	# Dash will surface this as a failed request; this message ends up in logs.
	return (
		f"Upload zu gross (Limit: {_max_upload_mb} MB). "
		"Verkleinere die CSV oder erhoehe DASH_MAX_UPLOAD_MB.",
		413,
	)


def template_csv_text() -> str:
	rows = [
		{"date": "2026-01-01", "kind": "income", "amount": "1200", "category": "Lohn", "group": "", "note": "Nebenjob"},
		{"date": "2026-01-02", "kind": "expense", "amount": "600", "category": "Miete", "group": "fix", "note": ""},
		{"date": "2026-01-05", "kind": "expense", "amount": "45.50", "category": "Restaurant", "group": "want", "note": ""},
		{"date": "2026-01-07", "kind": "expense", "amount": "200", "category": "Sparen", "group": "save", "note": ""},
	]
	buf = io.StringIO()
	writer = csv.DictWriter(buf, fieldnames=["date", "kind", "amount", "category", "group", "note"])
	writer.writeheader()
	writer.writerows(rows)
	return buf.getvalue()


app.layout = html.Div(
	style={"maxWidth": "1100px", "margin": "0 auto", "fontFamily": "system-ui"},
	children=[
		html.H2("Budget Dashboard (CSV Upload)"),
		html.Div(
			[
				html.P(
					"Lade deine CSV hoch und schaue dir deine Ausgaben nach Monat, Kategorie und Gruppe an. "
					"Die Gruppe (Fixkosten/Wünsche/Sparen) wird aus der Spalte group oder aus budget.py abgeleitet."
				),
				html.Ul(
					[
						html.Li("Pflichtspalten: date, kind, amount, category"),
						html.Li("Optional: group (fix/want/save), note"),
						html.Li("Beträge: 12.50 oder 12,50"),
					]
				),
			],
		),

		html.Div(
			style={"display": "flex", "gap": "12px", "alignItems": "center", "flexWrap": "wrap"},
			children=[
				html.Button("Template-CSV herunterladen", id="btn-template", n_clicks=0),
				dcc.Download(id="download-template"),
				dcc.Upload(
					id="upload-csv",
					children=html.Div(["CSV hierher ziehen oder ", html.A("Datei auswählen")]),
					style={
						"width": "420px",
						"height": "60px",
						"lineHeight": "60px",
						"borderWidth": "1px",
						"borderStyle": "dashed",
						"borderRadius": "8px",
						"textAlign": "center",
					},
					multiple=False,
				),
			],
		),
		html.Div(id="upload-status", style={"marginTop": "8px"}),
		dcc.Store(id="store-transactions"),

		html.Hr(),
		html.Div(
			style={"display": "flex", "gap": "18px", "alignItems": "center", "flexWrap": "wrap"},
			children=[
				html.Div(
					[
						html.Label("Monatlich sparbarer Betrag (CHF)"),
						dcc.Input(
							id="input-monthly-saving",
							type="number",
							min=0,
							step=10,
							placeholder="leer lassen = aus Daten berechnen",
							debounce=True,
						),
						html.Div(
							"(Wird für die 10–40 Jahres-Prognose genutzt.)",
							style={"fontSize": "0.9em", "color": "#555"},
						),
					],
				),
				dcc.Checklist(
					id="check-show-income",
					options=[{"label": "Einnahmen im Cashflow-Plot anzeigen", "value": "show"}],
					value=["show"],
				),
			],
		),

		html.Div(id="summary", style={"marginTop": "10px"}),

		dcc.Loading(
			children=[
				dcc.Graph(id="fig-monthly-cat"),
				dcc.Graph(id="fig-monthly-group"),
				html.Div(
					style={"display": "flex", "gap": "12px", "flexWrap": "wrap"},
					children=[
						html.Div(dcc.Graph(id="fig-totals-cat"), style={"flex": "1 1 520px"}),
						html.Div(dcc.Graph(id="fig-pie-group"), style={"flex": "1 1 420px"}),
					],
				),
				dcc.Graph(id="fig-cashflow"),
				dcc.Graph(id="fig-savings"),
			],
		),
	],
)


@app.callback(
	Output("download-template", "data"),
	Input("btn-template", "n_clicks"),
	prevent_initial_call=True,
)
def download_template(n_clicks: int):
	return dict(content=template_csv_text(), filename="transactions_template.csv")


@app.callback(
	Output("store-transactions", "data"),
	Output("upload-status", "children"),
	Input("upload-csv", "contents"),
	State("upload-csv", "filename"),
)
def handle_upload(contents: str | None, filename: str | None):
	if not contents:
		return None, "Noch keine Datei hochgeladen."
	# Some Dash versions/browsers may deliver list values even with multiple=False.
	if isinstance(contents, list):
		contents = contents[0] if contents else None
	if isinstance(filename, list):
		filename = filename[0] if filename else None
	if not contents:
		return None, "Noch keine Datei hochgeladen."
	try:
		parsed = parse_transactions_from_upload(contents, filename)
		msg = f"OK: {len(parsed.transactions)} Zeilen eingelesen ({filename})."
		if parsed.warnings:
			msg += " Hinweise: " + " | ".join(parsed.warnings)
		# dcc.Store muss JSON-serialisierbar sein: date_obj -> ISO-String
		serializable = []
		for tx in parsed.transactions:
			serializable.append(
				{
					**{k: v for k, v in tx.items() if k != "date_obj"},
					"date_obj": tx["date_obj"].isoformat(),
				}
			)
		return serializable, html.Div(msg, style={"color": "#0a7"})
	except Exception as e:
		return None, html.Div(str(e), style={"color": "#c00"})


@app.callback(
	Output("summary", "children"),
	Output("fig-monthly-cat", "figure"),
	Output("fig-monthly-group", "figure"),
	Output("fig-totals-cat", "figure"),
	Output("fig-pie-group", "figure"),
	Output("fig-cashflow", "figure"),
	Output("fig-savings", "figure"),
	Input("store-transactions", "data"),
	Input("input-monthly-saving", "value"),
	Input("check-show-income", "value"),
)
def update_figures(store_data, monthly_saving_value, show_income_values):
	if not store_data:
		empty = (
			_empty_fig("Ausgaben pro Monat (nach Kategorie)"),
			_empty_fig("Ausgaben pro Monat (nach Gruppe)"),
			_empty_fig("Ausgaben total (nach Kategorie)"),
			_empty_fig("Ausgaben total (nach Gruppe)"),
			_empty_fig("Cashflow pro Monat"),
			_empty_fig("Vermögensentwicklung (in heutiger Kaufkraft)"),
		)
		return (
			html.Div("Upload eine CSV, um Auswertungen zu sehen."),
			*empty,
		)

	# Rebuild tx list and parse date_obj
	transactions = []
	for tx in store_data:
		tx2 = dict(tx)
		tx2["date_obj"] = budget.parse_date(tx2["date_obj"])
		transactions.append(tx2)

	months, monthly_income, monthly_expense, monthly_cat, monthly_group, totals_by_category, totals_by_group = compute_aggregates(
		transactions
	)

	# Summary numbers
	total_income = sum(monthly_income.values())
	total_expense = sum(monthly_expense.values())
	net = total_income - total_expense
	month_count = max(1, len(months))
	avg_net = net / month_count

	# Choose monthly saving for projection
	if monthly_saving_value is None:
		monthly_saving = max(0.0, avg_net)
		monthly_saving_note = "(aus Durchschnitt Netto/Monat)"
	else:
		monthly_saving = max(0.0, float(monthly_saving_value))
		monthly_saving_note = "(manuell)"

	show_income = "show" in (show_income_values or [])

	summary = html.Div(
		[
			html.B("Kurzüberblick"),
			html.Ul(
				[
					html.Li(f"Monate im Datensatz: {len(months)}"),
					html.Li(f"Einnahmen total: {total_income:.2f} CHF"),
					html.Li(f"Ausgaben total: {total_expense:.2f} CHF"),
					html.Li(f"Saldo (Netto): {net:.2f} CHF"),
					html.Li(f"Ø Netto pro Monat: {avg_net:.2f} CHF"),
					html.Li(f"Sparbetrag für Prognose: {monthly_saving:.2f} CHF/Monat {monthly_saving_note}"),
				]
			),
		],
		style={"background": "#f7f7f7", "padding": "10px", "borderRadius": "8px"},
	)

	return (
		summary,
		fig_monthly_expenses_by_category(months, monthly_cat),
		fig_monthly_expenses_by_group(months, monthly_group),
		fig_totals_by_category(totals_by_category),
		fig_group_pie(totals_by_group),
		fig_monthly_cashflow(months, monthly_income, monthly_expense, show_income=show_income),
		fig_savings_projection(monthly_saving),
	)


if __name__ == "__main__":
	app.run(debug=True)

