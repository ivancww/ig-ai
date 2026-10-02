from ig_ai.database import Database
from ig_ai.decision import (
    BigWaveEngine,
    MarketRegimeEngine,
    NormalizedMarketState,
    ProfitProtectionEngine,
    RejectionEngine,
    StructureValidityEngine,
    TradingDecisionEngine,
    position_context,
    true_vwap,
)


def market_state(**overrides):
    values = {
        "instrument": "ig:NDX",
        "market": "US Tech 100",
        "epic": "IX.D.NASDAQ.IFMM.IP",
        "source": "IG_LIVE",
        "reference_time": "2026-10-02T10:00:00+00:00",
        "structure": {
            "1H": {"direction": "DOWN_STRUCTURE", "prior_direction": "DOWN"},
            "15M": {"direction": "DOWN_STRUCTURE"},
        },
        "technical": {
            "current_price": 100.0,
            "atr": 100.0,
            "support": 0.0,
            "resistance": 250.0,
            "macd": {"polarity": "negative", "relative_to_signal": "below"},
            "momentum_state": "strengthening",
            "volatility_state": "expanding",
        },
        "quality": {
            "candle_eligibility": "ELIGIBLE",
            "evidence_coverage": 1.0,
            "missing_data_flags": [],
        },
        "event": {"state": "UNAVAILABLE", "verified": False},
        "position": {"state": "NONE"},
        "direction": {"direction": "DOWN", "up_score": 10, "down_score": 75},
    }
    values.update(overrides)
    return NormalizedMarketState(**values)


def test_a_clean_bearish_trend_is_trend_down():
    result = MarketRegimeEngine().analyze(market_state())
    assert result["regime"] == "TREND_DOWN"


def test_b_bear_structure_lost_bull_not_confirmed_is_transition():
    state = market_state(
        structure={
            "1H": {
                "direction": "RANGE",
                "prior_direction": "DOWN",
                "failed_breakdown": True,
                "previous_lh_broken": True,
                "reclaim": True,
                "opposite_trend_confirmed": False,
            },
            "15M": {"direction": "UP_STRUCTURE"},
        }
    )
    assert MarketRegimeEngine().analyze(state)["regime"] == "TRANSITION"
    validity = StructureValidityEngine().analyze(state, side="SHORT")
    assert validity["invalidation_state"] == "BEARISH_STRUCTURE_LOST"
    assert validity["opposite_trend_confirmation_state"] == "NOT_CONFIRMED"


def test_c_confirmed_bull_reversal_is_reversal():
    state = market_state(
        structure={
            "1H": {
                "direction": "UP_STRUCTURE",
                "prior_direction": "DOWN",
                "reversal_confirmed": True,
                "opposite_trend_confirmed": True,
            },
            "15M": {"direction": "UP_STRUCTURE"},
        },
        direction={"direction": "UP"},
    )
    assert MarketRegimeEngine().analyze(state)["regime"] == "REVERSAL"


def test_d_increasing_rebound_is_strengthening_rejection():
    tests = [
        {"rebound_distance": value, "close_back": True, "follow_through_distance": value, "structure_result": "HELD"}
        for value in (20, 30, 45, 70)
    ]
    assert RejectionEngine().analyze(tests, side="SUPPORT", atr=100)["state"] == "STRENGTHENING_REJECTION"


def test_e_shrinking_rebound_is_possible_absorption():
    tests = [
        {"rebound_distance": value, "close_back": True, "follow_through_distance": value, "structure_result": "HELD"}
        for value in (200, 120, 60, 20)
    ]
    assert RejectionEngine().analyze(tests, side="SUPPORT", atr=100)["state"] == "POSSIBLE_ABSORPTION"


def test_f_profitable_short_near_support_activates_profit_protection():
    position = position_context(side="SHORT", entry=180, current_price=100, mfe=90, mae=5)
    state = market_state(
        position=position,
        technical={
            "current_price": 100,
            "atr": 100,
            "support": 80,
            "resistance": 250,
            "momentum_state": "weakening",
        },
    )
    validity = StructureValidityEngine().analyze(state, side="SHORT")
    assert ProfitProtectionEngine().analyze(state, validity)["state"] == "PROFIT_PROTECTION"


def test_g_aligned_continuation_with_room_is_runner_eligible():
    state = market_state(
        position=position_context(side="SHORT", entry=250, current_price=100, mfe=160, mae=10),
        technical={
            "current_price": 100,
            "atr": 100,
            "support": -100,
            "resistance": 250,
            "momentum_state": "strengthening",
            "volatility_state": "expanding",
        },
    )
    assert BigWaveEngine().analyze(state)["qualification_state"] == "RUNNER_ELIGIBLE"


def test_h_verified_event_context_reduces_confidence_and_sets_event_risk():
    state = market_state(
        event={"state": "ACTIVE", "source": "VERIFIED_TEST", "verified": True}
    )
    decision = TradingDecisionEngine().analyze(state)
    assert decision["regime"]["regime"] == "EVENT_DRIVEN"
    assert decision["regime"]["technical_confidence_state"] == "REDUCED"
    assert decision["decision_state"] == "EVENT_RISK"


def test_i_bearish_bias_can_still_be_do_not_chase():
    state = market_state(
        technical={
            "current_price": 100,
            "atr": 100,
            "support": 55,
            "resistance": 250,
            "macd": {"polarity": "negative", "relative_to_signal": "below"},
            "momentum_state": "weakening",
        }
    )
    decision = TradingDecisionEngine().analyze(state)
    assert decision["market_bias"] == "BEARISH"
    assert decision["primary_action_state"] == "DO_NOT_CHASE"


def test_j_structure_invalidated_does_not_confirm_long_or_keep_short_valid():
    state = market_state(
        structure={
            "1H": {
                "direction": "RANGE",
                "prior_direction": "DOWN",
                "failed_breakdown": True,
                "previous_lh_broken": True,
                "reclaim": True,
                "opposite_trend_confirmed": False,
            },
            "15M": {"direction": "UP_STRUCTURE"},
        }
    )
    decision = TradingDecisionEngine().analyze(state)
    assert decision["decision_state"] == "STRUCTURE_INVALIDATED"
    assert decision["structure_validity"]["original_structure_valid"] is False
    assert decision["structure_validity"]["opposite_trend_confirmation_state"] == "NOT_CONFIRMED"


def test_k_insufficient_data_has_no_fabricated_score_or_probability():
    state = market_state(
        structure={"1H": {}, "15M": {}},
        quality={"candle_eligibility": "UNKNOWN", "evidence_coverage": 0.0},
        direction={},
    )
    decision = TradingDecisionEngine().analyze(state)
    assert decision["regime"]["regime"] == "UNCLEAR"
    assert decision["entry_quality"]["LONG"]["entry_quality_score"] is None
    assert decision["probability"] is None


def test_l_vwap_is_unavailable_without_reliable_volume():
    decision = TradingDecisionEngine().analyze(market_state())
    assert decision["vwap"] == {
        "state": "UNAVAILABLE",
        "value": None,
        "reason": "reliable traded volume unavailable",
    }
    assert true_vwap([(100, 10), (110, 10)])["state"] == "UNAVAILABLE"
    assert true_vwap([(100, 10), (110, 30)], reliable_traded_volume=True)["value"] == 107.5


def test_parameters_are_all_explicitly_provisional():
    metadata = TradingDecisionEngine().analyze(market_state())["parameter_metadata"]
    assert metadata
    assert {item["status"] for item in metadata.values()} == {"PROVISIONAL"}


def test_decision_telemetry_is_append_safe_and_keeps_future_expectancy_fields(tmp_path):
    database = Database(tmp_path / "decision.sqlite3")
    decision = TradingDecisionEngine().analyze(market_state())
    first = database.save_decision_telemetry(decision)
    second = database.save_decision_telemetry(decision)
    rows = database.get_decision_telemetry("ig:NDX")
    assert first == second
    assert len(rows) == 1
    assert {
        "mfe",
        "mae",
        "give_back_points",
        "give_back_ratio",
        "holding_duration",
        "risk_reward",
        "drawdown",
        "consecutive_losses",
        "time_of_day",
        "event_day",
    } <= rows[0].keys()
    database.close()
