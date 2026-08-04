from __future__ import annotations


class FixedAlphaPredictor:
    """Deterministic predictor for state-machine and execution replay tests."""

    def __init__(
        self,
        *,
        p_setup_success: float = 0.70,
        p_followthrough: float = 0.72,
        p_fakeout: float = 0.22,
        expected_r: float = 0.50,
    ):
        self.values = {
            "p_setup_success": float(p_setup_success),
            "p_followthrough": float(p_followthrough),
            "p_fakeout": float(p_fakeout),
            "expected_r": float(expected_r),
            "model_versions": {"replay": "fixed-v1"},
        }

    def __call__(self, payload: dict) -> dict:
        stage = str(payload.get("stage") or "setup").lower()
        return {
            **self.values,
            "max_position_factor": {
                "setup": 0.0,
                "trigger": 0.30,
                "acceptance": 0.70,
                "retest": 1.00,
            }.get(stage, 0.0),
        }
