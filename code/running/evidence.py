"""The immutable record, flattened into field / value / what it means.

WHY NOT A JSON TREE. Every audit page used to end in `st.json(doc)`. That is
faithful and unreadable: the reader who most needs it is the one who does not
already know that `policy_margin` is measured against a different bar for a
starter than for a bench flier, and a collapsible tree tells them nothing. The
record has to stay verifiable field by field, so this flattens it and puts a
sentence beside each name instead of hiding it behind one.

WHAT IS AND IS NOT EXPANDED. Scalars and nested objects are walked. A list of
records is NOT -- it is reported by length and left to the typed table that
already renders it, because exploding 2,000 simulated bid rows into this view
would bury the twenty fields somebody is actually checking. Dicts keyed by
player id or week number are summarised the same way: those keys are data, not
field names, and glossing them would be nonsense.

A KEY WITH NO GLOSS STILL SHOWS, with an empty meaning. That is deliberate --
a field added upstream should appear here as an obvious gap rather than
silently vanish from the audit.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# What each field means, in the words of somebody who has not read the module.
# Keyed by LEAF name: the two record types share most of their vocabulary, and
# where they do it means the same thing in both.
# --------------------------------------------------------------------------
FIELD_GLOSS: dict[str, str] = {
    # --- record identity -------------------------------------------------
    "schema": "Version of the record format itself.",
    "at": "When this evaluation ran (epoch seconds).",
    "fingerprint": "Hash of the inputs. Identical inputs reuse the record rather than writing a second one.",
    "week": "NFL week this evaluation was priced for.",
    "league_id": "Sleeper league the run acted in.",
    "mode": "Which pass ran: patch (unfillable slot) > ros (ordinary upgrade) > block (deny an opponent) > ir_fill.",
    "dry_run": "True means nothing could reach Sleeper regardless of what cleared.",
    "submission_authorized": "Whether this run had live authority to send a transaction.",
    "duration_seconds": "Wall-clock time of the rebuild.",

    # --- the trigger -----------------------------------------------------
    "events": "The players whose news actually changed. One entry per trigger.",
    "reasons": "Why the watcher counted this as an event rather than noise.",
    "changes": "The exact fields that moved, before and after.",
    "before": "Value the provider carried on the previous poll.",
    "after": "Value the provider carries now.",
    "field": "Which provider field moved.",
    "pft": "Matching Pro Football Talk headlines. A title match only — a description match once linked Cooper Rush to Bijan Robinson.",
    "affected": "Every player the rebuild expanded to: the triggers plus their team/position room.",
    "categories": "Counts of what kind of signal fired.",
    "source_errors": "Feeds that failed on this poll. Any entry suppresses live authority.",
    "provider_at_trigger": "What Sleeper said about each player immediately before and at the trigger.",
    "points_before": "Weekly projection before the event.",
    "points_at_trigger": "Weekly projection at the moment the event fired.",
    "points_delta": "Change in the weekly projection. Often ~0 even for a season-ending injury — Sleeper does not propagate an absence into future weeks.",
    "status_before": "Injury designation before the event.",
    "status_at_trigger": "Injury designation at the event.",
    "news_updated_before": "Provider news timestamp before the event.",
    "news_updated_at_trigger": "Provider news timestamp at the event. A moved timestamp buys a read, never a revaluation.",
    "trending_top": "Most-added players league-wide. Archived for context; it can corroborate an event, never create one.",
    "pricing_status": "Whether this market snapshot is calibrated for bid pricing. It is not.",
    "captured_at": "When the market snapshot was taken.",
    "market_snapshot": "The crowd's behaviour at decision time, kept for context.",

    # --- timing ----------------------------------------------------------
    "timing": "The return-date pass: what is a rule, what is a reading, and what was thrown out.",
    "summary": "One-line count of what the timing pass did.",
    "deterministic": "Return weeks that come from a published rule, not from prose. Only these may change availability.",
    "advisory": "Players whose prose was read but whose dates cannot move a valuation.",
    "advisory_reviews": "What the local model read, kept as visible context only.",
    "model_reviewed": "Players the local model actually judged this run.",
    "quarantined": "Prose discarded because the structured feed contradicted it.",
    "cleared": "Timing verdicts retired because the underlying designation went away.",
    "bounds": "Earliest and latest week each man could return.",
    "return_week": "The week this player is judged likely to return.",
    "return_week_min": "Earliest week the rules permit. This is a floor, not a forecast.",
    "return_week_max": "Latest week the reporting supports.",
    "return_basis": "Where the date came from: a rule, a report, or a model reading.",
    "out_for_season": "A return date in the next calendar year, which means done for the year.",
    "timing_sentence": "The exact sentence the date was read out of.",
    "timing_actionable": "False means this verdict is context and may not move a number.",
    "timing_reported_at": "When the reporting behind the date was published.",
    "timing_subject_id": "The player the sentence is actually about.",
    "confidence": "The model's own stated confidence in its reading.",
    "advisory_return_week": "A return week read from prose. Advisory — it cannot override the rule-based floor.",
    "role_week": "The week a role change is expected to land.",
    "queued": "Players deferred to the scout queue rather than read now.",
    "batch": "Which queue batch this pulse drained.",
    "attention": "Players flagged for a human to look at.",
    "advisory_pending": "Prose reads still outstanding when the run finished.",

    # --- measured value change -------------------------------------------
    "event_deltas": "Per player: what the rebuild actually did to his value, and whether the event explains it.",
    "pre_ros": "Rest-of-season value before the rebuild.",
    "post_ros": "Rest-of-season value after the rebuild.",
    "delta_ros": "The change. This is measured, not asserted.",
    "by_week": "Which weeks the change landed in.",
    "causal_edge": "Why we may act: 'self' (he is the trigger) or 'successor-of' (his lead is). No edge means the move is not authorised, however good it looks.",
    "lead_id": "The man ahead of him whose absence he would inherit.",
    "complete": "Whether both sides of the before/after series were fully populated.",
    "series_complete": "Same check, on the candidate screen.",
    "ownership": "Who held him at decision time, frozen so it cannot be re-derived from today.",
    "acquisition_candidate": "Whether he was actually available to acquire. The rest are context.",
    "primary_event": "Whether he was a trigger himself or was pulled in by his room.",
    "expected_players": "How many players the rebuilt valuation covered.",

    # --- the screens -----------------------------------------------------
    "candidate_checks": "Every player the acquisition screen judged, and where each one stopped.",
    "rejections": "Older name for the same list.",
    "stage": "How far he got: fresh_table, pre_post, causal_delta, coverage, drop_pool, ros_comparison.",
    "outcome": "proposed or rejected.",
    "reason": "The deciding module's own words for why.",
    "on_waivers": "True means he must be claimed and paid for, not simply added.",
    "options": "Every add/drop pairing the simulator priced, including the ones that lost.",
    "verdict": "Which bar the pairing met or missed.",
    "selected": "Whether this pairing was the one taken.",
    "gain": "Change in our OPTIMAL STARTING LINEUP, averaged over simulated seasons. Not a difference of two season totals.",
    "se": "The simulator's own error on that comparison. Every hypothetical is scored on identical worlds, so this is a paired error.",
    "ceiling": "The p90 outcome. This is the bar a bench flier is judged on, since his average is beside the point.",
    "starter": "Whether he starts in the median simulated world. Decides which tail is read.",
    "noise_margin": "gain minus the noise bar. Negative means the gain is not distinguishable from simulation error.",
    "policy_margin": "How far past the policy bar it is — the starting-gain bar for a starter, the bench-ceiling bar for a bench piece.",
    "thresholds": "The bars in force for this run, recorded so a near-miss is never re-judged against today's settings.",
    "noise_multiple": "How many standard errors a gain must clear.",
    "starting_gain": "Minimum gain for a move into a starting slot.",
    "bench_ceiling": "Minimum ceiling for a bench flier.",
    "drop_floor": "Most value we will cut.",
    "skill_drop_limit": "How many skill-position drops one pass may make.",
    "coverage_floor": "Minimum bodies per position.",
    "direct_ros": "The season-total comparator: two players' weekly-model totals subtracted, over the same weeks the simulator priced. The paired figure decides inside the tiers; a gap past the veto refuses regardless.",
    "prefers_move": "Whether the season-total comparator points the same way as the simulator.",
    "verdict_tier": "agrees, flagged (recorded and shown amber) or refused (the gap is too large to spend on).",
    "veto_at": "How negative the season-total gap may be before the claim is refused.",
    "flag_at": "How negative the season-total gap may be before it is flagged.",
    "add_ros": "Simple rest-of-season total of the man coming in.",
    "drop_ros": "Simple rest-of-season total of the man going out.",
    "over_free": "The best free-agent alternative this claim has to beat.",
    "over_free_gain": "How much better the claim is than simply taking that free agent. At or below the bar the claim is refused: paying FAAB for a man something free beats is the one mistake this comparison exists to stop.",
    "over_free_se": "A conservative bound on that difference's own error. Recorded, never gated on.",
    "claim_over_free_min": "How much better than the best free agent a claim must be to cost FAAB.",
    "direct_ros_flag": "Where the season-total comparator starts being flagged.",
    "direct_ros_veto": "Where the season-total comparator refuses outright.",
    "screens": "Every screen's verdict on this claim, not just the one that refused it. Recorded in full so a reader can tell which bars were doing work.",
    "refused": "Whether this screen refused the claim.",
    "margin": "How far this screen's number sat from its threshold.",
    "threshold": "The bar this screen applied.",
    "detail": "This screen's own sentence about what it found.",

    # --- the two free-agent screens a pulse runs -------------------------
    "news_free_channel": "The causal screen's free-agent result: whether the event gives us standing to act on this man at all.",
    "ros_free_channel": "The canonical rest-of-season free-agent result, run on every pulse so the best free move is reachable the moment it appears rather than at the next scheduled pass.",
    "submitted": "What actually reached Sleeper, as opposed to what was proposed.",

    # --- the horizon a claim can reach -----------------------------------
    "claim_horizon": "When a claim built in this run would settle, and the first week it could be played in.",
    "priced_weeks": "The weeks this option was priced over. A claim does not resolve until the waiver run, so weeks ending before it are weeks nobody can receive.",
    "weeks_excluded": "Weeks left out because the claim settles after them.",
    "excluded_reason": "Why those weeks were left out.",
    "settlement_week": "The first NFL week a claim submitted now could be played in.",
    "settles_at": "When the waiver run that decides this claim happens.",
    "settles_label": "That settlement, as a day and time.",
    "settles_iso": "That settlement as a full timestamp.",
    "settlement_basis": "Where the settlement time came from: a live unlock, or the Wednesday calendar fallback.",
    "current_week": "Sleeper's own idea of the week when the run happened.",

    # --- what was actually on offer --------------------------------------
    "roster_first": "The injury and IR sweep this pulse ran BEFORE anything was priced. Reserve is three slots on top of the active roster, so an unswept IR is a roster cap that forces a drop for a slot we already had.",
    "blocked_by_lineup": "Men who could be reserved except that Sleeper still has them in a starting slot. Their presence is what makes this pulse run the lineup; an empty list means it did not.",
    "ir_after_lineup": "The second sweep, run only when the first was blocked by the lineup.",
    "drop_se": "The standard error on what cutting him costs. Two drop prices closer together than this are not a ranking.",
    "slots_passed_over": "The other roster slots this run could have turned over, and the best move against each. These lost on the value of the move; the slot is no longer chosen before the move is.",
    "claim_pool": "What the waiver pool was when this slate was built. Reported, never a gate.",
    "waiver_pool_size": "How many men a claim could have been made on at all.",
    "free_pool_size": "How many men could have been added for nothing instead.",
    "waiver_teams": "Whose players those were. On most days this is just the teams whose games have finished.",
    "waiver_by_pos": "The waiver pool broken down by position.",
    "wire_floor": "How much of each position the wire already supplies. A high percentage means depth there is a wasted roster spot -- and is why cutting a startable tight end can price near zero.",
    "ours": "Mean weekly points from our own starters at that position.",
    "wire_first": "Mean weekly points from the best man at that position in the pool this run priced against — the same floor the gains were measured over, not a separate survey of the wire.",
    "excluded": "How many candidates this run was considering were held out of that floor, so none of them is his own baseline.",
    "supplier": "Who supplies that floor in most weeks.",
    "supplier_weeks": "In how many of the priced weeks he is the one supplying it.",
    "wire_second": "And from the second best -- the shape, not just the level.",
    "pct": "The wire's best as a fraction of our own starters.",
    "event_delta": "The value change that authorised this proposal.",
    "why": "The deciding module's one-line summary of the move.",
    "real": "Whether this is a live move or a placeholder rung.",

    # --- drops and roster construction -----------------------------------
    "drop_checks": "Every incumbent tested as the outgoing player, and why each was or was not eligible.",
    "eligible": "Whether he could legally be dropped at that moment.",
    "drop_price": "What cutting him costs, priced on hold (mean plus upside), not on mean.",
    "drop_value": "Value of the man going out.",
    "add_value": "Value of the man coming in.",
    "control_checks": "The roster-construction gate on each pairing.",
    "roster_control_checks": "Construction checks applied to the run as a whole.",
    "coverage": "Bodies per position before and after, and which shortages the move relieves.",
    "coverage_priority": "0 relieves or preserves every floor; 1 is merely not worse. This ORDERS the ladder; it does not close the door.",
    "counts": "Bodies per position after the move.",
    "counts_before": "Bodies per position before the move.",
    "relieves": "Positions this move brings back up to the floor.",
    "meets_floor": "Whether every positional floor is satisfied.",
    "minimums": "Required bodies per position.",
    "short_after": "Positions still under the floor once the move is made.",
    "active_after": "Active roster size after the move.",
    "skill_lineup_fillable": "Whether a legal skill lineup can still be filled that week.",
    "specialists": "Kicker and defence counts, tracked apart from skill positions.",
    "roster": "Player ids on the active roster when the run began.",
    "roster_state": "Roster size, open spots, FAAB and time to kickoff at decision time.",
    "active": "Players on the active roster.",
    "maximum": "Active roster limit.",
    "open": "Empty active spots. Every one is a claim that needs no drop.",
    "hours_to_kickoff": "Hours to the next kickoff. Ordinary moves refuse inside the blackout and say so.",
    "blackout": "Set when the run refused because kickoff was too close.",
    "control_block": "Set when a roster control stopped the run.",

    # --- the slate -------------------------------------------------------
    "plans": "The waiver ladder as submitted, in order.",
    "claim_plans": "Same, on the news channel.",
    "claim_proposals": "Claims that cleared every gate.",
    "free_plans": "Moves available with no claim and no bid.",
    "free_proposals": "Free moves that cleared every gate.",
    "free_audit": "The free-agent half of the pass, frozen before waivers were repriced.",
    "claims_audit": "The waiver half, priced against the roster the free pass would leave.",
    "sequence_basis": "Which roster the claims were priced against. Without this the same slot is counted empty twice.",
    "free_gated": "Whether the free-agent channel could send anything.",
    "claims_gated": "Whether the waiver channel could send anything.",
    "gated": "Whether the transaction gate was shut. Closed means everything here is a proposal.",
    "free_submitted": "Free moves actually sent.",
    "claims_submitted": "Claims actually sent.",
    "claims": "The claims in this run.",
    "group_id": "Which capacity pool this claim belongs to.",
    "group_capacity": "How many of this group can actually settle, bounded by open roster spots.",
    "capacity": "Settlements this group can absorb.",
    "kind": "What the group is for: an open spot, a skill swap, or a defence replacement.",
    "exposure": "Worst-case FAAB this group can cost.",
    "worst_case_faab": "Most the whole slate could spend if every claim won.",
    "priority": "Rung on the ladder. Sleeper reaches them in order and the first winner takes the slot.",
    "seq": "Position in the planned order.",
    "submit_order": "Order the claims were actually sent in, so equal bids resolve as intended.",
    "claim_kind": "Whether this rung fills an open spot, swaps a skill player, or streams a defence.",
    "defence_group_error": "Why the defence-streaming group could not be built, if it could not.",
    "valuation_computed": "Vintage of the valuation this run priced on. Joins the record back to the value table.",
    "faab_left": "FAAB remaining when the run began.",
    "faab": "FAAB remaining.",

    # --- the bid ---------------------------------------------------------
    "bid": "What we offered.",
    "bid_quote": "The whole pricing argument behind that number.",
    "bid_gain": "Lineup gain used to price the bid.",
    "bid_se": "Error on that gain.",
    "p_win": "Chance of winning at this bid.",
    "curve": "The objective at every legal dollar, 0 up to the cap. This is the whole argument, not a summary of it.",
    "net_if_won": "What the claim is worth after paying the bid — counted only in the worlds where we win.",
    "expected_utility": "P(win) times net_if_won. The thing being maximised.",
    "near_optimal": "Band within a couple of percent of the peak. We take the cheapest bid in it.",
    "reservation_bid": "Most this gain can justify. Past it the claim is worth less than the budget it eats.",
    "dollar_price": "What one FAAB dollar costs in lineup points.",
    "shadow_price": "The realised price of a dollar, not the floor constant — the number actually charged.",
    "points_per_dollar": "Lineup points a dollar buys. The bid ceiling is gain divided by this.",
    "floor_constant": "Lower bound on the dollar price.",
    "basis": "How the dollar price was arrived at.",
    "expected_highest": "Top bid we expect from the rest of the league.",
    "expected_highest_plus_one": "One dollar more than that — what it would take to beat the field.",
    "highest_quantiles": "Distribution of the top rival bid.",
    "opponent_field": "Per-manager forecast of who else wants him and what they would pay. Private; never published.",
    "opponents": "The eleven other managers, each with a modelled need and bid distribution.",
    "manager": "Which manager.",
    "manager_id": "Sleeper user id.",
    "roster_id": "Sleeper roster id.",
    "p_claim": "Chance this manager claims him at all.",
    "bid_p50": "Median bid we expect from him.",
    "bid_p75": "Upper-quartile bid.",
    "bid_p90": "Top-decile bid.",
    "pmf": "Full bid distribution for this manager.",
    "max_pmf": "Distribution of the highest bid across the field.",
    "win_curve": "P(win) at each dollar, from the opponent model.",
    "waiver_position": "Tie-break order if bids are equal.",
    "our_waiver_position": "Our own tie-break order.",
    "need": "How much this player would actually improve that manager's lineup.",
    "starts": "Weeks he would start for them.",
    "evidence": "How many observations sit behind each forecast. Thin counts mean a pooled number was used.",
    "manager_samples": "Claims by this manager in the history.",
    "manager_bid_samples": "Bids by this manager with an amount recorded.",
    "week_position_samples": "Observations for this week bucket and position.",
    "need_samples": "Observations behind the need estimate.",
    "league_rate": "League-wide base rate, used when a manager is thin.",
    "bid_samples": "Bids behind the amount distribution.",
    "positive_bid_probability": "How often this manager bids above zero.",
    "validation": "Held-out check that the per-manager model beats the pooled one. If it fails, pooled is used.",
    "demand_brier": "Brier score on who claims. Lower is better.",
    "pooled_demand_brier": "Same score for the pooled fallback.",
    "bid_crps": "Scoring rule on the bid amount. Lower is better.",
    "pooled_bid_crps": "Same for the pooled fallback.",
    "bid_mean_mae": "Mean absolute error on the bid amount.",
    "pooled_bid_mean_mae": "Same for the pooled fallback.",
    "demand_pass": "Whether the demand half beat pooled.",
    "bid_distribution_pass": "Whether the amount half beat pooled.",
    "passes": "Whether the per-manager model is used at all this run.",
    "training_rows": "Claims the model was fitted on.",
    "bid_rows": "Rows with a bid amount recorded.",
    "demand_rows": "Rows used for the demand half.",
    "model_version": "Version of the bid model.",
    "version": "Version of the opponent model.",
    "available": "Whether the opponent model could be built. False falls back to pooled history.",
    "position": "Position being bid on.",
    "candidate_id": "The player being bid on.",
    "method": "How this figure was arrived at.",

    # --- pipeline provenance ---------------------------------------------
    "capture": "Projection capture step. Must run before the export or the export is a cache hit.",
    "capture_ok": "Whether the capture succeeded.",
    "export": "Weekly export from the local NFL model.",
    "export_ok": "Whether the export succeeded.",
    "model": "The model run that produced this week's simulated means.",
    "action": "Everything the rebuild did once the pulse decided an event was real.",

    # --- identity --------------------------------------------------------
    "player_id": "Sleeper player id. Names are not unique — 'Josh Allen' is a quarterback and a linebacker.",
    "name": "Player name.",
    "pos": "Position.",
    "team": "NFL team.",
    "add": "Player coming in.",
    "drop": "Player going out.",
    "add_id": "Id of the player coming in.",
    "drop_id": "Id of the player going out.",
    "drop_name": "Name of the player going out.",
    "drop_pos": "Position of the player going out.",
    "drop_team": "Team of the player going out.",
    "injury_status": "Designation at decision time.",
    "bye": "Bye week.",
    "weeks": "Weeks still being priced.",
    "channel": "free agent (add now) or waivers (claim and pay).",
    "status": "Designation, or the run's result, depending on where it sits.",

    # --- draft-board leftovers on player rows ----------------------------
    "adp_ffc": "Average draft position from the locked FFC 2QB board.",
    "adp_live": "Live FFC average draft position.",
    "adp_sleeper_2qb": "Sleeper's own 2QB average draft position.",
    "adp_stdev": "Spread of draft position, used for contingency runs.",
    "blend_pts": "Blended projected points from the draft board.",
    "blend_rank": "Blended rank from the draft board.",
    "ecr": "FantasyPros expert consensus rank, superflex.",
    "expert_pts": "Expert projected points.",
    "proj_pts": "Projected points.",
    "pos_rank": "Rank within position.",
    "value_rank": "Rank by value over replacement.",
    "vorp": "Value over a 2QB replacement player.",
    "tier": "Draft board tier.",
}

# Keys whose children are data, not field names -- player ids, week numbers,
# manager ids. Glossing "13432" would be nonsense, so these are summarised.
KEYED_BY_DATA = {
    "event_deltas", "provider_at_trigger", "bounds", "quarantined", "by_week",
    "pmf", "max_pmf", "counts", "counts_before", "weights", "minimums",
    "highest_quantiles", "categories", "filtered", "coverage_floor",
    "waiver_by_pos",
}

# Fields holding an epoch. Rendered at 4 significant figures an epoch reads
# "1.79e+09", which is not a check anybody can perform.
EPOCH_FIELDS = {"at", "computed", "captured_at", "valuation_computed", "judged_at",
                "last_poll", "polled_at", "timing_reported_at"}


def _scalar(value, field: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if field in EPOCH_FIELDS and isinstance(value, (int, float)) and value > 1e9:
        from robo import news_audit
        return f"{news_audit.local_time(value)}  ({value:.0f})"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= 300 else text[:297] + "…"


def explain_fields(doc: dict, prefix: str = "", depth: int = 0,
                   max_depth: int = 4) -> list[dict]:
    """Flatten a record to [{field, path, value, meaning}], deepest-first order kept."""
    out: list[dict] = []
    for key, value in (doc or {}).items():
        if str(key).startswith("_"):
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        row = {"field": str(key), "path": path,
               "meaning": FIELD_GLOSS.get(str(key), "")}
        if isinstance(value, dict):
            if key in KEYED_BY_DATA or depth >= max_depth or _data_keyed(value):
                out.append({**row, "value": f"{len(value)} entr{'y' if len(value) == 1 else 'ies'}"})
            else:
                out.append({**row, "value": ""})
                out += explain_fields(value, path, depth + 1, max_depth)
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                out.append({**row, "value": f"{len(value)} record{'' if len(value) == 1 else 's'}"})
            else:
                shown = ", ".join(_scalar(v, str(key)) for v in value[:8])
                more = f" … (+{len(value) - 8})" if len(value) > 8 else ""
                out.append({**row, "value": (shown + more) if value else "(empty)"})
        else:
            out.append({**row, "value": _scalar(value, str(key))})
    return out


def _data_keyed(value: dict) -> bool:
    """A dict whose keys are ids or week numbers carries data, not fields."""
    keys = list(value)[:8]
    return bool(keys) and all(str(k).lstrip("-").isdigit() for k in keys)


def missing_gloss(doc: dict) -> list[str]:
    return sorted({r["field"] for r in explain_fields(doc) if not r["meaning"]})


def _main() -> None:
    """Gloss coverage against a live record of each type."""
    from robo import news_audit, waiver_audit
    total_rows = total_missing = 0
    for label, docs in (("news_events", news_audit.events(limit=1)),
                        ("waiver_events", waiver_audit.events(limit=1))):
        if not docs:
            print(f"{label}: no record on disk")
            continue
        rows = explain_fields(docs[0])
        missing = [r["field"] for r in rows if not r["meaning"]]
        total_rows += len(rows)
        total_missing += len(missing)
        pct = 100.0 * (len(rows) - len(missing)) / max(1, len(rows))
        print(f"{label}: {len(rows)} fields, {pct:.1f}% glossed")
        if missing:
            print("  missing: " + ", ".join(sorted(set(missing))))
    pct = 100.0 * (total_rows - total_missing) / max(1, total_rows)
    print(f"\noverall {pct:.1f}% ({total_rows - total_missing}/{total_rows})")


if __name__ == "__main__":
    _main()
