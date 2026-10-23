"""
Paper Trading Bot – eigenständiger Bot, der Telegram-Signale liest und
fiktive Trades mit einem virtuellen Konto ausführt.

Läuft alle 5 Minuten über GitHub Actions (siehe .github/workflows/paper-bot.yml).
Kein echtes Geld, keine Börsen-Konten, keine API-Schlüssel von Binance nötig –
nur öffentliche Kursdaten von data-api.binance.vision.

Woher kommen die Signale?
  1. Automatisch aus dem öffentlichen Repo des Signal-Bots (open_trades.json und
     trade_history.json) – Telegram lässt Bots keine Nachrichten anderer Bots lesen.
  2. Oder: Du LEITEST eine Signal-Nachricht an diesen Bot WEITER (manuell).

Eingestiegen wird rückwirkend zum Zeitpunkt des Signals – so, als hätte der Bot
sofort gehandelt, auch wenn GitHub ihn erst ein paar Minuten später startet.

Befehle im Chat mit dem Bot: /hilfe
"""

import html
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Einstellungen – hier kannst du alles anpassen
# ---------------------------------------------------------------------------

START_BALANCE = 10_000.0     # virtuelles Startkapital in $
RISK_PERCENT = 1.0           # Risiko pro Trade in % vom Kontostand (Abstand Einstieg -> SL)
MAX_LEVERAGE = 10.0          # Positionsgröße höchstens Kontostand x Hebel
FEE_PERCENT = 0.05           # Gebühr pro Seite in % (Binance Futures ~0,05 %, Spot 0,1 %, 0 = ohne)
MAX_OPEN_POSITIONS = 5       # mehr gleichzeitig offene Trades werden abgelehnt
MAX_SIGNAL_AGE_MIN = 180     # ältere Signale werden ignoriert (z. B. alte Einträge beim ersten Start)

# Signale werden direkt aus dem (öffentlichen) Repo des Signal-Bots gelesen.
# Telegram lässt Bots keine Nachrichten anderer Bots lesen – deshalb dieser Weg.
# Leer lassen ("") = nur Telegram-Weiterleitungen nutzen.
SIGNAL_REPO = os.getenv("SIGNAL_REPO", "").strip() or "caps4476-dotcom/15min-trading-bot"
SIGNAL_BRANCH = "main"
CATEGORY_NAMES = {"RETEST": "Re-Test", "BREAKOUT": "Breakout", "MOMENTUM": "Momentum"}

# Welche Signale werden gehandelt? Alles, was hier nicht steht, wird ignoriert.
# Optionen je Markt: "RETEST", "MOMENTUM", "BREAKOUT"
# Beispiel alles handeln: TRADE_FILTER = {}  (leer = kein Filter)
TRADE_FILTER = {}   # aktuell: ALLE Signale werden gehandelt
# Beispiel nur bestimmte Signale:
# TRADE_FILTER = {
#     "BTCUSDT": ["RETEST", "MOMENTUM"],
#     "PAXGUSDT": ["RETEST"],   # Gold
#     "EURUSDT": ["RETEST"],    # EUR/USD
# }

# Kurze Erklärung, WARUM ein Signal entsteht (Regeln aus dem Signal-Bot)
WHY = {
    ("RETEST", "BUY"): "Aufwärtstrend (Kurs über EMA 200). Die Kerze hat das untere Bollinger-Band "
                       "berührt und grün wieder darüber geschlossen = Abprall. RSI ≤ 40 = überverkauft.",
    ("RETEST", "SELL"): "Abwärtstrend (Kurs unter EMA 200). Die Kerze hat das obere Bollinger-Band "
                        "berührt und rot wieder darunter geschlossen = Ablehnung. RSI ≥ 60 = überkauft.",
    ("MOMENTUM", "BUY"): "Aufwärtstrend (Kurs über EMA 200). Die schnelle EMA 9 kreuzt die EMA 21 "
                         "nach oben = Schwung dreht nach oben. RSI ≥ 55 bestätigt Stärke.",
    ("MOMENTUM", "SELL"): "Abwärtstrend (Kurs unter EMA 200). Die schnelle EMA 9 kreuzt die EMA 21 "
                          "nach unten = Schwung dreht nach unten. RSI ≤ 45 bestätigt Schwäche.",
    ("BREAKOUT", "BUY"): "Aufwärtstrend (Kurs über EMA 200). Der Kurs bricht über das obere "
                         "Bollinger-Band aus, RSI ≥ 60 bestätigt den Ausbruch.",
    ("BREAKOUT", "SELL"): "Abwärtstrend (Kurs unter EMA 200). Der Kurs bricht unter das untere "
                          "Bollinger-Band aus, RSI ≤ 40 bestätigt den Ausbruch.",
}
SL_TP_RULE = "SL = 1,5× bzw. 2× ATR (übliche Kerzengröße) vom Einstieg, TP = 1,5× der SL-Abstand."


def category_key(category: str) -> str:
    for k, name in CATEGORY_NAMES.items():
        if category.lower() in (k.lower(), name.lower()):
            return k
    return category.upper()


def why_text(cat_key: str, side: str, rsi=None) -> str:
    txt = WHY.get((cat_key, side), "")
    if txt and rsi is not None:
        txt += f" (RSI war {rsi:.1f})"
    return txt


def signal_allowed(symbol: str, category: str) -> bool:
    """category darf der Schlüssel (RETEST) oder der Anzeigename (Re-Test) sein."""
    if not TRADE_FILTER:
        return True
    return category_key(category) in TRADE_FILTER.get(symbol.upper(), [])
MAX_TRADE_HOURS = 48         # Trade wird nach dieser Zeit zum Marktpreis geschlossen (0 = nie)

# Optionale Secrets (in GitHub unter Settings -> Secrets -> Actions)
BOT_TOKEN = os.getenv("PAPER_BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()          # deine eigene Chat-ID
SIGNAL_CHANNEL_ID = os.getenv("SIGNAL_CHANNEL_ID", "").strip()  # nur diesen Kanal akzeptieren

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_state.json")

PRICE_URLS = [
    "https://data-api.binance.vision/api/v3",
    "https://api.binance.com/api/v3",
]

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def log(msg: str):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Zustand (Konto, offene und geschlossene Trades) – liegt in paper_state.json
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {
        "balance": START_BALANCE,
        "start_balance": START_BALANCE,
        "risk_percent": RISK_PERCENT,
        "paused": False,
        "telegram_offset": 0,
        "seen_channels": [],
        "seen_signals": [],
        "open": [],
        "closed": [],
        "created": now_iso(),
    }


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        base = new_state()
        base.update(state)
        return base
    except (FileNotFoundError, json.JSONDecodeError):
        return new_state()


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Kursdaten (öffentlich, ohne Schlüssel)
# ---------------------------------------------------------------------------

def _price_get(path: str, params: dict):
    last_err = None
    for base in PRICE_URLS:
        try:
            r = requests.get(f"{base}/{path}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_err = e
            log(f"Kursabruf über {base} fehlgeschlagen: {e}")
    raise last_err


def get_price(symbol: str) -> float:
    return float(_price_get("ticker/price", {"symbol": symbol})["price"])


def get_price_at(symbol: str, ms: int):
    """Schlusskurs der 1-Minuten-Kerze, die zum Zeitpunkt ms lief bzw. gerade endete."""
    data = _price_get("klines", {"symbol": symbol, "interval": "1m",
                                 "endTime": ms, "limit": 1})
    if not data:
        return None
    return float(data[-1][4]), int(data[-1][6])


def get_minute_candles(symbol: str, start_ms: int) -> list:
    """Alle ABGESCHLOSSENEN 1-Minuten-Kerzen ab start_ms als (open_ms, close_ms, high, low, close)."""
    candles = []
    cursor = start_ms
    while True:
        data = _price_get("klines", {"symbol": symbol, "interval": "1m",
                                     "startTime": cursor, "limit": 1000})
        if not data:
            break
        for k in data:
            close_ms = int(k[6])
            if close_ms >= now_ms():  # Kerze läuft noch
                return candles
            candles.append((int(k[0]), close_ms, float(k[2]), float(k[3]), float(k[4])))
        if len(data) < 1000:
            break
        cursor = int(data[-1][6]) + 1
    return candles


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_send(chat_id, text: str):
    if not chat_id:
        log("Keine Chat-ID zum Senden – Nachricht übersprungen.")
        return
    try:
        r = requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if not r.ok:
            log(f"Telegram-Fehler: {r.status_code} {r.text[:200]}")
    except requests.RequestException as e:
        log(f"Telegram nicht erreichbar: {e}")


def tg_updates(offset: int) -> list:
    r = requests.get(f"{TG_API}/getUpdates", params={
        "offset": offset, "timeout": 0,
        "allowed_updates": json.dumps(["message", "channel_post"]),
    }, timeout=20)
    r.raise_for_status()
    return r.json().get("result", [])


def owner_id(state: dict):
    return OWNER_CHAT_ID or None


# ---------------------------------------------------------------------------
# Signal aus Telegram-Text lesen
# ---------------------------------------------------------------------------

NUM = r"([0-9]+(?:[.,][0-9]+)?)"
RE_HEAD = re.compile(r"(\d+[MH])\s+SIGNAL\s+(BUY|SELL)\s*(?:\(([^)]*)\))?\s*-\s*([A-Z0-9]+)", re.I)
RE_ENTRY = re.compile(r"Einstieg[^0-9]*" + NUM, re.I)
RE_SL = re.compile(r"Stop\s*Loss[^:]*:[^0-9]*" + NUM, re.I)
RE_TP = re.compile(r"Take\s*Profit[^:]*:[^0-9]*" + NUM, re.I)
RE_RSI = re.compile(r"RSI[^:]*:[^0-9]*" + NUM, re.I)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_signal(text: str):
    """Erkennt Nachrichten wie:
    🚨 15M SIGNAL BUY (Re-Test) - BTCUSD 🚨
    - Einstieg: 63174.54 $
    - Stop Loss (SL): 63144.54 $
    - Take Profit (TP): 63219.54 $
    Ergebnis-Meldungen ("TAKE PROFIT ERREICHT") werden absichtlich ignoriert."""
    if not text:
        return None
    t = text.replace("*", "").replace("_", "")
    if "ERREICHT" in t.upper():
        return None
    head, e, sl, tp = RE_HEAD.search(t), RE_ENTRY.search(t), RE_SL.search(t), RE_TP.search(t)
    if not (head and e and sl and tp):
        return None

    symbol = head.group(4).upper()
    if symbol.endswith("USD"):
        symbol += "T"  # BTCUSD -> BTCUSDT (Binance-Schreibweise)

    sig = {
        "timeframe": head.group(1).lower(),
        "side": head.group(2).upper(),
        "category": (head.group(3) or "").strip(),
        "symbol": symbol,
        "entry": _num(e.group(1)),
        "sl": _num(sl.group(1)),
        "tp": _num(tp.group(1)),
    }
    rsi = RE_RSI.search(t)
    sig["rsi"] = _num(rsi.group(1)) if rsi else None
    # Plausibilität: SL und TP müssen auf der richtigen Seite liegen
    if sig["side"] == "BUY" and not (sig["sl"] < sig["entry"] < sig["tp"]):
        return None
    if sig["side"] == "SELL" and not (sig["tp"] < sig["entry"] < sig["sl"]):
        return None
    return sig


# ---------------------------------------------------------------------------
# Trades eröffnen / schließen
# ---------------------------------------------------------------------------

def fmt(price: float) -> str:
    if price < 10:
        return f"{price:.5f}"
    if price < 1000:
        return f"{price:.3f}"
    return f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def money(v: float, sign: bool = False) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if sign:
        return ("+" if v >= 0 else "−") + s + " $"
    return ("−" if v < 0 else "") + s + " $"


def short(sym: str) -> str:
    return sym[:-1] if sym.endswith("USDT") else sym


def open_position(state: dict, sig: dict, source: str, at_ms: int):
    """Eröffnet RÜCKWIRKEND zum Zeitpunkt at_ms (wann das Signal kam) – so, als hätte
    der Bot sofort gehandelt. SL/TP werden ab dieser Minute geprüft.
    Gibt (Position, None) oder (None, Grund) zurück."""
    if state["paused"]:
        return None, "Bot ist pausiert (/start zum Fortsetzen)"
    if len(state["open"]) >= MAX_OPEN_POSITIONS:
        return None, f"schon {MAX_OPEN_POSITIONS} Trades offen"
    for p in state["open"]:
        if (p["symbol"], p["side"], p["timeframe"], p["signal_entry"]) == \
           (sig["symbol"], sig["side"], sig["timeframe"], sig["entry"]):
            return None, "gleiches Signal ist schon offen"

    try:
        got = get_price_at(sig["symbol"], at_ms)
    except Exception as e:
        return None, f"kein Kurs für {sig['symbol']} ({e})"
    if not got:
        return None, f"kein Kurs für {sig['symbol']}"
    price, candle_close_ms = got

    # Lag der Kurs zum Signalzeitpunkt schon jenseits von SL/TP, war das Signal nicht handelbar
    if sig["side"] == "BUY" and not (sig["sl"] < price < sig["tp"]):
        return None, f"Kurs {fmt(price)} lag schon außerhalb von SL/TP"
    if sig["side"] == "SELL" and not (sig["tp"] < price < sig["sl"]):
        return None, f"Kurs {fmt(price)} lag schon außerhalb von SL/TP"

    risk_amount = state["balance"] * state["risk_percent"] / 100
    qty = risk_amount / abs(price - sig["sl"])
    max_qty = state["balance"] * MAX_LEVERAGE / price
    capped = qty > max_qty
    qty = min(qty, max_qty)
    notional = qty * price
    fee_open = notional * FEE_PERCENT / 100

    pos = {
        "id": uuid.uuid4().hex[:6],
        "symbol": sig["symbol"],
        "timeframe": sig["timeframe"],
        "category": sig["category"],
        "category_key": category_key(sig["category"]),
        "rsi": sig.get("rsi"),
        "side": sig["side"],
        "signal_entry": sig["entry"],
        "entry": price,
        "sl": sig["sl"],
        "tp": sig["tp"],
        "qty": qty,
        "notional": notional,
        "risk": qty * abs(price - sig["sl"]),
        "fee_open": fee_open,
        "opened_at": datetime.fromtimestamp(candle_close_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "opened_ms": candle_close_ms,
        # ab der Minute NACH dem Einstieg prüfen
        "checked_until_ms": candle_close_ms + 1,
        "source": source,
        "capped": capped,
    }
    state["balance"] -= fee_open
    state["open"].append(pos)
    return pos, None


def close_position(state: dict, pos: dict, exit_price: float, reason: str) -> dict:
    direction = 1 if pos["side"] == "BUY" else -1
    gross = (exit_price - pos["entry"]) * pos["qty"] * direction
    fee_close = exit_price * pos["qty"] * FEE_PERCENT / 100
    pnl = gross - pos["fee_open"] - fee_close
    state["balance"] += gross - fee_close

    closed = dict(pos)
    closed.update({
        "exit": exit_price,
        "reason": reason,
        "pnl": pnl,
        "r_multiple": pnl / pos["risk"] if pos["risk"] else 0,
        "closed_at": now_iso(),
        "balance_after": state["balance"],
    })
    for k in ("checked_until_ms",):
        closed.pop(k, None)
    state["open"] = [p for p in state["open"] if p["id"] != pos["id"]]
    state["closed"].append(closed)
    return closed


def check_open_positions(state: dict) -> list:
    """Prüft jede offene Position Minute für Minute auf SL/TP. Gibt geschlossene Trades zurück."""
    results = []
    for pos in list(state["open"]):
        try:
            candles = get_minute_candles(pos["symbol"], pos["checked_until_ms"])
        except Exception as e:
            log(f"Kerzen für {pos['symbol']} nicht abrufbar: {e}")
            continue

        done = None
        for open_ms, close_ms, high, low, close in candles:
            if pos["side"] == "BUY":
                hit_sl, hit_tp = low <= pos["sl"], high >= pos["tp"]
            else:
                hit_sl, hit_tp = high >= pos["sl"], low <= pos["tp"]
            # Beide in derselben Minute? Vorsichtig rechnen -> SL zählt
            if hit_sl:
                done = close_position(state, pos, pos["sl"], "SL")
            elif hit_tp:
                done = close_position(state, pos, pos["tp"], "TP")
            elif MAX_TRADE_HOURS and close_ms - pos["opened_ms"] >= MAX_TRADE_HOURS * 3_600_000:
                done = close_position(state, pos, close, "ZEIT")
            if done:
                break
            pos["checked_until_ms"] = close_ms + 1

        if done:
            results.append(done)
    return results


# ---------------------------------------------------------------------------
# Nachrichten-Texte
# ---------------------------------------------------------------------------

def side_icon(side: str) -> str:
    return "🟢 LONG" if side == "BUY" else "🔴 SHORT"


def msg_opened(p: dict) -> str:
    slip = p["entry"] - p["signal_entry"]
    extra = "\n⚠️ Größe durch Hebel-Limit begrenzt" if p["capped"] else ""
    return (
        f"📥 <b>Trade eröffnet</b> #{p['id']}\n"
        f"{side_icon(p['side'])} <b>{short(p['symbol'])}</b> · {p['timeframe'].upper()} {html.escape(p['category'])}\n\n"
        f"Einstieg: <b>{fmt(p['entry'])}</b> (Signal {fmt(p['signal_entry'])}, Abw. {fmt(abs(slip))})\n"
        f"SL: {fmt(p['sl'])} · TP: {fmt(p['tp'])}\n"
        f"Größe: {p['qty']:.6g} ({money(p['notional'])})\n"
        f"Risiko: {money(p['risk'])}{extra}"
        + (f"\n\n💡 <b>Warum?</b> {html.escape(why_text(p.get('category_key', ''), p['side'], p.get('rsi')))}"
           if WHY.get((p.get("category_key", ""), p["side"])) else "")
    )


def msg_closed(c: dict, state: dict) -> str:
    icon = {"TP": "✅", "SL": "❌", "ZEIT": "⏱", "MANUELL": "✋"}.get(c["reason"], "•")
    label = {"TP": "Take Profit", "SL": "Stop Loss", "ZEIT": "Zeitlimit", "MANUELL": "Manuell"}[c["reason"]]
    return (
        f"{icon} <b>{label}</b> #{c['id']} – {short(c['symbol'])} {side_icon(c['side'])}\n\n"
        f"Einstieg {fmt(c['entry'])} → Ausstieg {fmt(c['exit'])}\n"
        f"Ergebnis: <b>{money(c['pnl'], True)}</b> ({c['r_multiple']:+.2f} R)\n"
        f"Kontostand: <b>{money(state['balance'])}</b>"
    )


def stats_text(state: dict) -> str:
    closed = state["closed"]
    if not closed:
        return "📊 Noch keine abgeschlossenen Trades."
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] <= 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = -sum(c["pnl"] for c in losses)
    pf = (gross_win / gross_loss) if gross_loss else float("inf")
    total = state["balance"] - state["start_balance"]

    # größter Rückgang vom Höchststand
    peak, max_dd, bal = state["start_balance"], 0.0, state["start_balance"]
    for c in closed:
        bal += c["pnl"]
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)

    lines = [
        "📊 <b>Statistik</b>\n",
        f"Trades: {len(closed)} · Gewinnquote: <b>{len(wins) / len(closed) * 100:.0f} %</b>",
        f"Gesamt: <b>{money(total, True)}</b> ({total / state['start_balance'] * 100:+.2f} %)",
        f"Profit-Faktor: {pf:.2f}" if pf != float("inf") else "Profit-Faktor: ∞",
        f"Ø Gewinn: {money(gross_win / len(wins)) if wins else '–'} · Ø Verlust: {money(-gross_loss / len(losses)) if losses else '–'}",
        f"Max. Rückgang: {max_dd:.1f} %",
        "\n<b>Nach Strategie</b>",
    ]
    by_sym = {}
    for c in closed:
        k = f"{short(c['symbol'])} {c['category']} {c['timeframe'].upper()}"
        s = by_sym.setdefault(k, [0, 0, 0.0])
        s[0] += 1
        s[1] += c["pnl"] > 0
        s[2] += c["pnl"]
    for k, (n, w, p) in sorted(by_sym.items(), key=lambda x: -x[1][2]):
        lines.append(f"{k}: {n} Trades, {w / n * 100:.0f} % · {money(p, True)}")
    return "\n".join(lines)


def status_text(state: dict) -> str:
    unreal = 0.0
    rows = []
    for p in state["open"]:
        try:
            price = get_price(p["symbol"])
            d = 1 if p["side"] == "BUY" else -1
            u = (price - p["entry"]) * p["qty"] * d
            unreal += u
            rows.append(f"#{p['id']} {short(p['symbol'])} {side_icon(p['side'])}: {fmt(price)} · {money(u, True)}")
        except Exception:
            rows.append(f"#{p['id']} {short(p['symbol'])}: Kurs nicht abrufbar")
    total = state["balance"] - state["start_balance"]
    paused = "\n⏸ <b>Pausiert</b> – neue Signale werden ignoriert" if state["paused"] else ""
    txt = (
        f"💼 <b>Konto</b>{paused}\n\n"
        f"Kontostand: <b>{money(state['balance'])}</b>\n"
        f"Mit offenen Trades: {money(state['balance'] + unreal)}\n"
        f"Seit Start: {money(total, True)} ({total / state['start_balance'] * 100:+.2f} %)\n"
        f"Risiko pro Trade: {state['risk_percent']:g} %\n"
        f"Offene Trades: {len(state['open'])}/{MAX_OPEN_POSITIONS}\n"
        f"Gehandelt wird: " + (", ".join(
            f"{short(s)} {'/'.join(CATEGORY_NAMES.get(c, c) for c in cs)}"
            for s, cs in TRADE_FILTER.items()) if TRADE_FILTER else "alle Signale")
    )
    if rows:
        txt += "\n\n" + "\n".join(rows)
    return txt


def open_text(state: dict) -> str:
    if not state["open"]:
        return "Keine offenen Trades."
    out = ["📂 <b>Offene Trades</b>\n"]
    for p in state["open"]:
        out.append(
            f"#{p['id']} {side_icon(p['side'])} <b>{short(p['symbol'])}</b> {p['timeframe'].upper()}\n"
            f"  Einstieg {fmt(p['entry'])} · SL {fmt(p['sl'])} · TP {fmt(p['tp'])}\n"
            f"  seit {p['opened_at'][5:16].replace('T', ' ')} UTC"
        )
    return "\n".join(out)


def history_text(state: dict) -> str:
    if not state["closed"]:
        return "Noch keine abgeschlossenen Trades."
    out = ["🧾 <b>Letzte Trades</b>\n"]
    icons = {"TP": "✅", "SL": "❌", "ZEIT": "⏱", "MANUELL": "✋"}
    for c in reversed(state["closed"][-10:]):
        out.append(f"{icons.get(c['reason'], '•')} {c['closed_at'][5:16].replace('T', ' ')} "
                   f"{short(c['symbol'])} {c['side']} · {money(c['pnl'], True)}")
    return "\n".join(out)


HELP = (
    "🤖 <b>Paper Trading Bot</b>\n\n"
    "Ich lese die Signale deines Signal-Bots automatisch (oder weitergeleitete Signal-Nachrichten) "
    "und handle sie mit virtuellem Geld – rückwirkend ab dem Moment des Signals.\n\n"
    "/status – Kontostand & offene Trades live\n"
    "/offen – offene Trades mit SL/TP\n"
    "/verlauf – letzte 10 Trades\n"
    "/statistik – Gewinnquote, Profit-Faktor, je Markt\n"
    "/schliessen ID – Trade zum Marktpreis schließen\n"
    "/risiko 1.5 – Risiko pro Trade in %\n"
    "/pause – keine neuen Trades · /start – weiter\n"
    "/reset JA – Konto auf Start zurücksetzen\n\n"
    "⏱ Ich arbeite im 5-Minuten-Takt – Antworten kommen also mit kurzer Verzögerung."
)


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------

def handle_command(state: dict, chat_id, text: str):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/hilfe", "/help"):
        if cmd == "/start" and state["paused"]:
            state["paused"] = False
            tg_send(chat_id, "▶️ Weiter geht's – neue Signale werden wieder gehandelt.")
            return
        tg_send(chat_id, HELP)
    elif cmd == "/status":
        tg_send(chat_id, status_text(state))
    elif cmd == "/offen":
        tg_send(chat_id, open_text(state))
    elif cmd == "/verlauf":
        tg_send(chat_id, history_text(state))
    elif cmd == "/statistik":
        tg_send(chat_id, stats_text(state))
    elif cmd == "/pause":
        state["paused"] = True
        tg_send(chat_id, "⏸ Pausiert. Offene Trades laufen weiter, neue Signale werden ignoriert.\n/start zum Fortsetzen.")
    elif cmd == "/risiko":
        try:
            v = float(arg.replace(",", "."))
            if not 0.1 <= v <= 10:
                raise ValueError
            state["risk_percent"] = v
            tg_send(chat_id, f"✅ Risiko pro Trade: {v:g} %")
        except ValueError:
            tg_send(chat_id, "Bitte so: /risiko 1.5 (erlaubt 0,1 bis 10)")
    elif cmd == "/schliessen":
        pos = next((p for p in state["open"] if p["id"] == arg.lstrip("#")), None)
        if not pos:
            tg_send(chat_id, "Trade nicht gefunden. IDs siehst du mit /offen")
            return
        try:
            price = get_price(pos["symbol"])
        except Exception as e:
            tg_send(chat_id, f"Kurs nicht abrufbar: {e}")
            return
        c = close_position(state, pos, price, "MANUELL")
        tg_send(chat_id, msg_closed(c, state))
    elif cmd == "/reset":
        if arg.upper() != "JA":
            tg_send(chat_id, "⚠️ Löscht alle Trades und setzt das Konto zurück.\nZum Bestätigen: /reset JA")
            return
        keep = {k: state[k] for k in ("telegram_offset", "seen_channels")}
        state.clear()
        state.update(new_state())
        state.update(keep)
        tg_send(chat_id, f"🔄 Konto zurückgesetzt auf {money(START_BALANCE)}.")
    else:
        tg_send(chat_id, "Unbekannter Befehl. /hilfe zeigt alle Befehle.")


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def process_signal_text(state: dict, text: str, msg_date: int, source: str):
    sig = parse_signal(text)
    if not sig:
        return
    owner = owner_id(state)
    if not signal_allowed(sig["symbol"], sig["category"]):
        tg_send(owner, f"⏭ Signal {short(sig['symbol'])} {html.escape(sig['category'])} nicht gehandelt: "
                       f"durch Filter ausgeschlossen (TRADE_FILTER in paper_bot.py)")
        return
    age_min = (time.time() - msg_date) / 60
    if age_min > MAX_SIGNAL_AGE_MIN:
        log(f"Signal zu alt ({age_min:.0f} min) – übersprungen")
        tg_send(owner, f"⏭ Signal {short(sig['symbol'])} {sig['side']} übersprungen: "
                       f"schon {age_min:.0f} Minuten alt")
        return
    pos, reason = open_position(state, sig, source, int(msg_date * 1000))
    if pos:
        log(f"Eröffnet: {pos['symbol']} {pos['side']} @ {pos['entry']}")
        tg_send(owner, msg_opened(pos))
    else:
        log(f"Nicht eröffnet: {reason}")
        tg_send(owner, f"⏭ Signal {short(sig['symbol'])} {sig['side']} nicht gehandelt: {html.escape(reason)}")


def _raw_json(path: str):
    url = f"https://raw.githubusercontent.com/{SIGNAL_REPO}/{SIGNAL_BRANCH}/{path}"
    r = requests.get(url, params={"t": int(time.time())}, timeout=15)
    r.raise_for_status()
    return r.json()


def process_repo_signals(state: dict):
    """Liest neue Signale aus open_trades.json + trade_history.json des Signal-Bots."""
    if not SIGNAL_REPO:
        return
    signals = []
    try:
        signals += list(_raw_json("open_trades.json").values())
    except Exception as e:
        log(f"open_trades.json nicht lesbar: {e}")
    try:
        h = _raw_json("trade_history.json")
        signals += h.get("entries", []) if isinstance(h, dict) else h
    except Exception as e:
        log(f"trade_history.json nicht lesbar: {e}")

    seen = set(state["seen_signals"])
    fresh = []
    for s in signals:
        try:
            key = f"{s['symbol']}_{s['interval']}_{s['category']}_{s['candle_time']}"
            if key in seen:
                continue
            seen.add(key)
            state["seen_signals"].append(key)
            t = datetime.fromisoformat(str(s["candle_time"]).replace("Z", "+00:00"))
            age_min = (time.time() - t.timestamp()) / 60
            if age_min > MAX_SIGNAL_AGE_MIN:
                continue  # alte Einträge (z. B. beim ersten Start) still überspringen
            fresh.append((t.timestamp(), s))
        except (KeyError, ValueError, TypeError) as e:
            log(f"Unlesbarer Signal-Eintrag übersprungen: {e}")

    for ts, s in sorted(fresh, key=lambda x: x[0]):
        sig = {
            "timeframe": s["interval"].lower(),
            "side": s["type"].upper(),
            "category": CATEGORY_NAMES.get(s["category"], s["category"]),
            "symbol": s["symbol"].upper(),
            "entry": float(s["entry"]),
            "sl": float(s["sl"]),
            "tp": float(s["tp"]),
            "rsi": float(s["rsi"]) if s.get("rsi") is not None else None,
        }
        if not signal_allowed(s["symbol"], s["category"]):
            log(f"Gefiltert: {s['symbol']} {s['interval']} {s['category']}")
            continue
        ok_buy = sig["side"] == "BUY" and sig["sl"] < sig["entry"] < sig["tp"]
        ok_sell = sig["side"] == "SELL" and sig["tp"] < sig["entry"] < sig["sl"]
        if not (ok_buy or ok_sell):
            continue
        pos, reason = open_position(state, sig, "Signal-Bot", int(ts * 1000))
        if pos:
            log(f"Eröffnet: {pos['symbol']} {pos['side']} @ {pos['entry']}")
            tg_send(owner_id(state), msg_opened(pos))
        else:
            log(f"Nicht eröffnet: {reason}")
            tg_send(owner_id(state), f"⏭ Signal {short(sig['symbol'])} {sig['side']} nicht gehandelt: {html.escape(reason)}")

    state["seen_signals"] = state["seen_signals"][-1500:]


def process_updates(state: dict):
    updates = tg_updates(state["telegram_offset"])
    for u in updates:
        state["telegram_offset"] = u["update_id"] + 1

        if "channel_post" in u:
            m = u["channel_post"]
            ch = str(m["chat"]["id"])
            if ch not in state["seen_channels"]:
                state["seen_channels"].append(ch)
            if SIGNAL_CHANNEL_ID and ch != SIGNAL_CHANNEL_ID:
                continue
            process_signal_text(state, m.get("text", ""), m["date"], f"Kanal {ch}")

        elif "message" in u:
            m = u["message"]
            chat_id = str(m["chat"]["id"])
            text = m.get("text", "") or m.get("caption", "")

            if not OWNER_CHAT_ID:
                # Ersteinrichtung: Chat-ID verraten, sonst nichts tun
                tg_send(chat_id, (
                    f"👋 Deine Chat-ID ist <code>{chat_id}</code>\n\n"
                    "Trage sie in GitHub als Secret <b>OWNER_CHAT_ID</b> ein. "
                    "Danach reagiere ich nur noch auf dich."
                ))
                continue
            if chat_id != OWNER_CHAT_ID:
                continue  # Fremde ignorieren

            if text.startswith("/"):
                handle_command(state, chat_id, text)
            else:
                # weitergeleitete Signale: Zeitpunkt der Original-Nachricht verwenden
                date = m.get("forward_date") or (m.get("forward_origin") or {}).get("date") or m["date"]
                if parse_signal(text):
                    process_signal_text(state, text, date, "weitergeleitet")
                else:
                    tg_send(chat_id, "Das sieht nicht nach einem Signal aus. /hilfe zeigt die Befehle.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("PAPER_BOT_TOKEN fehlt (GitHub Secret).")

    state = load_state()

    try:
        process_updates(state)
    except Exception as e:
        log(f"Telegram-Updates fehlgeschlagen: {e}")

    try:
        process_repo_signals(state)
    except Exception as e:
        log(f"Signale aus dem Repo fehlgeschlagen: {e}")

    for c in check_open_positions(state):
        log(f"Geschlossen: {c['symbol']} {c['reason']} {c['pnl']:+.2f}")
        tg_send(owner_id(state), msg_closed(c, state))

    save_state(state)


if __name__ == "__main__":
    main()
