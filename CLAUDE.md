# CLAUDE.md

@AGENTS.md

The runbook above is the whole contract. Two reminders that matter most in Claude Code sessions:

- Never render before the user approves the cut table, never upload before the user approves the
  metadata (gates 1 and 2 in `AGENTS.md`).
- Long waits (Transcribe ~10 min, MediaConvert ~8 min per 20 output minutes) are polled by
  re-reading the log file, not by `sleep` chains in one shell call.

The same procedure is available as an Agent Skill at `skills/meetup-video-publish/SKILL.md`
(`.claude/skills/` links to it).
