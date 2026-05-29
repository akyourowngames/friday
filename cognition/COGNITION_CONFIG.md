# KING Cognition Config

Control surface for KING's cognition substrate (situational awareness, life
cadence, episode stitching, and the proactive intelligence engine).

Edit the values below to tune behavior. Nothing in the cognition code hardcodes
these numbers, phrases, or thresholds; everything is read from this file. Keys
are simple `- key: value` pairs grouped under `## Section` headings. Unknown
keys are ignored, so this file is forward compatible.

The design rules this file enforces:

- Proactivity defaults to silence. A candidate must clear every gate to speak.
- Thresholds are adaptive: the bar rises right after KING speaks and decays back
  over time, so KING never fires twice in a row.
- Relevance, novelty, and situational fit are all measured by embeddings and
  arithmetic, never by keyword tables or regex.

## Situation

- busy_event_window_seconds: 120
- busy_event_count_for_full_load: 8
- idle_seconds_for_available: 600
- rapid_turn_window_seconds: 90
- rapid_turn_count_for_engaged: 4
- load_decay_half_life_seconds: 300
- min_availability_to_speak: 0.45
- max_load_to_speak: 0.6

## Cadence

- buckets_per_day: 24
- ema_alpha: 0.3
- min_observations_for_signal: 5
- deviation_min_strength: 0.4
- expected_activity_floor: 0.15
- max_tracked_nodes: 200

## Episodes

- time_gap_minutes: 180
- similarity_link_threshold: 0.55
- min_episode_size: 2
- max_episode_size: 40
- max_episodes: 60
- title_max_chars: 80

## Proactive

- base_confidence_threshold: 0.35
- threshold_rise_after_speak: 0.20
- threshold_decay_half_life_seconds: 1800
- daily_budget: 8
- novelty_suppression_similarity: 0.85
- relevance_weight: 0.4
- freshness_weight: 0.2
- importance_weight: 0.2
- situational_weight: 0.2
- freshness_half_life_seconds: 86400
- annoyance_penalty_per_dismissal: 0.15
- max_queue_size: 50

## Memory Signals

- enabled: true
- importance_floor: 0.7
- recent_days: 14
- max_candidates: 5
- commitment_extraction_enabled: true
- commitment_max_tokens: 500
- commitment_lookback: 40
- project_alerts_enabled: true
- project_alert_severity_floor: 0.4
- project_alert_max: 5

## Verification

- `python -m unittest tests.test_cognition`
- `python -m maintenance.daily --dry-run`
