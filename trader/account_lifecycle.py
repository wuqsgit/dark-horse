"""Safety-critical lifecycle operations for configured trading accounts."""

from __future__ import annotations

import time
import uuid

from shared.accounts import (
    account_exchange_config,
    account_open_position_count,
    delete_account,
    prepare_account_deletion,
)
from shared.db import reset_account_context, set_account_context
from trader.exchange import BinanceFutures


class AccountDeletionError(RuntimeError):
    """The account was preserved because exchange cleanup was not confirmed."""


def _position_label(position: dict) -> str:
    symbol = str(position.get("symbol") or "?").upper()
    position_side = str(position.get("positionSide") or "BOTH").upper()
    return f"{symbol}/{position_side}"


def _order_symbols(orders) -> set[str]:
    return {
        str(order.get("symbol") or "").upper()
        for order in (orders or [])
        if order.get("symbol")
    }


def close_positions_and_delete_account(
    account_id: int,
    *,
    exchange_factory=BinanceFutures,
    close_attempts: int = 3,
    retry_delay_seconds: float = 0.25,
) -> dict:
    """Freeze entries, flatten Binance futures, cancel orders, then delete.

    The credential row is removed only after Binance confirms that positions
    and both regular/algo orders are empty. On failure the account remains in
    the database with every entry switch disabled so the operation is safely
    retryable.
    """
    account_id = int(account_id)
    account = prepare_account_deletion(account_id)
    config = account_exchange_config(account)
    has_credentials = bool(config.get("api_key") and config.get("api_secret"))
    if not has_credentials:
        local_open = account_open_position_count(account_id)
        if local_open:
            raise AccountDeletionError(
                f"账户缺少 Binance API 凭据，无法市价平掉本地记录的 {local_open} 个持仓；"
                "账户已停止新开仓并保留，请补充凭据后重试"
            )
        delete_account(account_id)
        return {
            "account_id": account_id,
            "closed": 0,
            "closed_symbols": [],
            "cancelled_regular_orders": 0,
            "cancelled_algo_orders": 0,
            "exchange_verified": False,
            "warnings": ["账户未配置 API 凭据；本地无持仓，未连接交易所"],
        }

    account_token = set_account_context(account_id)
    exchange = None
    try:
        exchange = exchange_factory(
            config=config,
            account_id=account_id,
            account_name=account.get("name"),
        )
        initial_positions = list(exchange.get_positions() or [])
        regular_orders = list(exchange.get_open_orders() or [])
        algo_orders = list(exchange.get_open_algo_orders() or [])
        symbols = (
            {str(pos.get("symbol") or "").upper() for pos in initial_positions}
            | _order_symbols(regular_orders)
            | _order_symbols(algo_orders)
        )
        symbols.discard("")

        # Cancel ordinary orders first so a pending entry cannot create fresh
        # exposure while deletion is in progress. Protective algo stops remain
        # active until Binance confirms every position is flat.
        for symbol in sorted(_order_symbols(regular_orders)):
            exchange.cancel_all_open_orders(symbol)

        close_orders = []
        remaining = initial_positions
        close_errors = []
        for attempt in range(max(1, int(close_attempts))):
            if not remaining:
                break
            for index, position in enumerate(remaining):
                symbol = str(position.get("symbol") or "").upper()
                side = "SELL" if str(position.get("side")).upper() == "LONG" else "BUY"
                quantity = abs(float(position.get("quantity") or 0))
                if not symbol or quantity <= 0:
                    continue
                client_order_id = (
                    f"acctdel-{account_id}-{attempt}-{index}-{uuid.uuid4().hex[:8]}"
                )[:36]
                try:
                    order = exchange.close_position_market(
                        symbol,
                        side,
                        quantity,
                        client_order_id=client_order_id,
                        position_side=position.get("positionSide"),
                    )
                    close_orders.append(
                        {
                            "symbol": symbol,
                            "position_side": position.get("positionSide"),
                            "quantity": quantity,
                            "order_id": (order or {}).get("orderId"),
                        }
                    )
                except Exception as exc:
                    close_errors.append(f"{_position_label(position)}: {exc}")
            if retry_delay_seconds > 0:
                time.sleep(float(retry_delay_seconds))
            remaining = list(exchange.get_positions() or [])

        if remaining:
            labels = ", ".join(_position_label(pos) for pos in remaining)
            detail = f"；最近错误：{close_errors[-1]}" if close_errors else ""
            raise AccountDeletionError(
                f"交易所仍有持仓 {labels}，账户未删除{detail}"
            )

        # A final sweep removes orders created before/during the flatten. Algo
        # orders are cancelled only now, after their protected position is flat.
        post_regular = list(exchange.get_open_orders() or [])
        post_algo = list(exchange.get_open_algo_orders() or [])
        symbols |= _order_symbols(post_regular) | _order_symbols(post_algo)
        for symbol in sorted(symbols):
            exchange.cancel_all_open_orders(symbol)
            exchange.cancel_all_algo_orders(symbol)

        remaining_positions = list(exchange.get_positions() or [])
        remaining_regular = list(exchange.get_open_orders() or [])
        remaining_algo = list(exchange.get_open_algo_orders() or [])
        if remaining_positions or remaining_regular or remaining_algo:
            raise AccountDeletionError(
                "交易所清理确认失败："
                f"持仓 {len(remaining_positions)}，普通挂单 {len(remaining_regular)}，"
                f"条件挂单 {len(remaining_algo)}；账户未删除"
            )

        warnings = []
        try:
            # Persist the exchange's realized PnL before credentials disappear.
            from trader.runner import fetch_and_store_income

            fetch_and_store_income(exchange, days_back=1)
        except Exception as exc:
            warnings.append(f"平仓成功，但成交历史同步失败，历史记录可能不完整：{exc}")

        delete_account(account_id, exchange_flat_verified=True)
        return {
            "account_id": account_id,
            "closed": len(initial_positions),
            "closed_symbols": sorted(
                {_position_label(position) for position in initial_positions}
            ),
            "close_orders": close_orders,
            "cancelled_regular_orders": len(regular_orders),
            "cancelled_algo_orders": len(algo_orders),
            "exchange_verified": True,
            "warnings": warnings,
        }
    except AccountDeletionError:
        raise
    except Exception as exc:
        raise AccountDeletionError(
            f"删除账户失败：{exc}；账户已停止新开仓并保留，可修复后重试"
        ) from exc
    finally:
        if exchange is not None:
            try:
                exchange.close()
            except Exception:
                pass
        reset_account_context(account_token)
