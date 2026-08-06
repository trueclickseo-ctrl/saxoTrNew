# -*- coding: utf-8 -*-
with open('e:/saxobackup/SaxoTrader/files_kwaseem/atos_runner.py', 'r', encoding='utf-8') as f:
    text = f.read()

import re

# 1. Update risk.py imports
text = re.sub(
    r'from atos\.risk import \(\n    RiskEngine, get_risk_capital, record_fill, get_day_start_equity,\n    daily_loss_cap_breached, kill_switch_active, commission_sek,\n    STARTING_CAPITAL_SEK,\n\)',
    'from atos.risk import (\n    RiskEngine, get_risk_capital, get_available_cash, get_total_equity,\n    record_fill, get_day_start_equity,\n    daily_loss_cap_breached, kill_switch_active, commission_sek,\n    STARTING_CAPITAL_SEK,\n)',
    text
)

# 2. Add open_trades fetch earlier and pass to loss cap & start equity
text = text.replace(
    'day_start = get_day_start_equity()\n\n    if daily_loss_cap_breached():',
    'open_trades = db.get_open_trades()\n    day_start = get_day_start_equity(open_trades)\n\n    if daily_loss_cap_breached(open_trades):'
)

text = text.replace(
    'open_trades   = db.get_open_trades()',
    '# open_trades already fetched'
)

text = text.replace(
    'if not daily_loss_cap_breached():',
    'if not daily_loss_cap_breached(open_trades):'
)

# 3. Trailing Stop logic
old_exit = '''            if not hit_stop:
                continue
            exit_reason = "stop_loss"'''
new_exit = '''            exit_reason = None
            if hit_stop:
                exit_reason = "stop_loss"
                
            # Check trailing stop (track highest price since entry)
            current_high = last_row.get('High', last_row['Close'])
            trailing_high = trade.get('trailing_stop_high') or trade.get('entry_price', 0)
            if current_high > trailing_high:
                trailing_high = current_high
            
            atr_val = last_row.get('atr', 0)
            if pd.notna(atr_val) and atr_val > 0 and trailing_high > 0:
                trailing_stop_price = trailing_high - 2.0 * atr_val
                if last_row['Close'] <= trailing_stop_price:
                    exit_reason = 'trailing_stop'
            
            if exit_reason is None:
                continue'''
text = text.replace(old_exit, new_exit)

# 4. Signal inserts
old_signal_exit = '''        db.insert_signal({
            "signal_date": date.today().isoformat(), "market_group": mkt,
            "ticker": ticker, "final_score": decision.score if decision else 0,
            "d1_trend": decision.d1_trend if decision else 0,
            "d2_momentum": decision.d2_momentum if decision else 0,
            "d3_breakout": decision.d3_breakout if decision else 0,
            "d4_mean_revert": decision.d4_mean_revert if decision else 0,
            "d5_volume": decision.d5_volume if decision else 0,
            "action": "EXIT", "executed": 1 if order_ok else 0,
            "block_reason": None if order_ok else "order_failed",
        })'''
new_signal_exit = '''        db.insert_signal({
            "signal_date": date.today().isoformat(), "market_group": mkt,
            "ticker": ticker, "final_score": decision.score if decision else 0,
            "d1_trend": decision.d1_trend if decision else 0,
            "d2_momentum": decision.d2_momentum if decision else 0,
            "d3_breakout": decision.d3_breakout if decision else 0,
            "d4_mean_revert": decision.d4_mean_revert if decision else 0,
            "d5_volume": decision.d5_volume if decision else 0,
            "d6_smart_money": getattr(decision, 'd6_smart_money', 0) if decision else 0,
            "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0) if decision else 0,
            "d8_regime": getattr(decision, 'd8_regime', 0) if decision else 0,
            "regime": getattr(decision, 'regime', 'unknown') if decision else 'unknown',
            "action": "EXIT", "executed": 1 if order_ok else 0,
            "block_reason": None if order_ok else "order_failed",
        })'''
text = text.replace(old_signal_exit, new_signal_exit)

old_signal_buy = '''            db.insert_signal({
                "signal_date": date.today().isoformat(), "market_group": mkt,
                "ticker": ticker, "final_score": decision.score,
                "d1_trend": decision.d1_trend, "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "action": "BUY",
                "executed": 1 if approval["approved"] else 0,
                "block_reason": None if approval["approved"] else approval["reason"],
            })'''
new_signal_buy = '''            db.insert_signal({
                "signal_date": date.today().isoformat(), "market_group": mkt,
                "ticker": ticker, "final_score": decision.score,
                "d1_trend": decision.d1_trend,
                "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "d6_smart_money": getattr(decision, 'd6_smart_money', 0) if decision else 0,
                "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0) if decision else 0,
                "d8_regime": getattr(decision, 'd8_regime', 0) if decision else 0,
                "regime": getattr(decision, 'regime', 'unknown') if decision else 'unknown',
                "action": "BUY",
                "executed": 1 if approval["approved"] else 0,
                "block_reason": None if approval["approved"] else approval["reason"],
            })'''
text = text.replace(old_signal_buy, new_signal_buy)

# 5. Trade insert
old_trade = '''            trade_id = db.insert_trade({
                "market_group": mkt, "ticker": ticker, "direction": "BUY",
                "entry_date": date.today().isoformat(),
                "entry_price": entry_price,
                "shares": shares,
                "commission_sek": comm_sek,
                "entry_score": decision.score,
                "d1_trend": decision.d1_trend,
                "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "stop_price": stop_p,
            })'''
new_trade = '''            trade_id = db.insert_trade({
                "market_group": mkt, "ticker": ticker, "direction": "BUY",
                "entry_date": date.today().isoformat(),
                "entry_price": entry_price,
                "shares": shares,
                "commission_sek": comm_sek,
                "entry_score": decision.score,
                "d1_trend": decision.d1_trend,
                "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "d6_smart_money": getattr(decision, 'd6_smart_money', 0) if decision else 0,
                "d7_mom_quality": getattr(decision, 'd7_mom_quality', 0) if decision else 0,
                "d8_regime": getattr(decision, 'd8_regime', 0) if decision else 0,
                "stop_price": stop_p,
                "trailing_stop_high": entry_price,
                "regime_at_entry": getattr(decision, 'regime', 'unknown') if decision else 'unknown',
            })'''
text = text.replace(old_trade, new_trade)

# 6. total_equity
text = text.replace('total_equity    = get_risk_capital()', 'total_equity    = get_total_equity(open_trades_now)')

# 7. Print banner definition
old_banner_def = '''def print_banner(total_equity: float, day_start: float, open_count: int,
                 weights: dict, todays_actions: list, learning_result: dict):'''
new_banner_def = '''def print_banner(total_equity: float, day_start: float, open_count: int,
                 weights: dict, todays_actions: list, learning_result: dict,
                 current_regime: str = "unknown"):'''
text = text.replace(old_banner_def, new_banner_def)

old_banner_str = '''╠══════════════════════════════════════════════════════════════════╣
║  ALGORITHM WEIGHTS  (learned from {num_t} trades)               ║
║  Trend      {format_weight_bar(w.get('w_trend',1.0))}  {w.get('w_trend',1.0):.3f}                           ║
║  Momentum   {format_weight_bar(w.get('w_momentum',1.0))}  {w.get('w_momentum',1.0):.3f}                           ║
║  Breakout   {format_weight_bar(w.get('w_breakout',1.0))}  {w.get('w_breakout',1.0):.3f}                           ║
║  Mean Rev   {format_weight_bar(w.get('w_mean_revert',1.0))}  {w.get('w_mean_revert',1.0):.3f}                           ║
║  Volume     {format_weight_bar(w.get('w_volume',1.0))}  {w.get('w_volume',1.0):.3f}                           ║'''
new_banner_str = '''╠══════════════════════════════════════════════════════════════════╣
║  MARKET REGIME: {current_regime:<49}║
╠══════════════════════════════════════════════════════════════════╣
║  ALGORITHM WEIGHTS  (learned from {num_t} trades)               ║
║  Trend      {format_weight_bar(w.get('w_trend',1.0))}  {w.get('w_trend',1.0):.3f}                           ║
║  Momentum   {format_weight_bar(w.get('w_momentum',1.0))}  {w.get('w_momentum',1.0):.3f}                           ║
║  Breakout   {format_weight_bar(w.get('w_breakout',1.0))}  {w.get('w_breakout',1.0):.3f}                           ║
║  Mean Rev   {format_weight_bar(w.get('w_mean_revert',1.0))}  {w.get('w_mean_revert',1.0):.3f}                           ║
║  Volume     {format_weight_bar(w.get('w_volume',1.0))}  {w.get('w_volume',1.0):.3f}                           ║
║  SmartMoney {format_weight_bar(w.get('w_smart_money',1.0))}  {w.get('w_smart_money',1.0):.3f}                           ║
║  MomQuality {format_weight_bar(w.get('w_mom_quality',1.0))}  {w.get('w_mom_quality',1.0):.3f}                           ║
║  Regime     {format_weight_bar(w.get('w_regime',1.0))}  {w.get('w_regime',1.0):.3f}                           ║'''
text = text.replace(old_banner_str, new_banner_str)

old_print_call = '''    print_banner(total_equity, day_start, len(open_trades_now),
                 weights, todays_actions, learning_result)'''
new_print_call = '''    current_regime = next(iter(decisions.values())).regime if decisions else "unknown"
    print_banner(total_equity, day_start, len(open_trades_now),
                 weights, todays_actions, learning_result, current_regime)'''
text = text.replace(old_print_call, new_print_call)

with open('e:/saxobackup/SaxoTrader/files_kwaseem/atos_runner.py', 'w', encoding='utf-8') as f:
    f.write(text)
