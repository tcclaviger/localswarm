#!/usr/bin/env bash
# swarm_plan.sh — have a local agent plan the job, validate the plan, and get it reviewed cold.
#
#   swarm_plan.sh --ws DIR --intake FILE [--spec REL] [--repo REL] [--effort high] [--round N]
#
# Writes   DIR/tickets/plan.json                      (the planner's ticket list, copied in by this
#                                                     script after validation — agents cannot
#                                                     write tickets/; the planner writes
#                                                     DIR/workingtemp/plan/plan-N.json)
#          DIR/workingtemp/reviews/plan-review-N.md  (independent review, Verdict: PASS|CHANGES_REQUIRED)
# Exit: 0 plan valid (read the review before importing), 1 planner failed or plan invalid, 2 bad args.
# Next step (orchestrator): read both, fix or re-plan (pass hints via --hints FILE), then
#   swarm_board.py init --ws DIR --goal ... && swarm_board.py import --ws DIR DIR/tickets/plan.json
set -u
HERE=$(cd "$(dirname "$0")" && pwd); T=$(dirname "$HERE")/templates
ws=""; intake=""; spec=""; repo=""; effort=high; round=1; hints=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ws) ws=$2; shift 2 ;;        --intake) intake=$2; shift 2 ;;
    --spec) spec=$2; shift 2 ;;    --repo) repo=$2; shift 2 ;;
    --effort) effort=$2; shift 2 ;; --round) round=$2; shift 2 ;;
    --hints) hints=$2; shift 2 ;;
    *) echo "swarm_plan: unknown arg $1" >&2; exit 2 ;;
  esac
done
[ -n "$ws" ] && [ -f "$intake" ] || { echo "swarm_plan: need --ws and an existing --intake file" >&2; exit 2; }
ws=$(readlink -f "$ws"); intake=$(readlink -f "$intake")
W=$ws/workingtemp; mkdir -p "$ws/tickets" "$W/prompts" "$W/reviews" "$W/plan"
draft=$W/plan/plan-$round.json            # the planner (a sandboxed agent) writes here
plan=$ws/tickets/plan.json                 # this script copies it here after validation
specp=${spec:+$ws/$spec}; repop=${repo:+$ws/$repo}

hv=""; [ -n "$hints" ] && [ -f "$hints" ] && hv="@$hints"
python3 "$HERE/fill_template.py" "$T/planner.md" "$W/prompts/plan-$round.md" \
  "WS=$ws" "INTAKE=$intake" "SPEC=${specp:-(none yet)}" "REPO=${repop:-(none)}" "REPORT_FILE=$draft"
[ -n "$hv" ] && { printf '\nGuidance from the orchestrator for this planning round:\n'; cat "$hints"; } >> "$W/prompts/plan-$round.md"
"$HERE/swarm_run.sh" --ws "$ws" --name "plan-$round" --effort "$effort" --prompt "$W/prompts/plan-$round.md" --timeout 3600 \
  || { echo "swarm_plan: planner run failed"; exit 1; }
[ -s "$draft" ] || { echo "swarm_plan: the planner wrote no plan at $draft (see $W/logs/plan-$round.result)"; exit 1; }

# Validate against the board rules without touching a real board.
tmpws=$(mktemp -d)
python3 "$HERE/swarm_board.py" init --ws "$tmpws" --goal validate > /dev/null
if ! python3 "$HERE/swarm_board.py" import --ws "$tmpws" "$draft"; then
  rm -rf "$tmpws"; echo "swarm_plan: plan is invalid (see above); re-plan with --hints"; exit 1
fi
rm -rf "$tmpws"
cp "$draft" "$plan"

python3 "$HERE/fill_template.py" "$T/plan-review.md" "$W/prompts/plan-review-$round.md" \
  "INTAKE=$intake" "WORKTREE=$plan" "REVIEW_FILE=$W/reviews/plan-review-$round.md"
"$HERE/swarm_run.sh" --ws "$ws" --role review --name "plan-review-$round" --effort high \
  --prompt "$W/prompts/plan-review-$round.md" --cwd "$W/reviews" --rw "$W/reviews" --ro "$plan" --ro "$intake" \
  ${specp:+--ro "$specp"} --timeout 2400 || echo "swarm_plan: plan review run failed (plan itself is valid)"
echo "plan:   $plan"
echo "review: $W/reviews/plan-review-$round.md"
grep -m1 -i 'verdict' "$W/reviews/plan-review-$round.md"
exit 0
