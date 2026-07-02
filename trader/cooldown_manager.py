"""V3.0 交易冷却管理器
- 止损后进入冷却期（防止震荡行情连续止损）
- 连续止损自动延长冷却时间
- 每日最大止损次数限制
"""

import time
from datetime import datetime, timedelta, timezone
from shared.db import get_conn

# 冷却时间配置（秒）
COOLDOWN_AFTER_STOP = 6 * 3600       # 单次止损后冷却6小时
COOLDOWN_AFTER_2ND_STOP = 24 * 3600  # 连续2次止损后冷却24小时
MAX_STOPS_PER_DAY = 3                # 每日最多3次止损

def is_in_cooldown(symbol: str) -> tuple:
    """检查币种是否在冷却中
    Returns: (bool, reason, seconds_remaining)
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trade_cooldown WHERE symbol=?",
            (symbol,)
        ).fetchone()
        conn.close()
        
        if not row:
            return False, "", 0
        
        cooldown_until = row["cooldown_until"]
        if not cooldown_until:
            return False, "", 0
        
        if isinstance(cooldown_until, str):
            cooldown_until = datetime.fromisoformat(cooldown_until)
        
        now = datetime.now(timezone.utc)
        remaining = (cooldown_until - now).total_seconds()
        
        if remaining > 0:
            return True, row["reason"] or "cooling", int(remaining)
        
        return False, "", 0
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return False, "", 0


def record_stop(symbol: str, pnl: float = 0) -> None:
    """记录止损事件，更新冷却状态
    Args:
        symbol: 币种
        pnl: 止损亏损金额
    """
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        row = conn.execute(
            "SELECT * FROM trade_cooldown WHERE symbol=?",
            (symbol,)
        ).fetchone()
        
        if row:
            # 更新已有记录
            stop_count_24h = row["stop_count_24h"] or 0
            consecutive_stops = row["consecutive_stops"] or 0
            
            # 检查是否是今天第一次止损
            last_stop = row["last_stop_time"]
            if last_stop:
                if isinstance(last_stop, str):
                    last_stop = datetime.fromisoformat(last_stop)
                if last_stop >= today_start:
                    stop_count_24h += 1
                else:
                    stop_count_24h = 1  # 新的一天，重置
            else:
                stop_count_24h = 1
            
            consecutive_stops += 1
            
            # 根据连续止损次数决定冷却时间
            if consecutive_stops >= 2:
                cooldown_duration = COOLDOWN_AFTER_2ND_STOP
                reason = f"连续止损{consecutive_stops}次"
            else:
                cooldown_duration = COOLDOWN_AFTER_STOP
                reason = f"止损冷却{cooldown_duration//3600}h"
            
            cooldown_until = now + timedelta(seconds=cooldown_duration)
            
            conn.execute("""
                UPDATE trade_cooldown SET
                    last_stop_time = ?,
                    stop_count_24h = ?,
                    consecutive_stops = ?,
                    cooldown_until = ?,
                    reason = ?,
                    updated_at = ?
                WHERE symbol = ?
            """, (now.isoformat(), stop_count_24h, consecutive_stops,
                  cooldown_until.isoformat(), reason, now.isoformat(), symbol))
        else:
            # 新记录
            now_ts = now.isoformat()
            cooldown_until = now + timedelta(seconds=COOLDOWN_AFTER_STOP)
            reason = f"止损冷却{COOLDOWN_AFTER_STOP//3600}h"
            
            conn.execute("""
                INSERT OR REPLACE INTO trade_cooldown 
                (symbol, last_stop_time, stop_count_24h, consecutive_stops, 
                 cooldown_until, reason)
                VALUES (?, ?, 1, 1, ?, ?)
            """, (symbol, now_ts, cooldown_until.isoformat(), reason))
        
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        raise e


def record_profit(symbol: str, is_weak_exit: bool = False) -> None:
    """盈利后重置连续止损计数
    Args:
        symbol: 币种
        is_weak_exit: 是否是弱退出（是的话不重置计数）
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM trade_cooldown WHERE symbol=?",
            (symbol,)
        ).fetchone()
        
        if row:
            if not is_weak_exit:
                now = datetime.now(timezone.utc)
                cooldown_until = now + timedelta(seconds=1800)
                conn.execute("""
                    UPDATE trade_cooldown SET
                        consecutive_stops = 0,
                        updated_at = ?,
                        cooldown_until = ?
                    WHERE symbol = ?
                """, (now.isoformat(), cooldown_until.isoformat(), symbol))
        else:
            # 记录开仓后冷却(30分钟)
            now = datetime.now(timezone.utc)
            cooldown_until = now + timedelta(seconds=1800)
            conn.execute("""
                INSERT OR REPLACE INTO trade_cooldown 
                (symbol, last_stop_time, stop_count_24h, consecutive_stops, 
                 cooldown_until, reason)
                VALUES (?, ?, 0, 0, ?, ?)
            """, (symbol, now.isoformat(), cooldown_until.isoformat(), '开仓冷却30min'))
            conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except:
            pass


def get_daily_stop_count() -> dict:
    """获取当日各币种止损次数
    Returns: {symbol: count}
    """
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        rows = conn.execute(
            "SELECT symbol, stop_count_24h FROM trade_cooldown WHERE last_stop_time >= ?",
            (today_start.isoformat(),)
        ).fetchall()
        conn.close()
        
        return {r["symbol"]: r["stop_count_24h"] or 0 for r in rows}
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return {}


def is_daily_limit_reached(symbol: str = None) -> bool:
    """检查是否达到每日止损次数上限
    Args:
        symbol: None表示检查全局限制
    """
    stop_counts = get_daily_stop_count()
    
    if symbol and symbol in stop_counts:
        return stop_counts[symbol] >= MAX_STOPS_PER_DAY
    
    # 检查全局
    total_today = sum(stop_counts.values())
    return total_today >= MAX_STOPS_PER_DAY * 3  # 假设最多同时交易3个币


def cleanup_old_records(days: int = 7) -> None:
    """清理过期记录"""
    conn = get_conn()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        conn.execute(
            "DELETE FROM trade_cooldown WHERE updated_at < ?",
            (cutoff.isoformat(),)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except:
            pass