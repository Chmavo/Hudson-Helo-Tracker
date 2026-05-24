# Threat Model — Hoboken Helo Accountability Tracker

Single-operator civic monitoring project. No server infrastructure, no user
accounts, no inbound API surface. Dashboard is a static site on GitHub Pages.

## What we're protecting

| Asset | Why it matters |
|---|---|
| Filer PII (name, address, phone, email) | Appears verbatim in complaint letters signed by a real person |
| Resend API key | Allows sending email as our domain |
| Complaint accuracy | False claims against named businesses carry legal and reputational risk |
| Data branch integrity | Source of truth for all dashboard metrics and complaint evidence |

---

## Threats and mitigations

### ADS-B spoofing

**Threat:** Attacker with a software-defined radio broadcasts false aircraft
positions to either suppress real violations (by drowning them in noise) or
fabricate fake violations against a target.

**Mitigations:**
- Hard bounds: reject observations outside the bounding box or with
  implausible altitude/speed values.
- Temporal bounds: reject entire batches where the API's reported timestamp
  deviates >5 min from system clock.
- Flight reconstruction sanity check: implied speed between two consecutive
  observations of the same ICAO hex must be <250 kt. Failures → `confidence: low`.
- Low-confidence flights are excluded from complaint filing and from all
  headline counters on the dashboard.
- Every dashboard flight links to the raw ADS-B observations that produced it.

**Residual risk:** A determined attacker with a local SDR generating
plausible-looking positions within all validation bounds could inject data.
The per-flight sanity checks reduce but do not eliminate this risk for
long-duration injections.

---

### Secret leakage in commits

**Threat:** Filer PII or the Resend API key accidentally committed to the
public repo.

**Mitigations:**
- `.gitignore` excludes `.env`, `.env.*`, `*.pem`, `*.key`, `secrets/`,
  `credentials/`, and `flights.db` (database lives on the `data` branch only).
- Gitleaks runs as a required CI check on every push; blocks merge on detection.
- Stage 5 secrets are imported only in the complaints module (`kearny_zoning.py`
  etc.). Stages 1–4 code paths have zero imports of those names.
- A pre-commit hook (Stage 5 setup) blocks commits when `.env` is present
  in the working tree.

**Residual risk:** A sufficiently unusual encoding of a secret might evade the
default gitleaks ruleset. Mitigated in Stage 5 by adding custom rules for
the specific secret formats in use.

---

### Defamation / false attribution

**Threat:** A complaint letter names an operator for a flight they did not
operate, or uses language that asserts illegality rather than requesting
investigation.

**Mitigations:**
- Operator name appears in complaints only when **both** conditions are met:
  1. FAA registered owner-name contains the operator string as a substring, AND
  2. At least one observation places the aircraft departing a known heliport.
  If either condition is false, the complaint refers to the aircraft by
  N-number only.
- All templates use "I believe" and "I respectfully request investigation" —
  never causal or intent language ("violated", "illegally flew").
- CORRECTIONS.md is public; anyone can email corrections.
- Every flight detail page on the dashboard links to the specific raw
  observations that produced it (traceable evidence chain).
- Permitted-hours logic is explicitly marked `# PLACEHOLDER` in code and
  labeled on the dashboard until the actual 2014 zoning approval text is
  reviewed.

**Residual risk:** Disputed ownership records or FAA registry lag (aircraft
sold but not yet re-registered). Mitigated by conservative, factual language
and the two-condition attribution gate.

---

### Complaint spam / accidental rate abuse

**Threat:** A code bug causes hundreds of complaint emails to be sent to a
recipient in a short window.

**Mitigations (Stage 5):**
- Per-recipient daily cap (default 50, configurable lower via secret).
- Per-recipient hourly cap (10).
- Global daily circuit breaker (200 across all recipients combined).
- `UNIQUE(flight_id, recipient_channel)` in the `submissions` table provides
  physical deduplication at the database layer, not just in-memory.
- Dry-run is the default mode. Real submission requires `--live` flag AND
  approval from the `production-complaints` GitHub Environment (required
  reviewer + 5-minute wait timer).
- 24-hour cooldown after any new template version is deployed.

**Residual risk:** Sufficiently creative database corruption could defeat the
dedup constraint. The manual approval gate is the last line of defense.

---

### CI pipeline compromise

**Threat:** A malicious pull request modifies a workflow YAML to escalate
permissions, exfiltrate secrets, or execute arbitrary code with write access.

**Mitigations:**
- No `pull_request_target` triggers anywhere in the repo. Workflow changes
  in PRs from forks run with fork-limited token scope (no secrets access).
- All `actions/` steps pinned to full 40-char commit SHAs, not mutable tags.
  Dependabot is configured to keep SHAs current.
- Explicit `permissions:` block on every workflow; no workflow holds more
  than `contents: write` plus the minimum additional scope needed.
- Complaint workflow (`file_complaints.yml`) is `contents: read` only and
  can only be triggered via `workflow_dispatch` with Environment approval.

**Residual risk:** Compromise of the GitHub-hosted Actions runner itself, or
a supply-chain attack on a pinned dependency at the pinned SHA. These are
outside the threat model of a small civic project.

---

### Operator retaliation

**Threat:** Heliport operators discover the tool and attempt to disrupt it.

**Mitigations:**
- No server to attack. Dashboard is a static site on GitHub Pages; GitHub
  provides DDoS protection.
- No inbound API surface at all — nothing to attack.
- Filer PII is stored only in GitHub Secrets; it does not appear in code,
  the data branch, or the dashboard.

**Residual risk:** Legal threats or cease-and-desist letters. Mitigation is
accurate, conservative, well-sourced, and neutrally-worded claims. This
document does not constitute legal advice.

---

### adsb.fi rate limit violation

**Threat:** Polling too aggressively triggers a ban or Terms of Service
termination.

**Mitigations:**
- 10-second polling interval = 0.1 req/sec; public limit is 1 req/sec
  (10× margin of safety).
- Exponential backoff (starting at 10s, capped at 120s) on 429 responses.
- Fallback to adsb.lol (a separate network) on primary failure rather than
  hammering the same endpoint.
- `User-Agent` header identifies the project by name and repo URL so the
  operator can contact us if needed.

---

## What we are NOT protecting against

- **GitHub infrastructure outages.** No redundant hosting. Acceptable for
  a single-person civic monitoring project.
- **adsb.fi or adsb.lol API changes / shutdowns.** If both sources go away,
  the harvester stops and data collection halts. No mitigation planned.
- **ADS-B coverage gaps.** Some flights may not be picked up if no ADS-B
  receiver is within range. We report what we observe; we do not claim
  completeness.
- **Determined legal action regardless of factual accuracy.** Being correct
  does not prevent a lawsuit. This is a known and accepted risk of civic
  accountability journalism.
