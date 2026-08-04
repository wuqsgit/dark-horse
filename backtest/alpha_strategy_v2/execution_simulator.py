from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SimulatedFill:
    side: str
    quantity: float
    raw_price: float
    fill_price: float
    fee: float
    slippage: float


class ExecutionSimulator:
    def __init__(self, *, fee_rate: float = 0.0005, slippage_bps: float = 5):
        self.fee_rate = max(0.0, float(fee_rate))
        self.slippage_bps = max(0.0, float(slippage_bps))

    def fill(self, side: str, quantity: float, price: float) -> SimulatedFill:
        direction = 1 if str(side).upper() == "BUY" else -1
        slippage = float(price) * self.slippage_bps / 10_000
        fill_price = float(price) + direction * slippage
        fee = abs(float(quantity) * fill_price) * self.fee_rate
        return SimulatedFill(
            side=str(side).upper(),
            quantity=float(quantity),
            raw_price=float(price),
            fill_price=fill_price,
            fee=fee,
            slippage=slippage,
        )
