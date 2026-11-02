# bluesky-his08-bot

Autonomous Bluesky bot that operates the account **[@his08.bsky.social](https://bsky.app/profile/his08.bsky.social)** on its own. No VPS, no always-on process: GitHub Actions is the scheduler, the repository is the state store.

Every run (cron, 15 min) the bot behaves like the account owner:

- reads the timeline and trending feeds;
- likes and replies with contextual AI, following human profiles;
- publishes scheduled posts plus trending-driven posts;
- updates its own bio and avatar.

## Anti-ban / humanisation

Automation that looks like automation gets banned. The `anti_spam` and `safety` blocks in `config.json` drive a rate governor with jitter, per-action budgets and a spam filter applied to every outbound action.

## Files

| File | Role |
|---|---|
| `tick.py` | single execution unit — one tick, all actions |
| `bot_ops.py` | operations layer: structured JSON logs, sliding-window metrics, latencies, error rates, repeated-failure detection, health check, tuning history |
| `healthcheck.py` | remote watchdog — re-dispatches the bot when the last run failed, when no run completed in 3 h, or when the heartbeat is older than 6 h (with anti-loop cooldown) |
| `bluesky_agent.py`, `ai_engine.py` | ATProto client and contextual text engine |
| `audit_replies.py` | audits published replies |
| `tests.py` | test suite |

## Workflows

`workflow.yml` (tick) · `healthcheck.yml` · `audit.yml` · `auth-check.yml` · `diag.yml`

## Configuration

`config.json` holds `handle`, `schedule`, `engagement`, `comment_replies`, `engagement_v2`, `autonomy`, `autonomy_plus`, `images`, `unfollow`, `anti_spam`, `safety`, `tuning`.

Credentials must be supplied through **GitHub Actions secrets** — never committed.

## Stack

Python · GitHub Actions · ATProto
